"""HTTP transport: a stdlib server that hands `Api` answers to a browser.

Deliberately thin, and deliberately boring. It binds to the loopback address, serves
one page and a handful of JSON endpoints, and holds no logic of its own — the moment
routing starts making decisions about data, that decision belongs in `api.py`, where it
can be tested without a socket.

`Api` is re-exported here because it is what a caller wiring up a server needs, and
making them import from two modules to start one server would be pedantry.
"""

from __future__ import annotations

import gzip
import io
import json
import queue
from functools import lru_cache
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from typing import Any
from urllib.parse import parse_qs, urlparse

from netpulse.web.api import Api
from netpulse.web.auth import Guard

__all__ = ["Api", "Guard", "make_handler", "serve"]

#: The largest body this server will read, before and after decompression. Only the
#: ingest endpoint sends anything substantial, and a batch of five thousand rows is
#: comfortably under this even uncompressed.
MAX_BODY_BYTES = 64 * 1024 * 1024


#: Assets are authored as separate files and stitched into one response at startup.
#: The page must still arrive as a single self-contained document — it has to render
#: during the outage it is explaining, and a page that fetches to describe the network
#: goes blank exactly when it is needed — but nobody should have to maintain a
#: thousand-line file to get that.
#:
#: Order is dependency order, not preference: these become one script with a shared
#: scope, so a module must appear after everything it calls at load time. `app.js`
#: wires everything and therefore goes last.
STYLES = ("css/app.css",)
SCRIPTS = (
    "core/format.js",
    "core/store.js",
    "core/metrics.js",
    "core/chart.js",
    "core/scene.js",
    "views/dashboard.js",
    "views/detail.js",
    "views/speedtests.js",
    "views/path.js",
    "views/spectrum.js",
    "views/network.js",
    "views/usage.js",
    "views/rules.js",
    "app.js",
)


@lru_cache(maxsize=1)
def _dashboard_html() -> bytes:
    """The shell with its assets inlined, built once per process.

    Cached rather than re-read: the page is static, and a dashboard being refreshed
    every fifteen seconds should not touch the disk to say the same thing again.
    """
    assets = resources.files("netpulse.web") / "assets"
    page = (assets / "index.html").read_text()

    def bundle(names: tuple[str, ...]) -> str:
        # Each file keeps a banner comment naming it, so a stack trace in the browser
        # still points at a file somebody can open.
        return "\n".join(
            f"/* ---- {name} ---- */\n" + (assets / name).read_text() for name in names
        )

    page = page.replace("{{styles}}", bundle(STYLES))
    page = page.replace("{{scripts}}", bundle(SCRIPTS))
    return page.encode()


