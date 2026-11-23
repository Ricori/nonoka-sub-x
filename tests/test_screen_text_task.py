"""The on-screen text task: its request, its translation, the tracks it becomes, and how they are appended.

The worker side is stood in for (a script that writes `screen_text.json` and
says it is done); what is checked is the core's part: the request it accepts,
that it never projects over the document, that its tracks are added beside the
default track and the user's tracks without touching them, `artifacts.json`
or the cloud's view, and that a later transcription keeps them.
"""

from __future__ import annotations

import json
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from typing import Any

from nonoka_x import screen_text
from nonoka_x.document_store import DocumentStore
from nonoka_x.local_provider import LocalProvider, ProviderError, validate_request

ROOT = Path(__file__).resolve().parents[1]


def ocr_document() -> dict[str, Any]:
    return {
        "schema": 1,
        "engine": {"id": "flowocr"},
        "video": {"w": 1920, "h": 1080, "fps": 30.0, "duration": 20.0},
        "range": {"start": 0.0, "end": 0.0},
        "regions": [
            {"id": "r00", "label": "dialogue-or-caption", "name": "画面·对白", "box": [0.7, 0.1, 0.95, 0.4], "skip": False},
            {"id": "r01", "label": "clock", "name": "画面·时间", "box": [0.0, 0.0, 0.1, 0.05], "skip": True},
        ],
        "cues": [
            {"id": "a", "region": "r00", "t0": 1.0, "t1": 3.0, "lines": [{"text": "限定ガチャ", "box": [0.7, 0.1, 0.9, 0.15], "conf": 0.9}],
             "box": [0.7, 0.1, 0.9, 0.15], "an": 9, "fs": 0.05, "fill": "#102030"},
            {"id": "b", "region": "r00", "t0": 2.0, "t1": 4.0, "lines": [{"text": "本日まで", "box": [0.7, 0.2, 0.85, 0.25], "conf": 0.9}],
             "box": [0.7, 0.2, 0.85, 0.25], "an": 9, "fs": 0.05, "fill": None},
            {"id": "c", "region": "r00", "t0": 5.0, "t1": 6.0, "lines": [{"text": "ありがとう", "box": [0.7, 0.1, 0.9, 0.15], "conf": 0.9}],
             "box": [0.7, 0.1, 0.9, 0.15], "an": 8, "fs": 0.05, "fill": None, "matched": {"cn": "谢谢"}},
            {"id": "d", "region": "r01", "t0": 0.0, "t1": 20.0, "lines": [{"text": "12:00", "box": [0.0, 0.0, 0.1, 0.05], "conf": 0.9}],
             "box": [0.0, 0.0, 0.1, 0.05], "an": 7, "fs": 0.03, "fill": None},
        ],
        "options": {"cover": "plate"},
    }


class OptionsTests(unittest.TestCase):
    def test_defaults(self) -> None:
        options = screen_text.validate_options(None)
        self.assertEqual((options["translate"], options["cover"], options["on_edited"]), ("zh", "plate", "replace"))
        self.assertFalse(options["skip_main"])
        self.assertFalse(options["trial"])
        self.assertIsNone(options["range"])

    def test_bad_values_are_refused(self) -> None:
        for bad in ({"translate": "en"}, {"cover": "blur"}, {"range": {"start": 5, "end": 2}}, {"trial": "yes"},
                    {"roi": [0.5, 0.5, 0.2, 0.9]}, {"match": {"game": ""}}):
            with self.subTest(bad=bad), self.assertRaises(screen_text.ScreenTextError):
                screen_text.validate_options(bad)

    def test_speech_lines_come_from_the_default_track(self) -> None:
        lines = screen_text.speech_lines({"subtitles": [{"t0": 1, "t1": 2, "ja": "こんにちは", "zh": "你好"},
                                                         {"t0": 3, "t1": 4, "ja": "", "zh": ""}]})
        self.assertEqual(lines, [{"t0": 1.0, "t1": 2.0, "text": "こんにちは / 你好"}])


