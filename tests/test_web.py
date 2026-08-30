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


def top_level_functions(path: Path) -> set[str]:
    return set(re.findall(r"^(?:async\s+)?function\s+(\w+)", path.read_text(), re.M))


def test_core_modules_do_not_reach_into_views() -> None:
    """`core` is the vocabulary every view speaks; a core module calling a view would
    make the two inseparable, which is the state this split exists to leave.

    Checked by name against what the views actually define, rather than by guessing at
    prefixes: `core/scene.js` legitimately has a `draw`, because drawing a projection
    is a primitive and not a view.
    """
    defined_by_views: set[str] = set()
    for name in SCRIPTS:
        if name.startswith("views/"):
            defined_by_views |= top_level_functions(ASSETS / name)

    for path in sorted((ASSETS / "core").glob("*.js")):
        text = path.read_text()
        for view_function in defined_by_views:
            assert f"{view_function}(" not in text, (
                f"core/{path.name} calls {view_function}(), which a view defines"
            )


def test_no_two_modules_define_the_same_function() -> None:
    """They share one scope once concatenated, so a duplicate silently wins rather than
    erroring — the worst kind of collision, because everything still runs."""
    seen: dict[str, str] = {}
    for name in SCRIPTS:
        for function in top_level_functions(ASSETS / name):
            assert function not in seen, (
                f"{function}() is defined in both {seen[function]} and {name}"
            )
            seen[function] = name


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


# ------------------------------------------------------------------ the 3D scene


def code_of(path: Path) -> str:
    """The file with its comments stripped.

    Scanning raw text catches a module's own prose explaining what it does *not* do —
    scene.js says "WebGL" only to say it needs none, and api.py said "socket" only to
    say it has none. Twice bitten.
    """
    text = re.sub(r"/\*.*?\*/", "", path.read_text(), flags=re.S)
    return re.sub(r"^\s*//.*$", "", text, flags=re.M)


def test_the_scene_needs_no_library() -> None:
    """A carrier stack is a few dozen boxes. At that size a hand-rolled projection is
    faster to write and read than a shader, and adds no dependency to a project whose
    identity is having none."""
    scene = code_of(ASSETS / "core" / "scene.js")
    for library in ("three.", "webgl", "import ", "require("):
        assert library not in scene.lower(), f"scene.js reaches for {library}"
    assert 'getContext("2d")' in scene


def test_the_scene_guards_geometry_behind_the_camera() -> None:
    """Without it a box that passes the camera plane folds through the origin and draws
    as a spike across the whole canvas."""
    assert "depth > 0.05" in code_of(ASSETS / "core" / "scene.js")


def test_the_spectrum_view_states_what_is_measured_and_what_is_shared() -> None:
    """Frequency and width are per carrier and exact; height is the leg's signal,
    because the router reports one figure for its LTE carriers together. A view that
    implied five separate measurements would be inventing four of them."""
    view = (ASSETS / "views" / "spectrum.js").read_text()
    assert "leg's signal" in view
    assert "one figure for its LTE" in view


def test_a_view_opened_by_link_waits_on_readiness_not_on_success() -> None:
    """A linked view is most wanted exactly when something is broken enough to make a
    panel fail, so it must not sit inside the dashboard refresh's success path — and it
    must not start a second refresh either, which queues it behind the page that has
    already loaded."""
    app = code_of(ASSETS / "app.js")
    assert "const ready = refresh()" in app
    for name in ("spectrum", "network", "usage", "rules"):
        view = code_of(ASSETS / "views" / f"{name}.js")
        assert "await ready" in view, f"{name} does not wait on the shared readiness"
        assert "await refresh()" not in view, f"{name} starts a second refresh"


def test_the_page_surfaces_its_own_failures() -> None:
    """A monitoring tool that goes blank while claiming everything is fine is the worst
    version of failing silently."""
    app = code_of(ASSETS / "app.js")
    assert 'addEventListener("error"' in app
    assert 'addEventListener("unhandledrejection"' in app


def test_every_metric_that_runs_upward_says_so() -> None:
    """ "Best" is not "smallest". Without a declared direction a signal panel reports
    -96 dBm as its best hour and -90 as its worst, which is exactly backwards."""
    metrics = (ASSETS / "core" / "metrics.js").read_text()
    for name in (
        "signal.rsrp_dbm",
        "signal.sinr_db",
        "signal.rsrp_5g_dbm",
        "signal.sinr_5g_db",
        "traffic.down_bytes_s",
        "traffic.up_bytes_s",
    ):
        block = metrics.split(f'"{name}": {{')[1].split("},")[0]
        assert "higherIsBetter: true" in block, f"{name} does not declare its direction"


