"""Which service an address belongs to, when the address is willing to say.

"Where did the data go" is the question every usage screen is really asked, and a list
of IP addresses does not answer it. This turns an address into a name.

It does so from a table of published network allocations rather than by asking anything
at lookup time. Reverse DNS was tried against this machine's real destinations first and
returned nothing usable: Google, Apple and Cloudflare addresses have no PTR record at
all, and the router's resolver times out on the queries that remain. A table needs no
network, cannot be slow, and cannot leak which addresses are being looked up.

The table's honesty rests on one distinction. Some addresses identify a **service** —
17.x is Apple's and nobody else's. Others identify a **content network** shared by
millions of sites; a Cloudflare address says the traffic went through Cloudflare and
says nothing whatever about which site it was for. Collapsing those two into one label
would produce a screen that names a service for every row and is right about only some
of them, with no way to tell which. So a shared network is labelled as one, and the
screen can say plainly that the site behind it is not knowable from the address.

Anything not in the table stays an address. An unrecognised destination is not a
mystery to be guessed at — it is simply a place this table does not cover, and saying
so costs nothing next to naming the wrong company.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from functools import lru_cache

#: What a name means. `service` is the operator of the thing you were actually using;
#: `network` is shared infrastructure that many services sit behind; `cloud` is rented
#: compute, which is the same problem one layer down.
SERVICE = "service"
NETWORK = "network"
CLOUD = "cloud"


@dataclass(frozen=True, slots=True)
class Service:
    name: str
    kind: str

    @property
    def known(self) -> bool:
        return self.kind != ""

    @property
    def identifies_a_site(self) -> bool:
        """Whether naming this tells you what somebody was doing.

        False for content networks and rented compute, where the honest caption is
        "through Cloudflare", not "on Cloudflare" — and certainly not a guess at which
        of the millions of sites behind it was involved.
        """
        return self.kind == SERVICE


UNKNOWN = Service("", "")

#: Published allocations, narrowest first is not required — the lookup takes the longest
#: match, so a specific range inside a broad one wins on its own.
#:
#: Kept deliberately short. Every row here is an allocation stable enough to still be
#: true in a year; anything faster-moving belongs to a list that can be refreshed, not
#: compiled in. A wrong name is worse than an address, so a range that is merely
#: probable is left out.
_TABLE: tuple[tuple[str, str, str], ...] = (
    # Apple's /8 — one of the oldest and least ambiguous allocations in the registry.
    ("17.0.0.0/8", "Apple", SERVICE),
    # Google, which is also YouTube, Gmail, Search, Android and Play.
    ("8.8.4.0/24", "Google DNS", SERVICE),
    ("8.8.8.0/24", "Google DNS", SERVICE),
    ("64.233.160.0/19", "Google", SERVICE),
    ("66.102.0.0/20", "Google", SERVICE),
    ("72.14.192.0/18", "Google", SERVICE),
    ("74.125.0.0/16", "Google", SERVICE),
    ("108.177.0.0/17", "Google", SERVICE),
    ("142.250.0.0/15", "Google", SERVICE),
    ("172.217.0.0/16", "Google", SERVICE),
    ("172.253.0.0/16", "Google", SERVICE),
    ("173.194.0.0/16", "Google", SERVICE),
    ("209.85.128.0/17", "Google", SERVICE),
    ("216.58.192.0/19", "Google", SERVICE),
    ("216.239.32.0/19", "Google", SERVICE),
    ("2001:4860::/32", "Google", SERVICE),
    # Meta — Facebook, Instagram, WhatsApp and Messenger share this space.
    ("31.13.24.0/21", "Meta", SERVICE),
    ("31.13.64.0/18", "Meta", SERVICE),
    ("66.220.144.0/20", "Meta", SERVICE),
    ("69.63.176.0/20", "Meta", SERVICE),
    ("69.171.224.0/19", "Meta", SERVICE),
    ("129.134.0.0/16", "Meta", SERVICE),
    ("157.240.0.0/16", "Meta", SERVICE),
    ("173.252.64.0/18", "Meta", SERVICE),
    ("179.60.192.0/22", "Meta", SERVICE),
    ("185.60.216.0/22", "Meta", SERVICE),
    ("2a03:2880::/32", "Meta", SERVICE),
    # Netflix runs its own network, which is what makes it nameable at all — most
    # streaming rides on a CDN and cannot be told apart from anything else there.
    ("23.246.0.0/18", "Netflix", SERVICE),
    ("37.77.184.0/21", "Netflix", SERVICE),
    ("45.57.0.0/17", "Netflix", SERVICE),
    ("64.120.128.0/17", "Netflix", SERVICE),
    ("66.197.128.0/17", "Netflix", SERVICE),
    ("108.175.32.0/20", "Netflix", SERVICE),
    ("185.2.220.0/22", "Netflix", SERVICE),
    ("185.9.188.0/22", "Netflix", SERVICE),
    ("192.173.64.0/18", "Netflix", SERVICE),
    ("198.38.96.0/19", "Netflix", SERVICE),
    ("198.45.48.0/20", "Netflix", SERVICE),
    ("208.75.76.0/22", "Netflix", SERVICE),
    ("2a00:86c0::/32", "Netflix", SERVICE),
    # Microsoft: Windows Update, Office, Teams, Xbox, Bing.
    ("13.107.0.0/16", "Microsoft", SERVICE),
    ("40.64.0.0/10", "Microsoft", SERVICE),
    ("52.96.0.0/12", "Microsoft", SERVICE),
    ("150.171.0.0/16", "Microsoft", SERVICE),
    ("204.79.195.0/24", "Microsoft", SERVICE),
    ("20.0.0.0/8", "Microsoft Azure", CLOUD),
    # X, GitHub, Anthropic — small, stable, and likely to show up on a developer's link.
    ("104.244.40.0/21", "X", SERVICE),
    ("199.16.156.0/22", "X", SERVICE),
    ("140.82.112.0/20", "GitHub", SERVICE),
    ("185.199.108.0/22", "GitHub", SERVICE),
    ("192.30.252.0/22", "GitHub", SERVICE),
    ("160.79.104.0/23", "Anthropic", SERVICE),
    # Content networks. Naming one of these says how the traffic travelled, never what
    # it was for — millions of unrelated sites sit behind each.
    ("104.16.0.0/12", "Cloudflare", NETWORK),
    ("172.64.0.0/13", "Cloudflare", NETWORK),
    ("162.158.0.0/15", "Cloudflare", NETWORK),
    ("173.245.48.0/20", "Cloudflare", NETWORK),
    ("108.162.192.0/18", "Cloudflare", NETWORK),
    ("141.101.64.0/18", "Cloudflare", NETWORK),
    ("188.114.96.0/20", "Cloudflare", NETWORK),
    ("190.93.240.0/20", "Cloudflare", NETWORK),
    ("197.234.240.0/22", "Cloudflare", NETWORK),
    ("198.41.128.0/17", "Cloudflare", NETWORK),
    ("131.0.72.0/22", "Cloudflare", NETWORK),
    ("1.1.1.0/24", "Cloudflare DNS", SERVICE),
    ("2606:4700::/32", "Cloudflare", NETWORK),
    ("2.16.0.0/13", "Akamai", NETWORK),
    ("23.32.0.0/11", "Akamai", NETWORK),
    ("23.192.0.0/11", "Akamai", NETWORK),
    ("95.100.0.0/15", "Akamai", NETWORK),
    ("104.64.0.0/10", "Akamai", NETWORK),
    ("184.24.0.0/13", "Akamai", NETWORK),
    ("146.75.0.0/16", "Fastly", NETWORK),
    ("151.101.0.0/16", "Fastly", NETWORK),
    ("199.232.0.0/16", "Fastly", NETWORK),
    ("13.32.0.0/15", "Amazon CloudFront", NETWORK),
    ("13.35.0.0/16", "Amazon CloudFront", NETWORK),
    ("52.84.0.0/15", "Amazon CloudFront", NETWORK),
    ("54.192.0.0/16", "Amazon CloudFront", NETWORK),
    ("54.230.0.0/16", "Amazon CloudFront", NETWORK),
    ("99.84.0.0/16", "Amazon CloudFront", NETWORK),
    ("143.204.0.0/16", "Amazon CloudFront", NETWORK),
    # Rented compute. A name here tells you where a service is hosted and nothing about
    # which service it is — the same caveat as a content network, one layer down.
    ("3.0.0.0/8", "Amazon AWS", CLOUD),
    ("34.64.0.0/10", "Google Cloud", CLOUD),
    ("35.184.0.0/13", "Google Cloud", CLOUD),
    # 52.0.0.0/8 and 54.0.0.0/8 are deliberately absent. Both are split between Amazon
    # and other holders — 52.182 is Microsoft's, not Amazon's — and the first live
    # address this table was tested against fell in exactly that gap and came back with
    # the wrong company's name on it. An address nobody can place is a smaller error
    # than a confident wrong answer, so those two ranges stay out.
)


#: Domain suffixes, which are a far better signal than addresses when one is available.
#: An address tells you which company's network a packet crossed; a name tells you what
#: was on the other end, and it is the difference between "Google" and "YouTube" — the
#: same addresses serve both, so no address table can ever separate them.
#:
#: Matched on suffix, longest first. Streaming and media domains are listed alongside
#: the front door because they are where the bytes actually are: nobody's data goes on
#: netflix.com, it goes on nflxvideo.net.
_DOMAINS: tuple[tuple[str, str, str], ...] = (
    ("googlevideo.com", "YouTube", SERVICE),
    ("youtube.com", "YouTube", SERVICE),
    ("youtu.be", "YouTube", SERVICE),
    ("ytimg.com", "YouTube", SERVICE),
    ("nflxvideo.net", "Netflix", SERVICE),
    ("nflximg.net", "Netflix", SERVICE),
    ("nflxso.net", "Netflix", SERVICE),
    ("netflix.com", "Netflix", SERVICE),
    ("whatsapp.net", "WhatsApp", SERVICE),
    ("whatsapp.com", "WhatsApp", SERVICE),
    ("cdninstagram.com", "Instagram", SERVICE),
    ("instagram.com", "Instagram", SERVICE),
    ("fbcdn.net", "Meta", SERVICE),
    ("facebook.com", "Facebook", SERVICE),
    ("tiktokcdn.com", "TikTok", SERVICE),
    ("tiktokv.com", "TikTok", SERVICE),
    ("byteoversea.com", "TikTok", SERVICE),
    ("scdn.co", "Spotify", SERVICE),
    ("spotifycdn.com", "Spotify", SERVICE),
    ("spotify.com", "Spotify", SERVICE),
    ("twimg.com", "X", SERVICE),
    ("twitter.com", "X", SERVICE),
    ("x.com", "X", SERVICE),
    ("zoom.us", "Zoom", SERVICE),
    ("mzstatic.com", "Apple", SERVICE),
    ("aaplimg.com", "Apple", SERVICE),
    ("icloud.com", "Apple", SERVICE),
    ("apple.com", "Apple", SERVICE),
    ("windowsupdate.com", "Windows Update", SERVICE),
    ("office.com", "Microsoft", SERVICE),
    ("microsoft.com", "Microsoft", SERVICE),
    ("gstatic.com", "Google", SERVICE),
    ("googleapis.com", "Google", SERVICE),
    ("ggpht.com", "Google", SERVICE),
    ("google.com", "Google", SERVICE),
    ("anthropic.com", "Anthropic", SERVICE),
    ("claude.ai", "Anthropic", SERVICE),
    ("githubusercontent.com", "GitHub", SERVICE),
    ("github.com", "GitHub", SERVICE),
    ("steamcontent.com", "Steam", SERVICE),
    ("steampowered.com", "Steam", SERVICE),
    # Shared infrastructure. The name is real and the caveat travels with it.
    ("cloudfront.net", "Amazon CloudFront", NETWORK),
    ("akamaized.net", "Akamai", NETWORK),
    ("akamaiedge.net", "Akamai", NETWORK),
    ("akamai.net", "Akamai", NETWORK),
    ("fastly.net", "Fastly", NETWORK),
    ("cloudflare.net", "Cloudflare", NETWORK),
    ("googleusercontent.com", "Google Cloud", CLOUD),
    ("amazonaws.com", "Amazon AWS", CLOUD),
    ("azure.com", "Microsoft Azure", CLOUD),
    ("azureedge.net", "Microsoft Azure", CLOUD),
)

_SUFFIXES = sorted(
    ((suffix, Service(name, kind)) for suffix, name, kind in _DOMAINS),
    key=lambda row: -len(row[0]),
)


def registered_domain(host: str) -> str:
    """The last two labels of a hostname — "bc.googleusercontent.com" -> the domain.

    An approximation: it reads `co.uk` as a domain when it is a suffix. That is
    acceptable here because the result is only ever shown as a fallback label, never
    used to decide that two hosts belong to the same company.
    """
    labels = host.strip(".").split(".")
    return ".".join(labels[-2:]) if len(labels) >= 2 else host


def identify_host(host: str) -> Service:
    """The service behind a hostname, by domain suffix."""
    lowered = host.strip(".").lower()
    for suffix, service in _SUFFIXES:
        if lowered == suffix or lowered.endswith("." + suffix):
            return service
    return UNKNOWN


def describe(endpoint: str) -> tuple[str, Service]:
    """A remote endpoint as (label, service), whether it arrived as a name or a number.

    Names win when there is one: the same Google addresses serve YouTube, Gmail and
    Search, so an address can only ever say "Google" where the name says which of them.
    An endpoint nobody can place keeps its own label — the domain if it has one, the
    address otherwise — because both are facts, and a guess would be neither.
    """
    try:
        ipaddress.ip_address(endpoint)
    except ValueError:
        service = identify_host(endpoint)
        return (service.name or registered_domain(endpoint)), service
    service = identify(endpoint)
    return (service.name or endpoint), service


def _compiled() -> list[tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, Service]]:
    built = [
        (ipaddress.ip_network(network), Service(name, kind)) for network, name, kind in _TABLE
    ]
    # Longest prefix first, so a specific allocation inside a broad one wins.
    built.sort(key=lambda row: row[0].prefixlen, reverse=True)
    return built


_NETWORKS = _compiled()


@lru_cache(maxsize=4096)
def identify(address: str) -> Service:
    """The service that holds this address, or UNKNOWN.

    Cached because a handful of addresses account for nearly every connection a machine
    makes, and the same ones are looked up on every poll forever.
    """
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        return UNKNOWN
    if parsed.is_private or parsed.is_loopback or parsed.is_link_local:
        # A destination on this network is not a service anybody rents. Naming it would
        # be inventing an outside party for traffic that never left the building.
        return Service("Local network", SERVICE)
    for network, service in _NETWORKS:
        if parsed in network:
            return service
    return UNKNOWN
