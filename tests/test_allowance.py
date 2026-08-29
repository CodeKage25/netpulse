"""The data allowance meter: an odometer that resets, against a cycle that does not."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from netpulse.allowance import assess, crossed, cycle_start, travelled
from netpulse.storage import Store
from tests.conftest import Clock

GB = 1_000_000_000.0


# ------------------------------------------------------------------ the billing cycle


def test_the_cycle_containing_today_starts_on_the_reset_day() -> None:
    assert cycle_start(date(2026, 8, 29), 15) == date(2026, 8, 15)
    assert cycle_start(date(2026, 8, 3), 15) == date(2026, 7, 15)
    assert cycle_start(date(2026, 8, 15), 15) == date(2026, 8, 15)  # the day itself


def test_a_reset_day_of_31_lands_on_the_last_day_a_short_month_has() -> None:
    """It has to land somewhere, and it lands where carriers put it."""
    assert cycle_start(date(2026, 6, 30), 31) == date(2026, 6, 30)
    assert cycle_start(date(2026, 3, 1), 31) == date(2026, 2, 28)


def test_the_first_of_the_month_is_the_ordinary_case() -> None:
    assert cycle_start(date(2026, 8, 29), 1) == date(2026, 8, 1)
    assert cycle_start(date(2026, 1, 1), 1) == date(2026, 1, 1)


# ------------------------------------------------------------------ the odometer


def test_a_steady_odometer_measures_the_distance_travelled() -> None:
    assert travelled([100.0, 250.0, 400.0]) == 300.0


def test_a_reset_is_not_negative_usage() -> None:
    """The data before a reset was still used. Subtracting the endpoints would report a
    sudden refund of a month's traffic — the most confidently wrong number available."""
    assert travelled([100.0, 900.0, 50.0, 300.0]) == 1050.0


def test_several_resets_in_one_cycle_all_count() -> None:
    assert travelled([0.0, 500.0, 0.0, 500.0, 0.0, 200.0]) == 1200.0


def test_an_odometer_that_never_moved_reports_nothing_used() -> None:
    assert travelled([700.0, 700.0, 700.0]) == 0.0


def test_a_single_reading_is_a_position_not_a_distance() -> None:
    assert travelled([4_000.0]) == 0.0


# ------------------------------------------------------------------ the assessment


def seed(store: Store, clock: Clock, readings: list[float], step_hours: float = 6) -> None:
    """Record readings `step_hours` apart, leaving the clock on the last one — so N
    readings span (N-1) steps, and `now` is the moment of the final reading."""
    for index, reading in enumerate(readings):
        if index:
            clock.advance(seconds=step_hours * 3600)
        store.record("mtn", {"data.month_total_bytes": reading})


def test_nothing_recorded_gives_no_assessment_rather_than_a_confident_zero(
    store: Store, clock: Clock
) -> None:
    assert assess(store, "mtn", clock.now, limit_bytes=100 * GB) is None


def test_usage_and_projection_over_a_started_cycle(store: Store, clock: Clock) -> None:
    start = datetime(2026, 8, 1, tzinfo=UTC)
    clock.set(start)
    seed(store, clock, [0.0, 5 * GB, 10 * GB, 15 * GB, 20 * GB])  # 20 GB over 24 h

    result = assess(store, "mtn", clock.now, limit_bytes=100 * GB, reset_day=1)
    assert result is not None
    assert result.used_bytes == 20 * GB
    assert result.cycle_start == date(2026, 8, 1)
    assert result.days_total == 31
    assert result.rate_per_day is not None
    assert round(result.rate_per_day / GB) == 20
    # 20 GB/day against a 100 GB plan runs out five days in — well inside the cycle.
    assert result.exhausted_on == date(2026, 8, 6)
    assert result.on_track is False


def test_a_plan_that_lasts_says_so(store: Store, clock: Clock) -> None:
    clock.set(datetime(2026, 8, 1, tzinfo=UTC))
    seed(store, clock, [0.0, GB, 2 * GB, 3 * GB, 4 * GB])  # 4 GB/day

    result = assess(store, "mtn", clock.now, limit_bytes=500 * GB, reset_day=1)
    assert result is not None
    assert result.exhausted_on is None
    assert result.on_track is True
    assert result.projected_bytes is not None
    assert round(result.projected_bytes / GB) == 124  # 4 GB/day over 31 days


