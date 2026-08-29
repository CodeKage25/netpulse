"""Path analysis: whose fault it is, and where the answer stops being knowable."""

from __future__ import annotations

from netpulse.path import Hop, analyse, is_internal, parse, trace

# Real traceroute output from an MTN Nigeria 5G link, trimmed.
MTN_OUTPUT = """traceroute to 1.1.1.1 (1.1.1.1), 16 hops max, 60 byte packets
 1  192.168.0.1  9.612 ms  9.104 ms
 2  10.9.149.2  40.300 ms  38.221 ms
 3  10.9.232.121  59.812 ms  58.004 ms
 4  10.198.135.1  60.041 ms  59.550 ms
 5  * * *
 6  102.89.89.78  40.212 ms  41.001 ms
 7  105.177.8.64  139.702 ms  141.220 ms
 8  41.181.244.177  160.100 ms  159.400 ms
"""


def replay(output: str):  # type: ignore[no-untyped-def]
    return lambda command: output


# ------------------------------------------------------------------ parsing


def test_a_real_traceroute_parses() -> None:
    hops = parse(MTN_OUTPUT)
    assert len(hops) == 8
    assert hops[0].host == "192.168.0.1"
    assert hops[0].rtt_ms == 9.612  # median of two probes takes the upper
    assert hops[4].silent is True
    assert hops[4].rtt_ms is None


def test_a_silent_hop_is_not_a_lost_packet() -> None:
    """Routers deprioritise the replies traceroute needs. Silence is refusal to
    answer, not loss, and reporting it as loss would invent a fault."""
    hops = parse(" 5  * * *\n")
    assert hops[0].silent is True
    assert hops[0].host == "*"


def test_hostnames_and_addresses_both_parse() -> None:
    hops = parse(" 3  core1.mtn.ng (102.89.89.78)  41.0 ms\n")
    assert hops[0].host == "102.89.89.78"  # the address, not the name


def test_output_with_no_hops_yields_nothing() -> None:
    assert parse("traceroute: unknown host\n") == []


# ------------------------------------------------------------------ address space


def test_private_and_cgnat_ranges_are_internal() -> None:
    for host in ("10.9.149.2", "192.168.0.1", "172.16.4.1", "172.31.255.1", "100.64.0.1"):
        assert is_internal(host), host


def test_addresses_adjacent_to_the_private_ranges_are_not() -> None:
    """172.15 and 172.32 are ordinary public space; the private block is 172.16/12."""
    for host in ("172.15.0.1", "172.32.0.1", "102.89.89.78", "1.1.1.1"):
        assert not is_internal(host), host


# ------------------------------------------------------------------ attribution


def test_a_rise_inside_the_carriers_network_is_named_as_theirs() -> None:
    """A private address past your router is inside the carrier's network, and that is
    the one segment a traceroute can attribute with certainty."""
    output = """ 1  192.168.0.1  4.0 ms
 2  10.9.149.2  190.0 ms
 3  10.9.232.121  196.0 ms
 4  102.89.89.78  200.0 ms
 5  1.1.1.1  208.0 ms
"""
    verdict = analyse(parse(output))
    assert verdict.where == "carrier"
    assert verdict.culprit is not None
    assert "10.9.149.2" in verdict.detail


def test_the_real_mtn_path_blames_the_international_leg() -> None:
    """Captured from the author's own 5G link: seven hops of MTN at 40-60ms, then a
    +99ms step onto public space. The step out is the finding, not the domestic core."""
    verdict = analyse(parse(MTN_OUTPUT))
    assert verdict.where == "beyond"
    assert verdict.culprit is not None
    assert verdict.culprit.host == "105.177.8.64"


def test_a_rise_on_public_space_stops_short_of_blaming_anyone() -> None:
    """Transit, peering and the destination are indistinguishable without asking who
    owns the address — which would mean sending the path to a third party."""
    output = """ 1  192.168.0.1  4.0 ms
 2  10.9.1.1  8.0 ms
 3  102.89.1.1  12.0 ms
 4  41.181.2.2  220.0 ms
 5  1.1.1.1  228.0 ms
"""
    verdict = analyse(parse(output))
    assert verdict.where == "beyond"
    assert "cannot tell those apart" in verdict.detail


def test_the_first_hop_is_your_own_equipment() -> None:
    output = """ 1  192.168.0.1  180.0 ms
 2  10.9.1.1  186.0 ms
 3  1.1.1.1  190.0 ms
"""
    verdict = analyse(parse(output))
    assert verdict.where == "local"
    assert "Wi-Fi" in verdict.detail


def test_a_middle_hop_spike_that_does_not_persist_is_not_a_finding() -> None:
    """A hop reporting 400ms while everything past it reports 40ms found a busy control
    plane, not a problem: its own replies are deprioritised, the traffic through it is
    not. Blaming it is the classic traceroute misreading."""
    output = """ 1  192.168.0.1  4.0 ms
 2  10.9.1.1  8.0 ms
 3  10.9.2.1  400.0 ms
 4  102.89.1.1  38.0 ms
 5  1.1.1.1  42.0 ms
"""
    verdict = analyse(parse(output))
    assert verdict.where == "clear"
    assert verdict.culprit is None


