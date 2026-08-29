"""Where a carrier actually sits in the spectrum.

Routers report the channel a carrier is on as an EARFCN (LTE) or NR-ARFCN (5G) — an
integer index, not a frequency. Turning those into megahertz is what makes carrier
aggregation something you can *see*: five numbers like `2852+1650+3050+6250` become
801, 1850, 2630 and 2650 MHz, laid out where they really are.

The conversions are 3GPP's own, from TS 36.101 §5.7.3 and TS 38.104 §5.4.2.1. They are
exact arithmetic, not approximations — which matters, because a spectrum chart drawn on
guessed positions would be decoration rather than measurement.
"""

from __future__ import annotations

from dataclasses import dataclass

#: LTE downlink, TS 36.101 Table 5.7.3-1: band -> (F_DL_low MHz, N_Offs-DL).
#: F_DL = F_DL_low + 0.1 * (N_DL - N_Offs-DL)
LTE_BANDS: dict[int, tuple[float, int]] = {
    1: (2110.0, 0),
    2: (1930.0, 600),
    3: (1805.0, 1200),
    4: (2110.0, 1950),
    5: (869.0, 2400),
    7: (2620.0, 2750),
    8: (925.0, 3450),
    11: (1475.9, 4750),
    12: (729.0, 5010),
    13: (746.0, 5180),
    14: (758.0, 5280),
    17: (734.0, 5730),
    18: (860.0, 5850),
    19: (875.0, 6000),
    20: (791.0, 6150),
    21: (1495.9, 6450),
    25: (1930.0, 8040),
    26: (859.0, 8690),
    28: (758.0, 9210),
    32: (1452.0, 9920),
    38: (2570.0, 37750),
    39: (1880.0, 38250),
    40: (2300.0, 38650),
    41: (2496.0, 39650),
    42: (3400.0, 41590),
    43: (3600.0, 43590),
    66: (2110.0, 66436),
    71: (617.0, 68586),
}

#: NR-ARFCN, TS 38.104 Table 5.4.2.1-1: (N_low, N_high, ΔF_global kHz, F_offs MHz, N_offs).
NR_RANGES: tuple[tuple[int, int, float, float, int], ...] = (
    (0, 599999, 5.0, 0.0, 0),
    (600000, 2016666, 15.0, 3000.0, 600000),
    (2016667, 3279165, 60.0, 24250.08, 2016667),
)

#: Common band labels, for saying "n78" rather than "band 78".
NR_BAND_NAMES = {78: "n78", 77: "n77", 79: "n79", 28: "n28", 3: "n3", 1: "n1", 41: "n41"}


def lte_mhz(earfcn: int, band: int | None = None) -> float | None:
    """Downlink centre frequency for an LTE channel number.

    The band is used when the router names one, and otherwise inferred by finding the
    band whose channel range contains this EARFCN — several bands overlap in frequency
    but never in channel numbering, so the inference is unambiguous.
    """
    if band is not None and band in LTE_BANDS:
        low, offset = LTE_BANDS[band]
        return round(low + 0.1 * (earfcn - offset), 2)
    for candidate, (low, offset) in sorted(LTE_BANDS.items()):
        # Each band's block is 0.1 MHz per channel; a plausible fit lands inside it.
        span = _lte_span(candidate)
        if span is not None and offset <= earfcn <= offset + span * 10:
            return round(low + 0.1 * (earfcn - offset), 2)
    return None


#: Bandwidth of each band's downlink block in MHz, enough to bound its channel range.
LTE_SPANS: dict[int, float] = {
    1: 60,
    2: 60,
    3: 75,
    4: 45,
    5: 25,
    7: 70,
    8: 35,
    11: 20,
    12: 17,
    13: 10,
    14: 10,
    17: 12,
    18: 15,
    19: 15,
    20: 30,
    21: 15,
    25: 65,
    26: 35,
    28: 45,
    32: 44,
    38: 50,
    39: 40,
    40: 100,
    41: 194,
    42: 200,
    43: 200,
    66: 90,
    71: 35,
}


def _lte_span(band: int) -> float | None:
    return LTE_SPANS.get(band)


