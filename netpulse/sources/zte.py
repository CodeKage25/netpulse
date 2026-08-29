"""ZTE LTE/5G CPE routers — the other half of carrier broadband boxes (Airtel MC-series,
many MiFis). JSON API at the router address: one GET with a comma-separated command list
returns every field at once, which suits the poll-gently rule perfectly — the whole sweep
is a single request.
"""

from __future__ import annotations

import json
import urllib.request
from collections.abc import Callable

from netpulse.core.model import Reading
from netpulse.sources import AdapterError

FIELDS = ",".join(
    [
        "network_type",
        "signalbar",
        "lte_rsrp",
        "lte_rsrq",
        "lte_snr",
        "lte_rssi",
        "lte_band",
        "cell_id",
        "network_provider",
        "ppp_status",
        "realtime_rx_thrpt",
        "realtime_tx_thrpt",
        "monthly_rx_bytes",
        "monthly_tx_bytes",
        "realtime_rx_bytes",
        "realtime_tx_bytes",
    ]
)

Fetch = Callable[[str, dict[str, str]], bytes]


def _urllib_fetch(url: str, headers: dict[str, str]) -> bytes:
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=5) as response:
        return bytes(response.read())


def _number(value: object) -> float | None:
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return None


#: ZTE firmware ships two API roots for the same command protocol: MC-series CPE uses
#: /goform/goform_get_cmd_process, MF-series and many carrier builds use /reqproc/proc_get.
API_PATHS = ("/goform/goform_get_cmd_process", "/reqproc/proc_get")


class ZteAdapter:
    kind = "zte"

    def __init__(self, name: str, url: str = "http://192.168.0.1", *, fetch: Fetch = _urllib_fetch):
        self.name = name
        # A pasted browser URL often carries the SPA route (http://192.168.0.1/#/).
        self.base = url.split("#")[0].rstrip("/")
        self._fetch = fetch
        self._api_path: str | None = None

    def _query(self) -> dict[str, object]:
        last: Exception | None = None
        paths = (self._api_path,) if self._api_path else API_PATHS
        for path in paths:
            query = f"{self.base}{path}?isTest=false&multi_data=1&cmd={FIELDS}"
            try:
                # The Referer is required: the firmware answers an empty body without it.
                payload = self._fetch(query, {"Referer": f"{self.base}/index.html"})
                data = json.loads(payload or b"")
            except (OSError, json.JSONDecodeError) as exc:
                last = exc
                continue
            if isinstance(data, dict) and data:
                self._api_path = path
                return data
        if last is not None:
            raise AdapterError(f"router did not answer a known ZTE API: {last}") from last
        raise AdapterError("router returned an empty reply (Referer guard, or rebooting)")

    def read(self) -> Reading:
        data = self._query()

        metrics: dict[str, float] = {
            "up": 1.0 if str(data.get("ppp_status", "")).lower() == "ppp_connected" else 0.0
        }
        texts: dict[str, str] = {}

        for field, metric in (
            ("lte_rsrp", "signal.rsrp_dbm"),
            ("lte_rsrq", "signal.rsrq_db"),
            ("lte_snr", "signal.sinr_db"),
            ("lte_rssi", "signal.rssi_dbm"),
            ("realtime_rx_thrpt", "traffic.down_bytes_s"),
            ("realtime_tx_thrpt", "traffic.up_bytes_s"),
            ("monthly_rx_bytes", "data.month_down_bytes"),
            ("monthly_tx_bytes", "data.month_up_bytes"),
        ):
            value = _number(data.get(field))
            if value is not None:
                metrics[metric] = value

        if data.get("network_type"):
            texts["net.type"] = str(data["network_type"])
        if data.get("network_provider"):
            texts["net.operator"] = str(data["network_provider"])
        if data.get("lte_band"):
            texts["signal.band"] = f"B{data['lte_band']}"
        if data.get("cell_id"):
            texts["signal.cell_id"] = str(data["cell_id"])
        return Reading(metrics=metrics, texts=texts)
