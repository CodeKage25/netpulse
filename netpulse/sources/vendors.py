"""What a router is, decided by what it answers.

Every family here is identified by exactly one unauthenticated, harmless request. The
registry is data: a new vendor is a `Vendor(...)` entry, not new code in the scanner,
and the same table drives auto-discovery, the model name shown in the UI, and the
diagnostic that captures an unknown firmware's replies.

Two rules hold for every entry. The probe must be read-only and cheap — these are
fragile embedded boxes, and a scan must never be the reason one reboots. And a matcher
must be *specific*: returning a family for a payload that merely parsed would make
discovery confidently wrong, which is worse than silent.
"""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass, field

#: A matcher returns the model name, "" for "this family, model unknown", or None for
#: "not this family". The empty string and None mean genuinely different things.
Match = Callable[[bytes], str | None]


@dataclass(frozen=True)
class Signature:
    """One request that identifies a family. Templated on {base} where needed."""

    path: str
    body: bytes | None = None
    headers: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Vendor:
    name: str
    #: The adapter kind that can read it, or "" when we can name the box but not poll it.
    kind: str
    #: Where this family ships from the factory. The default gateway is always tried too.
    addresses: tuple[str, ...]
    signatures: tuple[Signature, ...]
    match: Match
    #: Shown when a router is identified but unsupported, to say what is worth doing.
    note: str = ""


def _xml(payload: bytes) -> ET.Element | None:
    try:
        return ET.fromstring(payload.decode("utf-8", errors="replace"))
    except ET.ParseError:
        return None


def _obj(payload: bytes) -> dict[str, object] | None:
    try:
        data = json.loads(payload or b"")
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None


# --------------------------------------------------------------------------- matchers


def _match_huawei(payload: bytes) -> str | None:
    """The session-token endpoint answers unauthenticated on every Huawei CPE seen."""
    root = _xml(payload)
    if root is None:
        return None
    if root.findtext("SesInfo") is not None or root.findtext("TokInfo") is not None:
        return ""
    # The basic-information follow-up carries the marketing name (B535-232, E5577…).
    return root.findtext("devicename") or None


def _match_zlt(payload: bytes) -> str | None:
    """ZLT/Tozed — MTN Nigeria's own-brand 5G boxes. cmd 113 names the board."""
    data = _obj(payload)
    if data is None or not data.get("success"):
        return None
    board = str(data.get("board_type") or "").strip()
    return board or ""


def _match_zte(payload: bytes) -> str | None:
    data = _obj(payload)
    if data is None or not data:
        return None
    # Any of these keys coming back proves the goform/reqproc protocol answered.
    if not any(key in data for key in ("network_type", "ppp_status", "cr_version")):
        return None
    return str(data.get("cr_version") or "").strip()


def _match_openwrt(payload: bytes) -> str | None:
    """A stock ubus endpoint answers JSON-RPC even before login, with an auth error."""
    data = _obj(payload)
    if data is None or data.get("jsonrpc") != "2.0":
        return None
    return ""


def _match_starlink(payload: bytes) -> str | None:
    """The dish answers gRPC-Web on 9201 with a framed reply. Any well-formed frame is
    proof enough — decoding the payload is the adapter's job, not the scanner's."""
    if len(payload) < 5 or payload[0] not in (0x00, 0x80):
        return None
    return "dish"


def _match_netgear_cellular(payload: bytes) -> str | None:
    """model.json is served without a login and carries the whole radio. Some firmware
    answers with the HTML login page under a 200, so the body must actually parse."""
    data = _obj(payload)
    if data is None:
        return None
    general = data.get("general")
    if not isinstance(general, dict):
        return None
    if "NETGEAR" not in str(general.get("companyName", "")).upper():
        return None
    return str(general.get("deviceName") or general.get("model") or "").strip()


def _match_netgear_home(payload: bytes) -> str | None:
    """currentsetting.htm is plain key=value text, unauthenticated on every model."""
    text = payload.decode("utf-8", errors="replace")
    if "Firmware=" not in text and "Model=" not in text:
        return None
    for line in text.splitlines():
        if line.startswith("Model="):
            return line.split("=", 1)[1].strip()
    return ""


