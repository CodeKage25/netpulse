from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from netpulse.storage import Store


class Clock:
    """Injectable time. Tests never sleep."""

    def __init__(self) -> None:
        self.now = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta: float) -> None:
        self.now += timedelta(**delta)


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def store(clock: Clock) -> Store:
    return Store(":memory:", clock=clock)
