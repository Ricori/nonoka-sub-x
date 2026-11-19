from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from nonoka_x.local_provider import (
    LocalProvider,
    ProviderError,
    detect_devices,
    runtime_report,
)
from nonoka_x.sidecar import SidecarServer, session_authorized


def wait_state(provider: LocalProvider, task_id: str, expected: set[str], timeout: float = 5) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        snapshot = provider.status(task_id)
        if snapshot["state"] in expected:
            return snapshot
        time.sleep(0.02)
    raise AssertionError(f"task did not reach {expected}: {provider.status(task_id)}")


def wait_progress(provider: LocalProvider, task_id: str, timeout: float = 5) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        snapshot = provider.status(task_id)
        if snapshot["progress"] is not None:
            return snapshot
        time.sleep(0.02)
    raise AssertionError(f"task did not report progress: {provider.status(task_id)}")


class ProviderFixture(unittest.TestCase):
    """A provider whose worker is a fixture script, and the request it takes.

    Shared by the suites below rather than subclassed from one of them: a test
    class that inherits another's tests runs them again, and two of these
    fixtures sleep for thirty seconds on purpose.
    """

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "video.mp4"
        self.source.write_bytes(b"fixture")
        self.providers: list[LocalProvider] = []

    def tearDown(self) -> None:
        # A test that fails part way never reaches its own `provider.shutdown()`,
        # and its worker threads keep writing under the temp root while cleanup
        # deletes it -- which raised `[WinError 145] directory not empty` from
        # tearDown and buried the assertion that actually failed. Shutting the
        # providers down here is idempotent, so tests keep their explicit call.
        for provider in self.providers:
            provider.shutdown()
        self.temp.cleanup()

    def command(self, _task_id: str, task_dir: Path) -> list[str]:
        return [sys.executable, str(ROOT / "tests/fake_provider_worker.py"), "--task-dir", str(task_dir)]

    def provider(self) -> LocalProvider:
        instance = LocalProvider(
            self.root / "tasks",
            ROOT / "engines",
            worker_command=self.command,
            issues=[],
        )
        self.providers.append(instance)
        return instance

    def request(self, mode: str = "success") -> dict:
        return {
            "schema": 1,
            "provider": "local",
            "source": {"kind": "local_file", "path": str(self.source), "title": mode},
            "target": "raw-srt",
            "language": "ja",
            "device": "cuda",
            "gpu_budget_gb": 8,
            "correction": {"enabled": False, "media": "audio"},
            "knowledge": "none",
            "cleanup_intermediate": False,
        }


