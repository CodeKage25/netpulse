"""SNMPv2c in the standard library, and the two vendors whose OIDs are verified.

The wire format is tested by round-tripping through the real encoder and decoder, and
by decoding bytes captured from the RFC's own definitions — not by asserting that the
code agrees with itself.
"""

from __future__ import annotations

import pytest

from netpulse.adapters import AdapterError
from netpulse.adapters.snmp_router import SnmpAdapter
from netpulse.snmp import (
    COUNTER64,
    GAUGE32,
    INTEGER,
    OCTET_STRING,
    SnmpError,
    _message,
    _parse,
    _read_tlv,
    decode_oid,
    decode_value,
    encode_int,
    encode_oid,
)

# ------------------------------------------------------------------ BER


def test_the_universal_oid_prefix_encodes_to_the_known_bytes() -> None:
    """1.3.6.1.2.1.1.1.0 is sysDescr, and its encoding is fixed by X.690: the first two
    arcs collapse into 0x2b."""
    assert encode_oid("1.3.6.1.2.1.1.1.0").hex() == "06082b06010201010100"


def test_multi_byte_arcs_survive_a_round_trip() -> None:
    """MikroTik's enterprise number is 14988, well past the 127 a single byte holds."""
    for oid in (
        "1.3.6.1.4.1.14988.1.1.16.1.1.4",
        "1.3.6.1.4.1.48690.2.2.1.20",
        "1.3.6.1.2.1.1.3.0",
    ):
        assert decode_oid(encode_oid(oid)[2:]) == oid


@pytest.mark.parametrize("value", [-140, -95, -20, -1, 0, 1, 127, 128, 255, 2**31 - 1])
def test_integers_round_trip_including_negative_ones(value: int) -> None:
    """RSRP, RSRQ and SINR are all negative. An unsigned decode turns -95 dBm into 161,
    which is a plausible-looking number and therefore the worst kind of wrong — this is
    the single most common bug in hand-rolled SNMP."""
    tag, body, _ = _read_tlv(encode_int(value), 0)
    assert decode_value(tag, body) == value


def test_counters_and_gauges_are_unsigned() -> None:
    """A counter near its ceiling has the top bit set, and reading it signed reports a
    large negative throughput."""
    assert decode_value(GAUGE32, (2**32 - 1).to_bytes(4, "big")) == 4294967295
    assert decode_value(COUNTER64, (2**63).to_bytes(8, "big")) == 2**63


def test_long_form_lengths_encode_and_decode() -> None:
    """A GETBULK reply passes 127 bytes immediately, so short-form-only breaks at once."""
    long_oids = [f"1.3.6.1.2.1.1.{n}.0" for n in range(1, 20)]
    message = _message("public", 0xA1, 1, long_oids)
    assert message[1] & 0x80, "a message this size needs a long-form length"
    tag, body, _ = _read_tlv(message, 0)
    assert tag == 0x30
    assert len(body) > 127


def test_the_exception_tags_decode_to_absent_not_zero() -> None:
    """noSuchObject means the router does not have it; zero would be a measurement."""
    for tag in (0x80, 0x81, 0x82):
        assert decode_value(tag, b"") is None


# ------------------------------------------------------------------ messages


def reply_for(request: bytes, values: dict[str, tuple[int, bytes]]) -> bytes:
    """Build a Response PDU echoing the request's id — what a real agent does."""
    from netpulse.snmp import RESPONSE, SEQUENCE, _tlv

    _, body, _ = _read_tlv(request, 0)
    _, _, at = _read_tlv(body, 0)
    _, _, at = _read_tlv(body, at)
    _, pdu, _ = _read_tlv(body, at)
    _, id_body, _ = _read_tlv(pdu, 0)
    request_id = int.from_bytes(id_body, "big", signed=True)

    binds = b"".join(
        _tlv(SEQUENCE, encode_oid(oid) + _tlv(tag, value)) for oid, (tag, value) in values.items()
    )
    return _tlv(
        SEQUENCE,
        encode_int(1)
        + _tlv(OCTET_STRING, b"public")
        + _tlv(
            RESPONSE,
            encode_int(request_id) + encode_int(0) + encode_int(0) + _tlv(SEQUENCE, binds),
        ),
    )


def test_a_response_parses_back_into_varbinds() -> None:
    request = _message("public", 0xA0, 4242, ["1.3.6.1.2.1.1.1.0"])
    reply = reply_for(request, {"1.3.6.1.2.1.1.1.0": (OCTET_STRING, b"RouterOS RB760iGS")})
    bound = _parse(reply, 4242)
    assert bound[0].oid == "1.3.6.1.2.1.1.1.0"
    assert bound[0].value == "RouterOS RB760iGS"


def test_a_reply_carrying_another_requests_id_is_refused() -> None:
    """UDP delivers stale replies from an attempt that already timed out. Accepting the
    first datagram that arrives silently mixes up two readings."""
    request = _message("public", 0xA0, 1111, ["1.3.6.1.2.1.1.1.0"])
    reply = reply_for(request, {"1.3.6.1.2.1.1.1.0": (INTEGER, b"\x01")})
    with pytest.raises(SnmpError, match="another request's id"):
        _parse(reply, 2222)


def test_a_truncated_datagram_raises_rather_than_indexing_off_the_end() -> None:
    with pytest.raises(SnmpError):
        _parse(b"\x30\x82\xff\xff\x02", 1)


