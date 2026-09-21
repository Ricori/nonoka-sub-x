from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from nonoka_x.axis import (
    AxisError,
    axis_projection,
    axis_speakers,
    conform_to_axis,
    normalize_axis,
    split_speaker_tracks,
    stable_from_axis,
)
from nonoka_x.local_provider import LocalProvider, ProviderError
from nonoka_x.sidecar import SidecarServer


def row(t0: float, t1: float, ja: str = "", zh: str = "", spk: str = "") -> dict:
    return {"t0": t0, "t1": t1, "ja": ja, "zh": zh, "spk": spk}


class NormalizeTests(unittest.TestCase):
    def test_none_passes_through(self) -> None:
        self.assertIsNone(normalize_axis(None))

    def test_rows_are_sorted_and_defaulted(self) -> None:
        axis = normalize_axis({"kind": "empty", "rows": [{"t0": 3, "t1": 4}, {"t0": 1, "t1": 2}]})
        self.assertEqual([item["t0"] for item in axis["rows"]], [1.0, 3.0])
        self.assertEqual(axis["rows"][0], {"t0": 1.0, "t1": 2.0, "ja": "", "zh": "", "spk": ""})

    def test_unknown_kind_is_refused(self) -> None:
        with self.assertRaisesRegex(AxisError, "axis.kind"):
            normalize_axis({"kind": "srt", "rows": [row(0, 1)]})

    def test_zero_length_row_is_refused(self) -> None:
        with self.assertRaisesRegex(AxisError, "non-positive duration"):
            normalize_axis({"kind": "empty", "rows": [row(2, 2)]})

    def test_empty_row_list_is_refused(self) -> None:
        with self.assertRaisesRegex(AxisError, "empty"):
            normalize_axis({"kind": "empty", "rows": []})


class SpeakerTests(unittest.TestCase):
    def test_speakers_are_ordered_by_airtime(self) -> None:
        rows = [row(0, 1, spk="B"), row(1, 5, spk="A"), row(5, 6, spk="B")]
        self.assertEqual(axis_speakers(rows), ["A", "B"])

    def test_a_single_speaker_is_not_a_split(self) -> None:
        # One style is typesetting, not a cast: splitting on it would leave the
        # editor with one full track and one empty one.
        self.assertEqual(axis_speakers([row(0, 1, spk="A"), row(1, 2, spk="A")]), [])

    def test_unlabelled_rows_stay_with_the_busiest_speaker(self) -> None:
        rows = [row(0, 4, spk="A"), row(4, 5, spk="B"), row(5, 6)]
        segments = [{"t0": item["t0"], "t1": item["t1"], "ja": str(index), "zh": ""} for index, item in enumerate(rows)]
        primary, tracks, meta = split_speaker_tracks(segments, rows)
        self.assertEqual([item["ja"] for item in primary], ["0", "2"])
        self.assertEqual(len(tracks), 1)
        self.assertEqual(tracks[0]["name"], "B")
        self.assertEqual([item["ja"] for item in tracks[0]["segs"]], ["1"])
        self.assertEqual(meta["name"], "A")

    def test_split_tracks_show_the_translation_in_cn(self) -> None:
        rows = [row(0, 4, spk="A"), row(4, 5, spk="B")]
        segments = [{"t0": item["t0"], "t1": item["t1"], "ja": str(index), "zh": ""} for index, item in enumerate(rows)]
        _, tracks, _ = split_speaker_tracks(segments, rows)
        self.assertEqual(tracks[0]["zh"], {"hidden": False, "style": "cn"})
        self.assertEqual(tracks[0]["ja"], {"hidden": True, "style": "origin"})


class StableFromAxisTests(unittest.TestCase):
    def test_blank_rows_are_dropped_but_ids_stay_positional(self) -> None:
        rows = [row(0, 1, ja="一"), row(1, 2), row(2, 3, ja="三")]
        self.assertEqual(
            stable_from_axis(rows)["segments"],
            [
                {"id": "1", "start": 0.0, "end": 1.0, "text": "一"},
                {"id": "3", "start": 2.0, "end": 3.0, "text": "三"},
            ],
        )

    def test_an_axis_with_no_text_cannot_be_translated(self) -> None:
        with self.assertRaisesRegex(AxisError, "no source text"):
            stable_from_axis([row(0, 1)])


