"""A connection quality score people can argue with, graded like Dishylink grades latency.

The weighting is theirs, and deliberate: jitter counts more than the p99 tail because a
predictable 40ms is worth more than a fast-but-spiky link — that is what video calls and
games actually feel.

Weighting a term heavily only helps if the term measures what its name says. Checked
against a real hour, this graded a link with a 20 ms median and 0.26% loss an F, because
"jitter" was the standard deviation of the whole window — which counts slow drift, and
counts a change in how latency is measured as though the network had done it. It now
comes from the probe's own per-poll measurement of one path, and the same hour grades A.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from itertools import pairwise
from statistics import fmean, median

from netpulse.core.storage import Store

WINDOW = timedelta(hours=1)


def _band(value: float, good: float, bad: float) -> float:
    return max(0.0, min(1.0, (bad - value) / (bad - good)))


def _percentile(ordered: list[float], fraction: float) -> float:
    """Nearest rank, which is the definition that keeps p95 inside the data.

    `int(len * fraction)` is one place too high — for a hundred samples it returns the
    96th, and for twenty it returns the maximum and calls it the 95th percentile.
    """
    # Integer arithmetic on purpose: ceil(0.95 * 20) is 20 in binary floating point,
    # which would quietly return the maximum again.
    rank = max(1, -(-round(fraction * 100) * len(ordered) // 100))
    return ordered[min(len(ordered), rank) - 1]


def _variation(store: Store, source: str, since: datetime, until: datetime,
               latencies: list[float]) -> float:
    """How much the link moves, measured over short spans rather than the whole hour.

    The standard deviation of an hour of latency is not jitter: a link that sits at
    20 ms all morning and 60 ms all evening has a large one while every single call on
    it was smooth. It also turns any change in how the figure is measured into a spike
    of apparent jitter — which is precisely what happened here, and graded a 20 ms link
    an F.

    The probe already measures the real thing: the spread within one poll, of one path.
    The median of those is the typical variation. Its outliers are not thrown away, they
    are scored by p95 and p99, and counting the same stall in both places is double
    punishment. Sources with no probe fall back to the movement between consecutive
    readings, which is the same idea at a coarser resolution.
    """
    recorded = store.values(source, "jitter.internet_ms", since, until)
    if recorded:
        return median(recorded)
    if len(latencies) < 2:
        return 0.0
    return fmean(abs(b - a) for a, b in pairwise(latencies))


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
    jitter = _variation(store, source, since, now, latencies)

    # Loss only counts when loss was measured. A source that does not report it has not
    # reported perfection, and scoring it as zero would hand a tenth of the grade to
    # every router that stays quiet on the subject.
    weighted = [
        (0.4, _band(p95, 20, 250)),
        (0.3, _band(jitter, 5, 60)),
        (0.2, _band(p99, 50, 400)),
    ]
    loss = fmean(losses) if losses else 0.0
    if losses:
        weighted.append((0.1, _band(loss, 0, 5)))
    total = sum(weight for weight, _ in weighted)
    score = round(100 * sum(weight * band for weight, band in weighted) / total)
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
