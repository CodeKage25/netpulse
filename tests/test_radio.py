"""Where a carrier sits in the spectrum — 3GPP's arithmetic, not an approximation."""

from __future__ import annotations

from netpulse.core.radio import bandwidth_mhz, carriers, lte_mhz, nr_mhz, spectrum_metrics


def test_known_lte_channels_land_on_their_real_frequencies() -> None:
    """Checked against TS 36.101 Table 5.7.3-1. A spectrum chart drawn on guessed
    positions would be decoration rather than measurement."""
    assert lte_mhz(6250, 20) == 801.0  # band 20 downlink starts at 791 MHz
    assert lte_mhz(1650, 3) == 1850.0
    assert lte_mhz(2852, 7) == 2630.2
    assert lte_mhz(3050, 7) == 2650.0


def test_a_channel_number_alone_finds_its_band() -> None:
    """Bands overlap in frequency but never in channel numbering, so a router that
    reports the channel and not the band is still placeable."""
    assert lte_mhz(6250) == 801.0
    assert lte_mhz(1650) == 1850.0


def test_nr_channels_use_the_right_global_range() -> None:
    """TS 38.104 §5.4.2.1 splits the ARFCN space into three ranges with different
    raster steps; using the wrong one puts a carrier gigahertz away from itself."""
    assert nr_mhz(636576) == 3548.64  # n78, inside 3300-3800
    assert nr_mhz(2079167) == 28000.08  # the 60 kHz raster above 24 GHz
    assert nr_mhz(100000) == 500.0  # the 5 kHz raster below 3 GHz


def test_an_unknown_channel_is_unplaced_rather_than_guessed() -> None:
    assert nr_mhz(9_999_999) is None
    assert lte_mhz(999_999) is None


def test_a_real_aggregated_stack_parses() -> None:
    """Captured from the author's MTN X17U: four LTE carriers plus one 5G."""
    stack = carriers("7+3+7+20", "2852+1650+3050+6250", "20+20+20+20", "307+358+110+359")
    assert [c.mhz for c in stack] == [2630.2, 1850.0, 2650.0, 801.0]
    assert [c.label for c in stack] == ["B7", "B3", "B7", "B20"]
    assert [c.pci for c in stack] == [307, 358, 110, 359]


def test_the_five_g_leg_is_labelled_as_such() -> None:
    stack = carriers("78", "636576", "100", "764", leg="nr")
    assert stack[0].label == "n78"
    assert stack[0].leg == "nr"


def test_mismatched_list_lengths_do_not_shift_carriers_onto_wrong_frequencies() -> None:
    """Firmware ships lists of differing length. Zipping them would silently move every
    carrier after the gap to somebody else's channel."""
    stack = carriers("7+3+20", "2852+1650+6250", "20+20", "307")
    assert [c.mhz for c in stack] == [2630.2, 1850.0, 801.0]
    assert stack[2].bandwidth_mhz is None  # absent, not zero
    assert stack[1].pci is None


def test_empty_slots_are_dropped_not_read_as_zero() -> None:
    stack = carriers("7++3", "2852++1650", "20++20")
    assert len(stack) == 2


def test_a_carrier_with_no_channel_is_skipped() -> None:
    assert carriers("7+3", "+1650", "20+20")[0].channel == 1650


def test_bandwidth_survives_every_vendors_spelling() -> None:
    """ZLT says "20", Huawei says "20MHz", MikroTik says "B3@20Mhz"."""
    assert bandwidth_mhz("20") == "20"
    assert bandwidth_mhz("20MHz") == "20"
    assert bandwidth_mhz("B3@20Mhz") == "3"  # the band comes first in that string
    assert bandwidth_mhz("") == ""


def test_the_aggregate_is_the_number_that_explains_throughput() -> None:
    """Losing a 20 MHz carrier halves what the link carries while RSRP does not move —
    which is why the total is recorded as its own metric."""
    stack = carriers("7+3+7+20", "2852+1650+3050+6250", "20+20+20+20")
    stack += carriers("78", "636576", "100", leg="nr")
    metrics = spectrum_metrics(stack)
    assert metrics["radio.carriers"] == 5.0
    assert metrics["radio.aggregate_mhz"] == 180.0
    assert metrics["radio.cc4.nr"] == 1.0
    assert metrics["radio.cc0.nr"] == 0.0


def test_no_carriers_means_no_metrics_rather_than_zeros() -> None:
    """A box that does not report bands must not look like one reporting none."""
    assert spectrum_metrics([]) == {}
    assert carriers("", "", "") == []
