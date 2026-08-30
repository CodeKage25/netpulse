"""Pushing what was measured at home to a dashboard that lives somewhere else.

The agent is the ordinary monitor. It polls the router, probes the connection and writes
everything to its own store exactly as it always did — the local dashboard still works,
and nothing here is load-bearing for the measuring. What this adds is a second reader of
that store which walks forward through it and posts what the far end has not seen.

The whole design follows from one fact: **the push travels over the link being
measured.** So it will fail, regularly, and it will fail hardest during an outage —
which is when the readings are worth the most. Nothing is therefore sent and forgotten.
The store is the queue, a cursor marks how far the far end has acknowledged, and a failed
push simply changes nothing and is tried again. When the link returns, the backlog goes
with it, timestamps intact.

The second fact is that the link is usually metered, and this spends it. A monitor that
quietly costs its owner data would be a poor sort of monitor, so batches are compressed,
sent on a slow cadence rather than per poll, and the bytes spent are recorded as a metric
like any other — visible, chartable, and arguable with.
"""

from __future__ import annotations

import gzip
import json
import threading
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from netpulse.core.clock import Clock, utcnow
from netpulse.core.storage import Store

#: The streams an agent ships, in the order a reader would want them applied.
STREAMS = ("samples", "texts", "usage", "devices")

#: Rows per push. Enough to drain a night's backlog in a few requests without building a
#: single body large enough to time out on the connection that just came back.
BATCH = 5_000

#: How often to push when there is nothing wrong. Deliberately much slower than the poll
#: interval: batching is what makes the compression work, and a dashboard that is a
#: minute behind is not worse in any way that matters.
INTERVAL_S = 60.0

#: After a failure, back off to here. The link is down or the far end is unwell; either
#: way retrying every second helps nobody and costs data every time.
MAX_BACKOFF_S = 900.0

#: Where the agent's own push cursors live in its store, alongside the ones a hosted
#: instance keeps for its agents. Same shape, opposite direction.
UPSTREAM = "upstream"

Send = Callable[[str, bytes, dict[str, str]], bytes]


class PushFailed(Exception):
    """The far end could not be reached, or refused. Never fatal — always retried."""


@dataclass(frozen=True)
class Pushed:
    rows: int
    bytes_sent: int
    cursors: dict[str, int]


def _urllib_send(url: str, body: bytes, headers: dict[str, str]) -> bytes:
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return bytes(response.read())
    except urllib.error.HTTPError as exc:
        # A refusal is worth distinguishing in the message: 401 means the token is wrong
        # and no amount of retrying will fix it, which is not what a timeout means.
        raise PushFailed(f"HTTP {exc.code}: {exc.reason}") from exc
    except OSError as exc:
        raise PushFailed(str(exc)) from exc


class Pusher:
    """Walks the local store forward and posts what the far end has not acknowledged."""

    def __init__(
        self,
        store: Store,
        url: str,
        token: str,
        name: str,
        *,
        send: Send = _urllib_send,
        clock: Clock = utcnow,
        batch: int = BATCH,
    ) -> None:
        self.store = store
        self.url = url.rstrip("/") + "/api/ingest"
        self.name = name
        self._token = token
        self._send = send
        self._clock = clock
        self._batch = batch

    def pending(self) -> tuple[dict[str, list[tuple[object, ...]]], dict[str, int]]:
        """The next batch from each stream, and the cursor each one ends at."""
        rows: dict[str, list[tuple[object, ...]]] = {}
        cursors: dict[str, int] = {}
        for stream in STREAMS:
            held = self.store.agent_cursor(UPSTREAM, stream)
            found, cursor = self.store.after(stream, held, self._batch)
            if found:
                rows[stream] = found
                cursors[stream] = cursor
        return rows, cursors

    def push_once(self) -> Pushed | None:
        """One batch, or None when the far end is already up to date.

        Cursors advance only after the far end has confirmed what it applied. A push
        that fails halfway leaves the store untouched, so the next attempt sends the
        same rows rather than skipping the ones that were in flight.
        """
        rows, cursors = self.pending()
        if not rows:
            return None

        payload = {"agent": self.name, "cursors": cursors, **rows}
        # Compressed because this is somebody's data allowance. Readings are long runs of
        # similar numbers and repeated source names, which is close to the best case for
        # gzip — measured on real batches it is a fivefold reduction or better.
        body = gzip.compress(json.dumps(payload, separators=(",", ":")).encode(), 6)
        answer = self._send(
            self.url,
            body,
            {
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json",
                "Content-Encoding": "gzip",
            },
        )
        try:
            applied = json.loads(answer or b"{}")
            acknowledged = {str(k): int(v) for k, v in (applied.get("cursors") or {}).items()}
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            raise PushFailed(f"unreadable reply: {exc}") from exc

        now = self._clock()
        for stream, cursor in acknowledged.items():
            if stream in STREAMS:
                self.store.advance_agent(UPSTREAM, stream, cursor, now)
        return Pushed(
            rows=sum(len(batch) for batch in rows.values()),
            bytes_sent=len(body),
            cursors=acknowledged,
        )

    def run(self, stop: threading.Event, interval_s: float = INTERVAL_S) -> None:
        """Push until told to stop, backing off while the link is down."""
        backoff = 0.0
        while not stop.is_set():
            try:
                pushed = self.push_once()
                backoff = 0.0
                if pushed is not None:
                    self._record_cost(pushed)
            except PushFailed:
                # No logging of the reason at this level: it is almost always "the link
                # this agent exists to watch is down", which the readings already say,
                # and a log line per minute during an outage buries the ones that matter.
                backoff = min(MAX_BACKOFF_S, backoff * 2 or interval_s)
            stop.wait(backoff or interval_s)

    def _record_cost(self, pushed: Pushed) -> None:
        """What this cost, as a metric, because it is spending somebody's allowance.

        Recorded against the agent rather than a monitored source: it is traffic NetPulse
        itself created, and folding it into a source's figures would make the tool a
        silent line item in its own report.
        """
        self.store.record(
            f"agent/{self.name}",
            {"agent.push_bytes": float(pushed.bytes_sent), "agent.push_rows": float(pushed.rows)},
            at=self._clock(),
        )


def spent_today(store: Store, name: str, since: datetime) -> float:
    """Bytes this agent has spent pushing since `since` — the cost of being watched."""
    return sum(store.values(f"agent/{name}", "agent.push_bytes", since))
