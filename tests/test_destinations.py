"""Where the data went: naming a destination, and refusing to name one that cannot be.

The sample text here is `nettop -x` output in the exact shape macOS produces, so the
parser is tested against the format rather than against a convenient version of it.
"""

from __future__ import annotations

from netpulse.analysis.destinations import DestinationMonitor, parse_connections
from netpulse.core.services import describe, identify, identify_host, registered_domain

SAMPLE = """\
,bytes_in,bytes_out,
apsd.136,6644,68350,
tcp4 192.168.0.128:55432<->17.57.146.184:5223,6644,68350,
Google Chrome H.30084,900000,12000,
tcp4 192.168.0.128:55501<->r5---sn-4g5e6nsz.googlevideo.com:443,890000,10000,
tcp4 192.168.0.128:55502<->172.64.155.209:443,10000,2000,
launchd.1,0,0,
tcp4 *:5900<->*:*,,,
"""


def sampler(*outputs: str) -> DestinationMonitor:
    replies = list(outputs)
    return DestinationMonitor(run=lambda _: replies.pop(0) if len(replies) > 1 else replies[0])


# ------------------------------------------------------------------ naming


def test_a_hostname_names_the_service_where_an_address_only_names_the_company() -> None:
    """The same Google addresses serve YouTube, Gmail and Search. Only the name can say
    which, and "which" is the whole question."""
    assert describe("r5---sn-4g5e6nsz.googlevideo.com")[0] == "YouTube"
    assert describe("142.251.150.119")[0] == "Google"


def test_streaming_domains_are_listed_because_that_is_where_the_bytes_are() -> None:
    """Nobody's allowance goes on netflix.com; it goes on nflxvideo.net."""
    assert describe("ipv4-c001.lagos.nflxvideo.net")[0] == "Netflix"
    assert describe("media-lhr.cdninstagram.com")[0] == "Instagram"
    assert describe("e15.whatsapp.net")[0] == "WhatsApp"


def test_a_content_network_says_how_the_traffic_travelled_not_what_it_was_for() -> None:
    """Millions of unrelated sites sit behind one Cloudflare address. Rendering that as
    a service name would put a confident answer next to an unknowable one."""
    label, service = describe("172.64.155.209")
    assert label == "Cloudflare"
    assert service.identifies_a_site is False
    assert describe("d1234.cloudfront.net")[1].identifies_a_site is False
    assert describe("host.googleusercontent.com")[1].identifies_a_site is False


def test_an_unplaceable_endpoint_keeps_its_own_label() -> None:
    """An address the table does not cover is a fact. A guess would not be."""
    # A real public address, off this table — one of the destinations this machine
    # actually reached while the table was being written.
    label, service = describe("165.66.149.34")
    assert label == "165.66.149.34"
    assert service.known is False
    assert describe("box.example.org")[0] == "example.org"


def test_the_ranges_shared_between_two_companies_are_left_out() -> None:
    """52.0.0.0/8 holds both Amazon and Microsoft. The first real address this table
    was tried against fell in that gap and came back with the wrong company on it."""
    assert identify("52.182.143.212").known is False


def test_a_longer_prefix_wins_over_a_broader_one() -> None:
    assert identify("8.8.8.8").name == "Google DNS"
    assert identify("74.125.1.1").name == "Google"


def test_traffic_that_never_left_the_building_is_not_a_service() -> None:
    assert identify("192.168.0.1").name == "Local network"
    assert identify("127.0.0.1").name == "Local network"


def test_a_domain_falls_back_to_its_registrable_part() -> None:
    assert registered_domain("r5---sn-4g5e6nsz.googlevideo.com") == "googlevideo.com"
    assert identify_host("nothing.known.invalid").known is False


# ------------------------------------------------------------------ the parser


def test_each_connection_is_attributed_to_the_process_above_it() -> None:
    """nettop prints a process line then its connections beneath. Losing that pairing
    would put every byte against whichever process happened to be printed first."""
    found = parse_connections(SAMPLE)
    processes = {process for process, _, _, _ in found.values()}
    assert processes == {"apsd", "Google Chrome H"}


def test_a_listening_socket_has_no_far_end_and_is_skipped() -> None:
    for _, remote, _, _ in parse_connections(SAMPLE).values():
        assert "*" not in remote


# ------------------------------------------------------------------ differencing


def test_the_first_sample_reports_nothing() -> None:
    """A connection's counter is cumulative over its life. Reporting it whole on first
    sighting credits this interval with everything it ever carried."""
    assert sampler(SAMPLE).poll() == []


def test_the_second_sample_reports_what_moved_grouped_by_service() -> None:
    later = SAMPLE.replace(
        "tcp4 192.168.0.128:55501<->r5---sn-4g5e6nsz.googlevideo.com:443,890000,10000,",
        "tcp4 192.168.0.128:55501<->r5---sn-4g5e6nsz.googlevideo.com:443,1890000,15000,",
    )
    box = sampler(SAMPLE, later)
    box.poll()
    found = {use.name: use for use in box.poll()}
    assert found["YouTube"].down_bytes == 1_000_000
    assert found["YouTube"].up_bytes == 5_000
    assert found["YouTube"].apps == ("Google Chrome H",)
    assert "Apple" not in found  # it moved nothing between the samples


def test_a_connection_that_closes_is_not_carried_forward() -> None:
    """Its bytes were counted in the interval it moved them. Counting them again when
    it disappears would invent traffic at the moment a download finished."""
    box = sampler(SAMPLE, ",bytes_in,bytes_out,\napsd.136,6644,68350,\n")
    box.poll()
    assert box.poll() == []


def test_an_unavailable_sampler_says_so_rather_than_reporting_no_traffic() -> None:
    """An empty list reads as "nothing happened", which is a measurement. Not being
    able to look is not."""
    assert DestinationMonitor(run=lambda _: "", system="Linux").available is False
