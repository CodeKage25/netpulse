"""Turning cumulative byte counters into throughput, without inventing bursts.

Most routers report totals, not rates. Differencing them is arithmetic; the whole
difficulty is that counters do not only go up. A reboot zeroes them, a 32-bit counter
wraps, a firmware update re-numbers them. Each of those looks, to a naive subtraction,
like a several-gigabyte second — and a monitor that draws that has published a number
nobody measured.

So the rule here is the same one the rest of the project follows: when the counters
stop being comparable, re-baseline and emit nothing for that cycle. A missing point is
a gap in the chart, which is true. A fabricated spike is not.
"""

from __future__ import annotations

#: A 32-bit counter's range. Wide counters wrap too, at 2**64, but not within the
#: lifetime of any router this will ever poll.
COUNTER_WRAP = 2**32


class Counters:
    """Two cumulative counters and the clock they are read against.

    The clock may be the router's own uptime — better, because it rewinds visibly on a
    reboot — or wall-clock seconds when the firmware does not publish uptime.
    """

    def __init__(self, wrap: int = COUNTER_WRAP) -> None:
        self._wrap = wrap
        self._previous: tuple[float, float, float] | None = None

    def rates(self, rx: float | None, tx: float | None, clock: float | None) -> dict[str, float]:
        """Bytes per second since the previous reading, or an empty dict.

        Empty on the first call (there is nothing to difference), on a rewound clock,
        and on any counter movement that cannot be explained as ordinary traffic or a
        single wrap.
        """
        if rx is None or tx is None or clock is None:
            return {}
        previous, self._previous = self._previous, (rx, tx, clock)
        if previous is None:
            return {}
        last_rx, last_tx, last_clock = previous
        elapsed = clock - last_clock
        if elapsed <= 0:  # rebooted, or polled twice within the same second
            return {}
        deltas = []
        for current, last in ((rx, last_rx), (tx, last_tx)):
            delta = current - last
            if delta < 0:
                delta += self._wrap
                # A wrap lands just above the old value; a reset lands anywhere. Half
                # the counter's range in one interval is the latter, and there is no
                # honest number to publish for it.
                if delta < 0 or delta > self._wrap / 2:
                    return {}
            deltas.append(delta)
        return {
            "traffic.down_bytes_s": deltas[0] / elapsed,
            "traffic.up_bytes_s": deltas[1] / elapsed,
        }
