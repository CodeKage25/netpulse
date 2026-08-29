# NetPulse

**Local-first monitoring for any home connection — MTN, Airtel, Glo, Starlink, fibre,
anything with a gateway.**

Your ISP's app tells you what they want you to know. NetPulse measures what you actually
get — latency, loss, signal, throughput, data used — records it on your own machine, and
tells you in plain language whether the problem is your WiFi, your router's placement, or
the network you're paying for.

```bash
pip install netpulse-monitor
netpulse run          # zero config: watches the connection you're on
```

Open http://127.0.0.1:8787. No account, no cloud, no telemetry. Everything stays on your
machine, and the dashboard has zero external assets — it keeps rendering **during** the
outage, which is exactly when you want it.

> Inspired by [Dishylink](https://github.com/DaveyHert/Dishylink)'s local-first design,
> generalised beyond Starlink: adapters are named for hardware, not carriers, because
> MTN, Airtel, Glo and 9mobile broadband boxes are Huawei and ZTE underneath.

## What it watches

| Adapter | Covers | Setup |
|---|---|---|
| `probe` | **any connection at all** — TCP latency to the internet, gateway latency, DNS timing, jitter, loss | none |
| `huawei` | Huawei LTE/5G CPE and MiFi (MTN HynetFlex, most MTN/Glo/9mobile routers): signal (RSRP/RSRQ/SINR), band, operator, live throughput, **data used this month**, SMS where balance texts arrive | router URL |
| `zte` | ZTE CPE (many Airtel boxes, ZTE MiFis): same story, one JSON request per sweep | router URL |
| `demo` | a synthetic LTE link with congestion and an outage, for trying the dashboard | `--demo` |

The probe works alongside a router adapter, so "the router says the signal is fine" and
"the internet actually answers" are measured separately — which is the whole diagnosis.

```toml
# ~/.netpulse/netpulse.toml
[[source]]
name = "mtn"
kind = "huawei"
url = "http://192.168.8.1"

[[source]]
name = "wan"
kind = "probe"
```

## What makes it honest

Lessons inherited from Dishylink's hard-won experience, built in from day one:

- **A gap in recording is a gap.** Charts break their line, totals count only sampled
  time, and every ranged answer carries a coverage fraction ("recorded 82% of this
  period"). No value is ever carried forward across a gap.
- **Latency buckets keep the worst value.** A 2-second spike averaged into a minute reads
  as fine; bucketing by max means the spike survives downsampling, because the spike is
  the story.
- **Poll gently.** Carrier CPE routers are small embedded boxes (Dishylink's router
  watchdog-rebooted from a single extra poll endpoint). One sweep per cycle, exponential
  backoff when a router struggles, and every call is time-bounded — an unbounded call
  doesn't just stall, it *invents the outage it then writes down*.
- **A blip is not an outage.** Three consecutive failed polls open an outage event; one
  failed poll is a satellite handover, a hiccup, nothing.

## Diagnosis

`netpulse diagnose` (and the Diagnosis panel) answers the questions people actually argue
about, from evidence, with the numbers shown:

- **"Is it my WiFi or is it MTN?"** — gateway latency vs internet latency, separated.
- **"Should I move the router?"** — SINR and RSRP read against real thresholds, including
  the honest opposite: *"radio conditions are excellent; if speeds are still poor, the
  limit is the plan, not the placement."*
- **Slow DNS** (with the fix), **flapping** (3+ outages/day), **congestion hours** (this
  hour vs your own 24h baseline).

The diagnosis is deliberately deterministic — no model, no API key, no cost, same answer
every time, evidence attached. For those who want it, two small optional extras exist:
`netpulse ask "why was last night bad?"` (Claude narrates your recorded evidence — it is
never asked to invent a number) and `netpulse mcp` (serve history read-only to Claude
Desktop/Code). Neither touches the core, and nothing AI-facing can touch a router.

## CLI

```
netpulse run [--demo] [--port 8787]   record + dashboard
netpulse status                       latest reading per source, with 24h coverage
netpulse events [--hours 48]          outages and degradations
netpulse diagnose                     rule-based findings (exit 1 on critical)
netpulse ask "question"               Claude over your own history   [ai extra]
netpulse mcp                          serve history to MCP clients   [mcp extra]
```

## Design

- **Zero runtime dependencies.** stdlib only — sqlite3, urllib, tomllib, http.server.
  Installs and runs on a Raspberry Pi with nothing else.
- One small adapter contract (`read() -> Reading`); everything else — storage, outage
  detection, charts, insights, AI — is written against it. A new router is a new adapter,
  nothing more.
- SQLite at `~/.netpulse/history.db`, append-only samples, WAL. Dashboard is served by the
  collector process and bound to localhost by default.
- Tests never touch the network: adapters take injectable probes/fetchers, and router
  behaviour is tested against recorded XML/JSON fixtures — including half-formed XML from
  a router mid-reboot, which must register as a failed poll, not a crash.

## Known limits (read before trusting it)

- **No retention policy yet.** Samples accumulate in SQLite indefinitely (~1.5 MB/day/source
  at 5s cadence). A minute-rollup ladder is the next piece of work.
- **Huawei/ZTE coverage is firmware-dependent.** These APIs are undocumented; fields vary
  by firmware. Missing optional endpoints degrade gracefully, but a firmware that renames
  fields needs an adapter update. SMS reading requires router credentials.
- **The probe measures TCP handshakes, not ICMP** — unprivileged and honest about what
  applications feel, but a few ms above what `ping` reports.
- **No Starlink adapter yet.** The probe monitors a Starlink connection fine; dish
  telemetry (obstruction, power) needs the gRPC schema — planned, and Dishylink covers it
  deeply today.
- **Windows untested.** Gateway discovery and ping parsing are macOS/Linux; the rest is
  portable.
- **Data-balance SMS parsing is not attempted.** Carrier message formats churn; NetPulse
  shows you the messages rather than mis-parsing them.

## License

MIT.
