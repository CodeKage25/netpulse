"""Each rule answers a question a person actually asks, with the numbers to back it."""

from __future__ import annotations

from netpulse.insights import diagnose
from netpulse.model import EventKind, Severity
from netpulse.storage import Store
from tests.conftest import Clock


def feed(store: Store, clock: Clock, minutes: int, metrics: dict[str, float]) -> None:
    for _ in range(minutes * 12):  # 5s cadence
        store.record("wan", dict(metrics, up=1.0))
        clock.advance(seconds=5)


def titles(store: Store, clock: Clock) -> list[str]:
    return [insight.title for insight in diagnose(store, "wan", clock.now)]


def test_bad_internet_with_a_fast_gateway_blames_the_provider(store: Store, clock: Clock) -> None:
    feed(store, clock, 20, {"latency.gateway_ms": 3, "latency.internet_ms": 600})
    found = diagnose(store, "wan", clock.now)
    upstream = next(i for i in found if i.rule == "upstream_or_local")
    assert "upstream" in upstream.title
    assert upstream.evidence["gateway_ms"] == 3


def test_a_slow_gateway_blames_the_local_network(store: Store, clock: Clock) -> None:
    feed(store, clock, 20, {"latency.gateway_ms": 250, "latency.internet_ms": 400})
    assert any("local network" in title for title in titles(store, clock))


def test_a_healthy_connection_flags_nothing(store: Store, clock: Clock) -> None:
    feed(
        store,
        clock,
        20,
        {
            "latency.gateway_ms": 3,
            "latency.internet_ms": 60,
            "latency.internet_best_ms": 45,
            "dns.lookup_ms": 20,
        },
    )
    assert titles(store, clock) == []


def test_negative_sinr_is_critical_and_says_what_to_do(store: Store, clock: Clock) -> None:
    feed(store, clock, 30, {"signal.sinr_db": -3, "signal.rsrp_dbm": -100})
    found = diagnose(store, "wan", clock.now)
    signal = next(i for i in found if i.rule == "weak_signal")
    assert signal.severity is Severity.CRITICAL
    assert "antenna" in signal.detail or "window" in signal.detail


def test_excellent_radio_conditions_are_said_out_loud(store: Store, clock: Clock) -> None:
    """So a user with a bad plan stops blaming the router placement."""
    feed(store, clock, 30, {"signal.sinr_db": 16, "signal.rsrp_dbm": -85})
    found = diagnose(store, "wan", clock.now)
    assert any(i.severity is Severity.INFO and "excellent" in i.title for i in found)


def test_slow_dns_is_called_out_with_the_fix(store: Store, clock: Clock) -> None:
    feed(
        store,
        clock,
        20,
        {
            "dns.lookup_ms": 400,
            "latency.internet_best_ms": 30,
            "latency.internet_ms": 60,
            "latency.gateway_ms": 3,
        },
    )
    found = diagnose(store, "wan", clock.now)
    dns = next(i for i in found if i.rule == "slow_dns")
    assert "1.1.1.1" in dns.detail


def test_three_outages_in_a_day_reads_as_flapping(store: Store, clock: Clock) -> None:
    for _ in range(3):
        event = store.open_event("wan", EventKind.OUTAGE, Severity.CRITICAL, "unreachable")
        clock.advance(minutes=5)
        store.close_event(event)
        clock.advance(hours=2)
    found = diagnose(store, "wan", clock.now)
    assert any(i.rule == "flapping" and i.severity is Severity.CRITICAL for i in found)


def test_a_worse_than_baseline_hour_reads_as_congestion(store: Store, clock: Clock) -> None:
    feed(store, clock, 23 * 60 // 12, {"latency.internet_ms": 60})  # quiet baseline
    feed(store, clock, 60, {"latency.internet_ms": 300})  # busy hour now
    found = diagnose(store, "wan", clock.now)
    assert any(i.rule == "congestion_hours" for i in found)


def test_critical_findings_sort_first(store: Store, clock: Clock) -> None:
    feed(
        store,
        clock,
        30,
        {
            "signal.sinr_db": -3,
            "signal.rsrp_dbm": -100,
            "dns.lookup_ms": 400,
            "latency.internet_best_ms": 30,
            "latency.internet_ms": 60,
            "latency.gateway_ms": 3,
        },
    )
    found = diagnose(store, "wan", clock.now)
    assert found[0].severity is Severity.CRITICAL


def test_no_data_means_no_findings_not_a_crash(store: Store, clock: Clock) -> None:
    assert diagnose(store, "wan", clock.now) == []


def test_diagnosis_reads_typical_values_not_chart_spikes(store: Store, clock: Clock) -> None:
    """A link that is fine 95% of the time with rare spikes must not be diagnosed as an
    upstream problem — charts show the worst moment, diagnosis describes the norm."""
    for tick in range(240):
        latency = 900.0 if tick % 24 == 0 else 60.0  # rare spikes on a healthy link
        store.record(
            "wan",
            {"latency.internet_ms": latency, "latency.gateway_ms": 3.0, "up": 1.0},
        )
        clock.advance(seconds=5)
    assert all(i.rule != "upstream_or_local" for i in diagnose(store, "wan", clock.now))