def nr_mhz(arfcn: int) -> float | None:
    """Centre frequency for an NR channel number, across all three global ranges."""
    for low, high, step_khz, offset_mhz, offset_n in NR_RANGES:
        if low <= arfcn <= high:
            return round(offset_mhz + (step_khz / 1000.0) * (arfcn - offset_n), 3)
    return None


@dataclass(frozen=True)
class Carrier:
    """One component carrier, positioned in real spectrum."""

    #: "lte" or "nr" — which leg of a 5G non-standalone connection this belongs to.
    leg: str
    band: int | None
    channel: int
    mhz: float | None
    bandwidth_mhz: float | None
    pci: int | None

    @property
    def label(self) -> str:
        if self.band is None:
            return f"ch {self.channel}"
        return (
            NR_BAND_NAMES.get(self.band, f"B{self.band}") if self.leg == "nr" else f"B{self.band}"
        )


def _split(value: str) -> list[str]:
    """Routers join per-carrier values with "+", and pad with blanks when a slot is
    unused. An empty slot is dropped rather than parsed as zero."""
    return [part.strip() for part in str(value or "").split("+") if part.strip()]


def _int(value: str) -> int | None:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _float(value: str) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def carriers(
    bands: str, channels: str, bandwidths: str, pcis: str = "", leg: str = "lte"
) -> list[Carrier]:
    """Parse a router's "+"-joined carrier lists into positioned carriers.

    The lists are meant to run parallel, and mostly do — but firmware ships mismatched
    lengths, so each list is indexed independently and a missing entry becomes None
    rather than shifting every carrier after it onto the wrong frequency.
    """
    band_list, channel_list = _split(bands), _split(channels)
    width_list, pci_list = _split(bandwidths), _split(pcis)
    count = max(len(band_list), len(channel_list))

    found: list[Carrier] = []
    for index in range(count):
        band = _int(band_list[index]) if index < len(band_list) else None
        channel = _int(channel_list[index]) if index < len(channel_list) else None
        if channel is None:
            continue
        mhz = nr_mhz(channel) if leg == "nr" else lte_mhz(channel, band)
        found.append(
            Carrier(
                leg=leg,
                band=band,
                channel=channel,
                mhz=mhz,
                bandwidth_mhz=_float(width_list[index]) if index < len(width_list) else None,
                pci=_int(pci_list[index]) if index < len(pci_list) else None,
            )
        )
    return found


def spectrum_metrics(stack: list[Carrier]) -> dict[str, float]:
    """A carrier stack as metrics, in the one shape every adapter emits.

    Carrier aggregation is the usual explanation for throughput changing while the
    signal does not: losing a 20 MHz carrier halves what the link can carry with RSRP
    sitting exactly where it was. Every router that knows its bands can say so through
    this, and the dashboard never learns which vendor it came from.
    """
    if not stack:
        return {}
    metrics: dict[str, float] = {"radio.carriers": float(len(stack))}
    total = 0.0
    for index, carrier in enumerate(stack):
        prefix = f"radio.cc{index}"
        if carrier.mhz is not None:
            metrics[f"{prefix}.mhz"] = carrier.mhz
        if carrier.bandwidth_mhz is not None:
            metrics[f"{prefix}.bw_mhz"] = carrier.bandwidth_mhz
            total += carrier.bandwidth_mhz
        if carrier.band is not None:
            metrics[f"{prefix}.band"] = float(carrier.band)
        if carrier.pci is not None:
            metrics[f"{prefix}.pci"] = float(carrier.pci)
        metrics[f"{prefix}.nr"] = 1.0 if carrier.leg == "nr" else 0.0
    if total:
        metrics["radio.aggregate_mhz"] = total
    return metrics


#: Bandwidth strings differ per vendor: ZLT says "20", Huawei says "20MHz", MikroTik
#: says "B3@20Mhz". This pulls the number out of any of them.
def bandwidth_mhz(value: str) -> str:
    import re as _re

    found = _re.search(r"(\d+(?:\.\d+)?)", str(value or ""))
    return found.group(1) if found else ""
