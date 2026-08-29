"""Time, as a value the caller supplies.

Every module that needs "now" takes a `Clock` rather than calling the system clock,
which is why the whole test suite runs without sleeping and why billing cycles and
outage durations can be exercised at real dates. It lives on its own so that reaching
for the time does not mean importing the database.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

#: Anything that answers "what time is it" in UTC.
Clock = Callable[[], datetime]


def utcnow() -> datetime:
    """The only place the system clock is read."""
    return datetime.now(UTC)
