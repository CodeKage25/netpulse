"""Getting the data out, without losing what makes it trustworthy on the way."""

from __future__ import annotations

import csv
import io
import json
from datetime import timedelta

from netpulse.analysis.export import prometheus, series, to_csv, to_json, uptime_report
from netpulse.core.model import EventKind, Severity
from netpulse.core.storage import Store
from tests.conftest import Clock

# ------------------------------------------------------------------ prometheus


def test_the_exposition_carries_one_series_per_source() -> None:
    text = prometheus(
        latest={"mtn": {"latency.internet_ms": 45.0, "up": 1.0}, "wan": {"up": 0.0}},
        texts={"mtn": {"net.operator": "MTN-NG"}},
        coverage={"mtn": 0.97},
    )
    assert "# TYPE netpulse_latency_milliseconds gauge" in text
    assert 'netpulse_latency_milliseconds{source="mtn"} 45' in text
    assert 'netpulse_up{source="wan"} 0' in text
    assert 'netpulse_coverage_ratio{source="mtn"} 0.97' in text
    assert 'net_operator="MTN-NG"' in text


def test_a_metric_nobody_reports_is_absent_not_zero() -> None:
    """A scraper filling in a zero for a signal reading nobody took would draw a cliff."""
    text = prometheus({"wan": {"up": 1.0}}, {}, {})
    assert "netpulse_signal_rsrp_dbm" not in text


def test_label_values_are_escaped() -> None:
    """An operator name with a quote in it must not produce unparseable exposition."""
    text = prometheus({"a": {"up": 1.0}}, {"a": {"net.operator": 'MTN "NG"'}}, {})
    assert r'net_operator="MTN \"NG\""' in text


def test_every_exported_name_is_a_legal_prometheus_name() -> None:
    from netpulse.analysis.export import PROM_NAMES

    for metric, (name, kind, help_text) in PROM_NAMES.items():
        assert name.replace("_", "").isalnum(), f"{metric} exports an illegal name"
        assert kind in ("gauge", "counter")
        assert help_text and not help_text.endswith("."), f"{metric} help reads oddly"


# ------------------------------------------------------------------ csv and json


def seed(store: Store, clock: Clock, count: int) -> None:
    for index in range(count):
        store.record("wan", {"latency.internet_ms": 40.0 + index, "up": 1.0})
        clock.advance(seconds=5)


def test_a_gap_is_an_empty_cell_not_a_zero(store: Store, clock: Clock) -> None:
    """A spreadsheet reads an empty cell as no data and a zero as a measurement, and
    only one of those is true."""
    seed(store, clock, 12)
    clock.advance(minutes=20)  # a hole
    seed(store, clock, 12)

    header, rows = series(
        store, "wan", ["latency.internet_ms"], clock.now - timedelta(hours=1), clock.now, 30
    )
    text = to_csv(header, rows)
    parsed = list(csv.reader(io.StringIO(text)))
    assert parsed[0] == ["time", "latency.internet_ms"]
    blanks = [row for row in parsed[1:] if row[1] == ""]
    assert blanks, "the unrecorded stretch should leave empty cells"
    assert "0" not in {row[1] for row in blanks}


def test_unrecorded_buckets_keep_their_row(store: Store, clock: Clock) -> None:
    """Dropping them would let the survivors close ranks and hide a missed hour."""
    seed(store, clock, 6)
    clock.advance(minutes=40)
    _, rows = series(
        store, "wan", ["latency.internet_ms"], clock.now - timedelta(hours=1), clock.now, 60
    )
    assert len(rows) == 60
    assert any(row[1] is None for row in rows)


def test_several_metrics_share_one_time_axis(store: Store, clock: Clock) -> None:
    for _ in range(20):
        store.record("wan", {"latency.internet_ms": 50.0, "loss.pct": 0.0, "up": 1.0})
        clock.advance(seconds=5)
    header, rows = series(
        store,
        "wan",
        ["latency.internet_ms", "loss.pct"],
        clock.now - timedelta(minutes=10),
        clock.now,
        20,
    )
    assert header == ["time", "latency.internet_ms", "loss.pct"]
    assert all(len(row) == 3 for row in rows)


def test_the_json_export_states_its_coverage(store: Store, clock: Clock) -> None:
    """A number pasted into a complaint should still say how much of the window it
    stands on."""
    seed(store, clock, 12)
    header, rows = series(
        store, "wan", ["latency.internet_ms"], clock.now - timedelta(hours=1), clock.now, 20
    )
    payload = json.loads(to_json(header, rows, "wan", 0.17))
    assert payload["source"] == "wan"
    assert payload["coverage"] == 0.17
    assert payload["columns"] == header


