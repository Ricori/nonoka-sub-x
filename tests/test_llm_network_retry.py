"""Retrying LLM calls over network failures (vendor patch 0013).

A 2026-11-09 desktop correction failed four hours in on one
`ConnectError: [Errno 11001] getaddrinfo failed`: the retry check matched
transport failures by wording, and its markers only ever fit the timeouts.
These pin the patch: network failures are retried by type and are transient to
the route policy, a request that can never work still fails at once, a failed
connection stays out of the timeout abort, and the message names the host.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "third_party/finesub/src"))
sys.path.insert(0, str(ROOT / "tests"))

from finesub.llm import llm_runtime  # noqa: E402
from finesub.llm.client import is_retryable_provider_error  # noqa: E402
from finesub.llm.http import DeadlineExceeded, name_request_host  # noqa: E402
from finesub.llm.routing import model_router  # noqa: E402
from test_llm_cooldown_wait import _limiter  # noqa: E402

DNS = "[Errno 11001] getaddrinfo failed"
REQUEST = httpx.Request("POST", "https://cloud-jp.nonoka.online/v1/llm/chat/completions")


class RetryCheckTests(unittest.TestCase):
    def test_network_failures_are_retryable_and_transient(self) -> None:
        for exc in (
            httpx.ConnectError(DNS),
            httpx.ConnectError("[Errno -3] Temporary failure in name resolution"),
            httpx.ConnectError("[WinError 10054] connection reset"),
            httpx.ReadError("reset"),
            httpx.WriteError("broken pipe"),
            httpx.RemoteProtocolError("Server disconnected without sending a response."),
            httpx.ProxyError("503 Service Unavailable"),
        ):
            with self.subTest(exc=repr(exc)):
                self.assertTrue(is_retryable_provider_error(exc))
                self.assertIs(model_router.classify_failure(exc), model_router.FailureKind.TRANSIENT)

    def test_a_request_that_cannot_work_is_not_retried(self) -> None:
        for exc in (httpx.UnsupportedProtocol("no scheme"), httpx.LocalProtocolError("illegal header")):
            with self.subTest(exc=repr(exc)):
                self.assertFalse(is_retryable_provider_error(exc))


class HostNamingTests(unittest.TestCase):
    def test_the_host_is_added_once(self) -> None:
        exc = httpx.ConnectError(DNS, request=REQUEST)
        name_request_host(exc)
        name_request_host(exc)
        self.assertEqual(str(exc), f"{DNS} (host: cloud-jp.nonoka.online)")

    def test_an_error_without_a_request_is_left_alone(self) -> None:
        exc = httpx.ConnectError(DNS)
        name_request_host(exc)
        self.assertEqual(str(exc), DNS)

    def test_other_errors_are_left_alone(self) -> None:
        exc = ValueError("bad")
        name_request_host(exc)
        self.assertEqual(str(exc), "bad")


class ChatCompleteTests(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.limiter = _limiter(self._dir.name)
        self.limiter.note_request = lambda *args, **kwargs: None  # type: ignore[method-assign]
        for patcher in (
            mock.patch.dict(os.environ, {"NONOKA_TEST_NET_KEY": "sk-test-net"}),
            mock.patch.object(llm_runtime, "_read_dotenv", return_value={}),
            mock.patch.object(llm_runtime.time, "sleep"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def _call(self, outcomes: list[Any]) -> Any:
        def generate_once(**_kwargs):
            outcome = outcomes.pop(0)
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome

        with mock.patch.object(llm_runtime, "_generate_once", side_effect=generate_once):
            return llm_runtime.chat_complete(
                [{"role": "user", "content": "hi"}],
                provider_tier="NONOKA_CLOUD",
                model="gpt-6.1-terra",
                retries=3,
                rate_limiter=self.limiter,
                provider_spec={
                    "kind": "openai_compat",
                    "base_url": "http://127.0.0.1:9/v1",
                    "key_env": "NONOKA_TEST_NET_KEY",
                },
            )

    def test_a_dns_failure_is_retried_until_the_answer(self) -> None:
        answer = {"choices": [{"message": {"content": "ok"}}]}
        result = self._call([httpx.ConnectError(DNS), httpx.ReadError("reset"), answer])
        self.assertEqual(result["choices"][0]["message"]["content"], "ok")

    def test_a_failed_connection_stays_out_of_the_timeout_abort(self) -> None:
        deadline = "request timed out: exceeded its 10600s hard deadline and was closed"
        with self.assertRaises(DeadlineExceeded):
            self._call(
                [
                    DeadlineExceeded(deadline),
                    httpx.ConnectError(DNS),
                    DeadlineExceeded(deadline),
                    {"choices": [{"message": {"content": "must not be reached"}}]},
                ]
            )

    def test_a_spent_retry_budget_reports_the_host(self) -> None:
        failures = [httpx.ConnectError(DNS, request=REQUEST) for _ in range(12)]
        with self.assertRaises(httpx.ConnectError) as raised:
            self._call(failures)
        self.assertIn("(host: cloud-jp.nonoka.online)", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
