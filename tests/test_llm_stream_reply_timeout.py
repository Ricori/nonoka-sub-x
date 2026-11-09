"""A streamed LLM call is judged by its replies, not its bytes (vendor patch 0008).

Answer and thinking text are replies. Keep-alive comments, empty deltas and
usage-only chunks prove a connection is open, not that a model is answering, so
a relay holding a dead upstream open must not look alive. No reply for ten
minutes sends a backup request; ten more with neither answering ends the pair.
A call that keeps replying is never cut for its total length.
"""

from __future__ import annotations

import json
import sys
import threading
import time
import unittest
from pathlib import Path
from typing import Any
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "third_party/finesub/src"))

from finesub.llm import http  # noqa: E402
from finesub.llm.client import is_retryable_provider_error  # noqa: E402
from finesub.llm.http import (  # noqa: E402
    DeadlineExceeded,
    StreamCancelled,
    StreamInterrupted,
    StreamProgress,
    hedge_on_silence,
    stream_within_deadline,
)
from finesub.llm.streaming import (  # noqa: E402
    AnthropicMessagesAssembler,
    GeminiAssembler,
    OpenAIChatAssembler,
)


BACKUP_AFTER = 0.15
CUT_AFTER = 0.15


def _until_cancelled(progress: StreamProgress, *, each_slice: Any) -> str:
    while not progress.cancelled:
        each_slice()
        time.sleep(0.01)
    raise StreamCancelled("closed")


class HedgedRequestTests(unittest.TestCase):
    def setUp(self) -> None:
        patcher = mock.patch.object(http, "CUT_AFTER_BACKUP_NO_REPLY_SECONDS", CUT_AFTER)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.backups: list[StreamProgress] = []
        self.ended: list[Any] = []

    def hedge(self, send: Any) -> Any:
        return hedge_on_silence(
            send,
            silence_seconds=BACKUP_AFTER,
            on_backup=self.backups.append,
            on_end=self.ended.append,
        )

    def test_keep_alives_get_a_backup_then_both_requests_are_cut(self) -> None:
        def send(progress: StreamProgress) -> str:
            return _until_cancelled(progress, each_slice=progress.line)

        started = time.monotonic()
        with self.assertRaisesRegex(StreamInterrupted, "no reply"):
            self.hedge(send)

        elapsed = time.monotonic() - started
        self.assertEqual(len(self.backups), 1)
        self.assertGreaterEqual(elapsed, BACKUP_AFTER + CUT_AFTER)
        self.assertLess(elapsed, BACKUP_AFTER + CUT_AFTER + 1.5)
        self.assertEqual(len(self.ended), 2)
        self.assertTrue(all(isinstance(item.error, StreamCancelled) for item in self.ended))

    def test_a_reply_during_the_backup_restarts_the_second_ten_minutes(self) -> None:
        calls = {"count": 0}
        lock = threading.Lock()

        def send(progress: StreamProgress) -> str:
            with lock:
                calls["count"] += 1
                backup = calls["count"] == 2
            if not backup:
                return _until_cancelled(progress, each_slice=lambda: None)
            # Replies for three times the cut, then answers.
            ends = time.monotonic() + CUT_AFTER * 3
            while time.monotonic() < ends:
                progress.reply()
                time.sleep(0.02)
            return "answer"

        self.assertEqual(self.hedge(send), "answer")

    def test_a_call_still_replying_gets_no_backup(self) -> None:
        def send(progress: StreamProgress) -> str:
            ends = time.monotonic() + BACKUP_AFTER * 3
            while time.monotonic() < ends:
                progress.output("字")
                time.sleep(0.02)
            return "answer"

        self.assertEqual(self.hedge(send), "answer")
        self.assertEqual(self.backups, [])

    def test_a_failed_backup_still_leaves_the_first_request_its_ten_minutes(self) -> None:
        calls = {"count": 0}
        lock = threading.Lock()

        def send(progress: StreamProgress) -> str:
            with lock:
                calls["count"] += 1
                backup = calls["count"] == 2
            if backup:
                raise RuntimeError("HTTP 429")
            return _until_cancelled(progress, each_slice=lambda: None)

        started = time.monotonic()
        with self.assertRaises(StreamInterrupted):
            self.hedge(send)
        self.assertGreaterEqual(time.monotonic() - started, BACKUP_AFTER + CUT_AFTER)

    def test_the_cut_is_retried_by_type(self) -> None:
        self.assertTrue(is_retryable_provider_error(StreamInterrupted("no reply")))


