"""`netpulse probe-router <url>` — measure what a router actually answers.

Undocumented CPE APIs differ per firmware build, and guessing from a distance is how
adapters end up wrong. This walks the vendor registry — the same probes discovery uses,
so there is one source of truth for what NetPulse knows how to ask — plus a short list
of endpoints worth trying on an unrecognised box. Anything secret-looking is elided,
and the output is exactly what writing a new adapter needs.
"""

from __future__ import annotations

import gzip
import re
import urllib.error
import urllib.request
import zlib

from netpulse.vendors import VENDORS, Signature

#: Tried after the registry, for firmware nobody has mapped yet. Read-only, all of them.
EXPLORATORY: tuple[tuple[str, Signature], ...] = (
    (
        "zlt signal",
        Signature(
            "/cgi-bin/http.cgi",
            body=b'{"cmd":205,"method":"GET","sessionId":""}',
            headers={"Content-Type": "application/json;charset=UTF-8"},
        ),
    ),
    ("huawei status", Signature("/api/monitoring/status")),
    ("huawei signal", Signature("/api/device/signal")),
    ("tr064 desc", Signature("/tr064desc.xml")),
    ("upnp desc", Signature("/rootDesc.xml")),
    ("igd desc", Signature("/igd.xml")),
    ("luci status", Signature("/cgi-bin/luci/admin/status/overview")),
    ("generic status", Signature("/cgi-bin/status")),
)

#: Tokens, session ids and anything password-shaped never reach the terminal.
SECRET_KEYS = "SessionID|TokInfo|token|passwd|password|ICCID|IMSI|IMEI|device_sn|module_sn"
SECRETIVE = re.compile(
    r"((?:" + SECRET_KEYS + r")[\"'>=: ]{1,4})([^<&\s\"',}]{4,})",
    re.IGNORECASE,
)


def _elide(text: str) -> str:
    return SECRETIVE.sub(lambda match: match.group(1) + "…", text)


#: Enough to reach the script tags in a compressed SPA shell, not enough to slurp a
#: firmware image if something answers with one.
MAX_BODY = 256 * 1024


def _decompress(body: bytes, encoding: str) -> bytes:
    """Router web roots are usually served gzipped, and the compressed bytes hide the
    script paths that say where the real API lives — the most useful line of output."""
    if encoding == "gzip" or body[:2] == b"\x1f\x8b":
        try:
            return gzip.decompress(body)
        except (OSError, zlib.error):
            return body
    if encoding == "deflate":
        try:
            return zlib.decompress(body)
        except zlib.error:
            return body
    return body


def _request(base: str, signature: Signature, timeout: float) -> tuple[str, str, str] | str:
    headers = {key: value.format(base=base) for key, value in signature.headers.items()}
    target = f"{base}{signature.path}"
    try:
        request = urllib.request.Request(target, headers=headers, data=signature.body)
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = _decompress(
                response.read(MAX_BODY), response.headers.get("Content-Encoding", "")
            )
            kind = response.headers.get("Content-Type", "?").split(";")[0]
            return str(response.status), kind, _elide(body.decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as exc:
        return f"HTTP {exc.code}"
    except Exception as exc:
        return f"{type(exc).__name__}: {exc}"


def probe_router(url: str, timeout: float = 4.0) -> int:
    base = url.split("#")[0].rstrip("/")
    if not base.startswith("http"):
        base = f"http://{base}"
    print(f"probing {base} — paste this whole output into an issue\n")

    probes: list[tuple[str, Signature]] = []
    for vendor in VENDORS:
        for index, signature in enumerate(vendor.signatures):
            name = vendor.name or "web root"
            probes.append((f"{name} {index + 1}" if index else name, signature))
    probes.extend(EXPLORATORY)

    answered = 0
    for label, signature in probes:
        result = _request(base, signature, timeout)
        if isinstance(result, str):
            print(f"  [ --- ] {label:16} {result}")
            continue
        status, kind, body = result
        answered += 1
        summary = " ".join(body.split())
        print(f"  [ {status} ] {label:16} ({kind})")
        if "html" in kind:
            # A SPA's script paths say where its API lives; the markup itself says little.
            assets = re.findall(r'(?:src|href)="([^"]+\.(?:js|css))"', body)[:5]
            title = re.search(r"<title>([^<]{0,80})</title>", body, re.IGNORECASE)
            if title:
                print(f"           title: {title.group(1).strip()}")
            print(f"           assets: {', '.join(assets) if assets else '(none inline)'}")
        else:
            print(f"           {summary[:200] or '(empty body)'}")

    print(
        f"\n{answered}/{len(probes)} endpoints answered."
        "\nIf none carried signal data, this firmware needs a new adapter — and the"
        "\noutput above, plus the JS bundles named under assets, is what it gets built from."
    )
    return 0 if answered else 1
