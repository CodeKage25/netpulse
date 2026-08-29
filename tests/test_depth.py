"""The depth features: rollup, devices, speed test, notifications.

The rollup tests are the important ones — compaction must be invisible to everything
downstream: same chart shapes, same coverage, same spike survival.
"""

from __future__ import annotations

from datetime import timedelta

from netpulse.adapters import AdapterError
from netpulse.adapters.fake import ScriptedAdapter
from netpulse.adapters.huawei import HuaweiAdapter
from netpulse.model import DeviceSeen, Reading
from netpulse.monitor import Collector
from netpulse.notify import Notifier
from netpulse.speedtest import run_speedtest
from netpulse.storage import RAW_RETENTION, Store
from tests.conftest import Clock

# ------------------------------------------------------------------ rollup ladder


def feed_old_and_new(store: Store, clock: Clock) -> None:
    """Ten minutes of samples that will age past retention, then fresh ones."""
    for value in (50, 52, 900, 51, 48, 60, 55, 47, 900, 52):  # two spikes
        for _ in range(12):
            store.record("wan", {"latency.internet_ms": float(value), "up": 1.0})
            clock.advance(seconds=5)
    clock.advance(seconds=RAW_RETENTION.total_seconds() + 60)
    for _ in range(12):
        store.record("wan", {"latency.internet_ms": 65.0, "up": 1.0})
        clock.advance(seconds=5)


def test_compaction_folds_old_samples_and_deletes_raw(store: Store, clock: Clock) -> None:
    feed_old_and_new(store, clock)
    folded = store.compact(clock.now)
    assert folded == 240  # 120 latency + 120 up rows

    raw = store._conn.execute("SELECT COUNT(*) AS n FROM samples").fetchone()["n"]
    rolled = store._conn.execute("SELECT COUNT(*) AS n FROM rollup").fetchone()["n"]
    assert raw == 24  # only the fresh minute survives raw
    assert rolled == 20  # ten old minutes x two metrics


def test_a_spike_survives_compaction(store: Store, clock: Clock) -> None:
    """The whole point of sufficient statistics: max of maxes is exact."""
    start = clock.now
    feed_old_and_new(store, clock)
    store.compact(clock.now)

    series = store.history("wan", "latency.internet_ms", start, start + timedelta(minutes=10), 1)
    assert series[0][1] == 900


def test_means_stay_weighted_across_compaction(store: Store, clock: Clock) -> None:
    start = clock.now
    for value in (-90.0,) * 36 + (-102.0,) * 12:  # three quiet minutes, one bad one
        store.record("mtn", {"signal.rsrp_dbm": value, "up": 1.0})
        clock.advance(seconds=5)
    clock.advance(seconds=RAW_RETENTION.total_seconds() + 60)
    store.compact(clock.now)

    series = store.history("mtn", "signal.rsrp_dbm", start, start + timedelta(minutes=4), 1)
    assert series[0][1] == (-90 * 36 + -102 * 12) / 48  # exact, not mean-of-means


def test_coverage_is_unchanged_by_compaction(store: Store, clock: Clock) -> None:
    start = clock.now
    feed_old_and_new(store, clock)
    window_end = start + timedelta(minutes=10)
    before = store.coverage("wan", start, window_end, 5).fraction
    store.compact(clock.now)
    after = store.coverage("wan", start, window_end, 5).fraction
    assert before == after == 1.0


def test_history_is_seamless_across_the_retention_boundary(store: Store, clock: Clock) -> None:
    start = clock.now
    feed_old_and_new(store, clock)
    store.compact(clock.now)

    series = store.history("wan", "latency.internet_ms", start, clock.now, 300)
    values = [value for _, value in series if value is not None]
    assert 900 in values  # the old spike, from rollup
    assert 65 in values  # the fresh samples, from raw
    assert any(value is None for _, value in series)  # the week-long gap stays a gap


def test_compaction_is_idempotent_and_crash_safe(store: Store, clock: Clock) -> None:
    feed_old_and_new(store, clock)
    first = store.compact(clock.now)
    second = store.compact(clock.now)
    assert first > 0
    assert second == 0  # nothing left to fold; totals cannot double


def test_the_collector_compacts_once_a_day(store: Store, clock: Clock) -> None:
    ok = Reading(metrics={"up": 1.0})
    adapter = ScriptedAdapter("wan", [ok] * 10)
    collector = Collector(store, [adapter], clock=clock)
    collector.poll_once()
    first = collector._last_compact
    clock.advance(hours=1)
    collector.poll_once()
    assert collector._last_compact == first
    clock.advance(hours=24)
    collector.poll_once()
    assert collector._last_compact != first


