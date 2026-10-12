"""Speaker labels from FineSub 0.6.0's diarization, as the editor receives them.

The engine writes who is speaking onto `-stable.json` (a `speaker` object per
segment) and into the `spk` column of `-annotated.csv`. The editor files each
line under one speaker track; these tests pin how the labels are read, how a
projection is split, and that re-projecting replaces the generated tracks
instead of stacking a second copy beside them.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from nonoka_x.axis import GENERATED_TRACK_PREFIXES, conform_to_axis, split_diarized_tracks
from nonoka_x.document_store import DocumentStore
from nonoka_x.projector import parse_annotated, project_edit_document, speaker_id

HEADER_060 = "# type|position|duration|gap|corrected|translation|conf|char_count|spk|note"


def _stable(path: Path, speakers: list[object]) -> Path:
    segments = []
    for index, speaker in enumerate(speakers, start=1):
        segment = {"id": str(index), "start": float(index), "end": index + 0.8, "text": f"行{index}"}
        if speaker is not None:
            segment["speaker"] = speaker
        segments.append(segment)
    path.write_text(json.dumps({"segments": segments}, ensure_ascii=False), encoding="utf-8")
    return path


class SpeakerIdTests(unittest.TestCase):
    def test_cells_keep_only_the_lead_speaker(self) -> None:
        cases = {
            "s3": "s3",
            "s3?": "s3",
            "s3?(s5)": "s3",
            "s4*": "s4",
            "s0+s1": "s0",
            "s1?+s0": "s1",
            "s7<s3": "s7",
            "柊優花": "柊優花",
            "": "",
        }
        for cell, expected in cases.items():
            with self.subTest(cell=cell):
                self.assertEqual(speaker_id(cell), expected)

    def test_stable_objects_read_track_then_ranked_tracks_then_filled_mark(self) -> None:
        self.assertEqual(speaker_id({"labels": [], "track": 2, "block": 0}), "s2")
        self.assertEqual(speaker_id({"track": 2, "tracks": [5, 2]}), "s5")
        self.assertEqual(speaker_id({"labels": [], "mark": {"tier": "filled", "track": 4}}), "s4")
        self.assertEqual(
            speaker_id({"labels": [{"role": "guest", "conf": "high", "name": "戌神ころね"}], "track": 1}),
            "戌神ころね",
        )
        # A missing track is "not labelled", never a speaker of its own.
        self.assertEqual(speaker_id({"labels": [], "block": 0}), "")


class AnnotatedLayoutTests(unittest.TestCase):
    def test_060_header_places_spk_before_the_note(self) -> None:
        rows = parse_annotated(
            HEADER_060 + "\n"
            "sub|1|1.0|0.0|こんにちは|你好|high|2|s1?(s0)|note | with pipe\n"
        )
        self.assertEqual(rows[0]["spk"], "s1")
        self.assertEqual(rows[0]["translation"], "你好")

    def test_legacy_files_without_spk_still_parse(self) -> None:
        legacy = "sub|1|1.0|0.0|こんにちは|你好|high|2|a note\n"
        self.assertEqual(parse_annotated(legacy)[0]["spk"], "")
        headed = "# type|position|duration|gap|corrected|translation|conf|char_count|note\n" + legacy
        self.assertEqual(parse_annotated(headed)[0]["translation"], "你好")


class DiarizedProjectionTests(unittest.TestCase):
    def test_stable_projection_carries_speakers_and_splits_into_tracks(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            stable = _stable(Path(temp) / "x-stable.json", [
                {"labels": [], "track": 0}, {"labels": [], "track": 1},
                {"labels": [], "track": 0}, None,
            ])
            projection = project_edit_document(stable, video_id="v", title="v")
        self.assertEqual([item.get("spk") for item in projection["subtitles"]], ["s0", "s1", "s0", None])

        split = split_diarized_tracks(projection)
        # The busiest speaker keeps the main lane, together with unlabelled lines.
        self.assertEqual(split["track_meta"]["name"], "说话人 s0")
        self.assertEqual([item["ja"] for item in split["subtitles"]], ["行1", "行3", "行4"])
        self.assertEqual([track["id"] for track in split["tracks"]], ["diar1"])
        self.assertEqual(split["tracks"][0]["name"], "说话人 s1")
        self.assertEqual([item["ja"] for item in split["tracks"][0]["segs"]], ["行2"])
        # Unlike an editor-made track, a speaker's source lane is shown: a
        # source-only run has nothing else to export for them.
        self.assertFalse(split["tracks"][0]["ja"]["hidden"])
        self.assertFalse(split["track_meta"]["ja"]["hidden"])
        # The track is the speaker; nothing keeps the label on the line.
        self.assertFalse(any("spk" in item for item in split["subtitles"]))

    def test_final_projection_prefers_the_spk_cell_over_stable_labels(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            stable = _stable(root / "x-stable.json", [{"labels": [], "track": 0}, {"labels": [], "track": 0}])
            annotated = root / "x-annotated.csv"
            # The judge moved line 2 to s3 (`s3<s0`); line 1 has no cell.
            annotated.write_text(
                HEADER_060 + "\n"
                "sub|1|0.8|0.0|行1|第一行|high|3||\n"
                "sub|2|0.8|0.0|行2|第二行|high|3|s3<s0|\n",
                encoding="utf-8",
            )
            final = root / "x.srt"
            final.write_text(
                "1\n00:00:01,000 --> 00:00:01,800\n第一行\n\n2\n00:00:02,000 --> 00:00:02,800\n第二行\n",
                encoding="utf-8",
            )
            projection = project_edit_document(
                stable, annotated_csv=annotated, final_srt=final, video_id="v", title="v"
            )
        self.assertEqual([item.get("spk") for item in projection["subtitles"]], ["s0", "s3"])

    def test_one_speaker_is_not_split(self) -> None:
        projection = {"subtitles": [{"t0": 0.0, "t1": 1.0, "ja": "a", "spk": "s0"}], "tracks": []}
        split = split_diarized_tracks(projection)
        self.assertEqual(split["tracks"], [])
        self.assertEqual(split["subtitles"], [{"t0": 0.0, "t1": 1.0, "ja": "a"}])


class AxisPrecedenceTests(unittest.TestCase):
    PROJECTION = {
        "subtitles": [
            {"t0": 0.0, "t1": 1.0, "ja": "一", "zh": "", "spk": "s0"},
            {"t0": 1.0, "t1": 2.0, "ja": "二", "zh": "", "spk": "s1"},
        ],
        "tracks": [],
        "projection": {"mode": "stable"},
    }

    def test_an_axis_that_names_speakers_outranks_diarization(self) -> None:
        rows = [
            {"t0": 0.0, "t1": 1.0, "spk": "Aさん"},
            {"t0": 1.0, "t1": 2.0, "spk": "Bさん"},
            {"t0": 2.0, "t1": 3.0, "spk": "Aさん"},
        ]
        conformed = conform_to_axis(self.PROJECTION, rows)
        self.assertEqual(conformed["track_meta"]["name"], "Aさん")
        self.assertEqual([track["id"] for track in conformed["tracks"]], ["axis1"])

    def test_an_axis_without_speakers_takes_the_diarization_labels(self) -> None:
        rows = [{"t0": 0.0, "t1": 1.0, "spk": ""}, {"t0": 1.0, "t1": 2.0, "spk": ""}]
        conformed = conform_to_axis(self.PROJECTION, rows)
        self.assertEqual([track["id"] for track in conformed["tracks"]], ["diar1"])
        self.assertEqual([item["ja"] for item in conformed["subtitles"]], ["一"])


class GeneratedTrackReplacementTests(unittest.TestCase):
    def test_reprojection_replaces_generated_tracks_and_keeps_the_users(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = DocumentStore(temp)

            def projection(names: list[str]) -> dict:
                return {
                    "schema": 1, "title": "v", "source": "", "fp": None, "rev": 0,
                    "subtitles": [{"t0": 0.0, "t1": 1.0, "ja": "主", "zh": ""}],
                    "tracks": [
                        {"id": f"diar{index}", "name": name, "segs": [{"t0": 1.0, "t1": 2.0, "ja": name, "zh": ""}]}
                        for index, name in enumerate(names, start=1)
                    ],
                    "track_meta": {"name": "说话人 s0", "ja": {"hidden": False, "style": "origin"},
                                   "zh": {"hidden": False, "style": "cn"}},
                    "effects": [],
                    "projection": {"schema": 1, "mode": "stable"},
                }

            store.create("v", projection(["说话人 s1", "说话人 s2"]))
            document = store.read("v")
            document["tracks"].append({"id": "tabc", "name": "我的轨", "segs": []})
            document["track_meta"]["zh"]["style"] = "custom"
            store.save("v", expected_rev=document["rev"], subtitles=document["subtitles"],
                       tracks=document["tracks"], track_meta=document["track_meta"])

            replaced = store.create(
                "v", projection(["说话人 s1"]), replace_default=True,
                generated_track_prefixes=GENERATED_TRACK_PREFIXES,
            )
        self.assertEqual([track["id"] for track in replaced["tracks"]], ["tabc", "diar1"])
        # The person's lane styling survives; the name follows the new split.
        self.assertEqual(replaced["track_meta"]["zh"]["style"], "custom")
        self.assertEqual(replaced["track_meta"]["name"], "说话人 s0")


if __name__ == "__main__":
    unittest.main()
