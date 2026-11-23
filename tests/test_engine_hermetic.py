"""The engine packages' processes are hermetic (plan 4.5 items 27, 29-32; WP-H's three proofs).

1. **Coverage scan.** Every environment variable the packages' code reads by
   name -- FineSub's own, the transcription package's original
   `finesub_bootstrap`, the correction and knowledge packages' compatibility
   ones, the packages' adapters -- is in the classification table: the
   neutral half (`nonoka_x.engine_host.CLASSIFICATION`) or the package's half
   (`CLASSIFICATION` in each package's bridge). The few reads whose name the
   code computes are listed here, each with where its names come from.
2. **Poisoning.** The parent's environment full of garbage for every variable
   the table knows that is not the system's, fake configuration, key and
   checkout files in the working directory, beside the task and in the
   (fake) user profile, a `sitecustomize` on `PYTHONPATH` and a
   `usercustomize` in the user site: the call-equivalence scenarios still
   match their baselines, the packages' own environment is what it is from a
   clean parent, and no package process ran either customization file.
3. **Read audit.** Every name a package process reads through `os.environ`
   while the call-equivalence scenarios run (the whole pipeline, its
   algorithms stood in for) is classified for that process; what it found
   there was handed to it by the engine host or written by its bridge or
   itself, never anything else.

The sandbox's write check (the fourth proof) needs the real runtime: it is
`scripts/quality_gate.py --real`'s `sandbox` check. The table itself is
checked here too: three categories, one per name and process, the bridge's
copies equal.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

from nonoka_x import engine_host

ROOT = Path(__file__).resolve().parents[1]
ENGINES = ROOT / "engines"
PACKAGES = {
    "finesub-transcribe": ("finesub_transcribe", "transcription"),
    "finesub-correct": ("finesub_correct", "correction"),
    "finesub-knowledge": ("finesub_knowledge_host", "knowledge"),
    "flowocr": ("flowocr_host", "ocr"),
}
ENTRY_KINDS = {entry: kind for entry, kind in PACKAGES.values()}
CATEGORIES = {"system", "nonoka", "absent"}


def load_bridge(path: Path | None = None, name: str = "wp_h_bridge") -> Any:
    """The FineSub packages' bridge (the three copies are the same bytes, `tests/test_engine_packages.py`)."""

    path = path or ENGINES / "finesub-transcribe" / "adapter" / "finesub_transcribe" / "bridge.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


BRIDGE = load_bridge()
#: The on-screen text package's own bridge: its half of the table, for the `ocr` process.
OCR_BRIDGE = load_bridge(ENGINES / "flowocr" / "adapter" / "flowocr_host" / "bridge.py", "wp_h_ocr_bridge")
BRIDGES = (BRIDGE, OCR_BRIDGE)


def _key(name: str) -> str:
    return name.upper() if os.name == "nt" else name


def classified(kind: str) -> dict[str, str]:
    """Name -> category for one process kind on this platform: the neutral half, then the package's."""

    table = {_key(name): row.category for name, row in engine_host.classification_for(kind).items()}
    for bridge in BRIDGES:
        for name, (category, _reason) in bridge.classification_for(kind).items():
            table.setdefault(_key(name), category)
    return table


def every_classified_name() -> set[str]:
    names = {_key(row.name) for row in engine_host.CLASSIFICATION}
    return names | {_key(row[0]) for bridge in BRIDGES for row in bridge.CLASSIFICATION}


# --- the table -------------------------------------------------------------------------------------------------


