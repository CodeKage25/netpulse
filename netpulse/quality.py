"""A connection quality score people can argue with, graded like Dishylink grades latency.

The weighting is theirs, and deliberate: jitter counts more than the p99 tail because a
predictable 40ms is worth more than a fast-but-spiky link — that is what video calls and
games actually feel.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from statistics import pstdev

from netpulse.storage import Store

WINDOW = timedelta(hours=1)


def _band(value: float, good: float, bad: float) -> float:
    return max(0.0, min(1.0, (bad - value) / (bad - good)))


def _percentile(ordered: list[float], fraction: float) -> float:
    return ordered[min(len(ordered) - 1, int(len(ordered) * fraction))]


@dataclass(frozen=True)
class Quality:
    score: int
    grade: str
    p50_ms: float
    p95_ms: float
    p99_ms: float
    jitter_ms: float
    loss_pct: float


def assess(store: Store, source: str, now: datetime) -> Quality | None:
    """Graded from the raw last hour; None until there is enough to be fair about."""
    since = now - WINDOW
    latencies = store.values(source, "latency.internet_ms", since, now)
    if len(latencies) < 12:
        return None
    losses = store.values(source, "loss.pct", since, now)

    ordered = sorted(latencies)
    p50 = _percentile(ordered, 0.50)
    p95 = _percentile(ordered, 0.95)
    p99 = _percentile(ordered, 0.99)
    jitter = pstdev(latencies)
    loss = sum(losses) / len(losses) if losses else 0.0

    score = round(
        100
        * (
            0.4 * _band(p95, 20, 250)
            + 0.3 * _band(jitter, 5, 60)
            + 0.2 * _band(p99, 50, 400)
            + 0.1 * _band(loss, 0, 5)
        )
    )
    grade = (
        "A"
        if score >= 90
        else "B"
        if score >= 75
        else "C"
        if score >= 60
        else "D"
        if score >= 40
        else "F"
    )
    return Quality(
        score=score,
        grade=grade,
        p50_ms=round(p50, 1),
        p95_ms=round(p95, 1),
        p99_ms=round(p99, 1),
        jitter_ms=round(jitter, 1),
        loss_pct=round(loss, 2),
    )
