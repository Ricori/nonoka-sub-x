"""Reading the Nonoka Cloud relay's answers as a stream (vendor patch 0013).

On 2026-11-08 the relay finished a correction call in 236 s and the desktop
waited 2 h 48 min for the answer: a non-streaming read cannot tell a dead
connection from a slow model. These run the real transport against a local
server that misbehaves on purpose.
"""

from __future__ import annotations

import json
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from unittest import mock

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "third_party/finesub/src"))

from finesub.llm import client as client_module  # noqa: E402
from finesub.llm import llm_runtime, provider_transports  # noqa: E402
from finesub.llm.http import StreamStalled  # noqa: E402
from finesub.llm.provider_transports import ProviderHTTPError, ProviderStreamBroken  # noqa: E402

IDLE = 0.4


def event(value: Any) -> bytes:
    return b"data: " + json.dumps(value, ensure_ascii=False).encode("utf-8") + b"\n\n"


def delta(content: str) -> bytes:
    return event({"id": "resp_1", "created": 7, "model": "m", "choices": [{"index": 0, "delta": {"content": content}}]})


FINISH = event({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
USAGE = event({"choices": [], "usage": {"prompt_tokens": 3, "completion_tokens": 5, "total_tokens": 8}})
DONE = b"data: [DONE]\n\n"


class _Server:
    def __init__(self, script: Callable[[BaseHTTPRequestHandler], None]) -> None:
        owner = self
        self.requests: list[tuple[dict[str, str], dict[str, Any]]] = []

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self) -> None:
                body = self.rfile.read(int(self.headers["Content-Length"]))
                owner.requests.append((dict(self.headers.items()), json.loads(body)))
                script(self)

            def log_message(self, *_args: Any) -> None:
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        # A client giving up mid-write is what several of these tests are about.
        self.httpd.handle_error = lambda *_args: None
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.base_url = f"http://127.0.0.1:{self.httpd.server_port}/v1"

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


def streaming(*pieces: bytes, then: Callable[[BaseHTTPRequestHandler], None] | None = None):
    def script(handler: BaseHTTPRequestHandler) -> None:
        handler.send_response(200)
        handler.send_header("Content-Type", "text/event-stream")
        handler.send_header("Connection", "close")
        handler.end_headers()
        for piece in pieces:
            handler.wfile.write(piece)
            handler.wfile.flush()
        if then is not None:
            then(handler)
        handler.close_connection = True

    return script


def silent(_handler: BaseHTTPRequestHandler) -> None:
    time.sleep(IDLE * 3)


def repeating(piece: bytes):
    def then(handler: BaseHTTPRequestHandler) -> None:
        for _ in range(int(IDLE * 3 / 0.05)):
            handler.wfile.write(piece)
            handler.wfile.flush()
            time.sleep(0.05)

    return then


class StreamReadingTest(unittest.TestCase):
    def setUp(self) -> None:
        for name, value in (("STREAMED_HOSTS", frozenset({"127.0.0.1"})), ("STREAM_IDLE_SECONDS", IDLE)):
            patcher = mock.patch.object(provider_transports, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def call(self, script: Callable[[BaseHTTPRequestHandler], None], *, base_url: str | None = None) -> dict[str, Any]:
        self.server = _Server(script)
        self.addCleanup(self.server.close)
        return provider_transports.openai_compat_generate(
            base_url=base_url or self.server.base_url,
            api_key="k",
            model="m",
            messages=[{"role": "user", "content": "hi"}],
            max_tokens=10,
            thinking_level=None,
            timeout=30.0,
        )

    def assert_retryable(self, exc: BaseException) -> None:
        self.assertTrue(client_module.is_retryable_provider_error(exc), repr(exc))

    def test_folds_the_stream_into_the_answer_a_json_call_returns(self) -> None:
        answer = self.call(streaming(delta("你"), delta("好"), FINISH, USAGE, DONE))

        self.assertEqual(answer["choices"][0]["message"], {"role": "assistant", "content": "你好"})
        self.assertEqual(answer["choices"][0]["finish_reason"], "stop")
        self.assertEqual((answer["id"], answer["created"], answer["model"]), ("resp_1", 7, "m"))
        self.assertEqual(answer["usage"]["total_tokens"], 8)
        self.assertIn("prompt_tokens_details", answer["usage"])

    def test_asks_for_a_stream_with_usage_and_no_compression(self) -> None:
        self.call(streaming(delta("x"), FINISH, DONE))

        headers, body = self.server.requests[0]
        self.assertIs(body["stream"], True)
        self.assertEqual(body["stream_options"], {"include_usage": True})
        self.assertEqual(headers["Accept-Encoding"], "identity")
        self.assertEqual(headers["Accept"], "text/event-stream")

    def test_a_character_split_across_reads_and_a_line_separator_survive(self) -> None:
        whole = delta("甲 乙")
        cut = whole.index("乙".encode("utf-8")) + 1

        def pause(handler: BaseHTTPRequestHandler) -> None:
            handler.wfile.write(whole[cut:] + FINISH + DONE)

        def script(handler: BaseHTTPRequestHandler) -> None:
            streaming(whole[:cut], then=lambda inner: (time.sleep(0.05), pause(inner)))(handler)

        self.assertEqual(self.call(script)["choices"][0]["message"]["content"], "甲 乙")

    def test_a_silent_connection_is_given_up_on_in_idle_seconds(self) -> None:
        started = time.monotonic()
        with self.assertRaises(StreamStalled) as raised:
            self.call(streaming(delta("x"), then=silent))

        self.assertLess(time.monotonic() - started, 10.0)  # the deadline is 30 s
        self.assert_retryable(raised.exception)

    def test_a_server_that_never_answers_counts_as_stalled(self) -> None:
        with self.assertRaises(StreamStalled):
            self.call(silent)

    def test_keep_alive_comments_are_not_progress(self) -> None:
        with self.assertRaises(StreamStalled):
            self.call(streaming(delta("x"), then=repeating(b": keep-alive\n\n")))

    def test_empty_deltas_are_not_progress(self) -> None:
        empty = event({"choices": [{"index": 0, "delta": {"content": ""}}]})
        with self.assertRaises(StreamStalled):
            self.call(streaming(delta("x"), then=repeating(empty)))

    def test_steady_output_outlives_the_idle_limit(self) -> None:
        def then(handler: BaseHTTPRequestHandler) -> None:
            for _ in range(int(IDLE * 2 / 0.05)):
                handler.wfile.write(delta("x"))
                handler.wfile.flush()
                time.sleep(0.05)
            handler.wfile.write(FINISH + DONE)

        self.assertGreater(len(self.call(streaming(then=then))["choices"][0]["message"]["content"]), 5)

    def test_a_cut_connection_is_retryable(self) -> None:
        def cut(handler: BaseHTTPRequestHandler) -> None:
            handler.connection.shutdown(2)

        def chunked(handler: BaseHTTPRequestHandler) -> None:
            handler.send_response(200)
            handler.send_header("Content-Type", "text/event-stream")
            handler.send_header("Transfer-Encoding", "chunked")
            handler.end_headers()
            piece = delta("x")
            handler.wfile.write(b"%x\r\n%s\r\n" % (len(piece), piece))
            handler.wfile.flush()
            cut(handler)
            handler.close_connection = True

        with self.assertRaises(ProviderStreamBroken) as raised:
            self.call(chunked)
        self.assertIn("HTTP 502", str(raised.exception))
        self.assert_retryable(raised.exception)

    def test_a_stream_that_ends_early_is_retryable(self) -> None:
        with self.assertRaises(ProviderStreamBroken) as raised:
            self.call(streaming(delta("x")))
        self.assert_retryable(raised.exception)

    def test_an_error_event_is_retryable(self) -> None:
        with self.assertRaises(ProviderStreamBroken) as raised:
            self.call(streaming(delta("x"), event({"error": {"message": "boom"}})))
        self.assertIn("boom", str(raised.exception))
        self.assert_retryable(raised.exception)

    def test_a_finished_answer_is_kept_when_the_end_marker_never_comes(self) -> None:
        answer = self.call(streaming(delta("x"), FINISH, then=silent))
        self.assertEqual(answer["choices"][0]["message"]["content"], "x")

    def test_an_error_status_keeps_the_shape_the_classifier_reads(self) -> None:
        def script(handler: BaseHTTPRequestHandler) -> None:
            body = b'{"error":"key"}'
            handler.send_response(401)
            handler.send_header("Content-Type", "application/json")
            handler.send_header("Content-Length", str(len(body)))
            handler.end_headers()
            handler.wfile.write(body)

        with self.assertRaises(ProviderHTTPError) as raised:
            self.call(script)
        self.assertEqual(raised.exception.status_code, 401)

    def test_a_json_answer_on_the_stream_path_is_read_as_json(self) -> None:
        def script(handler: BaseHTTPRequestHandler) -> None:
            body = json.dumps({"choices": [{"message": {"content": "whole"}, "finish_reason": "stop"}]}).encode()
            handler.send_response(200)
            handler.send_header("Content-Type", "application/json")
            handler.send_header("Content-Length", str(len(body)))
            handler.end_headers()
            handler.wfile.write(body)

        self.assertEqual(self.call(script)["choices"][0]["message"]["content"], "whole")

    def test_other_hosts_stay_on_the_json_path(self) -> None:
        with mock.patch.object(provider_transports, "STREAMED_HOSTS", frozenset()):
            def script(handler: BaseHTTPRequestHandler) -> None:
                body = b'{"choices":[{"message":{"content":"plain"}}]}'
                handler.send_response(200)
                handler.send_header("Content-Type", "application/json")
                handler.send_header("Content-Length", str(len(body)))
                handler.end_headers()
                handler.wfile.write(body)

            answer = self.call(script)
        self.assertEqual(answer["choices"][0]["message"]["content"], "plain")
        self.assertNotIn("stream", self.server.requests[0][1])

    def test_the_tokyo_relay_is_streamed(self) -> None:
        mock.patch.stopall()
        self.assertIn("cloud-jp.nonoka.online", provider_transports.STREAMED_HOSTS)
        self.assertGreaterEqual(provider_transports.STREAM_IDLE_SECONDS, 60)


class RetryAccountingTest(unittest.TestCase):
    def test_a_stall_stays_out_of_the_consecutive_timeout_abort(self) -> None:
        stall = StreamStalled("stream timed out: no data for 300s")
        self.assertEqual(llm_runtime._status_code_from_exception(stall), "NO_RESPONSE_TIMEOUT")
        self.assertIsInstance(stall, httpx.TimeoutException)
        self.assertIn("StreamStalled", _abort_guard_source())


def _abort_guard_source() -> str:
    import inspect

    return inspect.getsource(llm_runtime.chat_complete)


if __name__ == "__main__":
    unittest.main()
