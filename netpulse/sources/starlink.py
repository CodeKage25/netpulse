"""Starlink, over gRPC-Web — no gRPC library, no protobuf compiler, no credentials.

The dish speaks gRPC on 192.168.100.1:9200, which is HTTP/2 and out of reach of the
standard library. But **port 9201 serves the same service over gRPC-Web on HTTP/1.1**,
which is what the mobile app and the dish's own web UI use — and what makes a
stdlib-only client possible at all. Dishylink, being a browser app, had no other choice
either.

There is no authentication anywhere in this path. The dish keeps a separate mTLS
channel for SpaceX's own backend; the LAN side is deliberately open, so a client that
prompts for a password is a client asking for something that does not exist.

Two traps, both of which look like parser bugs:

* **The dish validates `Referer`.** An unrecognised one returns an empty 200 rather
  than an error, so this sends none at all.
* **A gRPC error still arrives as HTTP 200**, with the real status in a trailer frame.
  The status line proves nothing; the trailer does.

Only the fields NetPulse charts are decoded. Protobuf is length-delimited and
self-describing enough to skip what you do not recognise, which is what makes reading a
handful of fields out of a large message reasonable without the schema.
"""

from __future__ import annotations

import struct
from collections.abc import Callable
from http.client import HTTPConnection

from netpulse.core.model import Reading
from netpulse.sources import AdapterError

DEFAULT_HOST = "192.168.100.1"
GRPC_WEB_PORT = 9201
PATH = "/SpaceX.API.Device.Device/Handle"

#: Request frames, pre-encoded. Each is `flags(1) + length(4) + payload`, where the
#: payload sets one field of the request oneof to an empty message: tag byte(s) for
#: `(field << 3) | 2`, then a zero length.
GET_STATUS = bytes.fromhex("0000000003e23e00")  # field 1004
GET_DEVICE_INFO = bytes.fromhex("0000000003823f00")  # field 1008

#: Field numbers inside DishGetStatusResponse, from the published reflection dump.
DOWNLINK_BPS = 1007
UPLINK_BPS = 1008
POP_PING_LATENCY = 1009
POP_PING_DROP = 1003
DEVICE_STATE = 1005
DEVICE_INFO = 1
OBSTRUCTION = 1004
OUTAGE = 1014

#: The response carries its payload inside the same oneof field the request set.
STATUS_IN_RESPONSE = 1004
INFO_IN_RESPONSE = 1008
#: Fields that only ever appear in the status payload itself, used to tell an
#: already-unwrapped message from a wrapped one.
PAYLOAD_MARKERS = (DOWNLINK_BPS, POP_PING_LATENCY, DEVICE_STATE)

Fetch = Callable[[str, int, bytes], bytes]


def _http_fetch(host: str, port: int, frame: bytes) -> bytes:
    connection = HTTPConnection(host, port, timeout=5)
    try:
        # No Referer and no Origin on purpose: the dish silently returns an empty 200
        # for one it does not recognise, which reads as a parse failure rather than a
        # rejection.
        connection.request(
            "POST",
            PATH,
            body=frame,
            headers={"Content-Type": "application/grpc-web+proto", "X-Grpc-Web": "1"},
        )
        response = connection.getresponse()
        return response.read()
    finally:
        connection.close()


def _frames(payload: bytes) -> tuple[bytes, str]:
    """Split a gRPC-Web body into its message bytes and its trailer text.

    A trailer frame has the high bit of the flags byte set; it carries `grpc-status`,
    which is the only place a failure is actually reported.
    """
    message = b""
    trailer = ""
    at = 0
    while at + 5 <= len(payload):
        flags = payload[at]
        size = struct.unpack(">I", payload[at + 1 : at + 5])[0]
        body = payload[at + 5 : at + 5 + size]
        if flags & 0x80:
            trailer = body.decode("utf-8", errors="replace")
        else:
            message += body
        at += 5 + size
    return message, trailer


def fields(buffer: bytes) -> dict[int, list[object]]:
    """Decode one protobuf message into {field number: [values]}.

    Unknown fields are skipped rather than failing, which is the property that lets a
    handful of readings be pulled out of a large message with no schema at all.
    """
    found: dict[int, list[object]] = {}
    at = 0
    while at < len(buffer):
        key, at = _varint(buffer, at)
        number, wire = key >> 3, key & 7
        value: object
        if wire == 0:
            value, at = _varint(buffer, at)
        elif wire == 1:
            value, at = struct.unpack("<d", buffer[at : at + 8])[0], at + 8
        elif wire == 2:
            size, at = _varint(buffer, at)
            value, at = buffer[at : at + size], at + size
        elif wire == 5:
            value, at = struct.unpack("<f", buffer[at : at + 4])[0], at + 4
        else:
            break  # groups: deprecated, and nothing here uses them
        found.setdefault(number, []).append(value)
    return found


