"""Where readings come from: one adapter per router family, plus how to find them.

The whole package rests on one contract — `read() -> Reading`, or raise. Storage,
outage detection, charts, diagnosis and the dashboard are written against that and
nothing else, which is what makes NetPulse network-agnostic where Dishylink is
Starlink-only: a new router is a new file here and no change anywhere above.

Nothing in this package may import from `analysis`, `alerting`, `web` or `monitor`.
An adapter that could reach the store or the collector would stop being replaceable,
and `tests/test_architecture.py` fails if one ever does.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from netpulse.core.model import Reading


class AdapterError(Exception):
    """A failed poll. The collector records it as down; it never crashes the loop."""


@runtime_checkable
class Adapter(Protocol):
    name: str
    kind: str

    def read(self) -> Reading: ...


@runtime_checkable
class Blocker(Protocol):
    """Optionally implemented by an adapter whose router can deny a device.

    Deliberately separate from `Adapter`. Reading is what NetPulse does; writing to
    somebody's router is a different posture, and keeping it a distinct capability
    means the collector cannot reach it by accident and the dashboard has to ask
    whether it exists before offering it.
    """

    def blocked(self) -> list[str]: ...

    def block(self, mac: str, label: str = "") -> None: ...

    def unblock(self, mac: str) -> None: ...


def build(kind: str, name: str, options: dict[str, Any]) -> Adapter:
    if kind == "probe":
        from netpulse.sources.probe import ProbeAdapter

        return ProbeAdapter(name, **options)
    if kind == "huawei":
        from netpulse.sources.huawei import HuaweiAdapter

        return HuaweiAdapter(name, **options)
    if kind == "zte":
        from netpulse.sources.zte import ZteAdapter

        return ZteAdapter(name, **options)
    if kind == "zlt":
        from netpulse.sources.zlt import ZltAdapter

        return ZltAdapter(name, **options)
    if kind == "starlink":
        from netpulse.sources.starlink import StarlinkAdapter

        return StarlinkAdapter(name, **options)
    if kind == "netgear":
        from netpulse.sources.netgear import NetgearAdapter

        return NetgearAdapter(name, **options)
    if kind == "snmp":
        from netpulse.sources.snmp_router import SnmpAdapter

        return SnmpAdapter(name, **options)
    if kind == "demo":
        from netpulse.sources.fake import DemoAdapter

        return DemoAdapter(name, **options)
    raise ValueError(
        f"unknown adapter kind {kind!r}; available: "
        "probe, huawei, zte, zlt, starlink, netgear, snmp, demo"
    )
