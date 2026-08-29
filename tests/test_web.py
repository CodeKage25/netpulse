"""The page itself.

Python tests pass happily while the dashboard is a blank screen — a single JavaScript
syntax error kills every line of it, and nothing in a server test notices. These are the
cheap checks that catch that, plus the invariants the page has to keep to be useful
during the outage it is describing.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parent.parent / "netpulse" / "web"


def test_the_script_parses() -> None:
    """One stray identifier collision takes the whole dashboard down, and every Python
    test still passes. This is the only thing that catches it."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed; CI runs this check")
    result = subprocess.run(
        [node, "--check", str(WEB / "app.js")], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr


def test_every_placeholder_in_the_shell_has_a_file() -> None:
    """A missing asset ships a page with a hole where its stylesheet should be."""
    shell = (WEB / "dashboard.html").read_text()
    for name in re.findall(r"\{\{([\w.]+)\}\}", shell):
        assert (WEB / name).is_file(), f"{name} is referenced but not present"


def test_the_shell_holds_no_logic() -> None:
    """Structure in the shell, behaviour in app.js — otherwise the split buys nothing."""
    shell = (WEB / "dashboard.html").read_text()
    body = re.sub(r"<script>.*?</script>", "", shell, flags=re.S)
    assert "function " not in body
    assert "addEventListener" not in body


def test_the_page_asks_for_nothing_from_the_internet() -> None:
    """It has to render during the outage it is explaining."""
    for name in ("dashboard.html", "app.css", "app.js"):
        text = (WEB / name).read_text()
        for pattern in ('src="http', "src='http", 'href="http', "@import", "cdn."):
            assert pattern not in text, f"{name} reaches outside for {pattern}"


def test_it_lays_out_on_a_phone() -> None:
    """A network monitor is most needed on the device in your hand while the connection
    is misbehaving. Dishylink's UI stops below 700px."""
    css = (WEB / "app.css").read_text()
    widths = [int(w) for w in re.findall(r"max-width:\s*(\d+)px", css)]
    assert widths and min(widths) <= 640


def test_dark_and_light_are_both_fully_defined() -> None:
    """Every colour must exist in both themes, or a toggle lands on an unstyled token."""
    css = (WEB / "app.css").read_text()
    dark = set(re.findall(r"(--[\w-]+):", css.split(':root[data-theme="light"]')[0]))
    light = set(re.findall(r"(--[\w-]+):", css.split(':root[data-theme="light"]')[1]))
    missing = {token for token in dark if token not in light}
    # Shadows are deliberately shared; everything that carries colour is not.
    assert not missing - {"--shadow"}, f"light theme is missing {missing}"
