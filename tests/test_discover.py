"""Finding the router, because nobody should hand-edit a config file for their own network."""

from __future__ import annotations

import json
from pathlib import Path

from netpulse.config import SourceConfig, load, save_sources
from netpulse.discover import discover
from netpulse.quality import assess
from netpulse.storage import Store
from tests.conftest import Clock

HUAWEI_TOKENS = b"<response><SesInfo>SessionID=x</SesInfo><TokInfo>t</TokInfo></response>"
HUAWEI_NAME = b"<response><devicename>B535-232</devicename></response>"
ZTE_STATUS = json.dumps({"network_type": "LTE", "ppp_status": "ppp_connected"}).encode()


def fetch_for(network: dict[str, dict[str, bytes]]):  # type: ignore[no-untyped-def]
    def fetch(url: str, headers: dict[str, str]) -> bytes:
        for address, routes in network.items():
            if address in url:
                for path, payload in routes.items():
                    if path in url:
                        return payload
        raise OSError("no route to host")

    return fetch


def test_a_huawei_router_is_found_at_the_gateway() -> None:
    network = {
        "192.168.8.1": {"SesTokInfo": HUAWEI_TOKENS, "basic_information": HUAWEI_NAME},
    }
    found = discover(gateway="192.168.8.1", fetch=fetch_for(network))
    assert len(found) == 1
    assert found[0].kind == "huawei"
    assert found[0].url == "http://192.168.8.1"
    assert found[0].label == "B535-232"


def test_a_zte_router_is_found_on_a_well_known_address() -> None:
    network = {"192.168.0.1": {"goform_get_cmd_process": ZTE_STATUS}}
    found = discover(gateway="10.0.0.1", fetch=fetch_for(network))
    assert [item.kind for item in found] == ["zte"]


def test_nothing_answering_means_an_empty_list_not_an_error() -> None:
    found = discover(gateway="10.0.0.1", fetch=fetch_for({}))
    assert found == []


def test_an_ordinary_web_server_is_not_mistaken_for_a_router() -> None:
    """A captive portal or NAS answering 200 with HTML must not become a source."""
    network = {
        "192.168.1.1": {
            "SesTokInfo": b"<html><body>login</body></html>",
            "goform_get_cmd_process": b"<html>404</html>",
        }
    }
    assert discover(gateway="192.168.1.1", fetch=fetch_for(network)) == []


def test_the_gateway_is_probed_even_when_unusual() -> None:
    network = {"10.20.30.1": {"SesTokInfo": HUAWEI_TOKENS}}
    found = discover(gateway="10.20.30.1", fetch=fetch_for(network))
    assert found[0].url == "http://10.20.30.1"
    assert found[0].label == "Huawei router"  # the name endpoint is optional garnish


def test_saved_sources_round_trip_through_the_config(tmp_path: Path) -> None:
    location = tmp_path / "netpulse.toml"
    save_sources(
        [
            SourceConfig(name="mtn", kind="huawei", options={"url": "http://192.168.8.1"}),
            SourceConfig(name="wan", kind="probe"),
        ],
        location,
    )
    config = load(location)
    assert [source.kind for source in config.sources] == ["huawei", "probe"]
    assert config.sources[0].options["url"] == "http://192.168.8.1"


# ------------------------------------------------------------------ quality grade


def seed_latency(store: Store, clock: Clock, values: list[float]) -> None:
    for value in values:
        store.record("wan", {"latency.internet_ms": value, "loss.pct": 0.0, "up": 1.0})
        clock.advance(seconds=5)


def test_a_steady_link_grades_well(store: Store, clock: Clock) -> None:
    seed_latency(store, clock, [45.0 + (i % 3) for i in range(200)])
    graded = assess(store, "wan", clock.now)
    assert graded is not None
    assert graded.grade in ("A", "B")
    assert graded.p50_ms < 50


def test_a_spiky_link_is_marked_down_for_jitter(store: Store, clock: Clock) -> None:
    """Same median as the steady link, but unusable for calls — the grade must say so."""
    values = [40.0 if i % 2 == 0 else 700.0 for i in range(200)]
    seed_latency(store, clock, values)
    graded = assess(store, "wan", clock.now)
    assert graded is not None
    assert graded.grade in ("D", "F")
    assert graded.jitter_ms > 100


def test_too_little_history_gives_no_grade_rather_than_a_rash_one(
    store: Store, clock: Clock
) -> None:
    seed_latency(store, clock, [50.0] * 5)
    assert assess(store, "wan", clock.now) is None


def test_a_reqproc_only_zte_firmware_is_still_found() -> None:
    """Real hardware feedback: an MTN ZTE box answered /reqproc/proc_get, not /goform."""
    network = {"192.168.0.1": {"reqproc/proc_get": ZTE_STATUS}}
    found = discover(gateway="192.168.0.1", fetch=fetch_for(network))
    assert [item.kind for item in found] == ["zte"]


def test_an_unidentified_router_page_is_reported_not_hidden() -> None:
    """Discovery saw a router; saying nothing would leave the user thinking it saw none."""
    page = b"<html><head><title>MTN Router</title></head><body>zlt login</body></html>"

    def fetch(url: str, headers: dict[str, str]) -> bytes:
        if "192.168.0.1" in url and url.rstrip("/").endswith("192.168.0.1"):
            return page
        raise OSError("no route")

    found = discover(gateway="192.168.0.1", fetch=fetch)
    assert len(found) == 1
    assert found[0].kind == "unknown"
    assert "ZLT" in found[0].label


def test_zte_adapter_falls_back_to_reqproc() -> None:
    import json as jsonlib

    from netpulse.adapters.zte import ZteAdapter

    payload = jsonlib.dumps(
        {"ppp_status": "ppp_connected", "lte_rsrp": "-98", "network_type": "LTE"}
    ).encode()
    calls: list[str] = []

    def fetch(url: str, headers: dict[str, str]) -> bytes:
        calls.append(url)
        if "/reqproc/proc_get" in url:
            return payload
        raise OSError("404")

    adapter = ZteAdapter("mtn", url="http://192.168.0.1/#/", fetch=fetch)
    reading = adapter.read()
    assert reading.metrics["up"] == 1.0
    assert adapter.base == "http://192.168.0.1"  # the SPA route is stripped

    adapter.read()
    # The working path is remembered; the dead one is not retried every sweep.
    assert sum("/goform/" in call for call in calls) == 1
