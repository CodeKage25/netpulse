from __future__ import annotations

import argparse
import sys
import threading
from datetime import timedelta
from pathlib import Path

from netpulse import __version__
from netpulse.adapters import Adapter, build
from netpulse.config import Config, SourceConfig, load
from netpulse.insights import diagnose
from netpulse.monitor import Collector
from netpulse.notify import Notifier
from netpulse.server import Api, serve
from netpulse.storage import Store, utcnow


def _config(args: argparse.Namespace) -> Config:
    config = load(Path(args.config) if args.config else None)
    if getattr(args, "demo", False):
        config = Config(
            sources=(SourceConfig(name="demo", kind="demo"),),
            interval_s=1.0,
            db_path=Path(":memory:"),
            port=config.port,
        )
    return config


def _adapters(config: Config) -> list[Adapter]:
    return [build(source.kind, source.name, source.options) for source in config.sources]


def run(args: argparse.Namespace) -> int:
    config = _config(args)
    store = Store(config.db_path if str(config.db_path) != ":memory:" else ":memory:")
    notifier = Notifier() if config.notifications else None
    collector = Collector(store, _adapters(config), interval_s=config.interval_s, notifier=notifier)
    api = Api(store, collector, config.interval_s)

    def persist(source: SourceConfig) -> None:
        from netpulse.config import save_sources

        existing = [SourceConfig(s.name, s.kind, dict(s.options)) for s in config.sources]
        save_sources([source, *existing])

    api.persist_sources = persist

    stop = threading.Event()
    thread = threading.Thread(target=collector.run, args=(stop,), daemon=True)
    thread.start()

    configless = not getattr(args, "demo", False) and all(s.kind == "probe" for s in config.sources)
    if configless:
        # First run: go find the router while the probe is already recording.
        def autodiscover() -> None:
            from netpulse.adapters import build as build_adapter
            from netpulse.discover import discover

            for item in discover():
                print(f"found {item.label} at {item.url} — now watching it", flush=True)
                collector.add_adapter(build_adapter(item.kind, item.kind, {"url": item.url}))
                persist(SourceConfig(name=item.kind, kind=item.kind, options={"url": item.url}))

        threading.Thread(target=autodiscover, daemon=True).start()

    port = args.port or config.port
    httpd = serve(api, port)
    names = ", ".join(collector.sources)
    print(f"netpulse {__version__} — watching {names}")
    print(f"dashboard on http://127.0.0.1:{port}   (Ctrl+C to stop)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        httpd.shutdown()
        store.close()
    return 0


def status(args: argparse.Namespace) -> int:
    config = _config(args)
    store = Store(config.db_path)
    now = utcnow()
    sources = store.sources()
    if not sources:
        print("no recordings yet — run `netpulse run` first")
        return 1
    for name in sources:
        latest = store.latest(name)
        texts = store.latest_texts(name)
        up = latest.get("up")
        state = "up" if up and up[1] >= 1 else "DOWN"
        age = f"{(now - up[0]).total_seconds():.0f}s ago" if up else "never"
        line = f"{name:12} {state:5} (seen {age})"
        if "latency.internet_ms" in latest:
            line += f"  latency {latest['latency.internet_ms'][1]:.0f}ms"
        if "signal.rsrp_dbm" in latest:
            line += f"  rsrp {latest['signal.rsrp_dbm'][1]:.0f}dBm"
        if "signal.sinr_db" in latest:
            line += f"  sinr {latest['signal.sinr_db'][1]:.0f}dB"
        if texts.get("net.type"):
            line += f"  [{texts.get('net.operator', '?')} {texts['net.type']}]"
        print(line)
        coverage = store.coverage(name, now - timedelta(hours=24), now, config.interval_s)
        print(f"{'':12} recorded {coverage.fraction * 100:.0f}% of the last 24h")
    store.close()
    return 0


def events(args: argparse.Namespace) -> int:
    config = _config(args)
    store = Store(config.db_path)
    rows = store.events(since=utcnow() - timedelta(hours=args.hours))
    if not rows:
        print(f"no events in the last {args.hours}h")
    for event in rows:
        end = event.ended_at.strftime("%H:%M") if event.ended_at else "ongoing"
        started = event.started_at.astimezone().strftime("%b %d %H:%M")
        print(f"{started} → {end}  {event.source}  {event.kind.value}  {event.detail}")
    store.close()
    return 0


def diagnose_cmd(args: argparse.Namespace) -> int:
    config = _config(args)
    store = Store(config.db_path)
    now = utcnow()
    exit_code = 0
    for name in store.sources():
        findings = diagnose(store, name, now)
        print(f"— {name} —")
        if not findings:
            print("  nothing to flag; the connection looks healthy")
        for insight in findings:
            marker = {"critical": "!!", "warning": " !", "info": "  "}[insight.severity.value]
            print(f"  {marker} {insight.title}")
            print(f"     {insight.detail}")
            if insight.severity.value == "critical":
                exit_code = 1
    store.close()
    return exit_code


def discover_cmd(args: argparse.Namespace) -> int:
    from netpulse.config import save_sources
    from netpulse.discover import discover

    print("scanning the gateway and well-known router addresses…", flush=True)
    found = discover()
    if not found:
        print("no Huawei or ZTE router answered; the probe still monitors this connection")
        return 1
    sources = [SourceConfig(name="wan", kind="probe")]
    for item in found:
        if item.supported:
            print(f"  found {item.label} at {item.url} — watching it")
            sources.insert(
                0, SourceConfig(name=item.kind, kind=item.kind, options={"url": item.url})
            )
        else:
            print(f"  found {item.label} at {item.url} — no adapter yet")
            print(f"    {item.note}")
            print(f"    run: netpulse probe-router {item.url}")
    if len(sources) == 1:
        print("nothing readable found; the probe still monitors this connection")
    location = save_sources(sources)
    print(f"written to {location} — `netpulse run` reads it at startup")
    return 0 if len(sources) > 1 else 1


def probe_router_cmd(args: argparse.Namespace) -> int:
    from netpulse.probe_router import probe_router

    return probe_router(args.url)


def speedtest_cmd(args: argparse.Namespace) -> int:
    from netpulse.speedtest import COST_NOTE, run_speedtest

    if not args.yes:
        answer = input(f"A speed test {COST_NOTE}. Continue? [y/N] ")
        if answer.strip().lower() not in ("y", "yes"):
            print("cancelled")
            return 1
    config = _config(args)
    store = Store(config.db_path)
    source = store.sources()[0] if store.sources() else "wan"
    print("running…", flush=True)
    result = run_speedtest(store, source)
    print(
        f"download {result.down_mbps:.1f} Mbps   upload {result.up_mbps:.1f} Mbps"
        f"   ({result.seconds:.0f}s)"
    )
    store.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="netpulse",
        description="Local-first monitoring for any home connection.",
    )
    parser.add_argument("--config", help="path to netpulse.toml")
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command", required=True)

    running = commands.add_parser("run", help="record and serve the dashboard")
    running.add_argument("--port", type=int, default=None)
    running.add_argument("--demo", action="store_true", help="synthetic connection, no hardware")
    running.set_defaults(handler=run)

    stat = commands.add_parser("status", help="latest reading per source")
    stat.set_defaults(handler=status)

    ev = commands.add_parser("events", help="outages and degradations")
    ev.add_argument("--hours", type=int, default=48)
    ev.set_defaults(handler=events)

    diag = commands.add_parser("diagnose", help="rule-based diagnosis with evidence")
    diag.set_defaults(handler=diagnose_cmd)

    disco = commands.add_parser("discover", help="find your router and write the config")
    disco.set_defaults(handler=discover_cmd)

    probing = commands.add_parser(
        "probe-router", help="show what a router answers, for adapter debugging"
    )
    probing.add_argument("url", help="e.g. http://192.168.0.1")
    probing.set_defaults(handler=probe_router_cmd)

    speed = commands.add_parser("speedtest", help="on-demand speed test (moves ~30 MB)")
    speed.add_argument("--yes", action="store_true", help="skip the data-cost confirmation")
    speed.set_defaults(handler=speedtest_cmd)

    args = parser.parse_args(argv)
    try:
        return int(args.handler(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
