"""Getting the data out: Prometheus to scrape, CSV and JSON to keep.

A monitor that will not give you its numbers is asking you to trust it. Dishylink has
no export path at all — no metrics endpoint, no CSV, no dump — so its history is only
ever as useful as its own charts. NetPulse's history is a SQLite file you own, and
these are the three shapes people actually want it in:

- **Prometheus** for a Grafana dashboard or an existing alerting stack.
- **CSV** for a spreadsheet, which is what most disputes with an ISP are settled in.
- **JSON** for a script.

The honesty rules survive the export. A bucket nobody recorded is an empty CSV cell and
a JSON `null`, never a zero; and every ranged export carries the coverage fraction, so a
number pasted into a complaint can still say how much of the window it stands on.
"""

from __future__ import annotations

import csv
import io
import json
from datetime import datetime
from typing import Any

from netpulse.core.model import agg_for
from netpulse.core.storage import Store

#: Prometheus names cannot carry dots, and the convention is a unit suffix.
PROM_NAMES = {
    "latency.internet_ms": (
        "netpulse_latency_milliseconds",
        "gauge",
        "Round-trip time to the internet",
    ),
    "loss.pct": ("netpulse_packet_loss_ratio", "gauge", "Fraction of probe packets lost"),
    "traffic.down_bytes_s": (
        "netpulse_download_bytes_per_second",
        "gauge",
        "Current download throughput",
    ),
    "traffic.up_bytes_s": (
        "netpulse_upload_bytes_per_second",
        "gauge",
        "Current upload throughput",
    ),
    "signal.rsrp_dbm": ("netpulse_signal_rsrp_dbm", "gauge", "Reference signal received power"),
    "signal.sinr_db": (
        "netpulse_signal_sinr_db",
        "gauge",
        "Signal to interference plus noise ratio",
    ),
    "signal.rsrq_db": ("netpulse_signal_rsrq_db", "gauge", "Reference signal received quality"),
    "signal.rsrp_5g_dbm": ("netpulse_signal_rsrp_5g_dbm", "gauge", "5G carrier RSRP"),
    "signal.sinr_5g_db": ("netpulse_signal_sinr_5g_db", "gauge", "5G carrier SINR"),
    "signal.bars": ("netpulse_signal_bars", "gauge", "Signal bars as the router reports them"),
    "data.month_total_bytes": (
        "netpulse_data_month_bytes_total",
        "counter",
        "Data used this month",
    ),
    "data.month_down_bytes": (
        "netpulse_data_month_down_bytes_total",
        "counter",
        "Data downloaded this month",
    ),
    "data.month_up_bytes": (
        "netpulse_data_month_up_bytes_total",
        "counter",
        "Data uploaded this month",
    ),
    "router.uptime_s": ("netpulse_router_uptime_seconds", "gauge", "Router uptime"),
    "up": ("netpulse_up", "gauge", "1 when the source answered its last poll"),
}


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


def prometheus(
    latest: dict[str, dict[str, float]],
    texts: dict[str, dict[str, str]],
    coverage: dict[str, float],
) -> str:
    """The current reading of every source, in the text exposition format.

    Deliberately point-in-time rather than a range: Prometheus keeps its own history,
    and re-exporting ours would double-count into whatever it already stored.
    """
    lines: list[str] = []
    for metric, (name, kind, help_text) in PROM_NAMES.items():
        samples = [
            (source, values[metric])
            for source, values in sorted(latest.items())
            if metric in values
        ]
        if not samples:
            continue
        lines.append(f"# HELP {name} {help_text}")
        lines.append(f"# TYPE {name} {kind}")
        for source, value in samples:
            lines.append(f'{name}{{source="{_escape(source)}"}} {value:g}')

    # Coverage travels with the metrics: a scraper that keeps our numbers should be able
    # to see how much of the window they stood on.
    if coverage:
        lines.append("# HELP netpulse_coverage_ratio Fraction of the last hour actually recorded")
        lines.append("# TYPE netpulse_coverage_ratio gauge")
        for source, fraction in sorted(coverage.items()):
            lines.append(f'netpulse_coverage_ratio{{source="{_escape(source)}"}} {fraction:g}')

    if any(texts.values()):
        lines.append("# HELP netpulse_source_info Operator, network type and band as labels")
        lines.append("# TYPE netpulse_source_info gauge")
        for source, values in sorted(texts.items()):
            labels = ",".join(
                f'{key.replace(".", "_")}="{_escape(value)}"'
                for key, value in sorted(values.items())
            )
            lines.append(f'netpulse_source_info{{source="{_escape(source)}",{labels}}} 1')
    return "\n".join(lines) + "\n"


