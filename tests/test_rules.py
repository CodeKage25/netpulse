"""Data rules: an allowance, a timetable, or a countdown."""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta

from netpulse.analysis.rules import (
    Cycle,
    Kind,
    Rule,
    Verdict,
    Window,
    cycle_start,
    evaluate,
    reconcile,
)
from netpulse.core.storage import Store
from tests.conftest import Clock

GB = 1_000_000_000.0
PHONE = "AA:BB:CC:DD:EE:01"
TABLET = "AA:BB:CC:DD:EE:02"


def spend(store: Store, clock: Clock, mac: str, gigabytes: float) -> None:
    store.record_usage("wan", "device", [(mac, gigabytes * GB, 0.0)], at=clock.now)


# ------------------------------------------------------------------ allowances


def test_a_device_under_its_allowance_is_not_held(store: Store, clock: Clock) -> None:
    spend(store, clock, PHONE, 3)
    rule = Rule("phone", Kind.LIMIT, (PHONE,), limit_bytes=10 * GB)
    verdict = evaluate(store, "wan", rule, clock.now)[0]
    assert verdict.holds is False
    assert verdict.remaining_bytes == 7 * GB


def test_a_device_over_its_allowance_is_held(store: Store, clock: Clock) -> None:
    spend(store, clock, PHONE, 11)
    verdict = evaluate(
        store, "wan", Rule("phone", Kind.LIMIT, (PHONE,), limit_bytes=10 * GB), clock.now
    )[0]
    assert verdict.holds is True
    assert verdict.remaining_bytes == 0.0


def test_each_member_of_an_unpooled_group_gets_the_whole_allowance(
    store: Store, clock: Clock
) -> None:
    spend(store, clock, PHONE, 9)
    spend(store, clock, TABLET, 2)
    rule = Rule("kids", Kind.LIMIT, (PHONE, TABLET), limit_bytes=10 * GB, pooled=False)
    holds = {v.devices[0]: v.holds for v in evaluate(store, "wan", rule, clock.now)}
    assert holds == {PHONE: False, TABLET: False}


def test_a_pooled_group_runs_out_together(store: Store, clock: Clock) -> None:
    """The same spending that leaves both members fine on their own exhausts a shared
    budget — which is the entire difference between the two modes."""
    spend(store, clock, PHONE, 9)
    spend(store, clock, TABLET, 2)
    rule = Rule("kids", Kind.LIMIT, (PHONE, TABLET), limit_bytes=10 * GB, pooled=True)
    verdicts = evaluate(store, "wan", rule, clock.now)
    assert len(verdicts) == 1
    assert verdicts[0].holds is True
    assert verdicts[0].devices == (PHONE, TABLET)


def test_usage_before_the_cycle_started_does_not_count(store: Store, clock: Clock) -> None:
    clock.set(datetime(2026, 7, 20, tzinfo=UTC))
    spend(store, clock, PHONE, 40)
    clock.set(datetime(2026, 8, 3, tzinfo=UTC))
    spend(store, clock, PHONE, 2)
    rule = Rule(
        "phone", Kind.LIMIT, (PHONE,), limit_bytes=10 * GB, cycle=Cycle.MONTHLY, cycle_day=1
    )
    assert evaluate(store, "wan", rule, clock.now)[0].used_bytes == 2 * GB


# ------------------------------------------------------------------ cycles


def test_a_monthly_cycle_set_to_the_31st_rolls_in_a_short_month() -> None:
    assert cycle_start(Cycle.MONTHLY, 31, datetime(2026, 6, 15, tzinfo=UTC)).day == 31
    assert cycle_start(Cycle.MONTHLY, 31, datetime(2026, 7, 1, tzinfo=UTC)).day == 30


def test_a_weekly_cycle_starts_on_its_chosen_weekday() -> None:
    # 2026-08-29 is a Saturday; a cycle anchored to Monday began on the 24th.
    start = cycle_start(Cycle.WEEKLY, 0, datetime(2026, 8, 29, 12, tzinfo=UTC))
    assert start.day == 24
    assert start.weekday() == 0


