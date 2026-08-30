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
import platform
import queue
import threading
from contextlib import suppress
from datetime import datetime, timedelta
from typing import Any

from netpulse.analysis.allowance import Plan
from netpulse.analysis.allowance import assess as assess_allowance
from netpulse.analysis.allowance import by_day as usage_by_day
from netpulse.analysis.apps import AppMonitor
from netpulse.analysis.export import prometheus, series, to_csv, to_json, uptime_report
from netpulse.analysis.insights import diagnose
from netpulse.analysis.path import analyse, trace
from netpulse.analysis.quality import assess
from netpulse.analysis.speedtest import TestHostUnavailable, run_speedtest
from netpulse.config import SourceConfig
from netpulse.core.clock import Clock, utcnow
from netpulse.core.host import local_macs
from netpulse.core.model import Agg
from netpulse.core.storage import Store
from netpulse.monitor import Collector
from netpulse.sources import Blocker, build
from netpulse.sources.discover import discover

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
        #: Built on first use — sampling processes costs nothing until someone looks.
        self._apps: AppMonitor | None = None
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

    def path(self, target: str) -> dict[str, Any]:
        """Trace the path and attribute the delay. Runs on request only.

        Never on the poll loop: a traceroute takes tens of seconds, and a call that
        long inside a five-second cycle is how a monitor invents the outage it then
        writes down.
        """
        verdict = analyse(trace(target or "1.1.1.1"))
        return {
            "where": verdict.where,
            "summary": verdict.summary,
            "detail": verdict.detail,
            "culprit": verdict.culprit.number if verdict.culprit else None,
            "hops": [
                {"n": hop.number, "host": hop.host, "rtt_ms": hop.rtt_ms, "silent": hop.silent}
                for hop in verdict.hops
            ],
        }

    def speedtest_history(self, source: str, days: float) -> dict[str, Any]:
        """Past runs, newest first, with the trend they add up to.

        Dishylink has no speed-test history at all — its result lives in component
        state and is gone on the next render. A single number tells you what the link
        did once; the useful question is whether it is getting worse.
        """
        now = self._clock()
        since = now - timedelta(days=days)
        downs = self.store.stamped(source, "speedtest.down_bytes_s", since)
        ups = dict(self.store.stamped(source, "speedtest.up_bytes_s", since))
        runs = [
            {
                "at": at.isoformat(),
                "down_mbps": round(value * 8 / 1e6, 1),
                "up_mbps": round(ups[at] * 8 / 1e6, 1) if at in ups else None,
            }
            for at, value in reversed(downs)
        ]
        # A trend needs enough runs to mean something; two points are a line through
        # noise, not a direction.
        trend = None
        if len(downs) >= 4:
            half = len(downs) // 2
            older = sum(value for _, value in downs[:half]) / half
            newer = sum(value for _, value in downs[half:]) / (len(downs) - half)
            if older > 0:
                trend = round(100 * (newer - older) / older, 1)
        return {"runs": runs, "count": len(runs), "trend_pct": trend}

    def spectrum(self, source: str, minutes: int, slices: int = 48) -> dict[str, Any]:
        """The carrier stack over time — where the radio actually is.

        Each slice is one bucket; each carrier within it carries its true centre
        frequency and real bandwidth, so the view plots measurement rather than
        decoration. A slice nobody recorded comes back empty rather than repeating the
        one before it, because a spectrum that quietly holds its last shape across a
        gap is claiming the radio did too.
        """
        now = self._clock()
        since = now - timedelta(minutes=minutes)
        present = [m for m in self.store.latest(source) if m.startswith("radio.cc")]
        if not present:
            return {"slices": [], "carriers": 0, "supported": False}

        indices = sorted({int(m.split(".")[1][2:]) for m in present})
        fields = ("mhz", "bw_mhz", "band", "pci", "nr")
        series: dict[str, list[tuple[datetime, float | None]]] = {}
        times: list[datetime] = []
        for index in indices:
            for field in fields:
                metric = f"radio.cc{index}.{field}"
                points = self.store.history(source, metric, since, now, slices, Agg.LAST)
                series[metric] = points
                if not times:
                    times = [at for at, _ in points]

        sliced: list[dict[str, Any]] = []
        for position, at in enumerate(times):
            carriers = []
            for index in indices:
                mhz = series[f"radio.cc{index}.mhz"][position][1]
                if mhz is None:
                    continue  # this carrier was not present in this slice
                width = series[f"radio.cc{index}.bw_mhz"][position][1]
                band = series[f"radio.cc{index}.band"][position][1]
                nr = series[f"radio.cc{index}.nr"][position][1]
                carriers.append(
                    {
                        "mhz": mhz,
                        "bw_mhz": width,
                        "band": int(band) if band is not None else None,
                        "pci": None
                        if series[f"radio.cc{index}.pci"][position][1] is None
                        else int(series[f"radio.cc{index}.pci"][position][1] or 0),
                        "nr": bool(nr),
                    }
                )
            sliced.append({"at": at.isoformat(), "carriers": carriers})

        latest = self.store.latest(source)
        return {
            "supported": True,
            "slices": sliced,
            "carriers": int(latest.get("radio.carriers", (now, 0))[1]),
            "aggregate_mhz": latest.get("radio.aggregate_mhz", (now, 0))[1]
            if "radio.aggregate_mhz" in latest
            else None,
            "signal": {
                "lte_rsrp": latest["signal.rsrp_dbm"][1] if "signal.rsrp_dbm" in latest else None,
                "lte_sinr": latest["signal.sinr_db"][1] if "signal.sinr_db" in latest else None,
                "nr_rsrp": latest["signal.rsrp_5g_dbm"][1]
                if "signal.rsrp_5g_dbm" in latest
                else None,
                "nr_sinr": latest["signal.sinr_5g_db"][1]
                if "signal.sinr_5g_db" in latest
                else None,
            },
        }

    def network(self, source: str, hours: float = 24) -> dict[str, Any]:
        """Devices on the network, and what is knowable about each.

        Per-device byte counters are reported only where the router actually publishes
        them. The ZLT firmware returns zero for every client — verified under load — so
        this says "not reported" rather than showing a figure that is always nought.
        """
        adapter = self.collector.adapter(source)
        blocked: list[str] = []
        can_block = isinstance(adapter, Blocker)
        if can_block:
            with suppress(Exception):
                blocked = adapter.blocked()  # type: ignore[union-attr]

        denied = {mac.upper() for mac in blocked}
        unseen = set(denied)
        mine = local_macs()
        # This machine is the one device NetPulse can measure completely, because it is
        # running on it. Everything else depends on what the router chooses to publish.
        measured = self._host_usage()

        devices = []
        for device in self.store.devices(source, self._clock() - timedelta(hours=hours)):
            mac = str(device["mac"]).upper()
            unseen.discard(mac)
            is_self = mac in mine
            devices.append(
                {
                    **device,
                    "blocked": mac in denied,
                    "self": is_self,
                    # Router-reported counters win where they exist; otherwise this
                    # machine supplies its own, and nothing is invented for the rest.
                    "down_bytes": device.get("rx_bytes")
                    if device.get("rx_bytes") is not None
                    else (measured[0] if is_self else None),
                    "up_bytes": device.get("tx_bytes")
                    if device.get("tx_bytes") is not None
                    else (measured[1] if is_self else None),
                    "measured_here": is_self and device.get("rx_bytes") is None,
                }
            )
        # A device can be blocked and therefore absent from the lease list; it must
        # still be listed, or unblocking it becomes impossible from here.
        devices += [
            {
                "mac": mac,
                "name": "",
                "ip": "",
                "first_seen": None,
                "last_seen": None,
                "blocked": True,
            }
            for mac in sorted(unseen)
        ]
        from_router = any(
            d.get("down_bytes") is not None and not d["measured_here"] for d in devices
        )
        return {
            "devices": devices,
            "can_block": can_block,
            # Three distinct states, because "we have no numbers" and "the router has
            # no numbers" send someone to different places.
            "per_device_bytes": "router"
            if from_router
            else ("self" if any(d["measured_here"] for d in devices) else "none"),
        }

    def _host_usage(self) -> tuple[float | None, float | None]:
        """This machine's traffic since the last sample, or (None, None) if unknown."""
        if self._apps is None:
            self._apps = AppMonitor()
            self._apps.poll()  # a baseline; the next call carries the delta
            return (None, None)
        if not self._apps.available:
            return (None, None)
        usage = self._apps.poll()
        return self._apps.totals(usage) if usage else (None, None)

    def block(self, source: str, mac: str, on: bool, label: str = "") -> dict[str, Any]:
        """Deny or allow one device. Only ever called from an explicit user action."""
        adapter = self.collector.adapter(source)
        if not isinstance(adapter, Blocker):
            return {"error": "this router cannot block devices"}
        try:
            if on:
                adapter.block(mac, label)
            else:
                adapter.unblock(mac)
        except Exception as exc:
            return {"error": str(exc)}
        return {"blocked": on, "mac": mac}

    def apps(self) -> dict[str, Any]:
        """What is using the connection, on the machine NetPulse runs on.

        The scope is in the answer, not buried in a footnote: a router sees flows, not
        applications, so this cannot speak for other devices and does not pretend to.
        """
        priming = self._apps is None
        if self._apps is None:
            self._apps = AppMonitor()
            # Counters are cumulative, so the first sample is a baseline and nothing
            # more. Saying "measuring" is honest; an empty list would read as "nothing
            # is using your connection", which is a different and wrong claim.
            self._apps.poll()
        if not self._apps.available:
            return {"available": False, "apps": [], "host": platform.node()}
        return {
            "available": True,
            "priming": priming,
            "host": platform.node(),
            "apps": [
                {
                    "name": app.name,
                    "down_bytes": app.down_bytes,
                    "up_bytes": app.up_bytes,
                    "system": app.system,
                }
                for app in self._apps.poll()
                if app.down_bytes is not None
            ][:24],
        }

    def usage(self, source: str, days: int = 14) -> dict[str, Any]:
        """Data usage broken down by day, by application and by device.

        Three answers to three different questions, kept apart because they are
        measured by different things and will not sum to each other.
        """
        now = self._clock()
        since = now - timedelta(days=days - 1)
        start_of_day = since.replace(hour=0, minute=0, second=0, microsecond=0)

        daily = [
            {
                "day": day.isoformat(),
                "bytes": used,
                "coverage": round(coverage, 3),
            }
            for day, used, coverage in usage_by_day(self.store, source, start_of_day, now)
        ]
        return {
            "days": daily,
            "apps": [
                {"key": key, "down": down, "up": up}
                for key, down, up in self.store.usage_by_key(source, "app", start_of_day)[:20]
            ],
            "devices": [
                {"key": key, "down": down, "up": up}
                for key, down, up in self.store.usage_by_key(source, "device", start_of_day)[:20]
            ],
            "apps_by_day": [
                {"day": day, "down": down, "up": up}
                for day, down, up in self.store.usage_by_day(source, "app", start_of_day)
            ],
            "host": platform.node(),
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
        try:
            result = run_speedtest(self.store, source)
        except TestHostUnavailable as exc:
            # Deliberately not "the speed test failed": the connection carried the
            # refusal perfectly well, and saying otherwise would blame the wrong thing.
            return {"error": f"No measurement host would answer ({exc}). Your connection is fine."}
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