class TranslationTests(unittest.TestCase):
    def test_script_text_first_then_the_model_and_a_missing_answer_asked_again(self) -> None:
        document = ocr_document()
        calls: list[list[str]] = []

        def complete(messages: list[dict[str, str]]) -> str:
            body = json.loads(messages[1]["content"])
            ids = [item["id"] for item in body["items"]]
            calls.append(ids)
            if len(calls) == 1:
                return "```json\n" + json.dumps({"a": "限定扭蛋"}, ensure_ascii=False) + "\n```"
            return json.dumps({"b": "今天截止"}, ensure_ascii=False)

        screen_text.translate(document, [{"t0": 1.5, "t1": 2.5, "text": "今日まで！"}], complete)
        by_id = {cue["id"]: cue for cue in document["cues"]}
        self.assertEqual(calls, [["a", "b"], ["b"]], "the clock is not sent; the matched cue is not either")
        self.assertEqual((by_id["a"]["zh"], by_id["b"]["zh"], by_id["c"]["zh"]), ("限定扭蛋", "今天截止", "谢谢"))
        self.assertNotIn("zh", by_id["d"])

    def test_the_prompt_carries_the_speech_around_the_cues(self) -> None:
        messages = screen_text.batch_messages(ocr_document()["cues"][:1], [{"t0": 1.0, "t1": 2.0, "text": "x"},
                                                                          {"t0": 100.0, "t1": 101.0, "text": "far"}],
                                              {"r00": "画面·对白"})
        body = json.loads(messages[1]["content"])
        self.assertEqual(body["items"][0]["where"], "画面·对白")
        self.assertEqual(body["speech"], ["[1s] x"])

    def test_an_answer_that_is_not_json_is_nothing(self) -> None:
        self.assertEqual(screen_text.parse_answer("对不起"), {})


