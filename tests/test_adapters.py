"""Adapters against recorded fixtures — no router, no network, ever."""

from __future__ import annotations

import json

import pytest

from netpulse.sources import AdapterError, build
from netpulse.sources.fake import DemoAdapter
from netpulse.sources.huawei import HuaweiAdapter
from netpulse.sources.probe import ProbeAdapter
from netpulse.sources.zte import ZteAdapter

# ------------------------------------------------------------------ huawei fixtures

SESSION = b"<response><SesInfo>SessionID=abc123</SesInfo><TokInfo>tok456</TokInfo></response>"
STATUS = (
    b"<response><ConnectionStatus>901</ConnectionStatus>"
    b"<CurrentNetworkType>19</CurrentNetworkType><SignalIcon>4</SignalIcon></response>"
)
SIGNAL = (
    b"<response><rsrp>-97dBm</rsrp><rsrq>-9.0dB</rsrq><sinr>13dB</sinr>"
    b"<rssi>-71dBm</rssi><band>3</band><cell_id>12345678</cell_id></response>"
)
TRAFFIC = (
    b"<response><CurrentDownloadRate>2048000</CurrentDownloadRate>"
    b"<CurrentUploadRate>512000</CurrentUploadRate>"
    b"<TotalDownload>987654321</TotalDownload><TotalUpload>123456789</TotalUpload></response>"
)
MONTH = (
    b"<response><CurrentMonthDownload>9500000000</CurrentMonthDownload>"
    b"<CurrentMonthUpload>1200000000</CurrentMonthUpload></response>"
)
PLMN = b"<response><FullName>MTN Nigeria</FullName><ShortName>MTN</ShortName></response>"
API_ERROR = b"<error><code>125003</code><message></message></error>"

HUAWEI_ROUTES = {
    "/api/webserver/SesTokInfo": SESSION,
    "/api/monitoring/status": STATUS,
    "/api/device/signal": SIGNAL,
    "/api/monitoring/traffic-statistics": TRAFFIC,
    "/api/monitoring/month_statistics": MONTH,
    "/api/net/current-plmn": PLMN,
}


def huawei_fetch(routes: dict[str, bytes]):  # type: ignore[no-untyped-def]
    calls: list[str] = []

    def fetch(url: str, headers: dict[str, str], data: bytes | None):  # type: ignore[no-untyped-def]
        path = url.split("192.168.8.1")[1]
        calls.append(path)
        if path not in routes:
            raise OSError("connection refused")
        return routes[path], {}

    fetch.calls = calls  # type: ignore[attr-defined]
    return fetch


def test_huawei_reads_a_full_sweep_from_fixtures() -> None:
    adapter = HuaweiAdapter("mtn", fetch=huawei_fetch(HUAWEI_ROUTES))
    reading = adapter.read()

    assert reading.metrics["up"] == 1.0
    assert reading.metrics["signal.rsrp_dbm"] == -97
    assert reading.metrics["signal.sinr_db"] == 13
    assert reading.metrics["traffic.down_bytes_s"] == 2_048_000
    assert reading.metrics["data.month_down_bytes"] == 9_500_000_000
    assert reading.texts["net.type"] == "LTE"
    assert reading.texts["net.operator"] == "MTN Nigeria"
    assert reading.texts["signal.band"] == "B3"


def test_huawei_units_are_stripped_from_values() -> None:
    adapter = HuaweiAdapter("mtn", fetch=huawei_fetch(HUAWEI_ROUTES))
    reading = adapter.read()
    assert isinstance(reading.metrics["signal.rsrq_db"], float)
    assert reading.metrics["signal.rsrq_db"] == -9.0


def test_huawei_survives_missing_optional_endpoints() -> None:
    routes = {k: v for k, v in HUAWEI_ROUTES.items() if "month" not in k and "plmn" not in k}
    adapter = HuaweiAdapter("mtn", fetch=huawei_fetch(routes))
    reading = adapter.read()
    assert "data.month_down_bytes" not in reading.metrics
    assert reading.metrics["up"] == 1.0


