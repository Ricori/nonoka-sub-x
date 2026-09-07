import contextlib
import io
import json
import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from nonoka_x import local_provider, worker


class WorkerLogOptimizationTests(unittest.TestCase):
    def test_reporter_deduplicates_and_compacts_routing_preset_audio_warnings(self) -> None:
        reporter = worker.NonokaXReporter()
        output = io.StringIO()

        warnings = [
            "correction-mm/quality: 组内没有成员支持音频（local-codex-gpt-5_6-terra），带媒体的调用会在能力过滤后无候选可用",
            "correction-mm/intermediate: 组内没有成员支持音频（local-codex-gpt-5_6-terra），带媒体的调用会在能力过滤后无候选可用",
            "correction-mm/efficiency: 组内没有成员支持音频（local-codex-gpt-5_6-terra），带媒体的调用会在能力过滤后无候选可用",
            "planning-mm/quality: 组内没有成员支持音频（local-codex-gpt-5_6-terra），带媒体的调用会在能力过滤后无候选可用",
            "planning-mm/intermediate: 组内没有成员支持音频（local-codex-gpt-5_6-terra），带媒体的调用会在能力过滤后无候选可用",
            "planning-mm/efficiency: 组内没有成员支持音频（local-codex-gpt-5_6-terra），带媒体的调用会在能力过滤后无候选可用",
        ]

        with contextlib.redirect_stdout(output):
            for w in warnings:
                reporter.warning("routing-preset", w)

        lines = [json.loads(line) for line in output.getvalue().splitlines() if line.strip()]
        # Out of 6 identical core warnings, only 1 should be emitted!
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]["type"], "warning")
        self.assertIn("多模态组: 组内没有成员支持音频", lines[0]["payload"]["message"])

    def test_reporter_emits_distinct_warnings(self) -> None:
        reporter = worker.NonokaXReporter()
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            reporter.warning("routing-preset", "group1: 警告A")
            reporter.warning("routing-preset", "group2: 警告B")
            reporter.warning("routing-preset", "group1: 警告A")  # duplicate

        lines = [json.loads(line) for line in output.getvalue().splitlines() if line.strip()]
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[0]["payload"]["message"], "group1: 警告A")
        self.assertEqual(lines[1]["payload"]["message"], "group2: 警告B")

    def test_srt_line_budget_downgraded_to_log(self) -> None:
        reporter = worker.NonokaXReporter()
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            reporter.warning(
                "srt-line-budget",
                "workspace/sub.srt: Segment 1 line has 26 weighted characters; limit is 25.",
            )

        lines = [json.loads(line) for line in output.getvalue().splitlines() if line.strip()]
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]["type"], "log")
        self.assertEqual(lines[0]["payload"]["code"], "srt-line-budget")
        self.assertIn("Segment 1 line has 26 weighted characters", lines[0]["payload"]["message"])

    def test_format_worker_exception_extracts_vendor_error(self) -> None:
        class DummyLocalAgentError(Exception):
            pass

        exc = DummyLocalAgentError("Codex CLI exited with status 1 (capsule correction-text-123; inspect events/stderr.log)")
        setattr(
            exc,
            "_harness_execution_attempts",
            [
                {
                    "backend": "local_agent",
                    "driver": "codex",
                    "vendor_error": "You've hit your usage limit. Upgrade to Pro or try again at 12:48 AM.",
                }
            ],
        )

        formatted = worker.format_worker_exception(exc)
        self.assertIn("本地模型 CLI 调用失败", formatted)
        self.assertIn("You've hit your usage limit", formatted)
        self.assertNotIn("inspect events/stderr.log", formatted)

    def test_format_worker_exception_fallback_when_no_vendor_error(self) -> None:
        exc = RuntimeError("Generic engine crash")
        formatted = worker.format_worker_exception(exc)
        self.assertEqual(formatted, "RuntimeError: Generic engine crash")

    def test_classify_failure_detects_quota_and_auth(self) -> None:
        self.assertEqual(
            local_provider.classify_failure("You've hit your usage limit. Upgrade to Pro..."),
            "quota_exceeded",
        )
        self.assertEqual(
            local_provider.classify_failure("LocalAgentTransientError: 本地模型 CLI 调用失败: You've hit your usage limit..."),
            "quota_exceeded",
        )
        self.assertEqual(
            local_provider.classify_failure("Claude Code is not authenticated; run `claude /login`"),
            "auth_failed",
        )
        self.assertEqual(
            local_provider.classify_failure("Codex CLI exited with status 1"),
            "agent_failed",
        )
        self.assertEqual(
            local_provider.classify_failure("RuntimeError: unknown error"),
            "engine_failed",
        )


if __name__ == "__main__":
    unittest.main()
