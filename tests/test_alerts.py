"""User-defined alert rules, and where their alerts go."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from netpulse.alerting.alerts import AlertEngine, Rule, evaluate, parse_rules
from netpulse.alerting.channels import Channel, Channels, parse_channels, redact
from netpulse.core.model import Severity
from netpulse.core.storage import Store
from tests.conftest import Clock


def seed(
    store: Store, clock: Clock, values: list[float], metric: str = "latency.internet_ms"
) -> None:
    for value in values:
        store.record("wan", {metric: value})
        clock.advance(seconds=5)


# ------------------------------------------------------------------ evaluation


def test_a_sustained_breach_fires(store: Store, clock: Clock) -> None:
    rule = Rule(metric="latency.internet_ms", above=300, for_minutes=1)
    seed(store, clock, [500.0] * 20)  # 100 seconds of it
    breached, value = evaluate(store, rule, "wan", clock.now, interval_s=5)
    assert breached is True
    assert value == 500.0


def test_a_brief_spike_does_not_fire(store: Store, clock: Clock) -> None:
    """One bad second is ordinary; a minute of them is the connection."""
    rule = Rule(metric="latency.internet_ms", above=300, for_minutes=1)
    seed(store, clock, [50.0] * 15 + [500.0] * 2)
    breached, _ = evaluate(store, rule, "wan", clock.now, interval_s=5)
    assert breached is False


def test_a_below_rule_fires_on_weak_signal(store: Store, clock: Clock) -> None:
    rule = Rule(metric="signal.rsrp_dbm", below=-105, for_minutes=1)
    seed(store, clock, [-112.0] * 20, metric="signal.rsrp_dbm")
    breached, value = evaluate(store, rule, "wan", clock.now, interval_s=5)
    assert breached is True
    assert value == -112.0


def test_missing_data_cannot_breach_a_rule(store: Store, clock: Clock) -> None:
    """Absence is not a high reading.

    A router that stops answering would otherwise trip every threshold at once, and the
    outage that really happened would arrive buried under alarms that did not.
    """
    rule = Rule(metric="latency.internet_ms", above=300, for_minutes=10)
    seed(store, clock, [500.0, 500.0])  # ten seconds of a ten-minute window
    clock.advance(minutes=9)
    breached, _ = evaluate(store, rule, "wan", clock.now, interval_s=5)
    assert breached is False


def test_no_readings_at_all_is_not_a_breach(store: Store, clock: Clock) -> None:
    rule = Rule(metric="latency.internet_ms", above=300, for_minutes=1)
    breached, value = evaluate(store, rule, "wan", clock.now, interval_s=5)
    assert breached is False
    assert value is None


def test_the_duration_is_time_not_a_count_of_polls(store: Store, clock: Clock) -> None:
    """ "Above 300ms for two minutes" must mean two minutes at any poll rate."""
    rule = Rule(metric="latency.internet_ms", above=300, for_minutes=2)
    for _ in range(4):  # four readings, but only 20 seconds of them
        store.record("wan", {"latency.internet_ms": 900.0})
        clock.advance(seconds=5)
    clock.advance(minutes=2)
    breached, _ = evaluate(store, rule, "wan", clock.now, interval_s=5)
    assert breached is False


# ------------------------------------------------------------------ the engine


def test_the_engine_reports_each_transition_once(store: Store, clock: Clock) -> None:
    engine = AlertEngine([Rule(metric="latency.internet_ms", above=300, for_minutes=1)], 5)
    seed(store, clock, [500.0] * 20)

    started, cleared = engine.check(store, "wan", clock.now)
    assert len(started) == 1 and not cleared

    started, cleared = engine.check(store, "wan", clock.now)
    assert not started and not cleared  # still firing is not news

    seed(store, clock, [40.0] * 20)
    started, cleared = engine.check(store, "wan", clock.now)
    assert not started and len(cleared) == 1
    assert cleared[0].rule.metric == "latency.internet_ms"


def test_clearing_is_immediate(store: Store, clock: Clock) -> None:
    """Waiting to see whether it recovers would leave the dashboard saying "fine" while
    the alert list still said otherwise. Flap control belongs in the notifier."""
    engine = AlertEngine([Rule(metric="latency.internet_ms", above=300, for_minutes=1)], 5)
    seed(store, clock, [500.0] * 20)
    engine.check(store, "wan", clock.now)
    seed(store, clock, [40.0] * 20)
    _, cleared = engine.check(store, "wan", clock.now)
    assert len(cleared) == 1


def test_a_rule_can_be_pinned_to_one_source(store: Store, clock: Clock) -> None:
    engine = AlertEngine([Rule(metric="latency.internet_ms", above=300, source="mtn")], 5)
    seed(store, clock, [500.0] * 20)
    started, _ = engine.check(store, "wan", clock.now)
    assert not started


# ------------------------------------------------------------------ config parsing


def test_rules_parse_from_config() -> None:
    rules = parse_rules(
        [
            {
                "metric": "latency.internet_ms",
                "above": 300,
                "for_minutes": 2,
                "severity": "critical",
            },
            {"metric": "signal.rsrp_dbm", "below": -105, "message": "Signal too weak"},
        ]
    )
    assert len(rules) == 2
    assert rules[0].severity is Severity.CRITICAL
    assert rules[1].below == -105
    assert rules[1].describe(-110.0) == "Signal too weak"


def test_a_rule_with_no_bound_or_both_is_dropped() -> None:
    """Guessing which one was meant would enforce a condition nobody wrote."""
    assert parse_rules([{"metric": "latency.internet_ms"}]) == []
    assert parse_rules([{"metric": "x", "above": 1, "below": 2}]) == []
    assert parse_rules([{"above": 300}]) == []


def test_an_unknown_severity_falls_back_rather_than_crashing() -> None:
    rules = parse_rules([{"metric": "x", "above": 1, "severity": "catastrophic"}])
    assert rules[0].severity is Severity.WARNING


def test_a_rule_describes_itself_when_no_message_is_given() -> None:
    rule = Rule(metric="latency.internet_ms", above=300, for_minutes=2)
    described = rule.describe(512.0)
    assert "300" in described and "512" in described and "2 min" in described


# ------------------------------------------------------------------ channels


def sent_by(channel: Channel, severity: str = "warning") -> tuple[str, bytes, dict[str, str]]:
    captured: list[tuple[str, bytes, dict[str, str]]] = []
    Channels([channel], post=lambda url, body, headers: captured.append((url, body, headers))).send(
        "wan is down", "no route to the internet", severity
    )
    return captured[0]


def test_slack_gets_its_own_shape() -> None:
    _, body, headers = sent_by(Channel("slack", "https://hooks.slack.com/x"))
    assert headers["Content-Type"] == "application/json"
    assert "wan is down" in json.loads(body)["text"]


def test_discord_gets_an_embed_coloured_by_severity() -> None:
    _, body, _ = sent_by(Channel("discord", "https://discord.com/api/webhooks/x"), "critical")
    embed = json.loads(body)["embeds"][0]
    assert embed["title"] == "wan is down"
    assert embed["color"] == 0xD03B3B


def test_ntfy_puts_the_message_in_the_body_and_the_rest_in_headers() -> None:
    _, body, headers = sent_by(Channel("ntfy", "https://ntfy.sh/topic"), "critical")
    assert body == b"no route to the internet"
    assert headers["Title"] == "wan is down"
    assert headers["Priority"] == "urgent"


def test_one_broken_channel_does_not_silence_the_others() -> None:
    """A dead Discord webhook is not a reason for the Slack alert to go missing too."""
    reached: list[str] = []

    def post(url: str, body: bytes, headers: dict[str, str]) -> None:
        if "discord" in url:
            raise OSError("connection refused")
        reached.append(url)

    channels = Channels(
        [
            Channel("discord", "https://discord.com/api/webhooks/x"),
            Channel("slack", "https://hooks.slack.com/y"),
            Channel("ntfy", "https://ntfy.sh/z"),
        ],
        post=post,
    )
    assert channels.send("down", "body") == 2
    assert len(reached) == 2


def test_an_unknown_channel_kind_is_dropped_not_treated_as_a_webhook() -> None:
    """Posting a NetPulse-shaped body at a URL expecting something else is a silent
    misdelivery; a typo should cost you an alert you notice missing."""
    assert parse_channels([{"kind": "telegramm", "url": "https://api.telegram.org/x"}]) == []


def test_a_channel_without_a_usable_url_is_dropped() -> None:
    assert parse_channels([{"kind": "slack", "url": "not-a-url"}]) == []
    assert parse_channels([{"kind": "slack"}]) == []


def test_the_dashboard_never_sees_a_webhook_path() -> None:
    """A Slack or ntfy URL is a bearer credential: anyone holding it can post there."""
    listed = redact([Channel("slack", "https://hooks.slack.com/services/T0/B0/SECRET")])
    assert listed == [{"kind": "slack", "host": "hooks.slack.com"}]
    assert "SECRET" not in json.dumps(listed)


# ------------------------------------------------------------------ through the collector


def test_a_firing_rule_becomes_an_event_and_a_notification(store: Store, clock: Clock) -> None:
    from netpulse.alerting.notify import Notifier
    from netpulse.core.model import EventKind, Reading
    from netpulse.monitor import Collector
    from netpulse.sources.fake import ScriptedAdapter

    clock.set(datetime(2026, 8, 1, tzinfo=UTC))
    posted: list[tuple[str, bytes, dict[str, str]]] = []
    sent: list[str] = []

    readings = [Reading(metrics={"latency.internet_ms": 900.0, "up": 1.0}) for _ in range(30)]
    collector = Collector(
        store,
        [ScriptedAdapter("wan", readings)],
        clock=clock,
        interval_s=5,
        notifier=Notifier(deliver=lambda title, body: sent.append(title), clock=clock),
        alerts=AlertEngine([Rule(metric="latency.internet_ms", above=300, for_minutes=1)], 5),
        channels=Channels(
            [Channel("ntfy", "https://ntfy.sh/topic")],
            post=lambda url, body, headers: posted.append((url, body, headers)),
        ),
    )
    for _ in readings:
        collector.poll_once()
        clock.advance(seconds=5)

    alerts = [e for e in store.events() if e.kind == EventKind.ALERT]
    assert len(alerts) == 1
    assert "300" in alerts[0].detail
    assert len(sent) == 1  # the onset, once
    assert len(posted) == 1  # and it reached the channel
