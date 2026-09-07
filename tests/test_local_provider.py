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

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from nonoka_x.local_provider import (
    LocalProvider,
    ProviderError,
    _clear_legacy_separator_decode_probes,
    _prepare_msvc_environment,
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
            ROOT / "third_party/finesub",
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
        self.assertIn("stable_json", provider.artifacts(task["task_id"])["artifacts"])
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

    def test_worker_environment_sets_cuda_visible_devices(self) -> None:
        provider = self.provider()
        req = self.request("encoding-environment")
        req["device"] = "cuda:0"
        task = provider.start(req)
        wait_state(provider, task["task_id"], {"completed"})
        logs = [event for event in provider.events(task["task_id"])["events"] if event["type"] == "log"]
        self.assertEqual(logs[0]["payload"]["cuda_visible_devices"], "0")
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

    def test_old_separator_unicode_probe_is_retried_once(self) -> None:
        accel = self.root / "models" / "audio-separator" / "accel"
        decode_probe = accel / "decode" / "probe.json"
        compiler_probe = accel / "compiler" / "probe.json"
        for probe, reason in (
            (decode_probe, "InductorError: UnicodeDecodeError: 'utf-8' codec cannot decode byte 0xd3"),
            (compiler_probe, "CppCompileError: compiler returned exit status 2"),
        ):
            probe.parent.mkdir(parents=True)
            probe.write_text(json.dumps({"aoti": "unavailable", "reason": reason}), encoding="utf-8")

        _clear_legacy_separator_decode_probes({"FINESUB_MODEL_DIR": str(self.root / "models")})

        self.assertFalse(decode_probe.exists())
        self.assertTrue(compiler_probe.exists())

    def test_msvc_environment_is_activated_when_standard_headers_are_missing(self) -> None:
        program_files = self.root / "Program Files (x86)"
        vswhere = program_files / "Microsoft Visual Studio" / "Installer" / "vswhere.exe"
        install = self.root / "Visual Studio" / "BuildTools"
        vcvars = install / "VC" / "Auxiliary" / "Build" / "vcvars64.bat"
        vswhere.parent.mkdir(parents=True)
        vswhere.write_bytes(b"")
        vcvars.parent.mkdir(parents=True)
        vcvars.write_bytes(b"")
        calls: list[list[str] | str] = []

        def run(command: list[str] | str, **_kwargs: object) -> SimpleNamespace:
            calls.append(command)
            if isinstance(command, (list, tuple)) and command and command[0] == str(vswhere):
                return SimpleNamespace(returncode=0, stdout=str(install) + "\n")
            return SimpleNamespace(
                returncode=0,
                stdout=f"INCLUDE={self.root / 'msvc' / 'include'}{os.pathsep}{self.root / 'sdk' / 'include'}\nLIB={self.root / 'msvc' / 'lib'}\n",
            )

        environment = {"ProgramFiles(x86)": str(program_files), "PATH": "existing"}
        _prepare_msvc_environment(environment, _run=run, _windows=True)

        self.assertEqual(len(calls), 2)
        self.assertIn(str(self.root / "msvc" / "include"), environment["INCLUDE"])
        self.assertEqual(environment["LIB"], str(self.root / "msvc" / "lib"))

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

        from nonoka_x.local_provider import TARGET_READINESS

        self.assertEqual(TARGET_READINESS["vocal"], "raw-srt")
        self.assertEqual(TARGET_READINESS["stable"], "raw-srt")
        self.assertEqual(TARGET_READINESS["translated-srt"], "final-srt")

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
        self.assertIn("stable_json", provider.artifacts(task["task_id"])["artifacts"])
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
            ROOT / "third_party/finesub",
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
        ):
            with self.subTest(label):
                with self.assertRaisesRegex(ProviderError, expected):
                    provider.llm_complete(payload)
        self.assertIsNone(provider._llm_process)
        provider.shutdown()


if __name__ == "__main__":
    unittest.main()