# ------------------------------------------------------------------ uptime report


def test_uptime_and_coverage_are_reported_separately(store: Store, clock: Clock) -> None:
    """A 99.9% uptime over 3% coverage is not a 99.9% month, and the report must not
    let anyone read it as one."""
    for _ in range(20):
        store.record("wan", {"up": 1.0})
        clock.advance(seconds=5)
    clock.advance(days=6)

    report = uptime_report(store, "wan", clock.now - timedelta(days=7), clock.now, interval_s=5)
    assert report["uptime"] == 1.0
    assert report["coverage"] < 0.01
    assert report["polls_recorded"] == 20


def test_outages_are_counted_and_measured(store: Store, clock: Clock) -> None:
    start = clock.now
    for _ in range(10):
        store.record("wan", {"up": 1.0})
        clock.advance(seconds=5)
    event = store.open_event("wan", EventKind.OUTAGE, Severity.CRITICAL, "down", at=clock.now)
    clock.advance(minutes=4)
    store.close_event(event, at=clock.now)
    for _ in range(10):
        store.record("wan", {"up": 1.0})
        clock.advance(seconds=5)

    report = uptime_report(store, "wan", start, clock.now, interval_s=5)
    assert report["outages"] == 1
    assert report["downtime_seconds"] == 240
    assert report["longest_outage_seconds"] == 240


def test_an_ongoing_outage_is_measured_to_now_not_left_open(store: Store, clock: Clock) -> None:
    """An outage still running has still cost you the time it has run for."""
    start = clock.now
    store.record("wan", {"up": 0.0})
    store.open_event("wan", EventKind.OUTAGE, Severity.CRITICAL, "down", at=clock.now)
    clock.advance(minutes=10)
    report = uptime_report(store, "wan", start, clock.now, interval_s=5)
    assert report["downtime_seconds"] == 600


def test_nothing_recorded_gives_no_uptime_rather_than_a_perfect_score(
    store: Store, clock: Clock
) -> None:
    """An empty week is not a flawless week."""
    report = uptime_report(store, "wan", clock.now - timedelta(days=7), clock.now, interval_s=5)
    assert report["uptime"] is None
    assert report["coverage"] == 0.0


# ------------------------------------------------------------------ speed test history


def api_for(store: Store, clock: Clock):  # type: ignore[no-untyped-def]
    from netpulse.monitor import Collector
    from netpulse.sources.fake import ScriptedAdapter
    from netpulse.web.api import Api

    return Api(
        store,
        Collector(store, [ScriptedAdapter("wan", [])], clock=clock),
        interval_s=5,
        clock=clock,
    )


def test_past_runs_come_back_newest_first(store: Store, clock: Clock) -> None:
    """Dishylink keeps no speed-test history at all; its result is gone on the next
    render. Whether the link is getting worse needs the runs kept."""
    for mbps in (10.0, 20.0, 30.0):
        store.record(
            "wan",
            {"speedtest.down_bytes_s": mbps * 1e6 / 8, "speedtest.up_bytes_s": mbps * 1e5 / 8},
        )
        clock.advance(days=1)

    history = api_for(store, clock).speedtest_history("wan", days=30)
    assert history["count"] == 3
    assert history["runs"][0]["down_mbps"] == 30.0  # newest first
    assert history["runs"][0]["up_mbps"] == 3.0


def test_a_trend_needs_enough_runs_to_mean_anything(store: Store, clock: Clock) -> None:
    """Two points are a line through noise, not a direction."""
    for _ in range(2):
        store.record("wan", {"speedtest.down_bytes_s": 5e6})
        clock.advance(days=1)
    assert api_for(store, clock).speedtest_history("wan", days=30)["trend_pct"] is None


def test_a_degrading_link_reports_a_negative_trend(store: Store, clock: Clock) -> None:
    for mbps in (40.0, 40.0, 20.0, 20.0):
        store.record("wan", {"speedtest.down_bytes_s": mbps * 1e6 / 8})
        clock.advance(days=1)
    assert api_for(store, clock).speedtest_history("wan", days=30)["trend_pct"] == -50.0


def test_a_run_with_no_upload_reading_says_so_rather_than_zero(store: Store, clock: Clock) -> None:
    store.record("wan", {"speedtest.down_bytes_s": 5e6})
    history = api_for(store, clock).speedtest_history("wan", days=30)
    assert history["runs"][0]["up_mbps"] is None
