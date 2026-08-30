"""Counting bytes per device from 802.11 headers, which are never encrypted.

Frames here are built to IEEE 802.11 §9.2.4 and the pcap/radiotap layouts rather than
captured, so the tests run with no radio, no privileges and no network.
"""

from __future__ import annotations

import io
import re
import struct

import pytest

from netpulse.core.capture import RADIOTAP, Frame, Tally, frames

AP = "c0:61:f9:86:15:6d"
PHONE = "e2:c3:60:3c:bb:94"
LAPTOP = "ac:bc:32:b2:82:c3"
NEIGHBOUR_AP = "aa:bb:cc:dd:ee:ff"


def mac(text: str) -> bytes:
    return bytes(int(part, 16) for part in text.split(":"))


def dot11(
    *,
    source: str,
    bssid: str,
    downlink: bool,
    payload: int = 1000,
    subtype: int = 8,
    kind: int = 2,
    retry: bool = False,
    to_ds: bool | None = None,
    from_ds: bool | None = None,
) -> bytes:
    """One 802.11 frame. `source` is the station, whichever direction it points."""
    control = (subtype << 4) | (kind << 2)
    to = downlink is False if to_ds is None else to_ds
    frm = downlink if from_ds is None else from_ds
    flags = (0x01 if to else 0) | (0x02 if frm else 0) | (0x08 if retry else 0)
    first, second = (mac(source), mac(bssid)) if downlink else (mac(bssid), mac(source))
    return (
        bytes([control, flags])
        + b"\x00\x00"  # duration
        + first
        + second
        + mac(bssid)  # address 3
        + b"\x00\x00"  # sequence control
        + b"\x00" * payload
    )


def radiotap(frame: bytes, length: int = 8) -> bytes:
    return b"\x00\x00" + struct.pack("<H", length) + b"\x00" * (length - 4) + frame


