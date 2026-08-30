"""Reading 802.11 frames out of a pcap stream, and attributing their bytes to devices.

This is how NetPulse answers "which device used the data" on a router that cannot say.
A WiFi radio in monitor mode hears every frame in range. The *payload* of each one is
encrypted with a key only its sender and the access point hold — but the MAC header is
not, and never can be, because the radios need it to know who is talking. That header
carries who sent the frame, who it is for, and how long it is, which is exactly enough
to count bytes per device without decrypting anything.

Three rules, all of which the tests pin.

**Every device is counted, and which access point it was on is kept.** Monitor mode
hears whatever is in range, which for a household of phones, laptops and TVs is the
point. The BSSID travels with each device's total rather than being used to throw
frames away, so a caller can group by access point, narrow to one, or take the lot —
without the counting having already decided for it.

**Only frames that carried data.** Management, control and null-data frames move no
bytes; counting their airtime as traffic would report a device as busy while it sat
idle sending keepalives.

**Retransmissions are counted separately.** A retried frame is the same data again. It
is real airtime and a good signal of a struggling link, so it is tallied — but not as
bytes moved, because that would double-count the transfer.

The result is bytes *over the air*: 802.11 headers included, and the frame as the radio
sent it rather than the IP payload inside. That is a different quantity from what an
ISP bills, it is consistently different, and it is named so nobody reads it as the other
one.
"""

from __future__ import annotations

import struct
from collections.abc import Iterator
from dataclasses import dataclass
from typing import BinaryIO

#: pcap link types. Monitor-mode captures carry a radiotap header ahead of the frame;
#: some drivers hand over the bare frame instead.
RADIOTAP = 127
BARE_802_11 = 105

_PCAP_MAGIC_LE = 0xA1B2C3D4
_PCAP_MAGIC_NS_LE = 0xA1B23C4D

#: 802.11 frame types, from IEEE 802.11 §9.2.4.1.3.
TYPE_DATA = 2
#: Data subtypes with no payload: plain null (4), QoS null (12), and their variants.
_EMPTY_SUBTYPES = frozenset({4, 5, 6, 7, 12, 13, 14, 15})


@dataclass(frozen=True, slots=True)
class Frame:
    """One data frame, reduced to who moved how much.

    `device` is the station — never the access point. `downlink` says which way it went,
    from that station's point of view, so a caller never has to reason about which
    address field meant what.
    """

    device: str
    bssid: str
    length: int
    downlink: bool
    retry: bool


def _mac(raw: bytes) -> str:
    return ":".join(f"{byte:02x}" for byte in raw)


def _read(stream: BinaryIO, count: int) -> bytes | None:
    """Exactly `count` bytes, or None at end of stream.

    A pipe returns short reads whenever it feels like it; treating one as the end would
    silently truncate a capture and under-report every device on it.
    """
    chunks = []
    remaining = count
    while remaining > 0:
        chunk = stream.read(remaining)
        if not chunk:
            return None
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def frames(stream: BinaryIO) -> Iterator[Frame]:
    """Every attributable data frame in a pcap stream, as it arrives.

    A generator on purpose: the stream is a live capture that never ends, and nothing
    here may wait for it to.
    """
    header = _read(stream, 24)
    if header is None or len(header) < 24:
        return
    (magic,) = struct.unpack("<I", header[:4])
    if magic in (_PCAP_MAGIC_LE, _PCAP_MAGIC_NS_LE):
        endian = "<"
    elif struct.unpack(">I", header[:4])[0] in (_PCAP_MAGIC_LE, _PCAP_MAGIC_NS_LE):
        endian = ">"
    else:
        raise ValueError("not a pcap stream")
    (link,) = struct.unpack(endian + "I", header[20:24])
    if link not in (RADIOTAP, BARE_802_11):
        raise ValueError(f"capture is link type {link}, not 802.11 — wrong interface?")

    while True:
        record = _read(stream, 16)
        if record is None:
            return
        _, _, captured, original = struct.unpack(endian + "IIII", record)
        body = _read(stream, captured)
        if body is None:
            return
        found = _parse(body, original, radiotap=link == RADIOTAP)
        if found is not None:
            yield found


