from __future__ import annotations

import json
import queue
import threading
from collections.abc import Callable
from contextlib import suppress
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from typing import Any
from urllib.parse import parse_qs, urlparse

from netpulse.adapters import build
from netpulse.config import SourceConfig
from netpulse.discover import discover
from netpulse.insights import diagnose
from netpulse.monitor import Collector
from netpulse.quality import assess
from netpulse.speedtest import run_speedtest
from netpulse.storage import Store, utcnow

SPARK_POINTS = 30


class Api:
    """Everything the dashboard asks, answered from the store — so history survives the
    collector and the page keeps working while a source is down."""

    def __init__(
        self,
        store: Store,
        collector: Collector,
        interval_s: float,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self.store = store
        self.collector = collector
        self.interval_s = interval_s
        self._clock = clock
        #: Called with each added SourceConfig so it survives a restart; None in tests.
        self.persist_sources: Any = None
        self._streams: list[queue.Queue[str]] = []
        self._streams_lock = threading.Lock()
        collector.subscribe(self._publish)

    # ------------------------------------------------------------------ endpoints

    def overview(self) -> dict[str, Any]:
        now = self._clock()
        status = self.collector.status()
        sources = []
        for name in self.collector.sources:
            latest = self.store.latest(name)
            spark_since = now - timedelta(minutes=10)
            sparklines = {
                metric: [
                    v for _, v in self.store.history(name, metric, spark_since, now, SPARK_POINTS)
                ]
                for metric in (
                    "latency.internet_ms",
                    "traffic.down_bytes_s",
                    "traffic.up_bytes_s",
                    "signal.sinr_db",
                    "signal.rsrp_dbm",
                    "loss.pct",
                )
                if metric in latest
            }
            up_at = latest.get("up")
            sources.append(
                {
                    "name": name,
                    "kind": status.get(name, {}).get("kind", "?"),
                    "up": bool(up_at and up_at[1] >= 1.0),
                    "last_seen": up_at[0].isoformat() if up_at else None,
                    "latest": {metric: value for metric, (_, value) in latest.items()},
                    "texts": self.store.latest_texts(name),
                    "sparklines": sparklines,
                    "coverage": self.store.coverage(
                        name, now - timedelta(hours=1), now, self.interval_s
                    ).fraction,
                    "uptime_24h": self._uptime(name, now),
                    "in_outage": status.get(name, {}).get("in_outage", False),
                }
            )
        return {"sources": sources, "now": now.isoformat()}

    def _uptime(self, source: str, now: datetime) -> float | None:
        """Fraction of *recorded* polls that were up — per poll, never per bucket.

        Bucketing first would let one bad minute zero a day (up buckets by MIN so charts
        show any failure), which is exactly the distortion a summary figure must not
        inherit. Unrecorded time is excluded, not assumed up; coverage says how much of
        the day this figure actually covers."""
        polls = self.store.values(source, "up", now - timedelta(hours=24), now)
        if not polls:
            return None
        return sum(1 for value in polls if value >= 1.0) / len(polls)

    def devices(self, source: str, hours: float) -> dict[str, Any]:
        return {
            "devices": self.store.devices(source, self._clock() - timedelta(hours=hours)),
        }

    def distribution(
        self, source: str, metric: str, minutes: int, bins: int = 32
    ) -> dict[str, Any]:
        """A histogram of raw values — the shape a mean hides.

        Computed from raw samples, never from buckets: a bucketed series has already
        thrown away the distribution, and counting its maxima would draw a picture of
        the worst case pretending to be the whole.
        """
        now = self._clock()
        values = self.store.values(source, metric, now - timedelta(minutes=minutes), now)
        if len(values) < 4:
            return {"bins": [], "count": len(values)}
        low, high = min(values), max(values)
        summary = {
            "count": len(values),
            "mean": sum(values) / len(values),
            "min": low,
            "max": high,
        }
        if high <= low:
            return {**summary, "bins": [{"lo": low, "hi": high, "count": len(values)}]}

        # One 1300ms spike over a 200ms norm would push every real sample into the first
        # bin and draw a picture of nothing. Bins span up to p95 and the last one absorbs
        # the tail, so the shape stays legible and the outliers are still counted.
        ordered = sorted(values)
        top = ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))]
        overflowing = top < high
        width = (top - low) / bins if top > low else 0.0
        if width <= 0:
            return {**summary, "bins": [{"lo": low, "hi": high, "count": len(values)}]}
        counts = [0] * bins
        for value in values:
            counts[min(bins - 1, int((value - low) / width))] += 1
        return {
            **summary,
            "overflowing": overflowing,
            "bins": [
                {"lo": low + i * width, "hi": low + (i + 1) * width, "count": count}
                for i, count in enumerate(counts)
            ],
        }

    def quality(self, source: str) -> dict[str, Any]:
        graded = assess(self.store, source, self._clock())
        if graded is None:
            return {"quality": None}
        return {
            "quality": {
                "score": graded.score,
                "grade": graded.grade,
                "p50_ms": graded.p50_ms,
                "p95_ms": graded.p95_ms,
                "p99_ms": graded.p99_ms,
                "jitter_ms": graded.jitter_ms,
                "loss_pct": graded.loss_pct,
            }
        }

    def discover_routers(self) -> dict[str, Any]:
        found = discover()
        # A router already being watched should say so, not offer itself again.
        existing = {
            getattr(self.collector._states[name].adapter, "base", None)
            for name in self.collector.sources
        }
        return {
            "found": [
                {
                    "kind": item.kind,
                    "url": item.url,
                    "label": item.label,
                    "supported": item.supported,
                    "note": item.note,
                    "already_watched": item.url in existing,
                }
                for item in found
            ]
        }

    def add_source(self, kind: str, url: str, name: str) -> dict[str, Any]:
        source = SourceConfig(name=name or kind, kind=kind, options={"url": url} if url else {})
        adapter = build(source.kind, source.name, source.options)
        self.collector.add_adapter(adapter)
        if self.persist_sources is not None:
            self.persist_sources(source)
        return {"added": source.name}

    def speedtest(self, source: str) -> dict[str, Any]:
        result = run_speedtest(self.store, source)
        return {
            "down_mbps": round(result.down_mbps, 1),
            "up_mbps": round(result.up_mbps, 1),
            "seconds": round(result.seconds, 1),
        }

    def history(self, source: str, metric: str, minutes: int, buckets: int) -> dict[str, Any]:
        now = self._clock()
        since = now - timedelta(minutes=minutes)
        series = self.store.history(source, metric, since, now, buckets)
        return {
            "source": source,
            "metric": metric,
            "times": [start.isoformat() for start, _ in series],
            "points": [value for _, value in series],
            "coverage": self.store.coverage(source, since, now, self.interval_s).fraction,
        }

    def events(self, minutes: int) -> dict[str, Any]:
        since = self._clock() - timedelta(minutes=minutes)
        return {
            "events": [
                {
                    "source": event.source,
                    "kind": event.kind.value,
                    "severity": event.severity.value,
                    "started_at": event.started_at.isoformat(),
                    "ended_at": event.ended_at.isoformat() if event.ended_at else None,
                    "detail": event.detail,
                }
                for event in self.store.events(since=since)
            ]
        }

    def insights(self, source: str) -> dict[str, Any]:
        return {
            "insights": [
                {
                    "rule": insight.rule,
                    "severity": insight.severity.value,
                    "title": insight.title,
                    "detail": insight.detail,
                    "evidence": insight.evidence,
                }
                for insight in diagnose(self.store, source, self._clock())
            ]
        }

    # ------------------------------------------------------------------ streaming

    def _publish(self, source: str, metrics: dict[str, float]) -> None:
        message = json.dumps({"source": source, "metrics": metrics})
        with self._streams_lock:
            listeners = list(self._streams)
        for stream in listeners:
            # A stalled browser loses ticks; it never blocks recording.
            with suppress(queue.Full):
                stream.put_nowait(message)

    def stream(self) -> queue.Queue[str]:
        q: queue.Queue[str] = queue.Queue(maxsize=64)
        with self._streams_lock:
            self._streams.append(q)
        return q

    def unstream(self, q: queue.Queue[str]) -> None:
        with self._streams_lock:
            if q in self._streams:
                self._streams.remove(q)


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
                if url.path == "/api/discover":
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

        def _send(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
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
