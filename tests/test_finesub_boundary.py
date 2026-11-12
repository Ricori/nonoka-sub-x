"""FineSub stays behind its boundary (`docs/transcription-engines-plan.md` 1.5).

Inside `src/nonoka_x/`, only three files may touch FineSub before WP-P1:
`transcription/engines/finesub.py`, `correction/finesub.py` and
`finesub_bridge.py` (the fourth, the compatibility package
`src/finesub_bootstrap/`, lives outside this tree). Touching means, judged
from the code (AST) and not from comments or docstrings:

- `import:finesub` -- importing `finesub` (also lazily, inside a function);
- `import:finesub_bootstrap` -- importing `finesub_bootstrap`;
- `env:FINESUB_` -- a string literal naming a `FINESUB_*` variable;
- `file:finesub` -- a string literal naming `finesub.toml`, `finesub.env` or
  `model_catalog.toml`.

Everything that crosses the line today is listed in `TRANSITIONAL`, each
entry with the work package that removes it. The list may only get shorter:
a new crossing fails `test_no_new_crossings`, and an entry whose crossing is
gone fails `test_transitional_list_only_shrinks` until it is deleted here.
After WP-P1 the list is empty and so is `ALLOWED` (plan 4.27).
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "src" / "nonoka_x"

ALLOWED = frozenset({
    "transcription/engines/finesub.py",
    "correction/finesub.py",
    "finesub_bridge.py",
})

#: (file, kind) -> the work package that removes the crossing.
TRANSITIONAL: dict[tuple[str, str], str] = {
    ("model_install.py", "import:finesub_bootstrap"): "WP-R3",
    ("runtime_install.py", "import:finesub_bootstrap"): "WP-R3",
    ("provision.py", "import:finesub_bootstrap"): "WP-R3",
    ("provision.py", "env:FINESUB_"): "WP-R5",
    ("local_provider.py", "env:FINESUB_"): "WP-R5",
    ("worker.py", "import:finesub"): "WP-P1",  # WP4c removes finesub.stages first
    ("worker.py", "env:FINESUB_"): "WP-P1",
    ("settings.py", "import:finesub"): "WP-P1",
    ("settings.py", "import:finesub_bootstrap"): "WP-R4",
    ("settings.py", "env:FINESUB_"): "WP-R5",
    ("settings.py", "file:finesub"): "WP-R5",
    ("projector.py", "import:finesub"): "WP-P1",
    ("gpu_tier.py", "import:finesub"): "WP-P1",
    ("llm_worker.py", "import:finesub"): "WP-P1",
    ("knowledge_worker.py", "import:finesub"): "WP-P1",
}

#: Crossings that are not transitional: the migration tool has to recognise
#: the old layout it replaces, so its one inventory module may name FineSub's
#: old files (user 2026-11-12, WP-M1). Only that kind, only that file.
LEGACY_LAYOUT: dict[tuple[str, str], str] = {
    ("migration/legacy.py", "file:finesub"): "WP-M1",
}

WORK_PACKAGES = frozenset({"WP-R2", "WP-R3", "WP-R4", "WP-R5", "WP4b", "WP4c", "WP-P1"})
KINDS = frozenset({"import:finesub", "import:finesub_bootstrap", "env:FINESUB_", "file:finesub"})
_FILE_NAMES = ("finesub.toml", "finesub.env", "model_catalog.toml")


def _docstring_ids(tree: ast.AST) -> set[int]:
    """String constants that are statements of their own: docstrings and the like."""

    return {
        id(node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)
    }


def _module_root(name: str | None) -> str:
    return (name or "").split(".", 1)[0]


def crossings(source: str) -> set[str]:
    tree = ast.parse(source)
    found: set[str] = set()
    skipped = _docstring_ids(tree)
    for node in ast.walk(tree):
        roots: list[str] = []
        if isinstance(node, ast.Import):
            roots = [_module_root(alias.name) for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            roots = [_module_root(node.module)]
        elif (
            isinstance(node, ast.Call)
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
            and (
                (isinstance(node.func, ast.Name) and node.func.id in {"__import__", "import_module"})
                or (isinstance(node.func, ast.Attribute) and node.func.attr == "import_module")
            )
        ):
            roots = [_module_root(node.args[0].value)]
        for root in roots:
            if root in {"finesub", "finesub_bootstrap"}:
                found.add(f"import:{root}")
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in skipped:
            if "FINESUB_" in node.value:
                found.add("env:FINESUB_")
            if any(name in node.value for name in _FILE_NAMES):
                found.add("file:finesub")
    return found


def scan() -> dict[tuple[str, str], None]:
    result: dict[tuple[str, str], None] = {}
    for path in sorted(PACKAGE.rglob("*.py")):
        relative = path.relative_to(PACKAGE).as_posix()
        for kind in crossings(path.read_text(encoding="utf-8")):
            result[(relative, kind)] = None
    return result


def test_scanner_reads_code_not_comments() -> None:
    source = '''
"""Mentions FINESUB_HOME and finesub.toml in a docstring."""
# import finesub -- a comment
import os
def f():
    """FINESUB_X"""
    from finesub.stages import run_pipeline
    import finesub_bootstrap.paths
    os.environ["FINESUB_CONFIG_FILE"] = "x"
    return f"{os.sep}model_catalog.toml", importlib.import_module("finesub.llm")
'''
    assert crossings(source) == {
        "import:finesub", "import:finesub_bootstrap", "env:FINESUB_", "file:finesub",
    }
    assert crossings('"""FINESUB_ finesub.env"""\n# from finesub import x\nimport os\n') == set()
    # A relative import of a sibling module named like FineSub is not FineSub.
    assert crossings("from .finesub import engine\n") == set()


def test_no_new_crossings() -> None:
    unexpected = sorted(
        f"{file}: {kind}"
        for file, kind in scan()
        if file not in ALLOWED and (file, kind) not in TRANSITIONAL and (file, kind) not in LEGACY_LAYOUT
    )
    assert not unexpected, (
        "FineSub crosses its boundary outside the allowed files "
        "(plan 1.5; only transcription/engines/finesub.py, correction/finesub.py, "
        "finesub_bridge.py may):\n  " + "\n  ".join(unexpected)
    )


def test_transitional_list_only_shrinks() -> None:
    found = scan()
    stale = sorted(f"{file}: {kind} ({owner})" for (file, kind), owner in TRANSITIONAL.items() if (file, kind) not in found)
    assert not stale, (
        "these crossings are gone; delete them from TRANSITIONAL so they cannot come back:\n  "
        + "\n  ".join(stale)
    )


def test_the_legacy_layout_exemption_is_used_and_narrow() -> None:
    found = scan()
    for (file, kind) in LEGACY_LAYOUT:
        assert (file, kind) in found, f"{file}: {kind} no longer crosses; delete it from LEGACY_LAYOUT"
        assert kind == "file:finesub", (file, kind)
        assert file not in ALLOWED, file


def test_transitional_entries_are_well_formed() -> None:
    for (file, kind), owner in TRANSITIONAL.items():
        assert (PACKAGE / file).is_file(), file
        assert file not in ALLOWED, file
        assert kind in KINDS, (file, kind)
        assert owner in WORK_PACKAGES, (file, kind, owner)


def test_new_layers_do_not_cross() -> None:
    """The contract packages and facades WP0 added start clean and stay so."""

    for relative in ("transcription", "correction", "runtime", "config"):
        for path in sorted((PACKAGE / relative).rglob("*.py")):
            name = path.relative_to(PACKAGE).as_posix()
            if name in ALLOWED:
                continue
            assert not crossings(path.read_text(encoding="utf-8")), name
