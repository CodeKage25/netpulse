"""Huawei LTE/5G CPE routers — what MTN, Airtel and Glo broadband boxes are underneath.

These expose an XML API on the LAN (default http://192.168.8.1) that the router's own web
UI uses. Reading status, signal, traffic and the current operator needs no login on stock
firmware; SMS (where the carrier's data-balance messages arrive) does, so login is
optional and only attempted when credentials are configured.

Poll discipline: this is a small embedded box, so one sweep of five GET endpoints per
cycle and nothing else — Dishylink's router watchdog-rebooted from one poll too many, and
this class of hardware is no sturdier.
"""

from __future__ import annotations

import base64
import hashlib
import re
import urllib.request
import xml.etree.ElementTree as ET
from collections.abc import Callable
from typing import Any

from netpulse.core.model import DeviceSeen, Reading
from netpulse.sources import AdapterError

#: CurrentNetworkType / NetworkTypeEx codes seen in the wild. Unknown codes surface as
#: "type_<code>" rather than a guess.
NETWORK_TYPES = {
    0: "none",
    1: "GSM",
    2: "GPRS",
    3: "EDGE",
    4: "3G",
    5: "3G",
    6: "3G",
    7: "3G+",
    8: "3G+",
    9: "3G+",
    19: "LTE",
    20: "5G",
    41: "3G",
    44: "3G+",
    45: "3G+",
    46: "3G+",
    64: "3G+",
    65: "3G+",
    101: "LTE-CA",
    1011: "LTE+",
    111: "5G NSA",
    112: "5G SA",
}

CONNECTED = "901"

#: The client list is heavier than the status sweep and changes slowly, so it rides
#: every Nth cycle rather than every cycle — poll gently.
HOST_LIST_EVERY = 6

Fetch = Callable[[str, dict[str, str], bytes | None], tuple[bytes, dict[str, str]]]


def _urllib_fetch(
    url: str, headers: dict[str, str], data: bytes | None
) -> tuple[bytes, dict[str, str]]:
    request = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(request, timeout=5) as response:
        return response.read(), dict(response.headers)


def _number(text: str | None) -> float | None:
    """Huawei suffixes units onto values ('-97dBm', '13dB', '>=30dB')."""
    if not text:
        return None
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    return float(match.group()) if match else None


def _xml(payload: bytes) -> ET.Element:
    try:
        root = ET.fromstring(payload.decode("utf-8", errors="replace"))
    except ET.ParseError as exc:
        raise AdapterError(f"router returned unparseable XML: {exc}") from exc
    if root.tag == "error":
        code = root.findtext("code", "?")
        raise AdapterError(f"router API error {code}")
    return root