class TrackTests(unittest.TestCase):
    def test_single_track_holds_all_cues_including_overlaps(self) -> None:
        document = ocr_document()
        for cue in document["cues"]:
            cue.setdefault("zh", "译")
        tracks = screen_text.tracks_from(document, task_id="t1")
        self.assertEqual([track["id"] for track in tracks], ["ocr"])
        self.assertEqual([len(track["segs"]) for track in tracks], [3])
        first = tracks[0]
        self.assertEqual(first["name"], screen_text.STYLE_NAME)
        self.assertTrue(first["ja"]["hidden"])
        self.assertEqual(first["zh"]["style"], screen_text.STYLE_NAME)
        self.assertEqual(first["source"]["kind"], "screen-text")
        seg = first["segs"][0]
        # one line: anchored mid-box on the same side
        self.assertEqual(seg["pos"], {"x": 0.9, "y": 0.125, "an": 6, "w": 0.2, "fs": 0.05})
        self.assertEqual(seg["cover"], {"box": [0.7, 0.1, 0.9, 0.15], "fill": "#102030"})
        # Single position group uses the base style without per-segment override
        self.assertNotIn("style", seg)

    def test_different_positions_stay_on_one_track_with_distinct_styles(self) -> None:
        document = ocr_document()
        document["regions"].append(
            {"id": "r02", "label": "misc", "name": "画面·文字识别", "box": [0.05, 0.78, 0.35, 0.9], "skip": False},
        )
        document["cues"].append(
            {"id": "e", "region": "r02", "t0": 1.5, "t1": 3.5, "lines": [{"text": "左下角", "box": [0.05, 0.8, 0.3, 0.88], "conf": 0.95}],
             "box": [0.05, 0.8, 0.3, 0.88], "an": 7, "fs": 0.04, "fill": "#000000", "zh": "左下角"},
        )
        tracks = screen_text.tracks_from(document, task_id="t2")
        self.assertEqual([track["id"] for track in tracks], ["ocr"])
        self.assertEqual(len(tracks[0]["segs"]), 4)
        styles_by_ja = {seg["ja"]: seg.get("style", {}).get("zh") for seg in tracks[0]["segs"]}
        self.assertEqual(styles_by_ja["限定ガチャ"], f"{screen_text.STYLE_NAME}-右上")
        self.assertEqual(styles_by_ja["左下角"], f"{screen_text.STYLE_NAME}-左下")

        styles_text = screen_text.styles_from(document)
        self.assertIn(f"Style: {screen_text.STYLE_NAME},", styles_text)
        self.assertIn(f"Style: {screen_text.STYLE_NAME}-右上,", styles_text)
        self.assertIn(f"Style: {screen_text.STYLE_NAME}-左下,", styles_text)

        # Merging never overwrites an existing user-customized style
        custom = f"[V4+ Styles]\n{screen_text.ASS_STYLE_FORMAT}\nStyle: {screen_text.STYLE_NAME}-右上,微软雅黑,88,&H0000FFFF,&H000000FF,&H00000000,&H00000000,1,0,0,0,100,100,0,0,1,2,0,9,12,12,12,1\n"
        merged = screen_text.merge_missing_styles(custom, styles_text)
        self.assertIn("微软雅黑,88", merged)
        self.assertIn(f"Style: {screen_text.STYLE_NAME}-左下,", merged)
        self.assertEqual(merged.count(f"Style: {screen_text.STYLE_NAME}-右上,"), 1)

    def test_stacked_lines_carry_the_original_pitch(self) -> None:
        document = ocr_document()
        cue = document["cues"][0]
        cue["lines"] = [{"text": "一行目", "box": [0.2, 0.80, 0.5, 0.84], "conf": 0.9},
                        {"text": "二行目", "box": [0.2, 0.854, 0.45, 0.894], "conf": 0.9}]
        cue["box"], cue["an"], cue["fs"] = [0.2, 0.80, 0.5, 0.894], 7, 0.04
        seg = screen_text.tracks_from(document)[0]["segs"][0]
        self.assertEqual(seg["pos"], {"x": 0.2, "y": 0.8, "an": 7, "w": 0.3, "fs": 0.04, "lh": 0.054})

    def test_noise_answers_are_dropped_and_unanswered_ones_show_the_source(self) -> None:
        document = ocr_document()
        document["options"]["translate"] = "zh"

        def complete(messages: list[dict[str, str]]) -> str:
            return json.dumps({"a": ""})

        screen_text.translate(document, [], complete)
        by_id = {cue["id"]: cue for cue in document["cues"]}
        self.assertEqual(by_id["a"]["zh"], "")
        self.assertEqual(by_id["b"]["zh"], "本日まで")
        self.assertTrue(by_id["b"]["untranslated"])
        segs = screen_text.tracks_from(document)[0]["segs"]
        self.assertEqual([seg["ja"] for seg in segs], ["本日まで", "ありがとう"])

    def test_untranslated_runs_show_the_source_lane(self) -> None:
        document = ocr_document()
        document["options"]["translate"] = "none"
        track = screen_text.tracks_from(document)[0]
        self.assertFalse(track["ja"]["hidden"])
        self.assertTrue(track["zh"]["hidden"])

    def test_no_plate_keeps_the_box_for_erasing(self) -> None:
        document = ocr_document()
        document["options"]["cover"] = "none"
        seg = screen_text.tracks_from(document)[0]["segs"][0]
        self.assertEqual(seg["cover"], {"box": [0.7, 0.1, 0.9, 0.15], "fill": None})

    def test_an_edit_is_seen_and_a_round_trip_is_not(self) -> None:
        track = screen_text.tracks_from(ocr_document())[0]
        self.assertFalse(screen_text.edited(track))
        track["segs"][0]["style"] = {"zh": "cn"}
        track["segs"][0]["t0"] = track["segs"][0]["t0"] + 0.001
        self.assertFalse(screen_text.edited(track), "a style override or a rounding is not an edit")
        track["segs"][0]["zh"] = "人改过"
        self.assertTrue(screen_text.edited(track))


class DocumentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.store = DocumentStore(Path(self.temp.name) / "documents")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def document(self) -> dict[str, Any]:
        return {"schema": 1, "subtitles": [{"t0": 0, "t1": 1, "ja": "あ", "zh": "啊"}],
                "tracks": [{"id": "t1", "name": "我的轨", "segs": []}, {"id": "diar1", "name": "说话人", "segs": []}],
                "track_meta": {"name": "默认轨"}, "effects": [{"x": 1}], "styles": "[V4+ Styles]"}

    def test_the_tracks_are_added_beside_everything_else(self) -> None:
        self.store.create("v1", self.document(), artifacts={"artifacts": {"transcript_json": {}}})
        tracks = screen_text.tracks_from(ocr_document())
        updated = self.store.replace_generated_tracks("v1", "ocr", tracks)
        self.assertEqual([t["id"] for t in updated["tracks"]], ["t1", "diar1", "ocr"])
        self.assertEqual(updated["subtitles"], self.document()["subtitles"])
        self.assertEqual((updated["effects"], updated["styles"]), ([{"x": 1}], "[V4+ Styles]"))
        self.assertEqual(updated["rev"], 1)
        artifacts = json.loads((self.store.directory("v1") / "artifacts.json").read_text(encoding="utf-8"))
        self.assertEqual(artifacts, {"artifacts": {"transcript_json": {}}}, "the cloud's view is untouched")

        again = self.store.replace_generated_tracks("v1", "ocr", tracks)
        self.assertEqual([t["id"] for t in again["tracks"]], ["t1", "diar1", "ocr"], "a re-run replaces them")

    def test_edited_tracks_are_kept_as_the_users_when_asked(self) -> None:
        self.store.create("v1", self.document())
        tracks = screen_text.tracks_from(ocr_document())
        current = self.store.replace_generated_tracks("v1", "ocr", tracks)
        current["tracks"][2]["segs"][0]["zh"] = "人改过"
        self.store.save("v1", expected_rev=current["rev"], subtitles=current["subtitles"], tracks=current["tracks"])
        updated = self.store.replace_generated_tracks("v1", "ocr", tracks, keep=screen_text.edited)
        ids = [t["id"] for t in updated["tracks"]]
        self.assertEqual(ids, ["t1", "diar1", "tocr", "ocr"])
        self.assertNotIn("source", updated["tracks"][2])

    def test_a_video_without_a_document_gets_one(self) -> None:
        updated = self.store.replace_generated_tracks("v2", "ocr", screen_text.tracks_from(ocr_document()),
                                                      skeleton={"schema": 1, "subtitles": [], "tracks": []})
        self.assertEqual(updated["subtitles"], [])
        self.assertEqual(len(updated["tracks"]), 1)

    def test_a_later_transcription_keeps_them(self) -> None:
        from nonoka_x.axis import GENERATED_TRACK_PREFIXES

        self.store.create("v1", self.document())
        self.store.replace_generated_tracks("v1", "ocr", screen_text.tracks_from(ocr_document()))
        projected = self.store.create("v1", {"schema": 1, "subtitles": [], "tracks": [{"id": "diar1", "segs": []}]},
                                      replace_default=True, generated_track_prefixes=GENERATED_TRACK_PREFIXES)
        self.assertEqual([t["id"] for t in projected["tracks"]], ["t1", "ocr", "diar1"])


