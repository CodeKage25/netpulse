"""HTTP transport: a stdlib server that hands `Api` answers to a browser.

Deliberately thin, and deliberately boring. It binds to the loopback address, serves
one page and a handful of JSON endpoints, and holds no logic of its own — the moment
routing starts making decisions about data, that decision belongs in `api.py`, where it
can be tested without a socket.

`Api` is re-exported here because it is what a caller wiring up a server needs, and
making them import from two modules to start one server would be pedantry.
"""

from __future__ import annotations

import json
import queue
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from typing import Any
from urllib.parse import parse_qs, urlparse

from netpulse.api import Api

__all__ = ["Api", "make_handler", "serve"]


def _dashboard_html() -> bytes:
    return (resources.files("netpulse") / "web" / "dashboard.html").read_bytes()


def make_handler(api: Api) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            url = urlparse(self.path)
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
            params = {key: values[0] for key, values in parse_qs(url.query).items()}
            try:
                if url.path == "/api/path":
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
                else:
                    self._json({"error": "not found"}, 404)
            except Exception as exc:
                self._json({"error": str(exc)}, 500)

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
    0.0.0.0 explicitly (config `bind`) to open it to your LAN, e.g. for a phone."""
    server = ThreadingHTTPServer((bind, port), make_handler(api))
    server.daemon_threads = True
    return server
