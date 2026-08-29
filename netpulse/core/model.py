from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum


@dataclass(frozen=True, slots=True)
class DeviceSeen:
    """One client the router reports on its network."""

    mac: str
    name: str = ""
    ip: str = ""


@dataclass(frozen=True, slots=True)
class Reading:
    """One successful poll of one source.

    ``metrics`` are numeric and chartable; ``texts`` are labels (network type, band,
    operator) that change rarely and are stored only when they do; ``devices`` is the
    router's client list, present only on the cycles an adapter chose to fetch it.
    """

    metrics: dict[str, float] = field(default_factory=dict)
    texts: dict[str, str] = field(default_factory=dict)
    devices: list[DeviceSeen] | None = None


class Agg(StrEnum):
    """How a metric survives downsampling. The registry decides, not the call site."""

    MAX = "max"  # latency, loss: a spike averaged away is a lie
    MEAN = "mean"  # signal levels, rates
    LAST = "last"  # counters and gauges that already accumulate
    MIN = "min"


#: metric name prefix -> how to bucket it. Longest prefix wins; default MEAN.
AGG_RULES: dict[str, Agg] = {
    "latency.": Agg.MAX,
    "loss.": Agg.MAX,
    "dns.": Agg.MAX,
    "jitter.": Agg.MAX,
    "signal.": Agg.MEAN,
    "traffic.": Agg.MEAN,
    "data.": Agg.LAST,
    "devices.": Agg.LAST,
    "speedtest.": Agg.LAST,
    "router.": Agg.LAST,  # uptime is an odometer; a mean of it means nothing
    "up": Agg.MIN,  # a bucket that saw any failure shows as down
}


def agg_for(metric: str) -> Agg:
    best = Agg.MEAN
    best_len = -1
    for prefix, agg in AGG_RULES.items():
        if metric.startswith(prefix) and len(prefix) > best_len:
            best, best_len = agg, len(prefix)
    return best


class Severity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class EventKind(StrEnum):
    OUTAGE = "outage"
    DEGRADED = "degraded"
    #: A user-defined threshold rule was breached for its full duration.
    ALERT = "alert"


@dataclass(frozen=True, slots=True)
class Event:
    id: int
    source: str
    kind: EventKind
    severity: Severity
    started_at: datetime
    ended_at: datetime | None
    detail: str

    @property
    def open(self) -> bool:
        return self.ended_at is None


@dataclass(frozen=True, slots=True)
class Insight:
    """A diagnosis with its evidence. Deterministic; an LLM may narrate it, never invent it."""

    rule: str
    severity: Severity
    title: str
    detail: str
    evidence: dict[str, float | str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Coverage:
    """What fraction of a window was actually sampled. Charts and totals must show this."""

    sampled: int
    expected: int

    @property
    def fraction(self) -> float:
        return min(1.0, self.sampled / self.expected) if self.expected else 0.0
