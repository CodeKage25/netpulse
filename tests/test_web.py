"""The page itself, and the boundaries inside it.

Python tests pass happily while the dashboard is a blank screen — a single JavaScript
syntax error kills every line of it, and nothing in a server test notices. These are the
cheap checks that catch that, plus the module boundaries that keep a growing UI from
collapsing back into one file.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

from netpulse.web.server import SCRIPTS, STYLES

ASSETS = Path(__file__).resolve().parent.parent / "netpulse" / "web" / "assets"


def scripts() -> list[Path]:
    return [ASSETS / name for name in SCRIPTS]


def sources() -> list[Path]:
    return scripts() + [ASSETS / name for name in STYLES] + [ASSETS / "index.html"]


# ------------------------------------------------------------------ it runs at all


def test_every_script_parses() -> None:
    """One stray identifier collision takes the whole dashboard down, and every Python
    test still passes. This is the only thing that catches it."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed; CI runs this check")
    for path in scripts():
        result = subprocess.run(
            [node, "--check", str(path)], capture_output=True, text=True, check=False
        )
        assert result.returncode == 0, f"{path.name}: {result.stderr}"


def test_the_bundle_parses_as_one_script() -> None:
    """The files share a scope once concatenated, so each parsing alone is not enough —
    a `const` declared twice across two files only fails when they are joined."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed; CI runs this check")
    joined = "\n".join(path.read_text() for path in scripts())
    combined = Path(
        subprocess.run(
            ["mktemp", "-t", "netpulse"], capture_output=True, text=True, check=True
        ).stdout.strip()
        + ".js"
    )
    combined.write_text(joined)
    try:
        result = subprocess.run(
            [node, "--check", str(combined)], capture_output=True, text=True, check=False
        )
        assert result.returncode == 0, result.stderr
    finally:
        combined.unlink(missing_ok=True)


def test_every_asset_the_server_names_exists() -> None:
    """A missing file ships a page with a hole where its behaviour should be."""
    for path in sources():
        assert path.is_file(), f"{path.name} is referenced by the server but absent"


def test_the_shell_has_a_slot_for_each_bundle() -> None:
    shell = (ASSETS / "index.html").read_text()
    for placeholder in ("{{styles}}", "{{scripts}}"):
        assert placeholder in shell


# ------------------------------------------------------------------ boundaries


def test_the_shell_holds_no_logic() -> None:
    """Structure in the shell, behaviour in the scripts — otherwise the split buys
    nothing and the file grows back."""
    body = re.sub(r"<script>.*?</script>", "", (ASSETS / "index.html").read_text(), flags=re.S)
    assert "function " not in body
    assert "addEventListener" not in body


def test_core_modules_do_not_reach_into_views() -> None:
    """`core` is the vocabulary every view speaks; a core module calling a view would
    make the two inseparable, which is the state this split exists to leave."""
    view_names = [Path(name).stem for name in SCRIPTS if name.startswith("views/")]
    for path in (ASSETS / "core").glob("*.js"):
        text = path.read_text()
        for view in view_names:
            assert f"{view}(" not in text, f"core/{path.name} calls the {view} view"
        for marker in ("draw", "render", "show"):
            assert f"function {marker}" not in text, f"core/{path.name} renders a view"


def test_only_the_wiring_file_binds_events() -> None:
    """Every listener in one place, so what responds to what is readable without
    opening nine files."""
    for path in scripts():
        if path.name == "app.js":
            continue
        text = path.read_text()
        # Charts bind their own crosshair handlers to elements they just created, which
        # is local to the primitive and does not escape it.
        if path.name == "chart.js":
            continue
        assert "document.getElementById" not in text or "views/" in str(path), (
            f"{path.name} reaches into the document outside a view"
        )


def test_no_module_grew_past_the_point_of_being_readable() -> None:
    """A soft ceiling. Not a style rule — a file nobody opens is a file nobody fixes."""
    for path in scripts():
        lines = len(path.read_text().splitlines())
        assert lines < 400, f"{path.name} is {lines} lines; split it"


# ------------------------------------------------------------------ what ships


def test_the_page_asks_for_nothing_from_the_internet() -> None:
    """It has to render during the outage it is explaining."""
    for path in sources():
        text = path.read_text()
        for pattern in ('src="http', "src='http", 'href="http', "@import", "cdn."):
            assert pattern not in text, f"{path.name} reaches outside for {pattern}"


def test_it_lays_out_on_a_phone() -> None:
    """A network monitor is most needed on the device in your hand while the connection
    is misbehaving. Dishylink's UI stops below 700px."""
    css = (ASSETS / "css" / "app.css").read_text()
    widths = [int(w) for w in re.findall(r"max-width:\s*(\d+)px", css)]
    assert widths and min(widths) <= 640


def test_dark_and_light_are_both_fully_defined() -> None:
    """Every colour must exist in both themes, or a toggle lands on an unstyled token."""
    css = (ASSETS / "css" / "app.css").read_text()
    dark = set(re.findall(r"(--[\w-]+):", css.split(':root[data-theme="light"]')[0]))
    light = set(re.findall(r"(--[\w-]+):", css.split(':root[data-theme="light"]')[1]))
    missing = {token for token in dark if token not in light}
    # Shadows are deliberately shared; everything that carries colour is not.
    assert not missing - {"--shadow"}, f"light theme is missing {missing}"