def series(
    store: Store,
    source: str,
    metrics: list[str],
    since: datetime,
    until: datetime,
    buckets: int,
) -> tuple[list[str], list[list[Any]]]:
    """One row per bucket, one column per metric, aligned on a shared time axis.

    Unrecorded buckets keep their row and carry `None`. Dropping them would let the
    survivors close ranks and hide a missed hour, which is the whole failure this
    project exists to avoid.
    """
    columns: dict[str, list[float | None]] = {}
    times: list[str] = []
    for metric in metrics:
        points = store.history(source, metric, since, until, buckets, agg_for(metric))
        if not times:
            times = [at.isoformat() for at, _ in points]
        columns[metric] = [value for _, value in points]

    header = ["time", *metrics]
    rows = [[times[i], *(columns[m][i] for m in metrics)] for i in range(len(times))]
    return header, rows


def to_csv(header: list[str], rows: list[list[Any]]) -> str:
    """CSV with empty cells for gaps — a spreadsheet reads an empty cell as no data and
    a zero as a measurement, and only one of those is true."""
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(header)
    for row in rows:
        writer.writerow(["" if cell is None else cell for cell in row])
    return out.getvalue()


def to_json(header: list[str], rows: list[list[Any]], source: str, coverage: float) -> str:
    return json.dumps(
        {
            "source": source,
            "coverage": round(coverage, 4),
            "columns": header,
            "rows": rows,
        },
        indent=2,
    )


def uptime(
    store: Store, source: str, since: datetime, until: datetime, interval_s: float
) -> tuple[float | None, float, float]:
    """Uptime as a fraction of *time*, plus the seconds up and down behind it.

    Counting polls is the obvious implementation and it is wrong, because the collector
    backs off while a source is failing: one down poll covers far more wall clock than
    one up poll, so treating them as equal makes an outage barely dent the figure. On a
    real day this read 99.17% where the truth was 79.89% — a nineteen-point error in the
    flattering direction, which is the worst way for a monitor to be wrong.

    Time up is counted from polls, which are regular while things work; time down comes
    from the outage events, which know exactly how long they lasted. Uptime is the ratio
    over the period actually observed, not over the whole window.
    """
    polls = store.stamped(source, "up", since, until)
    if not polls:
        return None, 0.0, 0.0

    spans = [
        (max(event.started_at, since), min(event.ended_at or until, until))
        # Overlapping, not merely starting-within: an outage that began before this
        # window is the one a report most needs to see.
        for event in store.events_overlapping(source, since, until)
        if event.kind.value == "outage"
    ]
    down_seconds = sum(max(0.0, (end - start).total_seconds()) for start, end in spans)

    up_seconds = 0.0
    for at, value in polls:
        if value >= 1.0:
            up_seconds += interval_s
        elif not any(start <= at <= end for start, end in spans):
            # A failed poll that never became an outage — a blip too short to declare
            # one. It is still time the connection was not working, and dropping it
            # would report a link that fails every other minute as flawless.
            down_seconds += interval_s

    observed = up_seconds + down_seconds
    if observed <= 0:
        return None, 0.0, 0.0
    return up_seconds / observed, up_seconds, down_seconds


def uptime_report(
    store: Store, source: str, since: datetime, until: datetime, interval_s: float
) -> dict[str, Any]:
    """An uptime figure honest enough to put in front of an ISP.

    Two numbers, because they answer different questions and conflating them is how
    uptime reports become worthless. `uptime` is the fraction of *recorded* polls that
    succeeded. `coverage` is how much of the period was recorded at all. A 99.9% uptime
    over 3% coverage is not a 99.9% month, and this refuses to imply that it is.
    """
    polls = store.values(source, "up", since, until)
    coverage = store.coverage(source, since, until, interval_s)
    outages = [
        # Overlapping, like uptime(): an outage that began before this window still
        # cost time inside it, and a report that misses it undercounts the damage.
        event
        for event in store.events_overlapping(source, since, until)
        if event.kind.value == "outage"
    ]
    # Clipped to the window: an outage may have begun before it or still be running.
    downtime = sum(
        (min(event.ended_at or until, until) - max(event.started_at, since)).total_seconds()
        for event in outages
    )
    fraction, up_seconds, down_seconds = uptime(store, source, since, until, interval_s)
    return {
        "source": source,
        "from": since.isoformat(),
        "to": until.isoformat(),
        "uptime": fraction,
        "observed_seconds": round(up_seconds + down_seconds),
        "coverage": coverage.fraction,
        "polls_recorded": len(polls),
        "outages": len(outages),
        "downtime_seconds": round(downtime),
        "longest_outage_seconds": round(
            max(
                (
                    ((event.ended_at or until) - event.started_at).total_seconds()
                    for event in outages
                ),
                default=0,
            )
        ),
    }
