from __future__ import annotations

import threading
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime

from netpulse.adapters import Adapter
from netpulse.model import EventKind, Severity
from netpulse.notify import Notifier
from netpulse.storage import Store, utcnow

#: Three misses before an outage is declared: one failed poll is a blip, three is real.
OUTAGE_AFTER = 3
DEGRADED_LATENCY_MS = 400.0
DEGRADED_AFTER = 6
MAX_BACKOFF_CYCLES = 12


@dataclass
class _SourceState:
    adapter: Adapter
    failures: int = 0
    slow_polls: int = 0
    outage_id: int | None = None
    outage_started: datetime | None = None
    degraded_id: int | None = None
    skip: int = 0
    backoff: int = 0
    last_error: str = ""
    listeners_payload: dict[str, float] = field(default_factory=dict)


class Collector:
    """Polls every source each cycle and keeps the outage/degraded state machines.

    A failing source backs off exponentially rather than hammering a router that is
    already struggling — the recovering box needs the quiet more than we need the sample.
    """

    def __init__(
        self,
        store: Store,
        adapters: list[Adapter],
        interval_s: float = 5.0,
        clock: Callable[[], datetime] = utcnow,
        notifier: Notifier | None = None,
    ) -> None:
        self.store = store
        self.interval_s = interval_s
        self._clock = clock
        self._notifier = notifier
        self._last_compact: datetime | None = None
        self._states = {adapter.name: _SourceState(adapter) for adapter in adapters}
        self._listeners: list[Callable[[str, dict[str, float]], None]] = []

    @property
    def sources(self) -> list[str]:
        return list(self._states)

    def add_adapter(self, adapter: Adapter) -> None:
        """Hot-add a source — discovery finding a router must not need a restart."""
        if adapter.name not in self._states:
            self._states[adapter.name] = _SourceState(adapter)

    def subscribe(self, listener: Callable[[str, dict[str, float]], None]) -> None:
        self._listeners.append(listener)

    def poll_once(self) -> None:
        self._maybe_compact()
        for name, state in list(self._states.items()):
            if state.skip > 0:
                state.skip -= 1
                continue
            try:
                reading = state.adapter.read()
            except Exception as exc:
                self._failed(name, state, exc)
                continue
            if reading.devices is not None:
                self.store.record_devices(name, reading.devices, at=self._clock())
            self._succeeded(name, state, reading.metrics, reading.texts)

    def _maybe_compact(self) -> None:
        now = self._clock()
        if self._last_compact is None or (now - self._last_compact).total_seconds() >= 86400:
            self._last_compact = now
            with suppress(Exception):  # compaction failing must never stop recording
                self.store.compact(now)

    def _failed(self, name: str, state: _SourceState, exc: Exception) -> None:
        state.failures += 1
        state.last_error = str(exc)
        state.backoff = min(state.backoff * 2 or 1, MAX_BACKOFF_CYCLES)
        state.skip = state.backoff - 1
        self.store.record(name, {"up": 0.0}, at=self._clock())
        if state.failures == OUTAGE_AFTER and state.outage_id is None:
            state.outage_id = self.store.open_event(
                name,
                EventKind.OUTAGE,
                Severity.CRITICAL,
                f"unreachable: {exc}",
                at=self._clock(),
            )
            state.outage_started = self._clock()
            if self._notifier:
                self._notifier.send(f"outage:{name}", f"{name} is down", str(exc))
        self._notify(name, {"up": 0.0})

    def _succeeded(
        self, name: str, state: _SourceState, metrics: dict[str, float], texts: dict[str, str]
    ) -> None:
        state.failures = 0
        state.backoff = 0
        state.last_error = ""
        recorded = dict(metrics)
        recorded.setdefault("up", 1.0)
        self.store.record(name, recorded, texts, at=self._clock())

        if state.outage_id is not None:
            self.store.close_event(state.outage_id, at=self._clock())
            state.outage_id = None
            if self._notifier:
                lasted = ""
                if state.outage_started is not None:
                    minutes = (self._clock() - state.outage_started).total_seconds() / 60
                    lasted = f" after {max(1, round(minutes))} min"
                # The clear carries its own key: a recovery a second after the onset is
                # news, and sharing a key would let the onset's throttle swallow it.
                self._notifier.send(f"outage:{name}:cleared", f"{name} is back{lasted}", "")
            state.outage_started = None

        latency = recorded.get("latency.internet_ms")
        if latency is not None:
            state.slow_polls = state.slow_polls + 1 if latency > DEGRADED_LATENCY_MS else 0
            if state.slow_polls >= DEGRADED_AFTER and state.degraded_id is None:
                state.degraded_id = self.store.open_event(
                    name,
                    EventKind.DEGRADED,
                    Severity.WARNING,
                    f"latency above {DEGRADED_LATENCY_MS:.0f}ms for "
                    f"{DEGRADED_AFTER} consecutive polls",
                    at=self._clock(),
                )
            if state.slow_polls == 0 and state.degraded_id is not None:
                self.store.close_event(state.degraded_id, at=self._clock())
                state.degraded_id = None
        self._notify(name, recorded)

    def _notify(self, name: str, metrics: dict[str, float]) -> None:
        for listener in self._listeners:
            # A slow or broken dashboard listener must never stop recording.
            with suppress(Exception):
                listener(name, metrics)

    def status(self) -> dict[str, dict[str, object]]:
        return {
            name: {
                "kind": state.adapter.kind,
                "failures": state.failures,
                "in_outage": state.outage_id is not None,
                "degraded": state.degraded_id is not None,
                "last_error": state.last_error,
            }
            for name, state in self._states.items()
        }

    def run(self, stop: threading.Event) -> None:
        while not stop.is_set():
            self.poll_once()
            stop.wait(self.interval_s)
