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

- [x] Scaffold, pyproject, CLAUDE.md, plan
- [x] `model.py` — Reading, Event, Insight
- [x] `storage.py` — SQLite, bucketed history, coverage, events
- [x] `adapters/` — base contract, fake (demo+tests), probe, huawei
- [x] `monitor.py` — collector loop, outage/degraded state machine, backoff
- [x] `insights.py` — rule-based diagnosis with evidence
- [x] `web/dashboard.html` — tiles, charts, events, insights; no external assets
- [x] `server.py` — API + SSE + static, stdlib http.server
- [x] `cli.py` — run/status/events/diagnose/ask/mcp, --demo
- [x] ~~ask/mcp~~ built, then removed at user direction (recoverable at f72eaf1)
- [x] Tests for all of it, no network
- [x] README with honest limits

---

# v0.2 — depth

- [x] Retention ladder: per-minute sufficient statistics, seamless history, exact spikes
- [x] Devices on the network (Huawei host-list, own gentler cadence)
- [x] On-demand speed test, loud about its ~30 MB cost, never scheduled
- [x] OS notifications: down and back-with-duration, direction-keyed throttle
- [x] Dashboard: devices panel, uptime tile, speed test button, undistorted axis labels

## Review

65 tests, ruff and strict mypy clean, demo verified end-to-end with a rendered screenshot.

Bugs found while building, in the honest column:
1. A big sed-style patch silently missed its target after ruff reformatted the file, and I
   had not asserted on the replace — the exact lesson already in airlock's lessons file.
   Every patch now asserts.
2. Rendered-screenshot review caught a real diagnosis bug: insights computed "typical
   latency" from MAX-bucketed series (right for charts, wrong for norms), so a healthy
   link with rare spikes was blamed on the provider. Diagnosis now aggregates by mean,
   with a regression test of exactly that link shape.
3. Test fixtures raised KeyError where a real router raises OSError, hiding how the new
   host-list call would actually fail. Fixtures now fail like hardware.


## 2026-08-30 — rules UI, FastMile, grade calibration

- [x] Rules screen: every rule with its live verdict, remaining allowance, and whether it
      blocks or only reports. Verdicts recomputed per request, never cached.
- [x] Fixed `STATIC` being read above its own declaration in `app.js` — a top-level TDZ
      throw that took the rest of the file with it.
- [x] An unmeasured allowance no longer renders as a bar at 0%. Verdicts carry whether
      anything was measured; the panel says so instead.
- [x] Nokia FastMile adapter — per-carrier radio, no invented connection state, counter
      differencing shared through `core.rates` (now used by ZLT too).
- [x] Quality grade calibrated against the real database: F (22) -> A (98) on a 20 ms
      link. Jitter definition, p95 rank, and unmeasured-loss credit all corrected.

### Still open
- Federation — the real answer to per-device usage the CPE firmware cannot report.
- Week-over-week anomaly detection; scheduled speed tests.
- The running instance needs a restart to pick up the grade and jitter fixes.
