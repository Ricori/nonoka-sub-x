from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from nonoka_x.telemetry import Telemetry, model_snapshot, safe_error
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_local_provider import ProviderFixture, wait_state


class TelemetryTests(unittest.TestCase):
    def test_snapshot_only_models_and_overrides(self):
        config = {"default_provider": "openai", "default_model": "model-a", "correction_provider": "anthropic",
                  "correction_model": "model-b", "api_key": "sk-secret", "base_url": "https://private"}
        value = model_snapshot(config, {"target": "final-srt"})
        self.assertEqual(value["default"]["model"], "model-a")
        self.assertEqual(value["overrides"]["correction"]["model"], "model-b")
        self.assertNotIn("secret", json.dumps(value))
        self.assertIsNone(model_snapshot(config, {"target": "raw-srt"}))

    def test_snapshot_follows_the_pipeline_target(self):
        config = {"default_provider": "openai", "default_model": "a"}
        for target in ("vocal", "aligned", "stable", "raw-srt"):
            self.assertIsNone(model_snapshot(config, {
                "target": target, "knowledge": "update", "correction": {"enabled": True},
            }))
        for target in ("translated-srt", "final-srt"):
            self.assertIsNotNone(model_snapshot(config, {"target": target, "knowledge": "none"}))

    def test_runtime_default_and_named_override_take_precedence(self):
        config = {"default_provider": "openai", "default_model": "a", "correction_provider": "openai", "correction_model": "b"}
        request = {"target": "final-srt", "llm_model": {"default": "group-a", "research": "group-b"}}
        value = model_snapshot(config, request)
        self.assertEqual(value["default"]["model"], "group-a")
        self.assertNotIn("correction", value["overrides"])
        self.assertEqual(value["overrides"]["research"]["model"], "group-b")
        request["llm_model"] = "correction=group-c"
        self.assertEqual(model_snapshot(config, request)["overrides"]["correction"]["model"], "group-c")
        request["llm_model"] = "C:/Users/private"
        self.assertNotIn("private", json.dumps(model_snapshot(config, request)))

    def test_default_collection_and_dedup_survive_legacy_preferences_and_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prefs = root / "preferences.json"
            prefs.write_text(json.dumps({"shareDiagnostics": False, "diagnosticsEpoch": 1}))
            queue = Telemetry(root)
            context = queue.context(None, {})
            for _ in range(3):
                queue.emit(context, "task_error", error=safe_error("unknown", "execution"))
            self.assertEqual(len(Telemetry(root).pending()), 1)
            prefs.write_text(json.dumps({"shareDiagnostics": False, "diagnosticsEpoch": 3}))
            queue.emit(context, "task_error", error=safe_error("unknown", "execution"))
            self.assertEqual(len(queue.pending()), 1)
            queue.ack([context["sample_id"] + ":task_error"])
            self.assertEqual(queue.pending(), [])

    def test_collects_with_missing_or_damaged_preferences(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for body in (None, "{}", "not json"):
                if body is not None:
                    (root / "preferences.json").write_text(body)
                queue = Telemetry(root)
                context = queue.context(None, {})
                self.assertIsNotNone(context)
                queue.emit(context, "task_error", error=safe_error("unknown", "execution"))
            self.assertEqual(len(Telemetry(root).pending()), 3)


class LifecycleTelemetryTests(ProviderFixture):
    def prepare(self):
        provider = self.provider()
        provider._settings = SimpleNamespace(telemetry_models=lambda request: {"default": {"provider": "openai", "model": "model-a"}, "overrides": {}})
        # The fixture has no engine credentials; keep execution environment independent.
        provider._engine_environment = lambda: dict(__import__("os").environ)
        return provider

    def await_events(self, provider, count):
        for _ in range(100):
            events = provider.telemetry.pending()
            if len(events) >= count:
                return events
            time.sleep(.02)
        self.fail(f"missing telemetry: {events}")

    def test_final_failure_pair_and_retry_new_sample(self):
        provider = self.prepare()
        task = provider.start(self.request("fail"))
        wait_state(provider, task["task_id"], {"failed"})
        events = self.await_events(provider, 2)
        self.assertEqual({e["type"] for e in events}, {"task_model_config", "task_error"})
        self.assertEqual(events[0]["sample_id"], events[1]["sample_id"])
        self.assertNotIn("API key is missing", json.dumps(events))
        provider._settings.telemetry_models = lambda request: {"default": {"provider": "openai", "model": "model-b"}, "overrides": {}}
        provider.retry(task["task_id"])
        wait_state(provider, task["task_id"], {"failed"})
        events = self.await_events(provider, 4)
        self.assertNotEqual(events[0]["sample_id"], events[2]["sample_id"])
        self.assertEqual(events[1]["models"]["default"]["model"], "model-a")
        self.assertEqual(events[3]["models"]["default"]["model"], "model-b")

    def test_success_only_config_and_start_rejection_only_error(self):
        provider = self.prepare()
        task = provider.start(self.request())
        wait_state(provider, task["task_id"], {"completed"})
        self.assertEqual([e["type"] for e in self.await_events(provider, 1)], ["task_model_config"])
        with self.assertRaises(Exception):
            provider.start({"schema": 9})
        events = self.await_events(provider, 2)
        self.assertEqual(events[-1]["error"]["phase"], "start")
