"""The split: measuring at home, keeping the answers somewhere else.

Every test here runs both halves in-process against two stores, so the round trip is
exercised for real — the agent reads its own store, builds a batch, the hosted side
applies it — without a socket, a container or a network.
"""

from __future__ import annotations

import gzip
import json
from datetime import timedelta

import pytest

from netpulse.agent import UPSTREAM, Pusher, PushFailed
from netpulse.core.model import DeviceSeen
from netpulse.core.storage import Store
from netpulse.web.ingest import Ingest, clean_agent
from tests.conftest import Clock


class Wire:
    """A hosted instance at the other end of a rope, with a switch to cut it."""

    def __init__(self, store: Store, clock: Clock) -> None:
        self.ingest = Ingest(store)
        self.clock = clock
        self.up = True
        self.pushes = 0
        self.bytes_carried = 0
        self.last_body = b""
        self.last_headers: dict[str, str] = {}

    def __call__(self, url: str, body: bytes, headers: dict[str, str]) -> bytes:
        if not self.up:
            raise PushFailed("no route to host")
        self.pushes += 1
        self.bytes_carried += len(body)
        self.last_body, self.last_headers = body, headers
        assert headers["Content-Encoding"] == "gzip"
        assert headers["Authorization"].startswith("Bearer ")
        payload = json.loads(gzip.decompress(body))
        return json.dumps(self.ingest.apply(payload, self.clock.now).as_json()).encode()


@pytest.fixture
def wired(store: Store, clock: Clock):  # type: ignore[no-untyped-def]
    """An agent store, a hosted store, and the rope between them."""
    hosted = Store(":memory:", clock=lambda: clock.now)
    wire = Wire(hosted, clock)
    pusher = Pusher(store, "https://example.fly.dev", "a-long-enough-token", "home",
                    send=wire, clock=lambda: clock.now)
    return store, hosted, pusher, wire


def measure(store: Store, clock: Clock, latency: float = 20.0, up: float = 1.0) -> None:
    store.record("wan", {"latency.internet_ms": latency, "up": up}, at=clock.now)
    clock.advance(seconds=5)


# ------------------------------------------------------------------ the round trip


def test_readings_measured_at_home_arrive_with_their_own_timestamps(wired) -> None:  # type: ignore[no-untyped-def]
    """The hosted side is a copy, not the original. If it stamped rows on arrival, a
    backlog delivered after an outage would all land at the moment the link returned —
    an hour of history compressed into one second, and the outage itself erased."""
    store, hosted, pusher, _ = wired
    clock = Clock()
    at = None
    for _ in range(5):
        at = at or clock.now
        store.record("wan", {"latency.internet_ms": 20.0}, at=clock.now)
        clock.advance(seconds=5)

    pusher.push_once()
    stamped = hosted.stamped("home/wan", "latency.internet_ms", clock.now - timedelta(hours=1),
                             clock.now + timedelta(seconds=1))
    assert len(stamped) == 5
    # Five distinct moments, five seconds apart — not five rows at one instant.
    moments = sorted({moment for moment, _ in stamped})
    assert len(moments) == 5
    assert (moments[-1] - moments[0]).total_seconds() == 20


def test_sources_are_namespaced_by_agent(wired) -> None:  # type: ignore[no-untyped-def]
    """Every install calls its probe source `wan`. Two houses pushing to one dashboard
    would otherwise interleave into a single series, which is worse than either being
    missing — it looks like one connection behaving impossibly."""
    store, hosted, pusher, _ = wired
    store.record("wan", {"latency.internet_ms": 20.0})
    pusher.push_once()
    assert "home/wan" in hosted.sources()
    assert "wan" not in hosted.sources()


def test_nothing_to_send_is_not_a_push(wired) -> None:
    """An idle agent must not spend data saying nothing happened."""
    _, _, pusher, wire = wired
    assert pusher.push_once() is None
    assert wire.pushes == 0


# ------------------------------------------------------------------ the link failing


def test_an_outage_delays_the_readings_rather_than_losing_them(wired) -> None:  # type: ignore[no-untyped-def]
    """The push crosses the link being measured, so it fails hardest exactly when the
    readings matter most. The store is the queue; the backlog goes when the link
    returns."""
    store, hosted, pusher, wire = wired
    clock = Clock()

    wire.up = False
    for _ in range(10):
        store.record("wan", {"up": 0.0}, at=clock.now)
        clock.advance(seconds=5)
    with pytest.raises(PushFailed):
        pusher.push_once()
    assert hosted.sources() == []  # nothing arrived, and nothing was dropped either

    wire.up = True
    pusher.push_once()
    assert len(hosted.values("home/wan", "up", clock.now - timedelta(hours=1))) == 10


def test_a_failed_push_does_not_advance_the_cursor(wired) -> None:  # type: ignore[no-untyped-def]
    """Otherwise the rows in flight when the connection dropped are skipped forever,
    and the gap is invisible: the series simply has a hole nobody can account for."""
    store, _, pusher, wire = wired
    store.record("wan", {"up": 1.0})
    wire.up = False
    with pytest.raises(PushFailed):
        pusher.push_once()
    assert store.agent_cursor(UPSTREAM, "samples") == 0


# ------------------------------------------------------------------ idempotency


