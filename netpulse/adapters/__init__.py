"""One small contract: an adapter reads a source and returns a Reading, or raises.

Everything else — storage, outage detection, charts, insights, the AI — is written against
that contract, which is what makes NetPulse network-agnostic where Dishylink is
Starlink-only. A new router means a new adapter, nothing else.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from netpulse.model import Reading


class AdapterError(Exception):
    """A failed poll. The collector records it as down; it never crashes the loop."""


@runtime_checkable
class Adapter(Protocol):
    name: str
    kind: str

    def read(self) -> Reading: ...


def build(kind: str, name: str, options: dict[str, Any]) -> Adapter:
    if kind == "probe":
        from netpulse.adapters.probe import ProbeAdapter

        return ProbeAdapter(name, **options)
    if kind == "huawei":
        from netpulse.adapters.huawei import HuaweiAdapter

        return HuaweiAdapter(name, **options)
    if kind == "zte":
        from netpulse.adapters.zte import ZteAdapter

        return ZteAdapter(name, **options)
    if kind == "demo":
        from netpulse.adapters.fake import DemoAdapter

        return DemoAdapter(name, **options)
    raise ValueError(f"unknown adapter kind {kind!r}; available: probe, huawei, zte, demo")