class ImportProjectionTests(unittest.TestCase):
    def test_bilingual_rows_become_a_final_document(self) -> None:
        rows = [row(0, 1, "こんにちは", "你好"), row(1, 2, "またね", "回见")]
        document = axis_projection(rows, kind="bi", video_id="loc_1", title="片子")
        self.assertEqual(document["projection"], {"schema": 1, "mode": "final"})
        self.assertEqual(document["subtitles"], [
            {"t0": 0.0, "t1": 1.0, "ja": "こんにちは", "zh": "你好"},
            {"t0": 1.0, "t1": 2.0, "ja": "またね", "zh": "回见"},
        ])
        self.assertEqual(document["tracks"], [])

    def test_an_unfinished_axis_cannot_be_imported(self) -> None:
        with self.assertRaisesRegex(AxisError, "cannot be imported"):
            axis_projection([row(0, 1)], kind="empty", video_id="loc_1", title="片子")


class ConformTests(unittest.TestCase):
    def stable(self, subtitles: list[dict]) -> dict:
        return {"subtitles": subtitles, "projection": {"schema": 1, "mode": "stable"}, "tracks": []}

    def test_every_axis_row_survives_with_its_own_timing(self) -> None:
        rows = [row(0, 1), row(1, 2), row(2, 3)]
        projection = self.stable([{"t0": 0.1, "t1": 1.9, "ja": "まとめ", "zh": "合并"}])
        conformed = conform_to_axis(projection, rows)
        self.assertEqual([(item["t0"], item["t1"]) for item in conformed["subtitles"]], [(0.0, 1.0), (1.0, 2.0), (2.0, 3.0)])
        self.assertEqual(conformed["subtitles"][2], {"t0": 2.0, "t1": 3.0, "ja": "", "zh": ""})

    def test_word_timings_split_a_recognized_span_across_lines(self) -> None:
        rows = [row(0, 1), row(1, 2)]
        projection = self.stable([{
            "t0": 0.0,
            "t1": 2.0,
            "ja": "前半後半",
            "zh": "整句",
            "words": [
                {"word": "前半", "start": 0.1, "end": 0.6},
                {"word": "後半", "start": 1.1, "end": 1.6},
            ],
        }])
        conformed = conform_to_axis(projection, rows)
        self.assertEqual([item["ja"] for item in conformed["subtitles"]], ["前半", "後半"])
        # The translation has no word timing to split on, so it stays whole on
        # the line the segment overlaps most rather than being cut at a guess.
        self.assertEqual([item["zh"] for item in conformed["subtitles"]], ["整句", ""])

    def test_corrected_text_is_never_rebuilt_from_raw_words(self) -> None:
        rows = [row(0, 1), row(1, 2)]
        projection = {
            "subtitles": [{
                "t0": 0.0,
                "t1": 1.2,
                "ja": "訂正済みの一行",
                "zh": "已纠错",
                "words": [{"word": "訂正", "start": 0.1, "end": 0.4}],
                "low_conf": True,
            }],
            "projection": {"schema": 1, "mode": "final"},
            "tracks": [],
        }
        conformed = conform_to_axis(projection, rows)
        self.assertEqual(conformed["subtitles"][0]["ja"], "訂正済みの一行")
        self.assertTrue(conformed["subtitles"][0]["low_conf"])
        self.assertEqual(conformed["subtitles"][1]["ja"], "")

    def test_text_outside_the_axis_lands_on_the_nearest_line(self) -> None:
        rows = [row(10, 11), row(20, 21)]
        projection = self.stable([
            {"t0": 1.0, "t1": 2.0, "ja": "前", "zh": ""},
            {"t0": 40.0, "t1": 41.0, "ja": "後", "zh": ""},
        ])
        conformed = conform_to_axis(projection, rows)
        self.assertEqual([item["ja"] for item in conformed["subtitles"]], ["前", "後"])

    def test_simultaneous_speech_cannot_be_split_without_diarization(self) -> None:
        # The engine has no diarization (`features.diarization=false` on both
        # providers), so a passage where two people talk at once comes back as
        # one mixed stream of words that say when they were said and nothing
        # about who said them. Overlapping axis rows both cover those words, and
        # seating them by time necessarily puts some on the wrong speaker.
        #
        # Pinned rather than fixed: any other tie-break is a different guess
        # wearing the costume of an improvement. `assAxis.parseAxisFile` counts
        # these rows so the dialog can warn that they need proofreading.
        rows = [row(0, 4, spk="A"), row(3, 5, spk="B")]
        projection = self.stable([
            {"t0": 0.2, "t1": 3.8, "ja": "AB", "zh": "", "words": [
                {"word": "A", "start": 0.2, "end": 2.0},
                {"word": "B", "start": 3.2, "end": 3.8},
            ]},
        ])
        conformed = conform_to_axis(projection, rows)
        # Both words land on the row that starts first; B's line comes back
        # empty rather than holding the word spoken inside its own span.
        self.assertEqual([item["ja"] for item in conformed["subtitles"]], ["AB"])
        self.assertEqual([item["ja"] for item in conformed["tracks"][0]["segs"]], [""])

    def test_an_axis_without_speakers_stays_one_lane(self) -> None:
        # No diarization and nothing in the axis to stand in for it: the result
        # is exactly what an ordinary transcription produces, which is the
        # no-regression case.
        rows = [row(0, 1), row(1, 2)]
        conformed = conform_to_axis(self.stable([{"t0": 0.1, "t1": 1.9, "ja": "せりふ", "zh": ""}]), rows)
        self.assertEqual(conformed["tracks"], [])
        self.assertEqual(len(conformed["subtitles"]), 2)

    def test_speaker_rows_become_tracks(self) -> None:
        rows = [row(0, 4, spk="A"), row(4, 5, spk="B")]
        projection = self.stable([
            {"t0": 0.0, "t1": 4.0, "ja": "主", "zh": ""},
            {"t0": 4.0, "t1": 5.0, "ja": "客", "zh": ""},
        ])
        conformed = conform_to_axis(projection, rows)
        self.assertEqual([item["ja"] for item in conformed["subtitles"]], ["主"])
        self.assertEqual(conformed["tracks"][0]["name"], "B")
        self.assertEqual(conformed["track_meta"]["name"], "A")


class ProviderAxisTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "video.mp4"
        self.source.write_bytes(b"fixture")
        self.provider = LocalProvider(self.root / "tasks", ROOT / "third_party/finesub", issues=[])

    def tearDown(self) -> None:
        self.provider.shutdown()
        self.temp.cleanup()

    def request(self, **overrides) -> dict:
        request = {
            "schema": 1,
            "provider": "local",
            "source": {"kind": "local_file", "path": str(self.source), "title": "片子"},
            "target": "final-srt",
            "language": "ja",
            "device": "cuda",
            "gpu_budget_gb": 8,
            "correction": {"enabled": True, "media": "text"},
            "knowledge": "none",
            "cleanup_intermediate": False,
        }
        request.update(overrides)
        return request

    def test_axis_round_trips_through_the_document_directory(self) -> None:
        axis = {"kind": "empty", "filename": "轴.ass", "rows": [row(0, 1)]}
        self.provider.set_document_axis("loc_1", {"axis": axis})
        self.assertEqual(self.provider.document_axis("loc_1")["axis"]["kind"], "empty")
        self.provider.set_document_axis("loc_1", {"axis": None})
        self.assertIsNone(self.provider.document_axis("loc_1")["axis"])

    def test_finished_axes_do_not_start_a_task(self) -> None:
        from nonoka_x.local_provider import validate_request

        with self.assertRaisesRegex(ProviderError, "does not start a task"):
            validate_request(self.request(axis={"kind": "bi", "rows": [row(0, 1, "あ", "啊")]}))

    def test_a_source_text_axis_needs_the_final_target(self) -> None:
        from nonoka_x.local_provider import validate_request

        with self.assertRaisesRegex(ProviderError, "final-srt"):
            validate_request(self.request(target="raw-srt", axis={"kind": "ja", "rows": [row(0, 1, "あ")]}))

    def test_a_source_text_axis_reaches_the_worker(self) -> None:
        from nonoka_x.local_provider import validate_request

        validated = validate_request(self.request(axis={"kind": "ja", "rows": [row(0, 1, "あ")]}))
        self.assertEqual(validated["axis"]["kind"], "ja")
        self.assertEqual(validated["axis"]["rows"][0]["ja"], "あ")

    def test_import_writes_an_editable_document_and_clears_the_axis(self) -> None:
        self.provider.set_document_axis("loc_1", {"axis": {"kind": "bi", "rows": [row(0, 1, "あ", "啊")]}})
        document = self.provider.import_axis({
            "video_id": "loc_1",
            "title": "片子",
            "source_path": str(self.source),
            "axis": {"kind": "bi", "rows": [row(0, 1, "あ", "啊"), row(1, 2, "い", "咦")]},
        })
        self.assertEqual(len(document["subtitles"]), 2)
        self.assertEqual(self.provider.document("loc_1")["subtitles"][1]["zh"], "咦")
        # A finished import has nothing left to reshape; leaving the axis behind
        # would silently re-seat the next run on this video.
        self.assertIsNone(self.provider.document_axis("loc_1")["axis"])

    def test_projection_lands_on_a_recorded_axis(self) -> None:
        stable = self.root / "tasks" / "fixture-stable.json"
        stable.write_text(
            '{"segments":[{"id":"1","start":0.1,"end":1.8,"text":"認識結果","words":[]}]}',
            encoding="utf-8",
        )
        self.provider.set_document_axis("loc_1", {"axis": {"kind": "empty", "rows": [row(0, 1), row(1, 2)]}})
        manifest = {"schema": 1, "artifacts": {"stable_json": {"uri": stable.resolve().as_uri()}}}
        document = self.provider._project_manifest("loc_1", {"title": "片子"}, manifest)
        self.assertEqual([(item["t0"], item["t1"]) for item in document["subtitles"]], [(0.0, 1.0), (1.0, 2.0)])
        self.assertEqual(document["subtitles"][0]["ja"], "認識結果")




