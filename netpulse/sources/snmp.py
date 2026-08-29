"""A read-only SNMPv2c client in the standard library.

Every Python SNMP library either binds net-snmp and needs a compiler (easysnmp,
snimpy), or is a seven-thousand-line stack that has not changed functionally in two
years (puresnmp), or is an async-first rewrite carrying a pyasn1 dependency (pysnmp).
None of that earns its weight for GET, GETNEXT and GETBULK against a router on the LAN,
and NetPulse's zero-dependency promise is worth more than the convenience.

Scope is deliberately narrow: version 2c, reads only. No SET, no v3, no MIB compiler,
no traps. Encoding follows RFC 3416; the tags are listed below so the next person does
not have to take them on faith.

The corners naive implementations cut, all of which bite here:

* **Signed integers.** RSRP, RSRQ and SINR are all negative. An unsigned decode turns
  -95 dBm into 161, which is a plausible-looking number and therefore the worst kind of
  wrong. This is the single most common bug in hand-rolled SNMP.
* **Long-form lengths.** A GETBULK reply passes 127 bytes immediately.
* **Request-id matching.** UDP delivers stale replies from a timed-out earlier attempt;
  accepting the first datagram that arrives silently mixes up two readings.
* **Attacker-controlled lengths.** The read buffer is capped and never sized from a
  length field before the bytes have actually arrived.
"""

from __future__ import annotations

import random
import socket
from dataclasses import dataclass

# --- X.690 universal tags ------------------------------------------------------------
INTEGER = 0x02
OCTET_STRING = 0x04
NULL = 0x05
OID = 0x06
SEQUENCE = 0x30

# --- RFC 3416 application tags --------------------------------------------------------
IP_ADDRESS = 0x40
COUNTER32 = 0x41
GAUGE32 = 0x42  # Unsigned32 is a synonym and shares the tag
TIMETICKS = 0x43
OPAQUE = 0x44
COUNTER64 = 0x46  # note: APPLICATION 5 does not exist in v2c

# --- PDU tags -------------------------------------------------------------------------
GET = 0xA0
GET_NEXT = 0xA1
RESPONSE = 0xA2
GET_BULK = 0xA5

# --- varbind exceptions, all zero-length ----------------------------------------------
NO_SUCH_OBJECT = 0x80
NO_SUCH_INSTANCE = 0x81
END_OF_MIB_VIEW = 0x82
EXCEPTIONS = {NO_SUCH_OBJECT, NO_SUCH_INSTANCE, END_OF_MIB_VIEW}

VERSION_2C = 1
MAX_DATAGRAM = 65535
#: The conventional SNMP-over-UDP retry ladder.
BACKOFF_S = (1.0, 2.0)


class SnmpError(Exception):
    """A failed exchange. Never raised for "the object does not exist" — that is data."""


# ====================================================================== BER encoding


