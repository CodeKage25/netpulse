"""Starlink over gRPC-Web: the framing, the protobuf reader, and the traps."""

from __future__ import annotations

import struct

import pytest

from netpulse.adapters import AdapterError
from netpulse.adapters.starlink import GET_STATUS, StarlinkAdapter, _frames, fields


def frame(payload: bytes, trailer: str = "grpc-status:0\r\n") -> bytes:
    """A gRPC-Web body: one data frame, then a trailer frame with the high flag bit."""
    body = b"\x00" + struct.pack(">I", len(payload)) + payload
    tail = trailer.encode()
    return body + b"\x80" + struct.pack(">I", len(tail)) + tail


def varint(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        out.append(byte | (0x80 if value else 0))
        if not value:
            return bytes(out)


def field(number: int, wire: int, payload: bytes) -> bytes:
    return varint((number << 3) | wire) + payload


def nested(number: int, inner: bytes) -> bytes:
    """A length-delimited sub-message, with the length computed rather than guessed —
    a hand-written prefix that is wrong makes the parser overrun into the next field
    and silently corrupt everything after it."""
    return field(number, 2, varint(len(inner)) + inner)


def f32(value: float) -> bytes:
    return struct.pack("<f", value)


# ------------------------------------------------------------------ wire format


def test_the_request_frame_is_shaped_the_way_grpc_web_expects() -> None:
    """One flag byte, a four-byte big-endian length, then the payload."""
    assert GET_STATUS[0] == 0x00  # not a trailer
    assert struct.unpack(">I", GET_STATUS[1:5])[0] == len(GET_STATUS) - 5


def test_data_and_trailer_frames_are_separated() -> None:
    message, trailer = _frames(frame(b"hello", "grpc-status:0\r\n"))
    assert message == b"hello"
    assert "grpc-status:0" in trailer


def test_an_unknown_protobuf_field_is_skipped_not_fatal() -> None:
    """Skipping what you do not recognise is what lets a handful of readings come out
    of a large message with no schema at all."""
    buffer = field(1, 5, f32(1.5)) + field(999, 2, b"\x03abc") + field(2, 0, varint(42))
    found = fields(buffer)
    assert found[1] == [pytest.approx(1.5)]
    assert found[2] == [42]


def test_a_large_field_number_decodes() -> None:
    """Starlink's status fields are numbered above 1000, which needs a multi-byte tag."""
    found = fields(field(1009, 5, f32(38.5)))
    assert found[1009] == [pytest.approx(38.5)]


# ------------------------------------------------------------------ readings


def status_payload(**overrides: bytes) -> bytes:
    parts = [
        field(1007, 5, f32(80_000_000.0)),  # downlink bits/s
        field(1008, 5, f32(8_000_000.0)),  # uplink bits/s
        field(1009, 5, f32(38.5)),  # pop ping latency ms
        field(1003, 5, f32(0.02)),  # pop ping drop rate
        nested(1005, field(1, 5, f32(90000.0))),  # device_state.uptime_s
        nested(1004, field(1, 5, f32(0.014))),  # obstruction_stats.fraction_obstructed
    ]
    return b"".join(parts) + b"".join(overrides.values())


def adapter_for(payload: bytes, trailer: str = "grpc-status:0\r\n") -> StarlinkAdapter:
    return StarlinkAdapter("dish", fetch=lambda host, port, body: frame(payload, trailer))


def test_a_dish_reading_carries_throughput_latency_and_obstruction() -> None:
    reading = adapter_for(status_payload()).read()
    # The dish reports bits per second; NetPulse stores bytes, like every other adapter.
    assert reading.metrics["traffic.down_bytes_s"] == pytest.approx(10_000_000.0)
    assert reading.metrics["traffic.up_bytes_s"] == pytest.approx(1_000_000.0)
    assert reading.metrics["latency.internet_ms"] == pytest.approx(38.5)
    assert reading.metrics["loss.pct"] == pytest.approx(2.0)
    assert reading.metrics["sky.obstructed_pct"] == pytest.approx(1.4)
    assert reading.metrics["up"] == 1.0


def test_an_absent_outage_message_is_what_connected_looks_like() -> None:
    """There is no connection-state field on the wire. Reading it the other way round
    would report every working dish as permanently down."""
    assert adapter_for(status_payload()).read().metrics["up"] == 1.0


def test_an_outage_message_present_means_down() -> None:
    outage = nested(1014, field(1, 0, varint(3)))
    reading = adapter_for(status_payload() + outage).read()
    assert reading.metrics["up"] == 0.0


def test_a_grpc_error_is_a_failed_poll_even_though_http_said_200() -> None:
    """The status line proves nothing; only the trailer knows."""
    with pytest.raises(AdapterError, match="grpc-status 7"):
        adapter_for(status_payload(), "grpc-status:7\r\ngrpc-message:permission\r\n").read()


def test_an_empty_reply_is_reported_as_such() -> None:
    """The dish answers an unrecognised Referer with an empty 200, which reads as a
    parser bug unless it is named."""
    adapter = StarlinkAdapter("dish", fetch=lambda host, port, body: frame(b""))
    with pytest.raises(AdapterError, match="returned nothing"):
        adapter.read()


def test_an_unreachable_dish_explains_the_usual_cause() -> None:
    """Behind a third-party router the dish needs a static route, and a bare timeout
    sends people looking for a network fault instead of a routing one."""

    def refuse(host: str, port: int, body: bytes) -> bytes:
        raise OSError("connection refused")

    with pytest.raises(AdapterError, match="static route"):
        StarlinkAdapter("dish", fetch=refuse).read()


def test_it_talks_to_the_grpc_web_port_not_the_grpc_one() -> None:
    """9200 is HTTP/2 and out of reach of the standard library; 9201 serves the same
    service over HTTP/1.1, which is the whole reason this adapter can exist."""
    seen: list[int] = []

    def fetch(host: str, port: int, body: bytes) -> bytes:
        seen.append(port)
        return frame(status_payload())

    StarlinkAdapter("dish", fetch=fetch).read()
    assert seen[0] == 9201


def test_device_info_is_refreshed_rarely() -> None:
    """Hardware and firmware do not change minute to minute, and a fragile box should
    not be asked sixty times an hour for an answer that never moves."""
    calls: list[bytes] = []

    def fetch(host: str, port: int, body: bytes) -> bytes:
        calls.append(body)
        return frame(status_payload())

    adapter = StarlinkAdapter("dish", fetch=fetch)
    for _ in range(10):
        adapter.read()
    info_calls = [body for body in calls if body != GET_STATUS]
    assert len(info_calls) == 1
