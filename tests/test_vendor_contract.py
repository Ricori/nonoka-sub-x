from __future__ import annotations

import ast
import importlib
import json
import sys
import tomllib
import unittest
from pathlib import Path

from scripts.sync_finesub import DEFAULT_VENDOR, sha256_file, verify_snapshot

ROOT = Path(__file__).resolve().parents[1]
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


class VendorContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.upstream = verify_snapshot(DEFAULT_VENDOR)

    def test_engine_packages_import_without_loading_models(self) -> None:
        source = DEFAULT_VENDOR / "src"
        sys.path.insert(0, str(source))
        previous = sys.dont_write_bytecode
        sys.dont_write_bytecode = True
        try:
            self.assertIsNotNone(importlib.import_module("finesub"))
            self.assertIsNotNone(importlib.import_module("finesub_bootstrap"))
        finally:
            sys.dont_write_bytecode = previous
            sys.path.remove(str(source))
            sys.modules.pop("finesub", None)
            sys.modules.pop("finesub_bootstrap", None)

    def test_version_and_runtime_metadata_match(self) -> None:
        """The snapshot's own numbers, read where upstream 0.5.0 keeps them.

        `project.version` went dynamic when the desktop split moved the single
        number to a repository-root `VERSION`, and the installer's two assets
        became package data under `src/finesub_bootstrap/` -- resolved there by
        `finesub_bootstrap.resources` and `.environment`, which makes those the
        only copies that are ever read.
        """

        project = tomllib.loads((DEFAULT_VENDOR / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertIn("version", project["project"]["dynamic"])
        self.assertEqual(
            (DEFAULT_VENDOR / "VERSION").read_text(encoding="utf-8").strip(),
            self.upstream["engine_version"],
        )
        bootstrap = DEFAULT_VENDOR / "src/finesub_bootstrap"
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
        from nonoka_x.llm_worker import ROLES

        stages = _assignment(DEFAULT_VENDOR / "src/finesub/stages.py", "PIPELINE_STAGE_ORDER")
        self.assertEqual(
            set(PIPELINE_TARGETS),
            {key.value for key in stages.keys},
        )
        roles = _enum_members(DEFAULT_VENDOR / "src/finesub/llm/routing/config.py", "LLMRole")
        self.assertTrue(set(LLM_ROLES) <= roles, f"{set(LLM_ROLES) - roles} are not engine roles")
        self.assertEqual(set(ROLES), set(LLM_ROLES))

    def test_nonoka_x_macos_media_manifests_are_packaged(self) -> None:
        resources = Path(__file__).resolve().parents[1] / "src/nonoka_x/resources"
        self.assertTrue((resources / "runtime-manifest.macos-arm64.json").is_file())
        self.assertTrue((resources / "runtime-manifest.macos-amd64.json").is_file())

    def test_required_package_data_is_vendored(self) -> None:
        prompt_root = DEFAULT_VENDOR / "src/finesub/llm/prompt_templates"
        self.assertTrue(any(prompt_root.glob("*.md")))
        self.assertTrue((prompt_root / "LICENSE.md").is_file())
        routing = DEFAULT_VENDOR / "src/finesub/llm/routing"
        self.assertTrue((routing / "model_catalog.psv").is_file())
        self.assertTrue((routing / "model_routes.toml").is_file())
        bootstrap = DEFAULT_VENDOR / "src/finesub_bootstrap"
        self.assertTrue((bootstrap / "model-manifest.json").is_file())
        self.assertTrue((bootstrap / "download-sources.json").is_file())

    def test_recorded_finesub_patches_are_present_and_hashed(self) -> None:
        patch_root = DEFAULT_VENDOR.parents[1] / "patches" / "finesub"
        patches = self.upstream.get("patches") or []
        self.assertTrue(patches)
        self.assertEqual(
            [entry["path"] for entry in patches],
            [path.name for path in sorted(patch_root.glob("*.patch"))],
        )
        for entry in patches:
            path = patch_root / entry["path"]
            self.assertTrue(path.is_file())
            self.assertEqual(sha256_file(path), entry["sha256"])


if __name__ == "__main__":
    unittest.main()
