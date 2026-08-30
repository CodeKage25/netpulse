"""Nokia FastMile: the per-carrier radio, and what the endpoint may not be asked.

The payloads here follow the shape a working FastMile client reads — `cell_LTE_stats_cfg`
and `cell_5G_stats_cfg` arrays of `{"stat": {...}}`, plus `cellular_stats` counters.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from netpulse.sources import AdapterError
from netpulse.sources.fastmile import FastMileAdapter

RADIO_PATH = "/fastmile_radio_status_web_app.cgi"


def cell(**fields: Any) -> dict[str, Any]:
    return {"stat": fields}


def payload(**over: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "cellular_stats": [{"BytesReceived": 1_000_000, "BytesSent": 200_000}],
        "cell_LTE_stats_cfg": [
            cell(
                PhysicalCellID=157,
                RSRPCurrent=-92,
                RSRQCurrent=-11,
                SNRCurrent=14,
                RSSICurrent=-63,
                RSRPStrengthIndexCurrent=4,
                DownlinkEarfcn=1300,
                Band=3,
            ),
            cell(PhysicalCellID=157, RSRPCurrent=-98, SNRCurrent=9, DownlinkEarfcn=3050, Band=7),
        ],
        "cell_5G_stats_cfg": [],
    }
    body.update(over)
    return body


def adapter(*bodies: dict[str, Any], clock: list[float] | None = None) -> FastMileAdapter:
    """An adapter over a scripted sequence of replies, one per read()."""
    replies = list(bodies)
    ticks = list(clock or [0.0, 10.0, 20.0, 30.0])

    def fetch(url: str) -> bytes:
        assert RADIO_PATH in url or "overview" in url
        return json.dumps(replies.pop(0) if len(replies) > 1 else replies[0]).encode()

    return FastMileAdapter("nokia", fetch=fetch, clock=lambda: ticks.pop(0))


def test_the_serving_cell_supplies_the_signal_figures() -> None:
    reading = adapter(payload()).read()
    assert reading.metrics["signal.rsrp_dbm"] == -92
    assert reading.metrics["signal.sinr_db"] == 14
    assert reading.texts["signal.band"] == "B3"
    assert reading.texts["signal.cell_id"] == "157"


def test_an_aggregated_carrier_never_averages_into_the_serving_figure() -> None:
    """Two carriers at -92 and -98 do not make a link at -95. No radio measured that,
    and the number would move whenever aggregation changed while the signal did not."""
    reading = adapter(payload()).read()
    assert reading.metrics["signal.rsrp_dbm"] == -92
    assert reading.metrics["radio.carriers"] == 2


def test_carriers_land_where_the_3gpp_arithmetic_puts_them() -> None:
    """Band 3 EARFCN 1300 is 1815.0 MHz exactly: 1805 + 0.1 x (1300 - 1200)."""
    metrics = adapter(payload()).read().metrics
    assert metrics["radio.cc0.mhz"] == pytest.approx(1815.0)
    assert metrics["radio.cc0.band"] == 3
    assert metrics["radio.cc0.nr"] == 0.0


def test_a_five_g_carrier_is_placed_and_labelled_as_nr() -> None:
    body = payload(
        cell_5G_stats_cfg=[
            cell(RSRPCurrent=-85, SNRCurrent=18, Downlink_NR_ARFCN=632628, Band=78)
        ]
    )
    reading = adapter(body).read()
    assert reading.metrics["signal.rsrp_5g_dbm"] == -85
    assert reading.texts["net.type"] == "5G"
    # 3000 + 0.015 x (632628 - 600000)
    assert reading.metrics["radio.cc2.mhz"] == pytest.approx(3489.42)
    assert reading.metrics["radio.cc2.nr"] == 1.0


def test_an_idle_carrier_slot_is_not_counted_as_aggregation() -> None:
    """Firmware pads the array with unattached slots reporting -1000 dBm. Counting
    those would draw carriers the radio is not using."""
    body = payload(
        cell_LTE_stats_cfg=[
            cell(RSRPCurrent=-92, DownlinkEarfcn=1300, Band=3),
            cell(RSRPCurrent=-1000, DownlinkEarfcn=0, Band=0),
        ]
    )
    assert adapter(body).read().metrics["radio.carriers"] == 1


def test_a_radio_attached_to_nothing_reads_down() -> None:
    """The one down signal this endpoint can honestly give."""
    body = payload(cell_LTE_stats_cfg=[cell(RSRPCurrent=-1000, Band=0)], cell_5G_stats_cfg=[])
    reading = adapter(body).read()
    assert reading.metrics["up"] == 0.0
    assert "signal.rsrp_dbm" not in reading.metrics


def test_an_attached_radio_makes_no_claim_about_the_internet() -> None:
    """An attached cell is not a working connection, and this adapter must not say it
    is. Reachability is the probe source's measurement, not this one's inference."""
    reading = adapter(payload()).read()
    assert "up" not in reading.metrics


def test_throughput_needs_two_readings_before_it_says_anything() -> None:
    later = payload(cellular_stats=[{"BytesReceived": 1_500_000, "BytesSent": 300_000}])
    box = adapter(payload(), later)
    assert "traffic.down_bytes_s" not in box.read().metrics
    metrics = box.read().metrics
    assert metrics["traffic.down_bytes_s"] == pytest.approx(50_000.0)  # 500 kB / 10 s
    assert metrics["traffic.up_bytes_s"] == pytest.approx(10_000.0)


def test_a_reboot_publishes_no_throughput_rather_than_a_burst() -> None:
    """Counters restarting from zero would otherwise difference into a negative, or —
    with a wrap correction applied blindly — a four-gigabyte second that never happened."""
    restarted = payload(cellular_stats=[{"BytesReceived": 12_000, "BytesSent": 4_000}])
    box = adapter(payload(), restarted)
    box.read()
    assert "traffic.down_bytes_s" not in box.read().metrics


def test_a_login_page_is_reported_as_a_login_not_a_broken_router() -> None:
    def fetch(url: str) -> bytes:
        return b"<html><body>Please sign in</body></html>"

    with pytest.raises(AdapterError):
        FastMileAdapter("nokia", fetch=fetch).read()


def test_a_reply_without_radio_arrays_is_refused() -> None:
    """Some other device answering on 192.168.1.1 must not become a Nokia source."""
    def fetch(url: str) -> bytes:
        return json.dumps({"status": "ok"}).encode()

    with pytest.raises(AdapterError, match="wrong device"):
        FastMileAdapter("nokia", fetch=fetch).read()


def test_the_working_endpoint_is_remembered() -> None:
    """Two paths exist across firmware. Retrying the dead one every sweep is exactly
    the sort of unnecessary request a fragile CPE box is worst at absorbing."""
    calls: list[str] = []

    def fetch(url: str) -> bytes:
        calls.append(url)
        if url.endswith("/overview_get_web_app.cgi"):
            return json.dumps(payload()).encode()
        raise OSError("404")

    box = FastMileAdapter("nokia", fetch=fetch, clock=lambda: 0.0)
    box.read()
    box.read()
    assert sum(RADIO_PATH in call for call in calls) == 1