def _parse(body: bytes, original: int, *, radiotap: bool) -> Frame | None:
    """One captured record as a Frame, or None when it is not attributable traffic."""
    offset = 0
    if radiotap:
        if len(body) < 4:
            return None
        # The radiotap length is little-endian whatever the file's byte order is —
        # the header defines its own, and getting this wrong walks into the middle of
        # the frame and reads two arbitrary bytes as a frame control field.
        (radiotap_len,) = struct.unpack_from("<H", body, 2)
        if radiotap_len < 8 or radiotap_len > len(body):
            return None
        offset = radiotap_len

    if len(body) - offset < 24:  # frame control, duration, three addresses, sequence
        return None
    control, flags = body[offset], body[offset + 1]
    if (control >> 2) & 0x3 != TYPE_DATA:
        return None
    if (control >> 4) & 0xF in _EMPTY_SUBTYPES:
        return None

    to_ds, from_ds = bool(flags & 0x01), bool(flags & 0x02)
    if to_ds == from_ds:
        # Neither is an ad-hoc frame, both is a mesh or bridge link. In an
        # infrastructure network every station's traffic sets exactly one, and a frame
        # that sets neither or both has no station to attribute it to.
        return None
    address1 = _mac(body[offset + 4 : offset + 10])
    address2 = _mac(body[offset + 10 : offset + 16])
    device, bssid = (address1, address2) if from_ds else (address2, address1)

    # The over-the-air size, from the original length rather than the captured one: a
    # snaplen that truncates the copy must not also shrink the number.
    length = original - offset
    if length <= 0:
        return None
    return Frame(
        device=device,
        bssid=bssid,
        length=length,
        downlink=from_ds,
        retry=bool(flags & 0x08),
    )


@dataclass(slots=True)
class Station:
    """One device's traffic over one interval, and the link it moved it across."""

    device: str
    bssid: str = ""
    down: int = 0
    up: int = 0
    frames: int = 0
    retries: int = 0

    @property
    def retry_pct(self) -> float | None:
        """Retransmissions per frame, or None when nothing was heard from it.

        No consumer router shows this per device, and it separates two faults that feel
        identical from a browser: a congested channel, where one device retries and the
        rest are fine, from a weak link to the tower, where every device is slow at once.
        """
        return 100.0 * self.retries / self.frames if self.frames else None


class Tally:
    """Running byte counts per device, over one capture interval.

    Counts are drained rather than read: each call to `take` returns what was seen since
    the previous one and resets. The store wants intervals, not totals — an interval is
    true on its own, where a total has to be interpreted against the last one and a
    restart quietly corrupts it.

    `bssid` narrows the count to a single access point when given. Left empty, every
    device in range is counted and each one carries the access point it was heard on,
    which is the information the filter would otherwise have destroyed.
    """

    def __init__(self, bssid: str = "") -> None:
        self.bssid = bssid.lower()
        self._stations: dict[str, Station] = {}

    def add(self, frame: Frame) -> None:
        if self.bssid and frame.bssid != self.bssid:
            return
        if frame.device.startswith(("01:", "33:33")) or frame.device == "ff:ff:ff:ff:ff:ff":
            return  # multicast and broadcast belong to no single device
        station = self._stations.get(frame.device)
        if station is None:
            station = self._stations[frame.device] = Station(frame.device, frame.bssid)
        station.frames += 1
        if frame.retry:
            station.retries += 1
            return  # airtime, not new bytes
        if frame.downlink:
            station.down += frame.length
        else:
            station.up += frame.length

    def take(self) -> list[Station]:
        """Every device heard since the last call, drained. Ordered by what they moved,
        because the answer to "who used the data" is the top of that list."""
        found = sorted(self._stations.values(), key=lambda s: -(s.down + s.up))
        self._stations = {}
        return found
