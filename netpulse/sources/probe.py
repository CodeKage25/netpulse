"""The universal adapter: what the connection feels like, measured from this machine.

Works on any network with zero configuration, which is the point — an MTN MiFi, Starlink,
hotel WiFi, an office VPN. Everything here is unprivileged (TCP handshakes and one UDP DNS
query; ICMP needs root and lies behind CGNAT anyway) and cheap enough to run every cycle.

Separating gateway latency from internet latency is the whole diagnostic: gateway fine +
internet bad means the ISP; gateway bad means your WiFi or router.
"""

from __future__ import annotations

import platform
import re
import socket
import struct
import subprocess
import time
from collections.abc import Callable
from contextlib import suppress
from statistics import pstdev

from netpulse.core.model import Reading
from netpulse.sources import AdapterError

#: Anycast targets answered close to everyone, including African POPs.
INTERNET_TARGETS = (("1.1.1.1", 443), ("8.8.8.8", 443))
DNS_TARGETS = ("1.1.1.1", "8.8.8.8")
ATTEMPTS = 4


def tcp_ms(host: str, port: int, timeout: float = 2.0) -> float:
    started = time.perf_counter()
    with socket.create_connection((host, port), timeout=timeout):
        return (time.perf_counter() - started) * 1000


def dns_ms(resolver: str, hostname: str = "example.com", timeout: float = 2.0) -> float:
    """One raw A query over UDP, so the timing is the resolver's and not a local cache's."""
    header = struct.pack(">HHHHHH", 0x4E50, 0x0100, 1, 0, 0, 0)
    question = (
        b"".join(bytes([len(part)]) + part.encode() for part in hostname.split("."))
        + b"\x00"
        + struct.pack(">HH", 1, 1)
    )
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(timeout)
        started = time.perf_counter()
        sock.sendto(header + question, (resolver, 53))
        sock.recvfrom(512)
        return (time.perf_counter() - started) * 1000


def default_gateway() -> str | None:
    try:
        if platform.system() == "Darwin":
            out = subprocess.run(
                ["route", "-n", "get", "default"], capture_output=True, text=True, timeout=3
            ).stdout
            match = re.search(r"gateway:\s+(\S+)", out)
        else:
            out = subprocess.run(
                ["ip", "route", "show", "default"], capture_output=True, text=True, timeout=3
            ).stdout
            match = re.search(r"default via (\S+)", out)
        return match.group(1) if match else None
    except Exception:
        return None


def gateway_ms(gateway: str, timeout: float = 2.0) -> float:
    """A router rarely listens on a TCP port you can rely on, so ping first, TCP fallback."""
    flag = "-t" if platform.system() == "Darwin" else "-W"
    try:
        out = subprocess.run(
            ["ping", "-c", "1", flag, str(int(timeout)), gateway],
            capture_output=True,
            text=True,
            timeout=timeout + 1,
        ).stdout
        match = re.search(r"time[=<]([\d.]+)\s*ms", out)
        if match:
            return float(match.group(1))
    except Exception:
        pass
    for port in (80, 443, 53):
        try:
            return tcp_ms(gateway, port, timeout)
        except OSError:
            continue
    raise AdapterError(f"gateway {gateway} did not answer ping or TCP 80/443/53")


class ProbeAdapter:
    kind = "probe"

    def __init__(
        self,
        name: str,
        gateway: str | None = None,
        *,
        tcp: Callable[[str, int], float] = tcp_ms,
        dns: Callable[[str], float] = dns_ms,
        gateway_probe: Callable[[str], float] = gateway_ms,
        find_gateway: Callable[[], str | None] = default_gateway,
    ) -> None:
        self.name = name
        self._gateway = gateway
        self._tcp = tcp
        self._dns = dns
        self._gateway_probe = gateway_probe
        self._find_gateway = find_gateway

    def read(self) -> Reading:
        metrics: dict[str, float] = {}
        texts: dict[str, str] = {}

        timings: list[float] = []
        failures = 0
        for attempt in range(ATTEMPTS):
            host, port = INTERNET_TARGETS[attempt % len(INTERNET_TARGETS)]
            try:
                timings.append(self._tcp(host, port))
            except OSError:
                failures += 1
        if timings:
            # The nearest reachable point, not the furthest. These are different
            # destinations, and taking the worst of them answers "how far away is the
            # slowest thing I tried" rather than "how far away is the internet".
            # Measured from Lagos: 8.8.8.8 answers in 20-40 ms while 1.1.1.1 takes
            # 130-145 ms, so reporting the max overstated latency roughly fivefold and
            # graded a healthy link an F.
            #
            # Spikes are still preserved — the *bucketing* keeps the worst value over
            # time, which is a claim about this connection. The worst value across
            # destinations is a claim about somebody's routing.
            metrics["latency.internet_ms"] = min(timings)
            metrics["latency.internet_worst_ms"] = max(timings)
            if len(timings) > 1:
                # Jitter across different destinations would measure the gap between
                # them, so it is computed per target — and reported for the same target
                # the latency above came from. Taking the worst spread of any target
                # repeats the mistake the line above fixes: it describes whichever path
                # happened to be worst rather than the path being reported. Measured
                # from Lagos, 8.8.8.8 sits at 20 ms and 1.1.1.1 at 144 ms, and the two
                # stall at different moments.
                per_target: dict[str, list[float]] = {}
                for index, value in enumerate(timings):
                    host, _ = INTERNET_TARGETS[index % len(INTERNET_TARGETS)]
                    per_target.setdefault(host, []).append(value)
                nearest = min(per_target, key=lambda host: min(per_target[host]))
                if len(per_target[nearest]) > 1:
                    metrics["jitter.internet_ms"] = pstdev(per_target[nearest])
        metrics["loss.pct"] = 100.0 * failures / ATTEMPTS

        for resolver in DNS_TARGETS:
            try:
                metrics["dns.lookup_ms"] = self._dns(resolver)
                break
            except OSError:
                continue

        gateway = self._gateway or self._find_gateway()
        if gateway:
            texts["net.gateway"] = gateway
            # A silent gateway is recorded by its absence; internet timings still stand.
            with suppress(AdapterError, OSError):
                metrics["latency.gateway_ms"] = self._gateway_probe(gateway)

        if not timings and "latency.gateway_ms" not in metrics:
            raise AdapterError("no internet target and no gateway answered")
        return Reading(metrics=metrics, texts=texts)
