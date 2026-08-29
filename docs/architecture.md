# Architecture

NetPulse is a local-first network monitor with **zero runtime dependencies** — stdlib
only, so it runs on a Raspberry Pi as readily as a laptop. Nothing leaves the machine,
the dashboard binds to loopback, and the whole thing is one SQLite file plus a TOML
config.

## The dependency rule

Modules form a DAG with one direction of flow. Nothing lower may import anything higher.

```
                      cli · server (HTTP transport)
                                  │
                                 api                  ← the query layer
                                  │
                    ┌─────────────┼──────────────┐
                monitor        analysis        discover
                (collector)    quality           │
                    │          insights       vendors    ← registry (data)
                    │          allowance
                    │          speedtest
                    │          notify
                    └─────────────┼──────────────┐
                               storage        adapters   ← one per router family
                                  │              │
                                clock ─────── model      ← types, aggregation rules
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

One HTML file, every asset inline — no CDN, no build step, no fonts to fetch. The page
must render during the outage it is explaining, and a page that fetches from the network
to describe the network is a page that goes blank exactly when it is needed.

The dashboard merges sources rather than making you pick between them: a router knows
the radio and cannot see past its WAN port, a probe knows what the internet feels like
and nothing about the radio. Each metric resolves to whichever source reports it.
