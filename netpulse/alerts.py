"""User-defined alert rules: a threshold, a duration, and a promise not to guess.

Dishylink's alerts are firmware booleans — it fires when the dish says a flag is set,
and there is nothing to configure. That works for one vendor and fails for every other,
so NetPulse takes the opposite approach: a rule is a metric, a bound, and how long the
breach must hold.

Two decisions carry the design.

**A duration is measured in time, not in polls.** "Above 300 ms for two minutes" must
mean two minutes whether the source is polled every second or every thirty, and must
not be satisfied by two readings that happen to sit either side of a ten-minute gap.

**Missing data cannot breach a rule.** Absence is not a high reading. A rule fires only
when the window it is judged over was actually recorded — otherwise a router that stops
answering would trip every "signal too weak" rule at once, and the outage that really
happened would arrive buried under alarms that did not.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from netpulse.model import Severity
from netpulse.storage import Store

#: A rule whose metric was recorded for less of its window than this is not judged.
#: Half is generous on purpose: the cost of a missed alert is higher than the cost of
#: one judged on a slightly thin window, and the duration itself is the real filter.
MIN_COVERAGE = 0.5


@dataclass(frozen=True)
class Rule:
    """One condition. Exactly one of `above` or `below` is set."""

    metric: str
    above: float | None = None
    below: float | None = None
    for_minutes: float = 1.0
    severity: Severity = Severity.WARNING
    #: Which source to watch. Empty means every source that reports the metric.
    source: str = ""
    message: str = ""

    @property
    def key(self) -> str:
        bound = f">{self.above}" if self.above is not None else f"<{self.below}"
        return f"{self.metric}{bound}/{self.for_minutes}m"

    def describe(self, value: float) -> str:
        if self.message:
            return self.message
        bound = self.above if self.above is not None else self.below
        direction = "above" if self.above is not None else "below"
        unit = self.metric.rsplit("_", 1)[-1] if "_" in self.metric else ""
        return (
            f"{self.metric} {direction} {bound:g}{unit} "
            f"for {self.for_minutes:g} min (now {value:g}{unit})"
        )

    def breached_by(self, value: float) -> bool:
        if self.above is not None:
            return value > self.above
        if self.below is not None:
            return value < self.below
        return False


@dataclass(frozen=True)
class Firing:
    rule: Rule
    source: str
    value: float
    since: datetime


def evaluate(
    store: Store, rule: Rule, source: str, now: datetime, interval_s: float
) -> tuple[bool, float | None]:
    """Whether the rule is breached for its full duration, and the current value.

    Returns `(False, None)` when the window is too sparse to judge — a different answer
    from "not breached", and the caller treats it as such.
    """
    window = timedelta(minutes=rule.for_minutes)
    since = now - window
    values = store.values(source, rule.metric, since)
    if not values:
        return False, None

    # Density of *this metric*, not of the source's heartbeat: a router can answer every
    # poll while reporting no signal field, and "we were polling" is not the same claim
    # as "this was measured". Judged on time rather than on a count of readings, so a
    # slow poller and a fast one agree, and two readings either side of a gap cannot
    # satisfy a two-minute rule between them.
    expected = max(1, int(window.total_seconds() / interval_s))
    if len(values) / expected < MIN_COVERAGE:
        return False, values[-1]

    return all(rule.breached_by(value) for value in values), values[-1]


class AlertEngine:
    """Holds which rules are currently firing, per source.

    Firing is derived from the store on every check rather than from a remembered edge,
    so a restart re-derives the same answer instead of re-announcing every open alert or
    losing track of one. What is remembered is only *when* a firing began, which the
    store cannot say.
    """

    def __init__(self, rules: list[Rule], interval_s: float = 5.0) -> None:
        self.rules = rules
        self.interval_s = interval_s
        self._since: dict[tuple[str, str], datetime] = {}

    def check(self, store: Store, source: str, now: datetime) -> tuple[list[Firing], list[Firing]]:
        """Returns (newly firing, newly cleared) for this source."""
        started: list[Firing] = []
        cleared: list[Firing] = []
        for rule in self.rules:
            if rule.source and rule.source != source:
                continue
            identity = (source, rule.key)
            breached, value = evaluate(store, rule, source, now, self.interval_s)
            was_firing = identity in self._since

            if breached and not was_firing:
                self._since[identity] = now
                started.append(Firing(rule, source, value or 0.0, now))
            elif not breached and was_firing:
                began = self._since.pop(identity)
                # Clearing is immediate and unconditional. Waiting to see whether it
                # recovers would leave the dashboard saying "fine" while the alert list
                # still said otherwise, and flap control belongs in the notifier.
                cleared.append(Firing(rule, source, value or 0.0, began))
        return started, cleared

    def firing(self) -> list[tuple[str, str, datetime]]:
        return [(source, key, since) for (source, key), since in self._since.items()]


def parse_rules(raw: object) -> list[Rule]:
    """Read rules from config, skipping any that cannot mean anything.

    A rule with no bound, or with both, is dropped rather than guessed at: silently
    picking one would enforce a condition nobody wrote.
    """
    if not isinstance(raw, list):
        return []
    rules: list[Rule] = []
    for entry in raw:
        if not isinstance(entry, dict) or not entry.get("metric"):
            continue
        above = entry.get("above")
        below = entry.get("below")
        if (above is None) == (below is None):
            continue
        named = str(entry.get("severity", "warning")).lower()
        severity = next((s for s in Severity if s.value == named), Severity.WARNING)
        rules.append(
            Rule(
                metric=str(entry["metric"]),
                above=float(above) if above is not None else None,
                below=float(below) if below is not None else None,
                for_minutes=float(entry.get("for_minutes", 1.0)),
                severity=severity,
                source=str(entry.get("source", "")),
                message=str(entry.get("message", "")),
            )
        )
    return rules
