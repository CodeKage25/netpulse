# Security

## What NetPulse touches

It reads a router on your own network and writes a SQLite file on your own machine. The
dashboard binds to `127.0.0.1` and is not reachable from elsewhere on the network unless
you deliberately forward the port.

Nothing is transmitted anywhere, with one exception: **alert channels**, which exist
because you configured a URL. Those carry the alert text and nothing else — never
credentials, addresses, device names or history.

## Router credentials

A router password is optional. Everything the outage detector needs answers without one;
a password unlocks only the connected-device list and blocking.

Where a password is used, it is hashed with the router's own challenge token and only the
digest is sent. It is never logged, never included in `probe-router` output, and never
leaves the machine. `~/.netpulse/netpulse.toml` is created `0600`, and NetPulse warns on
every start if a file holding a password is readable by other accounts.

A refused login is remembered rather than retried — several firmwares lock the account
for minutes after three failures.

## Writes to your router

NetPulse is a reader. The single exception is blocking a device, which is always an
explicit user action behind a confirmation, never automatic and never on a poll. Tests
enforce that nothing in the discovery path can write, reboot, or carry a credential.

## Reporting a vulnerability

Open a GitHub security advisory on the repository, or an issue if it is not sensitive.
Please include what you observed and how to reproduce it.

## Scope note

The vendor APIs NetPulse speaks are undocumented and unofficial. They can change without
notice. `netpulse probe-router <url>` prints what a firmware actually answers — with
tokens, session ids, IMSI/ICCID and serial numbers elided — so its output is safe to
paste into an issue.