class LocalProviderTests(ProviderFixture):
    def test_success_persists_monotonic_events_and_artifacts(self) -> None:
        provider = self.provider()
        task = provider.start(self.request())
        completed = wait_state(provider, task["task_id"], {"completed"})
        page = provider.events(task["task_id"])
        cursors = [event["cursor"] for event in page["events"]]
        self.assertEqual(cursors, list(range(1, len(cursors) + 1)))
        self.assertEqual(completed["stage"], "stable")
        self.assertIn("correction_json", provider.artifacts(task["task_id"])["artifacts"])
        provider.shutdown()

    def test_completed_task_cleans_in_progress_progress_message(self) -> None:
        provider = self.provider()
        task = provider.start(self.request())
        task_id = task["task_id"]
        provider._update_progress(task_id, "stage", {"stage": "final-srt", "message": "正在处理"})
        self.assertEqual(provider.status(task_id)["progress"]["message"], "正在处理")
        provider._set_state(task_id, "completed")
        self.assertEqual(provider.status(task_id)["progress"]["message"], "字幕已完成")
        provider.shutdown()

    def test_local_provider_rejects_cloud_vocal_profile(self) -> None:
        provider = self.provider()
        request = self.request()
        request["vocal_profile"] = "cost"
        with self.assertRaisesRegex(ProviderError, "only supports vocal_profile=quality"):
            provider.start(request)

    def test_task_list_recovers_persisted_source_metadata(self) -> None:
        provider = self.provider()
        request = self.request()
        request["source"].update({"video_id": "loc_0123456789ab", "title": "迁移测试"})
        task = provider.start(request)
        wait_state(provider, task["task_id"], {"completed"})
        listing = provider.list_tasks()
        self.assertEqual(listing["schema"], 1)
        self.assertEqual(listing["tasks"][0]["snapshot"]["task_id"], task["task_id"])
        self.assertEqual(listing["tasks"][0]["media_id"], "loc_0123456789ab")
        self.assertEqual(listing["tasks"][0]["title"], "迁移测试")
        provider.shutdown()

    def test_completed_task_projects_and_saves_edit_document(self) -> None:
        provider = self.provider()
        request = self.request()
        request["source"].update({
            "video_id": "loc_0123456789ab",
            "fingerprint": "fingerprint",
            "duration": 1,
        })
        task = provider.start(request)
        wait_state(provider, task["task_id"], {"completed"})
        document = provider.document("loc_0123456789ab")
        self.assertEqual(document["subtitles"][0]["ja"], "fixture")
        saved = provider.save_document(
            "loc_0123456789ab",
            {"rev": document["rev"], "subtitles": [{**document["subtitles"][0], "ja": "edited"}]},
        )
        self.assertEqual(saved["rev"], 1)
        self.assertEqual(saved["subtitles"][0]["ja"], "edited")
        provider.shutdown()

    def test_failure_has_stable_error_code(self) -> None:
        provider = self.provider()
        task = provider.start(self.request("fail"))
        failed = wait_state(provider, task["task_id"], {"failed"})
        self.assertEqual(failed["error"]["code"], "missing_llm_key")

    def test_undecodable_worker_output_does_not_fail_the_task(self) -> None:
        provider = self.provider()
        task = provider.start(self.request("gbk"))
        wait_state(provider, task["task_id"], {"completed"})
        logs = [event for event in provider.events(task["task_id"])["events"] if event["type"] == "log"]
        self.assertTrue(logs, "the undecodable console line should survive as a log event")
        provider.shutdown()

    def test_worker_keeps_utf8_protocol_without_forcing_utf8_locale(self) -> None:
        provider = self.provider()
        task = provider.start(self.request("encoding-environment"))
        wait_state(provider, task["task_id"], {"completed"})
        logs = [event for event in provider.events(task["task_id"])["events"] if event["type"] == "log"]
        self.assertEqual(logs[0]["payload"]["python_utf8"], "0")
        self.assertEqual(logs[0]["payload"]["python_io_encoding"], "utf-8")
        provider.shutdown()

    def test_each_engine_process_gets_its_own_run_directory(self) -> None:
        """WP-R5, WP-P1: a run directory per package process, gone with it; the contexts are neutral.

        What an engine reads of its own is derived in the package's process
        (`tests/test_finesub_bridge.py`, which also shows an outside `FINESUB_*`
        never reaches the engine).
        """

        provider = self.provider()
        with provider.launch_contexts() as built:
            runs = {package: Path(context["paths"]["run"]) for package, context in built.contexts.items()}
            self.assertEqual(set(runs), {"finesub-transcribe", "finesub-correct", "finesub-knowledge"})
            for package, run in runs.items():
                self.assertEqual(run.parent, self.root.resolve() / "engines" / package / "run")
                self.assertTrue(run.is_dir())
            self.assertNotIn("FINESUB_", json.dumps(built.contexts, ensure_ascii=False))
        for run in runs.values():
            self.assertFalse(run.exists())
            self.assertEqual(list(run.parent.iterdir()), [])
        provider.shutdown()

    def test_a_run_directory_left_by_a_crash_is_swept_at_start_up(self) -> None:
        left = self.root / "engines" / "finesub-correct" / "run" / "999-deadbeef0000"
        left.mkdir(parents=True)
        (left / "finesub.toml").write_text("x", encoding="utf-8")
        self.provider()
        self.assertFalse(left.exists())

    def test_the_card_is_left_to_the_transcription_process(self) -> None:
        # WP-H: a `cuda:N` request's card is the transcription process's own
        # `CUDA_VISIBLE_DEVICES`, written by the engine host when it starts
        # that process (tests/test_engine_sandbox.py); the worker gets none.
        with patch.dict(os.environ):
            os.environ.pop("CUDA_VISIBLE_DEVICES", None)
            provider = self.provider()
            req = self.request("encoding-environment")
            req["device"] = "cuda:0"
            task = provider.start(req)
            wait_state(provider, task["task_id"], {"completed"})
            logs = [event for event in provider.events(task["task_id"])["events"] if event["type"] == "log"]
            self.assertIsNone(logs[0]["payload"]["cuda_visible_devices"])
            provider.shutdown()

    def test_local_provider_validates_device_format(self) -> None:
        provider = self.provider()
        req = self.request()
        req["device"] = "cuda:1"
        task = provider.start(req)
        self.assertIn(task["state"], {"queued", "running"})
        wait_state(provider, task["task_id"], {"completed"})
        provider.shutdown()

        provider2 = self.provider()
        invalid_req = self.request()
        invalid_req["device"] = "cuda:abc"
        with self.assertRaisesRegex(ProviderError, "unsupported device"):
            provider2.start(invalid_req)
        provider2.shutdown()

    def test_the_engines_new_switches_survive_validation(self) -> None:
        """`separate` and `llm_model`, the two knobs FineSub 0.5.0 added.

        Both are optional and both are refused rather than coerced when the
        type is wrong: skipping separation by accident costs the whole
        recognition quality and reports nothing, and a malformed model pin
        would surface much later as a routing error that names neither the
        field nor the request.
        """

        provider = self.provider()
        accepted = self.request()
        accepted["separate"] = False
        accepted["llm_model"] = {"correction": "local-agy-media-gemini-3_7-flash"}
        task = provider.start(accepted)
        self.assertIn(task["state"], {"queued", "running"})
        wait_state(provider, task["task_id"], {"completed"})
        provider.shutdown()

        provider2 = self.provider()
        with self.assertRaisesRegex(ProviderError, "separate must be a boolean"):
            provider2.start({**self.request(), "separate": "off"})
        with self.assertRaisesRegex(ProviderError, "llm_model must be"):
            provider2.start({**self.request(), "llm_model": ["a", "b"]})
        with self.assertRaisesRegex(ProviderError, "llm_model entries must be strings"):
            provider2.start({**self.request(), "llm_model": {"correction": 1}})
        provider2.shutdown()

    def test_a_request_defaults_to_separating_and_to_the_auto_tier(self) -> None:
        """The two defaults the desktop relies on when a request omits them.

        `auto` is the engine's own default and detects the card; separation on
        is what every input that is not already a vocal track needs. A request
        that still carries the retired `gpu_budget_gb` is left alone here and
        converted at the worker, so the migration lives in one place.
        """

        from nonoka_x.local_provider import validate_request

        request = self.request()
        request.pop("gpu_budget_gb", None)
        normalized = validate_request(request)
        self.assertEqual(normalized["gpu_tier"], "auto")
        self.assertTrue(normalized["separate"])

        # A request queued before the upgrade keeps its number and gains no
        # tier here: the worker converts it, so there is one mapping and not
        # two that could disagree.
        legacy = validate_request(self.request())
        self.assertNotIn("gpu_tier", legacy)
        self.assertEqual(legacy["gpu_budget_gb"], 8)

    def test_knowledge_subjects_are_validated_for_reading_and_writing(self) -> None:
        from nonoka_x.local_provider import MAX_KNOWLEDGE_SUBJECTS, validate_request

        def subject(name: str, **extra: str) -> dict:
            return {"kind": "streamer", "subject": name, "aliases": "", "description": "", **extra}

        request = self.request()
        request["target"] = "final-srt"
        request["knowledge"] = "update"
        request["knowledge_subjects"] = [subject("   ")]
        with self.assertRaisesRegex(ProviderError, r"knowledge_subjects\[0\]\.subject is required"):
            validate_request(request)

        # Several subjects, existing and new mixed; a repeat collapses.
        request["knowledge_subjects"] = [
            subject("猫又おかゆ", id="s1"),
            subject(" 戌神ころね ", id="s2"),
            subject("猫又おかゆ", id="s1"),
            {**subject("新番组"), "kind": "work"},
        ]
        normalized = validate_request(request)
        self.assertEqual(
            [(item.get("id"), item["subject"]) for item in normalized["knowledge_subjects"]],
            [("s1", "猫又おかゆ"), ("s2", "戌神ころね"), (None, "新番组")],
        )

        # Writing back needs at least one target.
        request["knowledge_subjects"] = []
        with self.assertRaisesRegex(ProviderError, "at least one subject"):
            validate_request(request)

        # Reading may name several, or none at all, but cannot create one.
        request["knowledge"] = "read"
        self.assertNotIn("knowledge_subjects", validate_request(request))
        request["knowledge_subjects"] = [subject("猫又おかゆ", id="s1"), subject("新番组")]
        with self.assertRaisesRegex(ProviderError, "only be created when knowledge=update"):
            validate_request(request)

        request["knowledge_subjects"] = [
            subject(f"主体{index}", id=f"s{index}") for index in range(MAX_KNOWLEDGE_SUBJECTS + 1)
        ]
        with self.assertRaisesRegex(ProviderError, "at most"):
            validate_request(request)

        # `none` reads nothing, so whatever was picked is dropped.
        request["knowledge"] = "none"
        self.assertNotIn("knowledge_subjects", validate_request(request))

    def test_legacy_knowledge_context_becomes_a_one_subject_list(self) -> None:
        from nonoka_x.local_provider import validate_request

        request = self.request()
        request["target"] = "final-srt"
        request["knowledge"] = "update"
        request["knowledge_context"] = {
            "kind": "streamer", "subject": "猫又おかゆ", "aliases": "", "description": ""
        }
        normalized = validate_request(request)
        self.assertNotIn("knowledge_context", normalized)
        self.assertEqual(
            normalized["knowledge_subjects"],
            [{"kind": "streamer", "subject": "猫又おかゆ", "aliases": "", "description": ""}],
        )

        request["knowledge"] = "read"
        normalized = validate_request(request)
        self.assertNotIn("knowledge_context", normalized)
        self.assertNotIn("knowledge_subjects", normalized)

    def test_legacy_collect_mode_is_read_now(self) -> None:
        from nonoka_x.local_provider import ProviderError, validate_request

        request = self.request()
        request["target"] = "final-srt"
        request["knowledge"] = "collect"
        normalized = validate_request(request)
        self.assertEqual(normalized["knowledge"], "read")
        self.assertEqual(normalized["legacy_knowledge"], "collect")

        request["knowledge"] = "sometimes"
        with self.assertRaisesRegex(ProviderError, "none, read, or update"):
            validate_request(request)

    def test_diarization_block_is_checked_and_normalized(self) -> None:
        from nonoka_x.local_provider import ProviderError, validate_request

        request = self.request()
        request["target"] = "final-srt"
        self.assertNotIn("diarization", validate_request(request))
        request["diarization"] = {"level": "off"}
        self.assertNotIn("diarization", validate_request(request))

        request["diarization"] = {"level": "judge-text", "max_speakers": None}
        self.assertEqual(
            validate_request(request)["diarization"],
            {"level": "judge-text", "max_speakers": None},
        )

        # The judge runs inside the LLM correction; a raw run keeps the acoustics.
        request["target"] = "raw-srt"
        self.assertEqual(
            validate_request(request)["diarization"],
            {"level": "acoustics", "max_speakers": None, "requested_level": "judge-text"},
        )

        request["target"] = "aligned"
        request["diarization"] = {"level": "acoustics"}
        with self.assertRaisesRegex(ProviderError, "target stable or later"):
            validate_request(request)

        request["target"] = "final-srt"
        for bad in ({"level": "loud"}, {"level": "acoustics", "max_speakers": 0},
                    {"level": "acoustics", "max_speakers": True}, "acoustics"):
            request["diarization"] = bad
            with self.assertRaises(ProviderError):
                validate_request(request)

    def test_pre_field_update_request_remains_retryable(self) -> None:
        from nonoka_x.local_provider import validate_request

        request = self.request()
        request["target"] = "final-srt"
        request["knowledge"] = "update"
        normalized = validate_request(request)
        self.assertNotIn("knowledge_context", normalized)
        self.assertNotIn("knowledge_subjects", normalized)

    def test_cancel_and_resume_interrupted_task(self) -> None:
        provider = self.provider()
        slow = provider.start(self.request("slow"))
        wait_state(provider, slow["task_id"], {"running"})
        self.assertEqual(provider.cancel(slow["task_id"])["state"], "cancelled")

        resumable = provider.start(self.request("resume"))
        wait_state(provider, resumable["task_id"], {"running"})
        wait_progress(provider, resumable["task_id"])
        provider.shutdown()
        self.assertEqual(provider.status(resumable["task_id"])["state"], "interrupted")
        provider.resume(resumable["task_id"])
        self.assertEqual(wait_state(provider, resumable["task_id"], {"completed"})["state"], "completed")

    def test_invalid_source_is_rejected_before_worker_start(self) -> None:
        provider = self.provider()
        request = self.request()
        request["source"]["path"] = "relative.mp4"
        with self.assertRaisesRegex(ProviderError, "absolute") as raised:
            provider.start(request)
        self.assertEqual(raised.exception.code, "invalid_source")

    def test_loopback_server_requires_session_token(self) -> None:
        provider = self.provider()
        self.assertFalse(session_authorized("", "secret-token"))
        self.assertFalse(session_authorized("Bearer wrong", "secret-token"))
        self.assertTrue(session_authorized("Bearer secret-token", "secret-token"))
        try:
            server = SidecarServer(("127.0.0.1", 0), provider, "secret-token")
        except PermissionError:
            provider.shutdown()
            self.skipTest("sandbox does not permit a loopback listener")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        url = f"http://127.0.0.1:{server.server_port}/v1/capabilities"
        try:
            with self.assertRaises(urllib.error.HTTPError) as raised:
                urllib.request.urlopen(url, timeout=2)
            self.assertEqual(raised.exception.code, 401)
            request = urllib.request.Request(url, headers={"Authorization": "Bearer secret-token"})
            with urllib.request.urlopen(request, timeout=2) as response:
                body = json.load(response)
            self.assertEqual(body["provider"], "local")
        finally:
            server.shutdown()
            server.server_close()
            provider.shutdown()
            thread.join(timeout=2)

    def test_missing_managed_models_block_asr_readiness(self) -> None:
        media_tool = self.source

        class Provisioner:
            def status(self) -> dict:
                return {
                    "runtime": {"state": "ready"},
                    "resources": [],
                    "models": [
                        {"id": "separator", "state": "ready"},
                        {"id": "whisper", "state": "missing"},
                        {"id": "qwen-referee", "state": "missing"},
                    ],
                }

            def tool_path(self, _name: str) -> Path:
                return media_tool

        report = runtime_report(provisioner=Provisioner())
        model_issue = next(issue for issue in report["issues"] if issue["code"] == "missing_model")
        self.assertIn("whisper", model_issue["message"])
        self.assertIn("qwen-referee", model_issue["message"])
        raw_stage = next(stage for stage in report["stages"] if stage["id"] == "raw-srt")
        self.assertFalse(raw_stage["ready"])

    def test_absent_gpu_warns_without_blocking_asr(self) -> None:
        """A Windows box with no card runs on the CPU rather than not at all.

        The engine's `cpu` tier is a supported mode, so the missing card is a
        speed warning. Putting it in `issues` made `ready` false, which is what
        greyed the whole "local" execution card out.
        """

        media_tool = self.source

        class Provisioner:
            def status(self) -> dict:
                return {
                    "runtime": {"state": "ready"},
                    "resources": [],
                    "models": [
                        {"id": model, "state": "ready"}
                        for model in ("separator", "whisper", "qwen-referee")
                    ],
                }

            def tool_path(self, _name: str) -> Path:
                return media_tool

        def executable(name: str) -> str | None:
            return None if name == "nvidia-smi" else str(media_tool)

        with (
            patch("nonoka_x.local_provider.sys.platform", "win32"),
            patch("nonoka_x.local_provider.shutil.which", side_effect=executable),
            patch("nonoka_x.local_provider.local_agent_executable", return_value=None),
        ):
            report = runtime_report(provisioner=Provisioner())

        self.assertTrue(report["ready"])
        self.assertNotIn("missing_gpu", {issue["code"] for issue in report["issues"]})
        self.assertIn("missing_gpu", {warning["code"] for warning in report["warnings"]})
        raw_stage = next(stage for stage in report["stages"] if stage["id"] == "raw-srt")
        self.assertTrue(raw_stage["ready"])

    def test_device_list_always_offers_the_cpu(self) -> None:
        """Empty used to mean "no choice at all" in the desktop's picker."""

        with patch("nonoka_x.local_provider.shutil.which", return_value=None):
            self.assertEqual([device["id"] for device in detect_devices()], ["cpu"])

        smi = self.source

        def run(*_args, **_kwargs):
            return SimpleNamespace(stdout="0, NVIDIA GeForce RTX 4090, 24564\n")

        with (
            patch("nonoka_x.local_provider.shutil.which", return_value=str(smi)),
            patch("nonoka_x.local_provider.subprocess.run", side_effect=run),
        ):
            devices = detect_devices()
        # CPU last: the desktop defaults a task to `devices[0]`, so a machine
        # with a card has to keep defaulting to the card.
        self.assertEqual([device["id"] for device in devices], ["cuda:0", "cpu"])

    def test_missing_git_does_not_block_knowledge_updates(self) -> None:
        media_tool = self.source

        class Provisioner:
            def status(self) -> dict:
                return {
                    "runtime": {"state": "ready"},
                    "resources": [],
                    "models": [
                        {"id": "separator", "state": "ready"},
                        {"id": "whisper", "state": "ready"},
                        {"id": "qwen-referee", "state": "ready"},
                    ],
                }

            def tool_path(self, name: str) -> Path | None:
                return media_tool if name in {"ffmpeg", "ffprobe"} else None

        class Settings:
            def snapshot(self) -> dict:
                return {"llmReady": True, "retrievalKeyConfigured": False}

        def executable(name: str) -> str | None:
            return None if name == "git" else str(media_tool)

        with (
            patch("nonoka_x.local_provider.sys.platform", "win32"),
            patch("nonoka_x.local_provider.shutil.which", side_effect=executable),
            patch("nonoka_x.local_provider.local_agent_executable", return_value=None),
        ):
            report = runtime_report(settings=Settings(), provisioner=Provisioner())

        knowledge_stage = next(stage for stage in report["stages"] if stage["id"] == "knowledge")
        self.assertTrue(knowledge_stage["ready"])
        self.assertNotIn("missing_git", {issue["code"] for issue in knowledge_stage["issues"]})

    def test_misleading_onnx_cuda_warning_is_filtered_from_events(self) -> None:
        provider = self.provider()
        task = provider.start(self.request("onnx-cuda-warning"))
        wait_state(provider, task["task_id"], {"completed"})
        page = provider.events(task["task_id"])
        messages = [event.get("payload", {}).get("message", "") for event in page["events"]]
        self.assertFalse(any("CUDAExecutionProvider not available in ONNXruntime" in msg for msg in messages))
        self.assertTrue(any("Useful information" in msg for msg in messages))


