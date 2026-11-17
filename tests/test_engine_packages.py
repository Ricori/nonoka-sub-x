"""The three FineSub engine packages as WP-P1 left them (plan 4.6 items 33-35a, WP-P1's completion criteria).

- Nothing in `engines/` is installable: no `pyproject.toml` (or `setup.py`).
- Each package's `src/` is exactly its list in `docs/engine-packages/<package>.json`,
  file for file and byte for byte; every file of the vendored tree's lock is
  in a package or in `not-carried.json` with the reason.
- A file two packages carry -- FineSub's, or the packages' own shared modules
  (`bridge`, `wire`, `failures`, the knowledge protocol's client) -- is the same
  bytes in each.
- A package's code imports neither the core nor another package, and in its
  own process cannot: only its own roots are on its path.
- The correction and knowledge packages' `finesub_bootstrap` is a
  compatibility package: exactly the names their FineSub code imports
  (`__all__`), no more public names than those need, no torch, numpy or
  `nonoka_x` on its import chain, every import of it in the package
  resolving, its locations from the launch context.
- Each package can be deleted: without the transcription package FineSub's
  correction still runs and passes its gate; without the correction package
  FineSub's transcription still runs, passes its gate and its task gets a raw
  SRT the core renders; without both, a cloud API engine still reaches the
  raw SRT. FineSub's own files of a task stay in the task's private
  directories (`native/<package>/`).
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENGINES = ROOT / "engines"
LISTS = ROOT / "docs" / "engine-packages"
PACKAGES = {"transcribe": "finesub-transcribe", "correct": "finesub-correct", "knowledge": "finesub-knowledge"}
#: Each package's own top-level packages (`adapter/`).
ADAPTERS = {
    "finesub-transcribe": {"finesub_transcribe", "finesub_knowledge"},
    "finesub-correct": {"finesub_correct", "finesub_knowledge"},
    "finesub-knowledge": {"finesub_knowledge_host", "finesub_knowledge"},
}
COMPAT = ("finesub-correct", "finesub-knowledge")

#: Public names a compatibility module keeps beyond its `__all__`: what the
#: copied definitions need, under the original's names (owner 2026-11-17: an
#: exception class renamed would change the type a failure message names).
COMPAT_HELPERS = {
    "finesub-correct": {
        "fsops": {"PUBLISH_REPLACE", "ReplaceBudget"},           # replace_path's retry budget
        "http_client": {"TunnelWatch"},                           # the hook watch_tunnel installs
        "locks": {"ACTIVITY_GATE_NAME", "ACTIVITY_LEASE_DIRECTORY", "LogCallback", "POLL_SECONDS",
                  "PauseCheck", "activity_gate_path", "activity_lease_path", "lease_path"},  # holding_activity's files
        "logs": {"DEFAULT_KEEP", "LOG_DIR_NAME"},                 # prune's budget, log_directory's name
        "paths": {"AppPaths"},                                    # what load_app_paths returns
        "processes": {"which"},                                   # token_counter.find_on_path's lookup
        "secrets": {"DpapiBackend", "KEK_PREFIX", "KEYRING_NAME", "KekBackend", "OBF_PREFIX",
                    "ObfuscationBackend", "ProtectionUnavailable", "SecretUnreadable", "VALUE_PREFIX",
                    "account_bound", "is_protected", "new_master", "open_", "protect_env_file",
                    "protection_enabled", "seal", "unwrap_master", "wrap_master"},  # the .env protection
        "token_counter": {"EXECUTABLE_NAME", "TOKEN_COUNTER_VARIABLE"},
    },
    "finesub-knowledge": {
        "http_client": {"TunnelWatch"},
        "locks": {"LogCallback", "POLL_SECONDS", "PauseCheck", "lease_path"},  # holding_lock's sidecar
        "logs": {"DEFAULT_KEEP"},
        "paths": {"AppPaths"},
        "processes": {"which"},
        "token_counter": {"EXECUTABLE_NAME", "TOKEN_COUNTER_VARIABLE"},
    },
}


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _files(root: Path) -> dict[str, Path]:
    return {
        path.relative_to(root).as_posix(): path for path in root.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts and path.suffix not in {".pyc", ".pyo"}
    }


def _listing(key: str) -> dict:
    return json.loads((LISTS / f"{key}.json").read_text(encoding="utf-8"))


def _expected_compat(key: str) -> dict[str, set[str]]:
    """Compatibility module -> the names this package's FineSub code imports of it (`bootstrap_imports`)."""

    expected: dict[str, set[str]] = {}
    for name in _listing(key)["bootstrap_imports"]:
        module, _, attribute = name.partition(":")
        parts = module.split(".")
        if not attribute and len(parts) == 2:
            # A module imported by name: what the code reads off it is listed separately.
            expected.setdefault(parts[1], set())
            continue
        expected.setdefault(parts[1], set()).add(attribute)
    surface = json.loads((ROOT / "tests/fixtures/quality/bootstrap-surface.json").read_text(encoding="utf-8"))
    for name in surface:
        module, _, attribute = name.partition(":")
        if module.split(".")[-1] in expected and attribute and module.count(".") == 1:
            if name in _listing(key)["bootstrap_surface_used"]:
                expected[module.split(".")[-1]].add(attribute)
    return expected


