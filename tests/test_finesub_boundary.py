"""FineSub stays behind its boundary: the core refers to no engine package (plan 4.6 item 33, WP-P1).

Since WP-P1 FineSub lives in its engine packages (`engines/`), which the core
finds by their `PACKAGE.json` and starts as processes of their own
(`nonoka_x.engine_host`). Inside `src/nonoka_x/`, judged from the code (AST)
and not from comments or docstrings (owner 2026-11-17, WP-P1 question 2):

- `import:<package>` -- importing `finesub`, `finesub_bootstrap`, the knowledge
  protocol (`finesub_knowledge`) or a package's own code (`finesub_transcribe`,
  `finesub_correct`, `finesub_knowledge_host`), also lazily;
- `env:FINESUB_` -- a string naming a `FINESUB_*` variable;
- `file:finesub` -- a string naming one of FineSub's own files (`finesub.toml`,
  `finesub.env`, `model_catalog.toml`, the model caches' `.finesub-verified.json`);
- `package:<id>` -- a string naming an engine package's directory
  (`finesub-transcribe`, `finesub-correct`, `finesub-knowledge`).

There is no allow-list of files that may. What does not count: comments and
docstrings, the pinned data under `resources/` (red line rule 5), Nonoka's own
engine ids (`finesub`, `finesub-cloud`, the request protocol's), and words in
messages. The exemptions below are the only ones, each with why.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "src" / "nonoka_x"

#: What the code of an engine package is imported as.
PACKAGE_MODULES = frozenset({
    "finesub", "finesub_bootstrap", "finesub_knowledge",
    "finesub_transcribe", "finesub_correct", "finesub_knowledge_host",
})
_FILE_NAMES = ("finesub.toml", "finesub.env", "model_catalog.toml", ".finesub-verified.json")
_PACKAGE_IDS = ("finesub-transcribe", "finesub-correct", "finesub-knowledge")

#: (file, kind) -> why it may. Not transitional: none of these goes away with a work package.
EXEMPT: dict[tuple[str, str], str] = {
    # The migration has to recognise the old layout it replaces: the one inventory module may name
    # FineSub's old files and the old key file's variable names (owner 2026-11-12, WP-M1; WP-R4),
    # and where their contents go -- the private directories of the packages that use them (4.4 item 25).
    ("migration/legacy.py", "file:finesub"): "WP-M1",
    ("migration/legacy.py", "env:FINESUB_"): "WP-R4",
    ("migration/legacy.py", "package:finesub-knowledge"): "WP-M1",
    ("migration/legacy.py", "package:finesub-correct"): "WP-M1",
    # And where an old task's transcription outputs go: its package's directory in the task (4.6 item 34, WP-M2).
    ("migration/legacy.py", "package:finesub-transcribe"): "WP-M2",
    # The model layer carries the engine's own verification markers, written at build time so the
    # engine accepts the models as they are (owner 2026-11-12, appendix B question 53, WP-E1).
    ("runtime/layer_manifest.py", "file:finesub"): "WP-E1",
}


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
            if root in PACKAGE_MODULES:
                found.add(f"import:{root}")
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in skipped:
            if "FINESUB_" in node.value:
                found.add("env:FINESUB_")
            if any(name in node.value for name in _FILE_NAMES):
                found.add("file:finesub")
            for package in _PACKAGE_IDS:
                if package in node.value:
                    found.add(f"package:{package}")
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
    from finesub_knowledge.client import prepare_subjects
    os.environ["FINESUB_CONFIG_FILE"] = "x"
    return f"{os.sep}model_catalog.toml", importlib.import_module("finesub_correct.engine"), "engines/finesub-correct"
'''
    assert crossings(source) == {
        "import:finesub", "import:finesub_bootstrap", "import:finesub_knowledge", "import:finesub_correct",
        "env:FINESUB_", "file:finesub", "package:finesub-correct",
    }
    assert crossings('"""FINESUB_ finesub.env"""\n# from finesub import x\nimport os\n') == set()
    # A relative import of a sibling module named like FineSub is not FineSub.
    assert crossings("from .finesub import engine\n") == set()
    # Nonoka's own engine ids are not crossings.
    assert crossings('DEFAULT_ENGINE = "finesub"\nCLOUD_ENGINE = "finesub-cloud"\n') == set()


def test_the_core_refers_to_no_engine_package() -> None:
    unexpected = sorted(f"{file}: {kind}" for file, kind in scan() if (file, kind) not in EXEMPT)
    assert not unexpected, (
        "the core refers to an engine package (plan 4.6 item 33: no import of its code, no `FINESUB_*`, "
        "none of FineSub's files, no package's directory):\n  " + "\n  ".join(unexpected)
    )


def test_every_exemption_is_used() -> None:
    found = scan()
    stale = sorted(f"{file}: {kind}" for file, kind in EXEMPT if (file, kind) not in found)
    assert not stale, "these no longer cross; delete them from EXEMPT so they cannot come back:\n  " + "\n  ".join(stale)


def test_the_vendored_engine_and_the_bridge_are_gone_from_the_core() -> None:
    for gone in ("finesub_bridge.py", "transcription/engines/finesub.py", "correction/finesub.py",
                 "llm_worker.py", "knowledge_worker.py", "cloud_route.py"):
        assert not (PACKAGE / gone).exists(), gone
    assert not (ROOT / "src" / "finesub_knowledge").exists()