class PipelineTargetTests(ProviderFixture):
    """Every stage the engine names is a target a caller may ask for.

    The provider used to accept two of the six, which was the desktop's own
    menu written into the contract. A caller that wants the vocal track, or
    the aligned JSON, has no reason to run recognition and the LLM stages to
    get it -- and the engine already skips a stage whose output exists, so
    asking for an earlier stage is also how a run is resumed cheaply.
    """

    def test_every_pipeline_stage_is_a_startable_target(self) -> None:
        from nonoka_x.local_provider import PIPELINE_TARGETS, validate_request

        for target in PIPELINE_TARGETS:
            with self.subTest(target=target):
                request = self.request()
                request["target"] = target
                self.assertEqual(validate_request(request)["target"], target)

    def test_an_unknown_target_names_the_ones_that_exist(self) -> None:
        from nonoka_x.local_provider import validate_request

        request = self.request()
        request["target"] = "subtitles"
        with self.assertRaisesRegex(ProviderError, "translated-srt"):
            validate_request(request)

    def test_readiness_is_gated_per_target(self) -> None:
        """An ASR-only target must not be blocked by a missing model provider.

        The gate used to be "raw-srt or everything else", so `vocal` would have
        demanded a configured LLM -- a stage that never calls one.
        """

        from nonoka_x.local_provider import CONTINUATION_READINESS, TARGET_READINESS

        self.assertEqual(TARGET_READINESS["vocal"], ("raw-srt",))
        self.assertEqual(TARGET_READINESS["stable"], ("raw-srt",))
        # From media, the LLM stages recognise first: both gates apply.
        self.assertEqual(TARGET_READINESS["translated-srt"], ("raw-srt", "final-srt"))
        # From the cloud's stable.json, nothing recognises: the ASR gate never does.
        self.assertEqual(CONTINUATION_READINESS["raw-srt"], ("harness",))
        self.assertEqual(CONTINUATION_READINESS["final-srt"], ("final-srt",))

    def test_projection_defaults_to_writing_the_document(self) -> None:
        from nonoka_x.local_provider import validate_request

        self.assertEqual(validate_request(self.request())["projection"], "document")
        request = self.request()
        request["projection"] = "artifacts"
        with self.assertRaisesRegex(ProviderError, "projection must be"):
            validate_request(request)

    def test_a_run_asked_not_to_project_leaves_the_document_alone(self) -> None:
        """`projection: none` is what makes a stage run safe to offer.

        Projection replaces the video's default document. A caller that only
        wanted one stage's artifacts would otherwise overwrite whatever the
        user has been editing.
        """

        provider = self.provider()
        request = self.request()
        request["source"]["video_id"] = "loc_0123456789ab"
        request["projection"] = "none"
        task = provider.start(request)
        wait_state(provider, task["task_id"], {"completed"})
        self.assertIn("correction_json", provider.artifacts(task["task_id"])["artifacts"])
        with self.assertRaises(ProviderError):
            provider.document("loc_0123456789ab")
        provider.shutdown()


