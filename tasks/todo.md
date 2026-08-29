# NetPulse v0.1 — build plan

A local-first monitor for *any* home connection — MTN broadband (Huawei/ZTE-based routers),
Starlink, fibre, anything with a gateway — inspired by Dishylink but not tied to one vendor.

Acceptance: `uv run pytest` green with zero network access, `netpulse run --demo` shows a
live dashboard, `netpulse run` on a real MTN (Huawei) LAN shows signal + traffic + data.

## Design decisions taken up front

- **Adapters, not vendors.** Every source of readings implements one small contract:
  `read() -> Reading`. Universal `probe` adapter (TCP/DNS/ping timings) works on any network
  with zero configuration; `huawei` covers MTN/Airtel/Glo-style LTE and 5G CPE routers.
  Starlink/ZTE later — the probe already monitors those links meanwhile.
- **Poll gently.** Dishylink's router watchdog-rebooted from one extra poll endpoint. CPE
  routers are small embedded boxes: one status sweep per cycle (default 5s), nothing
  per-client, back off on failure.
- **Honest gaps.** History is what was actually sampled. Bucketed reads return None for
  unsampled buckets, every ranged answer carries a coverage fraction, and nothing ever
  extends "last known value" across a gap.
- **Latency buckets by max**, so spikes survive downsampling instead of averaging away.
- **Local-only.** SQLite in ~/.netpulse, dashboard served from the collector process with
  zero external assets, so it still renders during an outage — which is when you need it.
- **Zero runtime dependencies.** stdlib only (sqlite3, urllib, tomllib, http.server).
  AI/MCP are optional extras.
- **AI that's honest.** Rule-based diagnosis first (deterministic, evidence-carrying);
  `netpulse ask` (Claude analyst over your own history) and an MCP server as extras.

## Tasks

- [ ] Scaffold, pyproject, CLAUDE.md, plan
- [ ] `model.py` — Reading, Event, Insight
- [ ] `storage.py` — SQLite, bucketed history, coverage, events
- [ ] `adapters/` — base contract, fake (demo+tests), probe, huawei
- [ ] `monitor.py` — collector loop, outage/degraded state machine, backoff
- [ ] `insights.py` — rule-based diagnosis with evidence
- [ ] `web/dashboard.html` — tiles, charts, events, insights; no external assets
- [ ] `server.py` — API + SSE + static, stdlib http.server
- [ ] `cli.py` — run/status/events/diagnose/ask/mcp, --demo
- [ ] `ask.py` + `mcp.py` — optional extras
- [ ] Tests for all of it, no network
- [ ] README with honest limits
