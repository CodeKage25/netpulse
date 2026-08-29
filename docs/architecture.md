# Architecture

NetPulse is a local-first network monitor with **zero runtime dependencies** — stdlib
only, so it runs on a Raspberry Pi as readily as a laptop. Nothing leaves the machine,
the dashboard binds to loopback, and the whole thing is one SQLite file plus a TOML
config.

## The dependency rule

Modules form a DAG with one direction of flow. Nothing lower may import anything higher,
and **`tests/test_architecture.py` fails the build when something does** — an
architecture that lives only in a document is a suggestion, and the person adding a
router a year from now will not have read this file.

    netpulse/
      core/       model · clock · storage          layer 0-1
      sources/    adapters · vendors · discover    layer 1
      analysis/   quality · insights · allowance · path · export · speedtest
      alerting/   alerts · channels · notify
      config.py                                    layer 4
      monitor.py  the collector                    layer 5
      web/        api · server · assets            layer 6
      cli.py                                       layer 7

The test also holds four specific edges that matter more than the general rule: an
adapter may never reach the store or the collector; `api.py` may never import a
transport; every package must say what it is for; and `core/clock.py` is the only place
the system clock is read.

```
                                 cli
                                  │
                      web/  api → server → the page
                                  │
                              monitor.py                ← the collector
                                  │
                               config.py
                                  │
                    ┌─────────────┴─────────────┐
                alerting/                   analysis/
          alerts · channels · notify   quality · insights · allowance
                    │                  path · export · speedtest
                    └─────────────┬─────────────┘
                              sources/                  ← one file per router family
                    adapters · vendors · discover · snmp
                                  │
                               core/
                       storage → clock · model
```

Two consequences worth stating because they are the point:

- **`api` holds every question, and knows nothing about HTTP.** The entire API is tested
  by calling it — no sockets, no ports, no server to start. `server.py` is a thin
  transport, and the moment routing starts making decisions about data, that decision
  belongs in `api.py`.
- **`adapters` depend only on `model`.** An adapter cannot reach storage, the collector,
  or the web layer even by accident, which is what keeps "add a router" a one-file
  change.

## The two contracts

Everything else is built on exactly two interfaces.

**An adapter reads a source.**

```python
class Adapter(Protocol):
    name: str
    kind: str

    def read(self) -> Reading: ...
```

`Reading` is `{metrics: dict[str, float], texts: dict[str, str], devices: list[...]}`.
A failed poll raises `AdapterError`; the collector records it as down and keeps running.
Storage, outage detection, charts, diagnosis and the dashboard are all written against
this, which is what makes NetPulse network-agnostic where Dishylink is Starlink-only.

**A vendor is recognised by one request.**

```python
Vendor(name, kind, addresses, signatures, match)
```

`netpulse/vendors.py` is data. One entry gives auto-discovery, the model name in the UI,
and a place in the `probe-router` diagnostic — see
[adding-a-router.md](adding-a-router.md).

## Metric naming, and why aggregation is a property of the name

Metrics are dotted and namespaced: `latency.internet_ms`, `signal.rsrp_dbm`,
`traffic.down_bytes_s`, `data.month_total_bytes`, `router.uptime_s`, `up`.

`model.AGG_RULES` maps a **prefix** to how that family folds into a bucket, longest
prefix winning. This is not a formatting detail — it is the difference between a chart
that tells the truth and one that does not:

| Prefix | Fold | Why |
|---|---|---|
| `latency.` `loss.` `dns.` | MAX | A spike averaged into a minute reads as fine. The worst moment is the point. |
| `signal.` `traffic.` | MEAN | These are levels; the average over a minute is the honest summary. |
| `data.` `router.` `speedtest.` | LAST | Odometers. A mean of an odometer means nothing. |
| `up` | MIN | A bucket that saw any failure shows as down. |

A new metric inherits the right behaviour from its name. Getting this wrong is subtle
and expensive: the diagnosis rules once computed "typical latency" from MAX-bucketed
data and blamed the provider for a healthy link with rare spikes.

## Storage: raw for a week, sufficient statistics forever

`samples` holds raw readings for 7 days. `compact()` folds anything older into `rollup`
rows of `{count, sum, min, max}` per metric per minute, in one transaction, and deletes
the raw.

Those four numbers **compose exactly**: max-of-max is the true max, sums add for a
weighted mean, counts add for coverage. So `history()` reads raw and rollup together and
a query spanning the retention boundary is seamless — not stitched, not approximated.

## The honesty rules

