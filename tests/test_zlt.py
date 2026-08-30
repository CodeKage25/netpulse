"""The ZLT/Tozed adapter, tested against payloads captured from a live MTN X17U.

Fixtures copied verbatim from the router rather than invented, so the tests fail if the
adapter stops matching real firmware rather than stopping matching my idea of it.
"""

from __future__ import annotations

import json

import pytest

from netpulse.sources import AdapterError
from netpulse.sources.discover import discover
from netpulse.sources.zlt import COUNTER_WRAP, ZltAdapter

# cmd 133 — WAN state and the cumulative byte counters. Trimmed to the fields read.
WAN = {
    "success": True,
    "cmd": 133,
    "SINR": "-1",
    "RSRP": "-94",
    "RSSI": "-80",
    "RSRQ": "-13",
    "RSRP_5G": "-87",
    "RSRQ_5G": "-12",
    "SINR_5G": "3",
    "uptime": "8515",
    "wan_ip": "10.101.35.178",
    "wan_gateway": "0.0.0.0",
    "wan_rx_bytes": "3801008336",
    "wan_tx_bytes": "301803122",
    "apn_name": "web.gprs.mtnnigeria.net",
    "network_type_str": "5G(NSA)",
    "CELL_ID": "53A803F",
    "currentband": "7+20+3+7",
    "real_fwversion": "4.2.3",
    # Empty on this unit — the reason the adapter differences counters itself.
    "netWanRxRate": "",
    "netWanTxRate": "",
    "wired_wan_ip": "",
}
# cmd 205 — the richer RF sweep.
RF = {
    "success": True,
    "cmd": 205,
    "RSRP": "-90",
    "SINR": "0",
    "signal_lvl": "4",
    "network_operator": "MTN-NG",
    "currentband_5g": "78",
    "mon_total_flow": "18988.77",
    "flow_dl": "3620.14",
    "flow_ul": "287.53",
    "network_type_str": "5G(NSA)",
}
# cmd 113 — the liveness read discovery uses to identify the board.
STATUS = {
    "success": True,
    "cmd": 113,
    "board_type": "ZLT X17U",
    "network_operator": "MTN-NG",
    "signal_lvl": "4",
    "uptime": "8485",
}


def responder(*, wan=WAN, rf=RF, status=STATUS):  # type: ignore[no-untyped-def]
    """Answers by the cmd in the body, the way the firmware does."""

    def fetch(url: str, body: bytes, headers: dict[str, str]) -> bytes:
        if "/cgi-bin/http.cgi" not in url:
            raise OSError("404")
        cmd = json.loads(body)["cmd"]
        payload = {133: wan, 205: rf, 113: status}.get(cmd)
        if payload is None:
            return json.dumps({"success": False, "message": "NO_AUTH"}).encode()
        return json.dumps(payload).encode()

    return fetch


def test_a_live_router_reading_carries_signal_and_state() -> None:
    reading = ZltAdapter("mtn", fetch=responder()).read()
    assert reading.metrics["up"] == 1.0
    assert reading.metrics["signal.rsrp_dbm"] == -94.0
    assert reading.metrics["signal.sinr_db"] == -1.0
    assert reading.metrics["signal.rsrp_5g_dbm"] == -87.0
    assert reading.metrics["router.uptime_s"] == 8515.0
    assert reading.texts["net.type"] == "5G(NSA)"
    assert reading.texts["net.operator"] == "MTN-NG"  # the RF sweep runs on cycle 1
    assert reading.texts["signal.band"] == "7+20+3+7"


def test_no_wan_ip_is_down_even_when_the_radio_looks_fine() -> None:
    """The firmware's own network_status reads 1 while the link is dead; an address on
    the WAN interface is the honest test."""
    reading = ZltAdapter("mtn", fetch=responder(wan={**WAN, "wan_ip": ""})).read()
    assert reading.metrics["up"] == 0.0
    assert reading.metrics["signal.rsrp_dbm"] == -94.0  # still measured, still recorded


def test_the_first_poll_reports_no_throughput() -> None:
    """One counter reading is a total, not a rate. Guessing one would be inventing it."""
    reading = ZltAdapter("mtn", fetch=responder()).read()
    assert "traffic.down_bytes_s" not in reading.metrics


def test_throughput_comes_from_counter_deltas_over_uptime() -> None:
    later = {
        **WAN,
        "uptime": "8525",
        "wan_rx_bytes": str(3801008336 + 12_500_000),
        "wan_tx_bytes": str(301803122 + 1_000_000),
    }
    adapter = ZltAdapter("mtn", fetch=responder())
    adapter.read()
    adapter._fetch = responder(wan=later)
    reading = adapter.read()
    assert reading.metrics["traffic.down_bytes_s"] == pytest.approx(1_250_000)
    assert reading.metrics["traffic.up_bytes_s"] == pytest.approx(100_000)