class TableTests(unittest.TestCase):
    def test_every_row_has_a_category_a_process_and_a_reason(self) -> None:
        for row in engine_host.CLASSIFICATION:
            self.assertIn(row.category, CATEGORIES, row)
            self.assertTrue(row.processes and row.processes <= engine_host.ALL_PROCESSES, row)
            self.assertTrue(row.reason.strip(), row)
        for name, category, processes, reason in (row for bridge in BRIDGES for row in bridge.CLASSIFICATION):
            self.assertIn(category, CATEGORIES - {"system"}, name)
            self.assertTrue(processes and processes <= engine_host.ALL_PROCESSES, name)
            self.assertTrue(reason.strip(), name)

    def test_a_name_has_one_category_per_process(self) -> None:
        for platform in ("win32", "darwin"):
            for kind in engine_host.ALL_PROCESSES:
                seen: dict[str, str] = {}
                for row in engine_host.CLASSIFICATION:
                    if row.applies(kind, platform):
                        previous = seen.setdefault(row.name, row.category)
                        self.assertEqual(previous, row.category, (platform, kind, row.name))
                bridge_seen: dict[str, str] = {}
                for name, category, processes, _reason in (row for bridge in BRIDGES for row in bridge.CLASSIFICATION):
                    if kind in processes:
                        previous = bridge_seen.setdefault(name, category)
                        self.assertEqual(previous, category, (platform, kind, name))
                        # The two halves never disagree either.
                        self.assertEqual(seen.get(name, category), category, (platform, kind, name))

    def test_no_wildcards_among_the_system_names(self) -> None:
        for row in engine_host.CLASSIFICATION:
            if row.category == "system":
                self.assertNotIn("*", row.name)

    def test_the_package_keys_are_the_manifests(self) -> None:
        manifests = {
            package: json.loads((ENGINES / package / "PACKAGE.json").read_text(encoding="utf-8"))
            for package in PACKAGES
        }
        self.assertEqual(tuple(manifests["finesub-transcribe"]["secrets"]), BRIDGE.TRANSCRIPTION_SECRETS)
        self.assertEqual(tuple(manifests["finesub-correct"]["secrets"]), BRIDGE.CORRECTION_SECRETS)
        self.assertEqual(manifests["finesub-knowledge"]["secrets"], [])
        self.assertEqual(manifests["flowocr"]["secrets"], [])


# --- 1. the coverage scan ------------------------------------------------------------------------------------------


#: Reads whose name the code computes, by `<package path>:<function>`, and where their names come from.
#: A new one fails the scan until it is reviewed and listed.
COMPUTED_READS = {
    "src/finesub/api_keys.py:_raw_key_entries":
        "a key pool's variable (`POOL_SPECS`: GEMINI_FREE, GEMINI_PAID, EXA_KEYS, TAVILY_KEYS, MOSS_PRO)",
    "src/finesub/api_keys.py:key_entries":
        "a provider tier's or a catalog row's `key_env`: the packaged tiers and the keys the derived catalog "
        "names (CORRECTION_SECRETS)",
    "src/finesub_bootstrap/download_sources.py:mirror_for":
        "`download_routes.OVERRIDES` (FINESUB_PYPI_INDEX, FINESUB_HF_ENDPOINT, FINESUB_GITHUB_FILE_PROXY)",
}


def _strings(node: ast.AST, names: dict[str, list[str]]) -> list[str] | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        found: list[str] = []
        for element in node.elts:
            values = _strings(element, names)
            if values is None:
                return None
            found += values
        return found
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in ("frozenset", "set", "tuple"):
        return _strings(node.args[0], names) if node.args else []
    if isinstance(node, ast.Dict):
        return _strings(ast.Tuple(elts=[value for value in node.values if value is not None]), names)
    if isinstance(node, ast.Name) and node.id in names:
        return names[node.id]
    if isinstance(node, ast.Attribute) and node.attr in names:
        return names[node.attr]
    return None


def _is_environ(node: ast.AST) -> bool:
    if isinstance(node, ast.Attribute):
        return node.attr == "environ" and isinstance(node.value, ast.Name) and node.value.id == "os"
    return isinstance(node, ast.Name) and node.id == "environ"


def _read_argument(node: ast.AST) -> ast.AST | None:
    """The name argument of an environment read, or None when `node` is not one."""

    if isinstance(node, ast.Call):
        function = node.func
        if isinstance(function, ast.Attribute) and function.attr in ("get", "setdefault", "pop") \
                and _is_environ(function.value) and node.args:
            return node.args[0]
        if isinstance(function, ast.Attribute) and function.attr == "getenv" and node.args:
            return node.args[0]
        if isinstance(function, ast.Name) and function.id == "getenv" and node.args:
            return node.args[0]
    if isinstance(node, ast.Subscript) and _is_environ(node.value) and isinstance(node.ctx, ast.Load):
        return node.slice
    if isinstance(node, ast.Compare) and len(node.ops) == 1 and isinstance(node.ops[0], (ast.In, ast.NotIn)) \
            and _is_environ(node.comparators[0]):
        return node.left
    return None