def test_no_limit_configured_still_reports_usage(store: Store, clock: Clock) -> None:
    """Knowing what you used is useful even when nothing is capping it."""
    clock.set(datetime(2026, 8, 1, tzinfo=UTC))
    seed(store, clock, [0.0, 3 * GB, 6 * GB])

    result = assess(store, "mtn", clock.now, limit_bytes=None, reset_day=1)
    assert result is not None
    assert result.used_bytes == 6 * GB
    assert result.fraction is None
    assert result.on_track is None
    assert result.exhausted_on is None


def test_the_first_hours_of_a_cycle_produce_no_run_rate(store: Store, clock: Clock) -> None:
    """A projection off ten minutes of data is noise wearing a decimal point."""
    clock.set(datetime(2026, 8, 1, tzinfo=UTC))
    seed(store, clock, [0.0, GB], step_hours=0.05)

    result = assess(store, "mtn", clock.now, limit_bytes=100 * GB, reset_day=1)
    assert result is not None
    assert result.rate_per_day is None
    assert result.projected_bytes is None
    assert result.exhausted_on is None


def test_a_router_reset_mid_cycle_does_not_erase_the_usage(store: Store, clock: Clock) -> None:
    """The router's counter restarting is not the user getting their data back."""
    clock.set(datetime(2026, 8, 1, tzinfo=UTC))
    seed(store, clock, [0.0, 30 * GB, 60 * GB, 0.0, 10 * GB])

    result = assess(store, "mtn", clock.now, limit_bytes=100 * GB, reset_day=1)
    assert result is not None
    assert result.used_bytes == 70 * GB


def test_readings_before_the_cycle_started_are_not_counted(store: Store, clock: Clock) -> None:
    """Last month's traffic is last month's problem — the router zeroes its counter and
    the previous cycle's readings are outside the window entirely."""
    clock.set(datetime(2026, 7, 20, tzinfo=UTC))
    seed(store, clock, [30 * GB, 40 * GB], step_hours=24)
    clock.set(datetime(2026, 8, 2, tzinfo=UTC))
    seed(store, clock, [0.0, 5 * GB], step_hours=12)

    result = assess(store, "mtn", clock.now, limit_bytes=100 * GB, reset_day=1)
    assert result is not None
    assert result.used_bytes == 5 * GB


def test_the_counter_is_the_month_total_not_the_part_we_watched(store: Store, clock: Clock) -> None:
    """A month counter is zero at the cycle's start whether or not anyone was watching.

    Joining mid-month and reporting only the hours since is the difference between
    "you have used 23.6 GB" and "we watched you use 227 MB" — and only one of those
    answers the question anyone is asking.
    """
    clock.set(datetime(2026, 8, 20, tzinfo=UTC))  # three weeks into the cycle
    seed(store, clock, [23.4 * GB, 23.5 * GB, 23.6 * GB], step_hours=2)

    result = assess(store, "mtn", clock.now, limit_bytes=100 * GB, reset_day=1)
    assert result is not None
    assert result.used_bytes == pytest.approx(23.6 * GB)
    # …and it still knows how much of that it saw happen, which is what the rate uses.
    assert result.observed_bytes == pytest.approx(0.2 * GB)


def test_the_run_rate_uses_the_span_actually_watched(store: Store, clock: Clock) -> None:
    """Dividing a month-to-date total by the hours we have been running would project a
    fortnight of somebody else's traffic onto every remaining day."""
    clock.set(datetime(2026, 8, 20, tzinfo=UTC))
    seed(store, clock, [50 * GB, 51 * GB], step_hours=24)  # 1 GB watched over 1 day

    result = assess(store, "mtn", clock.now, limit_bytes=100 * GB, reset_day=1)
    assert result is not None
    assert result.rate_per_day is not None
    assert round(result.rate_per_day / GB) == 1  # not 51


def test_a_download_upload_pair_is_summed_when_there_is_no_total(
    store: Store, clock: Clock
) -> None:
    """Huawei reports the pair; ZLT reports one total. Both have to work."""
    clock.set(datetime(2026, 8, 1, tzinfo=UTC))
    for index, (down, up) in enumerate(((0.0, 0.0), (8 * GB, 1 * GB), (16 * GB, 2 * GB))):
        if index:
            clock.advance(seconds=6 * 3600)
        store.record("mtn", {"data.month_down_bytes": down, "data.month_up_bytes": up})

    result = assess(store, "mtn", clock.now, limit_bytes=100 * GB, reset_day=1)
    assert result is not None
    assert result.used_bytes == 18 * GB