class SidecarAxisRouteTests(unittest.TestCase):
    """The axis surface is new routing, and nothing else exercises it."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.provider = LocalProvider(self.root / "tasks", ROOT / "third_party/finesub", issues=[])
        try:
            self.server = SidecarServer(("127.0.0.1", 0), self.provider, "secret-token")
        except PermissionError:
            self.provider.shutdown()
            self.temp.cleanup()
            self.skipTest("sandbox does not permit a loopback listener")
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.provider.shutdown()
        self.temp.cleanup()

    def call(self, method: str, path: str, body: dict | None = None) -> dict:
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.server.server_port}{path}",
            method=method,
            data=None if body is None else json.dumps(body).encode("utf-8"),
            headers={"Authorization": "Bearer secret-token", "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            return json.load(response)

    def test_axis_and_import_routes_round_trip(self) -> None:
        axis = {"kind": "bi", "filename": "轴.ass", "rows": [row(0, 1, "あ", "啊")]}
        self.assertEqual(self.call("PUT", "/v1/documents/loc_1/axis", {"axis": axis})["axis"]["kind"], "bi")
        self.assertEqual(self.call("GET", "/v1/documents/loc_1/axis")["axis"]["rows"][0]["zh"], "啊")
        document = self.call("POST", "/v1/documents/import", {"video_id": "loc_1", "title": "片子", "axis": axis})
        self.assertEqual(document["subtitles"], [{"t0": 0.0, "t1": 1.0, "ja": "あ", "zh": "啊"}])
        self.assertEqual(self.call("GET", "/v1/documents/loc_1")["video_id"], "loc_1")

    def test_a_malformed_axis_is_refused_by_code(self) -> None:
        with self.assertRaises(urllib.error.HTTPError) as raised:
            self.call("PUT", "/v1/documents/loc_1/axis", {"axis": {"kind": "empty", "rows": [row(2, 2)]}})
        self.assertEqual(raised.exception.code, 400)
        self.assertEqual(json.load(raised.exception)["error"]["code"], "invalid_axis")


if __name__ == "__main__":
    unittest.main()
