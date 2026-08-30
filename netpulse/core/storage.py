from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from netpulse.core.clock import Clock, utcnow
from netpulse.core.model import (
    Agg,
    Coverage,
    DeviceSeen,
    Event,
    EventKind,
    Severity,
    agg_for,
)

_TS = "%Y-%m-%dT%H:%M:%S.%f+00:00"

#: Raw samples older than this fold into per-minute sufficient statistics. The stats
#: compose exactly — max of maxes, sum/count for means — so nothing a chart can honestly
#: draw is lost, and a year of history stays queryable in milliseconds.
RAW_RETENTION = timedelta(days=7)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS samples (
    at TEXT NOT NULL,
    source TEXT NOT NULL,
    metric TEXT NOT NULL,
    value REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS samples_lookup ON samples (source, metric, at);
CREATE TABLE IF NOT EXISTS rollup (
    minute TEXT NOT NULL,
    source TEXT NOT NULL,
    metric TEXT NOT NULL,
    count INTEGER NOT NULL,
    sum REAL NOT NULL,
    min REAL NOT NULL,
    max REAL NOT NULL,
    PRIMARY KEY (source, metric, minute)
);
CREATE TABLE IF NOT EXISTS texts (
    at TEXT NOT NULL,
    source TEXT NOT NULL,
    metric TEXT NOT NULL,
    value TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS texts_lookup ON texts (source, metric, at);
CREATE TABLE IF NOT EXISTS devices (
    at TEXT NOT NULL,
    source TEXT NOT NULL,
    mac TEXT NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    ip TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS devices_lookup ON devices (source, mac, at);
CREATE TABLE IF NOT EXISTS usage (
    at      TEXT NOT NULL,
    source  TEXT NOT NULL,
    -- "app" for a process on this machine, "device" for a client the router reports.
    kind    TEXT NOT NULL,
    key     TEXT NOT NULL,
    down    REAL NOT NULL,
    up      REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS usage_at ON usage (source, kind, at);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    kind TEXT NOT NULL,
    severity TEXT NOT NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    detail TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS events_open ON events (source, kind, ended_at);
"""


def _ts(value: datetime) -> str:
    return value.astimezone(UTC).strftime(_TS)


def _dt(value: str) -> datetime:
    return datetime.strptime(value, _TS).replace(tzinfo=UTC)


def _minute(value: str) -> str:
    """The minute a sample timestamp falls in, as a rollup key."""
    return value[:17] + "00.000000+00:00"


@dataclass
class _Stat:
    """Sufficient statistics for one bucket. Raw values and rollup rows merge alike."""

    count: int = 0
    sum: float = 0.0
    min: float = float("inf")
    max: float = float("-inf")

    def add(self, count: int, total: float, low: float, high: float) -> None:
        self.count += count
        self.sum += total
        self.min = min(self.min, low)
        self.max = max(self.max, high)

    def finish(self, how: Agg) -> float:
        if how is Agg.MAX:
            return self.max
        if how is Agg.MIN:
            return self.min
        if how is Agg.LAST:
            # LAST metrics are monotonic counters (data.month_down_bytes), so within any
            # window the newest value is also the largest; max preserves it exactly.
            return self.max
        return self.sum / self.count


def _event(row: sqlite3.Row) -> Event:
    return Event(
        id=row["id"],
        source=row["source"],
        kind=EventKind(row["kind"]),
        severity=Severity(row["severity"]),
        started_at=_dt(row["started_at"]),
        ended_at=_dt(row["ended_at"]) if row["ended_at"] else None,
        detail=row["detail"],
    )


class Store:
    """Append-only local history with an honest rollup ladder.

    A gap in recording stays a gap: bucketed reads return None for unsampled buckets,
    and :meth:`coverage` reports how much of a window was actually seen — across both
    raw samples and compacted minutes, so compaction never inflates or deflates it.
    """

    def __init__(self, path: str | Path = ":memory:", clock: Clock = utcnow):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._clock = clock
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        # executescript issues its own COMMIT, so schema setup stays outside _tx.
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        self._last_text: dict[tuple[str, str], str] = {}

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            self._conn.execute("COMMIT")

    # ------------------------------------------------------------------ writing

    def record(
        self,
        source: str,
        metrics: dict[str, float],
        texts: dict[str, str] | None = None,
        at: datetime | None = None,
    ) -> None:
        moment = _ts(at or self._clock())
        with self._tx() as conn:
            conn.executemany(
                "INSERT INTO samples (at, source, metric, value) VALUES (?, ?, ?, ?)",
                [(moment, source, metric, float(value)) for metric, value in metrics.items()],
            )
            # Texts change rarely (network type, operator, band); store transitions only,
            # so a week of LTE is one row rather than 120960.
            changed = [
                (moment, source, metric, value)
                for metric, value in (texts or {}).items()
                if self._last_text.get((source, metric)) != value
            ]
            if changed:
                conn.executemany(
                    "INSERT INTO texts (at, source, metric, value) VALUES (?, ?, ?, ?)", changed
                )
                for _, src, metric, value in changed:
                    self._last_text[(src, metric)] = value

    def record_devices(
        self, source: str, devices: list[DeviceSeen], at: datetime | None = None
    ) -> None:
        moment = _ts(at or self._clock())
        with self._tx() as conn:
            conn.executemany(
                "INSERT INTO devices (at, source, mac, name, ip) VALUES (?, ?, ?, ?, ?)",
                [(moment, source, d.mac, d.name, d.ip) for d in devices],
            )

    def record_usage(
        self,
        source: str,
        kind: str,
        entries: list[tuple[str, float, float]],
        at: datetime | None = None,
    ) -> None:
        """Traffic attributed to named things, as an interval — not a running total.

        Deltas rather than counters on purpose: a counter would have to be interpreted
        against the previous row for its process or device, and a restart, a rename or
        a reboot would each silently corrupt that. An interval is true on its own.
        """
        moment = _ts(at or self._clock())
        rows = [(moment, source, kind, key, down, up) for key, down, up in entries if down or up]
        if not rows:
            return
        with self._tx() as conn:
            conn.executemany(
                "INSERT INTO usage (at, source, kind, key, down, up) VALUES (?, ?, ?, ?, ?, ?)",
                rows,
            )

    def usage_by_key(
        self, source: str, kind: str, since: datetime, until: datetime | None = None
    ) -> list[tuple[str, float, float]]:
        """Total per app or per device over a window, busiest first."""
        clause = "AND at < ?" if until is not None else ""
        parameters: tuple[object, ...] = (source, kind, _ts(since))
        if until is not None:
            parameters += (_ts(until),)
        with self._lock:
            rows = self._conn.execute(
                "SELECT key, SUM(down) AS down, SUM(up) AS up FROM usage "
                f"WHERE source = ? AND kind = ? AND at >= ? {clause} "
                "GROUP BY key ORDER BY (SUM(down) + SUM(up)) DESC",
                parameters,
            ).fetchall()
        return [(row["key"], row["down"], row["up"]) for row in rows]

    def usage_by_day(
        self, source: str, kind: str, since: datetime, until: datetime | None = None
    ) -> list[tuple[str, float, float]]:
        """Total per calendar day, oldest first.

        Grouped on the stored timestamp's date, which is UTC. A day boundary that does
        not match the viewer's midnight is a real limitation and is stated where the
        figures are shown, rather than corrected by guessing a timezone.
        """
        clause = "AND at < ?" if until is not None else ""
        parameters: tuple[object, ...] = (source, kind, _ts(since))
        if until is not None:
            parameters += (_ts(until),)
        with self._lock:
            rows = self._conn.execute(
                "SELECT substr(at, 1, 10) AS day, SUM(down) AS down, SUM(up) AS up "
                f"FROM usage WHERE source = ? AND kind = ? AND at >= ? {clause} "
                "GROUP BY day ORDER BY day",
                parameters,
            ).fetchall()
        return [(row["day"], row["down"], row["up"]) for row in rows]

    # ------------------------------------------------------------------ compaction

    def compact(self, now: datetime | None = None) -> int:
        """Fold raw samples older than the retention window into per-minute statistics.

        Fold and delete happen in one transaction, so a crash cannot double-count a
        minute: either both landed or neither did. Returns raw rows folded.
        """
        cutoff = _ts((now or self._clock()) - RAW_RETENTION)
        with self._tx() as conn:
            aged = conn.execute(
                "SELECT source, metric, at, value FROM samples WHERE at < ? ORDER BY at",
                (cutoff,),
            ).fetchall()
            if not aged:
                return 0

            folded: dict[tuple[str, str, str], _Stat] = {}
            for row in aged:
                key = (row["source"], row["metric"], _minute(row["at"]))
                folded.setdefault(key, _Stat()).add(1, row["value"], row["value"], row["value"])

            conn.executemany(
                "INSERT INTO rollup (source, metric, minute, count, sum, min, max) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (source, metric, minute) DO UPDATE SET "
                "count = count + excluded.count, sum = sum + excluded.sum, "
                "min = MIN(min, excluded.min), max = MAX(max, excluded.max)",
                [
                    (source, metric, minute, stat.count, stat.sum, stat.min, stat.max)
                    for (source, metric, minute), stat in folded.items()
                ],
            )
            conn.execute("DELETE FROM samples WHERE at < ?", (cutoff,))
            # Old per-poll device sightings coarsen to one per minute the same way.
            conn.execute(
                "DELETE FROM devices WHERE rowid NOT IN ("
                "  SELECT MIN(rowid) FROM devices WHERE at < ?"
                "  GROUP BY source, mac, substr(at, 1, 17)"
                ") AND at < ?",
                (cutoff, cutoff),
            )
            return len(aged)

    # ------------------------------------------------------------------ reading

    def latest(self, source: str) -> dict[str, tuple[datetime, float]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT metric, at, value FROM samples WHERE source = ? "
                "AND at = (SELECT MAX(at) FROM samples s2 WHERE s2.source = samples.source "
                "AND s2.metric = samples.metric)",
                (source,),
            ).fetchall()
        return {row["metric"]: (_dt(row["at"]), row["value"]) for row in rows}

    def latest_texts(self, source: str) -> dict[str, str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT metric, value FROM texts WHERE source = ? "
                "AND at = (SELECT MAX(at) FROM texts t2 WHERE t2.source = texts.source "
                "AND t2.metric = texts.metric)",
                (source,),
            ).fetchall()
        return {row["metric"]: row["value"] for row in rows}

    def devices(self, source: str, since: datetime) -> list[dict[str, object]]:
        """Devices seen on the network since ``since``, newest sighting first."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT mac, MAX(at) AS last_seen, "
                "  (SELECT name FROM devices d2 WHERE d2.source = devices.source "
                "   AND d2.mac = devices.mac AND d2.name != '' "
                "   ORDER BY at DESC LIMIT 1) AS name, "
                "  (SELECT ip FROM devices d3 WHERE d3.source = devices.source "
                "   AND d3.mac = devices.mac ORDER BY at DESC LIMIT 1) AS ip "
                "FROM devices WHERE source = ? AND at >= ? "
                "GROUP BY mac ORDER BY last_seen DESC",
                (source, _ts(since)),
            ).fetchall()
        return [
            {
                "mac": row["mac"],
                "name": row["name"] or "",
                "ip": row["ip"] or "",
                "last_seen": _dt(row["last_seen"]).isoformat(),
            }
            for row in rows
        ]

    def history(
        self,
        source: str,
        metric: str,
        since: datetime,
        until: datetime,
        buckets: int,
        agg: Agg | None = None,
    ) -> list[tuple[datetime, float | None]]:
        """``buckets`` evenly-spaced points; None where nothing was sampled.

        Reads raw samples and compacted minutes together, so a chart spanning the
        retention boundary is seamless. The aggregation comes from the metric registry:
        latency buckets by max so a spike survives downsampling — and survives
        compaction, because the rollup keeps the max.
        """
        how = agg or agg_for(metric)
        span = (until - since).total_seconds()
        if span <= 0 or buckets <= 0:
            return []
        width = span / buckets

        def bucket_of(stamp: str) -> int:
            index = int((_dt(stamp) - since).total_seconds() / width)
            return min(index, buckets - 1)

        stats: dict[int, _Stat] = {}
        with self._lock:
            for row in self._conn.execute(
                "SELECT at, value FROM samples WHERE source = ? AND metric = ? "
                "AND at >= ? AND at < ?",
                (source, metric, _ts(since), _ts(until)),
            ):
                stats.setdefault(bucket_of(row["at"]), _Stat()).add(
                    1, row["value"], row["value"], row["value"]
                )
            for row in self._conn.execute(
                "SELECT minute, count, sum, min, max FROM rollup "
                "WHERE source = ? AND metric = ? AND minute >= ? AND minute < ?",
                (source, metric, _ts(since), _ts(until)),
            ):
                stats.setdefault(bucket_of(row["minute"]), _Stat()).add(
                    row["count"], row["sum"], row["min"], row["max"]
                )

        out: list[tuple[datetime, float | None]] = []
        for index in range(buckets):
            start = since + (until - since) * (index / buckets)
            stat = stats.get(index)
            out.append((start, stat.finish(how) if stat else None))
        return out

    def coverage(
        self, source: str, since: datetime, until: datetime, interval_s: float
    ) -> Coverage:
        """Poll moments seen vs expected, across raw and compacted history alike.

        `up` is written on every poll, success or failure, so it is the heartbeat; a
        compacted minute contributes the polls it folded, so compaction cannot change
        what a window claims was recorded.
        """
        with self._lock:
            raw = self._conn.execute(
                "SELECT COUNT(DISTINCT at) AS n FROM samples WHERE source = ? "
                "AND metric = 'up' AND at >= ? AND at < ?",
                (source, _ts(since), _ts(until)),
            ).fetchone()["n"]
            rolled = self._conn.execute(
                "SELECT COALESCE(SUM(count), 0) AS n FROM rollup WHERE source = ? "
                "AND metric = 'up' AND minute >= ? AND minute < ?",
                (source, _ts(since), _ts(until)),
            ).fetchone()["n"]
        expected = max(1, int((until - since).total_seconds() / interval_s))
        return Coverage(sampled=raw + rolled, expected=expected)

    def values(
        self, source: str, metric: str, since: datetime, until: datetime | None = None
    ) -> list[float]:
        """Raw values in order over [since, until), or to the latest when until is None.

        Raw only, on purpose: a rollup keeps min/mean/max, and pretending percentiles
        out of those would be invention. The window is half-open like every other range
        here so adjacent windows compose without double-counting — which is why `until`
        can be omitted: an odometer wants its newest position included, and asking for
        "up to now" would drop a reading taken this very instant.
        """
        clause = "AND at < ?" if until is not None else ""
        parameters: tuple[object, ...] = (source, metric, _ts(since))
        if until is not None:
            parameters += (_ts(until),)
        with self._lock:
            rows = self._conn.execute(
                "SELECT value FROM samples WHERE source = ? AND metric = ? "
                f"AND at >= ? {clause} ORDER BY at",
                parameters,
            ).fetchall()
        return [row["value"] for row in rows]

    def samples_span(
        self, source: str, metric: str, since: datetime, until: datetime | None = None
    ) -> tuple[datetime, datetime] | None:
        """First and last time a metric was recorded, or None if never.

        The span a figure can speak for: a rate divided by wall-clock time would claim
        a period nobody was watching. Unbounded above like `values`, because the newest
        reading is exactly the end of the span and a half-open window would drop it.
        """
        clause = "AND at < ?" if until is not None else ""
        parameters: tuple[object, ...] = (source, metric, _ts(since))
        if until is not None:
            parameters += (_ts(until),)
        with self._lock:
            row = self._conn.execute(
                "SELECT MIN(at) AS first, MAX(at) AS last FROM samples "
                f"WHERE source = ? AND metric = ? AND at >= ? {clause}",
                parameters,
            ).fetchone()
        if row is None or row["first"] is None:
            return None
        return datetime.fromisoformat(row["first"]), datetime.fromisoformat(row["last"])

    def stamped(
        self, source: str, metric: str, since: datetime, until: datetime | None = None
    ) -> list[tuple[datetime, float]]:
        """Raw readings with their timestamps, oldest first.

        For metrics that are events rather than levels — a speed test happened at a
        moment, and when it happened is half the reading.
        """
        clause = "AND at < ?" if until is not None else ""
        parameters: tuple[object, ...] = (source, metric, _ts(since))
        if until is not None:
            parameters += (_ts(until),)
        with self._lock:
            rows = self._conn.execute(
                "SELECT at, value FROM samples WHERE source = ? AND metric = ? "
                f"AND at >= ? {clause} ORDER BY at",
                parameters,
            ).fetchall()
        return [(datetime.fromisoformat(row["at"]), row["value"]) for row in rows]

    def sources(self) -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT source FROM samples UNION SELECT source FROM rollup ORDER BY source"
            )
            return [row["source"] for row in rows.fetchall()]

    # ------------------------------------------------------------------ events

    def open_event(
        self,
        source: str,
        kind: EventKind,
        severity: Severity,
        detail: str,
        at: datetime | None = None,
    ) -> int:
        with self._tx() as conn:
            cursor = conn.execute(
                "INSERT INTO events (source, kind, severity, started_at, detail) "
                "VALUES (?, ?, ?, ?, ?)",
                (source, kind.value, severity.value, _ts(at or self._clock()), detail),
            )
            return int(cursor.lastrowid or 0)

    def close_event(self, event_id: int, at: datetime | None = None) -> None:
        with self._tx() as conn:
            conn.execute(
                "UPDATE events SET ended_at = ? WHERE id = ? AND ended_at IS NULL",
                (_ts(at or self._clock()), event_id),
            )

    def events_overlapping(self, source: str, since: datetime, until: datetime) -> list[Event]:
        """Every event whose span touches the window, not merely those that began in it.

        An outage that started before the window — or is still running — is exactly the
        one a report must not miss: asking for "the last 24 hours" during a day-long
        outage would otherwise find no events at all and report perfect uptime.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM events WHERE source = ? AND started_at < ? "
                "AND (ended_at IS NULL OR ended_at >= ?) ORDER BY started_at",
                (source, _ts(until), _ts(since)),
            ).fetchall()
        return [_event(row) for row in rows]

    def events(
        self,
        source: str | None = None,
        since: datetime | None = None,
        open_only: bool = False,
        limit: int = 200,
    ) -> list[Event]:
        clauses, params = ["1=1"], []
        if source is not None:
            clauses.append("source = ?")
            params.append(source)
        if since is not None:
            clauses.append("started_at >= ?")
            params.append(_ts(since))
        if open_only:
            clauses.append("ended_at IS NULL")
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM events WHERE {' AND '.join(clauses)} "
                "ORDER BY started_at DESC LIMIT ?",
                (*params, limit),
            ).fetchall()
        return [_event(row) for row in rows]

    def close(self) -> None:
        with self._lock:
            self._conn.close()
