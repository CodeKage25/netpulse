"""`netpulse ask` — Claude reads your recorded history and answers in plain language.

The deterministic layer does the measuring: recent stats, events, and rule-based insights
are computed locally and handed over as evidence. The model narrates and reasons over that
evidence; it is never asked to invent a number. Needs `pip install 'netpulse-monitor[ai]'`
and an Anthropic credential (ANTHROPIC_API_KEY, or `ant auth login`).
"""

from __future__ import annotations

import json
from datetime import timedelta

from netpulse.config import Config
from netpulse.insights import diagnose
from netpulse.storage import Store, utcnow

SYSTEM = (
    "You are a network analyst inside NetPulse, a local-first connection monitor. "
    "You are given measured evidence from the user's own machine: recent metrics, "
    "outage events, and rule-based findings. Answer their question from that evidence "
    "alone — cite the numbers you use, say plainly when the data cannot answer, and "
    "never invent measurements. Coverage below 1.0 means part of the window was not "
    "recorded. Be concise and practical; the reader is the person paying for this "
    "connection, not a network engineer."
)


def _evidence(store: Store, config: Config) -> str:
    now = utcnow()
    bundle: dict[str, object] = {"generated_at": now.isoformat(), "sources": {}}
    for name in store.sources():
        day = now - timedelta(hours=24)
        series = {
            metric: [value for _, value in store.history(name, metric, day, now, 48)]
            for metric in (
                "latency.internet_ms",
                "latency.gateway_ms",
                "loss.pct",
                "signal.rsrp_dbm",
                "signal.sinr_db",
                "traffic.down_bytes_s",
            )
        }
        bundle["sources"][name] = {  # type: ignore[index]
            "latest": {metric: value for metric, (_, value) in store.latest(name).items()},
            "labels": store.latest_texts(name),
            "last_24h_48buckets": {
                k: v for k, v in series.items() if any(x is not None for x in v)
            },
            "coverage_24h": store.coverage(name, day, now, config.interval_s).fraction,
            "events_7d": [
                {
                    "kind": event.kind.value,
                    "started": event.started_at.isoformat(),
                    "ended": event.ended_at.isoformat() if event.ended_at else None,
                    "detail": event.detail,
                }
                for event in store.events(source=name, since=now - timedelta(days=7))
            ],
            "findings": [
                {
                    "severity": insight.severity.value,
                    "title": insight.title,
                    "detail": insight.detail,
                    "evidence": insight.evidence,
                }
                for insight in diagnose(store, name, now)
            ],
        }
    return json.dumps(bundle, default=str)


def ask_claude(store: Store, config: Config, question: str) -> int:
    try:
        from anthropic import Anthropic
    except ImportError:
        print("the analyst needs the AI extra: pip install 'netpulse-monitor[ai]'")
        return 1

    client = Anthropic()
    request = {
        "model": "claude-opus-5",
        "max_tokens": 2048,
        "system": SYSTEM,
        "messages": [
            {
                "role": "user",
                "content": f"Evidence:\n{_evidence(store, config)}\n\nQuestion: {question}",
            }
        ],
    }
    try:
        # Server-side refusal fallbacks on by default for Opus 5, per current guidance.
        stream_ctx = client.beta.messages.stream(
            **request, betas=["server-side-fallback-2026-07-01"], fallbacks="default"
        )
    except TypeError:
        stream_ctx = client.messages.stream(**request)

    with stream_ctx as stream:
        for text in stream.text_stream:
            print(text, end="", flush=True)
    print()
    return 0
