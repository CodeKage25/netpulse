"""Netgear cellular: the whole radio for one unauthenticated request."""

from __future__ import annotations

import json

import pytest

from netpulse.sources import AdapterError
from netpulse.sources.netgear import NetgearAdapter

#: Shaped from a real M1 (MR1100) model.json, trimmed to the fields read.
MODEL = {
    "general": {
        "companyName": "NETGEAR",
        "deviceName": "Nighthawk M1",
        "FWversion": "NTG9X50C_12.06.12.00",
        "upTime": 43201,
    },
    "wwan": {
        "connection": "Connected",
        "currentNWserviceType": "LTE",
        "registerNetworkDisplay": "MTN-NG",
        "signalStrength": {
            "rssi": -72,
            "rscp": 0,
            "ecio": 0,
            "rsrp": -108,
            "rsrq": -17,
            "bars": 3,
            "sinr": -6,
        },
        "diagInfo": [
            {
                "lteAttached": True,
                "nr5gAttached": True,
                "ltesigRsrp": "-107 dBm",
                "ltesigRsrq": "-17 dB",
                "nr5gsigRsrp": "-117 dBm",
                "nr5gsigRsrq": "-15 dB",
                "nr5gsigSnr": "0 dB",
            }
        ],
    },
    "wwanadv": {
        "curBand": "LTE B3",
        "earfcn": 1650,
        "bandwidth": "20 MHz",
        "pci": 358,
        "cellId": 20915457,
        "radioQuality": 48,
    },
}


def serving(payload: object):  # type: ignore[no-untyped-def]
    body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    return lambda url: body


def test_the_radio_arrives_without_a_login() -> None:
    """This is the whole point of the adapter: most vendors gate the signal, Netgear
    gates only the byte counters."""
    reading = NetgearAdapter("m1", fetch=serving(MODEL)).read()
    assert reading.metrics["signal.rsrp_dbm"] == -108.0
    assert reading.metrics["signal.sinr_db"] == -6.0
    assert reading.metrics["signal.bars"] == 3.0
    assert reading.metrics["up"] == 1.0
    assert reading.texts["net.operator"] == "MTN-NG"


def test_unit_bearing_strings_and_plain_numbers_both_parse() -> None:
    """`signalStrength.rsrp` is an integer while `diagInfo.nr5gsigRsrp` is "-117 dBm",
    in the same document."""
    reading = NetgearAdapter("m1", fetch=serving(MODEL)).read()
    assert reading.metrics["signal.rsrp_5g_dbm"] == -117.0
    assert reading.metrics["signal.sinr_5g_db"] == 0.0


def test_the_band_becomes_a_placed_carrier() -> None:
    """ "LTE B3" plus EARFCN 1650 is 1850 MHz, the same as every other adapter reports."""
    reading = NetgearAdapter("m1", fetch=serving(MODEL)).read()
    assert reading.metrics["radio.cc0.mhz"] == 1850.0
    assert reading.metrics["radio.cc0.band"] == 3.0
    assert reading.metrics["radio.aggregate_mhz"] == 20.0


def test_a_login_page_served_with_a_200_is_not_a_reading() -> None:
    """Some firmware answers an unauthenticated request with HTML under a 200. Trusting
    the status code would turn "please sign in" into "the router is broken"."""
    adapter = NetgearAdapter("m1", fetch=serving(b"<html><body>Sign in</body></html>"))
    with pytest.raises(AdapterError, match="wants a login"):
        adapter.read()


def test_anything_but_connected_is_not_up() -> None:
    """Including a firmware that omits the field: absence is not a claim of health."""
    for state in ("Disconnected", "", "Connecting"):
        payload = {**MODEL, "wwan": {**MODEL["wwan"], "connection": state}}
        assert NetgearAdapter("m1", fetch=serving(payload)).read().metrics["up"] == 0.0


def test_byte_counters_are_absent_rather_than_zero_without_a_login() -> None:
    reading = NetgearAdapter("m1", fetch=serving(MODEL)).read()
    assert "data.month_down_bytes" not in reading.metrics


def test_a_device_that_is_not_a_netgear_modem_is_refused() -> None:
    """Valid JSON carrying no radio is somebody else's router, not a working reading."""
    adapter = NetgearAdapter("m1", fetch=serving({"hello": "world"}))
    with pytest.raises(AdapterError, match="no radio"):
        adapter.read()


def test_the_lb_line_lives_on_a_different_address() -> None:
    """LB1120/LB2120/LM1200 answer on 192.168.5.1 — a detail that turns "unsupported"
    into "wrong address"."""
    from netpulse.sources.vendors import VENDORS

    netgear = next(
        v for v in VENDORS if v.name == "Netgear" and "model.json" in v.signatures[0].path
    )
    assert "192.168.5.1" in netgear.addresses
