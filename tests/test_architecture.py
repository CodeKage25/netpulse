"""The dependency rule, enforced.

An architecture that lives only in a document is a suggestion. This reads the actual
imports and fails the build when a layer reaches upward — which is the only thing that
keeps "a new router is one file" true a year from now, when the person adding the
router has not read the document.

The layers, lowest first. Nothing may import from a layer above its own.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent / "netpulse"

#: Package -> its depth. Equal depths may not import each other either, unless listed
#: in SIBLINGS: two modules at the same level that know about each other are one module
#: wearing two names.
LAYERS: dict[str, int] = {
    "core": 0,
    "sources": 1,
    "analysis": 2,
    "alerting": 3,
    "config": 4,
    "monitor": 5,
    "web": 6,
    "cli": 7,
}

#: Deliberate same-layer edges, each with the reason it is not a smell.
SIBLINGS: set[tuple[str, str]] = {
    # The adapters, the vendor registry and discovery are one subsystem: discovery
    # exists to produce an adapter, and the registry is the data both read.
    ("sources", "sources"),
    ("core", "core"),
    ("analysis", "analysis"),
    ("alerting", "alerting"),
    # api and server are the two halves of the web layer, split so the query layer can
    # be tested without a socket.
    ("web", "web"),
}


def layer_of(path: Path) -> str:
    relative = path.relative_to(ROOT)
    return relative.parts[0] if len(relative.parts) > 1 else relative.stem


def imports_of(path: Path) -> set[str]:
    """Every netpulse layer this file imports from."""
    tree = ast.parse(path.read_text())
    found: set[str] = set()
    for node in ast.walk(tree):
        module = ""
        if isinstance(node, ast.ImportFrom) and node.module:
            module = node.module
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("netpulse."):
                    found.add(alias.name.split(".")[1])
            continue
        if module.startswith("netpulse."):
            found.add(module.split(".")[1])
    return found


def source_files() -> list[Path]:
    return sorted(p for p in ROOT.rglob("*.py") if "__pycache__" not in p.parts)


@pytest.mark.parametrize("path", source_files(), ids=lambda p: str(p.relative_to(ROOT)))
def test_no_module_imports_from_a_layer_above_it(path: Path) -> None:
    if path.name == "__init__.py" and layer_of(path) in LAYERS:
        return  # a package docstring, no logic to place
    here = layer_of(path)
    if here not in LAYERS:
        pytest.skip(f"{here} is not a placed layer")
    for imported in imports_of(path):
        if imported not in LAYERS:
            continue
        if (here, imported) in SIBLINGS:
            continue
        assert LAYERS[imported] < LAYERS[here], (
            f"{path.relative_to(ROOT)} (layer '{here}') imports '{imported}', "
            f"which sits at or above it. The dependency graph must stay a DAG."
        )


def test_an_adapter_cannot_reach_the_store_or_the_collector() -> None:
    """The single most important edge. An adapter that could write to storage or ask
    the collector anything would stop being a one-file change, and the whole
    network-agnostic claim rests on it staying one."""
    forbidden = {"analysis", "alerting", "monitor", "web", "config"}
    for path in source_files():
        if layer_of(path) != "sources":
            continue
        overreach = imports_of(path) & forbidden
        assert not overreach, f"{path.name} reaches into {overreach}"
        # storage is in core, which sources may use — but must not.
        text = path.read_text()
        assert "core.storage" not in text, f"{path.name} imports the store"


def test_the_query_layer_holds_no_transport() -> None:
    """api.py answers questions; server.py moves bytes. The moment routing makes a
    decision about data, that decision belongs one layer down where it can be tested
    without opening a socket.

    Checked on imports rather than on the text, so a docstring may say the word
    "socket" — which api.py's does, to explain that it has none.
    """
    tree = ast.parse((ROOT / "web" / "api.py").read_text())
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
        elif isinstance(node, ast.Import):
            imported |= {alias.name.split(".")[0] for alias in node.names}
    for transport in ("socket", "http", "socketserver", "wsgiref", "asyncio"):
        assert transport not in imported, f"api.py imports {transport}"


def test_every_layer_is_documented() -> None:
    """A package with no docstring is a package nobody decided the shape of."""
    for package in ("core", "sources", "analysis", "alerting", "web"):
        text = (ROOT / package / "__init__.py").read_text()
        assert text.lstrip().startswith('"""'), f"{package} has no docstring"
        assert len(text) > 200, f"{package}'s docstring says too little to be useful"


def test_only_the_clock_reads_the_system_clock() -> None:
    """Everything takes a Clock instead, which is why the suite never sleeps and why
    billing cycles can be exercised at real dates."""
    for path in source_files():
        if path.relative_to(ROOT).as_posix() == "core/clock.py":
            continue
        text = path.read_text()
        assert "datetime.now(" not in text, f"{path.name} reads the system clock directly"
        assert "time.time()" not in text, f"{path.name} reads the system clock directly"