def _constants(tree: ast.AST) -> dict[str, list[str]]:
    """Module and class level names bound to strings (or collections of them)."""

    found: dict[str, list[str]] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef)):
            for statement in node.body:
                targets = statement.targets if isinstance(statement, ast.Assign) else \
                    [statement.target] if isinstance(statement, ast.AnnAssign) and statement.value is not None else []
                for target in targets:
                    if isinstance(target, ast.Name):
                        values = _strings(statement.value, found)
                        if values is not None:
                            found.setdefault(target.id, values)
    return found


def scan_package(package_dir: Path, shared: dict[str, list[str]]) -> tuple[dict[str, set[str]], list[str]]:
    """(name -> where it is read, computed read sites as `<path>:<function>`) for one package."""

    names: dict[str, set[str]] = {}
    computed: list[str] = []
    for path in sorted(package_dir.rglob("*.py")):
        relative = path.relative_to(package_dir).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        module = {**shared, **_constants(tree)}

        def own_nodes(scope: ast.AST) -> list[ast.AST]:
            """The nodes of `scope`, not descending into the functions defined in it."""

            found, pending = [], list(ast.iter_child_nodes(scope))
            while pending:
                node = pending.pop()
                found.append(node)
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                    pending.extend(ast.iter_child_nodes(node))
            return found

        def visit(scope: ast.AST, function: str, bound: dict[str, list[str]]) -> None:
            nodes = own_nodes(scope)
            loops: dict[str, list[str]] = {}
            for node in nodes:
                if isinstance(node, (ast.For, ast.comprehension)) and isinstance(node.target, ast.Name):
                    values = _strings(node.iter, bound)
                    if values is not None:
                        loops[node.target.id] = loops.get(node.target.id, []) + values
            local = {**bound, **loops}
            for node in nodes:
                argument = _read_argument(node)
                if argument is None:
                    continue
                values = _strings(argument, local)
                if values is None:
                    computed.append(f"{relative}:{function}")
                    continue
                for value in values:
                    names.setdefault(value, set()).add(f"{relative}:{getattr(node, 'lineno', 0)}")

        visit(tree, "<module>", module)
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                visit(node, node.name, module)
    return names, sorted(set(computed))


def scan_all() -> tuple[dict[str, dict[str, set[str]]], dict[str, list[str]]]:
    shared: dict[str, list[str]] = {}
    for path in ENGINES.rglob("*.py"):
        for name, values in _constants(ast.parse(path.read_text(encoding="utf-8"))).items():
            shared.setdefault(name, values)
    reads: dict[str, dict[str, set[str]]] = {}
    computed: dict[str, list[str]] = {}
    for package in PACKAGES:
        reads[package], computed[package] = scan_package(ENGINES / package, shared)
    return reads, computed


class CoverageScanTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.reads, cls.computed = scan_all()

    def test_the_scan_sees_the_reads_it_must(self) -> None:
        # The scan itself: names known to be read in each package.
        self.assertIn("FINESUB_SEPARATOR_ACCEL", self.reads["finesub-transcribe"])
        self.assertIn("HF_HUB_CACHE", self.reads["finesub-transcribe"])
        self.assertIn("FINESUB_TASK_STRATEGY", self.reads["finesub-correct"])
        self.assertIn("FINESUB_KNOWLEDGE_ROOT", self.reads["finesub-knowledge"])
        self.assertIn("GEMINI_TOKEN_COUNTER_EXE", self.reads["finesub-correct"])

    def test_every_name_read_is_classified_for_its_process(self) -> None:
        unclassified: dict[str, list[str]] = {}
        for package, (_entry, kind) in PACKAGES.items():
            table = classified(kind)
            for name, where in self.reads[package].items():
                if _key(name) not in table:
                    unclassified.setdefault(f"{package} ({kind})", []).append(f"{name} @ {sorted(where)[0]}")
        self.assertEqual(unclassified, {}, "classify these in engine_host or the bridge")

    def test_every_computed_read_is_reviewed(self) -> None:
        found = {site for sites in self.computed.values() for site in sites}
        self.assertEqual(found - set(COMPUTED_READS), set(), "a new computed environment read: review and list it")
        self.assertEqual(set(COMPUTED_READS) - found, set(), "a reviewed computed read is gone: drop it here")

    def test_the_key_pools_and_tiers_a_computed_read_can_name_are_classified(self) -> None:
        sys.path[:0] = [str(ENGINES / "finesub-correct" / "src")]
        try:
            text = (ENGINES / "finesub-correct" / "src" / "finesub" / "api_keys.py").read_text(encoding="utf-8")
        finally:
            del sys.path[0]
        pools = {node.args[0].value for node in ast.walk(ast.parse(text))
                 if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "PoolSpec"}
        self.assertEqual(pools, {"GEMINI_FREE", "GEMINI_PAID", "EXA_KEYS", "TAVILY_KEYS", "MOSS_PRO"})
        table = classified("correction")
        for name in pools:
            self.assertIn(name, table)
        # A row of the derived catalog names its key by `key_env`: one of the correction's keys.
        self.assertEqual(
            {row["key_env"] for row in _derived_key_envs()} - set(BRIDGE.CORRECTION_SECRETS), set(),
        )


def _derived_key_envs() -> list[dict[str, str]]:
    """The `key_env` of every catalog row the core can derive (`settings` writes them for the bridge)."""

    from nonoka_x import settings

    rows = [{"key_env": settings.CLOUD_KEY_NAME}]
    for provider in settings._HTTP_TRANSPORTS:
        if provider != settings.CLOUD_PROVIDER_ID:
            rows.append({"key_env": settings.API_PROVIDER_BY_ID[provider]["keyName"]})
    assert len(rows) > 1, "the core derives catalog rows for its HTTP providers"
    return rows


# --- the environment a process is handed ------------------------------------------------------------------------


