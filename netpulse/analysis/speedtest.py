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
COST_NOTE = f"moves up to {(DOWN_BYTES + UP_BYTES) / 1e6:.0f} MB of real data, less on a slow link"


@dataclass(frozen=True)
class SpeedResult:
    down_bytes_s: float
    up_bytes_s: float
    seconds: float

    @property
    def down_mbps(self) -> float:
        return self.down_bytes_s * 8 / 1e6

    @property
    def up_mbps(self) -> float:
        return self.up_bytes_s * 8 / 1e6


class TestHostUnavailable(Exception):
    """No measurement host would answer — which is not a statement about the link.

    Distinct from a connection failure on purpose. Reporting a third party's rate limit
    as "your connection is down" is exactly the error a monitor exists to prevent.
    """


def _download(size: int) -> float:
    refusals: list[str] = []
    for template in DOWNLOAD_URLS:
        started = time.perf_counter()
        received = 0
        request = urllib.request.Request(template.format(size=size), headers=HEADERS)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                while chunk := response.read(65536):
                    received += len(chunk)
                    if time.perf_counter() - started >= MAX_SECONDS_PER_DIRECTION:
                        break  # enough to measure the rate; the rest costs data
        except urllib.error.HTTPError as exc:
            # The host answered and said no. Try the next one rather than blaming the
            # connection that just successfully carried the refusal.
            refusals.append(f"{urlsplit(template).netloc}: HTTP {exc.code}")
            continue
        except OSError as exc:
            refusals.append(f"{urlsplit(template).netloc}: {exc}")
            continue
        if received:
            return received / max(1e-6, time.perf_counter() - started)
    raise TestHostUnavailable("; ".join(refusals) or "no host answered")


def _upload(size: int) -> float:
    """Upload cannot be cut short mid-body without corrupting the request, so the size
    is chosen from what download just measured rather than the deadline enforced."""
    payload = b"\0" * size
    request = urllib.request.Request(
        UPLOAD_URL,
        data=payload,
        headers={**HEADERS, "Content-Type": "application/octet-stream"},
    )
    started = time.perf_counter()
    with urllib.request.urlopen(request, timeout=60):
        pass
    return size / max(1e-6, time.perf_counter() - started)


def run_speedtest(
    store: Store,
    source: str,
    *,
    download: Callable[[int], float] = _download,
    upload: Callable[[int], float] = _upload,
) -> SpeedResult:
    started = time.perf_counter()
    down = download(DOWN_BYTES)
    # Size the upload from what download just measured, so a slow link is not asked to
    # push eight megabytes it would spend a minute on. An upload cannot be cut short
    # mid-body without corrupting the request, so it has to be sized before it starts.
    budget = int(min(UP_BYTES, max(500_000, down * MAX_SECONDS_PER_DIRECTION)))
    up = upload(budget)
    result = SpeedResult(down_bytes_s=down, up_bytes_s=up, seconds=time.perf_counter() - started)
    store.record(
        source,
        {"speedtest.down_bytes_s": result.down_bytes_s, "speedtest.up_bytes_s": result.up_bytes_s},
    )
    return result