def test_getbulk_reuses_the_error_fields_for_its_own_counts() -> None:
    """RFC 3416: BulkPDU is structurally identical to PDU — positions two and three
    carry non-repeaters and max-repetitions instead of error-status and error-index."""
    message = _message("public", 0xA5, 7, ["1.3.6.1.2.1.1"], field_two=0, field_three=10)
    _, body, _ = _read_tlv(message, 0)
    _, _, at = _read_tlv(body, 0)
    _, _, at = _read_tlv(body, at)
    tag, pdu, _ = _read_tlv(body, at)
    assert tag == 0xA5
    _, _, at = _read_tlv(pdu, 0)
    _, non_repeaters, at = _read_tlv(pdu, at)
    _, max_reps, _ = _read_tlv(pdu, at)
    assert int.from_bytes(non_repeaters, "big") == 0
    assert int.from_bytes(max_reps, "big") == 10


# ------------------------------------------------------------------ the adapter


class FakeAgent:
    """Answers OIDs from a dict, the way `get` and `walk` call into the module."""

    def __init__(self, values: dict[str, object]) -> None:
        self.values = values
        self.asked: list[str] = []

    def get(self, host, community, oids, timeout=2.0, port=161):  # type: ignore[no-untyped-def]
        self.asked += oids
        return {oid: self.values[oid] for oid in oids if oid in self.values}

    def walk(self, host, community, root, timeout=2.0, port=161, max_rows=200):  # type: ignore[no-untyped-def]
        self.asked.append(root)
        return {oid: value for oid, value in self.values.items() if oid.startswith(root + ".")}


def wire(monkeypatch: pytest.MonkeyPatch, agent: FakeAgent) -> None:
    import netpulse.adapters.snmp_router as module

    monkeypatch.setattr(module, "get", agent.get)
    monkeypatch.setattr(module, "walk", agent.walk)
    monkeypatch.setattr(module, "identify", lambda h, c, t: agent.values.get("1.3.6.1.2.1.1.1.0"))


MIKROTIK_AGENT = {
    "1.3.6.1.2.1.1.1.0": "RouterOS RB5009UPr+S+",
    "1.3.6.1.2.1.1.3.0": 123456,  # centiseconds
    "1.3.6.1.2.1.1.5.0": "MikroTik",
    "1.3.6.1.4.1.14988.1.1.16.1.1.4.1": -95,  # RSRP, indexed row
    "1.3.6.1.4.1.14988.1.1.16.1.1.7.1": 12,  # SINR
    "1.3.6.1.4.1.14988.1.1.16.1.1.3.1": -11,  # RSRQ
    "1.3.6.1.4.1.14988.1.1.16.1.1.2.1": -70,  # RSSI
}


def test_a_mikrotik_reading_carries_its_radio(monkeypatch: pytest.MonkeyPatch) -> None:
    agent = FakeAgent(MIKROTIK_AGENT)
    wire(monkeypatch, agent)
    reading = SnmpAdapter("mt", host="192.168.88.1").read()
    assert reading.metrics["signal.rsrp_dbm"] == -95.0
    assert reading.metrics["signal.sinr_db"] == 12.0
    assert reading.metrics["router.uptime_s"] == 1234.56  # TimeTicks are centiseconds
    assert reading.texts["router.vendor"] == "MikroTik"


def test_teltonika_signal_values_arrive_as_strings(monkeypatch: pytest.MonkeyPatch) -> None:
    """RutOS reports RSRP, RSRQ and SINR as OctetStrings, sometimes with a unit."""
    agent = FakeAgent(
        {
            "1.3.6.1.2.1.1.1.0": "Teltonika RUTX11",
            "1.3.6.1.2.1.1.3.0": 500,
            "1.3.6.1.4.1.48690.2.2.1.20.1": "-98 dBm",
            "1.3.6.1.4.1.48690.2.2.1.19.1": "8",
            "1.3.6.1.4.1.48690.2.2.1.13.1": "MTN-NG",
        }
    )
    wire(monkeypatch, agent)
    reading = SnmpAdapter("rut", host="192.168.1.1").read()
    assert reading.metrics["signal.rsrp_dbm"] == -98.0
    assert reading.metrics["signal.sinr_db"] == 8.0
    assert reading.texts["net.operator"] == "MTN-NG"


def test_an_agent_with_no_radio_table_still_reports_what_it_has(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A switch or an OpenWrt box has uptime and reachability, and those are real.
    Claiming a signal we cannot read would not be."""
    agent = FakeAgent(
        {
            "1.3.6.1.2.1.1.1.0": "Linux openwrt 5.15",
            "1.3.6.1.2.1.1.3.0": 9000,
        }
    )
    wire(monkeypatch, agent)
    reading = SnmpAdapter("wrt", host="192.168.1.1").read()
    assert reading.metrics["up"] == 1.0
    assert "signal.rsrp_dbm" not in reading.metrics
    assert "router.vendor" not in reading.texts


def test_silence_is_a_failed_poll_not_an_empty_reading(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = FakeAgent({})
    wire(monkeypatch, agent)
    with pytest.raises(AdapterError):
        SnmpAdapter("mt", host="192.168.88.1").read()


def test_the_vendor_is_detected_once_not_on_every_poll(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Poll gently: re-identifying the box every five seconds is a request that buys
    nothing after the first answer."""
    calls = []
    agent = FakeAgent(MIKROTIK_AGENT)
    import netpulse.adapters.snmp_router as module

    monkeypatch.setattr(module, "get", agent.get)
    monkeypatch.setattr(module, "walk", agent.walk)
    monkeypatch.setattr(module, "identify", lambda h, c, t: calls.append(h) or "RouterOS")
    adapter = SnmpAdapter("mt", host="192.168.88.1")
    adapter.read()
    adapter.read()
    adapter.read()
    assert len(calls) == 1