class HandedEnvironmentTests(unittest.TestCase):
    """What `package_environment` builds from a parent full of garbage: only classified names, none of the garbage."""

    def setUp(self) -> None:
        self.temp = Path(tempfile.mkdtemp(prefix="nonoka-hermetic-"))
        self.addCleanup(shutil.rmtree, self.temp, True)
        self.packages = engine_host.discover(ENGINES)

    def poisoned_parent(self) -> dict[str, str]:
        system = {_key(row.name) for row in engine_host.CLASSIFICATION if row.category == "system"}
        parent = {name: value for name, value in os.environ.items() if _key(name) in system}
        for name in every_classified_name() | {_key(name) for name in os.environ}:
            if name not in system:
                parent[name] = "nonoka-poison"
        parent["NOT_IN_ANY_TABLE"] = "nonoka-poison"
        return parent

    def test_only_system_and_nonoka_names_none_of_the_poison(self) -> None:
        parent = self.poisoned_parent()
        for package_id, (_entry, kind) in PACKAGES.items():
            package = self.packages[package_id]
            context = {
                "paths": {"package_data": str(self.temp / package_id), "run": str(self.temp / package_id / "run")},
                "proxy": {"policy": True, "url": "http://127.0.0.1:7890"},
                "runtime": {"tools": [str(self.temp / "tools" / "ffmpeg")]},
                "agent_cli": {"path": [str(self.temp / "agents")]},
            }
            sandbox = (engine_host.SandboxTemp(engine_host.sandbox_root(context))
                       if kind in ("transcription", "ocr") else None)
            try:
                environment = engine_host.package_environment(package, context, parent, sandbox=sandbox)
            finally:
                if sandbox is not None:
                    sandbox.close()
            neutral = engine_host.classification_for(kind)
            with self.subTest(package=package_id):
                for name, value in environment.items():
                    row = neutral.get(name) or next(
                        (candidate for key, candidate in neutral.items() if _key(key) == _key(name)), None
                    )
                    self.assertIsNotNone(row, name)
                    self.assertIn(row.category, ("system", "nonoka"), name)
                    self.assertNotIn("nonoka-poison", value, name)
                self.assertNotIn("NOT_IN_ANY_TABLE", environment)
                self.assertEqual(environment["NO_PROXY"], engine_host.LOCAL_HOSTS)
                self.assertEqual(environment["HTTPS_PROXY"], "http://127.0.0.1:7890")
                self.assertEqual(environment["PYTHONNOUSERSITE"], "1")
                path = environment["PATH"].split(os.pathsep)
                self.assertEqual(path[0] if kind != "correction" else path[1], str(self.temp / "tools" / "ffmpeg"))
                self.assertEqual(str(self.temp / "agents") in path, kind == "correction")

    def test_direct_is_spelled_out(self) -> None:
        package = self.packages["finesub-correct"]
        for proxy in ({"policy": False, "url": "http://127.0.0.1:7890"}, {"policy": True, "url": None}, None):
            environment = engine_host.package_environment(package, {"proxy": proxy}, {})
            self.assertEqual((environment["NO_PROXY"], environment["no_proxy"]), ("*", "*"))
            self.assertNotIn("HTTP_PROXY", environment)

    def test_a_system_variable_is_copied_as_it_is(self) -> None:
        package = self.packages["finesub-correct"]
        environment = engine_host.package_environment(package, {}, {"SystemRoot": r"C:\Windows", "USERNAME": "u"})
        if os.name == "nt":
            self.assertEqual(environment["SystemRoot"], r"C:\Windows")
            self.assertEqual(environment["USERNAME"], "u")
            self.assertTrue(environment["PATH"].endswith(r"C:\Windows\System32\OpenSSH"))

    def test_agy_adds_launcher_python_to_agent_cli_path(self) -> None:
        from unittest import mock

        from nonoka_x import engine_contexts, settings

        agy_dir = self.temp / "agy-bin"
        agy_dir.mkdir()
        agy_exe = agy_dir / ("agy.exe" if os.name == "nt" else "agy")
        agy_exe.write_bytes(b"")
        launcher_dir = self.temp / "launcher"
        launcher_dir.mkdir()
        launcher_python = launcher_dir / ("python.exe" if os.name == "nt" else "python")
        launcher_python.write_bytes(b"")

        def detect(provider: str) -> Path | None:
            return agy_exe if provider == "local-agy" else None

        with (
            mock.patch.object(settings, "local_agent_executable", side_effect=detect),
            mock.patch.object(sys, "executable", str(launcher_python)),
        ):
            dirs = engine_contexts.agent_cli_directories()
        self.assertIn(str(agy_dir), dirs)
        self.assertIn(str(launcher_dir), dirs)

        codex_dir = self.temp / "codex-bin"
        codex_dir.mkdir()
        codex_exe = codex_dir / "codex.exe"
        codex_exe.write_bytes(b"")

        def detect_codex_only(provider: str) -> Path | None:
            return codex_exe if provider == "local-codex" else None

        with (
            mock.patch.object(settings, "local_agent_executable", side_effect=detect_codex_only),
            mock.patch.object(sys, "executable", str(launcher_python)),
        ):
            dirs_without_agy = engine_contexts.agent_cli_directories()
        self.assertEqual(dirs_without_agy, [str(codex_dir)])


# --- 2. poisoning ----------------------------------------------------------------------------------------------------

#: Left out of the poison, each for its reason:
NOT_POISONED = {
    # The core's own interpreter (the gate's child is the core) would not start, or would hang, on these.
    "PYTHONHOME", "PYTHONPATH", "PYTHONIOENCODING", "PYTHONINSPECT", "PYTHONSAFEPATH", "PYTHONOPTIMIZE",
    "PYTHONVERBOSE", "PYTHONDEBUG", "PYTHONHASHSEED", "PYTHONMALLOC", "PYTHONINTMAXSTRDIGITS",
    "PYTHONLEGACYWINDOWSSTDIO", "PYTHONEXECUTABLE", "PYTHONDEVMODE", "PYTHONWARNINGS",
    "PYTHONWARNDEFAULTENCODING", "PYTHONTRACEMALLOC", "PYTHONFAULTHANDLER", "PYTHONUSERBASE", "PYTHONNOUSERSITE",
    "PYTHONPLATLIBDIR", "_PYTHON_PROJECT_BASE", "_PYTHON_HOST_PLATFORM", "_PYTHON_SYSCONFIGDATA_NAME", "PYTHONTZPATH",
    "PATH",
    # The proxy is Nonoka's to read and decide (D17, plan 4.5 item 30): the core's policy reads the
    # environment's and hands its decision on; that it is handed, and only that, is tested above.
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
}


