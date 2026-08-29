# Lessons

## Assert on every patch, especially after a formatter ran

**What happened.** A multi-part patch to server.py silently missed one replacement because
ruff had reformatted the target text, and the miss surfaced later as a KeyError in a live
API response — the same lesson already recorded in airlock's lessons file, violated again.

**Rule.** Every `str.replace` patch asserts the target exists first. A patch that can
silently no-op is worse than one that fails loudly.

## Screenshot review finds bugs tests don't

**What happened.** The rendered dashboard showed a diagnosis blaming the provider for
254ms latency on a link that was typically at 70ms. Root cause: insights computed
"typical" values from MAX-bucketed history — correct for charts, where the spike is the
story, wrong for norms, where it slanders a healthy link.

**Rule.** The aggregation must match the question. Charts ask "what was the worst moment"
(max); diagnosis asks "what is this connection normally like" (mean). Render the output
and read it as a user would; the numbers being plumbed correctly is not the same as the
numbers meaning what the sentence around them claims.

## Fixtures must fail the way hardware fails

**What happened.** Test fetch fixtures raised KeyError for unknown endpoints where a real
router raises OSError, so a new endpoint call crashed in tests in a way it never would in
production — masking the actual degrade-gracefully behaviour under test.

**Rule.** A fake's failure modes are part of its contract. Unknown route → OSError, like a
connection refused, not an assertion artifact.

## Study the prior art with an agent, steal decisions not code

**What happened.** A deep scout report on Dishylink surfaced the load-bearing lessons
(coverage as a first-class field, bounded calls so a timeout can't invent the outage it
records, poll-gently after their router watchdog-rebooted, max-bucketing) and their #1
regret (NDJSON over SQLite) — which became our design before writing a line.

**Rule.** For a "build a better X", clone X and mine its comments and regrets first; the
prose invariants at the top of their files are worth more than their code.
