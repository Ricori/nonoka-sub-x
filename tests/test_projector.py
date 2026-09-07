from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from nonoka_x.document_store import DocumentError, DocumentStore, RevisionConflict
from nonoka_x.projector import ProjectionError, parse_srt, project_edit_document


FIXTURES = Path(__file__).parent / "fixtures" / "finesub"


class ProjectorTests(unittest.TestCase):
    def test_srt_parser_accepts_blank_line_between_index_and_timing(self) -> None:
        cues = parse_srt(
            "1\r\n\r\n00:00:01,000 --> 00:00:02,000\r\n第一句\r\n\r\n"
            "2\r\n\r\n00:00:03,000 --> 00:00:04,500\r\n第二句\r\n"
        )

        self.assertEqual([(cue.start, cue.end, cue.text) for cue in cues], [
            (1.0, 2.0, "第一句"),
            (3.0, 4.5, "第二句"),
        ])

    def test_srt_parser_still_rejects_files_without_any_timing(self) -> None:
        with self.assertRaisesRegex(ProjectionError, "has no timing lines"):
            parse_srt("1\n\n")

    def test_srt_parser_treats_loose_text_between_timings_as_cue_body(self) -> None:
        cues = parse_srt(
            "1\n00:00:01,000 --> 00:00:02,000\n第一行\n\n溶于清水的身体\n\n"
            "2\n00:00:03,000 --> 00:00:04,000\n第二行\n"
        )

        self.assertEqual(cues[0].text, "第一行\n溶于清水的身体")
        self.assertEqual(cues[1].text, "第二行")

    def test_final_projection_recovers_empty_srt_cue_from_annotated_translation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            final_srt = Path(temp) / "empty-cue.srt"
            final_srt.write_text(
                "1\n00:00:01,000 --> 00:00:03,000\n\n"
                "2\n00:00:04,000 --> 00:00:05,000\n回头见\n",
                encoding="utf-8",
            )

            document = project_edit_document(
                FIXTURES / "sample-stable.json",
                annotated_csv=FIXTURES / "sample-annotated.csv",
                final_srt=final_srt,
                video_id="video-1",
                title="Sample",
            )

        self.assertEqual(document["subtitles"][0]["ja"], "おはようございます")
        self.assertEqual(document["subtitles"][0]["zh"], "早上好")
        self.assertEqual(document["subtitles"][1]["zh"], "回头见")

    def test_stable_only_projection_preserves_words(self) -> None:
        document = project_edit_document(
            FIXTURES / "sample-stable.json", video_id="video-1", title="Sample"
        )
        self.assertEqual(document["projection"]["mode"], "stable")
        self.assertEqual(document["subtitles"][0]["ja"], "おはよう")
        self.assertEqual(document["subtitles"][0]["zh"], "")
        self.assertEqual(document["subtitles"][0]["words"][0]["word"], "おはよう")
        self.assertTrue(document["subtitles"][2]["low_conf"])

    def test_final_projection_merges_source_words_and_uses_final_timeline(self) -> None:
        document = project_edit_document(
            FIXTURES / "sample-stable.json",
            annotated_csv=FIXTURES / "sample-annotated.csv",
            final_srt=FIXTURES / "sample-final.srt",
            video_id="video-1",
            title="Sample",
        )
        first = document["subtitles"][0]
        self.assertEqual((first["t0"], first["t1"]), (1.0, 3.0))
        self.assertEqual(first["ja"], "おはようございます")
        self.assertEqual(first["zh"], "早上好")
        self.assertEqual([word["word"] for word in first["words"]], ["おはよう", "ございます"])
        self.assertTrue(document["subtitles"][1]["low_conf"])

    def test_row_count_mismatch_fails_instead_of_zipping(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            one_cue = Path(temp) / "one.srt"
            one_cue.write_text(
                "1\n00:00:01,000 --> 00:00:02,000\n一\n", encoding="utf-8"
            )
            with self.assertRaisesRegex(ProjectionError, "row counts differ"):
                project_edit_document(
                    FIXTURES / "sample-stable.json",
                    annotated_csv=FIXTURES / "sample-annotated.csv",
                    final_srt=one_cue,
                    video_id="video-1",
                    title="Sample",
                )

    def test_zero_duration_stable_segment_is_widened_instead_of_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            stable = Path(temp) / "collapsed-stable.json"
            stable.write_text(
                json.dumps(
                    {
                        "segments": [
                            {"id": "1", "start": 1.0, "end": 1.0, "text": "はい"},
                            {"id": "2", "start": 1.02, "end": 1.02, "text": "うん"},
                            {"id": "3", "start": 4.0, "end": 4.0, "text": "またね"},
                        ]
                    }
                ),
                encoding="utf-8",
            )

            document = project_edit_document(stable, video_id="video-1", title="Sample")

        timings = [(cue["t0"], cue["t1"]) for cue in document["subtitles"]]
        # The second segment starts before the floor would put the first one's
        # end, so the first stops there instead of swallowing it.
        self.assertEqual(timings, [(1.0, 1.02), (1.02, 1.06), (4.0, 4.04)])

    def test_zero_duration_final_srt_cue_is_widened_instead_of_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            final_srt = Path(temp) / "collapsed.srt"
            final_srt.write_text(
                "1\n00:00:01,000 --> 00:00:01,000\n早上好\n\n"
                "2\n00:00:04,000 --> 00:00:05,000\n再见\n",
                encoding="utf-8",
            )

            document = project_edit_document(
                FIXTURES / "sample-stable.json",
                annotated_csv=FIXTURES / "sample-annotated.csv",
                final_srt=final_srt,
                video_id="video-1",
                title="Sample",
            )

        first = document["subtitles"][0]
        self.assertEqual((first["t0"], first["t1"]), (1.0, 1.04))
        self.assertEqual(first["zh"], "早上好")

    def test_cloud_projection_recovers_from_unusable_final_srt(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            invalid_srt = Path(temp) / "invalid.srt"
            invalid_srt.write_text("溶于清水的身体\n", encoding="utf-8")
            document = project_edit_document(
                FIXTURES / "sample-stable.json",
                annotated_csv=FIXTURES / "sample-annotated.csv",
                final_srt=invalid_srt,
                video_id="video-1",
                title="Sample",
                relaxed_srt=True,
            )

        self.assertEqual(len(document["subtitles"]), 2)
        self.assertEqual((document["subtitles"][0]["t0"], document["subtitles"][0]["t1"]), (1.0, 3.0))
        self.assertEqual(document["subtitles"][0]["zh"], "早上好")
        self.assertEqual(document["subtitles"][1]["zh"], "再见")


class DocumentStoreTests(unittest.TestCase):
    def projection(self) -> dict:
        return project_edit_document(
            FIXTURES / "sample-stable.json", video_id="video-1", title="Sample"
        )

    def test_save_uses_revision_and_writes_history(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = DocumentStore(temp)
            created = store.create("video-1", self.projection(), artifacts={"schema": 1})
            saved = store.save(
                "video-1", expected_rev=created["rev"], subtitles=created["subtitles"]
            )
            self.assertEqual(saved["rev"], 1)
            self.assertTrue((Path(temp) / "video-1/history/00000000.json").is_file())
            self.assertTrue((Path(temp) / "video-1/original.json").is_file())
            with self.assertRaises(RevisionConflict):
                store.save("video-1", expected_rev=0, subtitles=[])

    def test_reprojection_preserves_manual_tracks(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = DocumentStore(temp)
            created = store.create("video-1", self.projection())
            track = {"id": "manual", "name": "Speaker", "segs": [{"t0": 1, "t1": 2, "ja": "x", "zh": ""}]}
            effects = [{
                "id": "fade-all", "templateId": "fade", "enabled": True,
                "target": {"scope": "all"}, "params": {"inMs": 200, "outMs": 200},
            }]
            store.save(
                "video-1",
                expected_rev=created["rev"],
                subtitles=created["subtitles"],
                tracks=[track],
                effects=effects,
            )
            replaced = store.create("video-1", self.projection(), replace_default=True)
            self.assertEqual(replaced["tracks"], [track])
            self.assertEqual(replaced["effects"], effects)
            self.assertEqual(replaced["rev"], 2)


if __name__ == "__main__":
    unittest.main()