def _poison(root: Path, environment: dict[str, str], markers: Path | None = None) -> str:
    """Garbage for every known variable that is not the system's; fake files where FineSub would look.

    Returns the working directory the core starts in: a fake checkout. The
    customization files leave a marker per process that ran them in `markers`
    (default `<root>/markers`).
    """

    system = {_key(row.name) for row in engine_host.CLASSIFICATION if row.category == "system"}
    fake = root / "poison"
    for name in sorted(every_classified_name()):
        if name in system or name in {_key(other) for other in NOT_POISONED}:
            continue
        environment[name] = str(fake / name.lower()) if name.endswith(("_FILE", "_DIR", "_HOME", "_ROOT", "_CACHE")) \
            else "1" if name.endswith(("_ACCEL", "_OFFLINE")) else "nonoka-poison"
    environment["FINESUB_SEPARATOR_ACCEL"] = "1"
    environment["FINESUB_REFEREE_ACCEL"] = "1"
    environment["FINESUB_CHECKOUT_DATA"] = "1"
    environment["HF_HUB_OFFLINE"] = "0"
    environment["CUDA_VISIBLE_DEVICES"] = "7"
    environment["FINESUB_ROOT"] = str(fake / "checkout")
    environment["PYTHONUTF8"] = "1"
    environment["PYTHONSTARTUP"] = str(fake / "startup.py")
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    # A sitecustomize on PYTHONPATH and a usercustomize in the user site: the core runs them (the
    # control), a package process must not.
    markers = markers or root / "markers"
    markers.mkdir(parents=True, exist_ok=True)
    customize = (
        "import json, os, sys\n"
        f"open(os.path.join({str(markers)!r}, f'{{os.getpid()}}-' + __name__ + '.json'), 'w').write("
        "json.dumps({'isolated': sys.flags.isolated, 'argv': sys.argv}))\n"
    )
    site = fake / "site"
    site.mkdir(parents=True, exist_ok=True)
    (site / "sitecustomize.py").write_text(customize, encoding="utf-8")
    # The repository root too: the core starts in the fake checkout, not there.
    environment["PYTHONPATH"] = os.pathsep.join([environment["PYTHONPATH"], str(ROOT), str(site)])
    environment.pop("PYTHONNOUSERSITE", None)
    version = f"Python{sys.version_info.major}{sys.version_info.minor}"
    user_site = Path(environment["APPDATA"]) / "Python" / version / "site-packages"
    user_site.mkdir(parents=True, exist_ok=True)
    (user_site / "usercustomize.py").write_text(customize, encoding="utf-8")
    # FineSub's fall-back locations, in the (fake) profile, beside the task and in the working directory.
    bad_config = '[nonoka_models]\ndefault_provider = "poison"\n[llm]\ntemperature = 9\n[segmentation]\nmax = 1\n'
    bad_env = "GEMINI_PAID=poison-key\nOPENAI_COMPAT_API_KEY=poison-key\nGEMINI_BASE_URL=http://127.0.0.1:9\n"
    for directory in (Path(environment["LOCALAPPDATA"]) / "FineSub", Path(environment["USERPROFILE"]) / ".finesub",
                      Path(environment["USERPROFILE"]) / ".cache" / "finesub", root / "data" / "tasks",
                      fake / "checkout", root / "cwd"):
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "pyproject.toml").write_text('[project]\nname = "finesub"\n', encoding="utf-8")
        (directory / "src" / "finesub").mkdir(parents=True, exist_ok=True)
        (directory / "src" / "finesub" / "__init__.py").write_text("raise SystemExit('poison checkout')\n",
                                                                     encoding="utf-8")
        for name in ("config.toml", "finesub.toml"):
            (directory / name).write_text(bad_config, encoding="utf-8")
        (directory / ".env").write_text(bad_env, encoding="utf-8")
        (directory / "model_catalog.toml").write_text("[models.poison]\nprovider_tier = 'X'\n", encoding="utf-8")
        (directory / ".state").write_text("{poison", encoding="utf-8")
    hf = Path(environment["USERPROFILE"]) / ".cache" / "huggingface"
    hf.mkdir(parents=True, exist_ok=True)
    (hf / "token").write_text("hf_poison", encoding="utf-8")
    return str(root / "cwd")