class LLMCallTests(ProviderFixture):
    """The channel around the LLM worker, which is all the provider owns.

    The call itself is the engine's; what is tested here is that one process
    serves many calls, that a dead one is replaced instead of failing the
    request that found it, and that a call which never answers ends.
    """

    def llm_provider(self) -> LocalProvider:
        instance = LocalProvider(
            self.root / "tasks",
            ROOT / "engines",
            worker_command=self.command,
            llm_worker_command=lambda: [sys.executable, str(ROOT / "tests/fake_llm_worker.py")],
            issues=[],
        )
        self.providers.append(instance)
        return instance

    @staticmethod
    def call(prompt: str = "hello", role: str = "lightweight") -> dict:
        return {"role": role, "messages": [{"role": "user", "content": prompt}]}

    def test_one_worker_serves_repeated_calls(self) -> None:
        provider = self.llm_provider()
        first = provider.llm_complete(self.call("one"))
        started = provider._llm_process
        second = provider.llm_complete(self.call("two"))
        self.assertEqual(first["content"], "answered one as lightweight")
        self.assertEqual(second["model"], "fixture-model")
        self.assertIs(provider._llm_process, started)
        provider.shutdown()
        self.assertIsNone(provider._llm_process)

    def test_saving_keys_retires_the_worker_holding_the_old_ones(self) -> None:
        provider = self.llm_provider()
        provider._settings = SimpleNamespace(update_keys=lambda updates, **_: {"saved": dict(updates)})
        provider.llm_complete(self.call("one"))
        started = provider._llm_process
        provider.update_keys({"OPENAI_KEYS": "new-secret"})
        self.assertEqual(provider.llm_complete(self.call("two"))["content"], "answered two as lightweight")
        self.assertIsNot(provider._llm_process, started)
        self.assertIsNotNone(started.poll())
        provider.shutdown()

    def test_a_worker_that_died_between_calls_is_replaced(self) -> None:
        provider = self.llm_provider()
        provider.llm_complete(self.call("one"))
        with self.assertRaises(ProviderError):
            provider.llm_complete(self.call("die"))
        self.assertEqual(provider.llm_complete(self.call("three"))["content"], "answered three as lightweight")
        provider.shutdown()

    def test_a_call_that_never_answers_ends_and_takes_the_worker_with_it(self) -> None:
        import nonoka_x.local_provider as module

        provider = self.llm_provider()
        previous = module.LLM_CALL_TIMEOUT_SEC
        module.LLM_CALL_TIMEOUT_SEC = 0.5
        try:
            with self.assertRaisesRegex(ProviderError, "did not answer"):
                provider.llm_complete(self.call("hang"))
        finally:
            module.LLM_CALL_TIMEOUT_SEC = previous
        self.assertIsNone(provider._llm_process)
        provider.shutdown()

    def test_a_caller_can_ask_for_a_shorter_limit(self) -> None:
        provider = self.llm_provider()
        started = time.monotonic()
        with self.assertRaisesRegex(ProviderError, "did not answer within 1s") as caught:
            provider.llm_complete({**self.call("hang"), "timeout_seconds": 1})
        self.assertLess(time.monotonic() - started, 15)
        self.assertEqual(caught.exception.code, "llm_timeout")
        self.assertIsNone(provider._llm_process)
        provider.shutdown()

    def test_only_a_caller_that_asks_skips_the_cooldown(self) -> None:
        from nonoka_x.local_provider import _validate_llm_request

        self.assertTrue(_validate_llm_request({**self.call(), "ignore_cooldown": True})["ignore_cooldown"])
        self.assertNotIn("ignore_cooldown", _validate_llm_request(self.call()))

    def test_waiting_behind_another_call_spends_the_same_limit(self) -> None:
        provider = self.llm_provider()
        with provider._llm_lock:
            with self.assertRaisesRegex(ProviderError, "did not answer within 1s"):
                provider.llm_complete({**self.call(), "timeout_seconds": 1})
        self.assertIsNone(provider._llm_process)
        provider.shutdown()

    def test_a_vendor_refusal_reaches_the_caller_with_its_own_words(self) -> None:
        provider = self.llm_provider()
        with self.assertRaisesRegex(ProviderError, "daily quota spent"):
            provider.llm_complete(self.call("refuse"))
        provider.shutdown()

    def test_the_sidecar_serves_the_call_over_its_own_route(self) -> None:
        """The route exists and carries the same refusals the provider makes.

        Everything else here calls the provider directly; this is the one check
        that the desktop's actual path -- HTTP, bearer token, JSON -- reaches
        it, and that a rejected request comes back as an error body rather than
        a 500.
        """

        provider = self.llm_provider()
        try:
            server = SidecarServer(("127.0.0.1", 0), provider, "secret-token")
        except PermissionError:
            provider.shutdown()
            self.skipTest("sandbox does not permit a loopback listener")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        url = f"http://127.0.0.1:{server.server_port}/v1/llm/complete"

        def post(payload: dict):
            return urllib.request.Request(
                url,
                data=json.dumps(payload).encode(),
                headers={"Authorization": "Bearer secret-token", "Content-Type": "application/json"},
                method="POST",
            )

        try:
            with urllib.request.urlopen(post(self.call("over http")), timeout=5) as response:
                body = json.load(response)
            self.assertEqual(body["content"], "answered over http as lightweight")
            self.assertEqual(body["role"], "lightweight")
            with self.assertRaises(urllib.error.HTTPError) as raised:
                urllib.request.urlopen(post({"role": "nope", "messages": []}), timeout=5)
            self.assertEqual(raised.exception.code, 400)
            self.assertEqual(json.load(raised.exception)["error"]["code"], "invalid_request")
        finally:
            server.shutdown()
            server.server_close()
            provider.shutdown()
            thread.join(timeout=2)

    def test_the_request_is_validated_before_a_worker_is_started(self) -> None:
        from nonoka_x.local_provider import MAX_LLM_PROMPT_BYTES

        provider = self.llm_provider()
        for label, payload, expected in (
            ("role", self.call(role="audio_multimodal"), "role must be one of"),
            ("empty", {"role": "lightweight", "messages": []}, "non-empty list"),
            (
                "speaker",
                {"role": "lightweight", "messages": [{"role": "tool", "content": "x"}]},
                "system, user or assistant",
            ),
            (
                "size",
                {"role": "lightweight", "messages": [{"role": "user", "content": "x" * (MAX_LLM_PROMPT_BYTES + 1)}]},
                "exceed",
            ),
            ("tokens", {**self.call(), "max_tokens": 0}, "max_tokens"),
            ("temperature", {**self.call(), "temperature": 5}, "temperature"),
            ("timeout low", {**self.call(), "timeout_seconds": 0}, "timeout_seconds"),
            ("timeout high", {**self.call(), "timeout_seconds": 301}, "timeout_seconds"),
            ("timeout type", {**self.call(), "timeout_seconds": True}, "timeout_seconds"),
            ("cooldown type", {**self.call(), "ignore_cooldown": "yes"}, "ignore_cooldown"),
        ):
            with self.subTest(label):
                with self.assertRaisesRegex(ProviderError, expected):
                    provider.llm_complete(payload)
        self.assertIsNone(provider._llm_process)
        provider.shutdown()


