from __future__ import annotations

import threading
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime

from netpulse.alerting.alerts import AlertEngine
from netpulse.alerting.channels import Channels
from netpulse.alerting.notify import Notifier
from netpulse.analysis.allowance import Plan, crossed, format_bytes
from netpulse.analysis.allowance import assess as assess_allowance
from netpulse.analysis.apps import AppMonitor
from netpulse.core.clock import Clock, utcnow
from netpulse.core.model import EventKind, Reading, Severity
from netpulse.core.storage import Store
from netpulse.sources import Adapter

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
        clock: Clock = utcnow,
        notifier: Notifier | None = None,
        plan: Plan | None = None,
        alerts: AlertEngine | None = None,
        apps: AppMonitor | None = None,
        channels: Channels | None = None,
    ) -> None:
        self.store = store
        self.interval_s = interval_s
        self._clock = clock
        self._notifier = notifier
        self._plan = plan
        self._alerts = alerts
        self._channels = channels
        #: Open alert event ids, so a clear closes the row its onset opened.
        self._alert_events: dict[tuple[str, str], int] = {}
        #: Per-application usage on this machine, recorded so the history can be asked
        #: what was using the connection last Tuesday rather than only right now.
        self._apps = apps
        #: Last known allowance fraction per source, so a threshold announces itself
        #: once on the way past rather than every poll while sitting above it.
        self._allowance_seen: dict[str, float] = {}
        self._last_compact: datetime | None = None
        self._states = {adapter.name: _SourceState(adapter) for adapter in adapters}
        self._listeners: list[Callable[[str, dict[str, float]], None]] = []

    @property
    def sources(self) -> list[str]:
        return list(self._states)

    def watched_urls(self) -> set[str]:
        """The router addresses already being polled, so discovery can say "watching"
        instead of offering a box the collector is on."""
        return {
            url
            for state in self._states.values()
            if (url := getattr(state.adapter, "base", None)) is not None
        }

    def adapter(self, name: str) -> Adapter | None:
        """The adapter behind a source, so the API can ask what it can do."""
        state = self._states.get(name)
        return state.adapter if state else None

    def kind_of(self, name: str) -> str:
        return str(getattr(self._states[name].adapter, "kind", ""))

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
            self._record_usage(name, reading)
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
        if any(key.startswith("data.month") for key in recorded):
            self._check_allowance(name)
        self._check_rules(name)
        self._notify(name, recorded)

    def _check_rules(self, name: str) -> None:
        """Evaluate the user's alert rules and announce the transitions.

        Both directions are recorded as events, so the history answers "was it bad last
        Tuesday" and not merely "is it bad now". Delivery is best-effort by design: an
        unreachable webhook must not become an unreachable router.
        """
        if self._alerts is None:
            return
        started, cleared = self._alerts.check(self.store, name, self._clock())
        for firing in started:
            detail = firing.rule.describe(firing.value)
            self._alert_events[(name, firing.rule.key)] = self.store.open_event(
                name, EventKind.ALERT, firing.rule.severity, detail, at=self._clock()
            )
            self._announce(
                f"alert:{name}:{firing.rule.key}",
                f"{name}: {detail}",
                "",
                firing.rule.severity.value,
            )
        for firing in cleared:
            minutes = (self._clock() - firing.since).total_seconds() / 60
            lasted = f" after {max(1, round(minutes))} min"
            event_id = self._alert_events.pop((name, firing.rule.key), None)
            if event_id is not None:
                self.store.close_event(event_id, at=self._clock())
            # A cleared alert carries its own key: a recovery a second after the onset
            # is news, and sharing a key would let the onset's throttle swallow it.
            self._announce(
                f"alert:{name}:{firing.rule.key}:cleared",
                f"{name}: recovered{lasted}",
                firing.rule.describe(firing.value),
                "info",
            )

    def _announce(self, key: str, title: str, body: str, severity: str) -> None:
        """One transition, to the desktop and to every configured channel."""
        if self._notifier and not self._notifier.send(key, title, body):
            return  # throttled: a flapping link reports once a minute, not endlessly
        if self._channels is not None:
            with suppress(Exception):
                self._channels.send(title, body or title, severity)

    def _record_usage(self, name: str, reading: Reading) -> None:
        """Attribute this interval's traffic to the things that can be named.

        Devices only where the router publishes counters; applications only for the
        machine NetPulse runs on. Nothing is apportioned — a row exists because
        something measured it.
        """
        devices = [
            (device.mac, device.rx_bytes or 0.0, device.tx_bytes or 0.0)
            # `devices` is None when an adapter does not list them at all, which is a
            # different thing from listing none — and only one of those is iterable.
            for device in (reading.devices or [])
            if device.rx_bytes is not None or device.tx_bytes is not None
        ]
        if devices:
            self.store.record_usage(name, "device", devices, at=self._clock())

        if self._apps is None or not self._apps.available:
            return
        with suppress(Exception):  # sampling processes must never stall a poll
            self.store.record_usage(
                name,
                "app",
                [
                    (app.name, app.down_bytes or 0.0, app.up_bytes or 0.0)
                    for app in self._apps.poll()
                    if app.down_bytes is not None
                ],
                at=self._clock(),
            )

    def _check_allowance(self, name: str) -> None:
        """Announce a crossed data threshold once, on the way past.

        Only ever runs on a poll that carried a fresh odometer reading — checking on
        every poll would re-derive the same figure hundreds of times an hour to say
        nothing, and these are the numbers people are billed on.
        """
        limit = self._plan.limit_bytes if self._plan else None
        if not self._notifier or not limit or not self._plan:
            return
        result = assess_allowance(self.store, name, self._clock(), limit, self._plan.reset_day)
        if result is None:
            return
        fraction = result.fraction
        threshold = crossed(self._allowance_seen.get(name), fraction)
        if fraction is not None:
            self._allowance_seen[name] = fraction
        if threshold is None:
            return
        used, cap = format_bytes(result.used_bytes), format_bytes(limit)
        if threshold >= 1.0:
            body = f"{used} of {cap} used. Expect throttling or charges."
        elif result.exhausted_on:
            body = (
                f"{used} of {cap} used — at this rate it runs out on {result.exhausted_on:%d %b}."
            )
        else:
            body = f"{used} of {cap} used, and on track for the cycle."
        # Keyed on the threshold so each level announces itself at most once per cycle.
        self._notifier.send(
            f"allowance:{name}:{threshold}", f"{name}: {threshold:.0%} of data used", body
        )

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
