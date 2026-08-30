# Changelog

## 0.6.0

The release that stopped guessing and went and checked.

### Added
- **Starlink** over gRPC-Web on port 9201 — no gRPC library, no protobuf compiler, no
  credentials. Port 9200 is HTTP/2 and out of reach of the standard library; 9201 serves
  the same service over HTTP/1.1.
- **SNMPv2c client** in the standard library (~300 lines), with verified MikroTik and
  Teltonika OIDs read from the vendors' own MIB files.
- **3D spectrum view.** Component carriers placed at their true frequency using 3GPP's
  own arithmetic, drawn at real bandwidth, coloured by SINR, receding through time.
- **Data usage by day, application and device**, kept apart because they measure three
  different things and will not sum.
- **Device blocking** — the first write NetPulse makes to a router, behind a
  confirmation, as a capability separate from the read contract.
- **Alert rules** with thresholds and durations, delivered to webhook, Slack, Discord,
  ntfy or Home Assistant.
- **Export**: Prometheus at `/metrics`, CSV, JSON, and an uptime report that reports
  uptime and coverage separately.
- **Path analysis** — `netpulse path` attributes a delay, and stops short of blaming
  anyone when a traceroute genuinely cannot tell.
- Per-application usage on the host, and the host recognising itself in the router's
  device list.
- CI: tests on Linux and macOS across Python 3.11 and 3.13, strict mypy, ruff, the
  dashboard syntax-checked joined as well as separately, and a gate that fails if
  installing NetPulse ever pulls in a dependency.

### Fixed
- **Data usage was 4.86% low.** The ZLT firmware labels its flow figures "MB" and means
  mebibytes — established against the router's own raw byte counter, where the ratio came
  back as exactly 1,048,576.
- **The speed test was dead.** Cloudflare refuses requests with no User-Agent; urllib
  sends one that gets refused. A 403 presenting as a broken connection.
- **A speed test took 108 seconds** on a slow link. A fixed byte budget is unbounded in
  time; download now stops at twelve seconds and the upload is sized from what download
  measured. Six seconds now.
- Uptime read 0% during an outage — it was bucketing by MIN before averaging, letting one
  bad minute zero a day.
- The data allowance read 227 MB beside a tile reading 23.8 GB: the odometer was anchored
  at the first reading NetPulse saw rather than at zero.
- Best/worst figures came from a max-bucketed series, giving the best *bad* minute.
- Latency is described as the TCP connect it is, not the ping it is not.

### Changed
- Restructured into layered packages, with `tests/test_architecture.py` failing the build
  when a layer reaches upward.
- The dashboard is ten authored files concatenated into one self-contained response, with
  tests holding the module boundaries.
