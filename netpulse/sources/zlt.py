"""ZLT / Tozed CPE — MTN Nigeria's own-brand 5G routers (X17U and relatives).

The web UI is a Vue SPA over a single JSON endpoint: POST /cgi-bin/http.cgi with
{"cmd": N, "method": "GET", "sessionId": ""}. Reads need no login — cmd 133 (WAN
state and byte counters), 205 (full RF detail) and 113 (liveness) all answer
unauthenticated, which is the whole monitoring set. Every value arrives as a string,
and "" means "not applicable on this unit".

Poll gently: cmd 133 alone carries connection state, uptime and the counters, so the
routine sweep is one request. The richer RF sweep (205) rides every sixth cycle.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import urllib.request
from collections.abc import Callable
from typing import Any

from netpulse.core.model import DeviceSeen, Reading
from netpulse.core.radio import carriers, spectrum_metrics
from netpulse.sources import AdapterError

#: The firmware gives no rate field (netWanRxRate is empty here), so throughput is
#: differenced from the cumulative counters. Counters are already near 2^32 on a
#: fresh-ish session, so a wrap is a real possibility, not a theoretical one.
COUNTER_WRAP = 2**32
RF_SWEEP_EVERY = 6

Fetch = Callable[[str, bytes, dict[str, str]], bytes]


def _urllib_fetch(url: str, body: bytes, headers: dict[str, str]) -> bytes:
    request = urllib.request.Request(url, data=body, headers=headers)
    with urllib.request.urlopen(request, timeout=5) as response:
        return bytes(response.read())


def _number(value: object) -> float | None:
    """Strings only, and "" is absent rather than zero — reporting an unpopulated
    field as 0 dBm would draw a flat line where there is no measurement."""
    text = str(value).strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _text(payload: dict[str, Any], key: str) -> str | None:
    value = str(payload.get(key, "")).strip()
    return value or None


def _spectrum(rf: dict[str, Any]) -> dict[str, float]:
    """The X17U reports both legs of a 5G non-standalone connection separately."""
    return spectrum_metrics(
        carriers(
            str(rf.get("currentband", "")),
            str(rf.get("FREQ", "")),
            str(rf.get("bandwidth", "")),
            str(rf.get("PCI", "")),
        )
        + carriers(
            str(rf.get("currentband_5g", "")),
            str(rf.get("FREQ_5G", "")),
            str(rf.get("bandwidth_5g", "")),
            str(rf.get("PCI_5G", "")),
            leg="nr",
        )
    )


#: Signing in unlocks the connected-device list and the flow counters. It is optional
#: on purpose — everything the outage detector needs answers without it.
DEVICE_LIST_EVERY = 6
#: Three consecutive failures lock the account for three minutes, so a wrong password
#: must be treated as final rather than retried on the next poll.
LOGIN_LOCKOUT_S = 180.0


class ZltAdapter:
    kind = "zlt"

    def __init__(
        self,
        name: str,
        url: str = "http://192.168.0.1",
        username: str = "admin",
        password: str = "",
        *,
        fetch: Fetch = _urllib_fetch,
    ):
        self.name = name
        # A pasted browser URL often carries the SPA route (http://192.168.0.1/#/).
        self.base = url.split("#")[0].rstrip("/")
        self._username = username
        self._password = password
        self._session: str | None = None
        #: Set once a login is refused, so a wrong password costs one attempt rather
        #: than one every poll — three in a row locks the account for three minutes.
        self._login_refused = False
        self._fetch = fetch
        self._cycle = 0
        self._previous: tuple[float, float, float] | None = None  # rx, tx, uptime

    def _login(self) -> str | None:
        """Sign in, returning a session id, or None if we cannot.

        The password never leaves this method: it is hashed with the router's own
        challenge token and only the digest is sent. Nothing here is logged, and a
        refusal is remembered so it is not retried into a lockout.
        """
        if not self._password or self._login_refused:
            return None
        try:
            challenge = self._command(232)
        except AdapterError:
            return None
        token = str(challenge.get("token", ""))
        if not token:
            return None

        # The client invents its own session id; the router echoes it back.
        session = secrets.token_hex(32)
        digest = hashlib.sha256((token + self._password).encode()).hexdigest()
        body = json.dumps(
            {
                "cmd": 100,
                "method": "POST",
                "username": self._username,
                "passwd": digest,
                "sessionId": session,
                "isAutoUpgrade": "1",
                "isCheckPasswd": "1",
            }
        ).encode()
        try:
            raw = self._fetch(
                f"{self.base}/cgi-bin/http.cgi",
                body,
                {"Content-Type": "application/json;charset=UTF-8"},
            )
            answer = json.loads(raw or b"")
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(answer, dict) or not answer.get("success"):
            # Wrong password, or the account is already locked. Either way, stop.
            self._login_refused = True
            return None
        # The router mints its own session id and echoes it back. Sending ours instead
        # is accepted at login and then refused on every read, which looks exactly like
        # a permissions problem and is not one.
        return str(answer.get("sessionId") or session)

    def _command(self, cmd: int, authenticated: bool = False) -> dict[str, Any]:
        session = ""
        if authenticated:
            if self._session is None:
                self._session = self._login()
            if self._session is None:
                raise AdapterError(f"cmd {cmd} needs a login")
            session = self._session
        body = json.dumps({"cmd": cmd, "method": "GET", "sessionId": session}).encode()
        try:
            raw = self._fetch(
                f"{self.base}/cgi-bin/http.cgi",
                body,
                {"Content-Type": "application/json;charset=UTF-8"},
            )
            payload = json.loads(raw or b"")
        except OSError as exc:
            raise AdapterError(f"router unreachable: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise AdapterError(f"router returned unparseable JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise AdapterError("router returned a non-object reply")
        # Errors arrive as HTTP 200 with success:false, so the status line proves nothing.
        if not payload.get("success"):
            message = str(payload.get("message", "unknown"))
            if message == "NO_AUTH" and authenticated:
                self._session = None  # expired; the next attempt signs in again
            raise AdapterError(f"cmd {cmd} refused: {message}")
        return payload

    def _rates(self, rx: float | None, tx: float | None, uptime: float | None) -> dict[str, float]:
        """Bytes per second from counter deltas, or nothing at all.

        A reboot rewinds uptime and zeroes the counters; a 32-bit wrap rewinds the
        counters alone. Either would otherwise be published as a multi-gigabyte burst
        that never happened, so both simply re-baseline and emit no rate this cycle.
        """
        if rx is None or tx is None or uptime is None:
            return {}
        previous, self._previous = self._previous, (rx, tx, uptime)
        if previous is None:
            return {}
        last_rx, last_tx, last_uptime = previous
        elapsed = uptime - last_uptime
        if elapsed <= 0:  # rebooted, or polled twice within the same second
            return {}
        deltas = []
        for current, last in ((rx, last_rx), (tx, last_tx)):
            delta = current - last
            if delta < 0:
                delta += COUNTER_WRAP
                if delta < 0 or delta > COUNTER_WRAP / 2:
                    return {}  # not a wrap — a reset; refuse to invent the traffic
            deltas.append(delta)
        return {
            "traffic.down_bytes_s": deltas[0] / elapsed,
            "traffic.up_bytes_s": deltas[1] / elapsed,
        }

    def read(self) -> Reading:
        wan = self._command(133)
        metrics: dict[str, float] = {}
        texts: dict[str, str] = {}

        # An IP on the WAN interface is the honest test of "connected": the firmware's
        # own network_status reads 1 while the SPA still shows a dead link.
        connected = bool(_text(wan, "wan_ip"))
        metrics["up"] = 1.0 if connected else 0.0

        uptime = _number(wan.get("uptime"))
        if uptime is not None:
            metrics["router.uptime_s"] = uptime

        for key, metric in (
            ("RSRP", "signal.rsrp_dbm"),
            ("SINR", "signal.sinr_db"),
            ("RSRQ", "signal.rsrq_db"),
            ("RSSI", "signal.rssi_dbm"),
            ("RSRP_5G", "signal.rsrp_5g_dbm"),
            ("SINR_5G", "signal.sinr_5g_db"),
        ):
            value = _number(wan.get(key))
            if value is not None:
                metrics[metric] = value

        rx = _number(wan.get("wan_rx_bytes"))
        tx = _number(wan.get("wan_tx_bytes"))
        metrics.update(self._rates(rx, tx, uptime))

        for key, name in (
            ("network_type_str", "net.type"),
            ("wan_ip", "net.wan_ip"),
            ("apn_name", "net.apn"),
            ("CELL_ID", "signal.cell_id"),
            ("currentband", "signal.band"),
            ("real_fwversion", "router.firmware"),
        ):
            found = _text(wan, key)
            if found is not None:
                texts[name] = found

        self._cycle += 1
        if self._cycle % RF_SWEEP_EVERY == 1:
            # The heavier RF sweep: operator, bars and 5G band detail. Failing it must
            # not fail the poll — the WAN read above is what the outage detector needs.
            try:
                rf = self._command(205)
            except AdapterError:
                rf = {}
            bars = _number(rf.get("signal_lvl"))
            if bars is not None:
                metrics["signal.bars"] = bars
            monthly = _number(rf.get("mon_total_flow"))
            if monthly is not None:
                # mon_total_flow is down+up together, so it must not wear a down label.
                metrics["data.month_total_bytes"] = monthly * 1e6  # reported in MB
            for key, name in (
                ("network_operator", "net.operator"),
                ("currentband_5g", "signal.band_5g"),
            ):
                found = _text(rf, key)
                if found is not None:
                    texts[name] = found
            metrics.update(_spectrum(rf))

        devices: list[DeviceSeen] = []
        if self._password and self._cycle % DEVICE_LIST_EVERY == 1:
            devices = self._devices()
        if devices:
            metrics["devices.count"] = float(len(devices))
        return Reading(metrics=metrics, texts=texts, devices=devices)

    def _devices(self) -> list[DeviceSeen]:
        """Connected clients, when a password unlocks them.

        Failing this must never fail the poll: the device list is garnish, and an
        expired session is not an outage.
        """
        try:
            payload = self._command(223, authenticated=True)
        except AdapterError:
            return []
        rows = payload.get("dhcp_list_info")
        if not isinstance(rows, list):
            return []
        found: list[DeviceSeen] = []
        for row in rows:
            if not isinstance(row, dict) or not row.get("mac"):
                continue
            found.append(
                DeviceSeen(
                    mac=str(row["mac"]).lower(),
                    name=str(row.get("hostname") or "").strip(),
                    ip=str(row.get("ip") or "").strip(),
                )
            )
        return found