These are load-bearing, and they are what the tests mostly enforce.

**A gap in recording is a gap in every chart and total.** A bucket nobody sampled returns
`None`, not zero and not the previous value. Charts break the line and shade the gap.
Every ranged answer carries a coverage fraction, and the UI prints it. A figure that
quietly spans a gap is worse than no figure, because it looks like knowledge.

**Absent is not zero.** An unpopulated field is `None`. Recording an empty RSRP as
`0.0` would draw a flat line where there is no measurement.

**Never publish a number you had to invent.** A first counter reading is a position, not
a rate. A reboot or a 32-bit wrap makes a delta meaningless, so the adapter re-baselines
and emits nothing for that cycle — a wrapped counter published as throughput is a
multi-gigabyte burst that never happened, and the charts keep it forever.

**Raw for extremes and percentiles, buckets for shape.** Best/worst and p95 come from
raw samples. Reading them off a MAX-bucketed series gives the *best bad minute*, which
is a different and wrong number.

**Under-claiming is the cheaper mistake.** Discovery reports a router it can name but not
read, rather than staying silent. A matcher that is unsure returns "not this family".
Uptime excludes unrecorded time rather than assuming it was up.

## Polling etiquette

CPE routers are fragile embedded boxes — Dishylink's watchdog-rebooted from one extra
poll endpoint. So:

- One sweep per cycle per source. Heavy endpoints (host lists, full RF sweeps) ride a
  cycle counter, and failing one must not fail the poll.
- Every call is bounded. An unbounded call does not merely stall: **it invents the
  outage it then writes down.**
- Failures back off exponentially to a ceiling rather than hammering a box that is
  already struggling.
- Discovery is parallel across addresses and strictly serial within one.
- Nothing in the vendor registry may write, reboot, or carry a credential. Tests enforce
  all three.

## Time

`clock.py` holds the only read of the system clock. Everything that needs "now" takes a
`Clock`, which is why the whole suite runs without sleeping and why billing cycles and
outage durations can be exercised at real dates.

## The web layer

### Why the UI is HTML

Because the alternative is Electron, and Electron costs about 200 MB, a code-signing
certificate, a per-platform build, and a release you have to notarize. Dishylink pays
all of that and still has no Linux build, no mobile, and unsigned Windows binaries that
raise a SmartScreen warning.

A local web dashboard costs none of it. `netpulse run` starts a stdlib HTTP server on
loopback and any browser on the machine is the client — the same code serves a laptop, a
phone on the same network, and a headless Raspberry Pi over SSH port-forwarding. Nothing
to install, nothing to sign, nothing to update separately from the Python package. It is
also why the dashboard is responsive down to a phone: a network monitor is most needed
on the device in your hand while the connection is misbehaving.

### One response, ten files

    web/assets/
      index.html         the shell — structure only, with {{styles}} / {{scripts}} slots
      css/app.css        the design system
      core/format.js     units, and the one place a design token is read
      core/store.js      shared state, and which source owns which metric
      core/metrics.js    the spec table driving tiles, charts and detail views
      core/chart.js      the SVG primitives
      views/*.js         one file per thing the user can look at
      app.js             wiring: events, routing, the poll loop

The server concatenates them in **dependency order** — they share a scope once joined,
so a module must come after everything it calls at load time — and caches the result.
Each keeps a banner naming it, so a browser stack trace still points at a file somebody
can open.

`tests/test_web.py` holds the boundaries: `core` may not call a view or render one, only
`app.js` binds events, no module may pass 400 lines, and the bundle is syntax-checked
**joined as well as separately**, because a `const` declared twice across two files only
fails once they meet.
The page must still arrive whole, with every asset inline — no CDN, no fonts to fetch,
no second request — because it has to render during the outage it is explaining, and a
page that fetches from the network to describe the network goes blank exactly when it is
needed. That guarantee is about what ships, not about what anyone has to edit.

**Why not a framework.** React or Vue would mean either a build step, which breaks
`pip install netpulse && netpulse run`, or a CDN fetch, which breaks the page precisely
when the network is down. Vendoring Preact would avoid both and is the reasonable next
move if the UI doubles again — but it puts someone else's rendering model in a project
whose whole identity is having no dependencies. Vanilla with real module boundaries
carries a long way, and the boundaries are what was actually missing.

### What the dashboard does with sources

It merges rather than making you choose. A router knows the radio and cannot see past
its WAN port; a probe knows what the internet feels like and nothing about the radio.
Each metric resolves to whichever source reports it, so the page shows one connection
instead of two partial ones.