def _match_fastmile(payload: bytes) -> str | None:
    """Nokia's 5G gateway marks this endpoint `security: []` in its own OpenAPI spec."""
    data = _obj(payload)
    if data is None:
        return None
    if not any(key.startswith("cell_") for key in data):
        return None
    return "FastMile"


def _match_teltonika_rest(payload: bytes) -> str | None:
    """`/api/unauthorized/status` is the cleanest zero-credential model fingerprint of
    any vendor here — it exists precisely to be asked before logging in."""
    data = _obj(payload)
    if data is None:
        return None
    body = data.get("data") if isinstance(data.get("data"), dict) else data
    if not isinstance(body, dict) or "deviceModel" not in body:
        return None
    return str(body.get("deviceModel") or "").strip()


def _match_glinet(payload: bytes) -> str | None:
    data = _obj(payload)
    if data is None or "jsonrpc" not in data:
        return None
    return "GL.iNet"


def _match_mikrotik(payload: bytes) -> str | None:
    """RouterOS answers /rest/ with 401 and a RouterOS-shaped body, or a JSON error."""
    text = payload.decode("utf-8", errors="replace").lower()
    return "" if "routeros" in text or "mikrotik" in text else None


#: Vendors we can name from a page but not yet poll. Ordered longest-first so a page
#: mentioning both a chipset and a brand resolves to the brand.
WEB_UI_MARKERS: tuple[tuple[str, str], ...] = (
    ("tozed", "ZLT (Tozed)"),
    ("fastmile", "Nokia FastMile"),
    ("teltonika", "Teltonika"),
    ("gl.inet", "GL.iNet"),
    ("glinet", "GL.iNet"),
    ("sagemcom", "Sagemcom"),
    ("technicolor", "Technicolor"),
    ("inseego", "Inseego"),
    ("franklin", "Franklin"),
    ("alcatel", "Alcatel"),
    ("huawei", "Huawei"),
    ("mikrotik", "MikroTik"),
    ("openwrt", "OpenWrt"),
    ("tp-link", "TP-Link"),
    ("tplink", "TP-Link"),
    ("netgear", "Netgear"),
    ("zyxel", "Zyxel"),
    ("tenda", "Tenda"),
    ("cudy", "Cudy"),
    ("nokia", "Nokia"),
    ("inseego", "Inseego"),
    ("zlt", "ZLT (Tozed)"),
    ("zte", "ZTE"),
)


def _match_web_ui(payload: bytes) -> str | None:
    """Last resort: a router page whose API we do not speak.

    Reported rather than swallowed — telling someone "no router found" about a box that
    plainly answered would send them looking for a network fault that is really a
    missing adapter.
    """
    body = payload.decode("utf-8", errors="replace").lower()
    if len(body) < 40:
        return None
    title = re.search(r"<title>([^<]{1,80})</title>", body)
    for marker, vendor in WEB_UI_MARKERS:
        if marker in body:
            return vendor
    if "<html" in body and any(word in body for word in ("login", "router", "admin", "modem")):
        named = (title.group(1).strip() if title else "").title()
        return named or "unidentified router"
    return None


# --------------------------------------------------------------------------- registry

ZTE_COMMANDS = "network_type,ppp_status,cr_version"