def _public_definitions(path: Path) -> set[str]:
    names = set()
    for node in ast.parse(path.read_text(encoding="utf-8")).body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            names.update(target.id for target in node.targets if isinstance(target, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return {name for name in names if not name.startswith("_")}


def _imported_tops(path: Path) -> set[str]:
    found = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.add(node.module.split(".")[0])
    return found


def _manifest(package: str) -> dict:
    return json.loads((ENGINES / package / "PACKAGE.json").read_text(encoding="utf-8"))


def _package_python(package: str, code: str, *arguments: str) -> subprocess.CompletedProcess:
    """`code` in an interpreter that has only `package`'s roots on its path (`-I`: no user site, no PYTHONPATH)."""

    roots = [str((ENGINES / package / relative).resolve()) for relative in _manifest(package)["paths"]]
    prefix = f"import sys; sys.path[:0] = {roots!r}\n"
    return subprocess.run(
        [sys.executable, "-I", "-B", "-c", prefix + code, *arguments],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120, cwd=tempfile.gettempdir(),
    )


class PackageLayoutTests(unittest.TestCase):
    def test_nothing_in_the_engines_is_installable(self) -> None:
        found = [path.relative_to(ROOT).as_posix() for name in ("pyproject.toml", "setup.py", "setup.cfg")
                 for path in ENGINES.rglob(name)]
        self.assertEqual(found, [])

    def test_each_package_carries_exactly_its_list(self) -> None:
        for key, package in PACKAGES.items():
            with self.subTest(package=package):
                listing = _listing(key)
                self.assertEqual(listing["source_root"], f"engines/{package}/src")
                expected = {entry["path"]: entry["sha256"] for entry in listing["files"]}
                actual = {relative: path for relative, path in _files(ENGINES / package / "src").items()
                          if package not in COMPAT or not relative.startswith("finesub_bootstrap/")}
                self.assertEqual(sorted(set(actual) ^ set(expected)), [])
                self.assertEqual([relative for relative, path in actual.items() if _sha(path) != expected[relative]], [])

    def test_every_file_of_the_vendored_lock_has_a_home_or_a_reason(self) -> None:
        vendor = {entry["path"]: entry for entry in
                  json.loads((LISTS / "vendor-quality-lock.json").read_text(encoding="utf-8"))["files"]}
        self.assertEqual(len(vendor), 550)
        carried = {entry["path"] for package in PACKAGES.values() for entry in
                   json.loads((ENGINES / package / "QUALITY_LOCK.json").read_text(encoding="utf-8"))["files"]}
        not_carried = {entry["path"]: entry for entry in
                       json.loads((LISTS / "not-carried.json").read_text(encoding="utf-8"))["files"]}
        self.assertEqual(sorted(set(vendor) - carried - set(not_carried)), [])
        self.assertEqual(sorted(carried & set(not_carried)), [])
        for path, entry in not_carried.items():
            with self.subTest(path=path):
                self.assertTrue(entry["reason"])
                if entry["locked"]:
                    self.assertEqual(entry["sha256"], vendor[path]["sha256"])
                for package in PACKAGES.values():
                    self.assertFalse((ENGINES / package / "src" / path).exists())

    def test_a_file_two_packages_carry_is_the_same_bytes(self) -> None:
        seen: dict[str, tuple[str, str]] = {}
        differing = []
        for package in PACKAGES.values():
            for relative, path in _files(ENGINES / package / "src").items():
                if package in COMPAT and relative.startswith("finesub_bootstrap/"):
                    continue
                digest = _sha(path)
                if relative in seen and seen[relative][1] != digest:
                    differing.append((relative, seen[relative][0], package))
                seen.setdefault(relative, (package, digest))
        self.assertEqual(differing, [])

    def test_the_packages_own_shared_modules_are_one_text(self) -> None:
        adapters = {package: ENGINES / package / "adapter" for package in PACKAGES.values()}
        own = {"finesub-transcribe": "finesub_transcribe", "finesub-correct": "finesub_correct",
               "finesub-knowledge": "finesub_knowledge_host"}
        for module in ("bridge.py", "wire.py", "failures.py"):
            with self.subTest(module=module):
                texts = {(adapters[package] / own[package] / module).read_bytes() for package in PACKAGES.values()}
                self.assertEqual(len(texts), 1)
        for module in ("__init__.py", "client.py", "protocol.py"):
            with self.subTest(module=f"finesub_knowledge/{module}"):
                texts = {(adapters[package] / "finesub_knowledge" / module).read_bytes()
                         for package in ("finesub-transcribe", "finesub-correct")}
                self.assertEqual(len(texts), 1)
        self.assertEqual((adapters["finesub-knowledge"] / "finesub_knowledge/protocol.py").read_bytes(),
                         (adapters["finesub-correct"] / "finesub_knowledge/protocol.py").read_bytes())
        self.assertFalse((adapters["finesub-knowledge"] / "finesub_knowledge/client.py").exists())
        for package in ("finesub-transcribe", "finesub-correct"):
            self.assertFalse((adapters[package] / "finesub_knowledge/server.py").exists())

    def test_the_packages_find_their_own_sources(self) -> None:
        for package in PACKAGES.values():
            with self.subTest(package=package):
                self.assertEqual(_manifest(package)["paths"], ["adapter", "src"])


class IsolationTests(unittest.TestCase):
    def test_no_package_code_imports_the_core_or_another_package(self) -> None:
        others = set().union(*ADAPTERS.values())
        for package, own in ADAPTERS.items():
            for root in (ENGINES / package / "adapter", ENGINES / package / "src"):
                for path in root.rglob("*.py"):
                    with self.subTest(path=path.relative_to(ENGINES).as_posix()):
                        tops = _imported_tops(path)
                        self.assertNotIn("nonoka_x", tops)
                        self.assertEqual(sorted(tops & (others - own)), [])

    def test_in_its_own_process_a_package_reaches_no_other(self) -> None:
        probes = {
            "finesub-transcribe": ["finesub_correct", "finesub_knowledge_host", "finesub_knowledge.server",
                                   "finesub.llm.client", "nonoka_x"],
            "finesub-correct": ["finesub_transcribe", "finesub_knowledge_host", "finesub_knowledge.server",
                                "finesub.speech.recognition.vad_asr_stage", "nonoka_x"],
            "finesub-knowledge": ["finesub_transcribe", "finesub_correct", "finesub_knowledge.client",
                                  "finesub.llm.client", "finesub.speech.recognition.vad_asr_stage", "nonoka_x"],
        }
        code = (
            "import importlib, json\n"
            "found = {}\n"
            "for name in json.loads(sys.argv[1]):\n"
            "    try:\n"
            "        importlib.import_module(name)\n"
            "        found[name] = 'imported'\n"
            "    except ModuleNotFoundError:\n"
            "        found[name] = 'missing'\n"
            "print(json.dumps(found))\n"
        )
        for package, names in probes.items():
            with self.subTest(package=package):
                completed = _package_python(package, code, json.dumps(names))
                self.assertEqual(completed.returncode, 0, completed.stderr)
                self.assertEqual(json.loads(completed.stdout.strip().splitlines()[-1]),
                                 {name: "missing" for name in names})


class CompatibilityPackageTests(unittest.TestCase):
    def test_the_names_are_exactly_what_the_package_imports(self) -> None:
        for key in ("correct", "knowledge"):
            package = PACKAGES[key]
            compat = ENGINES / package / "src" / "finesub_bootstrap"
            expected = _expected_compat(key)
            modules = {path.stem for path in compat.glob("*.py")} - {"__init__", "_launch"}
            with self.subTest(package=package):
                self.assertEqual(modules, set(expected))
                self.assertEqual(_public_definitions(compat / "__init__.py"), set())
            for module, names in expected.items():
                with self.subTest(package=package, module=module):
                    path = compat / f"{module}.py"
                    tree = ast.parse(path.read_text(encoding="utf-8"))
                    declared = next(ast.literal_eval(node.value) for node in tree.body if isinstance(node, ast.Assign)
                                    and any(isinstance(t, ast.Name) and t.id == "__all__" for t in node.targets))
                    self.assertEqual(set(declared), names)
                    self.assertEqual(_public_definitions(path), names | COMPAT_HELPERS[package].get(module, set()))
                    self.assertNotIn("nonoka_x", _imported_tops(path))

    def test_the_counts_the_lists_promise(self) -> None:
        """WP-P0's lists: 32 names for the correction package, 14 for the knowledge package (modules imported whole count once)."""

        for key, count in (("correct", 32), ("knowledge", 14)):
            self.assertEqual(len(_listing(key)["bootstrap_surface_used"]) + sum(
                1 for name in _listing(key)["bootstrap_imports"] if ":" not in name), count)

    def test_it_imports_without_the_heavy_stack_and_every_import_of_it_resolves(self) -> None:
        code = (
            "import importlib, importlib.abc, json, tempfile\n"
            "class Refuse(importlib.abc.MetaPathFinder):\n"
            "    def find_spec(self, name, path=None, target=None):\n"
            "        if name.split('.')[0] in {'torch', 'numpy', 'nonoka_x', 'scipy', 'soundfile'}:\n"
            "            raise ImportError('refused: ' + name)\n"
            "sys.meta_path.insert(0, Refuse())\n"
            "from finesub_bootstrap import _launch\n"
            "_launch.bind(tempfile.gettempdir())\n"
            "missing = []\n"
            "for name in json.loads(sys.argv[1]):\n"
            "    module, _, attribute = name.partition(':')\n"
            "    loaded = importlib.import_module(module)\n"
            "    if attribute and not hasattr(loaded, attribute):\n"
            "        missing.append(name)\n"
            "print(json.dumps({'missing': missing, 'heavy': sorted(m for m in ('torch', 'numpy') if m in sys.modules)}))\n"
        )
        for key in ("correct", "knowledge"):
            names = list(_listing(key)["bootstrap_imports"]) + list(_listing(key)["bootstrap_surface_used"])
            with self.subTest(package=PACKAGES[key]):
                completed = _package_python(PACKAGES[key], code, json.dumps(names))
                self.assertEqual(completed.returncode, 0, completed.stderr)
                self.assertEqual(json.loads(completed.stdout.strip().splitlines()[-1]), {"missing": [], "heavy": []})

    def test_its_locations_come_from_the_launch_context(self) -> None:
        script = (
            "import json\n"
            "from pathlib import Path\n"
            "from finesub_bootstrap import paths\n"
            "try:\n"
            "    paths.default_data_root()\n"
            "    unbound = 'answered'\n"
            "except RuntimeError as error:\n"
            "    unbound = str(error)\n"
            "entry = sys.argv[2]\n"
            "bridge = __import__(entry + '.bridge', fromlist=['bind_package_data'])\n"
            "bridge.bind_package_data({'paths': {'package_data': sys.argv[1]}})\n"
            "data = paths.default_data_root()\n"
            "layout = paths.load_app_paths(data, data_root=data)\n"
            "print(json.dumps({'unbound': unbound, 'data': str(data), 'user_data': str(layout.user_data),\n"
            "                  'capsules': str(layout.agent_capsules), 'logs': str(layout.logs)}))\n"
        )
        with tempfile.TemporaryDirectory() as temp:
            for package, entry in (("finesub-correct", "finesub_correct"), ("finesub-knowledge", "finesub_knowledge_host")):
                data = Path(temp) / "engines" / package
                with self.subTest(package=package):
                    completed = _package_python(package, script, str(data), entry)
                    self.assertEqual(completed.returncode, 0, completed.stderr)
                    found = json.loads(completed.stdout.strip().splitlines()[-1])
                    self.assertIn("no package data directory was bound", found["unbound"])
                    resolved = data.resolve()
                    self.assertEqual(Path(found["data"]), resolved)
                    self.assertEqual(Path(found["user_data"]), resolved / "user-data")
                    self.assertEqual(Path(found["capsules"]), resolved / "agent-capsules")
                    self.assertEqual(Path(found["logs"]), resolved / "user-data" / "logs")

    def test_the_transcription_package_carries_the_original(self) -> None:
        original = ENGINES / "finesub-transcribe" / "src" / "finesub_bootstrap"
        self.assertFalse((original / "_launch.py").exists())
        self.assertTrue((original / "model_ensure.py").is_file())
        self.assertTrue((original / "download_routes.py").is_file())


def _gate(only: str, engines: Path, *extra: str) -> subprocess.CompletedProcess:
    environment = {**os.environ, "NONOKA_QUALITY_ENGINES": str(engines), "PYTHONIOENCODING": "utf-8"}
    return subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "quality_gate.py"), "--only", only, *extra],
        cwd=ROOT, env=environment, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=600,
    )


