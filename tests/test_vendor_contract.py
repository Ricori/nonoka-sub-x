from __future__ import annotations

import ast
import importlib
import json
import sys
import tempfile
import subprocess
import unittest
from pathlib import Path

#: Since WP-P1 the vendored FineSub is three engine packages (`engines/<package>/`):
#: each carries its half's FineSub sources under `src/`, its own code under
#: `adapter/`, and the files that vouch for the sources. The test process sees
#: all three through the test union (`tests/quality_harness/union.py`).
ROOT = Path(__file__).resolve().parents[1]
ENGINES = ROOT / "engines"
PACKAGES = ("finesub-transcribe", "finesub-correct", "finesub-knowledge")
TRANSCRIBE = ENGINES / "finesub-transcribe"
CORRECT = ENGINES / "finesub-correct"
sys.path.insert(0, str(ROOT / "src"))


def _module(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _assignment(path: Path, name: str) -> ast.AST:
    for node in _module(path).body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == name for target in node.targets
        ):
            return node.value
    raise AssertionError(f"{name} is not defined in {path}")


def _enum_members(path: Path, name: str) -> set[str]:
    for node in _module(path).body:
        if isinstance(node, ast.ClassDef) and node.name == name:
            return {
                statement.value.value
                for statement in node.body
                if isinstance(statement, ast.Assign) and isinstance(statement.value, ast.Constant)
            }
    raise AssertionError(f"{name} is not defined in {path}")


def _tracked_top_level(directory: Path) -> set[str]:
    """The names directly under `directory` that the repository tracks.

    Asked of git, so a cache a local run left there (ignored, never committed)
    does not count; the directory itself outside a git checkout.
    """

    try:
        listed = subprocess.run(
            ["git", "ls-files", "--", "."], cwd=directory, capture_output=True, text=True, check=True
        ).stdout.splitlines()
    except (OSError, subprocess.CalledProcessError):
        return {entry.name for entry in directory.iterdir()}
    return {line.split("/", 1)[0] for line in listed if line}


class VendorContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.upstream = json.loads((TRANSCRIBE / "UPSTREAM.json").read_text(encoding="utf-8"))

    def test_upstream_record_names_the_version_and_nothing_else(self) -> None:
        """Which upstream the packages' FineSub is based on, the same in each; integrity is the locks' job."""

        for package in PACKAGES:
            self.assertEqual(json.loads((ENGINES / package / "UPSTREAM.json").read_text(encoding="utf-8")), self.upstream)
            manifest = json.loads((ENGINES / package / "PACKAGE.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["upstream"]["commit"], self.upstream["commit"])
        self.assertEqual(
            set(self.upstream),
            {"repository", "ref", "commit", "engine_version", "engine_bundle_id"},
        )
        self.assertEqual(
            self.upstream["engine_bundle_id"],
            f"finesub-{self.upstream['engine_version']}+{self.upstream['commit'][:12]}",
        )

    def test_engine_packages_import_without_loading_models(self) -> None:
        self.assertIsNotNone(importlib.import_module("finesub"))
        self.assertIsNotNone(importlib.import_module("finesub_bootstrap"))

    def test_each_package_keeps_only_its_core_files(self) -> None:
        """Plan section 1 item 7 (WP-R7), per package since WP-P1: the sources and what vouches for them.

        No `pyproject.toml`, `VERSION` or README: an engine package is not
        installed from here, its version is `UPSTREAM.json`'s, and without
        `pyproject.toml` beside `src/finesub` the engine never takes the package
        directory for a source checkout (`finesub.paths._is_checkout_root`). The
        prompt templates' licence goes where the templates are.
        """

        from finesub.paths import _is_checkout_root

        core = {"PACKAGE.json", "adapter", "src", "LICENSE", "LICENSES.json", "UPSTREAM.json",
                "QUALITY_LOCK.json", "LOCAL_CHANGES.md"}
        for package in PACKAGES:
            with self.subTest(package=package):
                directory = ENGINES / package
                templates = (directory / "src/finesub/llm/prompt_templates").is_dir()
                self.assertEqual(_tracked_top_level(directory), core | ({"PROMPT_LICENSE.md"} if templates else set()))
                licences = json.loads((directory / "LICENSES.json").read_text(encoding="utf-8"))
                listed = {component["path"] for component in licences["components"]}
                self.assertEqual(listed, {"LICENSE"} | ({"src/finesub/llm/prompt_templates"} if templates else set()))
                self.assertFalse(_is_checkout_root(directory))

    def test_runtime_metadata_is_package_data(self) -> None:
        """The installer's two assets are package data under `src/finesub_bootstrap/`.

        Resolved there by `finesub_bootstrap.resources` and `.environment`, which
        makes those the only copies that are ever read.
        """

        bootstrap = TRANSCRIBE / "src/finesub_bootstrap"
        runtime = json.loads(
            (bootstrap / "runtime-manifest.json").read_text(encoding="utf-8")
        )
        self.assertEqual(runtime["schema_version"], 1)
        self.assertTrue((bootstrap / "pylock.win-py312.toml").is_file())
        self.assertTrue((bootstrap / "pylock.win-py312.cn.toml").is_file())

    def test_desktop_restates_the_engine_stage_and_role_lists(self) -> None:
        """Two lists this repository keeps a copy of, checked against the source.

        `nonoka_x.local_provider` runs on the bootstrap interpreter and cannot
        import FineSub, so it restates the pipeline stages and the text-only
        LLM roles. A sync that renames or adds one has to reach those copies,
        and the failure without this check is a target the desktop offers and
        the engine rejects -- at run time, on the user's machine.

        Read statically rather than imported: `finesub.stages` pulls in the ASR
        stack, which is not installed in the test environment.
        """

        from nonoka_x.local_provider import LLM_ROLES, PIPELINE_TARGETS
        from finesub_correct.llm import ENGINE_ROLES, ROLES

        stages = _assignment(CORRECT / "src/finesub/stages.py", "PIPELINE_STAGE_ORDER")
        self.assertEqual(
            set(PIPELINE_TARGETS),
            {key.value for key in stages.keys},
        )
        roles = _enum_members(CORRECT / "src/finesub/llm/routing/config.py", "LLMRole")
        engine_roles = set(ENGINE_ROLES.values())
        self.assertTrue(engine_roles <= roles, f"{engine_roles - roles} are not engine roles")
        self.assertEqual(set(ROLES), set(LLM_ROLES))
        self.assertEqual(set(ENGINE_ROLES), set(ROLES))

    def test_a_disabled_limiter_neither_honours_nor_starts_a_cooldown(self) -> None:
        """What the LLM worker relies on for a settings-page model check."""

        from finesub.llm.rate_limit import ComboCooldownPhase, ModelRateLimiter
        from finesub.llm.routing.config import ModelEndpoint

        self.assertIn(
            "rate_limiter: ModelRateLimiter | None = None",
            (CORRECT / "src/finesub/llm/client.py").read_text(encoding="utf-8"),
        )
        endpoint = ModelEndpoint("TIER", "model")
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "state"
            ModelRateLimiter(state_path=state).note_combo_exhausted(endpoint, key_id="cooling")
            disabled = ModelRateLimiter(state_path=state, enabled=False)
            self.assertIs(disabled.combo_cooldown_phase(endpoint, key_id="cooling"), ComboCooldownPhase.NONE)
            disabled.note_combo_exhausted(endpoint, key_id="checked")
            fresh = ModelRateLimiter(state_path=state)
            self.assertIs(fresh.combo_cooldown_phase(endpoint, key_id="cooling"), ComboCooldownPhase.SKIP)
            self.assertIs(fresh.combo_cooldown_phase(endpoint, key_id="checked"), ComboCooldownPhase.NONE)

    def test_desktop_restates_the_engine_switch_values(self) -> None:
        """The knowledge modes and diarization levels the provider validates.

        Both are checked before a task is queued, on an interpreter that cannot
        import FineSub; a value the provider accepts and the engine rejects
        fails only after recognition has already run.
        """

        from nonoka_x.local_provider import DIARIZE_LEVELS, KNOWLEDGE_MODES

        engine = CORRECT / "src/finesub"
        self.assertEqual(
            KNOWLEDGE_MODES,
            ast.literal_eval(_assignment(engine / "llm/routing/profiles.py", "KNOWLEDGE_MODES")),
        )
        self.assertEqual(
            DIARIZE_LEVELS,
            ast.literal_eval(_assignment(engine / "stages.py", "DIARIZE_LEVELS")),
        )

    def test_nonoka_x_macos_media_manifests_are_packaged(self) -> None:
        resources = Path(__file__).resolve().parents[1] / "src/nonoka_x/resources"
        self.assertTrue((resources / "runtime-manifest.macos-arm64.json").is_file())
        self.assertTrue((resources / "runtime-manifest.macos-amd64.json").is_file())

    def test_required_package_data_is_vendored(self) -> None:
        prompt_root = CORRECT / "src/finesub/llm/prompt_templates"
        self.assertTrue(any(prompt_root.glob("*.md")))
        self.assertTrue((prompt_root / "LICENSE.md").is_file())
        for package in PACKAGES:
            routing = ENGINES / package / "src/finesub/llm/routing"
            self.assertTrue((routing / "model_catalog.toml").is_file(), package)
            self.assertTrue((routing / "model_routes.toml").is_file(), package)
        bootstrap = TRANSCRIBE / "src/finesub_bootstrap"
        self.assertTrue((bootstrap / "model-manifest.json").is_file())
        self.assertTrue((bootstrap / "download-sources.json").is_file())


if __name__ == "__main__":
    unittest.main()
