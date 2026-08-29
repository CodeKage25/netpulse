"""On-demand speed test. On demand *only*, and loud about the cost.

A speed test moves real data — ~30 MB per run at the defaults — and most of the
connections NetPulse watches are metered. Nothing here ever runs on a schedule; a person
asks, is told the cost, and the result is recorded so history can chart it.
"""

from __future__ import annotations

import time
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass

from netpulse.core.storage import Store

DOWNLOAD_URL = "https://speed.cloudflare.com/__down?bytes={size}"
UPLOAD_URL = "https://speed.cloudflare.com/__up"
DOWN_BYTES = 25_000_000
UP_BYTES = 8_000_000
COST_NOTE = f"moves about {(DOWN_BYTES + UP_BYTES) / 1e6:.0f} MB of real data"


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


def _download(size: int) -> float:
    started = time.perf_counter()
    received = 0
    with urllib.request.urlopen(DOWNLOAD_URL.format(size=size), timeout=60) as response:
        while chunk := response.read(65536):
            received += len(chunk)
    return received / max(1e-6, time.perf_counter() - started)


def _upload(size: int) -> float:
    payload = b"\0" * size
    request = urllib.request.Request(
        UPLOAD_URL, data=payload, headers={"Content-Type": "application/octet-stream"}
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
    up = upload(UP_BYTES)
    result = SpeedResult(down_bytes_s=down, up_bytes_s=up, seconds=time.perf_counter() - started)
    store.record(
        source,
        {"speedtest.down_bytes_s": result.down_bytes_s, "speedtest.up_bytes_s": result.up_bytes_s},
    )
    return result