FAKE_WORKER = textwrap.dedent('''
    import hashlib, json, sys
    from pathlib import Path
    task_dir = Path(sys.argv[sys.argv.index("--task-dir") + 1])
    request = json.loads(sys.stdin.readline())
    document = json.loads(Path(request["source"]["path"]).with_name("ocr.json").read_text(encoding="utf-8"))
    document["options"] = {"cover": request["screen_text"]["cover"]}
    (task_dir / "seen-request.json").write_text(json.dumps(request, ensure_ascii=False), encoding="utf-8")
    out = task_dir / "screen_text.json"
    out.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    data = out.read_bytes()
    print(json.dumps({"type": "stage", "payload": {"stage": "screen-read"}}), flush=True)
    print(json.dumps({"type": "completed", "payload": {"artifacts": {"schema": 1, "task_id": "x", "artifacts": {
        "screen_text_json": {"uri": out.resolve().as_uri(), "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}}}}}), flush=True)
''')


def wait(provider: LocalProvider, task_id: str, states: set[str], timeout: float = 20.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = provider.status(task_id)
        if status["state"] in states:
            return status
        time.sleep(0.05)
    raise AssertionError(f"task stayed {provider.status(task_id)['state']}")


class ProviderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "video.mp4"
        self.source.write_bytes(b"fixture")
        (self.root / "ocr.json").write_text(json.dumps(ocr_document(), ensure_ascii=False), encoding="utf-8")
        worker = self.root / "fake_screen_text_worker.py"
        worker.write_text(FAKE_WORKER, encoding="utf-8")
        self.provider = LocalProvider(self.root / "data" / "tasks", ROOT / "engines", issues=[],
                                      worker_command=lambda _id, task_dir: [sys.executable, str(worker), "--task-dir", str(task_dir)])

    def tearDown(self) -> None:
        self.provider.shutdown()
        self.temp.cleanup()

    def request(self, **options: Any) -> dict[str, Any]:
        return {"schema": 1, "provider": "local", "target": "screen-text", "device": "cuda",
                "source": {"kind": "local_file", "path": str(self.source), "video_id": "vid", "title": "t"},
                "screen_text": options}

    def test_the_request_is_never_projected(self) -> None:
        validated = validate_request(self.request(cover="none"))
        self.assertEqual(validated["projection"], "none")
        self.assertEqual(validated["screen_text"]["cover"], "none")
        with self.assertRaises(ProviderError):
            validate_request({**self.request(), "source": {"kind": "local_file", "path": str(self.source)}})

    def test_a_finished_task_appends_its_tracks_with_the_speech_as_context(self) -> None:
        self.provider.documents.create("vid", {"schema": 1, "subtitles": [{"t0": 1, "t1": 2, "ja": "あ", "zh": "啊"}],
                                               "tracks": [{"id": "t1", "segs": []}]})
        task = self.provider.start(self.request())
        self.assertEqual(task["kind"], "screen-text")
        done = wait(self.provider, task["task_id"], {"completed", "failed"})
        self.assertEqual(done["state"], "completed", done.get("error"))
        document = self.provider.document("vid")
        self.assertEqual([t["id"] for t in document["tracks"]], ["t1", "ocr"])
        self.assertIn(f"Style: {screen_text.STYLE_NAME},", document.get("styles", ""))
        self.assertEqual(document["subtitles"], [{"t0": 1, "t1": 2, "ja": "あ", "zh": "啊"}])
        seen = json.loads((self.provider._task_dir(task["task_id"]) / "seen-request.json").read_text(encoding="utf-8"))
        self.assertEqual(seen["screen_text"]["speech"], [{"t0": 1.0, "t1": 2.0, "text": "あ / 啊"}])
        completed = [event for event in self.provider.events(task["task_id"])["events"] if event["type"] == "completed"]
        self.assertEqual(completed[-1]["payload"]["document_rev"], document["rev"])
        preview = self.provider.screen_text_result(task["task_id"])
        self.assertEqual([t["id"] for t in preview["tracks"]], ["ocr"])
        self.assertIn(f"Style: {screen_text.STYLE_NAME},", preview["styles"])

    def test_a_trial_appends_nothing(self) -> None:
        self.provider.documents.create("vid", {"schema": 1, "subtitles": [], "tracks": []})
        task = self.provider.start(self.request(trial=True))
        self.assertEqual(wait(self.provider, task["task_id"], {"completed", "failed"})["state"], "completed")
        self.assertEqual(self.provider.document("vid")["tracks"], [])
        self.assertEqual(len(self.provider.screen_text_result(task["task_id"])["tracks"]), 1)

    def test_one_screen_text_task_at_a_time(self) -> None:
        busy = self.provider._task_dir("beef")
        busy.mkdir(parents=True)
        (busy / "snapshot.json").write_text(json.dumps({"task_id": "beef", "kind": "screen-text", "state": "running"}),
                                            encoding="utf-8")
        with self.assertRaises(ProviderError) as raised:
            self.provider.start(self.request())
        self.assertEqual(raised.exception.code, "screen_text_busy")

    def test_inpaint_request_validation_and_busy_lock(self) -> None:
        inpaint_req = {
            "schema": 1, "provider": "local", "target": "screen-text-inpaint", "device": "auto",
            "source": {"kind": "local_file", "path": str(self.source), "title": "t"},
            "inpaint": {"plates_ass": "[Events]\n", "start": 1.0, "end": 5.0},
        }
        validated = validate_request(inpaint_req)
        self.assertEqual(validated["projection"], "none")
        self.assertEqual(validated["inpaint"]["preset"], "default")
        with self.assertRaises(ProviderError):
            validate_request({**inpaint_req, "inpaint": {"start": 5.0, "end": 1.0, "plates_ass": "x"}})
        with self.assertRaises(ProviderError):
            validate_request({**inpaint_req, "inpaint": {"start": 0.0, "end": 2.0}})
        busy = self.provider._task_dir("cafe")
        busy.mkdir(parents=True)
        (busy / "snapshot.json").write_text(json.dumps({"task_id": "cafe", "kind": "screen-text-inpaint", "state": "running"}),
                                            encoding="utf-8")
        with self.assertRaises(ProviderError) as raised:
            self.provider.start(self.request())
        self.assertEqual(raised.exception.code, "screen_text_busy")

    def test_gametext_bundle_import_and_list(self) -> None:
        import zipfile

        self.assertEqual(self.provider.list_screen_text_gametexts()["games"], [])
        bundle = self.root / "genshin-5.0-abc123.zip"
        with zipfile.ZipFile(bundle, "w") as zf:
            zf.writestr("genshin/manifest.json", json.dumps({
                "schema": "gtd-bundle/1", "game": "genshin", "fingerprint": "sha256:aaa",
                "upstream": {"version": "5.0"},
            }))
        imported = self.provider.import_screen_text_gametext({"path": str(bundle)})
        self.assertEqual(imported["imported"]["game"], "genshin")
        self.assertEqual(len(imported["games"]), 1)
        self.assertEqual(self.provider.list_screen_text_gametexts()["games"][0]["version"], "5.0")

        # Importing a newer bundle for the same game replaces the old one cleanly
        newer = self.root / "genshin-5.1-def456.zip"
        with zipfile.ZipFile(newer, "w") as zf:
            zf.writestr("genshin/manifest.json", json.dumps({
                "schema": "gtd-bundle/1", "game": "genshin", "fingerprint": "sha256:bbb",
                "upstream": {"version": "5.1"},
            }))
        updated = self.provider.import_screen_text_gametext({"path": str(newer)})
        self.assertEqual(len(updated["games"]), 1)
        self.assertEqual(updated["games"][0]["version"], "5.1")

        # Non-bundle zip is refused
        bad = self.root / "bad.zip"
        with zipfile.ZipFile(bad, "w") as zf:
            zf.writestr("readme.txt", "hello")
        with self.assertRaises(ProviderError) as raised:
            self.provider.import_screen_text_gametext({"path": str(bad)})
        self.assertEqual(raised.exception.code, "invalid_gametext")


if __name__ == "__main__":
    unittest.main()