class _Response:
    status_code = 200

    def __init__(self, lines: list[tuple[float, str]]) -> None:
        self.lines = lines

    def iter_lines(self) -> Any:
        for delay, line in self.lines:
            time.sleep(delay)
            yield line


class _Client:
    def __init__(self, lines: list[tuple[float, str]]) -> None:
        self.lines = lines

    def __enter__(self) -> "_Client":
        return self

    def __exit__(self, *_exc: Any) -> None:
        return None

    def close(self) -> None:
        return None

    def stream(self, *_args: Any, **_kwargs: Any) -> Any:
        client = self

        class _Context:
            def __enter__(self) -> _Response:
                return _Response(client.lines)

            def __exit__(self, *_exc: Any) -> None:
                return None

        return _Context()


class _Assembler:
    def __init__(self, progress: StreamProgress) -> None:
        self.progress = progress

    def feed_line(self, line: str) -> None:
        if line.startswith("text:"):
            self.progress.output(line[5:])

    def finish(self) -> str:
        return self.progress.output_text()


class StreamDeadlineTests(unittest.TestCase):
    def call(self, lines: list[tuple[float, str]], **kwargs: Any) -> str:
        progress = StreamProgress()
        return stream_within_deadline(
            "https://relay.example/v1",
            headers={},
            json={},
            deadline_seconds=0.2,
            assembler=_Assembler(progress),
            error_for_status=lambda response: RuntimeError("status"),
            progress=progress,
            client_factory=lambda **_: _Client(lines),
            **kwargs,
        )

    def test_a_call_still_replying_outlives_its_hard_deadline(self) -> None:
        lines = [(0.1, f"text:{index}") for index in range(5)]
        with mock.patch.object(http, "REPLY_DEADLINE_SECONDS", 0.3):
            self.assertEqual(self.call(lines, extend_on_reply=True), "01234")

    def test_keep_alives_do_not_extend_the_deadline(self) -> None:
        lines = [(0.1, ": keep-alive") for _ in range(5)]
        with mock.patch.object(http, "REPLY_DEADLINE_SECONDS", 0.3):
            with self.assertRaises(DeadlineExceeded):
                self.call(lines, extend_on_reply=True)

    def test_callers_that_did_not_ask_keep_the_hard_deadline(self) -> None:
        lines = [(0.1, f"text:{index}") for index in range(5)]
        with self.assertRaises(DeadlineExceeded):
            self.call(lines)


class WhatCountsAsAReplyTests(unittest.TestCase):
    @staticmethod
    def feed(assembler: Any, event: Any, name: str = "") -> None:
        if name:
            assembler.feed_line(f"event: {name}")
        assembler.feed_line("data: " + json.dumps(event))
        assembler.feed_line("")

    def test_openai_keep_alives_and_empty_deltas_are_not_replies(self) -> None:
        progress = StreamProgress()
        assembler = OpenAIChatAssembler(progress)
        assembler.feed_line(": keep-alive")
        self.feed(assembler, {"choices": [{"index": 0, "delta": {"content": ""}}]})
        self.feed(assembler, {"choices": [], "usage": {"total_tokens": 1}})
        self.assertIsNone(progress.last_reply_at)

    def test_openai_reasoning_and_content_are_replies(self) -> None:
        for delta in ({"reasoning_content": "想"}, {"reasoning": "想"}, {"content": "答"}):
            with self.subTest(delta=delta):
                progress = StreamProgress()
                self.feed(OpenAIChatAssembler(progress), {"choices": [{"index": 0, "delta": delta}]})
                self.assertIsNotNone(progress.last_reply_at)

    def test_anthropic_thinking_is_a_reply(self) -> None:
        progress = StreamProgress()
        self.feed(
            AnthropicMessagesAssembler(progress),
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "thinking_delta", "thinking": "想"},
            },
            "content_block_delta",
        )
        self.assertIsNotNone(progress.last_reply_at)

    def test_gemini_thought_is_a_reply_but_not_answer_text(self) -> None:
        progress = StreamProgress()
        assembler = GeminiAssembler(progress, lambda status, body: RuntimeError(body))
        self.feed(
            assembler,
            {"candidates": [{"index": 0, "content": {"parts": [{"text": "想", "thought": True}]}}]},
        )
        self.assertIsNotNone(progress.last_reply_at)
        self.assertEqual(progress.output_chars, 0)


if __name__ == "__main__":
    unittest.main()