def pcap(*records: bytes, link: int = RADIOTAP) -> io.BytesIO:
    out = struct.pack("<IHHiIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, link)
    for record in records:
        out += struct.pack("<IIII", 0, 0, len(record), len(record)) + record
    return io.BytesIO(out)


def parsed(*records: bytes) -> list[Frame]:
    return list(frames(pcap(*records)))


# ------------------------------------------------------------------ the frame parser


def test_a_downlink_frame_is_attributed_to_the_station_not_the_access_point() -> None:
    """The access point sent it, but the device is what used the data."""
    found = parsed(radiotap(dot11(source=PHONE, bssid=AP, downlink=True)))
    assert len(found) == 1
    assert found[0].device == PHONE
    assert found[0].bssid == AP
    assert found[0].downlink is True


def test_an_uplink_frame_reads_the_other_address() -> None:
    """The two directions put the station in different address fields. Reading one
    field for both would report every upload as the access point's own traffic."""
    found = parsed(radiotap(dot11(source=PHONE, bssid=AP, downlink=False)))
    assert found[0].device == PHONE
    assert found[0].bssid == AP
    assert found[0].downlink is False


def test_the_length_is_the_frame_on_the_air_not_the_captured_copy() -> None:
    """A snaplen that truncates the copy must not also shrink the number."""
    record = radiotap(dot11(source=PHONE, bssid=AP, downlink=True, payload=1500))
    stream = struct.pack("<IHHiIII", 0xA1B2C3D4, 2, 4, 0, 0, 128, RADIOTAP)
    stream += struct.pack("<IIII", 0, 0, 128, len(record)) + record[:128]
    found = list(frames(io.BytesIO(stream)))
    assert found[0].length == len(record) - 8  # the whole frame, minus radiotap


def test_management_and_control_frames_move_no_bytes() -> None:
    """Beacons and acknowledgements are constant chatter. Counting their airtime as
    traffic reports a device as busy while it sits idle."""
    assert parsed(radiotap(dot11(source=PHONE, bssid=AP, downlink=True, kind=0))) == []
    assert parsed(radiotap(dot11(source=PHONE, bssid=AP, downlink=True, kind=1))) == []


def test_a_null_data_frame_is_a_keepalive_not_a_transfer() -> None:
    """Subtype 4 and QoS-null 12 carry no payload; a sleeping phone sends a stream of
    them, and counting those would show it using data all night."""
    for subtype in (4, 12):
        assert parsed(radiotap(dot11(source=PHONE, bssid=AP, downlink=True, subtype=subtype))) == []


def test_a_frame_with_no_station_to_blame_is_dropped() -> None:
    """Neither flag set is ad-hoc, both is a mesh or bridge link. In an infrastructure
    network every station's traffic sets exactly one."""
    for to_ds, from_ds in ((False, False), (True, True)):
        record = radiotap(
            dot11(source=PHONE, bssid=AP, downlink=True, to_ds=to_ds, from_ds=from_ds)
        )
        assert parsed(record) == []


def test_the_radiotap_length_is_read_little_endian_whatever_the_file_is() -> None:
    """Radiotap defines its own byte order. Reading it in the file's order walks into
    the middle of the frame and reads two arbitrary bytes as a frame control field."""
    found = parsed(radiotap(dot11(source=PHONE, bssid=AP, downlink=True), length=40))
    assert found[0].device == PHONE


def test_a_bare_frame_without_radiotap_still_parses() -> None:
    """Some drivers hand over the frame with no radio header at all."""
    stream = pcap(dot11(source=PHONE, bssid=AP, downlink=True), link=105)
    assert next(iter(frames(stream))).device == PHONE


def test_a_capture_from_the_wrong_interface_says_so() -> None:
    """Ethernet frames parse as nonsense rather than failing, so the link type is
    checked before a single byte is attributed to anybody."""
    with pytest.raises(ValueError, match=re.escape("not 802.11")):
        list(frames(pcap(b"\x00" * 64, link=1)))


def test_a_truncated_stream_ends_rather_than_inventing_a_frame() -> None:
    """The capture process can die mid-record. That is a gap, not a frame."""
    whole = pcap(radiotap(dot11(source=PHONE, bssid=AP, downlink=True))).getvalue()
    assert list(frames(io.BytesIO(whole[:-400]))) == []


# ------------------------------------------------------------------ the tally


def test_only_our_own_access_point_is_counted() -> None:
    """Monitor mode hears the neighbours. Counting their devices would be wrong
    attribution and would store traffic that is none of our business — and it buys the
    household nothing, because every device it owns is on its own access point."""
    tally = Tally(AP)
    for frame in parsed(
        radiotap(dot11(source=PHONE, bssid=AP, downlink=True)),
        radiotap(dot11(source="11:22:33:44:55:66", bssid=NEIGHBOUR_AP, downlink=True)),
    ):
        tally.add(frame)
    assert [station.device for station in tally.take()] == [PHONE]


def test_each_direction_lands_in_its_own_column() -> None:
    tally = Tally(AP)
    for frame in parsed(
        radiotap(dot11(source=PHONE, bssid=AP, downlink=True, payload=1000)),
        radiotap(dot11(source=PHONE, bssid=AP, downlink=False, payload=100)),
    ):
        tally.add(frame)
    station = tally.take()[0]
    assert station.down > station.up
    # payload plus the 802.11 header
    assert station.down == 1000 + 24 and station.up == 100 + 24


def test_a_retransmission_is_airtime_not_new_bytes() -> None:
    """The same data again. Counting it twice inflates every device on a busy channel,
    and busy is exactly when retries happen."""
    tally = Tally(AP)
    for frame in parsed(
        radiotap(dot11(source=PHONE, bssid=AP, downlink=True)),
        radiotap(dot11(source=PHONE, bssid=AP, downlink=True, retry=True)),
    ):
        tally.add(frame)
    station = tally.take()[0]
    assert station.down == 1024  # one frame's worth, not two
    assert station.retry_pct == 50.0


def test_broadcast_belongs_to_no_single_device() -> None:
    tally = Tally(AP)
    for frame in parsed(radiotap(dot11(source="ff:ff:ff:ff:ff:ff", bssid=AP, downlink=True))):
        tally.add(frame)
    assert tally.take() == []


def test_a_silent_device_reports_no_retry_rate_rather_than_zero() -> None:
    """Nothing heard from a device is not an interval free of retries for it, and only
    one of those is a measurement."""
    from netpulse.core.capture import Station

    assert Tally(AP).take() == []
    assert Station("aa:bb:cc:dd:ee:ff").retry_pct is None


def test_taking_the_count_drains_it() -> None:
    """The store records intervals. A total would have to be interpreted against the
    previous one, and a restart would quietly corrupt every figure after it."""
    tally = Tally(AP)
    for frame in parsed(radiotap(dot11(source=LAPTOP, bssid=AP, downlink=True))):
        tally.add(frame)
    assert tally.take()[0].down > 0
    assert tally.take() == []