def test_huawei_half_formed_xml_is_a_failed_poll_not_a_crash() -> None:
    routes = dict(HUAWEI_ROUTES)
    routes["/api/monitoring/status"] = b"<response><Conn"  # router mid-reboot
    # A stale-session retry refetches the same garbage; both attempts must fail cleanly.
    with pytest.raises(AdapterError):
        HuaweiAdapter("mtn", fetch=huawei_fetch(routes)).read()


def test_huawei_refreshes_a_stale_session_once() -> None:
    served = {"count": 0}
    fresh = dict(HUAWEI_ROUTES)

    def fetch(url: str, headers: dict[str, str], data: bytes | None):  # type: ignore[no-untyped-def]
        path = url.split("192.168.8.1")[1]
        if path == "/api/monitoring/status" and served["count"] == 0:
            served["count"] += 1
            return API_ERROR, {}
        if path not in fresh:
            raise OSError("connection refused")
        return fresh[path], {}

    reading = HuaweiAdapter("mtn", fetch=fetch).read()
    assert reading.metrics["up"] == 1.0


def test_huawei_disconnected_reports_down_with_signal_still_read() -> None:
    routes = dict(HUAWEI_ROUTES)
    routes["/api/monitoring/status"] = STATUS.replace(b"901", b"902")
    reading = HuaweiAdapter("mtn", fetch=huawei_fetch(routes)).read()
    assert reading.metrics["up"] == 0.0
    assert reading.metrics["signal.rsrp_dbm"] == -97  # signal still useful while offline