# ------------------------------------------------------------------ devices


HOSTS = (
    b"<response><Hosts><Host><MacAddress>AA:BB:CC:11:22:33</MacAddress>"
    b"<HostName>abduls-macbook</HostName><IpAddress>192.168.8.100</IpAddress></Host>"
    b"<Host><MacAddress>DD:EE:FF:44:55:66</MacAddress><HostName>tv</HostName>"
    b"<IpAddress>192.168.8.101</IpAddress></Host></Hosts></response>"
)


def test_huawei_reads_the_client_list_on_its_own_cadence() -> None:
    from tests.test_adapters import HUAWEI_ROUTES, huawei_fetch

    routes = dict(HUAWEI_ROUTES)
    routes["/api/wlan/host-list"] = HOSTS
    fetch = huawei_fetch(routes)
    adapter = HuaweiAdapter("mtn", fetch=fetch)

    first = adapter.read()
    assert first.devices is not None
    assert len(first.devices) == 2
    assert first.metrics["devices.count"] == 2
    assert first.devices[0].name == "abduls-macbook"

    second = adapter.read()
    assert second.devices is None  # not every sweep — poll gently
    host_calls = [call for call in fetch.calls if "host-list" in call]
    assert len(host_calls) == 1


def test_device_sightings_are_stored_and_summarised(store: Store, clock: Clock) -> None:
    store.record_devices(
        "mtn",
        [DeviceSeen("AA:BB", "laptop", "192.168.8.100"), DeviceSeen("CC:DD", "", "192.168.8.101")],
    )
    clock.advance(minutes=10)
    store.record_devices("mtn", [DeviceSeen("AA:BB", "laptop", "192.168.8.100")])

    seen = store.devices("mtn", clock.now - timedelta(hours=1))
    assert [device["mac"] for device in seen] == ["AA:BB", "CC:DD"]
    assert seen[0]["name"] == "laptop"


def test_the_collector_persists_devices_from_a_reading(store: Store, clock: Clock) -> None:
    reading = Reading(
        metrics={"up": 1.0, "devices.count": 1.0},
        devices=[DeviceSeen("AA:BB", "phone", "192.168.8.102")],
    )
    collector = Collector(store, [ScriptedAdapter("mtn", [reading])], clock=clock)
    collector.poll_once()
    assert store.devices("mtn", clock.now - timedelta(hours=1))[0]["name"] == "phone"


# ------------------------------------------------------------------ speed test


def test_speedtest_records_what_it_measured(store: Store, clock: Clock) -> None:
    result = run_speedtest(
        store, "wan", download=lambda size: 12_500_000.0, upload=lambda size: 2_500_000.0
    )
    assert result.down_mbps == 100.0
    assert result.up_mbps == 20.0
    latest = store.latest("wan")
    assert latest["speedtest.down_bytes_s"][1] == 12_500_000.0


def test_speedtest_never_runs_from_the_collector() -> None:
    """The guarantee that matters on a metered plan: nothing schedules it."""
    import inspect

    from netpulse import monitor

    assert "speedtest" not in inspect.getsource(monitor).lower()


# ------------------------------------------------------------------ notifications


def test_notifications_throttle_per_key_but_not_across_directions(clock: Clock) -> None:
    sent: list[str] = []
    notifier = Notifier(deliver=lambda title, body: sent.append(title), clock=clock)

    assert notifier.send("outage:wan", "wan is down", "")
    assert not notifier.send("outage:wan", "wan is down", "")  # throttled
    assert notifier.send("outage:wan:cleared", "wan is back", "")  # different direction
    clock.advance(seconds=61)
    assert notifier.send("outage:wan", "wan is down", "")
    assert sent == ["wan is down", "wan is back", "wan is down"]


def test_a_broken_delivery_backend_is_silence_not_a_crash(clock: Clock) -> None:
    def boom(title: str, body: str) -> None:
        raise RuntimeError("no notification daemon")

    notifier = Notifier(deliver=boom, clock=clock)
    assert notifier.send("outage:wan", "wan is down", "") is False


def test_the_collector_announces_down_and_back_with_duration(store: Store, clock: Clock) -> None:
    sent: list[str] = []
    notifier = Notifier(deliver=lambda title, body: sent.append(title), clock=clock)
    ok = Reading(metrics={"up": 1.0})
    script: list[Reading | Exception] = [AdapterError("down")] * 3 + [ok]
    collector = Collector(store, [ScriptedAdapter("wan", script)], clock=clock, notifier=notifier)
    for _ in range(12):
        collector.poll_once()
        clock.advance(seconds=60)

    assert any("down" in title for title in sent)
    assert any("back after" in title for title in sent)
