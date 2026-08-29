"""Configuration: a TOML file, or nothing at all.

With no config, NetPulse watches the connection it is running on via the probe — that is
the zero-setup story. A config adds router adapters:

    interval_s = 5

    [[source]]
    name = "mtn"
    kind = "huawei"          # any carrier's Huawei box: MTN, Airtel, Glo, 9mobile
    url = "http://192.168.8.1"
    # username/password unlock SMS reading (where data-balance texts arrive)

    [[source]]
    name = "airtel-5g"
    kind = "zte"
    url = "http://192.168.0.1"

    [[source]]
    name = "wan"
    kind = "probe"
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_DIR = Path.home() / ".netpulse"
DEFAULT_CONFIG = DEFAULT_DIR / "netpulse.toml"
DEFAULT_DB = DEFAULT_DIR / "history.db"


@dataclass(frozen=True)
class SourceConfig:
    name: str
    kind: str
    options: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Config:
    sources: tuple[SourceConfig, ...]
    interval_s: float = 5.0
    db_path: Path = DEFAULT_DB
    port: int = 8787
    notifications: bool = True


def load(path: Path | None = None) -> Config:
    location = path or DEFAULT_CONFIG
    if not location.exists():
        return Config(sources=(SourceConfig(name="wan", kind="probe"),))

    data = tomllib.loads(location.read_text())
    sources = tuple(
        SourceConfig(
            name=str(entry.get("name") or entry.get("kind", "source")),
            kind=str(entry["kind"]),
            options={k: v for k, v in entry.items() if k not in ("name", "kind")},
        )
        for entry in data.get("source", [])
    ) or (SourceConfig(name="wan", kind="probe"),)

    return Config(
        sources=sources,
        interval_s=float(data.get("interval_s", 5.0)),
        db_path=Path(data.get("db", DEFAULT_DB)).expanduser(),
        port=int(data.get("port", 8787)),
        notifications=bool(data.get("notifications", True)),
    )
