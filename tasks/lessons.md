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

## 2026-08-29 — NetPulse

### When given a reference implementation, study the artefact, not just the source
I read Dishylink's collector architecture closely and never opened their UI, then
shipped a single scrolling page against a multi-view app with per-tile detail views,
distribution charts and written explainers. The user's word for it was "mid," twice.
**Rule:** when handed a reference, find its rendered output first — screenshots,
demos, `assets/`, the landing page. `landing/src/assets/shots/` had 27 real UI
captures the whole time. Architecture is half the reference; the artefact is the
other half, and it is the half the user actually sees.

### An empty-looking UI is usually a data problem wearing a design costume
The dashboard looked bare because the router wasn't being read — two tiles is what
"probe only" honestly looks like. I nearly redesigned my way around a missing
adapter. **Rule:** before treating sparseness as a design failure, check what the
page would show if every source were working. Then fix whichever is actually broken.

### Ask what the user has, not what the vendor probably shipped
"MTN router" led me to Huawei, then to ZTE. It is a ZLT X17U — Tozed — and neither
adapter could ever have worked. Two rounds lost to inference. **Rule:** identify real
hardware from the device itself. `netpulse probe-router` now exists so the router
answers the question instead of me guessing at it.

### Make the user pick, and you have designed the hole they fall into
The router knows the radio and cannot see past its WAN port; the probe knows the
internet and nothing about the radio. A source dropdown meant either choice showed
half a connection. **Rule:** when two sources describe the same thing from different
sides, merge them and resolve per metric. A picker between two incomplete views is a
design failure disguised as flexibility.

### Rendering the thing keeps finding bugs that tests do not
A screenshot showed uptime at 0.0% during an outage (bucketing by MIN before
averaging, so one bad minute zeroed a day), and later "Best 191ms" above a
distribution axis starting at 150ms (the Best hero read off a MAX-bucketed series —
the best bad minute, not the best reading). Both had passing tests around them.
**Rule:** render every view and read the actual numbers against each other. Two
figures on one screen that cannot both be true is the cheapest bug detector there is.

### `cmd; echo $?` is not a gate — it is a report nobody reads
Twice in one session I ran `pytest -q >/dev/null; echo "pytest=$?"` and then committed
in the *next* shell invocation, having never looked at the number. A red suite went in
both times. Piping to `tail` does the same damage: the pipeline's exit code is `tail`'s,
which is always zero. **Rule:** the verification and the commit go in one `&&` chain —
`pytest -q && ruff check -q . && git commit` — so a failure physically cannot reach the
commit. Never `;` between a check and an action.

### A guard that greps prose will flag the comment explaining the guard
`test_the_query_layer_holds_no_transport` fired on api.py's docstring saying it has no
sockets. Then `test_the_scene_needs_no_library` fired on scene.js's comment explaining
why it needs no WebGL. Same bug, twice, a few hours apart. **Rule:** a check about what
code *does* must read code — parse the imports, or strip comments first. Prose in a
well-documented file names the thing it is avoiding, so scanning raw text finds exactly
the files that were most careful.

### A multi-step edit script that asserts must write before it can fail
A `python - <<PY` block that did `assert old in s` twice and `write_text` once at the end
lost the whole edit when the second assert failed — including the first, correct
replacement. It then looked like the edit had been applied, because the *script* printed
its progress. **Rule:** make edit scripts idempotent (`if X not in s`) and write after
each successful replacement, or verify the file afterwards rather than trusting the
script's own output.

### Screenshots find bugs that reading code cannot — six in one pass
The user sent seven screenshots of the running dashboard. They contained six real bugs
that months of unit tests had not caught, because every one was a *disagreement between
two things on the same screen* rather than a wrong value in isolation:

- Latency reported the **worst of several anycast targets**, so one badly-routed
  destination read as the connection's latency. Visible instantly as a square wave; a
  five-fold overstatement that graded a healthy link F.
- "Best −96 dBm, Worst −90 dBm" — extremes computed as min/max with no notion of which
  direction is good.
- The ping-success panel **plotted raw loss under a success heading**, so its chart
  disagreed with its own figures, and its axis ran to 120%.
- "Worst 0.0 Mbps" printed above an explainer saying an idle link reads near zero.
- A tile showing 100% beside a sparkline spiking upward on every lost packet.
- A deep link opening before sources resolved, querying "" and reading as "no data".

**Rule:** for anything with a UI, render it and *look* — at full panels, not one number.
Then read every figure against its neighbours and against its own caption. A value that
is wrong on its own is rare; a value that contradicts the thing beside it is common, and
only visible when both are on screen together.

### A direction flag must say which value it describes
`higherIsBetter` on a metric stored as loss but displayed as success inverted best/worst
a second time. **Rule:** when a metric is stored one way up and shown the other, every
flag about it must state explicitly whether it describes the stored or the displayed
value — and the tests should pin that sentence, because the code reads correct either way.

### A weighted term is only as good as the thing it names
The quality grade weighted jitter above the tail, deliberately — and then fed that weight
the standard deviation of an hour of latency, which is not jitter. It reads high for slow
drift, and highest of all when the *measurement* changes: fixing the latency bug made the
score plunge, because the step between the old and new figures was scored as though the
network had done it. A real hour with a 20 ms median and 0.26% loss graded F.

**Rule:** when a formula weights a term heavily, check what the term actually computes on
real data before trusting the weighting. Compare each input against an independent
estimate of the same quantity — pstdev over the window against consecutive movement
against the probe's own per-poll measurement. Three numbers that should agree and don't
locate the bug immediately; one number on its own never does.

### Checking is not always browsing
Rendering the page found two bugs in one screenshot, but the user's instruction stands:
verify from the data and the tests, and reserve a render for when the question is
genuinely visual. A calibration question is answered by running the estimator over the
real database, not by looking at the number it printed.
