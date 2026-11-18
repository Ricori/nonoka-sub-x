"""A task's library round trip in Nonoka's formats (WP-L): synced from one data directory, opened in another.

The desktop syncs exactly the manifest entries `local_provider.ARTIFACT_SUFFIXES`
names (`desktop/internal/cloud/service.go` `libraryArtifacts`), as text, and a
retrieval hands those texts to another sidecar's `/v1/documents/project`
(`LocalProvider.project_contents`). Here a cloud API engine (the recording
stand-in of `tests/test_worker_adapter.py`) transcribes to the raw SRT only --
once with no correction package installed (the core renders the raw SRT and
the document comes from `transcript.json`), once with it (the correction
package's `correction.json`) -- and the synced texts are opened in a second,
empty data directory.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from nonoka_x.local_provider import ARTIFACT_SUFFIXES, LocalProvider
from tests import test_worker_adapter as worker_tests
from tests.test_worker_adapter import BASE, ENGINES, file_of, run_orchestration_child


def texts(document: dict) -> list[str]:
    """Every line of a document, its main lane and the speaker tracks the provider split off, in time order."""

    lines = list(document["subtitles"]) + [seg for track in document["tracks"] for seg in track["segs"]]
    return [line["ja"] for line in sorted(lines, key=lambda line: (line["t0"], line["t1"]))]


def synced(manifest: dict) -> dict[str, str]:
    """What the desktop uploads of a task: the library's names only, as text."""

    return {
        name: file_of(entry["uri"]).read_text(encoding="utf-8")
        for name, entry in manifest["artifacts"].items() if name in ARTIFACT_SUFFIXES
    }


class LibraryRoundTripTests(unittest.TestCase):
    def run_raw_srt(self, root: Path, *, without: tuple[str, ...]) -> dict:
        record = run_orchestration_child(
            root,
            {**BASE, "target": "raw-srt"},
            after_validation={"transcription": {"engine": "transcript-only"}},
            transcription_engines=[worker_tests.OrchestrationTests.TRANSCRIPT_ONLY],
            without=without,
        )
        (run,) = record["runs"]
        self.assertEqual(run["exit"], 0, run["events"])
        return next(event for event in run["events"] if event["type"] == "completed")["payload"]["artifacts"]

    def open_elsewhere(self, root: Path, contents: dict[str, str]) -> dict:
        provider = LocalProvider(root / "elsewhere" / "tasks", ENGINES, issues=[])
        try:
            document = provider.project_contents({
                "video_id": "loc_0123456789ab", "task_id": "vid_0123456789abcdef01234567",
                "title": "节目", "artifacts": contents,
            })
            return provider.document(document["video_id"])
        finally:
            provider.shutdown()

    def test_without_a_correction_package_the_transcript_and_raw_srt_travel(self) -> None:
        with tempfile.TemporaryDirectory(prefix="nx-") as temp:
            root = Path(temp)
            manifest = self.run_raw_srt(root, without=("finesub-transcribe", "finesub-correct"))
            contents = synced(manifest)
            self.assertEqual(set(contents), {"transcript_json", "raw_srt"})
            transcript = json.loads(contents["transcript_json"])
            document = self.open_elsewhere(root, contents)
        self.assertEqual(texts(document), [line["text"].strip() for line in transcript["lines"]])
        self.assertEqual(document["projection"]["mode"], "stable")

    def test_with_the_correction_package_its_correction_json_travels(self) -> None:
        with tempfile.TemporaryDirectory(prefix="nx-") as temp:
            root = Path(temp)
            manifest = self.run_raw_srt(root, without=("finesub-transcribe",))
            contents = synced(manifest)
            self.assertEqual(set(contents), {"transcript_json", "correction_json", "raw_srt"})
            correction = json.loads(contents["correction_json"])
            document = self.open_elsewhere(root, contents)
        self.assertEqual(correction["mode"], "stable")
        self.assertEqual(texts(document), [line["source"] for line in correction["lines"]])
        # The transcript's own speakers, as the correction read its lines, became the tracks.
        self.assertTrue(any(line.get("speaker") for line in correction["lines"]))
        self.assertTrue(document["tracks"])

    def test_a_projection_without_nonoka_formats_is_refused(self) -> None:
        with tempfile.TemporaryDirectory(prefix="nx-") as temp:
            provider = LocalProvider(Path(temp) / "tasks", ENGINES, issues=[])
            try:
                with self.assertRaisesRegex(Exception, "stable_json"):
                    provider.project_contents({"video_id": "loc_1", "task_id": "abc", "artifacts": {"stable_json": "{}"}})
                with self.assertRaisesRegex(Exception, "transcript_json or correction_json"):
                    provider.project_contents({"video_id": "loc_1", "task_id": "abc", "artifacts": {"raw_srt": "1"}})
            finally:
                provider.shutdown()


if __name__ == "__main__":
    unittest.main()
