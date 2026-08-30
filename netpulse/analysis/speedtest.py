"""On-demand speed test. On demand *only*, and loud about the cost.

A speed test moves real data — ~30 MB per run at the defaults — and most of the
connections NetPulse watches are metered. Nothing here ever runs on a schedule; a person
asks, is told the cost, and the result is recorded so history can chart it.
"""

from __future__ import annotations

import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urlsplit

from netpulse.core.storage import Store

#: Download targets, tried in order. More than one on purpose: a single third-party
#: host that rate-limits turns "the test host refused us" into "your connection is
#: broken", which is the one lie a monitoring tool must never tell. Cloudflare was
#: observed returning 403 to identical requests twenty minutes apart.
DOWNLOAD_URLS = (
    "https://speed.cloudflare.com/__down?bytes={size}",
    "https://cachefly.cachefly.net/10mb.test",
    "https://proof.ovh.net/files/10Mb.dat",
)
UPLOAD_URL = "https://speed.cloudflare.com/__up"

#: Cloudflare refuses requests with no User-Agent — 403, not a network error, so it
#: presents as "your connection is broken" when the connection is fine. urllib sends
#: `Python-urllib/3.x` by default and that is what gets refused, so every request here
#: names itself.
HEADERS = {"User-Agent": "netpulse/monitor (+https://github.com/CodeKage25/netpulse)"}
DOWN_BYTES = 25_000_000
UP_BYTES = 8_000_000
#: Stop each direction here however much is left. A fixed byte budget is unbounded in
#: time: on a 2 Mbps link 25 MB takes a hundred seconds, and the run is measured after
#: about ten anyway. Whatever arrived by the deadline is the sample — a partial
#: transfer measures the rate just as well as a whole one.
MAX_SECONDS_PER_DIRECTION = 12.0
#: A single socket read may block this long. It bounds the overrun, because the deadline
#: above can only be checked between reads: without it one stalled read took a run that
#: was supposed to stop at twelve seconds out to thirty-nine.
READ_TIMEOUT = 6.0
#: Throughput is reported as the best rate sustained over a window this long, rather than
#: the average across the whole transfer. Capacity is a maximum, and an average is
#: dragged down by two things that are not the link: TCP's slow start at the beginning,
#: and any stall at the far end. Measured back to back on one unchanged connection, the
#: average swung between 4.6 and 61 Mbps.
RATE_WINDOW_S = 2.0
COST_NOTE = f"moves up to {(DOWN_BYTES + UP_BYTES) / 1e6:.0f} MB of real data, less on a slow link"


@dataclass(frozen=True)
class SpeedResult:
    down_bytes_s: float
    #: None when no upload host would accept the transfer. Absent, not zero — a link
    #: nobody would let us push data to has not been measured at zero.
    up_bytes_s: float | None
    seconds: float

    @property
    def down_mbps(self) -> float:
        return self.down_bytes_s * 8 / 1e6

    @property
    def up_mbps(self) -> float | None:
        return None if self.up_bytes_s is None else self.up_bytes_s * 8 / 1e6


class TestHostUnavailable(Exception):
    """No measurement host would answer — which is not a statement about the link.

    Distinct from a connection failure on purpose. Reporting a third party's rate limit
    as "your connection is down" is exactly the error a monitor exists to prevent.
    """


def best_rate(samples: list[tuple[float, int]], window: float = RATE_WINDOW_S) -> float:
    """The fastest rate sustained over any `window` of a transfer.

    `samples` is (elapsed seconds, cumulative bytes), in order. The answer is the
    steepest line between two points at least `window` apart — which is what a link's
    capacity means, as opposed to the average, which also measures how long TCP took to
    get going and how long the far end paused in the middle.

    Falls back to the overall average when the transfer was shorter than one window,
    because on a slow link that is the whole measurement and refusing to report it would
    be worse than reporting it with its own limitation.
    """
    if len(samples) < 2:
        return 0.0
    total_time, total_bytes = samples[-1]
    fallback = total_bytes / total_time if total_time > 0 else 0.0
    best = 0.0
    start = 0
    for end in range(1, len(samples)):
        while samples[end][0] - samples[start][0] > window and start < end - 1:
            start += 1
        span = samples[end][0] - samples[start][0]
        if span >= window:
            best = max(best, (samples[end][1] - samples[start][1]) / span)
    return best or fallback


def _download(size: int) -> float:
    refusals: list[str] = []
    for template in DOWNLOAD_URLS:
        request = urllib.request.Request(template.format(size=size), headers=HEADERS)
        try:
            with urllib.request.urlopen(request, timeout=READ_TIMEOUT) as response:
                first = response.read(65536)
                # The clock starts at the first byte. Name resolution, the TCP handshake
                # and TLS are real time, but they are latency rather than throughput, and
                # on a twelve second budget counting them understates a fast link badly.
                started = time.perf_counter()
                received = len(first)
                samples = [(0.0, received)]
                while first:
                    elapsed = time.perf_counter() - started
                    if elapsed >= MAX_SECONDS_PER_DIRECTION:
                        break  # enough to measure the rate; the rest costs real data
                    first = response.read(65536)
                    received += len(first)
                    samples.append((time.perf_counter() - started, received))
        except urllib.error.HTTPError as exc:
            # The host answered and said no. Try the next one rather than blaming the
            # connection that just successfully carried the refusal.
            refusals.append(f"{urlsplit(template).netloc}: HTTP {exc.code}")
            continue
        except OSError as exc:
            refusals.append(f"{urlsplit(template).netloc}: {exc}")
            continue
        if received:
            return best_rate(samples)
    raise TestHostUnavailable("; ".join(refusals) or "no host answered")


def _upload(size: int) -> float | None:
    """Upload cannot be cut short mid-body without corrupting the request, so the size
    is chosen from what download just measured rather than the deadline enforced.

    Returns None when no host would take it. There is exactly one public endpoint here
    against three for download, so a refusal is markedly more likely in this direction —
    and failing the whole run over it would throw away a download figure that was
    measured successfully seconds earlier. A test that reports one direction and says
    the other could not be measured is more use than no test at all.
    """
    payload = b"\0" * size
    request = urllib.request.Request(
        UPLOAD_URL,
        data=payload,
        headers={**HEADERS, "Content-Type": "application/octet-stream"},
    )
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=60):
            pass
    except (OSError, urllib.error.HTTPError):
        return None
    return size / max(1e-6, time.perf_counter() - started)


def run_speedtest(
    store: Store,
    source: str,
    *,
    download: Callable[[int], float] = _download,
    upload: Callable[[int], float | None] = _upload,
) -> SpeedResult:
    started = time.perf_counter()
    down = download(DOWN_BYTES)
    # Size the upload from what download just measured, so a slow link is not asked to
    # push eight megabytes it would spend a minute on. An upload cannot be cut short
    # mid-body without corrupting the request, so it has to be sized before it starts.
    budget = int(min(UP_BYTES, max(500_000, down * MAX_SECONDS_PER_DIRECTION)))
    up = upload(budget)
    result = SpeedResult(down_bytes_s=down, up_bytes_s=up, seconds=time.perf_counter() - started)
    # An unmeasured direction records nothing rather than a zero. A zero here would be
    # charted as a link that managed no upload at all, and would drag every average
    # taken over it down towards a speed nothing ever ran at.
    recorded = {"speedtest.down_bytes_s": result.down_bytes_s}
    if result.up_bytes_s is not None:
        recorded["speedtest.up_bytes_s"] = result.up_bytes_s
    store.record(source, recorded)
    return result
