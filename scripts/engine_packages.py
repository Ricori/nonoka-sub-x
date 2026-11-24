"""Package lists of the three FineSub engine packages (WP-P0, split in WP-P1).

`docs/transcription-engines-plan.md` 4.6 items 33-35a split the vendored
FineSub into `engines/finesub-transcribe/`, `engines/finesub-correct/` and
`engines/finesub-knowledge/`. This script computes which FineSub files each
package needs and writes the machine-readable lists under
`docs/engine-packages/`. WP-P0 computed them from the vendored tree; WP-P1
split that tree by these lists, and since then the script reads the packages'
own `src/` taken together (a file two packages carry is the same bytes in
both; the transcription package's `finesub_bootstrap` is the original), so a
rerun shows whether the packages still carry what their code reaches. It
reads, never writes, anything under `engines/`.

How a file gets into a package
------------------------------

1. **Static reachability, at symbol level.** Every module of
   `finesub` and `finesub_bootstrap` in the packages' `src/` is parsed. A node is a
   module's import-time code (`<module>`), one top-level function, or one
   top-level class (with all its methods). Each node's edges are the names it
   loads, resolved through that module's imports (and the function's own lazy
   imports) to another node. Importing `a.b.c` runs `a`, `a.b` and `a.b.c`, so
   reaching any symbol of a module reaches its `<module>` node and its parent
   packages'. Starting from each package's entry points (`ENTRIES`, every one
   with the core file and line that uses it today) the reached `<module>`
   nodes are the package's modules. Symbol level matters for `stages.py`,
   which holds both halves: `run_transcription` and `run_post_stable` reach
   different functions in it.

   Deliberately over-approximate: a reached class reaches all its methods; the
   import-time code of a module includes everything at its top level except
   the `if __name__ == "__main__":` block (a `-m` entry reaches that block
   as `<main>`); annotations count only in modules without
   `from __future__ import annotations`; `if TYPE_CHECKING:` imports do not
   count.

2. **Runtime import records** (`--record`, see `RECORDS`): `sys.modules` after
   a stubbed run of each package's real entry points (the existing tests and
   harness scenarios, run in a child interpreter). The lists are the union of
   1 and 2; a recorded module the static graph missed is reported.

3. **Package data** (non-`.py` files) follows its reader (`DATA_READERS`): a
   data file is in every package that contains one of its readers.

4. **Fixed rules.** `finesub_bootstrap/` travels whole and unchanged with the
   transcription package (plan 4.2 item 16). Package `__init__.py` files of
   every included module are included.

Usage::

    D:\\Envir\\Python312\\python scripts/engine_packages.py            # write the lists
    D:\\Envir\\Python312\\python scripts/engine_packages.py --check    # fail if they would change
    D:\\Envir\\Python312\\python scripts/engine_packages.py --record   # re-run the runtime records first

`--record` uses the interpreters named in `RECORDS` and needs about a
minute; the lists themselves are computed from the code and the committed
records only, so a plain run is deterministic.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).resolve().parents[1]
ENGINES = ROOT / "engines"
#: The packages' FineSub sources, in the order a path two of them carry is read
#: (the same bytes either way). `finesub_bootstrap` only from the transcription
#: package: the others carry a compatibility package with fewer names.
SOURCE_ORDER = ("finesub-transcribe", "finesub-correct", "finesub-knowledge")
#: The lock of the vendored tree the packages were split from.
LOCK = ROOT / "tests" / "engine-packages" / "vendor-quality-lock.json"
#: What no package carries, with the reason (written by the split, WP-P1).
NOT_CARRIED = ROOT / "tests" / "engine-packages" / "not-carried.json"
SURFACE = ROOT / "tests" / "fixtures" / "quality" / "bootstrap-surface.json"
OUT = ROOT / "tests" / "engine-packages"
RECORD_DIR = OUT / "runtime-imports"

PACKAGES = ("transcribe", "correct", "knowledge")


def source_files() -> dict[str, Path]:
    """Every FineSub file the packages carry, by its path below `src/`, read from its first package."""

    found: dict[str, Path] = {}
    for package in SOURCE_ORDER:
        source = ENGINES / package / "src"
        for path in sorted(source.rglob("*")):
            if not path.is_file() or "__pycache__" in path.parts or path.suffix in {".pyc", ".pyo"}:
                continue
            relative = path.relative_to(source).as_posix()
            if relative.startswith("finesub_bootstrap/") and package != "finesub-transcribe":
                continue
            if relative in found:
                if found[relative].read_bytes() != path.read_bytes():
                    raise SystemExit(f"{package} carries a different {relative}")
                continue
            found[relative] = path
    return found


SOURCES = source_files()
PACKAGE_DIRS = {
    "transcribe": "engines/finesub-transcribe",
    "correct": "engines/finesub-correct",
    "knowledge": "engines/finesub-knowledge",
}

# ---------------------------------------------------------------------------
# Entry points: what each package's own code calls of FineSub's, with where
# (since WP-P1 the packages' adapters under `engines/<package>/adapter/`, and
# the knowledge protocol's two ends). `module:name` is a symbol,
# `module:<main>` is `python -m module`, `module:<module>` the module's import alone.

#: Entry points no import in an adapter shows: (symbol, who uses it).
EXTRA_ENTRIES: dict[str, list[tuple[str, str]]] = {
    "transcribe": [
        # The tiers a request may name (`PACKAGE.json` `gpu_tiers`, the cloud's
        # `resolve_request_gpu_tier`); the package declares what this returns.
        ("finesub.speech.runtime.resources:gpu_tier_cli_choices", "engines/finesub-transcribe/PACKAGE.json gpu_tiers"),
    ],
    "correct": [
        ("finesub.llm.agent.agent_mcp_server:<main>", "finesub/llm/client.py:2504 (MCP child, `python -m`)"),
    ],
    "knowledge": [],
}


_CATCHES_IMPORT_ERROR = {"ImportError", "ModuleNotFoundError", "Exception", "BaseException"}


def _optional_imports(tree: ast.AST) -> set[ast.AST]:
    """Import statements inside a `try:` that catches their failure: the adapter runs on without them."""

    optional: set[ast.AST] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        caught = set()
        for handler in node.handlers:
            if handler.type is None:
                caught.add("BaseException")
            for item in ast.walk(handler.type) if handler.type is not None else ():
                if isinstance(item, ast.Name):
                    caught.add(item.id)
        if not caught & _CATCHES_IMPORT_ERROR:
            continue
        for statement in node.body:
            for inner in ast.walk(statement):
                if isinstance(inner, (ast.Import, ast.ImportFrom)):
                    optional.add(inner)
    return optional


def adapter_uses(package: str) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """What a package's adapter code uses of FineSub's: (entry points, optional uses).

    Read from the adapters themselves (`engines/<package>/adapter/`), so the
    lists follow the code: `from finesub.x import name` is `finesub.x:name`;
    `from finesub.x import module` (a module) is `finesub.x.module:<module>`,
    plus `finesub.x.module:attr` for each `module.attr` the file reads. In the
    knowledge package a string `"finesub.x:name"` (the server's allow-list) is
    `finesub.x:name` too; in the other two such strings name what the
    knowledge process runs, not this one.

    An import inside a `try:` that catches its failure is optional: the
    adapter carries on without it (the shared `bridge` pins what is there to
    pin, `failures` quotes a vendor's words when there are any). It enters a
    package only when something else reaches it, and is listed as optional.
    A symbol is resolved against the graph later (`entry_node`), which is
    where a typo fails.
    """

    adapter = ROOT / PACKAGE_DIRS[package] / "adapter"
    found: set[tuple[str, str]] = set()
    optional_found: set[tuple[str, str]] = set()
    for path in sorted(adapter.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        where = path.relative_to(ROOT).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        optional = _optional_imports(tree)
        modules: dict[str, tuple[str, bool]] = {}  # local name -> (FineSub module bound to it, optional)
        for node in ast.walk(tree):
            target = optional_found if node in optional else found
            if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module and \
                    node.module.split(".", 1)[0] in ("finesub", "finesub_bootstrap"):
                for alias in node.names:
                    full = f"{node.module}.{alias.name}"
                    if full == "finesub_bootstrap._launch":
                        # The compatibility packages' own binding (Nonoka code), not FineSub's.
                        continue
                    if full.replace(".", "/") + ".py" in SOURCES or \
                            full.replace(".", "/") + "/__init__.py" in SOURCES:
                        modules[alias.asname or alias.name] = (full, node in optional)
                        target.add((f"{full}:<module>", f"{where}:{node.lineno}"))
                    else:
                        target.add((f"{node.module}:{alias.name}", f"{where}:{node.lineno}"))
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.split(".", 1)[0] in ("finesub", "finesub_bootstrap"):
                        target.add((f"{alias.name}:<module>", f"{where}:{node.lineno}"))
                        if alias.asname:
                            modules[alias.asname] = (alias.name, node in optional)
            elif package == "knowledge" and isinstance(node, ast.Constant) and isinstance(node.value, str) and \
                    re.fullmatch(r"finesub(_bootstrap)?(\.\w+)+:\w+(\.\w+)*", node.value):
                module, _, attribute = node.value.partition(":")
                found.add((f"{module}:{attribute.split('.', 1)[0]}", f"{where}:{node.lineno}"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id in modules:
                module, is_optional = modules[node.value.id]
                (optional_found if is_optional else found).add((f"{module}:{node.attr}", f"{where}:{node.lineno}"))

    def merged(items: set[tuple[str, str]]) -> list[tuple[str, str]]:
        by_symbol: dict[str, list[str]] = defaultdict(list)
        for symbol, where in sorted(items):
            by_symbol[symbol].append(where)
        return [(symbol, ", ".join(wheres)) for symbol, wheres in by_symbol.items()]

    entries = merged(found)
    taken = {symbol for symbol, _ in entries}
    return sorted(entries + EXTRA_ENTRIES[package]), [item for item in merged(optional_found) if item[0] not in taken]


USES = {package: adapter_uses(package) for package in PACKAGES}
ENTRIES: dict[str, list[tuple[str, str]]] = {package: USES[package][0] for package in PACKAGES}
OPTIONAL: dict[str, list[tuple[str, str]]] = {package: USES[package][1] for package in PACKAGES}

#: Edges the static graph has but the package's runs never take: (from node,
#: to node or `module` prefix, why, with the guarding line). Only `stages.py`,
#: which holds both halves behind `stage`/`scope` guards, needs any. Every
#: pruned edge is reported in the package list; the runtime records confirm
#: none of them is taken.
NOT_TAKEN: dict[str, list[tuple[str, str, str]]] = {
    "transcribe": [
        ("finesub.stages:<module>", "finesub.stages:run_pipeline",
         "stages.py:1155-1159: `_PIPELINE_DEFAULTS` reads `inspect.signature(run_pipeline)`, it does not call it"),
        ("finesub.stages:_prepare_run", "finesub.stages:_validate_llm_configuration",
         "stages.py:1270: only when stage > raw-srt; run_transcription stops at stable (stages.py:1168)"),
        ("finesub.stages:_prepare_run", "finesub.stages:_correction_request",
         "stages.py:1336: only when stage >= translated-srt"),
        ("finesub.stages:_finish_run", "finesub.llm.task_report",
         "stages.py:1612: only when the target is >= translated-srt"),
    ],
    "correct": [
        ("finesub.stages:<module>", "finesub.stages:run_pipeline",
         "stages.py:1155-1159: `_PIPELINE_DEFAULTS` reads `inspect.signature(run_pipeline)`, it does not call it"),
        ("finesub.stages:_run_diarization_stage", "finesub.speech.diarization.stage:run_diarization",
         "stages.py:862: only when `-diar.json` is missing; the correction adapter enters it only when it is on disk "
         "(src/nonoka_x/correction/finesub.py:274)"),
        ("finesub.stages:_run_diarization_stage", "finesub.stages:_diarization_device",
         "stages.py:868: an argument of the same `run_diarization` call"),
        ("finesub.speech.recognition.asr_backend:<module>", "finesub.speech.recognition.fw_refine_backend",
         "speech/recognition/asr_backend.py:89-92: `BACKENDS` names the backend as a string, \"imported by the "
         "contract test only\"; nothing in the pipeline resolves it"),
        *[
            (f"finesub.speech.diarization.{backend}:<module>", f"finesub.speech.diarization.{backend}:{field}",
             f"speech/diarization/{backend}.py `BACKEND = Backend(ensure=..., activity=...)`: the two callables "
             "are called only in `stage.run_diarization` (stage.py:259, :276), which correction never enters")
            for backend in ("diarizen_backend", "nemotron3_backend", "sortformer_backend")
            for field in ("ensure", "activity")
        ],
    ],
}

# The FineSub CLI and its batch machinery: no Nonoka path runs it, and since
# WP-P1 no package carries it. WP-P0 computed what it alone would pull in; that
# is kept in `not-carried.json`, which a rerun reads instead.
CLI_ENTRIES = [("finesub.pipeline:<main>", "python -m finesub.pipeline")]

# Non-`.py` files and the modules that read them (with the reading line).
DATA_READERS: list[tuple[str, list[tuple[str, str]]]] = [
    ("finesub/llm/prompt_templates/", [
        ("finesub.llm.prompt_tables", "finesub/llm/prompt_tables.py:16"),
        ("finesub.llm.prompt_compose", "finesub/llm/prompt_compose.py:130"),
        ("finesub.llm.example_builder", "finesub/llm/example_builder.py:53"),
        ("finesub.llm.agent.channels", "finesub/llm/agent/channels.py:50"),
        ("finesub.llm.agent.local_agent", "finesub/llm/agent/local_agent.py:249"),
    ]),
    ("finesub/llm/routing/model_catalog.toml", [("finesub.llm.routing.model_catalog", "finesub/llm/routing/model_catalog.py:514")]),
    ("finesub/llm/routing/model_routes.toml", [("finesub.llm.routing.model_routes", "finesub/llm/routing/model_routes.py:948")]),
    ("finesub/llm/knowledge/node/presets/", [("finesub.llm.knowledge.node.presets", "finesub/llm/knowledge/node/presets.py:549")]),
    ("finesub/llm/glossary-manifest.json", [("finesub.llm.glossary_store", "finesub/llm/glossary_store.py:155")]),
]

# Runtime import records: (name, interpreter, what runs, package it evidences).
PBS_PYTHON = Path(r"D:\Development\nonoka-sub-x\.tmp\pbs-3.12.1\python\python.exe")
SYSTEM_PYTHON = Path(r"D:\Envir\Python312\python.exe")
RECORDS: list[dict] = [
    {"name": "correct-tests", "package": "correct", "python": "system",
     "pytest": ["tests/test_finesub_correction.py", "tests/test_axis.py", "tests/test_llm_worker.py"]},
    {"name": "transcribe-tests", "package": "transcribe", "python": "system",
     "pytest": ["tests/test_engine_finesub.py"]},
    {"name": "knowledge-tests", "package": "knowledge", "python": "system",
     "pytest": ["tests/test_knowledge_worker.py", "tests/test_knowledge_subjects.py"]},
    {"name": "split-contract", "package": None, "python": "system",
     "pytest": ["tests/test_split_stage_contract.py"]},
]

# ---------------------------------------------------------------------------
# Module index


@dataclass
class Module:
    name: str
    path: Path
    relative: str  # relative to a package's src/, posix
    is_package: bool
    tree: ast.Module
    future_annotations: bool
    bindings: dict[str, tuple[str, str | None]] = field(default_factory=dict)  # name -> (module, attr|None)
    defs: dict[str, ast.AST] = field(default_factory=dict)
    module_vars: set[str] = field(default_factory=set)


def discover() -> dict[str, Module]:
    modules: dict[str, Module] = {}
    for relative, path in SOURCES.items():
        if relative.split("/", 1)[0] not in ("finesub", "finesub_bootstrap") or not relative.endswith(".py"):
            continue
        parts = relative[:-3].split("/")
        is_package = parts[-1] == "__init__"
        if is_package:
            parts = parts[:-1]
        name = ".".join(parts)
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        future = any(
            isinstance(node, ast.ImportFrom) and node.module == "__future__"
            and any(alias.name == "annotations" for alias in node.names)
            for node in tree.body
        )
        modules[name] = Module(name, path, relative, is_package, tree, future)
    return modules


def resolve_relative(module: Module, level: int, target: str | None) -> str:
    base = module.name.split(".")
    if not module.is_package:
        base = base[:-1]
    if level > 1:
        base = base[: len(base) - (level - 1)]
    return ".".join([*base, target] if target else base)


def is_type_checking(test: ast.AST) -> bool:
    return (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
        isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"
    )


def is_main_guard(node: ast.AST) -> bool:
    if not isinstance(node, ast.If):
        return False
    test = node.test
    return (
        isinstance(test, ast.Compare) and isinstance(test.left, ast.Name) and test.left.id == "__name__"
        and len(test.comparators) == 1 and isinstance(test.comparators[0], ast.Constant)
        and test.comparators[0].value == "__main__"
    )


class Graph:
    """Symbol-level reference graph over the vendored tree."""

    def __init__(self, modules: dict[str, Module]) -> None:
        self.modules = modules
        self.edges: dict[str, set[str]] = defaultdict(set)
        self.import_sites: dict[str, list[tuple[str, int, str]]] = defaultdict(list)  # node -> (target, line, text)
        self.calls: dict[str, list[tuple[str, int]]] = defaultdict(list)  # node -> (target node, line)
        for module in modules.values():
            self._index(module)
        for module in modules.values():
            self._edges(module)

    # -- indexing ---------------------------------------------------------

    def _bind_import(self, module: Module, node: ast.AST, into: dict[str, tuple[str, str | None]]) -> list[str]:
        """Record the names `node` binds; return the modules it imports."""

        imported: list[str] = []
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported.append(alias.name)
                if alias.asname:
                    into[alias.asname] = (alias.name, None)
                else:
                    head = alias.name.split(".", 1)[0]
                    into[head] = (head, None)
        elif isinstance(node, ast.ImportFrom):
            if node.module == "__future__":
                return []
            base = resolve_relative(module, node.level, node.module) if node.level else (node.module or "")
            imported.append(base)
            for alias in node.names:
                if alias.name == "*":
                    continue
                local = alias.asname or alias.name
                full = f"{base}.{alias.name}"
                if full in self.modules:
                    into[local] = (full, None)
                    imported.append(full)
                else:
                    into[local] = (base, alias.name)
        return imported

    def _index(self, module: Module) -> None:
        def walk_top(body: list[ast.stmt]) -> None:
            for node in body:
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    self._bind_import(module, node, module.bindings)
                elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    module.defs[node.name] = node
                elif isinstance(node, ast.If):
                    if is_type_checking(node.test) or is_main_guard(node):
                        continue
                    walk_top(node.body)
                    walk_top(node.orelse)
                elif isinstance(node, ast.Try):
                    walk_top(node.body)
                    for handler in node.handlers:
                        walk_top(handler.body)
                    walk_top(node.orelse)
                    walk_top(node.finalbody)
                elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                    for target in targets:
                        for leaf in ast.walk(target):
                            if isinstance(leaf, ast.Name):
                                module.module_vars.add(leaf.id)
                elif isinstance(node, (ast.With,)):
                    walk_top(node.body)

        walk_top(module.tree.body)

    # -- resolution -------------------------------------------------------

    def module_node(self, name: str) -> str:
        return f"{name}:<module>"

    def symbol_node(self, module: str, attr: str, seen: frozenset = frozenset()) -> str | None:
        """The node `module.attr` names, following re-exports."""

        if module not in self.modules:
            return None
        if (module, attr) in seen:
            return self.module_node(module)
        target = self.modules[module]
        full = f"{module}.{attr}"
        if full in self.modules:
            return self.module_node(full)
        if attr in target.defs:
            return f"{module}:{attr}"
        if attr in target.bindings:
            bound_module, bound_attr = target.bindings[attr]
            if bound_attr is None:
                return self.module_node(bound_module) if bound_module in self.modules else self.module_node(module)
            return self.symbol_node(bound_module, bound_attr, seen | {(module, attr)}) or self.module_node(module)
        return self.module_node(module)

    def parents(self, name: str) -> list[str]:
        parts = name.split(".")
        return [".".join(parts[:i]) for i in range(1, len(parts) + 1) if ".".join(parts[:i]) in self.modules]

    def _add_import(self, node_id: str, imported: Iterable[str], line: int, text: str) -> None:
        for name in imported:
            for parent in self.parents(name):
                self.edges[node_id].add(self.module_node(parent))
            if name.split(".", 1)[0] in {"finesub", "finesub_bootstrap"}:
                self.import_sites[node_id].append((name, line, text))

    def _refs(self, module: Module, node_id: str, body: Iterable[ast.AST], local: dict[str, tuple[str, str | None]]) -> None:
        """Edges for every name loaded under `body`, lazy imports included."""

        scope = dict(module.bindings)
        scope.update(local)
        stack = list(body)
        annotations: list[ast.AST] = []
        visited: list[ast.AST] = []
        while stack:
            item = stack.pop()
            visited.append(item)
            if isinstance(item, (ast.Import, ast.ImportFrom)):
                imported = self._bind_import(module, item, scope)
                self._add_import(node_id, imported, item.lineno, ast.unparse(item))
                continue
            if isinstance(item, ast.If) and is_type_checking(item.test):
                stack.extend(item.orelse)
                continue
            if module.future_annotations:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    for arg in [*item.args.args, *item.args.kwonlyargs, *item.args.posonlyargs,
                                *(a for a in (item.args.vararg, item.args.kwarg) if a)]:
                        if arg.annotation is not None:
                            annotations.append(arg.annotation)
                            arg.annotation = None  # do not walk it below
                    if item.returns is not None:
                        annotations.append(item.returns)
                if isinstance(item, ast.AnnAssign):
                    annotations.append(item.annotation)
                    if item.value is not None:
                        stack.append(item.value)
                    stack.append(item.target)
                    continue
            stack.extend(ast.iter_child_nodes(item))
        docstrings = {
            id(node.value) for node in visited
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)
        }
        for item in visited:
            if isinstance(item, ast.Constant) and isinstance(item.value, str) and id(item) not in docstrings:
                self._ref_module_string(node_id, item)
            if isinstance(item, ast.Name) and isinstance(item.ctx, ast.Load):
                self._ref_name(module, node_id, scope, item)
            elif isinstance(item, ast.Attribute) and isinstance(item.ctx, ast.Load):
                self._ref_attribute(module, node_id, scope, item)

    _MODULE_STRING = re.compile(r"(finesub(?:_bootstrap)?(?:\.[A-Za-z_]\w*)+)(?::([A-Za-z_]\w*))?")

    def _ref_module_string(self, node_id: str, item: ast.Constant) -> None:
        """A string literal that is exactly a module name (`["-m", "<it>"]` for
        a child process, `importlib.resources.files("<it>")`) or a
        `"<module>:<Attr>"` registry entry (`asr_backend.py:91` imports its
        backends that way). The module has to be there, so it is an edge: to
        its import, its `__main__` block, and the named attribute.

        Only whole-string matches count. Strings that merely mention a module
        inside a sentence are, in this tree, hints shown to a person
        (`prog="python -m ..."`, "run `python -m finesub.llm.knowledge repair`")
        and run nothing. The one command string that does run one --
        `agent_transports.agent_task_command` (agent_transports.py:178), handed
        to a conversational agent -- is unreachable from every entry, so it
        needs no rule."""

        match = self._MODULE_STRING.fullmatch(item.value.strip())
        if not match:
            return
        name, attr = match.group(1), match.group(2)
        if name not in self.modules:
            return
        self.edges[node_id].add(self.module_node(name))
        self.edges[node_id].add(f"{name}:<main>")
        if f"{name}.__main__" in self.modules:
            self.edges[node_id].add(f"{name}.__main__:<module>")
        if attr:
            target = self.symbol_node(name, attr)
            if target:
                self.edges[node_id].add(target)
        self.calls[node_id].append((f"{name}:<main>", item.lineno))

    def _ref_name(self, module: Module, node_id: str, scope, item: ast.Name) -> None:
        name = item.id
        target: str | None = None
        if name in scope:
            bound_module, attr = scope[name]
            if attr is None:
                target = self.module_node(bound_module) if bound_module in self.modules else None
            else:
                target = self.symbol_node(bound_module, attr)
        elif name in module.defs:
            target = f"{module.name}:{name}"
        elif name in module.module_vars:
            target = self.module_node(module.name)
        if target and target != node_id:
            self.edges[node_id].add(target)
            self.calls[node_id].append((target, item.lineno))

    def _ref_attribute(self, module: Module, node_id: str, scope, item: ast.Attribute) -> None:
        chain: list[str] = []
        cursor: ast.AST = item
        while isinstance(cursor, ast.Attribute):
            chain.append(cursor.attr)
            cursor = cursor.value
        if not isinstance(cursor, ast.Name) or cursor.id not in scope:
            return
        bound_module, attr = scope[cursor.id]
        if attr is not None:
            return
        current = bound_module
        for part in reversed(chain):
            if f"{current}.{part}" in self.modules:
                current = f"{current}.{part}"
                continue
            target = self.symbol_node(current, part) if current in self.modules else None
            if target and target != node_id:
                self.edges[node_id].add(target)
                self.calls[node_id].append((target, item.lineno))
            return
        if current in self.modules:
            self.edges[node_id].add(self.module_node(current))

    def _edges(self, module: Module) -> None:
        init = self.module_node(module.name)
        # parent packages run first
        for parent in self.parents(module.name)[:-1]:
            self.edges[init].add(self.module_node(parent))
        top: list[ast.AST] = []
        main_block: list[ast.AST] = []
        for node in module.tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                # decorators, defaults and (for classes) bases run at import time
                top.extend(node.decorator_list)
                if isinstance(node, ast.ClassDef):
                    top.extend(node.bases)
                    top.extend(kw.value for kw in node.keywords)
                else:
                    top.extend(d for d in node.args.defaults if d is not None)
                    top.extend(d for d in node.args.kw_defaults if d is not None)
                symbol = f"{module.name}:{node.name}"
                self.edges[symbol].add(init)
                self._refs(module, symbol, [node], {})
            elif is_main_guard(node):
                main_block.append(node)
            else:
                top.append(node)
        self._refs(module, init, top, {})
        main = f"{module.name}:<main>"
        self.edges[main].add(init)
        self._refs(module, main, main_block, {})

    # -- queries ----------------------------------------------------------

    def reach(self, starts: Iterable[str], not_taken: Iterable[tuple[str, str, str]] = ()) -> set[str]:
        skip: dict[str, list[str]] = defaultdict(list)
        for source, target, _ in not_taken:
            skip[source].append(target)

        def taken(source: str, target: str) -> bool:
            for blocked in skip.get(source, ()):
                if target == blocked or (":" not in blocked and target.split(":", 1)[0] == blocked):
                    return False
            return True

        seen: set[str] = set()
        stack = list(starts)
        while stack:
            node = stack.pop()
            if node in seen:
                continue
            seen.add(node)
            stack.extend(t for t in self.edges.get(node, ()) if taken(node, t))
        return seen

    def modules_of(self, nodes: Iterable[str]) -> set[str]:
        return {node.split(":", 1)[0] for node in nodes if node.endswith(":<module>")}


def entry_node(graph: Graph, spec: str) -> str:
    module, attr = spec.split(":", 1)
    if module not in graph.modules:
        raise SystemExit(f"entry {spec}: no module {module}")
    if attr in {"<module>", "<main>"}:
        return f"{module}:{attr}"
    node = graph.symbol_node(module, attr)
    # Falling back to the module's own import-time node means the name was not
    # found; landing on another module's is a submodule it imports
    # (`qwen_referee.phase_timing`), which is defined.
    if node is None or node == graph.module_node(module) and attr not in graph.modules[module].module_vars:
        raise SystemExit(f"entry {spec}: {attr} is not defined in {module}")
    return node


# ---------------------------------------------------------------------------
# Knowledge access points (question 10)

KNOWLEDGE_PREFIX = "finesub.llm.knowledge"

#: Functions of the knowledge code that open the knowledge store or its files
#: (read, write or lock). Found by reading `llm/knowledge/`: every path to the
#: data goes through one of these. Each with what it does and where.
KNOWLEDGE_PRIMITIVES: dict[str, tuple[str, str]] = {
    "finesub.llm.knowledge.base:knowledge_root_path": ("root", "base.py:52 resolves FINESUB_KNOWLEDGE_ROOT / the root argument"),
    "finesub.llm.knowledge.base:<module>": ("root", "base.py:31 DEFAULT_KNOWLEDGE_ROOT = resolve_knowledge_root(required=False) at import"),
    "finesub.llm.knowledge.base:_repo": ("read", "base.py:294 KnowledgeRepo.open(root)"),
    "finesub.llm.knowledge.base:knowledge_write_lock": ("lock", "base.py:830 cross-process write lock on <root>/../knowledge.lock"),
    "finesub.llm.knowledge.base:knowledge_lock_path": ("lock", "base.py:751"),
    "finesub.llm.knowledge.base:worktree_blocks_knowledge_write": ("read", "base.py:763"),
    "finesub.llm.knowledge.base:knowledge_signals_enabled": ("read", "base.py:787"),
    "finesub.llm.knowledge.node.repo:KnowledgeRepo": ("read+write", "node/repo.py:142 the store facade (SQLite + rendered files)"),
    "finesub.llm.knowledge.node.store:KnowledgeStore": ("read+write", "node/store.py SQLite store"),
    "finesub.llm.knowledge.node.schema:connect": ("read+write", "node/schema.py SQLite connection"),
}


#: Knowledge modules that stay with their caller (the correction process) in
#: the recommended design: the knowledge update is an LLM task nested in the
#: correction run -- same RoleClient, rate limiter, token counter, task slot
#: reservation (update.py:728-738) and exchange log -- so its orchestration
#: runs where the correction runs, and only what it reads from or writes to the
#: store crosses to the knowledge package. `entries.py` goes with it: its
#: `select_kb_entries` takes the run's `count_tokens` callable (entries.py:215),
#: which cannot cross a process boundary. Their calls into the store layer are
#: access points like any other caller's.
CLIENT_SIDE_KNOWLEDGE = frozenset({"finesub.llm.knowledge.update", "finesub.llm.knowledge.entries"})


def knowledge_function(node: str) -> bool:
    module = node.split(":", 1)[0]
    if module in CLIENT_SIDE_KNOWLEDGE:
        return False
    return module == KNOWLEDGE_PREFIX or module.startswith(KNOWLEDGE_PREFIX + ".")


def data_reaching(graph: Graph) -> dict[str, set[str]]:
    """Knowledge-code nodes that reach a primitive, staying inside `llm/knowledge/`."""

    reverse: dict[str, set[str]] = defaultdict(set)
    for source, targets in graph.edges.items():
        if not knowledge_function(source):
            continue
        for target in targets:
            if knowledge_function(target) and not target.endswith(":<module>"):
                reverse[target].add(source)
    reaching: dict[str, set[str]] = {}
    for primitive in KNOWLEDGE_PRIMITIVES:
        if primitive.endswith(":<module>"):
            continue
        stack = [primitive]
        while stack:
            node = stack.pop()
            if primitive in reaching.setdefault(node, set()):
                continue
            reaching[node].add(primitive)
            stack.extend(reverse.get(node, ()))
    return reaching


def access_points(graph: Graph, reached_outside: set[str]) -> list[dict]:
    """Calls from outside `llm/knowledge/` into knowledge code that reaches the data."""

    reaching = data_reaching(graph)
    points = []
    for source in sorted(reached_outside):
        if knowledge_function(source):
            continue
        for target, line in graph.calls.get(source, ()):
            if target in reaching and not target.endswith(":<module>"):
                module = source.split(":", 1)[0]
                points.append({
                    "caller": source,
                    "file": graph.modules[module].relative,
                    "line": line,
                    "callee": target,
                    "primitives": sorted(reaching[target]),
                })
    # one row per (caller, callee); keep every line
    merged: dict[tuple[str, str], dict] = {}
    for point in points:
        key = (point["caller"], point["callee"])
        if key in merged:
            merged[key]["lines"].append(point["line"])
        else:
            merged[key] = {**point, "lines": [point.pop("line")]}
            merged[key].pop("line", None)
    for value in merged.values():
        value["lines"] = sorted(set(value["lines"]))
    return list(merged.values())


# ---------------------------------------------------------------------------
# Runtime records


RECORDER = r'''
import json, os, sys, atexit
def _dump():
    names = sorted(n for n in sys.modules if n == "finesub" or n.startswith(("finesub.", "finesub_bootstrap")))
    heavy = sorted(n for n in ("torch", "numpy", "scipy", "librosa", "onnxruntime", "ctranslate2") if n in sys.modules)
    with open(os.environ["NONOKA_P0_RECORD"], "w", encoding="utf-8") as handle:
        json.dump({"modules": names, "heavy": heavy}, handle, indent=1)
atexit.register(_dump)
'''


def record(spec: dict) -> dict:
    python = SYSTEM_PYTHON if spec["python"] == "system" else PBS_PYTHON
    with tempfile.TemporaryDirectory(prefix="p0-record-") as scratch:
        scratch_path = Path(scratch)
        (scratch_path / "sitecustomize.py").write_text(RECORDER, encoding="utf-8")
        output = scratch_path / "record.json"
        env = dict(os.environ)
        # The test process sees every package through the test union (`tests/conftest.py`).
        env["PYTHONPATH"] = os.pathsep.join([str(scratch_path), str(ROOT / "src"), str(ROOT)])
        env["NONOKA_P0_RECORD"] = str(output)
        command = [str(python), "-m", "pytest", "-q", "-p", "no:cacheprovider", *spec["pytest"]]
        completed = subprocess.run(command, cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace")
        tail = completed.stdout.strip().splitlines()[-1:] if completed.stdout.strip() else []
        data = json.loads(output.read_text(encoding="utf-8")) if output.exists() else {"modules": [], "heavy": []}
    return {
        "name": spec["name"], "package": spec["package"], "python": spec["python"],
        "command": ["python", "-m", "pytest", "-q", *spec["pytest"]],
        "returncode": completed.returncode, "summary": tail, **data,
    }


def load_records() -> list[dict]:
    if not RECORD_DIR.is_dir():
        return []
    return [json.loads(path.read_text(encoding="utf-8")) for path in sorted(RECORD_DIR.glob("*.json"))]


# ---------------------------------------------------------------------------
# Lists


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def module_file(graph: Graph, name: str) -> str:
    return graph.modules[name].relative


def data_files() -> list[str]:
    files = []
    for relative in sorted(SOURCES):
        if not relative.endswith(".py"):
            files.append(relative)
    return files


def surface_names() -> list[str]:
    return json.loads(SURFACE.read_text(encoding="utf-8"))


def bootstrap_users(graph: Graph, reached: set[str]) -> dict[str, list[str]]:
    """For each `finesub_bootstrap` name, the reached `finesub` nodes that import it."""

    users: dict[str, set[str]] = defaultdict(set)
    for node in reached:
        if node.startswith("finesub_bootstrap"):
            continue
        for target, line, text in graph.import_sites.get(node, ()):
            if not target.startswith("finesub_bootstrap"):
                continue
            module = graph.modules[node.split(":", 1)[0]]
            parsed = ast.parse(text).body[0]
            if isinstance(parsed, ast.ImportFrom):
                base = resolve_relative(module, parsed.level, parsed.module) if parsed.level else parsed.module
                for alias in parsed.names:
                    full = f"{base}.{alias.name}"
                    key = full if full in graph.modules else f"{base}:{alias.name}"
                    users[key].add(f"{module.relative}:{line}")
            else:
                for alias in parsed.names:
                    users[alias.name].add(f"{module.relative}:{line}")
    return {key: sorted(value) for key, value in sorted(users.items())}


def build(graph: Graph, records: list[dict]) -> dict:
    lock = json.loads(LOCK.read_text(encoding="utf-8"))
    locked = {entry["path"]: entry["sha256"] for entry in lock["files"]}

    starts = {package: [entry_node(graph, spec) for spec, _ in ENTRIES[package]] for package in PACKAGES}
    reached = {package: graph.reach(starts[package], NOT_TAKEN.get(package, ())) for package in ("transcribe", "correct")}

    # Question 10: the access points of the two halves; their callees are the
    # knowledge package's server side.
    outside = reached["transcribe"] | reached["correct"]
    points = access_points(graph, outside)
    reached["knowledge"] = graph.reach(starts["knowledge"])

    carried_cli = [(spec, where) for spec, where in CLI_ENTRIES if spec.split(":", 1)[0] in graph.modules]
    cli = graph.reach([entry_node(graph, spec) for spec, _ in carried_cli])
    not_carried = json.loads(NOT_CARRIED.read_text(encoding="utf-8")) if NOT_CARRIED.is_file() else {"files": []}
    recorded_orphans = {entry["path"]: entry for entry in not_carried["files"]}

    result: dict[str, dict] = {}
    all_data = data_files()
    record_by_package: dict[str, set[str]] = defaultdict(set)
    for item in records:
        if item.get("package"):
            record_by_package[item["package"]].update(item["modules"])

    for package in PACKAGES:
        static_modules = graph.modules_of(reached[package])
        runtime_modules = {name for name in record_by_package.get(package, set()) if name in graph.modules}
        if package == "knowledge":
            # WP-P0's knowledge records ran the client and the server in one test
            # process; the client side (`CLIENT_SIDE_KNOWLEDGE`, which imports the
            # protocol's client) is never imported in the knowledge package's own
            # process, and cannot be: the package has the server, not the client.
            runtime_modules -= CLIENT_SIDE_KNOWLEDGE
        modules = static_modules | runtime_modules
        if package == "transcribe":
            modules |= {name for name in graph.modules if name.split(".", 1)[0] == "finesub_bootstrap"}
        # parent packages
        for name in list(modules):
            modules.update(graph.parents(name))
        files: dict[str, dict] = {}
        compat: list[str] = []
        for name in sorted(modules):
            if package != "transcribe" and name.split(".", 1)[0] == "finesub_bootstrap":
                # Not the original: this package gets a compatibility package
                # with only the names it uses (plan 4.2 item 16).
                compat.append(name)
                continue
            relative = module_file(graph, name)
            why = []
            if name in static_modules:
                why.append("static")
            if name in runtime_modules:
                why.append("runtime")
            if package == "transcribe" and name.startswith("finesub_bootstrap"):
                why.append("bootstrap-whole")
            if not why:
                why.append("parent-package")
            files[relative] = {"module": name, "why": why}
        for prefix, readers in DATA_READERS:
            present = [(reader, where) for reader, where in readers if reader in modules]
            if not present:
                continue
            for data in all_data:
                if data == prefix or (prefix.endswith("/") and data.startswith(prefix)):
                    files[data] = {"read_by": [where for _, where in present], "why": ["data"]}
        if package == "transcribe":
            for data in all_data:
                if data.startswith("finesub_bootstrap/"):
                    files[data] = {"why": ["bootstrap-whole"]}
        for relative, info in files.items():
            if relative in locked:
                info["sha256"] = locked[relative]
                info["locked"] = True
            else:
                info["sha256"] = sha256(SOURCES[relative])
                info["locked"] = False
        result[package] = {
            "files": dict(sorted(files.items())),
            "missed_by_static": sorted(runtime_modules - static_modules),
            "compat_modules": compat,
        }

    covered = set().union(*(set(result[p]["files"]) for p in PACKAGES))
    listed_modules = {
        info["module"] for p in PACKAGES for info in result[p]["files"].values() if "module" in info
    } | {name for p in PACKAGES for name in result[p]["compat_modules"]}
    # Records not tied to one package (a real worker run does both halves):
    # every module they saw must be in some list.
    uncovered_runtime = {
        item["name"]: sorted(set(item["modules"]) - listed_modules)
        for item in records if not item.get("package")
    }
    orphans = sorted(set(locked) - covered)
    cli_modules = graph.modules_of(cli) if carried_cli else set(not_carried.get("cli_only_modules", []))
    orphan_info = []
    for relative in orphans:
        if relative not in SOURCES and relative in recorded_orphans:
            # Not carried since WP-P1: what WP-P0 found about it, from the record.
            entry = recorded_orphans[relative]
            orphan_info.append({key: entry[key] for key in ("path", "module", "reached_from_cli_only", "importers")})
            continue
        name = next((m.name for m in graph.modules.values() if m.relative == relative), None)
        orphan_info.append({
            "path": relative,
            "module": name,
            "reached_from_cli_only": bool(name and name in cli_modules),
            "importers": sorted({
                graph.modules[node.split(":", 1)[0]].relative
                for node, sites in graph.import_sites.items()
                for target, _, _ in sites
                if name and (target == name or target.startswith(name + "."))
            }),
        })
    return {
        "lists": result,
        "starts": starts,
        "reached": reached,
        "points": points,
        "orphans": orphan_info,
        "uncovered_runtime": uncovered_runtime,
        "cli_modules": sorted(cli_modules),
        "locked": locked,
    }


def write(graph: Graph, built: dict, check: bool) -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    outputs: dict[Path, str] = {}
    surface = surface_names()
    for package in PACKAGES:
        info = built["lists"][package]
        reached = built["reached"][package]
        users = bootstrap_users(graph, reached)
        bootstrap_used = sorted(name for name in surface if any(
            key == name or key == name.split(":", 1)[0] for key in users
        ))
        document = {
            "schema": 1,
            "package": package,
            "target": PACKAGE_DIRS[package],
            "source_root": f"{PACKAGE_DIRS[package]}/src",
            "generated_by": "scripts/engine_packages.py",
            "entries": [{"symbol": spec, "used_at": where} for spec, where in ENTRIES[package]],
            "optional": [
                {"symbol": spec, "used_at": where, "present": entry_node(graph, spec).split(":", 1)[0] in {
                    info.get("module") for info in built["lists"][package]["files"].values()
                } | set(built["lists"][package]["compat_modules"])}
                for spec, where in OPTIONAL[package]
            ],
            "counts": {
                "files": len(info["files"]),
                "py": sum(1 for path in info["files"] if path.endswith(".py")),
                "data": sum(1 for path in info["files"] if not path.endswith(".py")),
                "locked": sum(1 for value in info["files"].values() if value["locked"]),
            },
            "files": [{"path": path, **value} for path, value in info["files"].items()],
            "missed_by_static": info["missed_by_static"],
            "not_taken": [{"from": a, "to": b, "why": c} for a, b, c in NOT_TAKEN.get(package, ())],
            "compat_modules": info["compat_modules"],
            "bootstrap_imports": users,
            "bootstrap_surface_used": bootstrap_used,
        }
        outputs[OUT / f"{package}.json"] = json.dumps(document, ensure_ascii=False, indent=1) + "\n"

    coverage = {
        "schema": 1,
        "locked_total": len(built["locked"]),
        "per_package_locked": {p: sum(1 for v in built["lists"][p]["files"].values() if v["locked"]) for p in PACKAGES},
        "in_two_or_more": sorted(
            path for path in built["locked"]
            if sum(path in built["lists"][p]["files"] for p in PACKAGES) >= 2
        ),
        "orphans": built["orphans"],
        "uncovered_runtime": built["uncovered_runtime"],
        "runtime_records": [
            {k: item[k] for k in ("name", "package", "python", "returncode") if k in item} | {"modules": len(item["modules"]), "heavy": item.get("heavy", [])}
            for item in load_records()
        ],
        "cli_only_modules": built["cli_modules"],
    }
    outputs[OUT / "coverage.json"] = json.dumps(coverage, ensure_ascii=False, indent=1) + "\n"
    outputs[OUT / "knowledge-access.json"] = json.dumps({
        "schema": 1,
        "primitives": {k: {"kind": v[0], "where": v[1]} for k, v in KNOWLEDGE_PRIMITIVES.items()},
        "points": built["points"],
    }, ensure_ascii=False, indent=1) + "\n"

    stale = []
    for path, text in outputs.items():
        current = path.read_text(encoding="utf-8") if path.exists() else None
        if current != text:
            stale.append(path)
            if not check:
                path.write_text(text, encoding="utf-8", newline="\n")
    if check and stale:
        print("out of date: " + ", ".join(str(p.relative_to(ROOT)) for p in stale))
        return 1
    for package in PACKAGES:
        info = built["lists"][package]
        print(f"{package}: {len(info['files'])} files, missed_by_static={info['missed_by_static']}")
    print(f"orphans: {len(built['orphans'])}; access points: {len(built['points'])}")
    return 0


IMPORT_CHECK_CHILD = r'''
import importlib, importlib.metadata as md, json, re, sys
spec = json.loads(sys.argv[1])
allowed_finesub = set(spec["modules"])
roots = ["httpx", "pydantic", "opencc-python-reimplemented", "tzdata"]
dists, seen = [], set()
while roots:
    name = roots.pop()
    key = re.sub(r"[-_.]+", "-", name).lower()
    if key in seen:
        continue
    seen.add(key)
    try:
        dist = md.distribution(name)
    except md.PackageNotFoundError:
        continue
    dists.append(dist)
    for req in dist.requires or []:
        if "extra ==" in req:
            continue
        roots.append(re.split(r"[ ;<>=!~\[(]", req, 1)[0])
top = set()
for dist in dists:
    text = dist.read_text("top_level.txt")
    if text:
        top.update(line.strip() for line in text.splitlines() if line.strip())
    for file in dist.files or []:
        part = file.parts[0]
        if part.endswith((".dist-info", ".pth")) or part == "..":
            continue
        top.add(part[:-3] if part.endswith(".py") else part)
allowed = set(sys.stdlib_module_names) | top | set(spec.get("own", ()))
blocked = []
class Gate:
    def find_spec(self, fullname, path=None, target=None):
        head = fullname.split(".", 1)[0]
        if head in ("finesub", "finesub_bootstrap"):
            if fullname not in allowed_finesub:
                blocked.append(fullname)
                raise ModuleNotFoundError(f"not in this package: {fullname}", name=fullname)
            return None
        if head not in allowed:
            blocked.append(fullname)
            raise ModuleNotFoundError(f"not in the launcher environment: {fullname}", name=fullname)
        return None
sys.meta_path.insert(0, Gate())
try:
    # What the package's bridge does first: a compatibility `finesub_bootstrap`
    # takes its data directory from the launch context.
    import tempfile
    from finesub_bootstrap import _launch
    _launch.bind(tempfile.gettempdir())
except ImportError:
    pass
failures = {}
for name in sorted(allowed_finesub):
    try:
        importlib.import_module(name)
    except BaseException as exc:
        failures[name] = f"{type(exc).__name__}: {exc}"
heavy = sorted(m for m in ("torch", "numpy", "scipy", "soundfile", "librosa", "onnxruntime", "ctranslate2") if m in sys.modules)
print(json.dumps({"distributions": sorted(d.metadata["Name"] for d in dists), "failures": failures,
                  "blocked_attempts": sorted(set(blocked)), "heavy_imported": heavy,
                  "imported": len([m for m in sys.modules if m.split(".", 1)[0] in ("finesub", "finesub_bootstrap")])}))
'''


def import_check() -> dict:
    """Question 3: import every module of a package in the launcher's dependency set only.

    Runs PBS 3.12.1 isolated (`-I`), with an import gate in front of every
    finder: only the standard library, httpx, pydantic, opencc (and what
    their distributions require), and the package's own modules (for
    `finesub_bootstrap`, the compatibility package's module list) can be
    imported. Every listed module is then imported in turn.
    """

    results = {}
    for package in ("correct", "knowledge"):
        listing = json.loads((OUT / f"{package}.json").read_text(encoding="utf-8"))
        modules = [entry["module"] for entry in listing["files"] if "module" in entry] + listing["compat_modules"]
        # Since WP-P1 against the package itself: its own `src/` (with its
        # compatibility `finesub_bootstrap`) is all that is on the path.
        source = ROOT / PACKAGE_DIRS[package] / "src"
        adapter = ROOT / PACKAGE_DIRS[package] / "adapter"
        compat = {path.stem for path in (source / "finesub_bootstrap").glob("*.py") if path.stem != "__init__"}
        modules = [name for name in modules
                   if not name.startswith("finesub_bootstrap.") or name.split(".", 1)[1] in compat]
        # The compatibility package's own binding of the launch context.
        modules += sorted(f"finesub_bootstrap.{name}" for name in compat if name.startswith("_"))
        # The package's own code (the knowledge protocol's two ends, its adapter) is on its path too.
        own = sorted(path.name for path in adapter.iterdir() if (path / "__init__.py").is_file())
        env = {key: value for key, value in os.environ.items() if key.upper() in {"SYSTEMROOT", "WINDIR", "TEMP", "TMP", "PATH"}}
        completed = subprocess.run(
            [str(PBS_PYTHON), "-I", "-B", "-c",
             f"import sys; sys.path[:0] = [{str(adapter)!r}, {str(source)!r}]; exec({IMPORT_CHECK_CHILD!r})",
             json.dumps({"modules": modules, "own": own})],
            capture_output=True, text=True, encoding="utf-8", errors="replace", env=env, cwd=tempfile.gettempdir(),
        )
        if completed.returncode:
            raise SystemExit(completed.stderr[-2000:])
        results[package] = {"modules": len(modules), **json.loads(completed.stdout.strip().splitlines()[-1])}
    return {"schema": 1, "python": "pbs-3.12.1 -I -B", "results": results}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--record", action="store_true")
    parser.add_argument("--import-check", action="store_true", help="question 3; writes import-check.json")
    args = parser.parse_args(argv)
    if args.import_check:
        result = import_check()
        (OUT / "import-check.json").write_text(json.dumps(result, ensure_ascii=False, indent=1) + "\n", encoding="utf-8", newline="\n")
        for package, value in result["results"].items():
            print(package, value["modules"], "failures:", value["failures"], "heavy:", value["heavy_imported"], "blocked:", value["blocked_attempts"])
        return 0
    if args.record:
        RECORD_DIR.mkdir(parents=True, exist_ok=True)
        for spec in RECORDS:
            item = record(spec)
            (RECORD_DIR / f"{spec['name']}.json").write_text(
                json.dumps(item, ensure_ascii=False, indent=1) + "\n", encoding="utf-8", newline="\n"
            )
            print(f"recorded {spec['name']}: {len(item['modules'])} modules, heavy={item['heavy']}, rc={item['returncode']}")
    graph = Graph(discover())
    built = build(graph, load_records())
    return write(graph, built, args.check)


if __name__ == "__main__":
    sys.exit(main())
