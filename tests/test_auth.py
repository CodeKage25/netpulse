"""Who may ask, once the dashboard is reachable from more than this machine.

The endpoints behind this guard write a deny list to somebody's router and spend about
thirty megabytes of metered data per speed test. These tests exist because the failure
mode is silent: an unauthenticated deploy works perfectly, looks correct, and is wrong.
"""

from __future__ import annotations

import base64

import pytest

from netpulse.web.auth import DASHBOARD_ENV, INGEST_ENV, Guard, Misconfigured

GOOD = "a-long-enough-token-here"
OTHER = "a-different-long-token!"


def basic(password: str, user: str = "netpulse") -> str:
    return "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()


# ------------------------------------------------------------------ refusing to start


def test_a_public_bind_without_a_password_refuses_to_start(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """The dangerous configuration must be impossible, not discouraged. A warning is
    read after the thing is already running, usually by the person who did not need it."""
    monkeypatch.delenv(DASHBOARD_ENV, raising=False)
    with pytest.raises(Misconfigured, match="refusing to serve"):
        Guard.from_env("0.0.0.0")


def test_loopback_needs_no_password(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Anyone who can reach 127.0.0.1 is already sitting at the machine. A password
    there is ceremony, and ceremony is what gets disabled."""
    monkeypatch.delenv(DASHBOARD_ENV, raising=False)
    guard = Guard.from_env("127.0.0.1")
    assert guard.local_only is True
    assert guard.allows_person("") is True


def test_a_short_password_is_refused_rather_than_accepted(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """A four-character password on a public URL is guessable at leisure by anything
    that can reach it, and what it guards is a write to a router."""
    monkeypatch.setenv(DASHBOARD_ENV, "hunter2")
    with pytest.raises(Misconfigured, match="at least"):
        Guard.from_env("0.0.0.0")


def test_a_public_bind_with_a_password_starts(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv(DASHBOARD_ENV, GOOD)
    guard = Guard.from_env("0.0.0.0")
    assert guard.wants_password is True
    assert guard.local_only is False


# ------------------------------------------------------------------ people


def test_the_right_password_is_let_in_and_the_wrong_one_is_not() -> None:
    guard = Guard(dashboard_token=GOOD, local_only=False)
    assert guard.allows_person(basic(GOOD)) is True
    assert guard.allows_person(basic(OTHER)) is False
    assert guard.allows_person("") is False


def test_the_username_is_ignored_because_there_are_no_accounts() -> None:
    """One credential, not a user table. Checking a username would imply accounts that
    do not exist and that nobody can create."""
    guard = Guard(dashboard_token=GOOD, local_only=False)
    assert guard.allows_person(basic(GOOD, user="anybody")) is True


def test_a_malformed_header_is_rejected_rather_than_crashing() -> None:
    """These arrive from the open internet. Every one of them is somebody's first
    attempt at something, and a 500 here is an information leak at best."""
    guard = Guard(dashboard_token=GOOD, local_only=False)
    for header in ("Basic", "Basic !!!!not-base64!!!!", "Bearer " + GOOD, "Basic " + "x" * 5):
        assert guard.allows_person(header) is False


# ------------------------------------------------------------------ agents


def test_an_agent_token_does_not_open_the_dashboard() -> None:
    """An agent may run on a box you trust less than your laptop, with its token sitting
    in a config file on it. Reading that file should not also hand over the device list
    and the block button."""
    guard = Guard(dashboard_token=GOOD, ingest_token=OTHER, local_only=False)
    assert guard.allows_agent("Bearer " + OTHER) is True
    assert guard.allows_person(basic(OTHER)) is False


def test_the_dashboard_password_is_not_an_ingest_token() -> None:
    """And the reverse, so the two credentials cannot be quietly collapsed into one by
    a later change that only tests the happy path."""
    guard = Guard(dashboard_token=GOOD, ingest_token=OTHER, local_only=False)
    assert guard.allows_agent("Bearer " + GOOD) is False


def test_ingest_is_shut_when_no_token_is_configured() -> None:
    """An instance that never meant to accept agents must not accept them because the
    comparison happened to be empty against empty."""
    guard = Guard(dashboard_token=GOOD, local_only=False)
    assert guard.allows_agent("Bearer ") is False
    assert guard.allows_agent("Bearer anything") is False


def test_an_empty_expected_token_never_matches(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """The one comparison that must not be allowed to succeed by accident."""
    monkeypatch.delenv(INGEST_ENV, raising=False)
    guard = Guard.from_env("127.0.0.1")
    assert guard.allows_agent("Bearer ") is False


def test_the_database_path_follows_the_environment_even_with_no_config(
    monkeypatch, tmp_path
) -> None:  # type: ignore[no-untyped-def]
    """A container has no config file — that *is* its path through `load` — and is told
    where its volume is mounted by environment variable. Reading the variable only when
    a file exists would send every hosted deployment's history to a directory thrown
    away on the next restart, and it would look like it was working."""
    from netpulse.config import load

    monkeypatch.setenv("NETPULSE_DB", str(tmp_path / "history.db"))
    assert load(tmp_path / "absent.toml").db_path == tmp_path / "history.db"

    written = tmp_path / "netpulse.toml"
    written.write_text('db = "/ignored/history.db"\n')
    assert load(written).db_path == tmp_path / "history.db"