class HarnessReadinessTests(unittest.TestCase):
    """The LLM half stands on its own tier, not on the ASR one.

    `final-srt` used to inherit every ASR issue, so a machine without the
    managed runtime -- or not on Windows at all -- could not correct a
    subtitle the cloud had already recognised.
    """

    def report(self, *, runtime_state: str, platform: str = "win32", missing: set[str] = frozenset()):
        media_tool = Path(__file__)

        class Provisioner:
            def status(self) -> dict:
                return {"runtime": {"state": runtime_state}, "resources": [], "models": []}

            def tool_path(self, _name: str) -> Path:
                return media_tool

        class Settings:
            def snapshot(self) -> dict:
                return {"llmReady": True, "retrievalKeyConfigured": False}

        # Every module is present but the ones named: the launcher running
        # these tests has whatever it has, and the report must not depend on it.
        def find_spec(name, *_args, **_kwargs):
            return None if name in missing else object()

        with (
            patch("nonoka_x.local_provider.sys.platform", platform),
            patch("nonoka_x.local_provider.shutil.which", return_value=str(media_tool)),
            patch("nonoka_x.local_provider.local_agent_executable", return_value=None),
            patch("importlib.util.find_spec", side_effect=find_spec),
        ):
            report = runtime_report(settings=Settings(), provisioner=Provisioner())
        return {stage["id"]: stage for stage in report["stages"]}

    def test_correction_is_ready_where_recognition_is_not(self) -> None:
        stages = self.report(runtime_state="missing", missing={"torch"})
        self.assertFalse(stages["raw-srt"]["ready"])
        self.assertTrue(stages["harness"]["ready"])
        self.assertTrue(stages["final-srt"]["ready"])
        self.assertTrue(stages["knowledge"]["ready"])

    def test_an_unsupported_platform_blocks_recognition_only(self) -> None:
        stages = self.report(runtime_state="missing", platform="darwin")
        self.assertIn("unsupported_platform", {issue["code"] for issue in stages["raw-srt"]["issues"]})
        self.assertTrue(stages["final-srt"]["ready"])

    def test_a_launcher_without_the_harness_says_which_packages(self) -> None:
        stages = self.report(runtime_state="missing", missing={"opencc", "tzdata"})
        issue = stages["harness"]["issues"][0]
        self.assertEqual(issue["code"], "missing_harness")
        self.assertIn("opencc-python-reimplemented", issue["message"])
        self.assertIn("tzdata", issue["message"])
        self.assertFalse(stages["final-srt"]["ready"])

    def test_tzdata_is_only_wanted_on_windows(self) -> None:
        stages = self.report(runtime_state="missing", platform="darwin", missing={"tzdata"})
        self.assertTrue(stages["harness"]["ready"])

    def test_a_ready_runtime_is_trusted_to_carry_the_harness(self) -> None:
        # The worker runs on the managed interpreter then, whose lock includes
        # `[harness]`; the launcher's own packages are beside the point.
        stages = self.report(runtime_state="ready", missing={"opencc"})
        self.assertTrue(stages["harness"]["ready"])


