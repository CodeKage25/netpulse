# NetPulse

**Local-first monitoring for any home connection — MTN, Airtel, Glo, Starlink, fibre,
anything with a gateway.**

Your ISP's app tells you what they want you to know. NetPulse measures what you actually
get — latency, loss, signal, throughput, data used — records it on your own machine, and
tells you in plain language whether the problem is your WiFi, your router's placement, or
the network you're paying for.

```bash
pip install netpulse-monitor
netpulse run          # zero config — finds your router by itself
```

Open http://127.0.0.1:8787. The probe starts measuring your connection immediately, and
discovery scans the gateway and the well-known CPE addresses in the background — a Huawei
or ZTE box (which is what MTN, Airtel and Glo ship) is found, watched and remembered
without you touching a config file. There is also a **Scan for routers** button in the
dashboard's settings drawer, and `netpulse discover` for the terminal.

No account, no cloud, no telemetry. Everything stays on your
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

Discovery writes `~/.netpulse/netpulse.toml` for you; edit it only if your router hides
somewhere unusual:

```toml
[[source]]
name = "mtn"
kind = "huawei"
url = "http://192.168.0.1"
# username/password unlock SMS reading (where balance texts arrive)

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

The diagnosis is deliberately deterministic — no model, no API key, no cost, the same
answer every time, evidence attached.

## CLI

```
netpulse run [--demo] [--port 8787]   record + dashboard (+ background discovery)
netpulse discover                     find your router, write the config
netpulse probe-router <url>           show what a router answers (for new adapters)
netpulse status                       latest reading per source, with 24h coverage
netpulse events [--hours 48]          outages and degradations
netpulse diagnose                     rule-based findings (exit 1 on critical)
```

## Design

- **Zero runtime dependencies.** stdlib only — sqlite3, urllib, tomllib, http.server.
  Installs and runs on a Raspberry Pi with nothing else.
- One small adapter contract (`read() -> Reading`); everything else — storage, outage
  detection, charts, insights — is written against it. A new router is a new adapter,
  nothing more.
- SQLite at `~/.netpulse/history.db`, append-only samples, WAL. Dashboard is served by the
  collector process and bound to localhost by default.
- Tests never touch the network: adapters take injectable probes/fetchers, and router
  behaviour is tested against recorded XML/JSON fixtures — including half-formed XML from
  a router mid-reboot, which must register as a failed poll, not a crash.

## Routers it reads

| Adapter | Boxes | API | Login needed |
|---|---|---|---|
| `zlt` | ZLT / Tozed — MTN Nigeria's own-brand 5G (X17U and relatives) | `POST /cgi-bin/http.cgi` | no |
| `huawei` | Huawei CPE — MTN, Glo, 9mobile | XML at `/api/*` | only for SMS |
| `zte` | ZTE MC-series — Airtel, many MiFis | `/goform/` or `/reqproc/` | no |
| `probe` | anything at all | ICMP/DNS/TCP from this machine | n/a |

Your router missing? `netpulse probe-router http://<its address>` prints what its
firmware actually answers, with anything secret elided. That output is what an adapter
gets built from — the ZLT adapter above was written from exactly that, then tested
against payloads captured verbatim from a live X17U.

## The dashboard

A dark instrument panel in Dishylink's manner — huge live figures with sparklines, area
charts with per-chart time ranges, an events feed, and a **connection quality grade**
(A–F, scored on p95 latency, jitter, tail and loss, with jitter weighted above the tail
because a predictable 40ms beats a fast-but-spiky link). Light theme included; every
asset inline, so it renders mid-outage.

## Also in the box

- **Devices on the network** — the router's client list (name, IP, MAC, last seen),
  polled on its own gentler cadence.
- **On-demand speed test** — `netpulse speedtest` or the dashboard button. On demand
  *only*: it moves ~30 MB of real data and many watched connections are metered, so
  nothing ever schedules it, and both entry points state the cost first.
- **OS notifications** — down, and back (with how long it lasted), throttled so a
  flapping link cannot become a storm. `notifications = false` in the config turns it off.
- **A retention ladder** — raw samples are kept 7 days, then folded into per-minute
  sufficient statistics (count/sum/min/max) that compose exactly: a spike survives
  compaction because max-of-max is exact, means stay weighted, and coverage is unchanged.
  History is seamless across the boundary, and a year stays queryable in milliseconds.

## Known limits (read before trusting it)
- **Per-device throughput is not measured.** The device panel shows who is connected;
  per-client byte counters vary wildly by firmware and are not yet read.
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