def test_huawei_login_hashes_and_posts(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    posted: dict[str, bytes] = {}

    def fetch(url: str, headers: dict[str, str], data: bytes | None):  # type: ignore[no-untyped-def]
        path = url.split("192.168.8.1")[1]
        if data is not None:
            posted[path] = data
            return b"<response>OK</response>", {"__RequestVerificationToken": "tok789"}
        return HUAWEI_ROUTES[path], {}

    adapter = HuaweiAdapter("mtn", username="admin", password="pass", fetch=fetch)
    adapter.login()
    body = posted["/api/user/login"].decode()
    assert "<password_type>4</password_type>" in body
    assert ">pass<" not in body  # the raw password never crosses the wire


def test_huawei_sms_list_parses_messages() -> None:
    sms = (
        b"<response><Count>1</Count><Messages><Message>"
        b"<Phone>MTN</Phone><Date>2026-03-01 09:00:00</Date>"
        b"<Content>Dear customer, you have 4.2GB left, valid until 15/03.</Content>"
        b"</Message></Messages></response>"
    )

    def fetch(url: str, headers: dict[str, str], data: bytes | None):  # type: ignore[no-untyped-def]
        path = url.split("192.168.8.1")[1]
        if path == "/api/sms/sms-list":
            return sms, {}
        if data is not None:
            return b"<response>OK</response>", {}
        if path not in HUAWEI_ROUTES:
            raise OSError("connection refused")
        return HUAWEI_ROUTES[path], {}

    adapter = HuaweiAdapter("mtn", username="admin", password="pass", fetch=fetch)
    messages = adapter.sms_list()
    assert messages[0]["from"] == "MTN"
    assert "4.2GB" in messages[0]["text"]


# ------------------------------------------------------------------ zte fixtures

ZTE_OK = {
    "network_type": "LTE",
    "signalbar": "4",
    "lte_rsrp": "-95",
    "lte_rsrq": "-8",
    "lte_snr": "12",
    "lte_rssi": "-70",
    "lte_band": "3",
    "cell_id": "A1B2C3",
    "network_provider": "Airtel NG",
    "ppp_status": "ppp_connected",
    "realtime_rx_thrpt": "1500000",
    "realtime_tx_thrpt": "300000",
    "monthly_rx_bytes": "7000000000",
    "monthly_tx_bytes": "900000000",
}


def test_zte_reads_one_request_for_the_whole_sweep() -> None:
    calls: list[str] = []

    def fetch(url: str, headers: dict[str, str]) -> bytes:
        calls.append(url)
        assert "Referer" in headers  # the firmware's guard
        return json.dumps(ZTE_OK).encode()

    reading = ZteAdapter("airtel", fetch=fetch).read()
    assert len(calls) == 1
    assert reading.metrics["up"] == 1.0
    assert reading.metrics["signal.rsrp_dbm"] == -95
    assert reading.texts["net.operator"] == "Airtel NG"


def test_zte_empty_reply_is_a_failed_poll() -> None:
    with pytest.raises(AdapterError, match="did not answer a known ZTE API"):
        ZteAdapter("airtel", fetch=lambda url, headers: b"").read()


# ------------------------------------------------------------------ probe


def test_probe_measures_and_separates_gateway_from_internet() -> None:
    adapter = ProbeAdapter(
        "wan",
        gateway="192.168.8.1",
        tcp=lambda host, port: 80.0,
        dns=lambda resolver: 25.0,
        gateway_probe=lambda gateway: 3.0,
    )
    reading = adapter.read()
    assert reading.metrics["latency.internet_ms"] == 80.0
    assert reading.metrics["latency.gateway_ms"] == 3.0
    assert reading.metrics["dns.lookup_ms"] == 25.0
    assert reading.metrics["loss.pct"] == 0.0
    assert reading.texts["net.gateway"] == "192.168.8.1"


def test_probe_counts_failures_as_loss() -> None:
    attempts = {"n": 0}

    def flaky(host: str, port: int) -> float:
        attempts["n"] += 1
        if attempts["n"] % 2 == 0:
            raise OSError("timed out")
        return 60.0

    adapter = ProbeAdapter(
        "wan",
        gateway="192.168.8.1",
        tcp=flaky,
        dns=lambda resolver: 20.0,
        gateway_probe=lambda gateway: 2.0,
    )
    assert adapter.read().metrics["loss.pct"] == 50.0


def test_probe_with_nothing_answering_is_a_failed_poll() -> None:
    def down(*args: object) -> float:
        raise OSError("no route to host")

    adapter = ProbeAdapter(
        "wan",
        gateway="192.168.8.1",
        tcp=down,
        dns=down,
        gateway_probe=down,  # type: ignore[arg-type]
    )
    with pytest.raises(AdapterError):
        adapter.read()


def test_probe_keeps_internet_readings_when_only_the_gateway_hides() -> None:
    def no_gateway(gateway: str) -> float:
        raise AdapterError("gateway silent")

    adapter = ProbeAdapter(
        "wan",
        gateway="192.168.8.1",
        tcp=lambda host, port: 70.0,
        dns=lambda r: 20.0,
        gateway_probe=no_gateway,
    )
    reading = adapter.read()
    assert reading.metrics["latency.internet_ms"] == 70.0
    assert "latency.gateway_ms" not in reading.metrics


# ------------------------------------------------------------------ registry & demo


def test_the_registry_builds_every_kind() -> None:
    assert build("demo", "d", {}).kind == "demo"
    assert build("probe", "p", {"gateway": "10.0.0.1"}).kind == "probe"
    assert build("huawei", "h", {"url": "http://192.168.8.1"}).kind == "huawei"
    assert build("zte", "z", {}).kind == "zte"
    with pytest.raises(ValueError, match="available"):
        build("cisco", "c", {})


def test_demo_produces_a_full_lte_shaped_reading_and_an_outage() -> None:
    adapter = DemoAdapter("demo")
    reading = adapter.read()
    assert "signal.sinr_db" in reading.metrics
    assert reading.texts["net.type"] == "LTE"

    failures = 0
    for _ in range(400):
        try:
            adapter.read()
        except AdapterError:
            failures += 1
    assert failures > 0  # the scripted outage happened