_TRANSCRIPTION_SCENARIOS = r"""
import json, sys
from tests.quality_harness import calls
names = [name for name, scenario in calls.SCENARIOS.items()
         if scenario["request"].get("target") in ("vocal", "aligned", "stable")
         and scenario["request"].get("source", {}).get("kind") != "cloud_stable"]
records = calls.run_all(names)
print(json.dumps({"names": names, "differences": calls.differences(records)}, ensure_ascii=False))
"""


class DeletabilityTests(unittest.TestCase):
    """Plan 4.6 item 33: a package is found by its `PACKAGE.json`, and the rest works without it."""

    maxDiff = None

    def test_without_the_transcription_package_the_correction_passes_its_gate(self) -> None:
        from tests.test_worker_adapter import engines_without

        with tempfile.TemporaryDirectory() as temp:
            completed = _gate("replay", engines_without(Path(temp), "finesub-transcribe"))
            self.assertEqual(completed.returncode, 0, completed.stdout[-3000:] + completed.stderr[-3000:])
            self.assertIn("[PASS] LLM replay", completed.stdout)

    def test_without_the_correction_package_the_transcription_passes_its_gate(self) -> None:
        from tests.test_worker_adapter import engines_without

        with tempfile.TemporaryDirectory() as temp:
            engines = engines_without(Path(temp), "finesub-correct")
            completed = subprocess.run(
                [sys.executable, "-c", _TRANSCRIPTION_SCENARIOS], cwd=ROOT,
                env={**os.environ, "NONOKA_QUALITY_ENGINES": str(engines), "PYTHONIOENCODING": "utf-8"},
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=600,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr[-3000:])
            result = json.loads(completed.stdout.strip().splitlines()[-1])
            self.assertGreater(len(result["names"]), 3, result["names"])
            self.assertEqual(result["differences"], {})

    def test_without_the_correction_package_the_core_renders_the_raw_srt(self) -> None:
        from tests.test_worker_adapter import BASE, file_of, run_orchestration_child

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            record = run_orchestration_child(root, {**BASE, "target": "raw-srt"}, without=("finesub-correct",))
            (run,) = record["runs"]
            self.assertEqual(run["exit"], 0, run["events"])
            artifacts = next(e for e in run["events"] if e["type"] == "completed")["payload"]["artifacts"]["artifacts"]
            task_dir = root / "data" / "tasks" / "orchestration"
            raw = file_of(artifacts["raw_srt"]["uri"])
            self.assertEqual(raw, task_dir / "raw.srt")
            from nonoka_x.transcription.srt import render_srt
            from nonoka_x.transcription.model import read_transcript

            self.assertEqual(raw.read_text(encoding="utf-8"), render_srt(read_transcript(task_dir / "transcript.json")))
            self.assertTrue(raw.read_text(encoding="utf-8").startswith("1\n00:00:"))
            # Nothing of FineSub's correction ran, and FineSub's own files stay in its directory.
            self.assertFalse({"correction_translation.run_correction_task", "rendering.convert_json_to_srt"}
                             & set(record["calls"]), record["calls"])
            self.assert_finesub_files_are_private(task_dir)

    def test_without_either_finesub_package_a_cloud_api_engine_reaches_the_raw_srt(self) -> None:
        from tests.test_worker_adapter import BASE, OrchestrationTests, file_of, run_orchestration_child

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            record = run_orchestration_child(
                root, {**BASE, "target": "raw-srt"},
                after_validation={"transcription": {"engine": "transcript-only"}},
                transcription_engines=[OrchestrationTests.TRANSCRIPT_ONLY],
                without=("finesub-transcribe", "finesub-correct"),
            )
            (run,) = record["runs"]
            self.assertEqual(run["exit"], 0, run["events"])
            self.assertEqual(record["calls"], [])
            artifacts = next(e for e in run["events"] if e["type"] == "completed")["payload"]["artifacts"]["artifacts"]
            task_dir = root / "data" / "tasks" / "orchestration"
            raw = file_of(artifacts["raw_srt"]["uri"]).read_text(encoding="utf-8")
            self.assertTrue(raw.startswith("1\n"), raw)
            self.assertIn(" --> ", raw)
            self.assertFalse((task_dir / "native").exists())

    def test_with_both_finesub_packages_their_files_of_a_task_stay_private(self) -> None:
        from tests.test_worker_adapter import BASE, run_orchestration_child

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            record = run_orchestration_child(
                root, {**BASE, "target": "final-srt", "diarization": {"level": "acoustics"},
                       "correction": {"media": "text", "retrieval": "none"}})
            (run,) = record["runs"]
            self.assertEqual(run["exit"], 0, run["events"])
            task_dir = root / "data" / "tasks" / "orchestration"
            self.assertFalse((task_dir / "raw.srt").exists())  # the correction package's, with it installed
            self.assert_finesub_files_are_private(task_dir)

    def assert_finesub_files_are_private(self, task_dir: Path) -> None:
        """Outside `native/<package>/` only the core's own files of the task (no stable, timeline, rows or records)."""

        outside = sorted(path.relative_to(task_dir).as_posix() for path in task_dir.rglob("*")
                         if path.is_file() and path.relative_to(task_dir).parts[0] != "native")
        finesub_shaped = [name for name in outside if any(marker in name for marker in (
            "-stable.json", "-diar", "llm-artifacts", "-vad", "-aligned", ".annotated.csv", "_vocals", "-metadata.json"))]
        self.assertEqual(finesub_shaped, [], outside)


if __name__ == "__main__":
    unittest.main()