#: Order matters: the most specific probe runs first, and the generic page-sniff last.
#: Every probe here is unauthenticated by design — fingerprinting happens before anyone
#: is asked for a password, and several of these families hand over their whole radio
#: in the same reply that identifies them.
VENDORS: tuple[Vendor, ...] = (
    Vendor(
        name="Starlink",
        kind="starlink",
        addresses=("192.168.100.1",),
        # 9201 is gRPC-Web over HTTP/1.1. Port 9200 speaks HTTP/2 and is unreachable
        # from the standard library, which is why the dish is asked on this one.
        signatures=(
            Signature(
                ":9201/SpaceX.API.Device.Device/Handle",
                body=bytes.fromhex("0000000003e23e00"),
                headers={"Content-Type": "application/grpc-web+proto"},
            ),
        ),
        match=_match_starlink,
    ),
    Vendor(
        name="Netgear",
        kind="netgear",
        addresses=("192.168.1.1", "192.168.5.1"),
        signatures=(Signature("/api/model.json?internalapi=1"),),
        match=_match_netgear_cellular,
    ),
    Vendor(
        name="Nokia",
        kind="fastmile",
        addresses=("192.168.1.1", "192.168.0.1"),
        signatures=(
            # The radio endpoint first: it is the one the adapter reads, so a match
            # here means the source it configures will work, not merely that a Nokia
            # box is present.
            Signature("/fastmile_radio_status_web_app.cgi"),
            Signature("/overview_get_web_app.cgi"),
        ),
        match=_match_fastmile,
    ),
    Vendor(
        name="Teltonika",
        kind="",
        addresses=("192.168.1.1",),
        signatures=(Signature("/api/unauthorized/status"),),
        match=_match_teltonika_rest,
        note="RutOS exposes signal over its REST API once you log in.",
    ),
    Vendor(
        name="Huawei",
        kind="huawei",
        addresses=("192.168.8.1", "192.168.1.1"),
        signatures=(
            Signature("/api/webserver/SesTokInfo"),
            Signature("/api/device/basic_information"),
        ),
        match=_match_huawei,
    ),
    Vendor(
        name="ZLT",
        kind="zlt",
        addresses=("192.168.0.1", "192.168.1.1"),
        signatures=(
            Signature(
                "/cgi-bin/http.cgi",
                body=b'{"cmd":113,"method":"GET","sessionId":""}',
                headers={"Content-Type": "application/json;charset=UTF-8"},
            ),
        ),
        match=_match_zlt,
    ),
    Vendor(
        name="ZTE",
        kind="zte",
        addresses=("192.168.0.1", "192.168.1.1", "192.168.32.1"),
        signatures=(
            Signature(
                f"/goform/goform_get_cmd_process?isTest=false&multi_data=1&cmd={ZTE_COMMANDS}",
                headers={"Referer": "{base}/index.html"},
            ),
            Signature(
                f"/reqproc/proc_get?isTest=false&multi_data=1&cmd={ZTE_COMMANDS}",
                headers={"Referer": "{base}/index.html"},
            ),
        ),
        match=_match_zte,
    ),
    Vendor(
        name="OpenWrt",
        kind="",
        addresses=("192.168.1.1",),
        signatures=(
            Signature(
                "/ubus",
                body=b'{"jsonrpc":"2.0","id":1,"method":"list","params":[]}',
                headers={"Content-Type": "application/json"},
            ),
        ),
        match=_match_openwrt,
        note="OpenWrt exposes everything over ubus, but it needs an rpcd login first.",
    ),
    Vendor(
        name="MikroTik",
        kind="",
        addresses=("192.168.88.1",),
        signatures=(Signature("/rest/system/resource"),),
        match=_match_mikrotik,
        note="RouterOS REST needs credentials; nothing useful answers anonymously.",
    ),
    Vendor(
        name="GL.iNet",
        kind="",
        addresses=("192.168.8.1",),
        signatures=(
            Signature(
                "/rpc",
                body=b'{"jsonrpc":"2.0","id":1,"method":"challenge"}',
                headers={"Content-Type": "application/json"},
            ),
        ),
        match=_match_glinet,
        note="GL.iNet reports full modem signal over /rpc after a challenge login.",
    ),
    Vendor(
        name="Netgear",
        kind="",
        addresses=("192.168.1.1",),
        signatures=(Signature("/currentsetting.htm"),),
        match=_match_netgear_home,
        note="Netgear home routers answer this without a login, but carry no radio.",
    ),
    Vendor(
        name="",  # filled in from the page itself
        kind="",
        addresses=(),
        signatures=(Signature("/"),),
        match=_match_web_ui,
        note="Found the box, but NetPulse does not speak its API yet.",
    ),
)


def candidate_addresses(gateway: str | None) -> list[str]:
    """Every address worth trying, the gateway first — it is the likeliest by far."""
    seen: list[str] = []
    for address in (gateway, *(a for vendor in VENDORS for a in vendor.addresses)):
        if address and address not in seen:
            seen.append(address)
    return seen
