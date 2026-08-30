"""Accepting readings an agent measured somewhere this server cannot reach.

The router is on a private address inside somebody's house, so the measuring stays there
and only the answers travel. What arrives here is what the agent already wrote to its own
store, replayed with its original timestamps — this side is a copy, not the original, and
it is careful never to behave as though it were the one holding the stopwatch.

Three properties this has to have, because of what the link between the two is.

**Idempotent.** The push crosses the very connection being measured. It will fail
halfway, and the agent will send the batch again. Usage rows are intervals, so applying
one twice does not overwrite — it doubles. Every batch therefore carries the cursor it
ends at, this side remembers the highest it has applied per agent, and anything at or
below is dropped without being written.

**Namespaced by agent.** Every install calls its probe source `wan`. Two agents pushing
to one dashboard would silently interleave two different houses' readings into one
series, which is worse than either of them being missing. Sources arrive prefixed.

**Bounded.** A batch is capped and a malformed row is skipped rather than aborting its
batch, because the alternative is one bad row wedging an agent's queue forever behind a
row it will resend identically every time.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from netpulse.core.model import DeviceSeen
from netpulse.core.storage import Store

#: Rows accepted in one push. At a five second poll a busy agent produces a few hundred
#: rows a minute, so this is roughly an hour of catch-up per request — enough to drain a
#: backlog quickly, small enough that a retry does not resend a day's worth.
MAX_ROWS = 20_000

#: An agent naming itself something arbitrary would let one push claim another's
#: namespace. Kept to what makes a legible source prefix.
MAX_AGENT_NAME = 40


def clean_agent(name: str) -> str:
    """An agent name reduced to something safe to use as a source prefix.

    Anything else is rejected rather than sanitised into a different valid name: two
    agents whose names cleaned to the same string would quietly share a namespace, which
    is the exact collision the prefix exists to prevent.
    """
    trimmed = name.strip().lower()
    if not trimmed or len(trimmed) > MAX_AGENT_NAME:
        return ""
    if not all(character.isalnum() or character in "-_" for character in trimmed):
        return ""
    return trimmed


@dataclass
class Applied:
    """What a push actually changed, so the agent can advance its own cursors."""

    accepted: int = 0
    skipped: int = 0
    cursors: dict[str, int] = field(default_factory=dict)

    def as_json(self) -> dict[str, Any]:
        return {"accepted": self.accepted, "skipped": self.skipped, "cursors": self.cursors}


class Ingest:
    """Applies pushed batches, and remembers how far each agent has got.

    Cursors live in the store rather than in memory so a restart of this server does not
    ask every agent to resend everything it has — which, over a metered link, is a bill.
    """

    def __init__(self, store: Store) -> None:
        self.store = store

    def cursor(self, agent: str, stream: str) -> int:
        return self.store.agent_cursor(agent, stream)

    # ------------------------------------------------------------------ applying

    def apply(self, payload: dict[str, Any], now: datetime) -> Applied:
        """Write a pushed batch, skipping anything already applied."""
        agent = clean_agent(str(payload.get("agent", "")))
        if not agent:
            raise ValueError("push carries no usable agent name")

        result = Applied()
        cursors = payload.get("cursors") or {}
        for stream, rows in (
            ("samples", payload.get("samples") or []),
            ("texts", payload.get("texts") or []),
            ("usage", payload.get("usage") or []),
            ("devices", payload.get("devices") or []),
        ):
            if not isinstance(rows, list):
                continue
            sent = _as_int(cursors.get(stream))
            held = self.cursor(agent, stream)
            if sent is None or sent <= held:
                # Already applied, or the agent did not say where this batch ends. Either
                # way writing it would risk doubling an interval.
                result.skipped += len(rows[:MAX_ROWS])
                result.cursors[stream] = held
                continue
            written = self._write(agent, stream, rows[:MAX_ROWS])
            result.accepted += written
            self.store.advance_agent(agent, stream, sent, now)
            result.cursors[stream] = sent
        return result

    def _write(self, agent: str, stream: str, rows: list[Any]) -> int:
        writer = {
            "samples": self._samples,
            "texts": self._texts,
            "usage": self._usage,
            "devices": self._devices,
        }[stream]
        return writer(agent, rows)

    def _samples(self, agent: str, rows: list[Any]) -> int:
        # Grouped by moment and source so each group is one `record` call, which keeps
        # the rollup ladder maintained exactly as a local poll would have.
        grouped: dict[tuple[str, str], dict[str, float]] = {}
        for row in rows:
            parsed = _row(row, 4)
            if parsed is None:
                continue
            at, source, metric, value = parsed
            try:
                grouped.setdefault((at, source), {})[str(metric)] = float(value)
            except (TypeError, ValueError):
                continue
        written = 0
        for (at, source), metrics in grouped.items():
            moment = _moment(at)
            if moment is None:
                continue
            self.store.record(f"{agent}/{source}", metrics, at=moment)
            # Counted after the write, not before it. A row grouped and then dropped for
            # an unreadable timestamp is not a row accepted, and a count that says
            # otherwise is the kind of number this project exists not to publish.
            written += len(metrics)
        return written

    def _texts(self, agent: str, rows: list[Any]) -> int:
        written = 0
        for row in rows:
            parsed = _row(row, 4)
            if parsed is None:
                continue
            at, source, metric, value = parsed
            moment = _moment(at)
            if moment is None:
                continue
            self.store.record(f"{agent}/{source}", {}, {str(metric): str(value)}, at=moment)
            written += 1
        return written

    def _usage(self, agent: str, rows: list[Any]) -> int:
        written = 0
        for row in rows:
            parsed = _row(row, 6)
            if parsed is None:
                continue
            at, source, kind, key, down, up = parsed
            moment = _moment(at)
            if moment is None:
                continue
            try:
                entry = (str(key), float(down), float(up))
            except (TypeError, ValueError):
                continue
            self.store.record_usage(f"{agent}/{source}", str(kind), [entry], at=moment)
            written += 1
        return written

    def _devices(self, agent: str, rows: list[Any]) -> int:
        grouped: dict[tuple[str, str], list[DeviceSeen]] = {}
        for row in rows:
            parsed = _row(row, 5)
            if parsed is None:
                continue
            at, source, mac, name, ip = parsed
            grouped.setdefault((at, source), []).append(
                DeviceSeen(mac=str(mac), name=str(name), ip=str(ip))
            )
        written = 0
        for (at, source), devices in grouped.items():
            moment = _moment(at)
            if moment is None:
                continue
            self.store.record_devices(f"{agent}/{source}", devices, at=moment)
            written += len(devices)
        return written


def _row(row: Any, width: int) -> tuple[Any, ...] | None:
    """A pushed row, or None if it is not the shape it claims to be.

    Skipped rather than raised on: one malformed row aborting its batch would wedge the
    agent's queue behind a row it resends identically every time, and the queue is how
    an outage's readings get here.
    """
    if not isinstance(row, list | tuple) or len(row) != width:
        return None
    return tuple(row)


def _moment(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
