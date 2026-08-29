"""The store's one promise: history is what was sampled, nothing more."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from netpulse.core.model import Agg, EventKind, Severity
from netpulse.core.storage import Store
from tests.conftest import Clock


def test_latest_returns_the_newest_value_per_metric(store: Store, clock: Clock) -> None:
    store.record("wan", {"latency.internet_ms": 50.0})
    clock.advance(seconds=5)
    store.record("wan", {"latency.internet_ms": 80.0, "loss.pct": 0.0})

    latest = store.latest("wan")
    assert latest["latency.internet_ms"][1] == 80.0
    assert latest["loss.pct"][1] == 0.0


def test_latency_buckets_keep_the_worst_value(store: Store, clock: Clock) -> None:
    """A 2-second spike averaged into a minute reads as fine. Max survives."""
    start = clock.now
    for value in (50, 52, 900, 51):
        store.record("wan", {"latency.internet_ms": float(value)})
        clock.advance(seconds=5)

    series = store.history("wan", "latency.internet_ms", start, clock.now, 1)
    assert series[0][1] == 900


def test_signal_buckets_average(store: Store, clock: Clock) -> None:
    start = clock.now
    for value in (-90, -100):
        store.record("mtn", {"signal.rsrp_dbm": float(value)})
        clock.advance(seconds=5)
    series = store.history("mtn", "signal.rsrp_dbm", start, clock.now, 1)
    assert series[0][1] == -95


def test_a_gap_is_none_not_an_interpolation(store: Store, clock: Clock) -> None:
    start = clock.now
    store.record("wan", {"latency.internet_ms": 50.0, "up": 1.0})
    clock.advance(minutes=9)  # recorder was down for the middle stretch
    store.record("wan", {"latency.internet_ms": 60.0, "up": 1.0})
    clock.advance(minutes=1)

    series = store.history("wan", "latency.internet_ms", start, clock.now, 10)
    values = [value for _, value in series]
    assert values[0] == 50.0
    assert values[-1] == 60.0
    assert all(value is None for value in values[1:-1])


def test_coverage_reports_the_sampled_fraction(store: Store, clock: Clock) -> None:
    start = clock.now
    for _ in range(60):  # five minutes of a ten-minute window at 5s cadence
        store.record("wan", {"up": 1.0})
        clock.advance(seconds=5)
    clock.advance(minutes=5)

    coverage = store.coverage("wan", start, clock.now, interval_s=5)
    assert 0.45 < coverage.fraction < 0.55


def test_agg_can_be_overridden(store: Store, clock: Clock) -> None:
    start = clock.now
    for value in (10, 20, 30):
        store.record("wan", {"latency.internet_ms": float(value)})
        clock.advance(seconds=5)
    series = store.history("wan", "latency.internet_ms", start, clock.now, 1, agg=Agg.MIN)
    assert series[0][1] == 10


def test_texts_store_transitions_not_every_poll(store: Store, clock: Clock) -> None:
    for _ in range(50):
        store.record("mtn", {"up": 1.0}, {"net.type": "LTE"})
        clock.advance(seconds=5)
    store.record("mtn", {"up": 1.0}, {"net.type": "5G"})

    assert store.latest_texts("mtn")["net.type"] == "5G"
    rows = store._conn.execute("SELECT COUNT(*) AS n FROM texts").fetchone()
    assert rows["n"] == 2  # one for LTE, one for the change to 5G


def test_events_open_close_and_filter(store: Store, clock: Clock) -> None:
    event_id = store.open_event("wan", EventKind.OUTAGE, Severity.CRITICAL, "unreachable")
    assert store.events(open_only=True)[0].open
    clock.advance(minutes=3)
    store.close_event(event_id)

    closed = store.events()[0]
    assert not closed.open
    assert (closed.ended_at - closed.started_at) == timedelta(minutes=3)  # type: ignore[operator]
    assert store.events(open_only=True) == []


def test_history_survives_reopening_the_file(tmp_path, clock: Clock) -> None:  # type: ignore[no-untyped-def]
    path = tmp_path / "history.db"
    first = Store(path, clock=clock)
    first.record("wan", {"latency.internet_ms": 42.0, "up": 1.0})
    first.close()

    second = Store(path, clock=clock)
    assert second.latest("wan")["latency.internet_ms"][1] == 42.0
    second.close()


def test_usage_is_attributed_and_summed_per_key(store: Store, clock: Clock) -> None:
    """Intervals, not counters: a restart, a rename or a reboot each corrupt a counter
    read against its own previous row, and an interval is true on its own."""
    store.record_usage("wan", "app", [("firefox", 100.0, 20.0), ("ssh", 5.0, 5.0)])
    clock.advance(minutes=5)
    store.record_usage("wan", "app", [("firefox", 400.0, 80.0)])

    totals = dict(
        (key, (down, up))
        for key, down, up in store.usage_by_key("wan", "app", clock.now - timedelta(hours=1))
    )
    assert totals["firefox"] == (500.0, 100.0)
    assert totals["ssh"] == (5.0, 5.0)


def test_usage_rows_with_nothing_in_them_are_not_stored(store: Store, clock: Clock) -> None:
    """An idle process every five seconds would otherwise fill the database with zeroes."""
    store.record_usage("wan", "app", [("idle", 0.0, 0.0)])
    assert store.usage_by_key("wan", "app", clock.now - timedelta(hours=1)) == []


def test_usage_groups_by_calendar_day(store: Store, clock: Clock) -> None:
    clock.set(datetime(2026, 8, 12, 23, 0, tzinfo=UTC))
    store.record_usage("wan", "app", [("firefox", 100.0, 0.0)])
    clock.advance(hours=2)  # over midnight
    store.record_usage("wan", "app", [("firefox", 50.0, 0.0)])

    days = dict(
        (day, down)
        for day, down, _ in store.usage_by_day("wan", "app", clock.now - timedelta(days=3))
    )
    assert days["2026-08-12"] == 100.0
    assert days["2026-08-13"] == 50.0