def make_handler(api: Api, guard: Guard | None = None) -> type[BaseHTTPRequestHandler]:
    checks = guard or Guard()

    class Handler(BaseHTTPRequestHandler):
        def _authorised(self, path: str) -> bool:
            """Every path sits behind the password once one is set, reads included.

            A stranger who can list the devices on a network, watch its throughput and
            read its outage history has learned the shape of somebody's day and when
            their house is empty. That is not a smaller problem than the writes, and
            splitting the endpoints into public and private would invite exactly the
            mistake of putting a new one on the wrong side.
            """
            header = self.headers.get("Authorization", "")
            if path == "/api/ingest":
                return checks.allows_agent(header)
            if checks.allows_person(header):
                return True
            self.send_response(401)
            # Without this a browser cannot prompt; it renders a blank page instead.
            self.send_header("WWW-Authenticate", 'Basic realm="NetPulse"')
            self.send_header("Content-Length", "0")
            self.end_headers()
            return False

        def do_GET(self) -> None:
            url = urlparse(self.path)
            if not self._authorised(url.path):
                return
            params = {key: values[0] for key, values in parse_qs(url.query).items()}
            try:
                if url.path == "/":
                    self._send(200, _dashboard_html(), "text/html; charset=utf-8")
                elif url.path == "/api/overview":
                    self._json(api.overview())
                elif url.path == "/api/history":
                    self._json(
                        api.history(
                            params.get("source", ""),
                            params.get("metric", ""),
                            int(params.get("minutes", 60)),
                            min(500, int(params.get("buckets", 90))),
                        )
                    )
                elif url.path == "/api/events":
                    self._json(api.events(int(params.get("minutes", 1440))))
                elif url.path == "/api/insights":
                    self._json(api.insights(params.get("source", "")))
                elif url.path == "/api/distribution":
                    self._json(
                        api.distribution(
                            params.get("source", ""),
                            params.get("metric", ""),
                            int(params.get("minutes", "60")),
                        )
                    )
                elif url.path == "/api/allowance":
                    self._json(api.allowance(params.get("source", "")))
                elif url.path == "/metrics":
                    self._send(200, api.prometheus().encode(), "text/plain; version=0.0.4")
                elif url.path == "/api/uptime":
                    self._json(api.uptime(params.get("source", ""), float(params.get("days", "7"))))
                elif url.path == "/api/export":
                    body_csv, body_json = api.export(
                        params.get("source", ""),
                        int(params.get("minutes", "1440")),
                        int(params.get("buckets", "1440")),
                        [m for m in params.get("metrics", "").split(",") if m],
                    )
                    wants_json = params.get("format", "csv") == "json"
                    stamp = params.get("source", "netpulse")
                    self._send(
                        200,
                        (body_json if wants_json else body_csv).encode(),
                        "application/json" if wants_json else "text/csv",
                        # The browser saves it rather than rendering a wall of numbers.
                        extra={
                            "Content-Disposition": f'attachment; filename="netpulse-{stamp}.'
                            f'{"json" if wants_json else "csv"}"'
                        },
                    )
                elif url.path == "/api/speedtests":
                    self._json(
                        api.speedtest_history(
                            params.get("source", ""), float(params.get("days", "30"))
                        )
                    )
                elif url.path == "/api/spectrum":
                    self._json(
                        api.spectrum(
                            params.get("source", ""),
                            int(params.get("minutes", "60")),
                            int(params.get("slices", "48")),
                        )
                    )
                elif url.path == "/api/network":
                    self._json(
                        api.network(params.get("source", ""), float(params.get("hours", "24")))
                    )
                elif url.path == "/api/usage":
                    self._json(api.usage(params.get("source", ""), int(params.get("days", "14"))))
                elif url.path == "/api/apps":
                    self._json(api.apps())
                elif url.path == "/api/rules":
                    self._json(api.rules(params.get("source", "")))
                elif url.path == "/api/agents":
                    self._json(api.agents())
                elif url.path == "/api/quality":
                    self._json(api.quality(params.get("source", "")))
                elif url.path == "/api/devices":
                    self._json(
                        api.devices(params.get("source", ""), float(params.get("hours", 24)))
                    )
                elif url.path == "/api/stream":
                    self._sse()
                else:
                    self._json({"error": "not found"}, 404)
            except BrokenPipeError:
                pass
            except Exception as exc:
                self._json({"error": str(exc)}, 500)

        def do_POST(self) -> None:
            url = urlparse(self.path)
            if not self._authorised(url.path):
                return
            params = {key: values[0] for key, values in parse_qs(url.query).items()}
            try:
                if url.path == "/api/block":
                    self._json(
                        api.block(
                            params.get("source", ""),
                            params.get("mac", ""),
                            params.get("on", "1") == "1",
                            params.get("label", ""),
                        )
                    )
                elif url.path == "/api/path":
                    self._json(api.path(params.get("target", "1.1.1.1")))
                elif url.path == "/api/discover":
                    self._json(api.discover_routers())
                elif url.path == "/api/sources":
                    self._json(
                        api.add_source(
                            params.get("kind", ""), params.get("url", ""), params.get("name", "")
                        )
                    )
                elif url.path == "/api/speedtest":
                    # Deliberately synchronous and deliberately POST-only: it moves real
                    # data on what may be a metered plan, so only an explicit click or
                    # curl -X POST triggers it, never a page load.
                    self._json(api.speedtest(params.get("source", "")))
                elif url.path == "/api/ingest":
                    self._json(api.ingest(self._body()))
                else:
                    self._json({"error": "not found"}, 404)
            except Exception as exc:
                self._json({"error": str(exc)}, 500)

        def _body(self) -> dict[str, Any]:
            """The request body, decompressed if it arrived that way.

            Bounded before it is read: an agent's push is the one endpoint that accepts a
            large body, and `Content-Length` is a claim by whoever is calling rather than
            a fact. Reading it unbounded would let one request ask this process to
            allocate whatever it liked.
            """
            declared = int(self.headers.get("Content-Length") or 0)
            if declared <= 0 or declared > MAX_BODY_BYTES:
                raise ValueError(f"body must be between 1 and {MAX_BODY_BYTES} bytes")
            raw = self.rfile.read(declared)
            if self.headers.get("Content-Encoding", "").lower() == "gzip":
                # Bounded again after inflating, because the ratio is the caller's choice
                # too: a few kilobytes of zeroes expand to gigabytes if simply trusted.
                with gzip.GzipFile(fileobj=io.BytesIO(raw)) as unzipped:
                    raw = unzipped.read(MAX_BODY_BYTES + 1)
                if len(raw) > MAX_BODY_BYTES:
                    raise ValueError("compressed body expands beyond the limit")
            parsed = json.loads(raw)
            if not isinstance(parsed, dict):
                raise ValueError("body must be a JSON object")
            return parsed

        def _sse(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            stream = api.stream()
            try:
                while True:
                    try:
                        message = stream.get(timeout=15)
                        self.wfile.write(f"data: {message}\n\n".encode())
                    except queue.Empty:
                        self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                api.unstream(stream)

        def _json(self, payload: dict[str, Any], status: int = 200) -> None:
            self._send(status, json.dumps(payload).encode(), "application/json")

        def _send(
            self,
            status: int,
            body: bytes,
            content_type: str,
            extra: dict[str, str] | None = None,
        ) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            for header, value in (extra or {}).items():
                self.send_header(header, value)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_: Any) -> None:
            return None

    return Handler


def serve(api: Api, port: int, bind: str = "127.0.0.1") -> ThreadingHTTPServer:
    """Bound to localhost by design: readings of your home network are yours. Bind
    0.0.0.0 explicitly (config `bind`) to open it to your LAN, e.g. for a phone.

    Opening it requires a password, and the check happens here rather than at the first
    request: a server that starts and then refuses everyone is a working deployment with
    a locked door, while a server that starts and lets everyone in is a mistake nobody
    finds until it matters. `Guard.from_env` raises rather than returning something
    permissive, so there is no path through this function that ends in an open port
    over somebody's router.
    """
    server = ThreadingHTTPServer((bind, port), make_handler(api, Guard.from_env(bind)))
    server.daemon_threads = True
    return server