# ------------------------------------------------------------------ schedules


def test_a_window_holds_inside_its_hours(store: Store, clock: Clock) -> None:
    rule = Rule("homework", Kind.SCHEDULE, (PHONE,), windows=(Window(time(16, 0), time(20, 0)),))
    at_five = datetime(2026, 8, 26, 17, 0, tzinfo=UTC)
    at_nine = datetime(2026, 8, 26, 21, 0, tzinfo=UTC)
    assert evaluate(store, "wan", rule, at_five)[0].holds is True
    assert evaluate(store, "wan", rule, at_nine)[0].holds is False


def test_a_window_crossing_midnight_holds_on_the_far_side(store: Store, clock: Clock) -> None:
    """22:00 to 02:00 is one window, not two, and it must not lapse at midnight."""
    rule = Rule("bedtime", Kind.SCHEDULE, (PHONE,), windows=(Window(time(22, 0), time(2, 0)),))
    for moment, expected in (
        (datetime(2026, 8, 26, 23, 0, tzinfo=UTC), True),
        (datetime(2026, 8, 27, 1, 0, tzinfo=UTC), True),
        (datetime(2026, 8, 27, 3, 0, tzinfo=UTC), False),
    ):
        assert evaluate(store, "wan", rule, moment)[0].holds is expected


def test_a_weekday_window_only_holds_on_its_days(store: Store, clock: Clock) -> None:
    weekdays = Window(time(9, 0), time(17, 0), weekdays=frozenset({0, 1, 2, 3, 4}))
    rule = Rule("school", Kind.SCHEDULE, (PHONE,), windows=(weekdays,))
    monday = datetime(2026, 8, 24, 12, tzinfo=UTC)
    saturday = datetime(2026, 8, 29, 12, tzinfo=UTC)
    assert evaluate(store, "wan", rule, monday)[0].holds is True
    assert evaluate(store, "wan", rule, saturday)[0].holds is False


def test_a_friday_night_window_still_holds_on_saturday_morning(store: Store, clock: Clock) -> None:
    """The window belongs to the day it started on. Checking Saturday's weekday alone
    would end it at midnight, which is not what anybody who set it meant."""
    friday_night = Window(time(22, 0), time(2, 0), weekdays=frozenset({4}))
    rule = Rule("bedtime", Kind.SCHEDULE, (PHONE,), windows=(friday_night,))
    assert (
        evaluate(store, "wan", rule, datetime(2026, 8, 29, 1, 0, tzinfo=UTC))[0].holds is True
    )  # Sat 01:00


# ------------------------------------------------------------------ timers


def test_a_timer_holds_until_it_runs_out(store: Store, clock: Clock) -> None:
    started = clock.now
    rule = Rule("focus", Kind.TIMER, (PHONE,), duration=timedelta(hours=1), started_at=started)
    assert evaluate(store, "wan", rule, started + timedelta(minutes=30))[0].holds is True
    assert evaluate(store, "wan", rule, started + timedelta(minutes=61))[0].holds is False


def test_a_timer_is_capped_however_long_it_was_set_for(store: Store, clock: Clock) -> None:
    """A countdown longer than a day is a schedule badly expressed, and one somebody has
    forgotten they set."""
    started = clock.now
    rule = Rule("oops", Kind.TIMER, (PHONE,), duration=timedelta(days=30), started_at=started)
    assert evaluate(store, "wan", rule, started + timedelta(hours=25))[0].holds is False


def test_an_unstarted_timer_holds_nothing(store: Store, clock: Clock) -> None:
    rule = Rule("focus", Kind.TIMER, (PHONE,), duration=timedelta(hours=1))
    assert evaluate(store, "wan", rule, clock.now)[0].holds is False


# ------------------------------------------------------------------ reconciliation


def held(mac: str, block: bool = True) -> Verdict:
    return Verdict(Rule("r", Kind.LIMIT, (mac,), block=block), True, "over", devices=(mac,))


