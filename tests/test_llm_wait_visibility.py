"""What the log says while an LLM call is still running (vendor patch 0011).

A 2026-10-29 task sat 29 minutes between the routing notes and the first
correction window with not one line in the task log: the preparation task was
waiting on a slow model, its successful calls were filtered out, and the soft
timeout -- sized for a 64k answer -- was still 18 minutes away. These pin the
three lines that now fill that gap: the research round saying it is waiting and
how long the answer took, a parse retry said out loud, and a heartbeat while a
single call is in flight.
"""

from __future__ import annotations

import sys
import threading
import time
import unittest
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "third_party/finesub/src"))

from finesub.llm import llm_runtime, research  # noqa: E402
from finesub.llm.client import LLMCallResult  # noqa: E402
from finesub.llm.routing.config import LLMRole  # noqa: E402
from finesub.reporting import reporting_to  # noqa: E402


class _Recorder:
    def __init__(self) -> None:
        self.lines: list[tuple[str, str, Any]] = []
        self._lock = threading.Lock()

    def debug(self, message: str, fields: Any = None) -> None:
        with self._lock:
            self.lines.append(("debug", message, dict(fields or {})))

    def warning(self, code: str, message: str, *, impact: str = "", action: str = "") -> None:
        with self._lock:
            self.lines.append(("warning", code, message))

    def __getattr__(self, _name: str):
        return lambda *args, **kwargs: None


class _Counter:
    def count_text(self, text: str) -> int:
        return len(text or "")

    def count_messages(self, messages: Any) -> int:
        return sum(len(str(message)) for message in messages)


class _Client:
    def __init__(self, replies: list[str]) -> None:
        self._replies = list(replies)

    def complete(self, role, messages, **_kwargs) -> LLMCallResult:
        return LLMCallResult(
            content=self._replies.pop(0),
            role=role,
            model="qwen3.8",
            fallback_used=False,
            raw_response={},
        )


def _parser(text: str) -> str:
    if text != "ok":
        raise ValueError("missing <context> block")
    return text


class ResearchCallLinesTests(unittest.TestCase):
    def _run(self, replies: list[str], retries: int = 2) -> _Recorder:
        recorder = _Recorder()
        with reporting_to(recorder):
            research._call_and_parse(
                _Client(replies),
                [{"role": "user", "content": "transcript"}],
                parser=_parser,
                round_name="prepare t1",
                task_artifact_dir=None,
                task_id="t",
                artifact_kind="research_round1_response",
                max_parse_retries=retries,
                token_counter=_Counter(),
                role=LLMRole.GENERAL_CAPABLE,
            )
        return recorder

    def test_a_round_says_it_is_waiting_and_how_long_the_answer_took(self) -> None:
        lines = self._run(["ok"]).lines
        debug = [(message, fields) for kind, message, fields in lines if kind == "debug"]

        self.assertEqual(debug[0], ("research prepare t1: waiting for the model", {"attempt": 0}))
        self.assertEqual(debug[1][0], "research prepare t1: answered")
        self.assertEqual(debug[1][1]["model"], "qwen3.8")
        self.assertIn("sec", debug[1][1])

    def test_a_parse_retry_is_a_warning(self) -> None:
        lines = self._run(["garbage", "ok"]).lines
        warnings = [line for line in lines if line[0] == "warning"]

        self.assertEqual(len(warnings), 1)
        self.assertEqual(warnings[0][1], "research-parse-retry")
        self.assertIn("missing <context> block", warnings[0][2])
        waits = [line for line in lines if line[1] == "research prepare t1: waiting for the model"]
        self.assertEqual([fields["attempt"] for _, _, fields in waits], [0, 1])

    def test_the_last_failed_attempt_does_not_promise_another(self) -> None:
        recorder = _Recorder()
        with self.assertRaises(RuntimeError), reporting_to(recorder):
            research._call_and_parse(
                _Client(["bad", "bad"]),
                [{"role": "user", "content": "transcript"}],
                parser=_parser,
                round_name="prepare t1",
                task_artifact_dir=None,
                task_id="t",
                artifact_kind="research_round1_response",
                max_parse_retries=1,
                token_counter=_Counter(),
                role=LLMRole.GENERAL_CAPABLE,
            )
        warnings = [line for line in recorder.lines if line[0] == "warning"]
        self.assertEqual(len(warnings), 1)


class WaitHeartbeatTests(unittest.TestCase):
    def test_a_call_in_flight_says_so_before_the_soft_deadline(self) -> None:
        recorder = _Recorder()
        with reporting_to(recorder):
            with llm_runtime._SoftDeadline(10.0, model="qwen3.8", hard=20.0, heartbeat=0.05):
                time.sleep(0.18)
        beats = [line for line in recorder.lines if line[1] == "llm api call still waiting"]

        self.assertGreaterEqual(len(beats), 2)
        self.assertEqual(beats[0][2]["model"], "qwen3.8")
        self.assertFalse([line for line in recorder.lines if line[0] == "warning"])

    def test_the_soft_deadline_still_warns_once_and_beats_continue(self) -> None:
        recorder = _Recorder()
        with reporting_to(recorder):
            with llm_runtime._SoftDeadline(0.05, model="qwen3.8", hard=1.0, heartbeat=0.04):
                time.sleep(0.25)
        warnings = [line for line in recorder.lines if line[0] == "warning"]

        self.assertEqual(len(warnings), 1)
        self.assertEqual(warnings[0][1], "llm-api-soft-timeout")
        later = recorder.lines[recorder.lines.index(warnings[0]) + 1 :]
        self.assertTrue([line for line in later if line[1] == "llm api call still waiting"])

    def test_a_quick_call_says_nothing(self) -> None:
        recorder = _Recorder()
        with reporting_to(recorder):
            with llm_runtime._SoftDeadline(10.0, model="qwen3.8", hard=20.0, heartbeat=5.0):
                pass
        time.sleep(0.05)

        self.assertEqual(recorder.lines, [])


if __name__ == "__main__":
    unittest.main()
