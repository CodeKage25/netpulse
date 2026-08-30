"""The API the dashboard lives on, exercised over real HTTP on a random port."""

from __future__ import annotations

import json
import urllib.request
from datetime import timedelta

import pytest

from netpulse.core.model import Reading
from netpulse.core.storage import Store
from netpulse.monitor import Collector
from netpulse.sources.fake import ScriptedAdapter
from netpulse.web.server import Api, serve
from tests.conftest import Clock

READING = Reading(
    metrics={
        "latency.internet_ms": 72.0,
        "latency.gateway_ms": 3.0,
        "loss.pct": 0.0,
        "signal.sinr_db": 11.0,
        "up": 1.0,
    },
    texts={"net.type": "LTE", "net.operator": "MTN Nigeria"},
)


@pytest.fixture
def served(store: Store, clock: Clock):  # type: ignore[no-untyped-def]
    adapter = ScriptedAdapter("mtn", [READING] * 50)
    collector = Collector(store, [adapter], clock=clock)
    for _ in range(20):
        collector.poll_once()
        clock.advance(seconds=5)

    api = Api(store, collector, interval_s=5, clock=clock)
    server = serve(api, port=0)
    port = server.server_address[1]
    import threading

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{port}"
    server.shutdown()


def get(base: str, path: str) -> dict:
    with urllib.request.urlopen(base + path, timeout=5) as response:
        return json.loads(response.read())


def test_overview_carries_tiles_labels_and_coverage(served: str) -> None:
    overview = get(served, "/api/overview")
    source = overview["sources"][0]
    assert source["name"] == "mtn"
    assert source["up"] is True
    assert source["latest"]["latency.internet_ms"] == 72.0
    assert source["texts"]["net.operator"] == "MTN Nigeria"
    assert 0 < source["coverage"] <= 1
    assert "latency.internet_ms" in source["sparklines"]


def test_history_returns_buckets_and_coverage(served: str) -> None:
    payload = get(
        served, "/api/history?source=mtn&metric=latency.internet_ms&minutes=15&buckets=30"
    )
    assert len(payload["points"]) == 30
    assert any(value is not None for value in payload["points"])
    assert payload["coverage"] > 0


def test_unknown_metric_returns_empty_buckets_not_an_error(served: str) -> None:
    payload = get(served, "/api/history?source=mtn&metric=no.such_metric&minutes=15&buckets=10")
    assert all(value is None for value in payload["points"])


def test_insights_endpoint_shapes_findings(served: str) -> None:
    payload = get(served, "/api/insights?source=mtn")
    assert isinstance(payload["insights"], list)


def test_the_dashboard_page_is_self_contained(served: str) -> None:
    with urllib.request.urlopen(served + "/", timeout=5) as response:
        html = response.read().decode()
    assert "NetPulse" in html
    # Must render during an outage: no external scripts, styles or fonts.
    # No external resource ever loads — a page that fetches during an outage is a page
    # that cannot explain the outage. Prose may still name an address (the router's).
    for attribute in ('src="http', "src='http", 'href="http', "href='http", "@import"):
        assert attribute not in html
    # The assets are authored separately and stitched in; none may survive as a
    # placeholder, or the page ships with a hole where its stylesheet should be.
    assert "{{" not in html
    assert "<style>" in html and "position: relative" in html  # css really inlined
    assert "async function refresh()" in html  # and so did the script
    assert "https://" not in html
    assert "<script src" not in html


def test_unknown_paths_are_a_json_404(served: str) -> None:
    try:
        urllib.request.urlopen(served + "/nope", timeout=5)
    except urllib.error.HTTPError as error:
        assert error.code == 404
        assert json.loads(error.read())["error"] == "not found"
    else:
        raise AssertionError("expected a 404")


def test_quality_is_served_over_http(served: str) -> None:
    payload = get(served, "/api/quality?source=mtn")
    assert payload["quality"] is not None
    assert payload["quality"]["grade"] in "ABCDF"


