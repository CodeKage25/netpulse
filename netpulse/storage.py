from __future__ import annotations

import sqlite3
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from netpulse.model import Agg, Coverage, Event, EventKind, Severity, agg_for

_TS = "%Y-%m-%dT%H:%M:%S.%f+00:00"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS samples (
    at TEXT NOT NULL,
    source TEXT NOT NULL,
    metric TEXT NOT NULL,
    value REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS samples_lookup ON samples (source, metric, at);
CREATE TABLE IF NOT EXISTS texts (
    at TEXT NOT NULL,
    source TEXT NOT NULL,
    metric TEXT NOT NULL,
    value TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS texts_lookup ON texts (source, metric, at);
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


def utcnow() -> datetime:
    return datetime.now(UTC)


class Store:
    """Append-only local history.

    A gap in recording stays a gap: bucketed reads return None for unsampled buckets, and
    :meth:`coverage` reports how much of a window was actually seen, so nothing downstream
    can honestly pretend otherwise.
    """

    def __init__(self, path: str | Path = ":memory:", clock: Callable[[], datetime] = utcnow):
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

        The aggregation comes from the metric registry: latency buckets by max so a spike
        survives downsampling instead of averaging away into a lie.
        """
        how = agg or agg_for(metric)
        span = (until - since).total_seconds()
        if span <= 0 or buckets <= 0:
            return []
        width = span / buckets
        with self._lock:
            rows = self._conn.execute(
                "SELECT at, value FROM samples WHERE source = ? AND metric = ? "
                "AND at >= ? AND at < ? ORDER BY at",
                (source, metric, _ts(since), _ts(until)),
            ).fetchall()

        filled: list[list[float]] = [[] for _ in range(buckets)]
        for row in rows:
            index = int((_dt(row["at"]) - since).total_seconds() / width)
            filled[min(index, buckets - 1)].append(row["value"])

        out: list[tuple[datetime, float | None]] = []
        for index, values in enumerate(filled):
            start = since + (until - since) * (index / buckets)
            if not values:
                out.append((start, None))
            elif how is Agg.MAX:
                out.append((start, max(values)))
            elif how is Agg.MIN:
                out.append((start, min(values)))
            elif how is Agg.LAST:
                out.append((start, values[-1]))
            else:
                out.append((start, sum(values) / len(values)))
        return out

    def coverage(
        self, source: str, since: datetime, until: datetime, interval_s: float
    ) -> Coverage:
        """Distinct poll moments seen vs expected. `up` is written on every poll, success
        or failure, so it is the heartbeat this counts."""
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(DISTINCT at) AS n FROM samples WHERE source = ? "
                "AND metric = 'up' AND at >= ? AND at < ?",
                (source, _ts(since), _ts(until)),
            ).fetchone()
        expected = max(1, int((until - since).total_seconds() / interval_s))
        return Coverage(sampled=row["n"], expected=expected)

    def sources(self) -> list[str]:
        with self._lock:
            rows = self._conn.execute("SELECT DISTINCT source FROM samples ORDER BY source")
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
        return [
            Event(
                id=row["id"],
                source=row["source"],
                kind=EventKind(row["kind"]),
                severity=Severity(row["severity"]),
                started_at=_dt(row["started_at"]),
                ended_at=_dt(row["ended_at"]) if row["ended_at"] else None,
                detail=row["detail"],
            )
            for row in rows
        ]

    def close(self) -> None:
        with self._lock:
            self._conn.close()