def _varint(buffer: bytes, at: int) -> tuple[int, int]:
    value = shift = 0
    while at < len(buffer):
        byte = buffer[at]
        value |= (byte & 0x7F) << shift
        at += 1
        if not byte & 0x80:
            break
        shift += 7
    return value, at


def _one(found: dict[int, list[object]], number: int) -> object | None:
    values = found.get(number)
    return values[0] if values else None


def _float(found: dict[int, list[object]], number: int) -> float | None:
    value = _one(found, number)
    return float(value) if isinstance(value, (int, float)) else None


class StarlinkAdapter:
    kind = "starlink"

    def __init__(
        self,
        name: str,
        url: str = f"http://{DEFAULT_HOST}",
        *,
        fetch: Fetch = _http_fetch,
    ):
        self.name = name
        self.base = url.split("#")[0].rstrip("/")
        self.host = self.base.split("//")[-1].split("/")[0].split(":")[0]
        self._fetch = fetch
        self._cycle = 0
        self._identity: dict[str, str] = {}

    def _call(self, frame: bytes) -> dict[int, list[object]]:
        try:
            payload = self._fetch(self.host, GRPC_WEB_PORT, frame)
        except OSError as exc:
            raise AdapterError(
                f"dish unreachable at {self.host}:{GRPC_WEB_PORT} — in bypass mode or "
                f"behind another router, {DEFAULT_HOST}/24 needs a static route: {exc}"
            ) from exc
        message, trailer = _frames(payload)
        if not message:
            raise AdapterError(f"dish returned nothing ({trailer.strip() or 'empty reply'})")
        status = ""
        for line in trailer.splitlines():
            if line.lower().startswith("grpc-status"):
                status = line.split(":", 1)[-1].strip()
        # The HTTP status is 200 even for a gRPC failure; only the trailer knows.
        if status and status != "0":
            raise AdapterError(f"dish refused the call (grpc-status {status})")
        return fields(message)

    def read(self) -> Reading:
        status = self._unwrap(self._call(GET_STATUS), STATUS_IN_RESPONSE)

        metrics: dict[str, float] = {}
        texts: dict[str, str] = {}

        for number, metric in (
            (DOWNLINK_BPS, "traffic.down_bytes_s"),
            (UPLINK_BPS, "traffic.up_bytes_s"),
        ):
            if (value := _float(status, number)) is not None:
                metrics[metric] = value / 8.0  # the dish reports bits, we store bytes

        if (latency := _float(status, POP_PING_LATENCY)) is not None:
            metrics["latency.internet_ms"] = latency
        if (drop := _float(status, POP_PING_DROP)) is not None:
            metrics["loss.pct"] = drop * 100.0

        state = _one(status, DEVICE_STATE)
        if isinstance(state, bytes) and (uptime := _float(fields(state), 1)) is not None:
            metrics["router.uptime_s"] = uptime

        blocked = _one(status, OBSTRUCTION)
        if isinstance(blocked, bytes) and (fraction := _float(fields(blocked), 1)) is not None:
            metrics["sky.obstructed_pct"] = fraction * 100.0

        # There is no connection-state field: an absent outage message is the signal.
        # Reading it the other way round would report every dish as permanently down.
        metrics["up"] = 0.0 if OUTAGE in status else 1.0

        self._cycle += 1
        if not self._identity or self._cycle % 60 == 1:
            self._identity = self._device_info()
        texts.update(self._identity)
        return Reading(metrics=metrics, texts=texts)

    @staticmethod
    def _unwrap(outer: dict[int, list[object]], wrapper: int) -> dict[int, list[object]]:
        """Reach the payload whether or not it arrived inside the response oneof.

        Firmware differs on this, and the number of the wrapping field is not something
        to guess at — so the presence of a field only the payload has is the test.
        """
        if any(marker in outer for marker in PAYLOAD_MARKERS):
            return outer
        body = _one(outer, wrapper)
        return fields(body) if isinstance(body, bytes) else outer

    def _device_info(self) -> dict[str, str]:
        """Hardware and firmware, refreshed rarely — none of it changes minute to minute."""
        try:
            outer = self._call(GET_DEVICE_INFO)
        except AdapterError:
            return dict(self._identity)
        info = self._unwrap(outer, INFO_IN_RESPONSE)
        texts: dict[str, str] = {"net.operator": "Starlink"}
        info = self._unwrap(info, DEVICE_INFO)
        for number, key in ((2, "router.hardware"), (3, "router.firmware"), (4, "net.country")):
            value = _one(info, number)
            if isinstance(value, bytes):
                texts[key] = value.decode("utf-8", errors="replace")
        return texts
