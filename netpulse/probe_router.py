"""`netpulse probe-router <url>` — measure what a router actually answers.

Undocumented CPE APIs differ per firmware build, and guessing from a distance is how
adapters end up wrong. This hits every endpoint NetPulse knows across vendors, prints
status and the first bytes of each reply with anything secret-looking elided, and the
output is exactly what an adapter fix needs — paste it into an issue.
"""

from __future__ import annotations

import re
import urllib.error
import urllib.request

#: (label, path, headers, POST body or None). Covers Huawei, ZTE, and the ZLT/Tozed
#: candidates seen on MTN's own-brand routers (X17U and friends run a Vue SPA over a
#: cgi JSON API); whichever answers tells us what to build.
ENDPOINTS: tuple[tuple[str, str, dict[str, str], bytes | None], ...] = (
    ("huawei session", "/api/webserver/SesTokInfo", {}, None),
    ("huawei status", "/api/monitoring/status", {}, None),
    ("huawei signal", "/api/device/signal", {}, None),
    (
        "zte goform",
        "/goform/goform_get_cmd_process?isTest=false&multi_data=1"
        "&cmd=network_type,ppp_status,lte_rsrp",
        {"Referer": "{base}/index.html"},
        None,
    ),
    (
        "zte reqproc",
        "/reqproc/proc_get?isTest=false&multi_data=1&cmd=network_type,ppp_status,lte_rsrp",
        {"Referer": "{base}/index.html"},
        None,
    ),
    (
        "zlt http.cgi",
        "/cgi-bin/http.cgi",
        {"Content-Type": "application/json"},
        b'{"cmd":100,"method":"GET","language":"EN","sessionId":""}',
    ),
    (
        "zlt lua.cgi",
        "/cgi-bin/lua.cgi",
        {"Content-Type": "application/json"},
        b'{"cmd":"GetSystemInfo"}',
    ),
    ("zlt api", "/api/system/deviceinfo", {}, None),
    ("web root", "/", {}, None),
)

#: Anything shaped like a token or session id is elided before printing.
SECRETIVE = re.compile(r"(SessionID=|TokInfo>|token|password)[^<&\s\"]{4,}", re.IGNORECASE)


def _elide(text: str) -> str:
    return SECRETIVE.sub(lambda match: match.group(1) + "…", text)


def probe_router(url: str, timeout: float = 4.0) -> int:
    base = url.split("#")[0].rstrip("/")
    if not base.startswith("http"):
        base = f"http://{base}"
    print(f"probing {base} — paste this whole output into an issue\n")
    answered = 0
    for label, path, header_template, body_bytes in ENDPOINTS:
        headers = {k: v.format(base=base) for k, v in header_template.items()}
        target = f"{base}{path}"
        try:
            request = urllib.request.Request(target, headers=headers, data=body_bytes)
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = response.read(600)
                server = response.headers.get("Server", "?")
                kind = response.headers.get("Content-Type", "?").split(";")[0]
                summary = _elide(body.decode("utf-8", errors="replace"))
                if label == "web root":
                    # The SPA's script paths reveal where its API lives.
                    scripts = re.findall(r'src="([^"]+)"', summary)[:4]
                    summary = " ".join(summary.split())[:90] + "  scripts: " + ", ".join(scripts)
                else:
                    summary = " ".join(summary.split())[:160]
                print(f"  [{response.status}] {label:14} ({server} · {kind})")
                print(f"        {summary or '(empty body)'}")
                answered += 1
        except urllib.error.HTTPError as exc:
            print(f"  [{exc.code}] {label:14} HTTP error")
        except Exception as exc:
            print(f"  [---] {label:14} {type(exc).__name__}: {exc}")
    print(
        f"\n{answered}/{len(ENDPOINTS)} endpoints answered. If none carried signal data,"
        " this firmware needs a new adapter — the output above is what it gets built from."
    )
    return 0 if answered else 1
