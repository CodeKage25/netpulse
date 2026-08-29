"""Find the router. Nobody should hand-edit a config file to point at their own network.

Carrier CPE lives at a handful of well-known addresses (Huawei ships at 192.168.8.1, ZTE
at 192.168.0.1, plus whatever the default gateway is), and each family answers a cheap,
harmless identification request. Candidates are probed in parallel with short timeouts,
so a full scan is over in about two seconds.
"""

from __future__ import annotations

import json
import urllib.request
import xml.etree.ElementTree as ET
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from netpulse.adapters.probe import default_gateway

#: Where carrier CPE actually lives, tried alongside the default gateway.
WELL_KNOWN = ("192.168.8.1", "192.168.0.1", "192.168.1.1")
TIMEOUT_S = 2.0

Fetch = Callable[[str, dict[str, str]], bytes]


def _urllib_fetch(url: str, headers: dict[str, str]) -> bytes:
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
        return bytes(response.read())


@dataclass(frozen=True)
class Discovered:
    kind: str
    url: str
    label: str


def _detect_huawei(base: str, fetch: Fetch) -> Discovered | None:
    """The session-token endpoint answers unauthenticated on every Huawei CPE seen."""
    try:
        payload = fetch(f"{base}/api/webserver/SesTokInfo", {})
        root = ET.fromstring(payload.decode("utf-8", errors="replace"))
    except Exception:
        return None
    if root.findtext("SesInfo") is None and root.findtext("TokInfo") is None:
        return None
    label = "Huawei router"
    try:
        info = fetch(f"{base}/api/device/basic_information", {})
        name = ET.fromstring(info.decode("utf-8", errors="replace")).findtext("devicename")
        if name:
            label = name
    except Exception:
        pass  # the identification is optional garnish
    return Discovered(kind="huawei", url=base, label=label)


def _detect_zte(base: str, fetch: Fetch) -> Discovered | None:
    for path in ("/goform/goform_get_cmd_process", "/reqproc/proc_get"):
        try:
            payload = fetch(
                f"{base}{path}?isTest=false&multi_data=1&cmd=network_type,ppp_status",
                {"Referer": f"{base}/index.html"},
            )
            data = json.loads(payload or b"")
        except Exception:
            continue
        if isinstance(data, dict) and data:
            return Discovered(kind="zte", url=base, label="ZTE router")
    return None


def _detect_web_ui(base: str, fetch: Fetch) -> Discovered | None:
    """Last resort: a router page whose API we do not speak yet. Reported honestly as
    unidentified, so the user learns where their router is and can run the diagnostic,
    instead of discovery staying silent about a box it clearly saw."""
    try:
        body = fetch(f"{base}/", {}).decode("utf-8", errors="replace").lower()
    except Exception:
        return None
    if len(body) < 40:
        return None
    for marker, vendor in (
        ("tozed", "ZLT (Tozed)"),
        ("zlt", "ZLT (Tozed)"),
        ("zte", "ZTE"),
        ("huawei", "Huawei"),
        ("mifi", "MiFi"),
    ):
        if marker in body:
            return Discovered(
                kind="unknown", url=base, label=f"{vendor} web UI (API not identified)"
            )
    if "<html" in body and ("login" in body or "router" in body or "admin" in body):
        return Discovered(kind="unknown", url=base, label="router web UI (API not identified)")
    return None


def _probe(address: str, fetch: Fetch) -> Discovered | None:
    base = f"http://{address}"
    return _detect_huawei(base, fetch) or _detect_zte(base, fetch) or _detect_web_ui(base, fetch)


def discover(
    gateway: str | None = None,
    fetch: Fetch = _urllib_fetch,
    find_gateway: Callable[[], str | None] = default_gateway,
) -> list[Discovered]:
    """Every router found on the candidate addresses, gateway first."""
    seen: list[str] = []
    for address in ((gateway or find_gateway()), *WELL_KNOWN):
        if address and address not in seen:
            seen.append(address)

    with ThreadPoolExecutor(max_workers=len(seen) or 1) as pool:
        results = pool.map(lambda address: _probe(address, fetch), seen)
    found: list[Discovered] = []
    for result in results:
        if result is not None and all(r.url != result.url for r in found):
            found.append(result)
    return found
