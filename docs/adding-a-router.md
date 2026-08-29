# Adding a router

NetPulse reads a router through one small contract — `read() -> Reading` — so a new
box is a new adapter and nothing else. Storage, outage detection, charts, the
diagnosis rules and the dashboard are all written against that contract and need no
changes. This walks the whole path, using the ZLT X17U as the worked example, because
that adapter was built exactly this way in about an hour.

## 1. Ask the router what it answers

```bash
netpulse probe-router http://192.168.0.1
```

This walks every probe in the vendor registry plus a short exploratory list, prints
the status, content type and first bytes of each reply, and elides anything
secret-shaped (tokens, session ids, IMSI/ICCID, serial numbers). Nothing it sends can
change the router's state.

Two lines matter most. Any endpoint that answered `200` with JSON or XML is a
candidate API. And under the web root, the **assets** line lists the script bundles the
router's own web UI loads:

```
  [ 200 ] web root         (text/html)
           assets: css/app.css, js/app.js, js/chunk-vendors.js
```

Those bundles are the specification. The UI has to call the API, so the API is in
there.

## 2. Read the bundles

Fetch them and search for the request shape. Router web roots are usually gzipped:

```python
import gzip, urllib.request

request = urllib.request.Request(
    "http://192.168.0.1/js/app.js", headers={"Accept-Encoding": "gzip"}
)
body = urllib.request.urlopen(request, timeout=10).read()
if body[:2] == b"\x1f\x8b":
    body = gzip.decompress(body)
source = body.decode("utf-8", errors="replace")
```

Minified bundles mangle variable names but keep **string literals intact**, so the
endpoint paths, command constants and field names survive. Grep for the endpoint you
saw answer, for `cmd`, `sessionId`, `login`, and for the quantities you want — `rsrp`,
`sinr`, `network_type`. Command tables and field-to-label maps are usually right there.

The X17U turned out to drive everything through one endpoint:

```
POST /cgi-bin/http.cgi
Content-Type: application/json;charset=UTF-8
{"cmd": 133, "method": "GET", "sessionId": ""}
```

## 3. Find what works without logging in

Try each read command unauthenticated. Many firmwares gate configuration but leave
status open, and an adapter that needs no password is one nobody has to configure.
On the X17U, commands 80, 113, 133, 205 and 232 all answer with `sessionId: ""`;
104, 207, 337, 355 and 402 return `NO_AUTH`. That was enough — signal, connection
state and byte counters need no login at all.

**Poll gently while you do this.** One request every two seconds, never in parallel,
and stop the moment the box gets slow. These are fragile embedded devices; Dishylink's
router watchdog-rebooted from a single extra poll endpoint. Never send anything that
writes, reboots, or attempts a login you have not been given credentials for — several
firmwares lock the account for minutes after three failures.

## 4. Write the adapter

One file in `netpulse/adapters/`, one class, one `read()`. Register the kind in
`netpulse/adapters/__init__.py`. The rules that matter:

- **Take an injectable `fetch`.** Every test then runs without a network.
- **Absent is not zero.** An empty field is `None`, never `0.0`. Recording an
  unpopulated RSRP as zero draws a flat line where there is no measurement.
- **Check the payload, not the status code.** Several firmwares answer every error
  with HTTP 200 and `success: false`.
- **Raise `AdapterError` on a failed poll.** The collector records it as down and
  keeps running; it never crashes the loop.
- **Never publish a rate you had to invent.** If the firmware gives cumulative
  counters and no rate, difference them — and when a reboot or a 32-bit wrap makes the
  delta meaningless, re-baseline and emit *nothing* for that cycle. A wrapped counter
  published as throughput is a multi-gigabyte burst that never happened, and the charts
  keep it forever.
- **Ride heavy endpoints on every Nth sweep.** The routine poll should be one request.
  Anything expensive (host lists, full RF sweeps) rides a cycle counter, and failing it
  must not fail the poll.

## 5. Add it to the registry

`netpulse/vendors.py` is data. One entry gives you auto-discovery, the model name in
the UI, and a place in the diagnostic:

```python
Vendor(
    name="ZLT",
    kind="zlt",
    addresses=("192.168.0.1", "192.168.1.1"),
    signatures=(
        Signature(
            "/cgi-bin/http.cgi",
            body=b'{"cmd":113,"method":"GET","sessionId":""}',
            headers={"Content-Type": "application/json;charset=UTF-8"},
        ),
    ),
    match=_match_zlt,
)
```

A matcher returns the model name, `""` for "this family, model unknown", or `None` for
"not this family" — and those three mean genuinely different things. Be **specific**:
matching a payload that merely parsed makes discovery confidently wrong, which is worse
than silent, because it sends someone to configure an adapter that can never work.
Tests enforce that every registry probe is read-only, carries no credential, and
refuses the other families' payloads.

## 6. Test against captured payloads

Paste real responses into the test file, trimmed to the fields you read. Fixtures
copied from hardware fail when the adapter stops matching real firmware; fixtures you
invented fail only when it stops matching your idea of one. `tests/test_zlt.py` is the
model — it pins the reboot case, the counter wrap, the first-poll-has-no-rate case, and
the every-sixth-sweep cadence.

If your fixtures raise on an unknown route, raise `OSError`, not `KeyError`: a real
router that has nothing at a path raises a connection or HTTP error, and a fixture that
raises the wrong type will hide how your adapter actually fails.

## Sending it back

Open an issue with your `probe-router` output — elided, so it is safe to paste — and a
pull request with the adapter, the registry entry and the captured-payload tests. If
you only have the router and not the time, the `probe-router` output alone is genuinely
useful: it is what the X17U adapter was built from.