def test_latency_that_builds_gradually_blames_nobody() -> None:
    """Distance is not a fault."""
    output = "".join(f" {i}  10.0.0.{i}  {i * 12}.0 ms\n" for i in range(1, 9))
    verdict = analyse(parse(output))
    assert verdict.where == "clear"
    assert "distance" in verdict.detail


def test_too_few_answering_hops_is_unknown_not_clear() -> None:
    """ "We could not tell" and "nothing is wrong" are different answers."""
    verdict = analyse(parse(" 1  192.168.0.1  4.0 ms\n 2  * * *\n 3  * * *\n"))
    assert verdict.where == "unknown"


def test_no_traceroute_at_all_says_so() -> None:
    verdict = analyse([])
    assert verdict.where == "unknown"
    assert "unavailable" in verdict.detail


def test_a_small_absolute_rise_on_a_slow_path_is_not_the_cause() -> None:
    """40ms added to a 900ms path is not what is wrong with that path. The rise that
    matters is the one already there, and a later hop adding a fraction of it is noise."""
    output = """ 1  192.168.0.1  4.0 ms
 2  10.9.1.1  860.0 ms
 3  102.89.1.1  880.0 ms
 4  1.1.1.1  900.0 ms
"""
    verdict = analyse(parse(output))
    assert verdict.where == "carrier"  # hop 2 owns it, not hops 3 or 4
    assert verdict.culprit is not None
    assert verdict.culprit.host == "10.9.1.1"


def test_a_slow_gateway_is_a_local_problem_however_slow_the_rest_is() -> None:
    """If your own router takes 850ms to answer, that is not the carrier's fault."""
    output = """ 1  192.168.0.1  850.0 ms
 2  10.9.1.1  895.0 ms
 3  1.1.1.1  900.0 ms
"""
    assert analyse(parse(output)).where == "local"


def test_extra_local_hops_can_be_declared() -> None:
    """Behind a mesh or double-NAT, more than one leading hop is your own equipment —
    and getting it wrong is the difference between blaming your wifi and blaming MTN."""
    output = """ 1  192.168.1.1  3.0 ms
 2  192.168.0.1  150.0 ms
 3  10.9.1.1  156.0 ms
 4  1.1.1.1  160.0 ms
"""
    assert analyse(parse(output), local_hops=1).where == "carrier"
    assert analyse(parse(output), local_hops=2).where == "local"


# ------------------------------------------------------------------ the runner


def test_a_missing_traceroute_binary_returns_nothing_rather_than_guessing() -> None:
    import netpulse.path as path_module

    original = path_module.shutil.which
    path_module.shutil.which = lambda name: None  # type: ignore[assignment]
    try:
        assert trace("1.1.1.1") == []
    finally:
        path_module.shutil.which = original  # type: ignore[assignment]


def test_hops_survive_a_round_trip_through_the_runner() -> None:
    hops = trace("1.1.1.1", run=replay(MTN_OUTPUT))
    assert isinstance(hops[0], Hop)
    assert hops[0].host == "192.168.0.1"


# ------------------------------------------------------------------ awkward real output


def test_a_hop_answering_from_two_addresses_keeps_both_probe_times() -> None:
    """traceroute prints the second address on an unnumbered continuation line. Dropping
    it computes the median from fewer probes than were actually sent."""
    output = """12  41.181.244.232  159.175 ms
    80.249.211.140  150.334 ms
13  1.1.1.1  170.0 ms
"""
    hops = parse(output)
    assert len(hops) == 2
    assert hops[0].host == "41.181.244.232"  # the first address seen is the one named
    assert hops[0].rtt_ms == 159.175  # median of both probes, not of one


def test_a_leading_star_before_an_address_still_finds_the_address() -> None:
    """One probe timed out, a later one answered. Reporting that hop as "*" loses a
    measurement that was actually taken."""
    hops = parse("14  * 141.101.65.1  168.454 ms\n")
    assert hops[0].host == "141.101.65.1"
    assert hops[0].silent is False
    assert hops[0].rtt_ms == 168.454


def test_a_trailing_star_after_an_answer_is_not_silence() -> None:
    hops = parse("16  1.1.1.1  225.780 ms *\n")
    assert hops[0].host == "1.1.1.1"
    assert hops[0].rtt_ms == 225.780


def test_a_resolved_name_yields_the_address_in_the_parentheses() -> None:
    hops = parse(" 3  core1.mtn.ng (102.89.89.78)  41.0 ms  42.0 ms\n")
    assert hops[0].host == "102.89.89.78"


def test_the_header_line_is_not_mistaken_for_a_hop() -> None:
    hops = parse("traceroute to 1.1.1.1 (1.1.1.1), 16 hops max\n 1  192.168.0.1  4.0 ms\n")
    assert len(hops) == 1
    assert hops[0].number == 1
