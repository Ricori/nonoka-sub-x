from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "third_party" / "finesub" / "src"))

from finesub.speech.diarization.judgments import Inserted, Judgments
from finesub.subtitles.stable_rows import lines_hash
from nonoka_x.document_store import DocumentError, DocumentStore, RevisionConflict
from nonoka_x.projector import ProjectionError, hide_source_lanes, parse_srt, project_edit_document


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

    @staticmethod
    def _split_stable(directory: Path) -> Path:
        """A stable of two lines whose first is cut by acoustic row splitting."""

        def word(text: str, start: float, end: float) -> dict[str, object]:
            return {"word": text, "start": start, "end": end}

        segments = [
            {"id": "1", "start": 0.0, "end": 2.0, "text": "AB",
             "words": [word("A", 0.0, 1.0), word("B", 1.0, 2.0)]},
            {"id": "2", "start": 3.0, "end": 4.0, "text": "C", "words": [word("C", 3.0, 4.0)]},
        ]
        stable = directory / "task-stable.json"
        stable.write_text(json.dumps({"segments": segments}), encoding="utf-8")
        (directory / "task-diar-rows.json").write_text(
            json.dumps({
                "version": 1,
                "stable": lines_hash(segments),
                "speaker": {},
                "split": {"chosen": True, "version": 1, "cuts": 1},
                "rows": [
                    {"split": {"at": 1, "speakers": [{"track": 0}, {"track": 1}]}},
                    {"speaker": {"track": 0}},
                ],
            }),
            encoding="utf-8",
        )
        return stable

    def test_acoustic_split_rows_and_labels_come_from_the_rows_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            stable = self._split_stable(Path(temporary))
            document = project_edit_document(stable, video_id="video-1", title="Sample")
        self.assertEqual(
            [(item["ja"], item["t0"], item["t1"], item.get("spk")) for item in document["subtitles"]],
            [("A", 0.0, 1.0, "s0"), ("B", 1.0, 2.0, "s1"), ("C", 3.0, 4.0, "s0")],
        )

    def test_annotated_ids_count_the_split_rows(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            stable = self._split_stable(directory)
            annotated = directory / "task-annotated.csv"
            annotated.write_text(
                "# type|position|duration|gap|corrected|translation|conf|char_count|note\n"
                "sub|2|1.0|0.0|B|乙|high|1|\n"
                "sub|3|1.0|1.0|C|丙|high|1|\n",
                encoding="utf-8",
            )
            final = directory / "task.srt"
            final.write_text(
                "1\n00:00:01,000 --> 00:00:02,000\n乙\n\n2\n00:00:03,000 --> 00:00:04,000\n丙\n",
                encoding="utf-8",
            )
            document = project_edit_document(
                stable, annotated_csv=annotated, final_srt=final, video_id="video-1", title="Sample"
            )
        self.assertEqual([item["words"][0]["word"] for item in document["subtitles"]], ["B", "C"])
        self.assertEqual([item["spk"] for item in document["subtitles"]], ["s1", "s0"])

    def test_a_judges_second_half_and_inserted_line_have_ids(self) -> None:
        judgments = Judgments(
            moved={},
            halves={"1": (1, 1)},
            inserts=(Inserted(after="2", text="X", track=1, heard=(4.0, 5.0), shown=(4.0, 5.0)),),
        )
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            segments = [
                {"id": "1", "start": 0.0, "end": 2.0, "text": "AB", "speaker": {"track": 0},
                 "words": [{"word": "A", "start": 0.0, "end": 1.0}, {"word": "B", "start": 1.0, "end": 2.0}]},
                {"id": "2", "start": 3.0, "end": 4.0, "text": "C", "speaker": {"track": 0},
                 "words": [{"word": "C", "start": 3.0, "end": 4.0}]},
            ]
            stable = directory / "task-stable.json"
            stable.write_text(json.dumps({"segments": segments}), encoding="utf-8")
            annotated = directory / "task-annotated.csv"
            annotated.write_text(
                "# type|position|duration|gap|corrected|translation|conf|char_count|note\n"
                "sub|1b|1.0|0.0|B|乙|high|1|\n"
                "sub|2i|1.0|1.0|X|丁|high|1|\n",
                encoding="utf-8",
            )
            final = directory / "task.srt"
            final.write_text(
                "1\n00:00:01,000 --> 00:00:02,000\n乙\n\n2\n00:00:04,000 --> 00:00:05,000\n丁\n",
                encoding="utf-8",
            )
            with mock.patch("finesub.speech.diarization.product.read_judgments", return_value=judgments):
                document = project_edit_document(
                    stable, annotated_csv=annotated, final_srt=final, video_id="video-1", title="Sample"
                )
        self.assertEqual([item["zh"] for item in document["subtitles"]], ["乙", "丁"])
        self.assertEqual(document["subtitles"][0]["words"][0]["word"], "B")
        self.assertNotIn("words", document["subtitles"][1])

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

    def correct_only_run(self, temp: str, *, recorded: bool = True) -> dict[str, Path]:
        """A Chinese run as the engine leaves it: the filler line is in the annotated rows, not the subtitle."""

        root = Path(temp)
        stable = root / "zh-stable.json"
        stable.write_text(json.dumps({"segments": [
            {"start": 1.0, "end": 2.0, "text": "就是那个那个新角色"},
            {"start": 2.4, "end": 2.6, "text": "呃"},
            {"start": 3.0, "end": 4.0, "text": "下期再讲"},
        ]}, ensure_ascii=False), encoding="utf-8")
        annotated = root / "zh-annotated.csv"
        annotated.write_text(
            "# type|position|duration|gap|corrected|translation|conf|char_count|spk|note\n"
            "sub|1|1.0||就是那个那个新角色|就是那个新角色|high|7||\n"
            "sub|2|0.2|0.4|呃||high|0||整行只有垫词\n"
            "sub|3|1.0|0.4|下期再讲|下期再讲|high|4||\n",
            encoding="utf-8",
        )
        final = root / "zh.srt"
        final.write_text(
            "1\n00:00:01,000 --> 00:00:02,300\n就是那个新角色\n\n"
            "2\n00:00:03,000 --> 00:00:04,300\n下期再讲\n",
            encoding="utf-8",
        )
        mode = root / "correction-mode.json"
        if recorded:
            mode.write_text('{"version": 1, "mode": "single", "proofread": false, "correct_only": true}', encoding="utf-8")
        return {"stable": stable, "annotated": annotated, "final": final, "mode": mode}

    def project_run(self, run: dict[str, Path]) -> dict:
        return project_edit_document(
            run["stable"],
            annotated_csv=run["annotated"],
            final_srt=run["final"],
            correction_mode=run["mode"],
            video_id="video-1",
            title="中文",
        )

    def test_a_filler_row_keeps_its_transcript_without_taking_a_cue(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            document = self.project_run(self.correct_only_run(temp))

        self.assertEqual(
            [(row["t0"], row["t1"], row["ja"], row["zh"]) for row in document["subtitles"]],
            [
                (1.0, 2.3, "就是那个那个新角色", "就是那个新角色"),
                (2.4, 2.6, "呃", ""),
                (3.0, 4.3, "下期再讲", "下期再讲"),
            ],
        )
        self.assertEqual(document["projection"], {"schema": 1, "mode": "final", "correct_only": True})

    def test_a_task_without_the_correct_only_record_stays_a_translation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            document = self.project_run(self.correct_only_run(temp, recorded=False))

        self.assertEqual(document["projection"], {"schema": 1, "mode": "final"})
        self.assertFalse(document["track_meta"]["ja"]["hidden"])

    def test_hiding_source_lanes_covers_the_speaker_tracks_and_keeps_styles(self) -> None:
        hidden = hide_source_lanes({
            "track_meta": {"name": "A", "ja": {"hidden": False, "style": "origin"}, "zh": {"hidden": False, "style": "cn"}},
            "tracks": [{"id": "d1", "ja": {"hidden": False, "style": "x"}, "zh": {"hidden": False, "style": "cn"}}],
        })

        self.assertEqual(hidden["track_meta"]["ja"], {"hidden": True, "style": "origin"})
        self.assertFalse(hidden["track_meta"]["zh"]["hidden"])
        self.assertEqual(hidden["tracks"][0]["ja"], {"hidden": True, "style": "x"})
        self.assertFalse(hidden["tracks"][0]["zh"]["hidden"])

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

    def meta(self, *, hidden: bool, style: str = "origin") -> dict:
        return {"name": "默认轨", "ja": {"hidden": hidden, "style": style}, "zh": {"hidden": False, "style": "cn"}}

    def test_switching_to_a_correct_only_run_hides_the_source_lane_but_keeps_its_style(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = DocumentStore(temp)
            store.create("video-1", {**self.projection(), "track_meta": self.meta(hidden=False, style="mine")})
            replaced = store.create(
                "video-1",
                {
                    **self.projection(),
                    "projection": {"schema": 1, "mode": "final", "correct_only": True},
                    "track_meta": self.meta(hidden=True),
                },
                replace_default=True,
                generated_track_prefixes=("d",),
            )

        self.assertEqual(replaced["track_meta"]["ja"], {"hidden": True, "style": "mine"})

    def test_rerunning_the_same_kind_of_task_keeps_what_the_person_chose(self) -> None:
        correct_only = {"schema": 1, "mode": "final", "correct_only": True}
        with tempfile.TemporaryDirectory() as temp:
            store = DocumentStore(temp)
            store.create("video-1", {**self.projection(), "projection": correct_only, "track_meta": self.meta(hidden=False)})
            replaced = store.create(
                "video-1",
                {**self.projection(), "projection": correct_only, "track_meta": self.meta(hidden=True)},
                replace_default=True,
                generated_track_prefixes=("d",),
            )

        self.assertFalse(replaced["track_meta"]["ja"]["hidden"])


if __name__ == "__main__":
    unittest.main()
