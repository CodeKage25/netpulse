"""The API the dashboard lives on, exercised over real HTTP on a random port."""

from __future__ import annotations

import json
import urllib.request

import pytest

from netpulse.adapters.fake import ScriptedAdapter
from netpulse.model import Reading
from netpulse.monitor import Collector
from netpulse.server import Api, serve
from netpulse.storage import Store
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
    assert "http://" not in html.replace("http://127.0.0.1", "")
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


def test_uptime_is_a_poll_fraction_not_a_bucket_minimum(store: Store, clock: Clock) -> None:
    """One bad minute in an otherwise clean day must read ~96%, not 0%."""
    from netpulse.adapters.fake import ScriptedAdapter
    from netpulse.monitor import Collector
    from netpulse.server import Api

    for i in range(300):
        store.record("wan", {"up": 0.0 if 100 <= i < 112 else 1.0})
        clock.advance(seconds=5)
    api = Api(
        store,
        Collector(store, [ScriptedAdapter("wan", [])], clock=clock),
        interval_s=5,
        clock=clock,
    )
    uptime = api._uptime("wan", clock.now)
    assert uptime is not None
    assert 0.94 < uptime < 0.98
