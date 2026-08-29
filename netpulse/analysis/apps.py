"""Which applications are using the connection — on the machine NetPulse runs on.

This is the honest scope, and it is worth being blunt about why. A router sees IP
flows, not applications; attributing bytes to an app from the router's side would mean
inspecting traffic, which NetPulse does not do and should not. What *is* knowable is
what the operating system on this machine already accounts for per process — so this
answers "what is using my connection here", not "what is using it on every device".

The counters are cumulative per process, so usage is differenced between polls with the
same care the router counters get: a process that restarts gets a new PID and a fresh
counter, and treating that as a delta would bill it for everything the previous one
ever did.
"""

from __future__ import annotations

import platform
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass

TIMEOUT_S = 6.0

#: Processes that carry the machine's own housekeeping rather than anything a person
#: chose to run. Shown separately rather than hidden — mDNSResponder can genuinely be
#: the top talker on a noisy network, and that is worth seeing.
SYSTEM_PROCESSES = frozenset(
    {
        "mDNSResponder",
        "netbiosd",
        "apsd",
        "syslogd",
        "configd",
        "launchd",
        "systemstats",
        "nsurlsessiond",
        "trustd",
        "timed",
        "rapportd",
        "sharingd",
        "identityservicesd",
        "cloudd",
        "bird",
        "AirPlayXPCHelper",
        "symptomsd",
    }
)

Run = Callable[[list[str]], str]


def _run(command: list[str]) -> str:
    result = subprocess.run(command, capture_output=True, text=True, timeout=TIMEOUT_S, check=False)
    return result.stdout


@dataclass(frozen=True)
class AppUsage:
    name: str
    #: Bytes since the last sample, or None on the first sighting of this process.
    down_bytes: float | None
    up_bytes: float | None
    #: Cumulative counters, kept so a caller can re-derive without trusting our delta.
    down_total: float
    up_total: float
    system: bool

    @property
    def total(self) -> float:
        return (self.down_bytes or 0.0) + (self.up_bytes or 0.0)


#: `nettop` prints "name.pid,bytes_in,bytes_out," per line. The name may itself contain
#: dots and spaces ("Google Chrome H.30084"), so the pid is split off from the right.
NETTOP_LINE = re.compile(r"^(?P<process>.+)\.(?P<pid>\d+),(?P<down>\d+),(?P<up>\d+),?\s*$")


def parse_nettop(output: str) -> dict[tuple[str, int], tuple[str, float, float]]:
    """{(name, pid): (name, down, up)} — keyed on the pid so a restart is visible."""
    found: dict[tuple[str, int], tuple[str, float, float]] = {}
    for line in output.splitlines():
        match = NETTOP_LINE.match(line.strip())
        if not match:
            continue
        name = match.group("process").strip()
        pid = int(match.group("pid"))
        found[(name, pid)] = (name, float(match.group("down")), float(match.group("up")))
    return found


#: Linux: `ss -tuni` prints a socket line then an indented info line carrying
#: `bytes_sent:N` and `bytes_received:N`, with the owning process on the socket line.
SS_PROCESS = re.compile(r'users:\(\("(?P<process>[^"]+)",pid=(?P<pid>\d+)')
SS_SENT = re.compile(r"bytes_sent:(\d+)")
SS_RECEIVED = re.compile(r"bytes_received:(\d+)")


def parse_ss(output: str) -> dict[tuple[str, int], tuple[str, float, float]]:
    """Sockets are summed per process — one app holds many, and each is a fragment."""
    found: dict[tuple[str, int], tuple[str, float, float]] = {}
    current: tuple[str, int] | None = None
    for line in output.splitlines():
        owner = SS_PROCESS.search(line)
        if owner:
            current = (owner.group("process"), int(owner.group("pid")))
            found.setdefault(current, (current[0], 0.0, 0.0))
            continue
        if current is None:
            continue
        sent, received = SS_SENT.search(line), SS_RECEIVED.search(line)
        if sent or received:
            name, down, up = found[current]
            found[current] = (
                name,
                down + (float(received.group(1)) if received else 0.0),
                up + (float(sent.group(1)) if sent else 0.0),
            )
    return found


class AppMonitor:
    """Per-process usage, differenced between samples.

    Holds the previous counters so it can report what moved. A process that exits and
    a process that starts are both visible because the key carries the pid — a name
    alone would silently roll a restarted app's fresh counter into the old total.
    """

    def __init__(self, run: Run = _run, system: str = "") -> None:
        self._run = run
        self._system = system or platform.system()
        self._previous: dict[tuple[str, int], tuple[str, float, float]] = {}

    @property
    def available(self) -> bool:
        return self._system in ("Darwin", "Linux")

    def _sample(self) -> dict[tuple[str, int], tuple[str, float, float]]:
        try:
            if self._system == "Darwin":
                # -t external excludes loopback, so this is traffic that really left
                # the machine rather than one process talking to another.
                return parse_nettop(
                    self._run(
                        [
                            "nettop",
                            "-P",
                            "-x",
                            "-L",
                            "1",
                            "-t",
                            "external",
                            "-J",
                            "bytes_in,bytes_out",
                        ]
                    )
                )
            if self._system == "Linux":
                return parse_ss(self._run(["ss", "-tuni"]))
        except (OSError, subprocess.SubprocessError, ValueError):
            return {}
        return {}

    def poll(self) -> list[AppUsage]:
        """Usage since the last call, busiest first."""
        current = self._sample()
        if not current:
            return []

        by_name: dict[str, list[float]] = {}
        for key, (name, down, up) in current.items():
            was = self._previous.get(key)
            # A first sighting has no delta. Reporting the cumulative counter as though
            # it were this interval's traffic would credit an app that has been running
            # for a week with all of it, the moment NetPulse starts.
            delta_down = max(0.0, down - was[1]) if was else None
            delta_up = max(0.0, up - was[2]) if was else None
            totals = by_name.setdefault(name, [0.0, 0.0, 0.0, 0.0, 0.0])
            if delta_down is not None:
                totals[0] += delta_down
                totals[4] = 1.0  # at least one process of this name had a delta
            if delta_up is not None:
                totals[1] += delta_up
            totals[2] += down
            totals[3] += up
        self._previous = current

        found = [
            AppUsage(
                name=name,
                down_bytes=totals[0] if totals[4] else None,
                up_bytes=totals[1] if totals[4] else None,
                down_total=totals[2],
                up_total=totals[3],
                system=name in SYSTEM_PROCESSES,
            )
            for name, totals in by_name.items()
        ]
        return sorted(found, key=lambda app: (app.total, app.down_total), reverse=True)
