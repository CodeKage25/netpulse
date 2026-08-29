"""Where the problem is: your equipment, your carrier, or the far end.

This is the question every connection complaint is really about, and the one a
dish-shaped monitor cannot answer — Dishylink can tell you the link is bad, never
whose fault it is. NetPulse traces the path and reads the answer off the hop where
latency or loss first appears.

The reasoning is deliberately conservative. A traceroute is a noisy instrument: routers
deprioritise the ICMP replies that make hops visible, so a middle hop reporting 400 ms
while everything past it reports 40 ms has not found a problem — it has found a busy
control plane. **Only a rise that persists to the end is a real rise**, which is the
single rule that keeps this from blaming an innocent router every time.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass

#: Traceroute is slow by nature; the cap keeps a UI request bounded.
MAX_HOPS = 16
TIMEOUT_S = 25.0

#: A jump smaller than this is ordinary path variation, not a finding.
SIGNIFICANT_MS = 40.0
#: …and it has to be a real proportion of the total, not 40ms on top of 900.
SIGNIFICANT_RATIO = 0.25

Run = Callable[[list[str]], str]

#: RFC1918 plus the carrier-grade NAT range. A private address one hop past your own
#: router is the carrier's internal network — the one segment a traceroute can attribute
#: with certainty, without asking anybody who owns an address.
INTERNAL_PREFIXES = ("10.", "192.168.", "100.64.", "100.65.", "127.")


def is_internal(host: str) -> bool:
    if host.startswith(INTERNAL_PREFIXES):
        return True
    if host.startswith("172."):
        try:  # 172.16.0.0/12 is private; 172.15 and 172.32 are not
            return 16 <= int(host.split(".")[1]) <= 31
        except (IndexError, ValueError):
            return False
    return False


def _run(command: list[str]) -> str:
    result = subprocess.run(command, capture_output=True, text=True, timeout=TIMEOUT_S, check=False)
    return result.stdout + result.stderr


@dataclass(frozen=True)
class Hop:
    number: int
    host: str
    #: Median of the probes that answered; None when the hop stayed silent.
    rtt_ms: float | None
    #: True when nothing came back. Silence is not loss — see `analyse`.
    silent: bool


#: A numbered hop line: "1  192.168.0.1  2.1 ms  1.9 ms", possibly with a leading "*"
#: where one probe timed out and a later one answered.
HOP_LINE = re.compile(r"^\s*(\d+)\s+(.*)$")
#: A continuation line, no hop number: the probes for one hop came back from different
#: addresses, so traceroute prints the rest indented. Dropping these would compute the
#: median from fewer probes than were actually sent.
CONTINUATION = re.compile(r"^\s+(?!\d+\s)(\S.*)$")
TIMES = re.compile(r"([\d.]+)\s*ms")
#: An address, either bare or in parentheses after a resolved name.
ADDRESS = re.compile(r"\((\d{1,3}(?:\.\d{1,3}){3}|[0-9a-fA-F:]{3,})\)|(\d{1,3}(?:\.\d{1,3}){3})")


def _address(text: str) -> str:
    match = ADDRESS.search(text)
    return (match.group(1) or match.group(2)) if match else ""


def _median(times: list[float]) -> float | None:
    return sorted(times)[len(times) // 2] if times else None


def parse(output: str) -> list[Hop]:
    hops: list[Hop] = []
    times: list[float] = []
    number = 0
    host = ""

    def flush() -> None:
        if number:
            hops.append(
                Hop(
                    number=number,
                    host=host or "*",
                    rtt_ms=_median(times),
                    silent=not times,
                )
            )

    for line in output.splitlines():
        numbered = HOP_LINE.match(line)
        if numbered:
            flush()
            number, rest = int(numbered.group(1)), numbered.group(2)
            times = [float(value) for value in TIMES.findall(rest)]
            host = _address(rest)
            continue
        carried = CONTINUATION.match(line) if number else None
        if carried:
            # Same hop, another address. Its times count toward the same median, and
            # the first address seen is the one named.
            times += [float(value) for value in TIMES.findall(carried.group(1))]
            host = host or _address(carried.group(1))
    flush()
    return hops


def trace(host: str = "1.1.1.1", run: Run = _run, max_hops: int = MAX_HOPS) -> list[Hop]:
    """Trace the path, or return nothing if the platform cannot.

    Nothing is inferred when the tool is missing: an empty path says "not measured",
    and the caller must not read that as "no hops".
    """
    binary = shutil.which("traceroute") or shutil.which("tracert")
    if not binary:
        return []
    # -n skips reverse DNS, which otherwise dominates the runtime and can hang; -q 2
    # sends two probes per hop instead of three, halving the cost for a median that is
    # about as good.
    flags = ["-n", "-q", "2", "-w", "2", "-m", str(max_hops)]
    if binary.endswith("tracert"):
        flags = ["-d", "-w", "2000", "-h", str(max_hops)]
    try:
        return parse(run([binary, *flags, host]))
    except (OSError, subprocess.SubprocessError):
        return []


@dataclass(frozen=True)
class PathVerdict:
    #: "local" | "carrier" | "upstream" | "clear" | "unknown"
    where: str
    summary: str
    detail: str
    hops: list[Hop]
    #: The hop the rise is attributed to, when there is one.
    culprit: Hop | None = None


def _last_answering(hops: list[Hop]) -> Hop | None:
    return next((hop for hop in reversed(hops) if hop.rtt_ms is not None), None)


def analyse(hops: list[Hop], local_hops: int = 1) -> PathVerdict:
    """Attribute a latency rise to the segment it first appears in — and holds through.

    `local_hops` is how many leading hops are your own equipment. On a carrier CPE that
    is one: the router. Behind a mesh or a double-NAT setup it is more, and getting it
    wrong is the difference between blaming your own wifi and blaming MTN.
    Attribution stops where certainty does. A private address past your router is
    inside the carrier's network and can be named as theirs. A public one might be
    their transit, a peering link, or the far end — and telling those apart needs a
    lookup of who owns the address, which would mean sending your path to a third
    party. Under-claiming is the cheaper mistake, so it says so instead.
    """
    if not hops:
        return PathVerdict("unknown", "Path not measured", "traceroute is unavailable here.", [])

    answering = [hop for hop in hops if hop.rtt_ms is not None]
    if len(answering) < 2:
        return PathVerdict(
            "unknown",
            "Path could not be measured",
            "Too few hops answered to tell where the delay is. Many routers refuse the "
            "replies traceroute needs, which is normal and is not itself a fault.",
            hops,
        )

    final = _last_answering(hops)
    assert final is not None and final.rtt_ms is not None
    total = final.rtt_ms

    worst_jump = 0.0
    culprit: Hop | None = None
    previous = 0.0
    for hop in answering:
        assert hop.rtt_ms is not None
        jump = hop.rtt_ms - previous
        # Only a rise that survives to the end is real. A middle hop reporting 400ms
        # while everything past it reports 40ms found a busy control plane, not a
        # problem — its own replies are deprioritised, the traffic through it is not.
        sustained = total - previous >= jump * 0.6
        if jump > worst_jump and sustained:
            worst_jump, culprit = jump, hop
        previous = hop.rtt_ms

    significant = worst_jump >= SIGNIFICANT_MS and worst_jump >= total * SIGNIFICANT_RATIO
    if not significant or culprit is None:
        return PathVerdict(
            "clear",
            f"No single hop is responsible ({total:.0f}ms end to end)",
            "Latency builds gradually along the path, which is what distance looks "
            "like. There is no one link to blame.",
            hops,
            None,
        )

    position = answering.index(culprit)
    if position < local_hops:
        return PathVerdict(
            "local",
            f"The delay starts at your own equipment (+{worst_jump:.0f}ms)",
            f"Hop {culprit.number} ({culprit.host}) is inside your network. Wi-Fi, a "
            "powerline adapter or an overloaded router will do this; a wired test to "
            "the same target tells you which.",
            hops,
            culprit,
        )
    if is_internal(culprit.host):
        return PathVerdict(
            "carrier",
            f"The delay starts on your provider's network (+{worst_jump:.0f}ms)",
            f"Hop {culprit.number} ({culprit.host}) is a private address past your "
            "router, which means it is inside your carrier's own network — not a "
            "peering link and not the far end. This is the hop worth quoting to them, "
            "with the CSV export attached.",
            hops,
            culprit,
        )
    return PathVerdict(
        "beyond",
        f"The delay starts past your carrier's core (+{worst_jump:.0f}ms)",
        f"Hop {culprit.number} ({culprit.host}) is on public address space, so this is "
        "your carrier's transit, a peering link, or the destination — and a traceroute "
        "alone cannot tell those apart without looking up who owns the address, which "
        "NetPulse will not do because it would mean sending your path to someone else. "
        "Testing a different destination separates them: if only one target is slow, "
        "it is that target.",
        hops,
        culprit,
    )
