"""Who is allowed to ask, once the dashboard is reachable from anywhere.

On a laptop this file does nothing: the server binds loopback, the only client is the
person sitting at the machine, and a password would be ceremony. The moment it binds
anything else that stops being true, and what is on the other side of these endpoints is
not a read-only curiosity. `POST /api/block` writes a deny list to somebody's router.
`POST /api/speedtest` moves about thirty megabytes of metered data every time it is
called, on connections where that is a real cost. An unauthenticated public URL is
someone else's button for kicking a household off its own network.

So the rule here is that the dangerous configuration is impossible rather than
discouraged: binding a non-loopback address without a token **refuses to start**. A
warning would be the wrong shape — warnings are read after the thing is already running,
usually by the person who did not need the warning.

Two credentials, because they are used by different things for different reasons. People
get HTTP Basic, which every browser already knows how to prompt for and which needs no
login page, no session table and no cookie handling. Agents get a bearer token, because
an agent is not a browser and should never be handed a credential that also opens the
dashboard.
"""

from __future__ import annotations

import base64
import binascii
import hmac
import os
from dataclasses import dataclass

#: Environment is the only source. Fly injects secrets this way, and a token that lived
#: in the config file would be one `git add .` from being published.
DASHBOARD_ENV = "NETPULSE_DASHBOARD_TOKEN"
INGEST_ENV = "NETPULSE_INGEST_TOKEN"

#: Addresses where "anyone who can reach this is already on this machine" holds.
LOOPBACK = frozenset({"127.0.0.1", "::1", "localhost"})

#: Short tokens are guessable at leisure by anything that can reach the URL, and this
#: one guards a write to a router. Long enough that guessing is not the attack.
MIN_TOKEN_LENGTH = 16


class Misconfigured(Exception):
    """Raised at startup, never at request time. A refusal to run beats a hole."""


def _matches(provided: str, expected: str) -> bool:
    """Constant time, so a wrong answer takes as long as any other wrong answer.

    Ordinary `==` on strings returns at the first differing byte, which leaks the length
    of the correct prefix to anyone willing to time enough requests.
    """
    if not expected:
        return False
    return hmac.compare_digest(provided.encode(), expected.encode())


@dataclass(frozen=True)
class Guard:
    """The credentials this server will accept, and nothing about how they arrived."""

    dashboard_token: str = ""
    ingest_token: str = ""
    #: True when the server is only reachable from the machine it runs on, in which case
    #: there is nobody to authenticate.
    local_only: bool = True

    @classmethod
    def from_env(cls, bind: str) -> Guard:
        """Read the tokens, and refuse a configuration that would expose the writes.

        The check is on the bind address rather than on a flag, because the bind address
        is the thing that actually decides who can reach this.
        """
        local_only = bind in LOOPBACK
        dashboard = os.environ.get(DASHBOARD_ENV, "").strip()
        ingest = os.environ.get(INGEST_ENV, "").strip()
        if not local_only and not dashboard:
            raise Misconfigured(
                f"refusing to serve {bind} without a password: this exposes device "
                f"blocking and speed tests to anyone who can reach it. "
                f"Set {DASHBOARD_ENV} (16+ characters), or bind 127.0.0.1."
            )
        for name, token in ((DASHBOARD_ENV, dashboard), (INGEST_ENV, ingest)):
            if token and len(token) < MIN_TOKEN_LENGTH:
                raise Misconfigured(
                    f"{name} is {len(token)} characters; use at least {MIN_TOKEN_LENGTH}. "
                    "This guards a write to your router."
                )
        return cls(dashboard_token=dashboard, ingest_token=ingest, local_only=local_only)

    @property
    def wants_password(self) -> bool:
        return bool(self.dashboard_token)

    def allows_person(self, header: str) -> bool:
        """Whether an `Authorization` header carries the dashboard password.

        Basic auth's username is ignored on purpose. There is one credential here, not a
        user table, and pretending otherwise would imply accounts that do not exist.
        """
        if not self.wants_password:
            return True  # loopback, checked at startup
        prefix, _, encoded = header.partition(" ")
        if prefix.lower() != "basic" or not encoded:
            return False
        try:
            decoded = base64.b64decode(encoded, validate=True).decode("utf-8", "replace")
        except (binascii.Error, ValueError):
            return False
        _, _, password = decoded.partition(":")
        return _matches(password, self.dashboard_token)

    def allows_agent(self, header: str) -> bool:
        """Whether an `Authorization` header carries the ingest token.

        Separate from the dashboard password so that an agent — which may run on a box
        you trust less than your laptop, and whose token sits in a config file on it —
        cannot be used to read the dashboard or block a device.
        """
        prefix, _, token = header.partition(" ")
        if prefix.lower() != "bearer":
            return False
        return _matches(token.strip(), self.ingest_token)
