"""Nokia FastMile — the 4G and 5G fixed-wireless gateways.

Significant well beyond Nokia's own footprint: this is the box MTN South Africa,
Safaricom and a long list of other operators ship with a fixed-LTE plan, which makes it
one of the most common CPE units on the continent.

`fastmile_radio_status_web_app.cgi` answers without a login on the firmware seen so
far, and returns the whole radio: every aggregated carrier as its own entry, with RSRP,
RSRQ, SNR, PCI, band and channel per cell, for LTE and NR separately. That per-carrier
array is unusually good — most vendors flatten aggregation into one "+"-joined string
and lose which figure belongs to which carrier.

What it does **not** carry is a connection state. This adapter therefore does not claim
one: it reports the radio, which is what the endpoint knows. Whether traffic actually
reaches the internet is the probe source's question, and it is measured rather than
inferred. The one down signal taken from here is a radio attached to no cell at all —
which is a real fact about the link, not a guess about it.
"""

from __future__ import annotations

import json
import re
import time
import urllib.request
from collections.abc import Callable
from typing import Any

from netpulse.core.model import Reading
from netpulse.core.radio import carriers, spectrum_metrics
from netpulse.core.rates import Counters
from netpulse.sources import AdapterError

#: The unauthenticated radio endpoint, and the overview page some builds serve instead.
#: Whichever answers first is remembered, so a dead path is not retried every sweep.
PATHS = ("/fastmile_radio_status_web_app.cgi", "/overview_get_web_app.cgi")

Fetch = Callable[[str], bytes]


def _urllib_fetch(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=6) as response:
        return bytes(response.read())


def _number(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    found = re.search(r"-?\d+(?:\.\d+)?", str(value or ""))
    return float(found.group()) if found else None


def _cells(payload: dict[str, Any], key: str) -> list[dict[str, Any]]:
    """The `stat` object of each carrier under one of the `cell_*_stats_cfg` arrays.

    Firmware varies on whether the figures sit under `stat` or directly on the entry,
    so both shapes are accepted rather than one being declared correct.
    """
    entries = payload.get(key)
    if not isinstance(entries, list):
        return []
    found = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        stat = entry.get("stat")
        found.append(stat if isinstance(stat, dict) else entry)
    return found


def _attached(cell: dict[str, Any]) -> bool:
    """A carrier with a plausible RSRP. Idle slots in the array report 0 or -1000, and
    counting those as carriers would draw aggregation that is not happening."""
    rsrp = _number(cell.get("RSRPCurrent"))
    return rsrp is not None and -145.0 <= rsrp <= -30.0


class FastMileAdapter:
    kind = "fastmile"

    def __init__(
        self,
        name: str,
        url: str = "http://192.168.1.1",
        *,
        fetch: Fetch = _urllib_fetch,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.name = name
        self.base = url.split("#")[0].rstrip("/")
        self._fetch = fetch
        self._clock = clock
        self._path: str | None = None
        # No uptime field here, so throughput is differenced against the wall clock. A
        # reboot still shows up: the counters restart below where they were, and a
        # backwards counter publishes nothing rather than a burst.
        self._counters = Counters()

    def read(self) -> Reading:
        payload = self._payload()

        lte = _cells(payload, "cell_LTE_stats_cfg")
        nr = _cells(payload, "cell_5G_stats_cfg")
        if not lte and not nr:
            raise AdapterError("no cell_*_stats_cfg in the reply — wrong device?")

        metrics: dict[str, float] = {}
        texts: dict[str, str] = {}

        # The primary carrier is the first attached one; the rest are aggregation, and
        # averaging their RSRP into a single figure would report a signal no radio has.
        serving = next((cell for cell in lte if _attached(cell)), None)
        serving_nr = next((cell for cell in nr if _attached(cell)), None)

        for cell, suffix in ((serving, ""), (serving_nr, "_5g")):
            if cell is None:
                continue
            for key, metric in (
                ("RSRPCurrent", f"signal.rsrp{suffix}_dbm"),
                ("RSRQCurrent", f"signal.rsrq{suffix}_db"),
                ("SNRCurrent", f"signal.sinr{suffix}_db"),
                ("RSSICurrent", f"signal.rssi{suffix}_dbm"),
            ):
                value = _number(cell.get(key))
                if value is not None:
                    metrics[metric] = value

        bars = _number((serving or serving_nr or {}).get("RSRPStrengthIndexCurrent"))
        if bars is not None:
            metrics["signal.bars"] = bars

        if serving is not None:
            band = _number(serving.get("Band"))
            if band is not None:
                texts["signal.band"] = f"B{int(band)}"
            pci = _number(serving.get("PhysicalCellID"))
            if pci is not None:
                texts["signal.cell_id"] = str(int(pci))
        if serving is None and serving_nr is not None:
            band = _number(serving_nr.get("Band"))
            if band is not None:
                texts["signal.band"] = f"n{int(band)}"

        # Nothing attached anywhere is a genuine down reading, not a missing one.
        if serving is None and serving_nr is None:
            metrics["up"] = 0.0

        metrics.update(_spectrum(lte, nr))
        metrics.update(self._traffic(payload))

        texts["net.type"] = "5G" if serving_nr is not None else "LTE"
        return Reading(metrics=metrics, texts=texts)

    def _payload(self) -> dict[str, Any]:
        paths = (self._path,) if self._path else PATHS
        last: Exception | None = None
        for path in paths:
            try:
                raw = self._fetch(f"{self.base}{path}")
            except OSError as exc:
                last = exc
                continue
            try:
                found = json.loads(raw or b"")
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                # Some builds answer an unauthenticated request with the login page
                # under a 200, so an unparseable body means "sign in", not "broken".
                last = exc
                continue
            if isinstance(found, dict):
                self._path = path
                return found
            last = AdapterError("router returned an unexpected shape")
        raise AdapterError(f"no readable status endpoint: {last}")

    def _traffic(self, payload: dict[str, Any]) -> dict[str, float]:
        stats = payload.get("cellular_stats")
        first = stats[0] if isinstance(stats, list) and stats else stats
        if not isinstance(first, dict):
            return {}
        return self._counters.rates(
            _number(first.get("BytesReceived")),
            _number(first.get("BytesSent")),
            self._clock(),
        )


def _spectrum(lte: list[dict[str, Any]], nr: list[dict[str, Any]]) -> dict[str, float]:
    """Every attached carrier, placed where it really sits in the spectrum.

    FastMile gives no channel bandwidth, so carriers are positioned but not widened —
    the chart shows where each one is rather than pretending to know how wide it is.
    """
    stack = []
    for cells, leg, channel_key in (
        (lte, "lte", "DownlinkEarfcn"),
        (nr, "nr", "Downlink_NR_ARFCN"),
    ):
        placed = []
        for entry in cells:
            if not _attached(entry):
                continue
            band = _number(entry.get("Band"))
            channel = _number(entry.get(channel_key))
            if band is None or channel is None:
                continue  # a carrier with no channel cannot be placed, only counted
            pci = _number(entry.get("PhysicalCellID"))
            placed.append((int(band), int(channel), "" if pci is None else str(int(pci))))
        if not placed:
            continue
        stack += carriers(
            "+".join(str(band) for band, _, _ in placed),
            "+".join(str(channel) for _, channel, _ in placed),
            "",
            "+".join(pci for _, _, pci in placed),
            leg=leg,
        )
    return spectrum_metrics(stack)