def _markers(directory: Path) -> list[dict[str, Any]]:
    return [json.loads(path.read_text(encoding="utf-8")) for path in directory.glob("*.json")]


class PoisoningTests(unittest.TestCase):
    def test_the_call_scenarios_match_their_baselines_in_a_poisoned_parent(self) -> None:
        from tests.quality_harness import calls

        scratch = Path(tempfile.mkdtemp(prefix="nonoka-poison-"))
        self.addCleanup(shutil.rmtree, scratch, True)
        directory = scratch / "markers"

        def prepare(root: Path, environment: dict[str, str]) -> str:
            return _poison(root, environment, directory)

        records = calls.run_all(prepare=prepare)
        self.assertEqual(calls.differences(records), {})
        markers = _markers(directory)
        self.assertTrue(markers, "the control: the core itself ran the customization files")
        self.assertEqual([marker for marker in markers if marker["isolated"]], [],
                         "a package process ran a customization file")

    def test_the_packages_environment_is_the_clean_parent_s(self) -> None:
        from tests.quality_harness import calls, environment

        scratch = Path(tempfile.mkdtemp(prefix="nonoka-poison-env-"))
        self.addCleanup(shutil.rmtree, scratch, True)
        for profile in calls.SETTINGS_PROFILES:
            clean = environment.compared({profile: environment.run_profile(profile, scratch / "clean")})
            poisoned = environment.compared({profile: environment.run_profile(profile, scratch / "poisoned",
                                                                                prepare=_poison)})
            self.assertEqual(poisoned, clean, profile)


# --- 3. the read audit --------------------------------------------------------------------------------------------------


class ReadAuditTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from tests.quality_harness import calls

        cls.scratch = Path(tempfile.mkdtemp(prefix="nonoka-audit-"))
        audit = cls.scratch / "audit"
        cls.records = calls.run_all(audit=audit)
        cls.audits = [json.loads(path.read_text(encoding="utf-8")) for path in sorted(audit.glob("*.json"))]

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.scratch, ignore_errors=True)

    def test_the_audited_runs_match_their_baselines(self) -> None:
        from tests.quality_harness import calls

        self.assertEqual(calls.differences(self.records), {})
        kinds = {ENTRY_KINDS[audit["entry"]] for audit in self.audits}
        self.assertEqual(kinds, {"transcription", "correction", "knowledge"})

    def test_every_name_read_is_classified_for_the_process(self) -> None:
        unclassified: dict[str, set[str]] = {}
        for audit in self.audits:
            kind = ENTRY_KINDS[audit["entry"]]
            table = classified(kind)
            for name in audit["reads"]:
                if name not in table:
                    unclassified.setdefault(kind, set()).add(name)
        self.assertEqual(unclassified, {})

    def test_what_a_process_found_was_nonoka_s(self) -> None:
        for audit in self.audits:
            kind = ENTRY_KINDS[audit["entry"]]
            table = classified(kind)
            # Everything handed in is the system's or Nonoka's.
            handed = {name for name in audit["handed"] if table.get(name) not in ("system", "nonoka")}
            self.assertEqual(handed - {"PYTHONPATH"}, set(), (kind, "handed"))
            for name, read in audit["reads"].items():
                if not read["present"]:
                    continue
                with self.subTest(kind=kind, name=name):
                    self.assertIn(read["source"], ("handed", "bridge", "engine"))
                    if read["source"] in ("handed", "bridge"):
                        self.assertIn(table.get(name), ("system", "nonoka"))


if __name__ == "__main__":
    unittest.main()
