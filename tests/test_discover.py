"""Finding the router, because nobody should hand-edit a config file for their own network."""

from __future__ import annotations

import json
import re
from pathlib import Path

from netpulse.analysis.quality import assess
from netpulse.config import SourceConfig, load, save_sources
from netpulse.core.storage import Store
from netpulse.sources.discover import discover
from tests.conftest import Clock

HUAWEI_TOKENS = b"<response><SesInfo>SessionID=x</SesInfo><TokInfo>t</TokInfo></response>"
HUAWEI_NAME = b"<response><devicename>B535-232</devicename></response>"
ZTE_STATUS = json.dumps({"network_type": "LTE", "ppp_status": "ppp_connected"}).encode()


def fetch_for(network: dict[str, dict[str, bytes]]):  # type: ignore[no-untyped-def]
    def fetch(url: str, headers: dict[str, str], body: bytes | None = None) -> bytes:
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
    assert found[0].label == "Huawei B535-232"


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
    assert found[0].label == "Huawei"  # the name endpoint is optional garnish


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

    def fetch(url: str, headers: dict[str, str], body: bytes | None = None) -> bytes:
        if "192.168.0.1" in url and url.rstrip("/").endswith("192.168.0.1"):
            return page
        raise OSError("no route")

    found = discover(gateway="192.168.0.1", fetch=fetch)
    assert len(found) == 1
    assert not found[0].supported  # named, but nothing can poll it yet
    assert "ZLT" in found[0].label
    assert found[0].note  # and it says what would help


def test_zte_adapter_falls_back_to_reqproc() -> None:
    import json as jsonlib

    from netpulse.sources.zte import ZteAdapter

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


# ------------------------------------------------------------------ the vendor registry


def test_every_vendor_probe_is_read_only() -> None:
    """A scan must never be the reason a fragile CPE box reboots. Nothing in the
    registry may ask a router to change, restart, or forget anything."""
    from netpulse.sources.vendors import VENDORS

    forbidden = (
        "reboot",
        "restart",
        "restore",
        "reset",
        "set",
        "delete",
        "upgrade",
        "logout",
        "factory",
    )
    for vendor in VENDORS:
        for signature in vendor.signatures:
            # Bodies are not all text — Starlink's probe is a binary gRPC-Web frame.
            body = (signature.body or b"").decode("utf-8", errors="replace")
            target = (signature.path + body).lower()
            for word in forbidden:
                # Matched at a token start only: "currentsetting.htm" is a read, and a
                # guard that cannot tell it from "setConfig" gets switched off, which
                # would cost more than the false positive it was catching.
                assert not re.search(rf"(?<![a-z]){word}", target), (
                    f"{vendor.name} probe looks like a write: {word}"
                )


def test_no_vendor_probe_carries_a_credential() -> None:
    """Fingerprinting happens before any login, and must stay that way — a scanner that
    needed a password would have to be told one for every address it tries."""
    from netpulse.sources.vendors import VENDORS

    for vendor in VENDORS:
        for signature in vendor.signatures:
            body = (signature.body or b"").decode("utf-8", errors="replace")
            blob = signature.path + str(signature.headers) + body
            for secret in ("password", "passwd", "Authorization", "token="):
                assert secret.lower() not in blob.lower(), f"{vendor.name} probe carries a secret"


def test_a_matcher_refuses_a_payload_from_another_family() -> None:
    """Cross-matching would make discovery confidently wrong, which is worse than
    silent: it sends someone to configure an adapter that can never work."""
    from netpulse.sources.vendors import VENDORS

    payloads = {
        "Huawei": HUAWEI_TOKENS,
        "ZLT": json.dumps({"success": True, "cmd": 113, "board_type": "ZLT X17U"}).encode(),
        "ZTE": ZTE_STATUS,
    }
    for vendor in VENDORS:
        if vendor.name not in payloads:
            continue
        for other, payload in payloads.items():
            if other == vendor.name:
                assert vendor.match(payload) is not None, f"{vendor.name} rejects its own"
            elif vendor.name:  # the page-sniffer is deliberately permissive
                assert vendor.match(payload) is None, f"{vendor.name} claimed a {other} reply"


def test_the_gateway_is_always_tried_first() -> None:
    """Whatever the vendor defaults say, the box actually routing this machine's
    traffic is the likeliest router on the network."""
    from netpulse.sources.vendors import candidate_addresses

    assert candidate_addresses("10.20.30.1")[0] == "10.20.30.1"
    assert "192.168.8.1" in candidate_addresses("10.20.30.1")


def test_each_host_is_asked_one_question_at_a_time() -> None:
    """Parallel across hosts, serial within one: a burst of concurrent requests is
    exactly what a fragile embedded box handles worst."""
    import threading

    concurrent: dict[str, int] = {}
    peak = 0
    lock = threading.Lock()

    def fetch(url: str, headers: dict[str, str], body: bytes | None = None) -> bytes:
        nonlocal peak
        host = url.split("/")[2]
        with lock:
            concurrent[host] = concurrent.get(host, 0) + 1
            peak = max(peak, concurrent[host])
        try:
            raise OSError("no route")
        finally:
            with lock:
                concurrent[host] -= 1

    discover(gateway="10.0.0.1", fetch=fetch)
    assert peak == 1


def test_a_supported_router_is_offered_before_a_merely_named_one() -> None:
    """The thing you can actually watch should be the thing you are offered first."""
    page = b"<html><title>Tenda</title><body>router login</body></html>"

    def fetch(url: str, headers: dict[str, str], body: bytes | None = None) -> bytes:
        if "192.168.1.1" in url and url.rstrip("/").endswith("192.168.1.1"):
            return page
        if "192.168.8.1" in url and "SesTokInfo" in url:
            return HUAWEI_TOKENS
        raise OSError("no route")

    found = discover(gateway="192.168.1.1", fetch=fetch)
    assert [item.supported for item in found] == [True, False]
    assert found[0].kind == "huawei"
