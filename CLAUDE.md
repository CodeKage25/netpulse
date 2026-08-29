# Engineering conventions

NetPulse watches other people's networks from inside their homes. The rules follow from that.

## Non-negotiable

1. **Poll gently.** CPE routers (Huawei/ZTE boxes behind MTN, Airtel, Glo plans) are small
   embedded machines; Dishylink's router watchdog-rebooted from a single extra poll
   endpoint. One status sweep per cycle, exponential backoff on failure, never a new
   endpoint in the loop without measuring its cost, nothing per-client at high cadence.
2. **Never invent data.** A gap in recording is a gap in every chart and total. Bucketed
   reads return None for unsampled buckets; ranged answers carry a coverage fraction; no
   value is ever carried forward across a gap.
3. **Local-only.** Readings, history and credentials stay on the user's machine. Nothing
   phones home. The dashboard embeds every asset so it renders with the internet down —
   which is exactly when it is needed.
4. **Zero runtime dependencies** in the core. stdlib only. AI and MCP are optional extras
   that the core must never import at module level.
5. **Router credentials are live secrets.** Never log them, never store them outside the
   user's config file, never send them anywhere but the router itself.

## Correctness habits

- Latency-like metrics bucket by **max** (spikes must survive downsampling); signal-like
  by mean; counters by last. The metric registry says which; never guess at call sites.
- Time is injected (`clock`) everywhere. Tests never sleep and never touch the network:
  adapters take an injectable fetch/prober, and Huawei behaviour is tested against
  recorded XML fixtures.
- Day/week/month ranges align to local midnight — that is how a person thinks about
  "today's data".
- Parsers must survive garbage: a router mid-reboot returns half-formed XML, and that must
  register as a failed poll, not a crash.

## Style

- Clean code, no verbose comments; a comment only for a why the code can't show.
- Test names are sentences about behaviour.
