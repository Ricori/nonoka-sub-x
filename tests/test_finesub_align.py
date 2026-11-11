"""`scripts/finesub_align.py`: a report over the key algorithm paths, nothing more.

Offline only: every tree is built in a temporary directory and passed with
`--base-dir` / `--upstream-dir`, so no test downloads anything.
"""

from __future__ import annotations

import contextlib
import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from scripts import finesub_align
from scripts.finesub_align import is_key_path, main


ROOT = Path(__file__).resolve().parents[1]
QUALITY_LOCK = ROOT / "third_party" / "finesub" / "QUALITY_LOCK.json"


def _tree(root: Path, files: dict[str, str]) -> Path:
    for relative, text in files.items():
        path = root / "src" / "finesub" / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


class KeyPathTests(unittest.TestCase):
    def test_the_section_3_2_paths_are_key_and_the_rest_are_not(self) -> None:
        for path in (
            "speech/recognition/vad_asr_stage.py",
            "media/audio.py",
            "subtitles/srt.py",
            "text.py",
            "llm/stages/correction/run.py",
            "llm/prompt_templates/fragment.md",
            "llm/output_protocol.py",
            "llm/knowledge/subjects.py",
        ):
            with self.subTest(path=path):
                self.assertTrue(is_key_path(path))
        for path in (
            "speech/joint/backend.py",
            "stages.py",
            "pipeline.py",
            "llm/client.py",
            "llm/web_search.py",
            "llm/agent/local_agent.py",
            "llm/routing/config.py",
            "llm/knowledge/update.py",
            "llm/knowledge/share/server.py",
        ):
            with self.subTest(path=path):
                self.assertFalse(is_key_path(path))

    def test_every_named_key_file_exists_in_the_vendor(self) -> None:
        """A name that matches nothing would silently drop a file from every report."""

        locked = json.loads(QUALITY_LOCK.read_text(encoding="utf-8"))
        names = json.dumps(locked)
        for relative in finesub_align.KEY_FILES:
            with self.subTest(path=relative):
                self.assertTrue((ROOT / "third_party/finesub/src/finesub" / relative).is_file())
                self.assertIn(f"finesub/{relative}", names)
        for relative in finesub_align.KEY_DIRECTORIES:
            with self.subTest(path=relative):
                self.assertTrue((ROOT / "third_party/finesub/src/finesub" / relative).is_dir())


class DiffReportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.scratch = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.scratch, ignore_errors=True)
        common = {"text.py": "t", "llm/client.py": "c", "speech/joint/x.py": "j"}
        self.base = _tree(self.scratch / "base", {**common, "speech/a.py": "a", "speech/gone.py": "g"})
        self.target = _tree(
            self.scratch / "target",
            {**common, "speech/a.py": "a2", "speech/new.py": "n", "llm/client.py": "c2", "speech/joint/x.py": "j2"},
        )
        self.vendor = _tree(self.scratch / "vendor", {**common, "speech/a.py": "a-local", "speech/gone.py": "g"})
        (self.vendor / "UPSTREAM.json").write_text(
            json.dumps({"repository": "https://github.com/o/r", "ref": "v1", "commit": "abc"}),
            encoding="utf-8",
        )

    def _run(self, *extra: str) -> tuple[int, str]:
        out = io.StringIO()
        before = {p: p.read_bytes() for p in self.scratch.rglob("*") if p.is_file()}
        with contextlib.redirect_stdout(out):
            status = main([
                "diff", "--ref", "v2",
                "--base-dir", str(self.base),
                "--upstream-dir", str(self.target),
                "--vendor-dir", str(self.vendor),
                *extra,
            ])
        after = {p: p.read_bytes() for p in self.scratch.rglob("*") if p.is_file()}
        self.assertEqual(before, after, "the report must not write any file")
        return status, out.getvalue()

    def test_reports_upstream_local_and_overlapping_changes_on_key_paths_only(self) -> None:
        status, output = self._run("--json")
        self.assertEqual(status, 0)
        report = json.loads(output)
        self.assertEqual(
            report["upstream"],
            {"added": ["speech/new.py"], "removed": ["speech/gone.py"], "modified": ["speech/a.py"]},
        )
        self.assertEqual(report["local"], {"added": [], "removed": [], "modified": ["speech/a.py"]})
        self.assertEqual(report["both"], ["speech/a.py"])
        self.assertEqual((report["base_commit"], report["target_ref"]), ("abc", "v2"))

    def test_text_report_names_the_files(self) -> None:
        status, output = self._run()
        self.assertEqual(status, 0)
        self.assertIn("modified speech/a.py", output)
        self.assertNotIn("llm/client.py", output)
        self.assertNotIn("speech/joint", output)

    def test_a_tree_without_the_engine_is_an_error(self) -> None:
        empty = self.scratch / "empty"
        empty.mkdir()
        with contextlib.redirect_stderr(io.StringIO()):
            status = main([
                "diff", "--ref", "v2",
                "--base-dir", str(empty),
                "--upstream-dir", str(self.target),
                "--vendor-dir", str(self.vendor),
            ])
        self.assertEqual(status, 1)


if __name__ == "__main__":
    unittest.main()
