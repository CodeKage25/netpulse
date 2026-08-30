"""Netgear cellular — M1/M2/M5/M6, AirCard, LB1120/LB2120, LM1200.

One unauthenticated GET returns the entire radio: RSRP, RSRQ, SINR, RSSI, bars, band,
cell, operator, connection state, roaming and uptime. Only the byte counters and the
device identity are gated behind a login, which is the opposite of most vendors and
makes this the cheapest useful adapter in the project.

Two traps. Some firmware answers an unauthenticated request with the **HTML login page
under a 200**, so the body has to parse before it is trusted. And the LB/LM line lives
on 192.168.5.1 rather than the 192.168.1.1 the M-series uses — a detail that turns
"unsupported" into "wrong address".
"""

from __future__ import annotations

import json
import re
import urllib.request
from collections.abc import Callable
from typing import Any

from netpulse.core.model import Reading
from netpulse.core.radio import bandwidth_mhz, carriers, spectrum_metrics
from netpulse.sources import AdapterError

MODEL_PATH = "/api/model.json?internalapi=1"

Fetch = Callable[[str], bytes]


def _urllib_fetch(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=6) as response:
        return bytes(response.read())


def _number(value: object) -> float | None:
    """Netgear mixes plain numbers with unit-bearing strings in the same document:
    `signalStrength.rsrp` is an integer while `diagInfo.ltesigRsrp` is "-107 dBm"."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    found = re.search(r"-?\d+(?:\.\d+)?", str(value or ""))
    return float(found.group()) if found else None


def _dig(payload: dict[str, Any], *path: str) -> Any:
    node: Any = payload
    for key in path:
        if not isinstance(node, dict) or key not in node:
            return None
        node = node[key]
    return node


class NetgearAdapter:
    kind = "netgear"

    def __init__(self, name: str, url: str = "http://192.168.1.1", *, fetch: Fetch = _urllib_fetch):
        self.name = name
        self.base = url.split("#")[0].rstrip("/")
        self._fetch = fetch

    def read(self) -> Reading:
        try:
            raw = self._fetch(f"{self.base}{MODEL_PATH}")
        except OSError as exc:
            raise AdapterError(f"router unreachable: {exc}") from exc
        try:
            payload = json.loads(raw or b"")
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            # Some firmware serves the login page with a 200 rather than a 401, so an
            # unparseable body means "sign in", not "the router is broken".
            raise AdapterError("router returned a page, not JSON — it wants a login") from exc
        if not isinstance(payload, dict):
            raise AdapterError("router returned an unexpected shape")

        metrics: dict[str, float] = {}
        texts: dict[str, str] = {}

        strength = _dig(payload, "wwan", "signalStrength") or {}
        for key, metric in (
            ("rsrp", "signal.rsrp_dbm"),
            ("rsrq", "signal.rsrq_db"),
            ("sinr", "signal.sinr_db"),
            ("rssi", "signal.rssi_dbm"),
            ("bars", "signal.bars"),
        ):
            value = _number(strength.get(key)) if isinstance(strength, dict) else None
            if value is not None:
                metrics[metric] = value

        # The 5G leg lives in diagInfo, as strings with units.
        diag = _dig(payload, "wwan", "diagInfo")
        first = diag[0] if isinstance(diag, list) and diag else diag
        if isinstance(first, dict):
            for key, metric in (
                ("nr5gsigRsrp", "signal.rsrp_5g_dbm"),
                ("nr5gsigRsrq", "signal.rsrq_5g_db"),
                ("nr5gsigSnr", "signal.sinr_5g_db"),
            ):
                value = _number(first.get(key))
                if value is not None:
                    metrics[metric] = value

        connection = str(_dig(payload, "wwan", "connection") or "").lower()
        # "connected" is the documented value; anything else — including an empty
        # string on a firmware that omits it — is not a claim that the link is up.
        metrics["up"] = 1.0 if connection == "connected" else 0.0

        uptime = _number(_dig(payload, "general", "upTime"))
        if uptime is not None:
            metrics["router.uptime_s"] = uptime

        for path, name in (
            (("wwan", "registerNetworkDisplay"), "net.operator"),
            (("wwan", "currentNWserviceType"), "net.type"),
            (("wwanadv", "curBand"), "signal.band"),
            (("general", "deviceName"), "router.name"),
            (("general", "FWversion"), "router.firmware"),
        ):
            value = _dig(payload, *path)
            if value:
                texts[name] = str(value)

        cell = _dig(payload, "wwanadv", "cellId")
        if cell:
            texts["signal.cell_id"] = str(cell)

        metrics.update(_spectrum(payload))

        # Byte counters need a login; absent is absent rather than zero.
        for path, metric in (
            (("wwan", "dataTransferredRx"), "data.month_down_bytes"),
            (("wwan", "dataTransferredTx"), "data.month_up_bytes"),
        ):
            value = _number(_dig(payload, *path))
            if value:
                metrics[metric] = value

        if not metrics.get("signal.rsrp_dbm") and not uptime:
            raise AdapterError("model.json parsed but carried no radio — wrong device?")
        return Reading(metrics=metrics, texts=texts)


def _spectrum(payload: dict[str, Any]) -> dict[str, float]:
    """Netgear names the band as "LTE B3" rather than a number, and gives no channel."""
    band = str(_dig(payload, "wwanadv", "curBand") or "")
    number = re.search(r"[Bn](\d+)", band)
    channel = _number(_dig(payload, "wwanadv", "earfcn")) or _number(
        _dig(payload, "wwanadv", "channel")
    )
    if not number or channel is None:
        return {}
    leg = "nr" if band.strip().lower().startswith("n") else "lte"
    return spectrum_metrics(
        carriers(
            number.group(1),
            str(int(channel)),
            bandwidth_mhz(str(_dig(payload, "wwanadv", "bandwidth") or "")),
            str(_dig(payload, "wwanadv", "pci") or ""),
            leg=leg,
        )
    )