def test_a_source_can_be_added_while_running(served: str) -> None:
    """The whole point of discovery: pointing at a router must not need a restart."""
    import urllib.request

    request = urllib.request.Request(
        served + "/api/sources?kind=demo&name=added-demo", method="POST"
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        assert json.loads(response.read())["added"] == "added-demo"

    overview = get(served, "/api/overview")
    names = [source["name"] for source in overview["sources"]]
    assert "mtn" in names  # note: added source appears after its first poll records


def test_uptime_counts_time_not_polls(store: Store, clock: Clock) -> None:
    """The collector backs off while a source fails, so one down poll covers far more
    wall clock than one up poll. Counting polls read 99.17% on a real day where the
    truth was 79.89% — nineteen points, in the flattering direction."""
    from netpulse.analysis.export import uptime
    from netpulse.core.model import EventKind, Severity

    for _ in range(60):  # five minutes up, polled every five seconds
        store.record("wan", {"up": 1.0})
        clock.advance(seconds=5)

    # Five minutes down, but backed off to only four polls in that time.
    started = clock.now
    event = store.open_event("wan", EventKind.OUTAGE, Severity.CRITICAL, "down", at=started)
    for _ in range(4):
        store.record("wan", {"up": 0.0})
        clock.advance(seconds=75)
    store.close_event(event, at=clock.now)

    fraction, up_seconds, down_seconds = uptime(
        store, "wan", started - timedelta(minutes=10), clock.now, interval_s=5.0
    )
    assert up_seconds == 300.0
    assert down_seconds == 300.0
    assert fraction == pytest.approx(0.5)
    # Counting polls would have said 60/64 = 94%, because four polls spanned five minutes.


def test_a_blip_too_short_to_be_an_outage_still_costs_uptime(store: Store, clock: Clock) -> None:
    """A link that fails every other minute without ever tripping the outage threshold
    is not a flawless link."""
    from netpulse.analysis.export import uptime

    since = clock.now
    for index in range(100):
        store.record("wan", {"up": 0.0 if index % 10 == 0 else 1.0})
        clock.advance(seconds=5)

    fraction, _, down = uptime(store, "wan", since, clock.now, interval_s=5.0)
    assert down == 50.0  # ten failed polls, no outage event opened
    assert fraction == pytest.approx(0.9)


def test_an_outage_beginning_before_the_window_is_clipped_to_it(store: Store, clock: Clock) -> None:
    from netpulse.analysis.export import uptime
    from netpulse.core.model import EventKind, Severity

    long_ago = clock.now
    event = store.open_event("wan", EventKind.OUTAGE, Severity.CRITICAL, "down", at=long_ago)
    clock.advance(hours=2)
    window_start = clock.now
    clock.advance(minutes=10)
    store.close_event(event, at=clock.now)
    for _ in range(12):
        store.record("wan", {"up": 1.0})
        clock.advance(seconds=5)

    _, _, down = uptime(store, "wan", window_start, clock.now, interval_s=5.0)
    assert down == pytest.approx(600.0)  # the ten minutes inside the window, not 130


def test_the_distribution_reports_raw_extremes_not_bucketed_ones(
    store: Store, clock: Clock
) -> None:
    """Latency buckets keep the worst value, so the smallest of those maxima is the best
    bad minute, not the best reading. The detail view's Best figure must not inherit it.
    """
    from netpulse.monitor import Collector
    from netpulse.sources.fake import ScriptedAdapter
    from netpulse.web.server import Api

    for value in [150.0] + [200.0] * 60 + [1300.0]:
        store.record("wan", {"latency.internet_ms": value})
        clock.advance(seconds=5)
    api = Api(
        store,
        Collector(store, [ScriptedAdapter("wan", [])], clock=clock),
        interval_s=5,
        clock=clock,
    )

    dist = api.distribution("wan", "latency.internet_ms", 60)
    assert dist["min"] == 150.0
    assert dist["max"] == 1300.0
    # The lone spike must not flatten every real sample into one bin.
    assert dist["overflowing"] is True
    assert dist["bins"][-1]["hi"] < 1300.0
    assert sum(b["count"] for b in dist["bins"]) == dist["count"]
