"""A believable connection that exists only in memory.

Two jobs: `netpulse run --demo` shows a live dashboard to someone on no interesting
network at all, and the test suite gets an adapter with scriptable behaviour. The demo
traces an evening on a real LTE link: steady baseline, a congestion window, one outage.
"""

from __future__ import annotations

import math
import random

from netpulse.adapters import AdapterError
from netpulse.model import Reading


class DemoAdapter:
    kind = "demo"

    def __init__(self, name: str, seed: int = 7) -> None:
        self.name = name
        self._random = random.Random(seed)
        self._tick = 0

    def read(self) -> Reading:
        self._tick += 1
        tick, rng = self._tick, self._random

        # A short outage partway in, so events and gap handling have something to show.
        if 180 <= tick < 192:
            raise AdapterError("demo outage")

        congested = 300 <= tick < 420
        wave = math.sin(tick / 45)
        base = 68 + 14 * wave + rng.gauss(0, 6)
        spike = rng.random() < (0.12 if congested else 0.03)
        latency = base * (3.5 if spike else 1.0) + (60 if congested else 0)

        down = max(0.5, (24 if not congested else 7) + 6 * wave + rng.gauss(0, 3))
        sinr = 9 + 4 * math.sin(tick / 120) + rng.gauss(0, 1.2) - (5 if congested else 0)

        return Reading(
            metrics={
                "latency.internet_ms": round(latency, 1),
                "latency.gateway_ms": round(2.2 + rng.gauss(0, 0.5), 1),
                "dns.lookup_ms": round(18 + rng.gauss(0, 5) + (25 if spike else 0), 1),
                "jitter.internet_ms": round(abs(rng.gauss(4, 2)) + (9 if congested else 0), 1),
                "loss.pct": 25.0 if spike and congested else 0.0,
                "traffic.down_bytes_s": round(down * 125_000),
                "traffic.up_bytes_s": round(down * 125_000 / 8),
                "signal.rsrp_dbm": round(-96 + 3 * wave + rng.gauss(0, 1), 1),
                "signal.rsrq_db": round(-9 + rng.gauss(0, 0.8), 1),
                "signal.sinr_db": round(sinr, 1),
                "data.month_down_bytes": 9_500_000_000 + tick * 3_000_000,
                "data.month_up_bytes": 1_200_000_000 + tick * 400_000,
                "up": 1.0,
            },
            texts={
                "net.type": "LTE",
                "net.operator": "Demo Mobile",
                "signal.band": "B3",
                "net.gateway": "192.168.8.1",
            },
        )


class ScriptedAdapter:
    """Tests hand it a list of Readings and exceptions; it plays them back."""

    kind = "scripted"

    def __init__(self, name: str, script: list[Reading | Exception]) -> None:
        self.name = name
        self._script = list(script)

    def read(self) -> Reading:
        if not self._script:
            raise AdapterError("script exhausted")
        step = self._script.pop(0)
        if isinstance(step, Exception):
            raise step
        return step
