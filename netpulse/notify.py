"""OS notifications for the moments worth interrupting someone: down, and back.

Throttled per key with the direction in the key — a recovery arriving a second after the
onset is news, and sharing a key would let the onset's throttle swallow it. The backend
is a subprocess (osascript on macOS, notify-send on Linux) and injectable for tests; a
missing backend degrades to silence, never to a crash.
"""

from __future__ import annotations

import platform
import subprocess
from collections.abc import Callable
from datetime import datetime

from netpulse.storage import utcnow

#: Long enough that a flapping link cannot become a storm, short enough that a genuine
#: recurrence still reaches someone.
THROTTLE_S = 60.0


def _system_notify(title: str, body: str) -> None:
    system = platform.system()
    if system == "Darwin":
        script = f'display notification "{body}" with title "{title}"'.replace("\\", "")
        subprocess.run(["osascript", "-e", script], capture_output=True, timeout=5)
    elif system == "Linux":
        subprocess.run(["notify-send", title, body], capture_output=True, timeout=5)


class Notifier:
    def __init__(
        self,
        deliver: Callable[[str, str], None] = _system_notify,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._deliver = deliver
        self._clock = clock
        self._sent: dict[str, datetime] = {}

    def send(self, key: str, title: str, body: str) -> bool:
        """Deliver unless this key fired within the throttle window. Records the send
        when it returns True, so callers cannot forget to."""
        now = self._clock()
        last = self._sent.get(key)
        if last is not None and (now - last).total_seconds() < THROTTLE_S:
            return False
        self._sent[key] = now
        try:
            self._deliver(title, body)
        except Exception:
            return False
        return True
