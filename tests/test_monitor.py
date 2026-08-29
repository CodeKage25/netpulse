"""The collector's judgement: when a blip becomes an outage, and how it treats a sick router."""

from __future__ import annotations

from netpulse.core.model import Reading
from netpulse.core.storage import Store
from netpulse.monitor import Collector
from netpulse.sources import AdapterError
from netpulse.sources.fake import ScriptedAdapter
from tests.conftest import Clock

OK = Reading(metrics={"latency.internet_ms": 50.0, "up": 1.0})
FAIL = AdapterError("unreachable")


def ticks(collector: Collector, clock: Clock, n: int) -> None:
    for _ in range(n):
        collector.poll_once()
        clock.advance(seconds=5)


def test_one_failed_poll_is_a_blip_not_an_outage(store: Store, clock: Clock) -> None:
    adapter = ScriptedAdapter("wan", [OK, FAIL, OK])
    collector = Collector(store, [adapter], clock=clock)
    ticks(collector, clock, 3)
    assert store.events() == []


def test_three_consecutive_failures_open_an_outage(store: Store, clock: Clock) -> None:
    adapter = ScriptedAdapter("wan", [OK, FAIL, FAIL, FAIL])
    collector = Collector(store, [adapter], clock=clock)
    ticks(collector, clock, 8)  # backoff stretches the three failures over more cycles

    events = store.events()
    assert len(events) == 1
    assert events[0].kind.value == "outage"
    assert events[0].open


def test_recovery_closes_the_outage(store: Store, clock: Clock) -> None:
    adapter = ScriptedAdapter("wan", [FAIL, FAIL, FAIL, OK, OK])
    collector = Collector(store, [adapter], clock=clock)
    ticks(collector, clock, 12)

    events = store.events()
    assert len(events) == 1
    assert not events[0].open


def test_a_failing_source_is_polled_less_not_more(store: Store, clock: Clock) -> None:
    """The recovering router needs quiet more than we need the sample."""
    script: list[Reading | Exception] = [AdapterError("down") for _ in range(50)]
    adapter = ScriptedAdapter("wan", script)
    collector = Collector(store, [adapter], clock=clock)
    ticks(collector, clock, 20)

    attempts = 50 - len(adapter._script)
    assert attempts < 10  # far fewer than 20 cycles of hammering


def test_backoff_resets_after_recovery(store: Store, clock: Clock) -> None:
    adapter = ScriptedAdapter("wan", [FAIL, FAIL, OK, OK, OK])
    collector = Collector(store, [adapter], clock=clock)
    ticks(collector, clock, 10)
    assert len(adapter._script) == 0


def test_every_poll_writes_the_up_heartbeat(store: Store, clock: Clock) -> None:
    adapter = ScriptedAdapter("wan", [OK, FAIL, OK])
    collector = Collector(store, [adapter], clock=clock)
    ticks(collector, clock, 3)

    rows = store._conn.execute(
        "SELECT value FROM samples WHERE metric = 'up' ORDER BY at"
    ).fetchall()
    assert [row["value"] for row in rows] == [1.0, 0.0, 1.0]


def test_sustained_slowness_is_degraded_not_an_outage(store: Store, clock: Clock) -> None:
    slow = Reading(metrics={"latency.internet_ms": 900.0, "up": 1.0})
    adapter = ScriptedAdapter("wan", [slow] * 8)
    collector = Collector(store, [adapter], clock=clock)
    ticks(collector, clock, 8)

    events = store.events()
    assert len(events) == 1
    assert events[0].kind.value == "degraded"
    assert events[0].severity.value == "warning"


def test_listeners_get_every_reading_and_cannot_break_recording(store: Store, clock: Clock) -> None:
    adapter = ScriptedAdapter("wan", [OK, OK])
    collector = Collector(store, [adapter], clock=clock)
    seen: list[str] = []
    collector.subscribe(lambda source, metrics: seen.append(source))
    collector.subscribe(lambda *_: (_ for _ in ()).throw(RuntimeError("bad listener")))
    ticks(collector, clock, 2)

    assert seen == ["wan", "wan"]
    assert store.latest("wan")["up"][1] == 1.0