def test_the_direction_flag_describes_the_stored_metric() -> None:
    """loss.pct is stored as loss and shown as success. Reading the flag off the
    display would invert best and worst a second time."""
    metrics = (ASSETS / "core" / "metrics.js").read_text()
    block = metrics.split('"loss.pct": {')[1].split("},")[0]
    assert "higherIsBetter: false" in block  # less loss is better
    assert "invertAxis: true" in block  # …but the panel is drawn the other way up
    assert "toChart: v => 100 - v" in block


def test_a_percentage_chart_declares_its_ceiling() -> None:
    """Otherwise a chart of near-perfect values zooms into the last fraction of a
    percent and turns ordinary noise into a mountain range — or runs the axis to 120."""
    metrics = (ASSETS / "core" / "metrics.js").read_text()
    assert "ceiling: 100" in metrics.split('"loss.pct": {')[1].split("},")[0]


def test_every_linked_view_waits_for_a_resolved_source() -> None:
    """A deep link that opens before sources resolve queries the empty string, which
    returns nothing and reads as "no data" rather than "asked the wrong question"."""
    for name in ("spectrum", "network", "usage", "detail", "rules"):
        view = code_of(ASSETS / "views" / f"{name}.js")
        assert "await ready" in view, f"{name}.js does not wait for a source"


def test_throughput_names_its_extremes_peak_and_quietest() -> None:
    """Throughput measures demand, not quality. Calling an idle minute the "worst"
    download contradicts the panel's own explainer, which says an idle link reads near
    zero however fast it is."""
    metrics = (ASSETS / "core" / "metrics.js").read_text()
    for name in ("traffic.down_bytes_s", "traffic.up_bytes_s"):
        block = metrics.split(f'"{name}": {{')[1].split("},")[0]
        assert 'extremes: ["Peak", "Quietest"]' in block


def test_quality_metrics_keep_best_and_worst() -> None:
    """Latency and signal really do have a best and a worst; only demand does not."""
    metrics = (ASSETS / "core" / "metrics.js").read_text()
    for name in ("latency.internet_ms", "signal.rsrp_dbm"):
        block = metrics.split(f'"{name}": {{')[1].split("},")[0]
        assert "extremes:" not in block


def test_a_tile_sparkline_shows_the_same_quantity_as_its_number() -> None:
    """Ping success displays the inverse of what is stored. Without transforming the
    sparkline too, the figure reads 100% while the line spikes upward on every packet
    lost — the two disagreeing on the same tile."""
    dashboard = code_of(ASSETS / "views" / "dashboard.js")
    assert "shownSeries(spec, sp[key])" in dashboard
    assert "spec.toChart" in dashboard


def test_a_rule_that_only_reports_is_never_described_as_blocking() -> None:
    """A rule without `block` watches an allowance and says when it is spent. Labelling
    that "blocked" claims something about the network that is not true — the device is
    still online, and someone reading the panel would go looking for a fault."""
    view = code_of(ASSETS / "views" / "rules.js")
    assert "rule.blocks" in view
    assert "over — not blocking" in (ASSETS / "views" / "rules.js").read_text()


def test_the_rules_view_asks_rather_than_remembers() -> None:
    """A verdict is a statement about this moment. Showing a cached one after the
    allowance rolled over would hold a device the rules no longer hold."""
    view = code_of(ASSETS / "views" / "rules.js")
    assert "/api/rules?source=" in view


def test_an_unmeasured_allowance_is_never_drawn_as_an_empty_bar() -> None:
    """A bar at 0% under a device the router never reported is a measurement claim, and
    nothing made that measurement."""
    view = code_of(ASSETS / "views" / "rules.js")
    assert "rule.used_bytes == null" in view
    assert "nothing measured for" in (ASSETS / "views" / "rules.js").read_text()


def test_the_static_flag_is_declared_before_anything_reads_it() -> None:
    """A `const` used above its own line throws on load, and one thrown line at the top
    level takes the rest of the file with it. The flag that exists to make the page
    screenshotable put an error banner in the screenshot instead."""
    code = code_of(ASSETS / "app.js")
    for name in re.findall(r"^const ([A-Z][A-Z_0-9]*) =", code, re.M):
        assert code.index(name) == code.index(f"const {name} =") + len("const "), (
            f"{name} is read before it is declared"
        )


def test_a_service_label_survives_the_round_trip_through_the_store() -> None:
    """Usage is stored under the service's name, not the endpoint — a day of YouTube is
    one row, not four hundred addresses. Reading that label back through the endpoint
    parser treats "Anthropic" as a hostname, matches no domain, and reports a known
    service as unrecognised. It did exactly that on screen."""
    from netpulse.core.services import identify_name

    assert identify_name("Anthropic").identifies_a_site is True
    assert identify_name("YouTube").identifies_a_site is True
    # …while the caveat still travels with the ones that carry it.
    assert identify_name("Cloudflare").kind == "network"
    assert identify_name("Microsoft Azure").kind == "cloud"
    assert identify_name("165.66.149.34").known is False