class CloudTranscriptContinuationTests(ProviderFixture):
    """A task that starts where the cloud's recognition stopped, from the result a cloud job hands over."""

    def setUp(self) -> None:
        super().setUp()
        self.stable = self.root / "finesub.stable_json"
        self.stable.write_bytes((ROOT / "tests/fixtures/finesub/sample-stable.json").read_bytes())
        self.transcript = self.root / "transcript.json"
        self.transcript.write_text(json.dumps({
            "schema": 1, "engine": "finesub", "language": "ja", "duration": 2.0,
            "lines": [{"id": "1", "start": 0.0, "end": 1.0, "text": "x"}],
        }), encoding="utf-8")

    def continuation(self, target: str = "final-srt") -> dict:
        request = self.request()
        request["source"] = {
            "kind": "cloud_transcript",
            "path": str(self.source),
            "transcript_json": str(self.transcript),
            "native": {"finesub.stable_json": str(self.stable)},
            "native_package": "finesub-transcribe",
            "title": "success",
        }
        request["target"] = target
        return request

    def test_the_cloud_legs_own_fields_are_dropped(self) -> None:
        from nonoka_x.local_provider import validate_request

        request = self.continuation()
        request["vocal_profile"] = "cost"
        validated = validate_request(request)
        self.assertNotIn("vocal_profile", validated)
        self.assertEqual(validated["source"]["transcript_json"], str(self.transcript.resolve()))
        self.assertEqual(validated["source"]["native"], {"finesub.stable_json": str(self.stable.resolve())})
        self.assertEqual(validated["source"]["native_package"], "finesub-transcribe")
        # The cost profile is still refused for a run that separates here.
        local = self.request()
        local["vocal_profile"] = "cost"
        with self.assertRaisesRegex(ProviderError, "vocal_profile"):
            validate_request(local)

    def test_the_attachments_are_opaque_and_the_level_is_the_requests(self) -> None:
        """No attachment is read and no name is known here: the correction decides what a timeline is."""

        from nonoka_x.local_provider import validate_request

        timeline = self.root / "finesub.diar_json"
        timeline.write_text("not even json", encoding="utf-8")
        request = self.continuation()
        request["source"]["native"]["finesub.diar_json"] = str(timeline)
        request["diarization"] = {"level": "judge-audio", "max_speakers": 3}
        validated = validate_request(request)
        self.assertEqual(validated["source"]["native"]["finesub.diar_json"], str(timeline.resolve()))
        self.assertEqual(validated["diarization"], {"level": "judge-audio", "max_speakers": 3})
        # Without one the request keeps its level too; the correction package
        # turns it off when its stable came without its timeline.
        request = self.continuation()
        request["diarization"] = {"level": "judge-audio"}
        self.assertEqual(validate_request(request)["diarization"]["level"], "judge-audio")
        # A run that stops before the correction still gets the acoustic labels.
        request = self.continuation("raw-srt")
        request["diarization"] = {"level": "judge-text"}
        self.assertEqual(
            validate_request(request)["diarization"],
            {"level": "acoustics", "max_speakers": None, "requested_level": "judge-text"},
        )

    def test_a_transcript_alone_is_a_whole_result(self) -> None:
        from nonoka_x.local_provider import validate_request

        request = self.continuation()
        del request["source"]["native"], request["source"]["native_package"]
        validated = validate_request(request)
        self.assertNotIn("native", validated["source"])
        self.assertNotIn("native_package", validated["source"])

    def test_unusable_attachments_are_refused(self) -> None:
        from nonoka_x.local_provider import validate_request

        cases = {
            "relative": ({"finesub.stable_json": "finesub.stable_json"}, "finesub-transcribe", "absolute path"),
            "missing": ({"finesub.stable_json": str(self.root / "gone")}, "finesub-transcribe", "does not exist"),
            "no engine": ({"stable_json": str(self.stable)}, "finesub-transcribe", "<engine>.<name>"),
            "a path in the key": ({"finesub.../x": str(self.stable)}, "finesub-transcribe", "<engine>.<name>"),
            "no package": ({"finesub.stable_json": str(self.stable)}, None, "native_package"),
            "a path as package": ({"finesub.stable_json": str(self.stable)}, "../x", "native_package"),
            "not an object": (["x"], "finesub-transcribe", "must be an object"),
        }
        for name, (native, package, message) in cases.items():
            with self.subTest(name), self.assertRaisesRegex(ProviderError, message):
                request = self.continuation()
                request["source"]["native"] = native
                request["source"]["native_package"] = package
                validate_request(request)

    def test_a_missing_or_unusable_transcript_is_refused(self) -> None:
        from nonoka_x.local_provider import validate_request

        cases = {
            "relative": ("transcript.json", "absolute path"),
            "missing": (str(self.root / "gone.json"), "does not exist"),
        }
        broken = self.root / "broken.json"
        broken.write_text("{", encoding="utf-8")
        cases["broken"] = (str(broken), "unreadable")
        stable = self.root / "a-stable.json"
        stable.write_bytes(self.stable.read_bytes())
        cases["a stable, not a transcript"] = (str(stable), "unreadable")
        for label, (value, expected) in cases.items():
            with self.subTest(label):
                request = self.continuation()
                request["source"]["transcript_json"] = value
                with self.assertRaisesRegex(ProviderError, expected):
                    validate_request(request)

    def test_a_continuation_needs_no_transcription_engine_installed(self) -> None:
        """No FineSub package here: the cloud's recognition still continues (to the raw SRT the core renders)."""

        from nonoka_x.local_provider import validate_request

        for target in ("raw-srt", "final-srt"):
            with self.subTest(target):
                self.assertEqual(validate_request(self.continuation(target), engines={})["target"], target)
        # Asking for another engine is still refused, installed or not.
        request = self.continuation()
        request["transcription"] = {"engine": "mai2"}
        with self.assertRaises(ProviderError):
            validate_request(request, engines={})

    def test_the_old_source_is_refused(self) -> None:
        from nonoka_x.local_provider import validate_request

        request = self.continuation()
        request["source"] = {"kind": "cloud_stable", "path": str(self.source), "stable_json": str(self.stable)}
        with self.assertRaisesRegex(ProviderError, "local_file or cloud_transcript"):
            validate_request(request)

    def test_only_the_stages_after_stable_continue(self) -> None:
        from nonoka_x.local_provider import validate_request

        for target in ("raw-srt", "translated-srt", "final-srt"):
            with self.subTest(target):
                self.assertEqual(validate_request(self.continuation(target))["target"], target)
        for target in ("vocal", "aligned", "stable"):
            with self.subTest(target), self.assertRaisesRegex(ProviderError, "continues to"):
                validate_request(self.continuation(target))

    def test_an_axis_cannot_continue_a_cloud_run(self) -> None:
        from nonoka_x.local_provider import validate_request

        request = self.continuation()
        request["axis"] = {"kind": "ja", "rows": [{"t0": 0.0, "t1": 1.0, "ja": "x"}]}
        with self.assertRaisesRegex(ProviderError, "cannot continue a cloud run"):
            validate_request(request)

    def test_a_continuation_starts_where_the_asr_runtime_is_missing(self) -> None:
        provider = LocalProvider(
            self.root / "tasks", ROOT / "engines", worker_command=self.command
        )
        self.providers.append(provider)
        blocked = {"code": "missing_runtime", "message": "AI 运行时缺失：torch"}

        def report(*_args, **_kwargs) -> dict:
            return {"stages": [
                {"id": "raw-srt", "ready": False, "issues": [blocked]},
                {"id": "harness", "ready": True, "issues": []},
                {"id": "final-srt", "ready": True, "issues": []},
                {"id": "knowledge", "ready": True, "issues": []},
            ]}

        with patch("nonoka_x.local_provider.runtime_report", side_effect=report):
            task = provider.start(self.continuation("raw-srt"))
            local = self.request()
            local["target"] = "final-srt"
            with self.assertRaisesRegex(ProviderError, "torch"):
                provider.start(local)
            # A source-text axis recognises nothing either: it clears the
            # same tiers as a continuation.
            translated = self.request()
            translated["target"] = "final-srt"
            translated["knowledge"] = "none"
            translated["axis"] = {"kind": "ja", "rows": [{"t0": 0.0, "t1": 1.0, "ja": "x"}]}
            axis_task = provider.start(translated)
        wait_state(provider, task["task_id"], {"completed"})
        wait_state(provider, axis_task["task_id"], {"completed", "failed"})


if __name__ == "__main__":
    unittest.main()


class RuntimeOutdatedReportTests(unittest.TestCase):
    def test_an_outdated_runtime_asks_for_an_update_instead_of_listing_modules(self) -> None:
        from nonoka_x import local_provider

        class Provisioner:
            def status(self) -> dict:
                return {"runtime": {"state": "outdated", "detail": "x"}, "models": [], "optional_models": []}

            def tool_path(self, name: str):
                return Path("ffmpeg.exe")

        report = local_provider.runtime_report(None, Provisioner())
        codes = [issue["code"] for issue in report["issues"]]
        self.assertIn("runtime_outdated", codes)
        self.assertNotIn("missing_runtime", codes)
        message = next(issue["message"] for issue in report["issues"] if issue["code"] == "runtime_outdated")
        self.assertIn("需要更新", message)


# --- WP5: the transcription engine a request picks ----------------------------------------------------------

#: Stands in for any secret value; must never appear in a snapshot, log or event.
SECRET = "sk-or-wp5-never-shown-0123456789"


class KeySettings:
    """The settings as the config facade reads them: saved keys, and a configured LLM."""

    def __init__(self, root: Path, secrets: dict[str, str] | None = None) -> None:
        self.root = root
        self.secrets = dict(secrets or {})
        self.llm_ready = True

    def secret_values(self) -> dict[str, str]:
        return dict(self.secrets)

    def snapshot(self) -> dict:
        return {"llmReady": self.llm_ready, "llmKeyConfigured": self.llm_ready, "retrievalKeyConfigured": False}

    def update_keys(self, updates, *, extra_key_names=frozenset()):  # noqa: ANN001, ANN201
        self.secrets.update({name: value for name, value in updates.items() if value is not None})
        return self.snapshot()


class ToolsOnly:
    """A machine with the tool layer and nothing else: no Python runtime, no model."""

    def __init__(self, media_tool: Path) -> None:
        self.media_tool = media_tool

    def status(self) -> dict:
        return {
            "runtime": {"state": "missing"},
            "resources": [],
            "models": [{"id": model, "state": "missing"} for model in ("separator", "whisper", "qwen-referee")],
            "optional_models": [],
        }

    def tool_path(self, name: str) -> Path | None:
        return self.media_tool if name in {"ffmpeg", "ffprobe"} else None

    def worker_python(self) -> None:
        return None

    def worker_environment(self) -> dict[str, str]:
        return os.environ.copy()


