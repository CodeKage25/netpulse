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

from netpulse.core.storage import Store

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
    #: Of `used_bytes`, how much NetPulse watched happen. The rest is the router's own
    #: count from before it was being recorded — complete, but not ours.
    observed_bytes: float = 0.0

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


@dataclass(frozen=True)
class Cycle:
    """One run of the router's own counter, from a reset to the next one.

    Cycles are found in the readings rather than imposed from the calendar, because the
    router's month is whatever the router says it is. This one rolled over at 03:05 on
    the first, not at midnight, and a report that assumed midnight would have put the
    last three hours of August on the wrong side of the line.
    """

    used_bytes: float
    #: The first reading of this run — the reset that started it, or where NetPulse
    #: happened to arrive when the cycle was already under way.
    watched_from: datetime
    #: When the reset that closed this cycle was observed. None while it still runs.
    ended_at: datetime | None
    #: Whether NetPulse saw the cycle end. When it did, `used_bytes` is the router's own
    #: final count. When it did not, the cycle ended inside a gap and this is only the
    #: highest reading anybody saw — a floor, not a total.
    saw_the_end: bool
    #: Whether NetPulse saw the cycle begin. Almost always false for the oldest one, and
    #: it matters far less than it looks: the counter was running the whole time
    #: regardless, so the figure still covers the days nobody watched. That is the whole
    #: reason for reading a counter instead of summing observations.
    saw_the_start: bool

    @property
    def complete(self) -> bool:
        return self.ended_at is not None


#: A drop below this fraction of the running peak is a reset rather than noise. An
#: odometer does not go backwards on its own, so almost any decrease qualifies; the
#: margin exists only so firmware that recomputes and lands a byte lower does not get
#: treated as the start of a new month.
RESET_RATIO = 0.95


def cycles(store: Store, source: str, since: datetime, now: datetime) -> list[Cycle]:
    """Every run of the odometer between resets, oldest first.

    This is what answers "how much did I use last month" on a connection NetPulse has
    only just started watching. The counter was running long before this tool was, so
    its value at the moment the month rolled over is the month's total — including every
    day nobody observed. Summing what NetPulse itself recorded would answer a much
    smaller question while looking like an answer to this one.
    """
    readings = store.stamped(source, TOTAL_METRIC, since, now)
    if not readings:
        return []

    found: list[Cycle] = []
    run_start = readings[0][0]
    peak = readings[0][1]
    saw_start = False
    for at, value in readings[1:]:
        if value < peak * RESET_RATIO:
            found.append(
                Cycle(
                    used_bytes=peak,
                    watched_from=run_start,
                    # The boundary lies somewhere between the previous reading and this
                    # one. Reporting when the reset was *observed* is the honest version:
                    # it is a fact about the recording rather than a guess at the event.
                    ended_at=at,
                    saw_the_end=True,
                    saw_the_start=saw_start,
                )
            )
            run_start, peak, saw_start = at, value, True
            continue
        peak = max(peak, value)
    found.append(
        Cycle(
            used_bytes=peak,
            watched_from=run_start,
            ended_at=None,
            saw_the_end=False,
            saw_the_start=saw_start,
        )
    )
    return found


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


def travelled(readings: list[float], anchor: float | None = None) -> float:
    """Distance covered by an odometer that may have been reset along the way.

    Every backwards step is a reset, not negative usage: the data before it was still
    used, so the segments are summed rather than the endpoints subtracted. Getting this
    wrong reads as a sudden refund of a month's traffic.

    `anchor` is the odometer's value at the start of the cycle. For a `data.month_*`
    counter it is 0 by contract — the router zeroes it each month, so its current value
    is already the month to date, and anchoring at the first reading NetPulse happened
    to see would report only the hours since it started watching. That is the difference
    between "you have used 23.6 GB" and "we watched you use 227 MB", and only one of
    those answers the question anyone is asking.
    """
    if not readings:
        return 0.0
    total = 0.0
    start = readings[0] if anchor is None else anchor
    previous = readings[0]
    for reading in readings[1:]:
        if reading < previous:  # the odometer went back: reset, reboot, or wrap
            total += previous - start
            start = reading
        previous = reading
    return total + previous - start


def _watched_days(store: Store, source: str, since: datetime, now: datetime) -> float:
    """Days between the first and last odometer reading — the span we can speak for."""
    readings = store.samples_span(source, TOTAL_METRIC, since) or store.samples_span(
        source, DOWN_METRIC, since
    )
    if readings is None:
        return 0.0
    first, last = readings
    return max(0.0, (last - first).total_seconds() / 86400)


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
    # A month counter is zero at the cycle's start whether or not anyone was watching.
    used = travelled(readings, anchor=0.0)
    # How much of the cycle NetPulse actually observed, so the panel can say whether
    # the figure is the router's complete count or only the part we saw.
    observed = travelled(readings)

    days_total = (end - start).days
    elapsed = max(0.0, (now - since).total_seconds() / 86400)
    # The rate comes from what was observed over the time it was observed — dividing a
    # month-to-date total by the hours we have been watching would project a fortnight
    # of traffic onto every remaining day. Under six hours, a rate is noise wearing a
    # decimal point either way.
    watched = _watched_days(store, source, since, now)
    rate = observed / watched if watched >= 0.25 else None

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
        observed_bytes=observed,
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


def by_day(
    store: Store, source: str, since: datetime, until: datetime
) -> list[tuple[date, float | None, float]]:
    """How much the whole connection used on each calendar day.

    Derived from the odometer rather than from anything summed: the router's counter is
    the authoritative figure, so a day's usage is the distance it travelled between
    that day's first and last reading — with every reset along the way accounted for.

    Each day also carries the fraction of it that was actually recorded, because a day
    NetPulse watched for two hours is not a day with low usage. `None` for usage means
    the day was never sampled at all, which is a different claim from zero.
    """
    days: list[tuple[date, float | None, float]] = []
    day = since.date()
    while day < until.date() + timedelta(days=1):
        start = datetime.combine(day, datetime.min.time(), tzinfo=since.tzinfo)
        end = start + timedelta(days=1)
        readings = _odometer_window(store, source, start, end)
        if not readings:
            days.append((day, None, 0.0))
        else:
            span = store.samples_span(source, TOTAL_METRIC, start, end) or store.samples_span(
                source, DOWN_METRIC, start, end
            )
            watched = (span[1] - span[0]).total_seconds() / 86400 if span else 0.0
            # A day still running is measured against the hours it has had, not 24.
            elapsed = min(1.0, max((min(end, until) - start).total_seconds() / 86400, 1e-9))
            days.append((day, travelled(readings), min(1.0, watched / elapsed)))
        day += timedelta(days=1)
    return days


def _odometer_window(store: Store, source: str, since: datetime, until: datetime) -> list[float]:
    totals = store.values(source, TOTAL_METRIC, since, until)
    if totals:
        return totals
    downs = store.values(source, DOWN_METRIC, since, until)
    ups = store.values(source, UP_METRIC, since, until)
    if not downs:
        return []
    return [down + (ups[i] if i < len(ups) else 0.0) for i, down in enumerate(downs)]


def format_bytes(value: float) -> str:
    """Human units for notification text, where "104857600 bytes" helps nobody."""
    units = ("B", "KB", "MB", "GB", "TB")
    index = 0
    while value >= 1000 and index < len(units) - 1:
        value /= 1000
        index += 1
    return f"{value:.0f} {units[index]}" if value >= 100 else f"{value:.1f} {units[index]}"