def test_pushing_the_same_batch_twice_does_not_double_the_usage(wired) -> None:  # type: ignore[no-untyped-def]
    """The one failure that corrupts rather than loses. Usage rows are intervals, so
    applying a batch twice does not overwrite anything — it adds, and the month's total
    quietly becomes wrong in the direction that costs somebody money."""
    store, hosted, pusher, wire = wired
    clock = Clock()
    store.record_usage("wan", "device", [("AA:BB:CC:DD:EE:01", 5e9, 1e9)], at=clock.now)

    pusher.push_once()
    first = dict((key, down) for key, down, _ in hosted.usage_by_key(
        "home/wan", "device", clock.now - timedelta(hours=1)))

    # The real shape of this: the server applied the batch, the acknowledgement was lost
    # on the way back, and the agent resends the identical bytes. Rewinding the agent's
    # own cursor would not reproduce it — `advance_agent` takes a max and refuses to go
    # backwards, which is a separate guard with its own test.
    wire(pusher.url, wire.last_body, wire.last_headers)
    second = dict((key, down) for key, down, _ in hosted.usage_by_key(
        "home/wan", "device", clock.now - timedelta(hours=1)))

    assert first == second == {"AA:BB:CC:DD:EE:01": 5e9}
    assert wire.pushes == 2  # the same bytes really did arrive twice


def test_a_rewound_cursor_cannot_reopen_applied_rows(store: Store, clock: Clock) -> None:
    """A retry arriving out of order must not lower the watermark and let old rows in."""
    hosted = Store(":memory:", clock=lambda: clock.now)
    ingest = Ingest(hosted)
    ingest.apply({"agent": "home", "cursors": {"samples": 50},
                  "samples": [[clock.now.isoformat(), "wan", "up", 1.0]]}, clock.now)
    ingest.apply({"agent": "home", "cursors": {"samples": 10},
                  "samples": [[clock.now.isoformat(), "wan", "up", 0.0]]}, clock.now)
    assert hosted.agent_cursor("home", "samples") == 50
    assert hosted.values("home/wan", "up", clock.now - timedelta(hours=1)) == [1.0]


# ------------------------------------------------------------------ robustness


def test_one_malformed_row_does_not_wedge_the_queue(store: Store, clock: Clock) -> None:
    """A row that always fails would sit at the head of the queue being resent
    identically forever, and everything behind it — including an outage — never
    arrives."""
    hosted = Store(":memory:", clock=lambda: clock.now)
    applied = Ingest(hosted).apply(
        {
            "agent": "home",
            "cursors": {"samples": 3},
            "samples": [
                ["not-a-timestamp", "wan", "up", 1.0],
                [clock.now.isoformat(), "wan"],  # too short
                [clock.now.isoformat(), "wan", "up", 1.0],
            ],
        },
        clock.now,
    )
    assert applied.accepted == 1
    assert hosted.values("home/wan", "up", clock.now - timedelta(hours=1)) == [1.0]


def test_an_unusable_agent_name_is_refused_rather_than_cleaned(clock: Clock) -> None:
    """Two names that cleaned to the same string would share a namespace silently,
    which is the exact collision the prefix exists to prevent."""
    assert clean_agent("home-mac") == "home-mac"
    assert clean_agent("  HOME_MAC ") == "home_mac"
    for bad in ("", "../etc", "home/wan", "a" * 60, "home mac"):
        assert clean_agent(bad) == ""

    hosted = Store(":memory:", clock=lambda: clock.now)
    with pytest.raises(ValueError, match="agent name"):
        Ingest(hosted).apply({"agent": "../etc", "samples": []}, clock.now)


def test_devices_and_texts_make_the_trip_too(wired) -> None:  # type: ignore[no-untyped-def]
    """The dashboard is only as useful as the least-shipped stream."""
    store, hosted, pusher, _ = wired
    clock = Clock()
    store.record("wan", {"up": 1.0}, {"net.type": "LTE"}, at=clock.now)
    store.record_devices("wan", [DeviceSeen(mac="AA:BB", name="Pixel", ip="192.168.0.5")],
                         at=clock.now)
    pusher.push_once()

    assert hosted.latest_texts("home/wan").get("net.type") == "LTE"
    seen = hosted.devices("home/wan", clock.now - timedelta(hours=1))
    assert [device["mac"] for device in seen] == ["AA:BB"]


# ------------------------------------------------------------------ the cost of being watched


def test_the_push_records_what_it_cost(wired) -> None:  # type: ignore[no-untyped-def]
    """This spends the allowance of the connection it is measuring. A monitor that costs
    its owner data without saying so is a poor sort of monitor."""
    store, _, pusher, wire = wired
    for _ in range(50):
        store.record("wan", {"latency.internet_ms": 20.0, "up": 1.0})
    pushed = pusher.push_once()

    assert pushed is not None
    assert pushed.bytes_sent == wire.bytes_carried

    # `push_once` records nothing itself — `run` does, so a caller driving the pusher by
    # hand is not surprised by rows it never asked for. Drive the recording directly.
    pusher._record_cost(pushed)
    spent = store.values("agent/home", "agent.push_bytes", Clock().now - timedelta(hours=1))
    assert spent and spent[-1] == float(pushed.bytes_sent)


def test_batches_compress_because_readings_repeat(wired) -> None:  # type: ignore[no-untyped-def]
    """Long runs of similar numbers and one repeated source name per row. If this ever
    stops holding, the agent is quietly costing more than it should."""
    store, _, pusher, _ = wired
    for _ in range(500):
        store.record("wan", {"latency.internet_ms": 20.1, "loss.pct": 0.0, "up": 1.0})
    pushed = pusher.push_once()

    assert pushed is not None
    raw = len(json.dumps({"samples": [[str(i), "wan", "up", 1.0] for i in range(1500)]}))
    assert pushed.bytes_sent < raw / 5
