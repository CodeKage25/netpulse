"""Where an alert goes: the desktop, or somewhere you will actually see it.

An OS toast is useless for the case that matters most — the connection went down while
you were out, and the machine watching it is a Raspberry Pi in a cupboard. So alerts
can also be posted to a webhook, Slack, Discord, ntfy or Home Assistant.

Three rules govern everything here, because this is the one part of NetPulse that
deliberately sends data off the machine.

**Only what the user configured, only where they configured it.** A channel exists
because someone wrote a URL in their own config file. Nothing is discovered, nothing is
defaulted, and there is no fallback destination.

**Only the alert.** The payload carries the source name, the metric, the value and the
message. Router credentials, addresses, device names and history never travel — a
monitoring alert should not be a data leak.

**A failed delivery is never an outage.** Posting happens off the poll loop with a hard
timeout, and a channel that is down is logged and dropped. A webhook that hangs must not
stall the recorder, because a stalled recorder invents the gap it then writes down.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

TIMEOUT_S = 6.0

Post = Callable[[str, bytes, dict[str, str]], None]


def _urllib_post(url: str, body: bytes, headers: dict[str, str]) -> None:
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(request, timeout=TIMEOUT_S):
        return


@dataclass(frozen=True)
class Channel:
    kind: str
    url: str
    #: ntfy priority / Home Assistant entity, depending on kind. Optional everywhere.
    option: str = ""


def _payload(
    channel: Channel, title: str, body: str, severity: str
) -> tuple[bytes, dict[str, str]]:
    """One alert, in whatever shape the destination expects."""
    json_headers = {"Content-Type": "application/json"}
    if channel.kind == "slack":
        marker = {"critical": ":rotating_light:", "warning": ":warning:"}.get(
            severity, ":information_source:"
        )
        return json.dumps({"text": f"{marker} *{title}*\n{body}"}).encode(), json_headers
    if channel.kind == "discord":
        colour = {"critical": 0xD03B3B, "warning": 0xFAB219}.get(severity, 0x0CA30C)
        embed = {"title": title, "description": body, "color": colour}
        return json.dumps({"embeds": [embed]}).encode(), json_headers
    if channel.kind == "ntfy":
        # ntfy takes the message as the raw body and everything else as headers.
        priority = channel.option or {"critical": "urgent", "warning": "high"}.get(
            severity, "default"
        )
        return body.encode(), {
            "Title": title,
            "Priority": priority,
            "Tags": {"critical": "rotating_light", "warning": "warning"}.get(
                severity, "information_source"
            ),
        }
    if channel.kind == "home_assistant":
        return json.dumps(
            {"title": title, "message": body, "severity": severity}
        ).encode(), json_headers
    # A plain webhook gets the structured form, which is the most useful to a script.
    return json.dumps(
        {"title": title, "message": body, "severity": severity}
    ).encode(), json_headers


class Channels:
    """Fan an alert out to every configured destination, failing quietly per channel."""

    def __init__(
        self,
        channels: list[Channel],
        post: Post = _urllib_post,
        on_error: Callable[[str, Exception], None] | None = None,
    ) -> None:
        self.channels = channels
        self._post = post
        self._on_error = on_error

    def send(self, title: str, body: str, severity: str = "warning") -> int:
        """Deliver to every channel; returns how many accepted it.

        One channel failing must never stop the others: a broken Discord webhook is not
        a reason for the Slack alert to go missing too.
        """
        delivered = 0
        for channel in self.channels:
            try:
                payload, headers = _payload(channel, title, body, severity)
                self._post(channel.url, payload, headers)
                delivered += 1
            except (urllib.error.URLError, OSError, ValueError) as exc:
                if self._on_error:
                    self._on_error(channel.kind, exc)
        return delivered


#: The kinds a config file may name. Anything else is a typo, not a new integration.
KINDS = ("webhook", "slack", "discord", "ntfy", "home_assistant")


def parse_channels(raw: object) -> list[Channel]:
    """Read channels from config, dropping any that could not deliver anything.

    An unknown kind is skipped rather than treated as a generic webhook: posting a
    NetPulse-shaped JSON body at a URL that expected something else is a silent
    misdelivery, and a typo should cost you an alert you notice missing.
    """
    if not isinstance(raw, list):
        return []
    channels: list[Channel] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        kind = str(entry.get("kind", "webhook")).lower()
        url = str(entry.get("url", ""))
        if kind not in KINDS or not url.startswith(("http://", "https://")):
            continue
        channels.append(Channel(kind=kind, url=url, option=str(entry.get("option", ""))))
    return channels


def redact(channels: list[Channel]) -> list[dict[str, Any]]:
    """Channels as the dashboard may see them — a webhook URL is a bearer credential.

    Anyone holding a Slack or ntfy URL can post to that channel, so the dashboard is
    told the kind and the host and never the path.
    """
    listed = []
    for channel in channels:
        without_scheme = channel.url.split("://", 1)[-1]
        host = without_scheme.split("/", 1)[0]
        listed.append({"kind": channel.kind, "host": host})
    return listed