class EngineSelectionTests(ProviderFixture):
    """`transcription.engine` (WP5): validation, readiness per engine, capabilities, keys."""

    def mai2(self, target: str = "final-srt", **extra) -> dict:  # noqa: ANN003
        request = self.request()
        request.update(target=target, transcription={"engine": "mai2", "options": {"split_length_scale": 1.2}})
        request.update(extra)
        return request

    # validation

    def test_a_finesub_request_is_normalized_exactly_as_before(self) -> None:
        from nonoka_x.local_provider import validate_request

        plain = validate_request(self.request())
        self.assertNotIn("transcription", plain)
        # An explicit FineSub block is kept as it came, and changes nothing else.
        explicit = self.request()
        explicit["transcription"] = {"engine": "finesub", "options": {"anything": 1}}
        normalized = validate_request(explicit)
        self.assertEqual(normalized.pop("transcription"), {"engine": "finesub", "options": {"anything": 1}})
        self.assertEqual(json.dumps(normalized, sort_keys=True), json.dumps(plain, sort_keys=True))

    def test_a_malformed_block_is_refused(self) -> None:
        from nonoka_x.local_provider import validate_request

        for block, code in (
            ("mai2", "invalid_request"),
            ({"engine": "Not An Id"}, "invalid_request"),
            ({"engine": "mai2", "options": []}, "invalid_request"),
            ({"engine": "no-such-engine"}, "unsupported_engine"),
        ):
            request = self.request()
            request["transcription"] = block
            with self.subTest(block=block), self.assertRaises(ProviderError) as caught:
                validate_request(request)
            self.assertEqual(caught.exception.code, code)

    def test_a_cloud_engine_drops_what_it_cannot_use(self) -> None:
        from nonoka_x.local_provider import dropped_fields, validate_request

        request = self.mai2(separate=False, gpu_tier="auto", vocal_profile="quality")
        validated = validate_request(request)
        for name in ("separate", "device", "gpu_tier", "gpu_budget_gb", "vocal_profile"):
            self.assertNotIn(name, validated)
        self.assertEqual(validated["transcription"], {"engine": "mai2", "options": {"split_length_scale": 1.2}})
        self.assertEqual(dropped_fields(request, validated),
                         ["separate", "vocal_profile", "device", "gpu_tier", "gpu_budget_gb"])

    def test_a_cloud_engine_runs_only_to_a_subtitle(self) -> None:
        from nonoka_x.local_provider import validate_request

        for target in ("vocal", "aligned", "stable"):
            with self.subTest(target=target), self.assertRaises(ProviderError) as caught:
                validate_request(self.mai2(target))
            self.assertEqual(caught.exception.code, "unsupported_target")
        for target in ("raw-srt", "translated-srt", "final-srt"):
            self.assertEqual(validate_request(self.mai2(target))["target"], target)

    def test_recognition_in_hand_cannot_pick_another_engine(self) -> None:
        from nonoka_x.local_provider import validate_request

        transcript = self.root / "transcript.json"
        transcript.write_text(json.dumps({"schema": 1, "engine": "finesub", "language": "ja", "duration": 1.0,
                                          "lines": []}), encoding="utf-8")
        continuation = self.mai2()
        continuation["source"] = {"kind": "cloud_transcript", "path": str(self.source), "transcript_json": str(transcript)}
        axis = self.mai2(axis={"kind": "ja", "rows": [{"t0": 0.0, "t1": 1.0, "ja": "x"}]})
        for request in (continuation, axis):
            with self.assertRaises(ProviderError) as caught:
                validate_request(request)
            self.assertEqual(caught.exception.code, "invalid_request")

    def test_the_engines_declared_features_bound_the_request(self) -> None:
        from nonoka_x.local_provider import validate_request
        from nonoka_x.transcription import EngineFeatures, EngineSpec

        narrow = EngineSpec(id="narrow", label="Narrow", kind="cloud-api",
                            features=EngineFeatures(diarization_levels=("off", "acoustics"), languages=("ja",)))
        engines = {"narrow": narrow}

        def request(**extra) -> dict:  # noqa: ANN003
            value = self.request()
            value.update(target="final-srt", transcription={"engine": "narrow"}, **extra)
            return value

        validate_request(request(diarization={"level": "acoustics"}), engines=engines)
        validate_request(request(language="auto"), engines=engines)
        for bad in (request(diarization={"level": "judge-text"}), request(language="zh")):
            with self.assertRaises(ProviderError) as caught:
                validate_request(bad, engines=engines)
            self.assertEqual(caught.exception.code, "invalid_request")

    # readiness

    def tools_only_provider(self, secrets: dict[str, str] | None = None) -> LocalProvider:
        provider = LocalProvider(
            self.root / "tasks",
            ROOT / "engines",
            worker_command=self.command,
            settings=KeySettings(self.root, secrets),
            provisioner=ToolsOnly(self.source),
        )
        self.providers.append(provider)
        return provider

    def no_gpu_no_harness_probe(self):  # noqa: ANN201
        """No card, nothing on PATH, and the launcher's harness taken as present."""

        from contextlib import ExitStack

        from nonoka_x import engine_host

        def readiness(package):  # noqa: ANN001, ANN202
            declared = dict(package.manifest.get("readiness") or {})
            if package.kind == "correction":
                declared.pop("modules", None)
                declared.pop("windows_modules", None)
            return declared

        stack = ExitStack()
        stack.enter_context(patch("shutil.which", return_value=None))
        stack.enter_context(patch.object(engine_host.Package, "readiness", property(readiness)))
        stack.enter_context(patch("nonoka_x.local_provider.local_agent_executable", return_value=None))
        return stack

    def test_mai2_starts_final_srt_with_only_the_tool_layer_and_its_key(self) -> None:
        """The WP5 completion criterion: no runtime, no model, no GPU; the MAI2 key is enough."""

        with self.no_gpu_no_harness_probe():
            for secrets in ({"OPENROUTER_STT_KEY": SECRET},
                            {"AZURE_SPEECH_KEY": SECRET, "AZURE_SPEECH_ENDPOINT": "https://x.example/speech"}):
                provider = self.tools_only_provider(secrets)
                # FineSub cannot run here at all...
                with self.assertRaises(ProviderError) as caught:
                    provider.start({**self.request(), "target": "final-srt"})
                # (whichever of its issues this launcher shows first: it has no runtime and no model)
                self.assertIn(caught.exception.code, {"missing_runtime", "missing_model", "unsupported_platform"})
                # ...and MAI2 can.
                task = provider.start(self.mai2("final-srt"))
                wait_state(provider, task["task_id"], {"completed"})

    def test_mai2_without_its_keys_says_missing_key(self) -> None:
        with self.no_gpu_no_harness_probe():
            # An endpoint alone is half of the Azure route, and not a route.
            for secrets in ({}, {"AZURE_SPEECH_ENDPOINT": "https://x.example/speech"}):
                provider = self.tools_only_provider(secrets)
                with self.assertRaises(ProviderError) as caught:
                    provider.start(self.mai2("final-srt"))
                self.assertEqual(caught.exception.code, "missing_key")
                self.assertIn("OPENROUTER_STT_KEY", str(caught.exception))
                self.assertEqual(caught.exception.http_status, 503)

    def test_a_cloud_engine_still_needs_the_llm_for_a_translation(self) -> None:
        provider = self.tools_only_provider({"OPENROUTER_STT_KEY": SECRET})
        provider._settings.llm_ready = False
        with self.no_gpu_no_harness_probe():
            with self.assertRaises(ProviderError) as caught:
                provider.start(self.mai2("final-srt"))
            self.assertEqual(caught.exception.code, "missing_llm_key")
            # The raw SRT needs no LLM.
            task = provider.start(self.mai2("raw-srt"))
        wait_state(provider, task["task_id"], {"completed"})

    def test_the_dropped_fields_are_said_in_the_task_log(self) -> None:
        provider = self.tools_only_provider({"OPENROUTER_STT_KEY": SECRET})
        with self.no_gpu_no_harness_probe():
            task = provider.start(self.mai2("raw-srt"))
        wait_state(provider, task["task_id"], {"completed"})
        events = provider.events(task["task_id"])["events"]
        notice = next(event for event in events if event["payload"].get("fields", {}).get("dropped"))
        self.assertEqual(notice["type"], "log")
        self.assertEqual(notice["payload"]["fields"]["dropped"], ["device", "gpu_budget_gb"])
        self.assertIn("MAI-Transcribe-2", notice["payload"]["message"])
        stored = json.loads((self.root / "tasks" / task["task_id"] / "request.json").read_text(encoding="utf-8"))
        self.assertNotIn("device", stored)
        self.assertNotIn(SECRET, json.dumps(events, ensure_ascii=False))
        # A FineSub task says nothing of the kind.
        finesub = self.provider()
        plain = finesub.start(self.request())
        wait_state(finesub, plain["task_id"], {"completed"})
        self.assertFalse([event for event in finesub.events(plain["task_id"])["events"]
                          if event["payload"].get("fields", {}).get("dropped")])

    def test_the_snapshot_names_the_transcription_engine(self) -> None:
        provider = self.tools_only_provider({"OPENROUTER_STT_KEY": SECRET})
        with self.no_gpu_no_harness_probe():
            task = provider.start(self.mai2("raw-srt"))
        self.assertEqual(task["transcription_engine"], "mai2")
        wait_state(provider, task["task_id"], {"completed"})
        self.assertEqual(provider.status(task["task_id"])["transcription_engine"], "mai2")
        # A request without the block runs on FineSub, and says so.
        finesub = self.provider()
        plain = finesub.start(self.request())
        self.assertEqual(plain["transcription_engine"], "finesub")
        wait_state(finesub, plain["task_id"], {"completed"})
        listed = {item["snapshot"]["task_id"]: item["snapshot"] for item in finesub.list_tasks()["tasks"]}
        self.assertEqual(listed[plain["task_id"]]["transcription_engine"], "finesub")

    def test_a_task_on_the_knowledge_base_needs_the_knowledge_package(self) -> None:
        from tests.test_worker_adapter import engines_without

        provider = self.tools_only_provider({"OPENROUTER_STT_KEY": SECRET})
        provider.engines = engines_without(self.root, "finesub-knowledge")
        with self.no_gpu_no_harness_probe():
            with self.assertRaises(ProviderError) as caught:
                provider.start(self.mai2("final-srt", knowledge="read"))
            self.assertEqual(caught.exception.code, "missing_knowledge_package")
            self.assertIn("knowledge=read", str(caught.exception))
            self.assertIn("知识库包未安装", str(caught.exception))
            # Off is off: nothing to refuse.
            task = provider.start(self.mai2("final-srt", knowledge="none"))
            wait_state(provider, task["task_id"], {"completed"})
            # A new Chinese-source task only corrects, so it reads no base either
            # (the correction engine says so: `task_mode`), and is not refused --
            # what the worker would run is what is judged (WP5's hand-back).
            chinese = provider.start(self.mai2("final-srt", knowledge="update", language="zh"))
        wait_state(provider, chinese["task_id"], {"completed"})
        # Asked of the resident correction process, which the provider keeps.
        self.assertIsNotNone(provider._llm_process)

    # capabilities

    def test_capabilities_list_every_engine_and_its_readiness(self) -> None:
        provider = self.tools_only_provider({})
        with self.no_gpu_no_harness_probe(), patch("nonoka_x.local_provider.sys.platform", "win32"):
            capabilities = provider.get_capabilities()
        engines = {item["id"]: item for item in capabilities["transcription_engines"]}
        self.assertEqual(list(engines), ["finesub", "mai2", "gemini-transcribe", "moss-pro"])
        self.assertEqual(engines["mai2"]["kind"], "cloud-api")
        self.assertEqual([issue["code"] for issue in engines["mai2"]["issues"]], ["missing_key"])
        self.assertFalse(engines["finesub"]["ready"])
        self.assertEqual(engines["finesub"]["issues"], capabilities["runtime"]["issues"])
        self.assertEqual(capabilities["correction_engines"], [{"id": "finesub", "ready": True, "issues": []}])
        # The stages keep their ids, and FineSub's tier is still the one the desktop greys out on.
        self.assertEqual([stage["id"] for stage in capabilities["runtime"]["stages"]],
                         ["media", "raw-srt", "harness", "final-srt", "knowledge", "video-multimodal"])
        self.assertIn("missing_gpu", {warning["code"] for warning in capabilities["runtime"]["warnings"]})
        with self.no_gpu_no_harness_probe():
            keyed = self.tools_only_provider({"MOSS_PRO": SECRET}).get_capabilities()
        self.assertTrue({item["id"]: item for item in keyed["transcription_engines"]}["moss-pro"]["ready"])
        self.assertNotIn(SECRET, json.dumps(keyed, ensure_ascii=False))

    # keys

    def test_the_engine_keys_are_saved_through_the_facade_and_never_shown(self) -> None:
        from nonoka_x.config import ConfigStore, registered_key_specs
        from nonoka_x.settings import KEY_SPECS, FineSubSettings

        settings = FineSubSettings(self.root / "data")
        provider = LocalProvider(self.root / "data" / "tasks", ROOT / "engines",
                                 worker_command=self.command, issues=[], settings=settings)
        self.providers.append(provider)
        self.assertLessEqual(
            {"OPENROUTER_STT_KEY", "AZURE_SPEECH_KEY", "AZURE_SPEECH_ENDPOINT", "MOSS_PRO"},
            {spec.name for spec in registered_key_specs()} | {spec["name"] for spec in KEY_SPECS},
        )
        snapshot = provider.update_keys({
            "OPENROUTER_STT_KEY": SECRET,
            "AZURE_SPEECH_ENDPOINT": "https://eastus.api.cognitive.microsoft.com/speech/v1",
        })
        store = ConfigStore(settings)
        self.assertEqual([entry.value for entry in store.key_pool("OPENROUTER_STT_KEY")], [SECRET])
        self.assertEqual([entry.value for entry in store.key_pool("AZURE_SPEECH_ENDPOINT")],
                         ["https://eastus.api.cognitive.microsoft.com/speech/v1"])
        # The settings page's list is still its own twelve, and no value is in it.
        self.assertEqual([item["name"] for item in snapshot["keys"]], [spec["name"] for spec in KEY_SPECS])
        self.assertNotIn(SECRET, json.dumps(snapshot, ensure_ascii=False))
        # ...the engine keys are listed apart, configured or not, masked (WP6).
        engine_keys = {item["name"]: item for item in snapshot["engineKeys"]}
        self.assertLessEqual({"OPENROUTER_STT_KEY", "AZURE_SPEECH_KEY", "AZURE_SPEECH_ENDPOINT", "MOSS_PRO"}, set(engine_keys))
        self.assertTrue(engine_keys["OPENROUTER_STT_KEY"]["configured"])
        self.assertEqual(engine_keys["OPENROUTER_STT_KEY"]["count"], 1)
        self.assertFalse(engine_keys["AZURE_SPEECH_KEY"]["configured"])
        self.assertEqual(provider.get_settings()["engineKeys"], snapshot["engineKeys"])
        # Nonoka's engines only: nothing is handed to an engine package, in no launch context.
        with provider.launch_contexts() as built:
            handed = json.dumps(built.contexts, ensure_ascii=False)
        for name in ("OPENROUTER_STT_KEY", "AZURE_SPEECH_KEY", "AZURE_SPEECH_ENDPOINT"):
            self.assertNotIn(name, handed)
        self.assertNotIn(SECRET, handed)

    # the worker's side

    def test_the_worker_is_told_where_the_keys_and_the_runtime_are(self) -> None:
        provider = LocalProvider(self.root / "tasks", ROOT / "engines", issues=[],
                                 settings=KeySettings(self.root), provisioner=SimpleNamespace(
                                     install_root=self.root / "runtime", worker_python=lambda: None))
        self.providers.append(provider)
        command = provider._default_worker_command("t", self.root / "tasks" / "t")
        self.assertEqual(command[command.index("--data-dir") + 1], str(self.root))
        self.assertEqual(command[command.index("--install-dir") + 1], str(self.root / "runtime"))

    def test_the_worker_registers_the_cloud_engines_on_deferred_facades(self) -> None:
        from nonoka_x import worker

        opened: list[str] = []

        class Store:
            def key_pool(self, name: str):  # noqa: ANN201
                return ("pool", name)

        def open_store(root):  # noqa: ANN001, ANN202
            opened.append(str(root))
            return Store()

        with patch("nonoka_x.config.ConfigStore.open", side_effect=open_store):
            context = worker.engine_context(self.root, None, ROOT / "engines")
            transcription_engines, correction_engines = worker.build_engines(**context)
            self.assertEqual([engine.spec.id for engine in transcription_engines],
                             ["finesub", "mai2", "gemini-transcribe", "moss-pro"])
            self.assertEqual([engine.spec.id for engine in correction_engines], ["finesub"])
            # Nothing is opened until an engine asks.
            self.assertEqual(opened, [])
            self.assertEqual(transcription_engines[1]._store.key_pool("MOSS_PRO"), ("pool", "MOSS_PRO"))
            self.assertEqual(opened, [str(self.root)])
        self.assertEqual(worker.engine_context(None, None, ROOT), {"store": None, "runtime": None})