def _length(size: int) -> bytes:
    if size < 0x80:
        return bytes([size])
    body = size.to_bytes((size.bit_length() + 7) // 8, "big")
    return bytes([0x80 | len(body)]) + body


def _tlv(tag: int, body: bytes) -> bytes:
    return bytes([tag]) + _length(len(body)) + body


def encode_int(value: int) -> bytes:
    """Two's-complement, minimal length. Negative values are the whole point."""
    size = max(1, (value.bit_length() + 8) // 8)
    while True:
        try:
            return _tlv(INTEGER, value.to_bytes(size, "big", signed=True))
        except OverflowError:
            size += 1


def encode_oid(oid: str) -> bytes:
    arcs = [int(part) for part in oid.strip(".").split(".")]
    if len(arcs) < 2:
        raise SnmpError(f"not an OID: {oid!r}")
    # The first two arcs share one subidentifier: 1.3 becomes 0x2B.
    body = bytearray([40 * arcs[0] + arcs[1]])
    for arc in arcs[2:]:
        if arc < 128:
            body.append(arc)
            continue
        chunk = bytearray()
        while arc:
            chunk.insert(0, (arc & 0x7F) | 0x80)
            arc >>= 7
        chunk[-1] &= 0x7F  # the last byte of a multi-byte arc clears the continuation
        body += chunk
    return _tlv(OID, bytes(body))


# ====================================================================== BER decoding


def _read_length(data: bytes, at: int) -> tuple[int, int]:
    first = data[at]
    if first < 0x80:
        return first, at + 1
    count = first & 0x7F
    if count == 0 or at + 1 + count > len(data):
        raise SnmpError("malformed length")
    return int.from_bytes(data[at + 1 : at + 1 + count], "big"), at + 1 + count


def _read_tlv(data: bytes, at: int) -> tuple[int, bytes, int]:
    if at + 2 > len(data):
        raise SnmpError("truncated message")
    tag = data[at]
    size, body_at = _read_length(data, at + 1)
    end = body_at + size
    if end > len(data):
        raise SnmpError("value runs past the end of the datagram")
    return tag, data[body_at:end], end


def decode_oid(body: bytes) -> str:
    if not body:
        return ""
    arcs = [body[0] // 40, body[0] % 40]
    value = 0
    for byte in body[1:]:
        value = (value << 7) | (byte & 0x7F)
        if not byte & 0x80:
            arcs.append(value)
            value = 0
    return ".".join(str(arc) for arc in arcs)


def decode_value(tag: int, body: bytes) -> object:
    if tag in EXCEPTIONS:
        return None
    if tag == INTEGER:
        return int.from_bytes(body, "big", signed=True) if body else 0
    if tag in (COUNTER32, GAUGE32, TIMETICKS, COUNTER64):
        return int.from_bytes(body, "big")  # counters and gauges are unsigned
    if tag == OCTET_STRING:
        return body.decode("utf-8", errors="replace")
    if tag == OID:
        return decode_oid(body)
    if tag == IP_ADDRESS:
        return ".".join(str(byte) for byte in body)
    if tag == NULL:
        return None
    return body


# ====================================================================== the client


@dataclass(frozen=True)
class VarBind:
    oid: str
    value: object


def _message(
    community: str,
    pdu_tag: int,
    request_id: int,
    oids: list[str],
    field_two: int = 0,
    field_three: int = 0,
) -> bytes:
    """One SNMP message. GETBULK reuses the error-status and error-index positions for
    non-repeaters and max-repetitions — structurally identical, semantically not."""
    varbinds = b"".join(_tlv(SEQUENCE, encode_oid(oid) + _tlv(NULL, b"")) for oid in oids)
    pdu = _tlv(
        pdu_tag,
        encode_int(request_id)
        + encode_int(field_two)
        + encode_int(field_three)
        + _tlv(SEQUENCE, varbinds),
    )
    return _tlv(
        SEQUENCE,
        encode_int(VERSION_2C) + _tlv(OCTET_STRING, community.encode()) + pdu,
    )


def _parse(reply: bytes, expect_id: int) -> list[VarBind]:
    tag, body, _ = _read_tlv(reply, 0)
    if tag != SEQUENCE:
        raise SnmpError("reply is not an SNMP message")
    _, _, at = _read_tlv(body, 0)  # version
    _, _, at = _read_tlv(body, at)  # community
    pdu_tag, pdu, _ = _read_tlv(body, at)
    if pdu_tag != RESPONSE:
        raise SnmpError(f"expected a Response PDU, got 0x{pdu_tag:02x}")

    _, id_body, at = _read_tlv(pdu, 0)
    if int.from_bytes(id_body, "big", signed=True) != expect_id:
        # A stale reply from an attempt that already timed out. Discarding it is the
        # whole reason the id exists.
        raise SnmpError("reply carries another request's id")
    _, status_body, at = _read_tlv(pdu, at)
    _, _, at = _read_tlv(pdu, at)  # error-index
    status = int.from_bytes(status_body, "big", signed=True)
    if status:
        raise SnmpError(f"agent reported error-status {status}")

    _, list_body, _ = _read_tlv(pdu, at)
    found: list[VarBind] = []
    at = 0
    while at < len(list_body):
        _, pair, at = _read_tlv(list_body, at)
        _, oid_body, value_at = _read_tlv(pair, 0)
        value_tag, value_body, _ = _read_tlv(pair, value_at)
        found.append(VarBind(decode_oid(oid_body), decode_value(value_tag, value_body)))
    return found


def _exchange(
    host: str,
    community: str,
    pdu_tag: int,
    oids: list[str],
    timeout: float,
    port: int,
    field_two: int = 0,
    field_three: int = 0,
) -> list[VarBind]:
    last: Exception | None = None
    for attempt, pause in enumerate((0.0, *BACKOFF_S)):
        request_id = random.randrange(1, 2**31 - 1)
        payload = _message(community, pdu_tag, request_id, oids, field_two, field_three)
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.settimeout(timeout + pause)
                sock.sendto(payload, (host, port))
                reply, _ = sock.recvfrom(MAX_DATAGRAM)
            return _parse(reply, request_id)
        except (OSError, SnmpError) as exc:
            last = exc
            if attempt == len(BACKOFF_S):
                break
    raise SnmpError(f"no usable reply from {host}: {last}")


def get(
    host: str,
    community: str = "public",
    oids: list[str] | None = None,
    timeout: float = 2.0,
    port: int = 161,
) -> dict[str, object]:
    """Fetch named objects. Missing objects are absent from the result, never zero."""
    if not oids:
        return {}
    bound = _exchange(host, community, GET, oids, timeout, port)
    return {bind.oid: bind.value for bind in bound if bind.value is not None}


def walk(
    host: str,
    community: str = "public",
    root: str = "1.3.6.1.2.1.1",
    timeout: float = 2.0,
    port: int = 161,
    max_rows: int = 200,
) -> dict[str, object]:
    """Walk a subtree with GETBULK.

    A short reply is not an error — RFC 3416 lets an agent truncate to fit the message
    size — so the walk ends on endOfMibView, on leaving the subtree, or on a reply that
    advances nothing.
    """
    found: dict[str, object] = {}
    cursor = root
    while len(found) < max_rows:
        try:
            bound = _exchange(
                host, community, GET_BULK, [cursor], timeout, port, field_two=0, field_three=10
            )
        except SnmpError:
            break
        advanced = False
        for bind in bound:
            if not bind.oid.startswith(root + ".") and bind.oid != root:
                return found  # walked out of the subtree
            if bind.value is None:
                continue
            if bind.oid not in found:
                found[bind.oid] = bind.value
                advanced = True
            cursor = bind.oid
        if not advanced:
            break
    return found


def identify(host: str, community: str = "public", timeout: float = 1.5) -> str | None:
    """sysDescr, or None. The cheapest question that proves an agent is listening."""
    try:
        answer = get(host, community, ["1.3.6.1.2.1.1.1.0"], timeout=timeout)
    except SnmpError:
        return None
    value = answer.get("1.3.6.1.2.1.1.1.0")
    return str(value) if value is not None else None
