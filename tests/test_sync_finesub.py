from __future__ import annotations

import io
import json
import tarfile
import tempfile
import unittest
from pathlib import Path

from scripts.build_finesub_bundle import build_bundle
from scripts.sync_finesub import (
    RUNTIME_LOCKS,
    RUNTIME_MANIFEST,
    SOURCE_FILES,
    SOURCE_TREES,
    SyncError,
    Upstream,
    VERSION_FILE,
    sync_archive,
    verify_snapshot,
)


COMMIT = "1" * 40
COMMITTED_AT = "2026-01-02T03:04:05Z"


def fixture_files() -> dict[str, bytes]:
    files = {
        "README.md": b"# FineSub fixture\n",
        "LICENSE": b"MIT fixture\n",
        # Dynamic since upstream 0.5.0; the number lives in `VERSION`.
        "pyproject.toml": b'[project]\nname = "finesub"\ndynamic = ["version"]\n',
        VERSION_FILE: b'1.2.3\n',
        RUNTIME_LOCKS[0]: b'lock = 1\n',
        RUNTIME_LOCKS[1]: b'lock = 1\n',
        RUNTIME_MANIFEST: b'{"schema_version": 1}\n',
        "src/finesub/__init__.py": b'__version__ = "1.2.3"\n',
        "src/finesub/llm/prompt_templates/LICENSE.md": b"CC BY-SA 4.0 fixture\n",
        "src/finesub/llm/prompt_templates/prompt.md": b"prompt\n",
        "src/finesub/llm/routing/model_catalog.psv": b"name|model\n",
        "src/finesub/llm/routing/model_routes.toml": b"routes = []\n",
        "src/finesub_bootstrap/__init__.py": b"\n",
        "src/finesub_bootstrap/model-manifest.json": b"{}\n",
        "src/finesub_bootstrap/download-sources.json": b"{}\n",
    }
    assert all(any(path == item or path.startswith(item + "/") for path in files) for item in SOURCE_TREES)
    assert all(path in files for path in SOURCE_FILES)
    assert VERSION_FILE in files
    assert RUNTIME_MANIFEST in files
    assert all(path in files for path in RUNTIME_LOCKS)
    return files


def make_archive(destination: Path, *, omit: str | None = None) -> None:
    root = f"finesub-{COMMIT}"
    with tarfile.open(destination, "w:gz") as archive:
        for relative, data in fixture_files().items():
            if relative == omit:
                continue
            info = tarfile.TarInfo(f"{root}/{relative}")
            info.size = len(data)
            info.mtime = 1
            archive.addfile(info, io.BytesIO(data))


class SyncFineSubTests(unittest.TestCase):
    def upstream(self) -> Upstream:
        return Upstream("https://github.com/caca2331/finesub", "v1.2.3", COMMIT, COMMITTED_AT)

    def test_sync_is_byte_stable_and_snapshot_verifies(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            archive = root / "source.tar.gz"
            make_archive(archive)
            first = root / "first"
            second = root / "second"
            sync_archive(archive, self.upstream(), first, root / "no-patches")
            sync_archive(archive, self.upstream(), second, root / "no-patches")
            self.assertEqual(
                {p.relative_to(first): p.read_bytes() for p in first.rglob("*") if p.is_file()},
                {p.relative_to(second): p.read_bytes() for p in second.rglob("*") if p.is_file()},
            )
            metadata = verify_snapshot(first)
            self.assertEqual(metadata["engine_bundle_id"], "finesub-1.2.3+111111111111")
            self.assertEqual((first / VERSION_FILE).read_text(encoding="utf-8"), "1.2.3\n")
            self.assertTrue((first / "UPSTREAM_README.md").is_file())
            vendor_readme = (first / "README.md").read_text(encoding="utf-8")
            self.assertIn("scripts/sync_finesub.py", vendor_readme)
            self.assertIn("提交前必须生成对应 patch", vendor_readme)

    def test_sync_does_not_replace_valid_snapshot_on_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            good = root / "good.tar.gz"
            bad = root / "bad.tar.gz"
            vendor = root / "vendor"
            make_archive(good)
            make_archive(bad, omit="LICENSE")
            sync_archive(good, self.upstream(), vendor, root / "no-patches")
            before = (vendor / "UPSTREAM.json").read_bytes()
            with self.assertRaises(SyncError):
                sync_archive(bad, self.upstream(), vendor, root / "no-patches")
            self.assertEqual((vendor / "UPSTREAM.json").read_bytes(), before)
            verify_snapshot(vendor)

    def test_check_detects_manual_vendor_change(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            archive = root / "source.tar.gz"
            vendor = root / "vendor"
            make_archive(archive)
            sync_archive(archive, self.upstream(), vendor, root / "no-patches")
            (vendor / "src/finesub/__init__.py").write_text("tampered\n")
            with self.assertRaisesRegex(SyncError, "modified"):
                verify_snapshot(vendor)

    def test_bundle_is_byte_stable_and_contains_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            archive = root / "source.tar.gz"
            vendor = root / "vendor"
            make_archive(archive)
            sync_archive(archive, self.upstream(), vendor, root / "no-patches")
            first = build_bundle(vendor, root / "first").read_bytes()
            bundle = build_bundle(vendor, root / "second")
            self.assertEqual(first, bundle.read_bytes())
            import zipfile

            with zipfile.ZipFile(bundle) as zipped:
                manifest_name = next(name for name in zipped.namelist() if name.endswith("engine-bundle.json"))
                manifest = json.loads(zipped.read(manifest_name))
                version_name = next(name for name in zipped.namelist() if name.endswith("/VERSION"))
                self.assertEqual(zipped.read(version_name), b"1.2.3\n")
            self.assertEqual(manifest["engine"]["commit"], COMMIT)
            self.assertEqual(manifest["adapter_schema"], 1)


if __name__ == "__main__":
    unittest.main()