class HuaweiAdapter:
    kind = "huawei"

    def __init__(
        self,
        name: str,
        url: str = "http://192.168.8.1",
        username: str = "",
        password: str = "",
        *,
        fetch: Fetch = _urllib_fetch,
    ) -> None:
        self.name = name
        self.base = url.rstrip("/")
        self._username = username
        self._password = password
        self._fetch = fetch
        self._cookie = ""
        self._token = ""
        self._logged_in = False
        self._sweeps = 0

    # ------------------------------------------------------------------ transport

    def _session(self) -> None:
        payload, _ = self._fetch(f"{self.base}/api/webserver/SesTokInfo", {}, None)
        root = _xml(payload)
        self._cookie = root.findtext("SesInfo", "")
        self._token = root.findtext("TokInfo", "")

    def _get(self, path: str) -> ET.Element:
        if not self._cookie:
            self._session()
        headers = {"Cookie": self._cookie, "__RequestVerificationToken": self._token}
        try:
            payload, _ = self._fetch(f"{self.base}{path}", headers, None)
        except OSError as exc:
            self._cookie = ""
            raise AdapterError(f"router unreachable: {exc}") from exc
        try:
            return _xml(payload)
        except AdapterError:
            # 125003 = stale session. One refresh, one retry, then give up honestly.
            self._session()
            headers = {"Cookie": self._cookie, "__RequestVerificationToken": self._token}
            payload, _ = self._fetch(f"{self.base}{path}", headers, None)
            return _xml(payload)

    def _post(self, path: str, body: str) -> ET.Element:
        headers = {
            "Cookie": self._cookie,
            "__RequestVerificationToken": self._token,
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        }
        payload, response_headers = self._fetch(f"{self.base}{path}", headers, body.encode())
        for key, value in response_headers.items():
            if key.lower() == "__requestverificationtoken":
                self._token = value.split("#")[0]
            if key.lower() == "set-cookie" and "SessionID" in value:
                self._cookie = value.split(";")[0]
        return _xml(payload)

    def login(self) -> None:
        """password_type 4: b64(sha256hex(user + b64(sha256hex(pass)) + token))."""
        if not self._username or not self._password:
            raise AdapterError("no router credentials configured")
        self._session()
        inner = base64.b64encode(
            hashlib.sha256(self._password.encode()).hexdigest().encode()
        ).decode()
        outer = base64.b64encode(
            hashlib.sha256((self._username + inner + self._token).encode()).hexdigest().encode()
        ).decode()
        body = (
            f'<?xml version="1.0" encoding="UTF-8"?><request>'
            f"<Username>{self._username}</Username><Password>{outer}</Password>"
            f"<password_type>4</password_type></request>"
        )
        self._post("/api/user/login", body)
        self._logged_in = True

    # ------------------------------------------------------------------ reading

    def read(self) -> Reading:
        metrics: dict[str, float] = {}
        texts: dict[str, str] = {}

        status = self._get("/api/monitoring/status")
        connected = status.findtext("ConnectionStatus") == CONNECTED
        metrics["up"] = 1.0 if connected else 0.0
        code = _number(
            status.findtext("CurrentNetworkTypeEx") or status.findtext("CurrentNetworkType")
        )
        if code is not None:
            texts["net.type"] = NETWORK_TYPES.get(int(code), f"type_{int(code)}")

        signal = self._get("/api/device/signal")
        for field, metric in (
            ("rsrp", "signal.rsrp_dbm"),
            ("rsrq", "signal.rsrq_db"),
            ("sinr", "signal.sinr_db"),
            ("rssi", "signal.rssi_dbm"),
        ):
            value = _number(signal.findtext(field))
            if value is not None:
                metrics[metric] = value
        band = signal.findtext("band")
        if band:
            texts["signal.band"] = f"B{band}" if band.isdigit() else band
        cell = signal.findtext("cell_id")
        if cell:
            texts["signal.cell_id"] = cell

        traffic = self._get("/api/monitoring/traffic-statistics")
        for field, metric in (
            ("CurrentDownloadRate", "traffic.down_bytes_s"),
            ("CurrentUploadRate", "traffic.up_bytes_s"),
            ("TotalDownload", "data.total_down_bytes"),
            ("TotalUpload", "data.total_up_bytes"),
        ):
            value = _number(traffic.findtext(field))
            if value is not None:
                metrics[metric] = value

        try:
            month = self._get("/api/monitoring/month_statistics")
            for field, metric in (
                ("CurrentMonthDownload", "data.month_down_bytes"),
                ("CurrentMonthUpload", "data.month_up_bytes"),
            ):
                value = _number(month.findtext(field))
                if value is not None:
                    metrics[metric] = value
        except (AdapterError, OSError):
            pass  # not every firmware has it; the sweep must not fail for a nice-to-have

        devices: list[DeviceSeen] | None = None
        self._sweeps += 1
        if self._sweeps % HOST_LIST_EVERY == 1:
            try:
                hosts = self._get("/api/wlan/host-list")
                devices = [
                    DeviceSeen(
                        mac=host.findtext("MacAddress", ""),
                        name=host.findtext("HostName", "") or host.findtext("ActualName", ""),
                        ip=host.findtext("IpAddress", ""),
                    )
                    for host in hosts.iter("Host")
                    if host.findtext("MacAddress")
                ]
                metrics["devices.count"] = float(len(devices))
            except (AdapterError, OSError):
                pass  # some firmware wants a login for this; the sweep must not fail

        try:
            plmn = self._get("/api/net/current-plmn")
            operator = plmn.findtext("FullName") or plmn.findtext("ShortName")
            if operator:
                texts["net.operator"] = operator
        except (AdapterError, OSError):
            pass

        return Reading(metrics=metrics, texts=texts, devices=devices)

    # ------------------------------------------------------------------ extras

    def sms_list(self, count: int = 20) -> list[dict[str, Any]]:
        """Carrier messages — where MTN sends data-balance and expiry notices. Needs login."""
        if not self._logged_in:
            self.login()
        body = (
            '<?xml version="1.0" encoding="UTF-8"?><request><PageIndex>1</PageIndex>'
            f"<ReadCount>{count}</ReadCount><BoxType>1</BoxType><SortType>0</SortType>"
            "<Ascending>0</Ascending><UnreadPreferred>0</UnreadPreferred></request>"
        )
        root = self._post("/api/sms/sms-list", body)
        return [
            {
                "from": message.findtext("Phone", ""),
                "at": message.findtext("Date", ""),
                "text": message.findtext("Content", ""),
            }
            for message in root.iter("Message")
        ]
