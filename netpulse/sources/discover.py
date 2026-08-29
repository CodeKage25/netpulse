"""Find the router. Nobody should hand-edit a config file to point at their own network.

The scan is a loop over the vendor registry, so what NetPulse can recognise is data in
`vendors.py` rather than logic here. Addresses are probed in parallel because they are
different hosts, but each host is asked *serially* — a fragile CPE box should never
meet a burst of concurrent requests from a tool whose whole job is not to disturb it.
"""

from __future__ import annotations

import urllib.request
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from netpulse.sources.probe import default_gateway
from netpulse.sources.vendors import VENDORS, Vendor, candidate_addresses

TIMEOUT_S = 2.0
MAX_PARALLEL_HOSTS = 8

Fetch = Callable[..., bytes]


def _urllib_fetch(url: str, headers: dict[str, str], body: bytes | None = None) -> bytes:
    request = urllib.request.Request(url, headers=headers, data=body)
    with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
        return bytes(response.read())


@dataclass(frozen=True)
class Discovered:
    kind: str
    url: str
    label: str
    #: Empty when the router is supported; otherwise why it is not, and what helps.
    note: str = ""

    @property
    def supported(self) -> bool:
        return bool(self.kind)


def _try_vendor(base: str, vendor: Vendor, fetch: Fetch) -> Discovered | None:
    """Ask one family's question. The first signature that matches wins; a later one
    only ever refines the model name, never the identification."""
    model: str | None = None
    for signature in vendor.signatures:
        headers = {key: value.format(base=base) for key, value in signature.headers.items()}
        # A path beginning with ":" carries its own port — Starlink answers on 9201.
        target = (
            base.rsplit(":", 1)[0] + signature.path
            if signature.path.startswith(":") and base.count(":") > 1
            else base + signature.path
        )
        try:
            payload = fetch(target, headers, signature.body)
        except Exception:
            continue
        found = vendor.match(payload)
        if found is None:
            continue
        if found:
            model = found
            break
        model = model or ""  # family confirmed, still nameless
    if model is None:
        return None
    return Discovered(
        kind=vendor.kind, url=base, label=_label(vendor.name, model), note=vendor.note
    )


def _label(vendor_name: str, model: str) -> str:
    """ "ZLT" + "ZLT X17U" is "ZLT X17U", not "ZLT ZLT X17U" — several firmwares report a
    board name that already carries the brand."""
    name = vendor_name or model or "router"
    if not model or model == name:
        return name
    if model.lower().startswith(name.lower()):
        return model
    return f"{name} {model}"


def _probe(address: str, fetch: Fetch) -> Discovered | None:
    base = f"http://{address}"
    for vendor in VENDORS:
        found = _try_vendor(base, vendor, fetch)
        if found is not None:
            return found
    return None


def discover(
    gateway: str | None = None,
    fetch: Fetch = _urllib_fetch,
    find_gateway: Callable[[], str | None] = default_gateway,
) -> list[Discovered]:
    """Every router found on the candidate addresses, gateway first.

    Supported routers sort ahead of merely-identified ones, so the thing you can
    actually watch is the thing you are offered.
    """
    addresses = candidate_addresses(gateway if gateway is not None else find_gateway())
    workers = min(MAX_PARALLEL_HOSTS, len(addresses)) or 1
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(lambda address: _probe(address, fetch), addresses))

    found: list[Discovered] = []
    for result in results:
        if result is not None and all(existing.url != result.url for existing in found):
            found.append(result)
    return sorted(found, key=lambda item: not item.supported)
