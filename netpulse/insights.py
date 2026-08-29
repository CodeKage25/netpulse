"""Deterministic diagnosis with evidence attached.

Each rule answers one question a person actually asks — "is it my WiFi or is it MTN?",
"should I move the router?" — from recorded history, and shows its numbers. An LLM may
narrate these findings; it never invents them.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta

from netpulse.model import Insight, Severity
from netpulse.storage import Store


def _values(series: list[tuple[datetime, float | None]]) -> list[float]:
    return [value for _, value in series if value is not None]


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[len(ordered) // 2]


Rule = Callable[[Store, str, datetime], Insight | None]


def upstream_or_local(store: Store, source: str, now: datetime) -> Insight | None:
    """The question every household argues about: whose fault is the slow internet."""
    window = now - timedelta(minutes=15)
    gateway = _median(_values(store.history(source, "latency.gateway_ms", window, now, 15)))
    internet = _median(_values(store.history(source, "latency.internet_ms", window, now, 15)))
    if gateway is None or internet is None:
        return None
    if internet > 250 and gateway < 20:
        return Insight(
            rule="upstream_or_local",
            severity=Severity.WARNING,
            title="The slowdown is upstream, not your WiFi",
            detail=(
                f"Your router answers in {gateway:.0f}ms but the internet beyond it takes "
                f"{internet:.0f}ms. The problem is your provider's network, not anything "
                "in your home — rebooting the router will not help."
            ),
            evidence={"gateway_ms": round(gateway, 1), "internet_ms": round(internet, 1)},
        )
    if gateway > 100:
        return Insight(
            rule="upstream_or_local",
            severity=Severity.WARNING,
            title="Your local network is the bottleneck",
            detail=(
                f"Reaching your own router takes {gateway:.0f}ms — that delay happens "
                "before your traffic ever leaves the building. Weak WiFi to the router, "
                "an overloaded router, or interference. Moving closer or rebooting it "
                "can actually help here."
            ),
            evidence={"gateway_ms": round(gateway, 1)},
        )
    return None


def weak_signal(store: Store, source: str, now: datetime) -> Insight | None:
    window = now - timedelta(minutes=30)
    rsrp = _median(_values(store.history(source, "signal.rsrp_dbm", window, now, 10)))
    sinr = _median(_values(store.history(source, "signal.sinr_db", window, now, 10)))
    if rsrp is None and sinr is None:
        return None
    if sinr is not None and sinr < 0:
        return Insight(
            rule="weak_signal",
            severity=Severity.CRITICAL,
            title="Signal quality is drowning in noise",
            detail=(
                f"SINR is {sinr:.0f}dB — the tower's signal is barely louder than the "
                "interference. Speeds will be a fraction of what the plan allows. Move "
                "the router near a window facing the nearest mast, raise it high, or "
                "consider an external antenna."
            ),
            evidence={"sinr_db": round(sinr, 1)},
        )
    if rsrp is not None and rsrp < -110:
        return Insight(
            rule="weak_signal",
            severity=Severity.WARNING,
            title="Weak coverage where the router sits",
            detail=(
                f"Signal strength is {rsrp:.0f}dBm, which is edge-of-coverage. Every dB "
                "gained by repositioning pays directly in speed and stability."
            ),
            evidence={"rsrp_dbm": round(rsrp, 1)},
        )
    if sinr is not None and sinr > 13 and rsrp is not None and rsrp > -95:
        return Insight(
            rule="weak_signal",
            severity=Severity.INFO,
            title="Radio conditions are excellent",
            detail=(
                f"RSRP {rsrp:.0f}dBm with SINR {sinr:.0f}dB — the router is well placed. "
                "If speeds are still poor, the limit is the network or the plan, not the signal."
            ),
            evidence={"rsrp_dbm": round(rsrp, 1), "sinr_db": round(sinr, 1)},
        )
    return None


def slow_dns(store: Store, source: str, now: datetime) -> Insight | None:
    window = now - timedelta(minutes=15)
    dns = _median(_values(store.history(source, "dns.lookup_ms", window, now, 15)))
    connect = _median(_values(store.history(source, "latency.internet_best_ms", window, now, 15)))
    if dns is None or connect is None or connect <= 0:
        return None
    if dns > 150 and dns > 4 * connect:
        return Insight(
            rule="slow_dns",
            severity=Severity.WARNING,
            title="DNS is slower than the connection itself",
            detail=(
                f"Lookups take {dns:.0f}ms while the line itself answers in {connect:.0f}ms "
                "— every new website pays that wait before loading begins. Switching the "
                "router's DNS to 1.1.1.1 or 8.8.8.8 usually fixes exactly this."
            ),
            evidence={"dns_ms": round(dns, 1), "connect_ms": round(connect, 1)},
        )
    return None


def flapping(store: Store, source: str, now: datetime) -> Insight | None:
    outages = [
        event
        for event in store.events(source=source, since=now - timedelta(hours=24))
        if event.kind.value == "outage"
    ]
    if len(outages) >= 3:
        return Insight(
            rule="flapping",
            severity=Severity.CRITICAL,
            title=f"{len(outages)} outages in 24 hours",
            detail=(
                "The connection is flapping rather than failing once. On a cellular link "
                "that pattern usually means marginal signal or a congested cell handing "
                "you off repeatedly; on fixed lines, a failing router or line fault."
            ),
            evidence={"outages_24h": len(outages)},
        )
    return None


def congestion_hours(store: Store, source: str, now: datetime) -> Insight | None:
    """Last hour vs the day's baseline, for the nightly-slowdown pattern."""
    hour = _values(store.history(source, "latency.internet_ms", now - timedelta(hours=1), now, 12))
    day = _values(store.history(source, "latency.internet_ms", now - timedelta(hours=24), now, 48))
    recent, baseline = _median(hour), _median(day)
    if recent is None or baseline is None or baseline <= 0:
        return None
    if recent > 2 * baseline and recent > 150:
        return Insight(
            rule="congestion_hours",
            severity=Severity.INFO,
            title="Right now is worse than your normal",
            detail=(
                f"Median latency this hour is {recent:.0f}ms against a 24-hour baseline of "
                f"{baseline:.0f}ms. If this repeats at the same time daily, it is cell/exchange "
                "congestion — the network being busy, not anything on your side."
            ),
            evidence={"hour_ms": round(recent, 1), "baseline_ms": round(baseline, 1)},
        )
    return None


RULES: tuple[Rule, ...] = (upstream_or_local, weak_signal, slow_dns, flapping, congestion_hours)


def diagnose(store: Store, source: str, now: datetime) -> list[Insight]:
    findings = []
    for rule in RULES:
        try:
            finding = rule(store, source, now)
        except Exception:
            continue
        if finding is not None:
            findings.append(finding)
    order = {Severity.CRITICAL: 0, Severity.WARNING: 1, Severity.INFO: 2}
    return sorted(findings, key=lambda insight: order[insight.severity])