def free(mac: str, block: bool = True) -> Verdict:
    return Verdict(Rule("r", Kind.LIMIT, (mac,), block=block), False, "under", devices=(mac,))


def test_reconciliation_is_the_difference_between_wanted_and_actual() -> None:
    to_block, to_unblock = reconcile([held(PHONE), free(TABLET)], {TABLET})
    assert to_block == {PHONE}
    assert to_unblock == {TABLET}


def test_a_rule_that_only_reports_never_blocks_anything() -> None:
    to_block, to_unblock = reconcile([held(PHONE, block=False)], set())
    assert to_block == set()
    assert to_unblock == set()


def test_a_device_blocked_by_hand_is_left_alone(store: Store) -> None:
    """A reporting-only rule must not quietly undo somebody's manual block."""
    _, to_unblock = reconcile([held(TABLET, block=False)], {"99:99:99:99:99:99"})
    assert to_unblock == set()


def test_an_override_stops_a_rule_reasserting_itself() -> None:
    """Unblocking by hand a device a rule still holds would otherwise be undone on the
    next pass, forever."""
    to_block, _ = reconcile([held(PHONE)], set(), overridden={PHONE})
    assert to_block == set()


def test_reconciliation_is_idempotent() -> None:
    """Calling it again after acting must ask for nothing, or enforcement becomes a
    loop that rewrites the router's filter list every few seconds."""
    to_block, to_unblock = reconcile([held(PHONE)], {PHONE})
    assert to_block == set() and to_unblock == set()


# ------------------------------------------------------------------ config parsing


def test_rules_parse_from_config() -> None:
    from netpulse.analysis.rules import parse_rules

    rules = parse_rules(
        [
            {
                "name": "kids",
                "kind": "limit",
                "devices": [PHONE, TABLET],
                "limit_gb": 20,
                "pooled": True,
                "block": True,
            },
            {
                "name": "bedtime",
                "kind": "schedule",
                "devices": [PHONE],
                "windows": [{"from": "22:00", "to": "07:00"}],
                "block": True,
            },
        ]
    )
    assert len(rules) == 2
    assert rules[0].limit_bytes == 20 * GB and rules[0].pooled is True
    assert rules[1].windows[0].crosses_midnight is True


def test_blocking_is_opt_in() -> None:
    """A rule that reports is useful on its own, and silently turning every allowance
    into an enforcement action would be a surprising thing to do to a network."""
    from netpulse.analysis.rules import parse_rules

    rule = parse_rules([{"kind": "limit", "devices": [PHONE], "limit_gb": 5}])[0]
    assert rule.block is False


def test_a_rule_that_cannot_mean_anything_is_dropped() -> None:
    """An allowance with no amount, a timetable with no hours, an unknown kind, or no
    device at all. Guessing is the last thing to do where blocking is involved."""
    from netpulse.analysis.rules import parse_rules

    assert parse_rules([{"kind": "limit", "devices": [PHONE]}]) == []
    assert parse_rules([{"kind": "schedule", "devices": [PHONE]}]) == []
    assert parse_rules([{"kind": "quota", "devices": [PHONE], "limit_gb": 5}]) == []
    assert parse_rules([{"kind": "limit", "limit_gb": 5}]) == []


def test_a_malformed_window_is_skipped_not_the_whole_rule() -> None:
    from netpulse.analysis.rules import parse_rules

    rules = parse_rules(
        [
            {
                "kind": "schedule",
                "devices": [PHONE],
                "windows": [{"from": "nonsense", "to": "07:00"}, {"from": "22:00", "to": "23:00"}],
            }
        ]
    )
    assert len(rules[0].windows) == 1


def test_device_addresses_are_normalised_to_upper_case() -> None:
    """The router reports lower case and people type either; a rule that misses because
    of case would look like a rule that does not work."""
    from netpulse.analysis.rules import parse_rules

    rule = parse_rules([{"kind": "limit", "devices": [PHONE.lower()], "limit_gb": 5}])[0]
    assert rule.devices == (PHONE,)