def test_a_reboot_re_baselines_instead_of_publishing_a_burst() -> None:
    """Uptime rewinding means the counters restarted; the delta is meaningless."""
    rebooted = {**WAN, "uptime": "30", "wan_rx_bytes": "900000", "wan_tx_bytes": "40000"}
    adapter = ZltAdapter("mtn", fetch=responder())
    adapter.read()
    adapter._fetch = responder(wan=rebooted)
    assert "traffic.down_bytes_s" not in adapter.read().metrics


def test_a_32_bit_counter_wrap_is_unwrapped_not_reported_as_negative() -> None:
    """These counters sit near 2^32 within hours, so the wrap is routine, not exotic."""
    near = {**WAN, "wan_rx_bytes": str(COUNTER_WRAP - 1_000_000), "uptime": "8515"}
    after = {
        **WAN,
        "wan_rx_bytes": "1000000",
        "uptime": "8525",
        "wan_tx_bytes": str(301803122 + 500_000),
    }
    adapter = ZltAdapter("mtn", fetch=responder(wan=near))
    adapter.read()
    adapter._fetch = responder(wan=after)
    reading = adapter.read()
    assert reading.metrics["traffic.down_bytes_s"] == pytest.approx(200_000)


def test_a_failing_rf_sweep_does_not_fail_the_poll() -> None:
    """The WAN read is what the outage detector needs; losing the garnish is not an
    outage, and reporting it as one would be a lie the charts then keep."""

    def fetch(url: str, body: bytes, headers: dict[str, str]) -> bytes:
        if json.loads(body)["cmd"] == 205:
            return json.dumps({"success": False, "message": "NO_AUTH"}).encode()
        return json.dumps(WAN).encode()

    reading = ZltAdapter("mtn", fetch=fetch).read()
    assert reading.metrics["up"] == 1.0
    assert "net.operator" not in reading.texts


def test_the_heavy_rf_sweep_rides_every_sixth_cycle() -> None:
    """Poll gently: a CPE box watchdog-reboots under an eager monitor."""
    seen: list[int] = []

    def fetch(url: str, body: bytes, headers: dict[str, str]) -> bytes:
        cmd = json.loads(body)["cmd"]
        seen.append(cmd)
        return json.dumps(WAN if cmd == 133 else RF).encode()

    adapter = ZltAdapter("mtn", fetch=fetch)
    for _ in range(12):
        adapter.read()
    assert seen.count(133) == 12
    assert seen.count(205) == 2


def test_an_unauthenticated_refusal_is_a_failed_poll_not_a_silent_zero() -> None:
    def refuse(url: str, body: bytes, headers: dict[str, str]) -> bytes:
        return json.dumps({"success": False, "message": "NO_AUTH"}).encode()

    with pytest.raises(AdapterError, match="NO_AUTH"):
        ZltAdapter("mtn", fetch=refuse).read()


def test_an_http_200_carrying_success_false_is_still_a_failure() -> None:
    """This firmware never uses status codes for errors, so the status line proves
    nothing and only the success flag can be trusted."""

    def broken(url: str, body: bytes, headers: dict[str, str]) -> bytes:
        return json.dumps({"success": False, "cmd": 133, "message": "ERROR"}).encode()

    with pytest.raises(AdapterError):
        ZltAdapter("mtn", fetch=broken).read()


def test_discovery_names_the_board_it_found() -> None:
    def fetch(url: str, headers: dict[str, str], body: bytes | None = None) -> bytes:
        if "192.168.0.1" in url and "/cgi-bin/http.cgi" in url:
            return json.dumps(STATUS).encode()
        raise OSError("no route")

    found = discover(gateway="192.168.0.1", fetch=fetch)
    assert len(found) == 1
    assert found[0].kind == "zlt"
    assert found[0].label == "ZLT X17U"


def test_monthly_data_is_read_as_mebibytes_not_megabytes() -> None:
    """The firmware says "MB" and means MiB.

    Established against the router itself: at one moment cmd 133 reported
    5,845,246,789 raw bytes received while cmd 205 reported flow_dl of 5,574.46 "MB".
    The ratio is 1,048,576 — two to the twentieth, not ten to the sixth. Reading it as
    decimal under-reports every data figure by 4.86%, and the allowance meter is
    exactly where a 5% error goes unnoticed until the plan runs out early.
    """
    reading = ZltAdapter("mtn", fetch=responder()).read()
    # RF fixture reports mon_total_flow = "18988.77"
    assert reading.metrics["data.month_total_bytes"] == pytest.approx(18988.77 * 1024 * 1024)
    assert reading.metrics["data.month_total_bytes"] > 18988.77 * 1e6  # not decimal
