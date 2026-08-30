"""Where this machine's data actually went, by service rather than by address.

The per-application view answers "what was running". It does not answer the question
people actually ask, which is "was that Netflix or was it a backup". A browser is one
application whether it is streaming a film or idle, so a list of process names can show
Chrome at the top of the table every single day and never once explain the bill.

`nettop` reports each connection separately, with the remote address and the bytes over
it, and needs no privileges to do so. Pairing that with the address table in
`core.services` turns a list of numbered endpoints into a list of names.

Two limits, both structural, both stated on the screen rather than buried here. This
covers **this machine only** — the router cannot see inside anyone else's connections
and neither can this. And a service is named only when its address identifies it; a
great deal of the internet sits behind shared content networks where the address
genuinely does not say which site was on the other end, and those rows say so instead
of guessing.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass

from netpulse.core.services import UNKNOWN, Service, describe

Run = Callable[[list[str]], str]

#: `nettop -x` prints a process line, then one line per connection beneath it:
#:
#:     apsd.136,6644,68350,
#:     tcp4 192.168.0.128:55432<->17.57.146.184:5223,6644,68350,
#:
#: IPv4 rows separate the port with a colon, IPv6 rows with a dot, because an IPv6
#: address is full of colons already.
CONNECTION = re.compile(
    r"^(?P<protocol>tcp|udp)(?P<family>[46])\s+"
    r"(?P<local>\S+?)<->(?P<remote>\S+?),(?P<down>\d+),(?P<up>\d+),?\s*$"
)
PROCESS = re.compile(r"^(?P<process>.+)\.(?P<pid>\d+),(?P<down>\d+),(?P<up>\d+),?\s*$")


def _address(endpoint: str, family: str) -> str:
    """The address half of `host:port`, or "" when the socket is only listening."""
    if "*" in endpoint:
        return ""
    host = endpoint.rsplit(":", 1)[0] if family == "4" else endpoint.rsplit(".", 1)[0]
    return host.strip("[]")


def parse_connections(output: str) -> dict[str, tuple[str, str, float, float]]:
    """{connection: (process, remote address, down, up)}.

    Keyed on the whole connection string because that is what stays stable while a
    connection lives: the same pair of endpoints is the same TCP session, and a new one
    starts at zero rather than continuing the old count.
    """
    found: dict[str, tuple[str, str, float, float]] = {}
    process = ""
    for line in output.splitlines():
        stripped = line.strip()
        owner = PROCESS.match(stripped)
        if owner:
            process = owner.group("process").strip()
            continue
        row = CONNECTION.match(stripped)
        if not row:
            continue
        remote = _address(row.group("remote"), row.group("family"))
        if not remote:
            continue  # a listening socket has no far end and no traffic
        key = f"{process}|{row.group('local')}|{row.group('remote')}"
        found[key] = (process, remote, float(row.group("down")), float(row.group("up")))
    return found


@dataclass(frozen=True)
class ServiceUsage:
    """One named destination's traffic over the interval just measured."""

    name: str
    service: Service
    down_bytes: float
    up_bytes: float
    #: The applications that talked to it, busiest first — "Netflix, via Chrome".
    apps: tuple[str, ...] = ()

    @property
    def total(self) -> float:
        return self.down_bytes + self.up_bytes

    @property
    def identified(self) -> bool:
        """Whether this row names what somebody was doing, or only how it travelled."""
        return self.service.identifies_a_site


def _run(command: list[str]) -> str:
    return subprocess.run(
        command, capture_output=True, text=True, timeout=15, check=False
    ).stdout


class DestinationMonitor:
    """Per-service usage for this machine, differenced between samples.

    Holds the previous per-connection counters. A connection that closes simply stops
    appearing, and its bytes are not carried forward — the interval it was open for was
    already counted when it happened.
    """

    def __init__(self, run: Run = _run, system: str = "Darwin") -> None:
        self._run = run
        self._system = system
        self._previous: dict[str, tuple[str, str, float, float]] = {}

    @property
    def available(self) -> bool:
        # nettop is the only tool here that reports per-connection byte counters without
        # privileges. Linux's `ss` carries them too and can be added; until it is, this
        # says no rather than reporting an empty list that reads as "no traffic".
        return self._system == "Darwin"

    def _sample(self) -> dict[str, tuple[str, str, float, float]]:
        try:
            return parse_connections(
                self._run(
                    # -x for raw byte counts, -t external so loopback traffic between
                    # local processes is not counted as data that left the machine.
                    ["nettop", "-x", "-L", "1", "-t", "external", "-J", "bytes_in,bytes_out"]
                )
            )
        except (OSError, subprocess.SubprocessError, ValueError):
            return {}

    def poll(self) -> list[ServiceUsage]:
        """Usage since the last call, busiest first.

        Empty on the first call. A connection's counter is cumulative over its life, and
        reporting it whole on first sighting would credit this interval with everything
        a long-running connection had ever carried.
        """
        current = self._sample()
        if not current:
            return []

        totals: dict[str, list[float]] = {}
        talkers: dict[str, dict[str, float]] = {}
        services: dict[str, Service] = {}
        for key, (process, remote, down, up) in current.items():
            was = self._previous.get(key)
            if was is None:
                continue
            moved_down = max(0.0, down - was[2])
            moved_up = max(0.0, up - was[3])
            if not moved_down and not moved_up:
                continue
            # An endpoint the tables do not cover keeps its own label — its domain if it
            # has one, its address otherwise. Both are facts a caller can look up; a
            # guess would be neither.
            name, service = describe(remote)
            services.setdefault(name, service)
            row = totals.setdefault(name, [0.0, 0.0])
            row[0] += moved_down
            row[1] += moved_up
            if process:
                by_app = talkers.setdefault(name, {})
                by_app[process] = by_app.get(process, 0.0) + moved_down + moved_up
        self._previous = current

        found = [
            ServiceUsage(
                name=name,
                service=services.get(name, UNKNOWN),
                down_bytes=down,
                up_bytes=up,
                apps=tuple(
                    app
                    for app, _ in sorted(
                        talkers.get(name, {}).items(), key=lambda pair: -pair[1]
                    )
                ),
            )
            for name, (down, up) in totals.items()
        ]
        found.sort(key=lambda usage: -usage.total)
        return found
