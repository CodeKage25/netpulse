"""Data allowance: how much of the plan is gone, and whether it lasts the month.

A router's monthly counter is an **odometer**, and it does not agree with the billing
cycle. It resets when the firmware feels like it, it resets to zero on a reboot, and it
starts counting on the router's own schedule rather than the day the plan renews. So
usage is measured against a *movable anchor*: the odometer reading when the current
cycle began. Usage is the distance travelled since, and every discontinuity — a reset,
a reboot, a wrap — re-anchors instead of publishing a jump.

The projection answers the question people actually have, which is not "how much have I
used" but "will this last". It is deliberately a plain linear run-rate over the cycle so
far: a cleverer forecast would be more confident without being more right, and this one
can be checked in your head.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta

from netpulse.storage import Store

#: Where the odometer is read from, in preference order. Firmware differs on whether it
#: reports a single total or a download/upload pair.
TOTAL_METRIC = "data.month_total_bytes"
DOWN_METRIC = "data.month_down_bytes"
UP_METRIC = "data.month_up_bytes"

#: Crossed-threshold notifications. 100% is included because a plan does not always cut
#: off — it often just gets slow, and knowing why is the whole point.
THRESHOLDS = (0.5, 0.8, 0.95, 1.0)


def cycle_start(today: date, reset_day: int) -> date:
    """The first day of the billing cycle containing `today`.

    A reset day of 31 in a 30-day month has to land somewhere; it lands on the last day
    the month actually has, which is what carriers do.
    """
    day = max(1, min(31, reset_day))
    first = today.replace(day=1)
    in_month = _clamp_day(first, day)
    if today >= in_month:
        return in_month
    previous = (first - timedelta(days=1)).replace(day=1)
    return _clamp_day(previous, day)


def _clamp_day(month_start: date, day: int) -> date:
    following = (month_start + timedelta(days=31)).replace(day=1)
    last = (following - timedelta(days=1)).day
    return month_start.replace(day=min(day, last))


@dataclass(frozen=True)
class Plan:
    """A data allowance to measure against. Absent means "just tell me what I used"."""

    #: Gigabytes per cycle, as sold. None when the plan is uncapped or unknown.
    limit_gb: float | None = None
    #: Day of the month the allowance renews. Carriers rarely use the 1st.
    reset_day: int = 1

    @property
    def limit_bytes(self) -> float | None:
        return self.limit_gb * 1_000_000_000 if self.limit_gb else None


@dataclass(frozen=True)
class Allowance:
    used_bytes: float
    limit_bytes: float | None
    cycle_start: date
    cycle_end: date
    days_elapsed: float
    days_total: int
    #: Bytes per day so far. None until enough of the cycle has passed to mean anything.
    rate_per_day: float | None
    #: The date the allowance runs out at this rate, or None if it lasts (or no limit).
    exhausted_on: date | None
    #: What the cycle will total at this rate. None when the rate is not yet meaningful.
    projected_bytes: float | None

    @property
    def fraction(self) -> float | None:
        if not self.limit_bytes:
            return None
        return self.used_bytes / self.limit_bytes

    @property
    def on_track(self) -> bool | None:
        """Whether the plan survives the cycle at the current rate."""
        if self.projected_bytes is None or not self.limit_bytes:
            return None
        return self.projected_bytes <= self.limit_bytes


def _odometer(store: Store, source: str, since: datetime) -> list[float]:
    """The odometer series, from whichever pair of fields this firmware publishes.

    Unbounded above: the current position is the whole answer, so excluding a reading
    taken this instant would under-report the cycle by however much just happened.
    """
    totals = store.values(source, TOTAL_METRIC, since)
    if totals:
        return totals
    downs = store.values(source, DOWN_METRIC, since)
    ups = store.values(source, UP_METRIC, since)
    if not downs:
        return []
    # Upload is often absent or shorter; pair by position and treat a missing tail as 0.
    return [down + (ups[i] if i < len(ups) else 0.0) for i, down in enumerate(downs)]


def travelled(readings: list[float]) -> float:
    """Distance covered by an odometer that may have been reset along the way.

    Every backwards step is a reset, not negative usage: the data before it was still
    used, so the segments are summed rather than the endpoints subtracted. Getting this
    wrong reads as a sudden refund of a month's traffic.
    """
    if not readings:
        return 0.0
    total = 0.0
    anchor = readings[0]
    previous = readings[0]
    for reading in readings[1:]:
        if reading < previous:  # the odometer went back: reset, reboot, or wrap
            total += previous - anchor
            anchor = reading
        previous = reading
    return total + previous - anchor


def assess(
    store: Store,
    source: str,
    now: datetime,
    limit_bytes: float | None = None,
    reset_day: int = 1,
) -> Allowance | None:
    """Usage this cycle, and whether it lasts. None when nothing has been recorded."""
    start = cycle_start(now.date(), reset_day)
    end = cycle_start(start + timedelta(days=32), reset_day)
    since = datetime.combine(start, datetime.min.time(), tzinfo=now.tzinfo)

    readings = _odometer(store, source, since)
    if not readings:
        return None
    used = travelled(readings)

    days_total = (end - start).days
    elapsed = max(0.0, (now - since).total_seconds() / 86400)
    # Under six hours of a cycle, a run-rate is noise wearing a decimal point.
    rate = used / elapsed if elapsed >= 0.25 else None

    projected = rate * days_total if rate is not None else None
    exhausted: date | None = None
    if rate and limit_bytes and used < limit_bytes:
        remaining_days = (limit_bytes - used) / rate
        if elapsed + remaining_days <= days_total:
            exhausted = start + timedelta(days=elapsed + remaining_days)
    elif rate and limit_bytes and used >= limit_bytes:
        exhausted = now.date()

    return Allowance(
        used_bytes=used,
        limit_bytes=limit_bytes,
        cycle_start=start,
        cycle_end=end,
        days_elapsed=elapsed,
        days_total=days_total,
        rate_per_day=rate,
        exhausted_on=exhausted,
        projected_bytes=projected,
    )


def crossed(previous_fraction: float | None, fraction: float | None) -> float | None:
    """The highest threshold newly crossed, for a one-time notification.

    Keyed on the crossing rather than the level, so sitting at 82% does not re-announce
    itself every poll, and a cycle rollover back to 0% re-arms every threshold.
    """
    if fraction is None:
        return None
    before = previous_fraction if previous_fraction is not None else 0.0
    passed = [t for t in THRESHOLDS if before < t <= fraction]
    return max(passed) if passed else None


def format_bytes(value: float) -> str:
    """Human units for notification text, where "104857600 bytes" helps nobody."""
    units = ("B", "KB", "MB", "GB", "TB")
    index = 0
    while value >= 1000 and index < len(units) - 1:
        value /= 1000
        index += 1
    return f"{value:.0f} {units[index]}" if value >= 100 else f"{value:.1f} {units[index]}"
