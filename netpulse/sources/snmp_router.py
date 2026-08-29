"""Routers that speak SNMP: MikroTik and Teltonika, so far.

There is **no standard MIB for cellular radio metrics**. The IETF never defined one —
the only cellular thing IANA registers is `ifType 243 (wwanPP)`, which lets you tag an
interface as mobile and tells you nothing about it — and 3GPP's own management framework
never used SNMP. So RSRP, RSRQ and SINR are entirely vendor-private, and this adapter is
a table of OIDs per vendor rather than anything generic.

SNMP is also **off by default nearly everywhere** and absent from consumer firmware
altogether. It is an enrichment path for a box someone deliberately configured, never a
baseline: the probe is cheap, the failure is quiet, and discovery does not offer it
unless something actually answered.

OIDs below are read from the vendors' own MIB files, not guessed.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from netpulse.core.model import Reading
from netpulse.sources import AdapterError
from netpulse.sources.snmp import SnmpError, get, identify, walk

#: sysUpTime and sysDescr, which every agent answers.
SYS_DESCR = "1.3.6.1.2.1.1.1.0"
SYS_UPTIME = "1.3.6.1.2.1.1.3.0"
SYS_NAME = "1.3.6.1.2.1.1.5.0"


def _number(value: object) -> float | None:
    """Vendors disagree on whether a signal reading is an integer or a string of one."""
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().split()[0] if str(value).strip() else ""
    try:
        return float(text)
    except ValueError:
        return None


@dataclass(frozen=True)
class Vendor:
    name: str
    #: A substring of sysDescr that identifies this family.
    marker: str
    #: Metric name -> OID *prefix*. Cellular tables are indexed by modem, so the row
    #: is found by walking rather than by guessing an index that differs per firmware.
    metrics: dict[str, str]
    texts: dict[str, str] = field(default_factory=dict)


#: MikroTik RouterOS, mtxrLTEModem at 1.3.6.1.4.1.14988.1.1.16.1.1.
#: Values are plain signed integers in native units — not scaled by ten. There is no
#: band or operator object in this table; RouterOS only exposes those over its own API.
MIKROTIK = Vendor(
    name="MikroTik",
    marker="routeros",
    metrics={
        "signal.rssi_dbm": "1.3.6.1.4.1.14988.1.1.16.1.1.2",
        "signal.rsrq_db": "1.3.6.1.4.1.14988.1.1.16.1.1.3",
        "signal.rsrp_dbm": "1.3.6.1.4.1.14988.1.1.16.1.1.4",
        "signal.sinr_db": "1.3.6.1.4.1.14988.1.1.16.1.1.7",
    },
    texts={"signal.cell_id": "1.3.6.1.4.1.14988.1.1.16.1.1.5"},
)

#: Teltonika RutOS. The modern firmware moved everything into a per-modem table at
#: 1.3.6.1.4.1.48690.2.2.1; RUT2xx-era firmware used flat scalars at 48690.2.x. Both
#: are tried, because a decade of deployed hardware runs the old one.
TELTONIKA = Vendor(
    name="Teltonika",
    marker="teltonika",
    metrics={
        "signal.rsrp_dbm": "1.3.6.1.4.1.48690.2.2.1.20",
        "signal.rsrq_db": "1.3.6.1.4.1.48690.2.2.1.21",
        "signal.sinr_db": "1.3.6.1.4.1.48690.2.2.1.19",
        "signal.bars": "1.3.6.1.4.1.48690.2.2.1.12",
    },
    texts={
        "net.operator": "1.3.6.1.4.1.48690.2.2.1.13",
        "net.type": "1.3.6.1.4.1.48690.2.2.1.16",
        "signal.cell_id": "1.3.6.1.4.1.48690.2.2.1.18",
    },
)

TELTONIKA_LEGACY = Vendor(
    name="Teltonika",
    marker="teltonika",
    metrics={
        "signal.rsrp_dbm": "1.3.6.1.4.1.48690.2.23.0",
        "signal.rsrq_db": "1.3.6.1.4.1.48690.2.24.0",
        "signal.sinr_db": "1.3.6.1.4.1.48690.2.22.0",
    },
    texts={
        "net.operator": "1.3.6.1.4.1.48690.2.5.0",
        "net.type": "1.3.6.1.4.1.48690.2.8.0",
        "signal.cell_id": "1.3.6.1.4.1.48690.2.21.0",
    },
)

VENDORS = (MIKROTIK, TELTONIKA, TELTONIKA_LEGACY)

#: IF-MIB 64-bit interface counters. The 32-bit ifInOctets wraps in under a minute on a
#: gigabit link, so only the HC variants are worth reading.
IF_IN_OCTETS = "1.3.6.1.2.1.31.1.1.1.6"
IF_OUT_OCTETS = "1.3.6.1.2.1.31.1.1.1.10"


class SnmpAdapter:
    kind = "snmp"

    def __init__(
        self,
        name: str,
        url: str = "",
        host: str = "",
        community: str = "public",
        timeout: float = 2.0,
    ):
        # `url` keeps the shape every other adapter uses, so config and discovery do not
        # need a special case for this one.
        self.name = name
        self.base = url or f"http://{host}"
        self.host = host or self.base.split("//")[-1].split("/")[0].split(":")[0]
        self._community = community
        self._timeout = timeout
        self._vendor: Vendor | None = None

    def _detect(self) -> Vendor | None:
        descr = (identify(self.host, self._community, self._timeout) or "").lower()
        if not descr:
            return None
        for vendor in VENDORS:
            if vendor.marker in descr:
                return vendor
        return None

    def read(self) -> Reading:
        try:
            system = get(
                self.host, self._community, [SYS_DESCR, SYS_UPTIME, SYS_NAME], timeout=self._timeout
            )
        except SnmpError as exc:
            raise AdapterError(f"no SNMP answer from {self.host}: {exc}") from exc
        if not system:
            raise AdapterError(f"{self.host} answered SNMP but reported nothing")

        metrics: dict[str, float] = {"up": 1.0}
        texts: dict[str, str] = {}
        uptime = system.get(SYS_UPTIME)
        if isinstance(uptime, int):
            metrics["router.uptime_s"] = uptime / 100.0  # TimeTicks are centiseconds
        if descr := system.get(SYS_DESCR):
            texts["router.firmware"] = str(descr)[:120]
        if named := system.get(SYS_NAME):
            texts["router.name"] = str(named)

        if self._vendor is None:
            self._vendor = self._detect()
        vendor = self._vendor
        if vendor is None:
            # An agent with no radio table is still worth recording: uptime and
            # reachability are real, and claiming a signal we cannot read would not be.
            return Reading(metrics=metrics, texts=texts)

        texts["router.vendor"] = vendor.name
        for metric, prefix in vendor.metrics.items():
            if (value := self._first(prefix)) is not None:
                metrics[metric] = value
        for key, prefix in vendor.texts.items():
            found = self._first_text(prefix)
            if found:
                texts[key] = found
        return Reading(metrics=metrics, texts=texts)

    def _row(self, prefix: str) -> object | None:
        """The first value under an OID prefix.

        Cellular tables are indexed by a modem index that differs per firmware and is
        not always 1, so the row is walked rather than guessed at.
        """
        if prefix.endswith(".0"):  # already a scalar instance
            try:
                return get(self.host, self._community, [prefix], timeout=self._timeout).get(prefix)
            except SnmpError:
                return None
        found = walk(self.host, self._community, prefix, timeout=self._timeout, max_rows=8)
        return next(iter(found.values()), None)

    def _first(self, prefix: str) -> float | None:
        value = self._row(prefix)
        return _number(value) if value is not None else None

    def _first_text(self, prefix: str) -> str:
        value = self._row(prefix)
        return str(value).strip() if value is not None else ""
