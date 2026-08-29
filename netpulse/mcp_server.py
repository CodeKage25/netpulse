"""Serve your connection's history to any MCP client, read-only.

Point Claude Desktop or Claude Code at `netpulse mcp` and Claude becomes your network
analyst with live access to the same store the dashboard reads: status, history, events
and the rule-based diagnosis. Read-only by design — nothing here can touch a router.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import timedelta

from netpulse.config import Config
from netpulse.insights import diagnose
from netpulse.storage import Store, utcnow


def build_tools(store: Store, config: Config) -> dict[str, Callable[..., str]]:
    """The tool functions, importable without the MCP dependency so tests cover them."""

    def get_status() -> str:
        """Latest reading, labels and 24h coverage for every monitored connection."""
        now = utcnow()
        return json.dumps(
            {
                name: {
                    "latest": {metric: value for metric, (_, value) in store.latest(name).items()},
                    "labels": store.latest_texts(name),
                    "coverage_24h": store.coverage(
                        name, now - timedelta(hours=24), now, config.interval_s
                    ).fraction,
                }
                for name in store.sources()
            },
            default=str,
        )

    def get_history(source: str, metric: str, hours: float = 24, buckets: int = 48) -> str:
        """Bucketed history for one metric. Null buckets were not sampled — say so rather
        than interpolating. Latency buckets carry the worst value in each interval."""
        now = utcnow()
        since = now - timedelta(hours=hours)
        series = store.history(source, metric, since, now, min(int(buckets), 500))
        return json.dumps(
            {
                "times": [start.isoformat() for start, _ in series],
                "values": [value for _, value in series],
                "coverage": store.coverage(source, since, now, config.interval_s).fraction,
            }
        )

    def get_events(hours: float = 168) -> str:
        """Outages and degradations, newest first."""
        return json.dumps(
            [
                {
                    "source": event.source,
                    "kind": event.kind.value,
                    "severity": event.severity.value,
                    "started_at": event.started_at.isoformat(),
                    "ended_at": event.ended_at.isoformat() if event.ended_at else None,
                    "detail": event.detail,
                }
                for event in store.events(since=utcnow() - timedelta(hours=hours))
            ]
        )

    def get_diagnosis(source: str) -> str:
        """Rule-based findings with evidence. Narrate these; never invent measurements."""
        return json.dumps(
            [
                {
                    "severity": insight.severity.value,
                    "title": insight.title,
                    "detail": insight.detail,
                    "evidence": insight.evidence,
                }
                for insight in diagnose(store, source, utcnow())
            ]
        )

    def list_metrics(source: str) -> str:
        """Metric names recorded for a source, for use with get_history."""
        return json.dumps(sorted(store.latest(source).keys()))

    return {
        fn.__name__: fn for fn in (get_status, get_history, get_events, get_diagnosis, list_metrics)
    }


def serve_mcp(store: Store, config: Config) -> int:
    try:
        try:
            from mcp.server import MCPServer as Server
        except ImportError:
            from mcp.server.fastmcp import FastMCP as Server
    except ImportError:
        print("the MCP server needs the extra: pip install 'netpulse-monitor[mcp]'")
        return 1

    server = Server("netpulse")
    for name, fn in build_tools(store, config).items():
        server.add_tool(fn, name=name, description=fn.__doc__ or "")
    server.run(transport="stdio")
    return 0
