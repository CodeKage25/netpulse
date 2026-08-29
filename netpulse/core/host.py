"""What this machine is, on its own network.

NetPulse runs on one of the devices it is watching, which makes that device the one it
can measure honestly and completely. Knowing its own addresses is what lets a row in
the device list stop being a name and a lease and start carrying real usage — and what
keeps the claim narrow, because it is a statement about this machine and no other.
"""

from __future__ import annotations

import platform
import re
import socket
import subprocess
from collections.abc import Callable

TIMEOUT_S = 4.0

Run = Callable[[list[str]], str]


def _run(command: list[str]) -> str:
    result = subprocess.run(command, capture_output=True, text=True, timeout=TIMEOUT_S, check=False)
    return result.stdout


#: A MAC in any of the spellings the platform tools use.
MAC_PATTERN = re.compile(r"\b([0-9a-fA-F]{2}(?::[0-9a-fA-F]{2}){5})\b")


def local_macs(run: Run = _run, system: str = "") -> set[str]:
    """Every hardware address this machine owns, uppercased.

    Used only to recognise ourselves in a router's device list. A machine has several —
    wifi, ethernet, and a pile of virtual ones — and matching any of them is correct,
    because the router will have leased to exactly one.
    """
    kind = system or platform.system()
    command = ["ifconfig"] if kind == "Darwin" else ["ip", "link", "show"]
    try:
        output = run(command)
    except (OSError, subprocess.SubprocessError):
        return set()
    found = {mac.upper() for mac in MAC_PATTERN.findall(output)}
    # 00:00:00:00:00:00 appears for interfaces with no hardware address; matching it
    # would claim every device whose MAC the router failed to report.
    return {mac for mac in found if mac != "00:00:00:00:00:00"}


def local_hostname() -> str:
    return socket.gethostname().split(".")[0]
