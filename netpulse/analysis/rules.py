"""Data rules: an allowance, a timetable, or a countdown — and what they say to do.

Three kinds of limit, because people mean three different things by "limit this
device". An allowance is a quantity. A schedule is a set of hours. A timer is a stretch
of time from now. They share a cycle, a subject and an action, and nothing else.

Two decisions carry the design, both learned from watching Dishylink get them right.

**Enforcement is derived from state on every evaluation, never from a remembered
edge.** A rule does not "fire"; at any moment it either holds a device or it does not,
and the answer is recomputed. An edge-triggered design loses its mind the first time a
poll is missed, a process restarts, or someone unblocks a device by hand.

**A timetable is not a quantity.** Restarting an allowance gives you more data;
restarting a schedule would open hours the rule exists to close. So "start over" resets
what was spent and never what the clock says.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from enum import StrEnum

from netpulse.core.storage import Store

#: A countdown longer than this is a schedule badly expressed, and a countdown someone
#: has forgotten they set.
MAX_TIMER_HOURS = 24.0


class Cycle(StrEnum):
    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"
    ONCE = "once"


class Kind(StrEnum):
    LIMIT = "limit"
    SCHEDULE = "schedule"
    TIMER = "timer"


@dataclass(frozen=True)
class Window:
    """One stretch of the clock, on chosen weekdays. May cross midnight."""

    start: time
    end: time
    #: 0 = Monday, matching date.weekday(). Empty means every day.
    weekdays: frozenset[int] = frozenset()

    def holds(self, moment: datetime) -> bool:
        if self.weekdays and moment.weekday() not in self.weekdays:
            # A window that crosses midnight belongs to the day it *started* on, so
            # Friday 22:00 to 02:00 still holds at one on Saturday morning.
            started_yesterday = (moment.weekday() - 1) % 7 in self.weekdays
            if not (self.crosses_midnight and started_yesterday):
                return False
        clock = moment.time()
        if self.crosses_midnight:
            return clock >= self.start or clock < self.end
        return self.start <= clock < self.end

    @property
    def crosses_midnight(self) -> bool:
        return self.end <= self.start


@dataclass(frozen=True)
class Rule:
    name: str
    kind: Kind
    #: MAC addresses this applies to. More than one means a group.
    devices: tuple[str, ...]
    #: LIMIT only: bytes per cycle.
    limit_bytes: float | None = None
    cycle: Cycle = Cycle.MONTHLY
    #: MONTHLY: day of month. WEEKLY: weekday. Ignored otherwise.
    cycle_day: int = 1
    #: SCHEDULE only.
    windows: tuple[Window, ...] = ()
    #: TIMER only: how long from `started_at`.
    duration: timedelta = timedelta(hours=1)
    started_at: datetime | None = None
    #: Whether members share one allowance or each get the whole thing.
    pooled: bool = False
    #: What to do when the rule holds. Reporting only, unless explicitly told to act.
    block: bool = False
    enabled: bool = True


def cycle_start(kind: Cycle, day: int, now: datetime) -> datetime:
    """When the current cycle began. Local dates throughout, deliberately.

    Every boundary is built from date parts rather than by subtracting milliseconds,
    which is what keeps a window set for 4pm at 4pm across a daylight-saving change.
    """
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if kind is Cycle.DAILY:
        return midnight
    if kind is Cycle.WEEKLY:
        wanted = max(0, min(6, day))
        return midnight - timedelta(days=(now.weekday() - wanted) % 7)
    if kind is Cycle.MONTHLY:
        # A rule set to the 31st still rolls in a month that has thirty days.
        first = midnight.replace(day=1)
        last_day = ((first + timedelta(days=32)).replace(day=1) - timedelta(days=1)).day
        anchor = first.replace(day=min(max(1, day), last_day))
        if now >= anchor:
            return anchor
        previous = (first - timedelta(days=1)).replace(day=1)
        last_prev = ((previous + timedelta(days=32)).replace(day=1) - timedelta(days=1)).day
        return previous.replace(day=min(max(1, day), last_prev))
    return midnight  # ONCE: the day it was written


@dataclass(frozen=True)
class Verdict:
    """What a rule says right now, and why."""

    rule: Rule
    holds: bool
    reason: str
    #: LIMIT only.
    used_bytes: float = 0.0
    remaining_bytes: float | None = None
    #: Devices this verdict applies to — the whole group when pooled.
    devices: tuple[str, ...] = ()
    #: LIMIT only: whether any covered device actually appeared in the usage table this
    #: cycle. Enforcement treats an unseen device as having spent nothing — refusing a
    #: device its network because nobody measured it would be the worse mistake — but a
    #: panel must not then draw an empty bar and call it zero.
    measured: bool = False


def _used(
    store: Store, source: str, macs: tuple[str, ...], since: datetime
) -> tuple[float, bool]:
    """Bytes attributed to these devices since the cycle began, and whether any of them
    were seen at all.

    Reads from recorded usage rather than a live counter, so a rule survives a restart
    with the same answer it had before — and cannot be reset by rebooting the router.
    """
    totals = dict(
        (key.upper(), down + up) for key, down, up in store.usage_by_key(source, "device", since)
    )
    seen = [totals[mac.upper()] for mac in macs if mac.upper() in totals]
    return sum(seen), bool(seen)


def evaluate(store: Store, source: str, rule: Rule, now: datetime) -> list[Verdict]:
    """What this rule says about each device it covers.

    Derived entirely from stored state and the clock. Calling it twice with the same
    inputs gives the same answer, which is what lets enforcement be a reconciliation
    rather than a sequence of events that must not be missed.
    """
    if not rule.enabled:
        return [Verdict(rule, False, "disabled", devices=rule.devices)]

    if rule.kind is Kind.SCHEDULE:
        holding = any(window.holds(now) for window in rule.windows)
        return [
            Verdict(
                rule,
                holding,
                "inside a scheduled window" if holding else "outside its windows",
                devices=rule.devices,
            )
        ]

    if rule.kind is Kind.TIMER:
        if rule.started_at is None:
            return [Verdict(rule, False, "not started", devices=rule.devices)]
        capped = min(rule.duration, timedelta(hours=MAX_TIMER_HOURS))
        left = (rule.started_at + capped) - now
        if left.total_seconds() > 0:
            minutes = int(left.total_seconds() // 60)
            return [Verdict(rule, True, f"{minutes} min left", devices=rule.devices)]
        return [Verdict(rule, False, "finished", devices=rule.devices)]

    # LIMIT
    since = cycle_start(rule.cycle, rule.cycle_day, now)
    limit = rule.limit_bytes or 0.0
    if rule.pooled:
        # One budget for the group. Derived on every read and never stored, so no member
        # can hold a stale copy of what the others have spent.
        used, measured = _used(store, source, rule.devices, since)
        over = used >= limit
        return [
            Verdict(
                rule,
                over,
                f"{used / max(limit, 1) * 100:.0f}% of a shared allowance"
                if measured
                else "nothing recorded for this group yet",
                used,
                max(0.0, limit - used),
                rule.devices,
                measured,
            )
        ]
    verdicts = []
    for mac in rule.devices:
        used, measured = _used(store, source, (mac,), since)
        over = used >= limit
        verdicts.append(
            Verdict(
                rule,
                over,
                f"{used / max(limit, 1) * 100:.0f}% of its allowance"
                if measured
                else "nothing recorded yet",
                used,
                max(0.0, limit - used),
                (mac,),
                measured,
            )
        )
    return verdicts


def held_devices(verdicts: list[Verdict]) -> set[str]:
    """Every device any rule currently holds, uppercased."""
    return {
        mac.upper()
        for verdict in verdicts
        if verdict.holds and verdict.rule.block
        for mac in verdict.devices
    }


def reconcile(
    verdicts: list[Verdict], currently_blocked: set[str], overridden: set[str] | None = None
) -> tuple[set[str], set[str]]:
    """(to block, to unblock) — the difference between what should be and what is.

    Recomputed from state every time rather than remembered. A device someone unblocked
    by hand is listed again unless it is in `overridden`, because the rule still holds
    it and pretending otherwise would leave the two disagreeing silently.
    """
    wanted = held_devices(verdicts) - (overridden or set())
    have = {mac.upper() for mac in currently_blocked}
    # Only rules that ask to block release anything: a device blocked by hand is not
    # something a reporting-only rule should quietly undo.
    governed = {mac.upper() for v in verdicts if v.rule.block for mac in v.devices}
    return wanted - have, (have - wanted) & governed


def parse_rules(raw: object) -> list[Rule]:
    """Read rules from config, dropping any that cannot mean anything.

    A malformed rule is skipped rather than guessed at: a rule that blocks devices is
    the last place to infer what somebody probably meant.
    """
    if not isinstance(raw, list):
        return []
    rules: list[Rule] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        devices = entry.get("devices") or ([entry["device"]] if entry.get("device") else [])
        if not isinstance(devices, list) or not devices:
            continue
        named = str(entry.get("kind", "limit")).lower()
        kind = next((k for k in Kind if k.value == named), None)
        if kind is None:
            continue

        limit_gb = entry.get("limit_gb")
        if kind is Kind.LIMIT and not limit_gb:
            continue  # an allowance with no amount is not an allowance
        windows = _parse_windows(entry.get("windows"))
        if kind is Kind.SCHEDULE and not windows:
            continue  # a timetable with no hours would hold nothing, always

        cycle_named = str(entry.get("cycle", "monthly")).lower()
        rules.append(
            Rule(
                name=str(entry.get("name") or ",".join(str(d) for d in devices)),
                kind=kind,
                devices=tuple(str(device).upper() for device in devices),
                limit_bytes=float(limit_gb) * 1_000_000_000 if limit_gb else None,
                cycle=next((c for c in Cycle if c.value == cycle_named), Cycle.MONTHLY),
                cycle_day=int(entry.get("cycle_day", 1)),
                windows=windows,
                duration=timedelta(hours=float(entry.get("hours", 1))),
                pooled=bool(entry.get("pooled", False)),
                # Blocking is opt-in per rule. A rule that reports is useful on its own,
                # and turning every allowance into an enforcement action by default
                # would be a surprising thing to do to somebody's network.
                block=bool(entry.get("block", False)),
                enabled=bool(entry.get("enabled", True)),
            )
        )
    return rules


def _parse_windows(raw: object) -> tuple[Window, ...]:
    if not isinstance(raw, list):
        return ()
    found: list[Window] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        try:
            start = time.fromisoformat(str(entry["from"]))
            end = time.fromisoformat(str(entry["to"]))
        except (KeyError, ValueError):
            continue
        days = entry.get("weekdays")
        found.append(
            Window(
                start,
                end,
                frozenset(int(day) for day in days) if isinstance(days, list) else frozenset(),
            )
        )
    return tuple(found)
