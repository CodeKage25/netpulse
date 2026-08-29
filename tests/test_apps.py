"""Per-application usage on this machine, and the honesty about whose machine."""

from __future__ import annotations

from netpulse.analysis.apps import AppMonitor, parse_nettop, parse_ss

# Captured from `nettop -P -x -L 1 -t external -J bytes_in,bytes_out` on macOS.
NETTOP = """,bytes_in,bytes_out,
claude.80672,161651,14138751,
mDNSResponder.246,45691419,10557772,
Google Chrome H.30084,280889,49559,
python3.13.21813,35496,39078,
"""

SS = """tcp   ESTAB  0  0  192.168.0.128:52344  1.1.1.1:443  users:(("firefox",pid=900,fd=42))
\t cubic wscale:8,7 rtt:12.5 bytes_sent:4096 bytes_received:65536
tcp   ESTAB  0  0  192.168.0.128:52345  9.9.9.9:443  users:(("firefox",pid=900,fd=43))
\t cubic wscale:8,7 rtt:11.0 bytes_sent:1024 bytes_received:2048
"""


def test_a_process_name_containing_dots_and_spaces_parses() -> None:
    """ "Google Chrome H.30084" — the pid is split off the right, or the name is cut."""
    found = parse_nettop(NETTOP)
    assert ("Google Chrome H", 30084) in found
    assert ("python3.13", 21813) in found
    assert found[("claude", 80672)] == ("claude", 161651.0, 14138751.0)


def test_the_header_line_is_not_a_process() -> None:
    assert len(parse_nettop(NETTOP)) == 4


def test_linux_sockets_are_summed_per_process() -> None:
    """One app holds many sockets, and each is a fragment of its traffic."""
    found = parse_ss(SS)
    assert found[("firefox", 900)] == ("firefox", 67584.0, 5120.0)


def replay(*outputs: str):  # type: ignore[no-untyped-def]
    remaining = list(outputs)
    return lambda command: remaining.pop(0) if remaining else ""


def test_the_first_sighting_of_a_process_reports_no_usage() -> None:
    """Its counter is cumulative since it started. Reporting that as this interval's
    traffic would credit a week-old browser with all of it the moment NetPulse starts.
    """
    monitor = AppMonitor(run=replay(NETTOP), system="Darwin")
    first = monitor.poll()
    assert first, "processes should still be listed"
    assert all(app.down_bytes is None for app in first)


def test_the_second_sample_reports_what_moved() -> None:
    later = NETTOP.replace("claude.80672,161651,14138751", "claude.80672,171651,14138751")
    monitor = AppMonitor(run=replay(NETTOP, later), system="Darwin")
    monitor.poll()
    claude = next(app for app in monitor.poll() if app.name == "claude")
    assert claude.down_bytes == 10_000.0
    assert claude.up_bytes == 0.0


def test_a_restarted_process_does_not_inherit_the_old_counter() -> None:
    """A new pid is a new counter. Keying on the name alone would bill the fresh
    process for everything its predecessor ever did."""
    restarted = NETTOP.replace("claude.80672,161651,14138751", "claude.99999,50,60")
    monitor = AppMonitor(run=replay(NETTOP, restarted), system="Darwin")
    monitor.poll()
    claude = next(app for app in monitor.poll() if app.name == "claude")
    assert claude.down_bytes is None  # first sighting of this pid


def test_a_counter_that_goes_backwards_is_clamped_not_negated() -> None:
    lower = NETTOP.replace("claude.80672,161651,14138751", "claude.80672,10,20")
    monitor = AppMonitor(run=replay(NETTOP, lower), system="Darwin")
    monitor.poll()
    claude = next(app for app in monitor.poll() if app.name == "claude")
    assert claude.down_bytes == 0.0


def test_system_processes_are_marked_rather_than_hidden() -> None:
    """mDNSResponder can genuinely be the top talker on a noisy network, and that is
    worth seeing rather than filtering away."""
    monitor = AppMonitor(run=replay(NETTOP, NETTOP), system="Darwin")
    monitor.poll()
    by_name = {app.name: app for app in monitor.poll()}
    assert by_name["mDNSResponder"].system is True
    assert by_name["claude"].system is False


def test_an_unsupported_platform_reports_nothing_rather_than_failing() -> None:
    monitor = AppMonitor(run=replay(""), system="Windows")
    assert monitor.available is False
    assert monitor.poll() == []


def test_a_missing_tool_is_silence_not_a_crash() -> None:
    def missing(command: list[str]) -> str:
        raise OSError("nettop: command not found")

    assert AppMonitor(run=missing, system="Darwin").poll() == []
