"""The query layer: every question the dashboard can ask, as plain Python.

No HTTP lives here. Each method returns JSON-shaped data, which is what lets the whole
API be tested by calling it — no sockets, no ports, no server to start — and what would
let a second transport reuse it unchanged.

One rule governs everything in this file: an answer says how much of its window it is
actually based on. A figure that quietly spans a gap is worse than no figure, because
it looks like knowledge.
"""

from __future__ import annotations

import json
import queue
import threading
from contextlib import suppress
from datetime import datetime, timedelta
from typing import Any

from netpulse.adapters import build
from netpulse.allowance import Plan
from netpulse.allowance import assess as assess_allowance
from netpulse.clock import Clock, utcnow
from netpulse.config import SourceConfig
from netpulse.discover import discover
from netpulse.export import prometheus, series, to_csv, to_json, uptime_report
from netpulse.insights import diagnose
from netpulse.monitor import Collector
from netpulse.quality import assess
from netpulse.speedtest import run_speedtest
from netpulse.storage import Store

SPARK_POINTS = 30


class Api:
    """Everything the dashboard asks, answered from the store — so history survives the
    collector and the page keeps working while a source is down."""

    def __init__(
        self,
        store: Store,
        collector: Collector,
        interval_s: float,
        clock: Clock = utcnow,
    ) -> None:
        self.store = store
        self.collector = collector
        self.interval_s = interval_s
        self._clock = clock
        #: Called with each added SourceConfig so it survives a restart; None in tests.
        self.persist_sources: Any = None
        #: The configured data plan, if any. Set by the runner; absent in tests.
        self.plan: Plan | None = None
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

    def allowance(self, source: str) -> dict[str, Any]:
        limit = self.plan.limit_bytes if self.plan else None
        reset_day = self.plan.reset_day if self.plan else 1
        result = assess_allowance(self.store, source, self._clock(), limit, reset_day)
        if result is None:
            return {"allowance": None}
        return {
            "allowance": {
                "used_bytes": result.used_bytes,
                "limit_bytes": result.limit_bytes,
                "fraction": result.fraction,
                "cycle_start": result.cycle_start.isoformat(),
                "cycle_end": result.cycle_end.isoformat(),
                "days_elapsed": round(result.days_elapsed, 2),
                "days_total": result.days_total,
                "rate_per_day": result.rate_per_day,
                "projected_bytes": result.projected_bytes,
                "exhausted_on": result.exhausted_on.isoformat() if result.exhausted_on else None,
                "on_track": result.on_track,
            }
        }

    def prometheus(self) -> str:
        latest: dict[str, dict[str, float]] = {}
        texts: dict[str, dict[str, str]] = {}
        coverage: dict[str, float] = {}
        now = self._clock()
        for name in self.collector.sources:
            # store.latest() carries (when, value) so callers can judge staleness; the
            # exposition format has no place for the timestamp.
            latest[name] = {metric: value for metric, (_, value) in self.store.latest(name).items()}
            texts[name] = self.store.latest_texts(name)
            coverage[name] = self.store.coverage(
                name, now - timedelta(hours=1), now, self.interval_s
            ).fraction
        return prometheus(latest, texts, coverage)

    def export(
        self, source: str, minutes: int, buckets: int, metrics: list[str]
    ) -> tuple[str, str]:
        """(header, rows) rendered as CSV — the shape most ISP disputes get settled in."""
        now = self._clock()
        since = now - timedelta(minutes=minutes)
        chosen = metrics or sorted(self.store.latest(source))  # every metric it reports
        header, rows = series(self.store, source, chosen, since, now, buckets)
        coverage = self.store.coverage(source, since, now, self.interval_s).fraction
        return to_csv(header, rows), to_json(header, rows, source, coverage)

    def uptime(self, source: str, days: float) -> dict[str, Any]:
        now = self._clock()
        return uptime_report(self.store, source, now - timedelta(days=days), now, self.interval_s)

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
        existing = self.collector.watched_urls()
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