# ------------------------------------------------------------------ threshold crossing


def test_a_threshold_announces_itself_once() -> None:
    assert crossed(0.78, 0.81) == 0.8
    assert crossed(0.81, 0.82) is None  # sitting at 82% is not news every poll


def test_a_jump_past_several_thresholds_reports_the_highest() -> None:
    assert crossed(0.3, 0.97) == 0.95


def test_a_cycle_rollover_re_arms_every_threshold() -> None:
    assert crossed(0.99, 0.0) is None  # the drop itself is not a crossing
    assert crossed(0.0, 0.55) == 0.5  # and the next climb announces again


def test_no_limit_means_no_thresholds_to_cross() -> None:
    assert crossed(None, None) is None


# ------------------------------------------------------- alerts through the collector


def test_crossing_a_threshold_notifies_once_per_level(store: Store, clock: Clock) -> None:
    """The point of the feature is arriving before the bill does — and then shutting up."""
    from netpulse.adapters.fake import ScriptedAdapter
    from netpulse.config import Plan
    from netpulse.model import Reading
    from netpulse.monitor import Collector
    from netpulse.notify import Notifier

    sent: list[tuple[str, str]] = []
    notifier = Notifier(deliver=lambda title, body: sent.append((title, body)), clock=clock)
    clock.set(datetime(2026, 8, 1, tzinfo=UTC))

    # Ten readings walking from 0 to 100 GB against a 100 GB plan.
    readings = [
        Reading(metrics={"data.month_total_bytes": step * 10 * GB, "up": 1.0}) for step in range(11)
    ]
    collector = Collector(
        store,
        [ScriptedAdapter("mtn", readings)],
        clock=clock,
        notifier=notifier,
        plan=Plan(limit_gb=100, reset_day=1),
    )
    for _ in readings:
        collector.poll_once()
        clock.advance(seconds=3600)

    levels = [title for title, _ in sent]
    # 10 GB steps, so the jump from 90% to 100% passes 95% and 100% together and only
    # the more urgent one is worth interrupting someone for.
    assert levels == [
        "mtn: 50% of data used",
        "mtn: 80% of data used",
        "mtn: 100% of data used",
    ]
    assert "Expect throttling or charges" in sent[-1][1]


def test_sitting_above_a_threshold_does_not_re_announce_it(store: Store, clock: Clock) -> None:
    """Crossing is news; remaining there is not, and an alert that repeats gets muted."""
    from netpulse.adapters.fake import ScriptedAdapter
    from netpulse.config import Plan
    from netpulse.model import Reading
    from netpulse.monitor import Collector
    from netpulse.notify import Notifier

    sent: list[tuple[str, str]] = []
    notifier = Notifier(deliver=lambda title, body: sent.append((title, body)), clock=clock)
    clock.set(datetime(2026, 8, 1, tzinfo=UTC))

    readings = [
        Reading(metrics={"data.month_total_bytes": used * GB, "up": 1.0})
        for used in (0, 55, 56, 57, 58, 59, 60)
    ]
    collector = Collector(
        store,
        [ScriptedAdapter("mtn", readings)],
        clock=clock,
        notifier=notifier,
        plan=Plan(limit_gb=100, reset_day=1),
    )
    for _ in readings:
        collector.poll_once()
        clock.advance(seconds=3600)

    assert [title for title, _ in sent] == ["mtn: 50% of data used"]


def test_no_plan_configured_means_no_data_alerts(store: Store, clock: Clock) -> None:
    """Nothing was promised, so nothing can be exceeded."""
    from netpulse.adapters.fake import ScriptedAdapter
    from netpulse.model import Reading
    from netpulse.monitor import Collector
    from netpulse.notify import Notifier

    sent: list[tuple[str, str]] = []
    notifier = Notifier(deliver=lambda title, body: sent.append((title, body)), clock=clock)
    collector = Collector(
        store,
        [
            ScriptedAdapter(
                "mtn", [Reading(metrics={"data.month_total_bytes": 900 * GB, "up": 1.0})]
            )
        ],
        clock=clock,
        notifier=notifier,
    )
    collector.poll_once()
    assert sent == []
