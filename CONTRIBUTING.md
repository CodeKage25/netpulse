# Contributing

The most useful contribution is **a router NetPulse cannot read yet**.

## Adding a router

Run this and paste the output into an issue:

```bash
netpulse probe-router http://<your router's address>
```

It is read-only, it elides anything secret, and it prints the script bundles the
router's own web UI loads — which are the specification for its API. The ZLT adapter was
written from exactly that output in about an hour.

If you want to write the adapter yourself, **[docs/adding-a-router.md](docs/adding-a-router.md)**
walks the whole path, including the etiquette that matters when you are poking at
somebody's hardware: one request every two seconds while exploring, nothing that writes,
and no login attempt without credentials — several firmwares lock the account after three
failures.

## The rules a change has to keep

Read **[docs/architecture.md](docs/architecture.md)** first. The short version:

- **A gap in recording is a gap in every chart and total.** Never return zero for
  something nobody measured, and never carry a value forward across a gap.
- **Absent is not zero.** An unpopulated field is `None` all the way to the screen.
- **Never publish a number you had to invent.** If a delta is meaningless — a reboot, a
  counter wrap, a first reading — emit nothing for that cycle.
- **Poll gently.** One sweep per cycle, heavy endpoints on a counter, every call bounded.
- **An adapter may never reach the store or the collector.** `tests/test_architecture.py`
  will fail you, and it is right to.

## Running it

```bash
uv sync
uv run pytest -q          # none of these touch the network
uv run ruff check .
uv run mypy netpulse/     # strict
```

Every adapter takes an injectable `fetch` and every module that needs the time takes a
`Clock`, so tests never sleep and never open a socket.

Test fixtures should be **payloads captured verbatim from real hardware**, trimmed to the
fields you read. Fixtures copied from a device fail when the adapter stops matching real
firmware; invented ones fail only when it stops matching your idea of one.

## Commit messages

Say what changed and why it was wrong before. If a render or a live device revealed the
bug, say so — that context is the most valuable part of the history.
