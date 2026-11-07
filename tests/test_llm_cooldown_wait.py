"""Waiting out a key cooldown when nothing else can answer (vendor patch 0013).

A 2026-11-07 desktop task, configured with only the free Nonoka Cloud
provider, failed at 75% with "No API key for NONOKA_CLOUD could be tried: 1 in
a transient cooldown". One upstream hiccup had spent the relay key's sticky
retries, the key went into a 20-minute skip, and with no other key or provider
the next correction window failed the run -- "继续" before the skip ended
failed the same way. These pin the patch: the skip is 5 minutes, sticky retries
are 6, a chain that failed *only* on cooldowns sleeps until the skip ends and
says so, and anything else still fails at once.
"""

from __future__ import annotations

import inspect
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest import mock

import httpx


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "third_party/finesub/src"))

from finesub.llm import client as client_module  # noqa: E402
from finesub.llm import llm_runtime  # noqa: E402
from finesub.llm.rate_limit import (  # noqa: E402
    COMBO_COOLDOWN_SKIP_SECONDS,
    ComboCooldownPhase,
    ModelRateLimiter,
)
from finesub.llm.routing import api_keys  # noqa: E402
from finesub.llm.routing import model_router  # noqa: E402
from finesub.llm.routing.config import LLMRole, ModelEndpoint, RoleModelConfig  # noqa: E402
from finesub.reporting import reporting_to  # noqa: E402


class _Recorder:
    def __init__(self) -> None:
        self.warnings: list[tuple[str, str]] = []

    def warning(self, code: str, message: str, *, impact: str = "", action: str = "") -> None:
        self.warnings.append((code, message))

    def __getattr__(self, _name: str):
        return lambda *args, **kwargs: None


def _limiter(directory: str) -> ModelRateLimiter:
    return ModelRateLimiter(state_path=Path(directory) / "state.json")


class CooldownSettingsTests(unittest.TestCase):
    def test_skip_window_is_five_minutes(self) -> None:
        self.assertEqual(COMBO_COOLDOWN_SKIP_SECONDS, 5 * 60)
        with tempfile.TemporaryDirectory() as directory:
            limiter = _limiter(directory)
            endpoint = ModelEndpoint("NONOKA_CLOUD", "gpt-6-luna")
            started = datetime(2026, 11, 7, 6, 0, tzinfo=timezone.utc)
            limiter.note_combo_exhausted(endpoint, key_id="k", now=started)
            self.assertIs(
                limiter.combo_cooldown_phase(endpoint, key_id="k", now=started + timedelta(minutes=4)),
                ComboCooldownPhase.SKIP,
            )
            self.assertEqual(
                limiter.combo_cooldown_retry_at(endpoint, key_id="k"),
                started + timedelta(minutes=5),
            )
            self.assertIs(
                limiter.combo_cooldown_phase(endpoint, key_id="k", now=started + timedelta(minutes=6)),
                ComboCooldownPhase.PROBE,
            )

    def test_sticky_retries_default_to_six(self) -> None:
        parameter = inspect.signature(client_module.RoleClient.__init__).parameters["max_retries"]
        self.assertEqual(parameter.default, 6)


class ChatCompleteCoolingTests(unittest.TestCase):
    """`chat_complete` says whether "no key could be tried" ends by itself."""

    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.limiter = _limiter(self._dir.name)
        self.endpoint = ModelEndpoint("NONOKA_CLOUD", "gpt-6-luna")
        patcher = mock.patch.dict(os.environ, {"NONOKA_TEST_COOL_KEY": "sk-test-cooling"})
        patcher.start()
        self.addCleanup(patcher.stop)
        dotenv = mock.patch.object(llm_runtime, "_read_dotenv", return_value={})
        dotenv.start()
        self.addCleanup(dotenv.stop)
        self.key_id = api_keys.ApiKeyEntry(name="", key="sk-test-cooling", named=False).key_id

    def _call(self) -> api_keys.ProviderUnavailableError:
        with self.assertRaises(api_keys.ProviderUnavailableError) as raised:
            llm_runtime.chat_complete(
                [{"role": "user", "content": "hi"}],
                provider_tier="NONOKA_CLOUD",
                model="gpt-6-luna",
                rate_limiter=self.limiter,
                provider_spec={
                    "kind": "openai_compat",
                    "base_url": "http://127.0.0.1:9/v1",
                    "key_env": "NONOKA_TEST_COOL_KEY",
                },
            )
        return raised.exception

    def test_a_key_in_its_skip_window_is_cooling_with_an_end_time(self) -> None:
        started = datetime.now(timezone.utc)
        self.limiter.note_combo_exhausted(self.endpoint, key_id=self.key_id, now=started)
        error = self._call()
        self.assertTrue(error.cooling)
        self.assertIn("transient cooldown", str(error))
        self.assertIsNotNone(error.cooldown_until)
        self.assertAlmostEqual(
            (error.cooldown_until - started).total_seconds(), COMBO_COOLDOWN_SKIP_SECONDS, delta=1
        )

    def test_a_daily_lock_is_not_cooling(self) -> None:
        self.limiter.mark_daily_exhausted(self.endpoint, key_id=self.key_id)
        error = self._call()
        self.assertFalse(error.cooling)

    def test_a_missing_key_is_not_cooling(self) -> None:
        with mock.patch.dict(os.environ, {"NONOKA_TEST_COOL_KEY": ""}):
            error = self._call()
        self.assertFalse(error.cooling)


class ChatCompleteFailureTests(unittest.TestCase):
    """What a key that was actually tried leaves behind (2026-11-07, second report).

    The same task, resumed, failed twice more without one cooldown skip: a
    3-hour hung generation followed by a dropped TLS handshake aborted the call
    as "two consecutive timeouts", and the next morning the relay's upstream
    answered 522 until the sticky retries ran out.
    """

    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.limiter = _limiter(self._dir.name)
        # RPM/TPM accounting needs a catalog fact this fixture tier has none of;
        # the cooldown bookkeeping under test does not.
        self.limiter.note_request = lambda *args, **kwargs: None  # type: ignore[method-assign]
        for patcher in (
            mock.patch.dict(os.environ, {"NONOKA_TEST_COOL_KEY": "sk-test-cooling"}),
            mock.patch.object(llm_runtime, "_read_dotenv", return_value={}),
            mock.patch.object(llm_runtime.time, "sleep"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def _call(self, outcomes: list[Any], *, retries: int = 3) -> Any:
        def generate_once(**_kwargs):
            outcome = outcomes.pop(0)
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome

        with mock.patch.object(llm_runtime, "_generate_once", side_effect=generate_once):
            return llm_runtime.chat_complete(
                [{"role": "user", "content": "hi"}],
                provider_tier="NONOKA_CLOUD",
                model="gpt-6-luna",
                retries=retries,
                rate_limiter=self.limiter,
                provider_spec={
                    "kind": "openai_compat",
                    "base_url": "http://127.0.0.1:9/v1",
                    "key_env": "NONOKA_TEST_COOL_KEY",
                },
            )

    def test_a_dropped_handshake_does_not_count_toward_the_timeout_abort(self) -> None:
        from finesub.llm.http import DeadlineExceeded

        answer = {"choices": [{"message": {"content": "ok"}}]}
        result = self._call(
            [
                DeadlineExceeded("request timed out: exceeded its 10600s hard deadline and was closed"),
                httpx.ConnectTimeout("_ssl.c:993: The handshake operation timed out"),
                httpx.ConnectTimeout("_ssl.c:993: The handshake operation timed out"),
                answer,
            ]
        )
        self.assertEqual(result["choices"][0]["message"]["content"], "ok")

    def test_two_hard_deadlines_still_abort(self) -> None:
        from finesub.llm.http import DeadlineExceeded

        with self.assertRaises(DeadlineExceeded) as raised:
            self._call(
                [
                    DeadlineExceeded("request timed out: exceeded its 10600s hard deadline and was closed"),
                    DeadlineExceeded("request timed out: exceeded its 10600s hard deadline and was closed"),
                ]
            )
        self.assertTrue(getattr(raised.exception, "_harness_consecutive_timeout_abort", False))
        self.assertFalse(getattr(raised.exception, "cooling", False))

    def test_spent_retries_on_a_5xx_leave_the_error_cooling(self) -> None:
        started = datetime.now(timezone.utc)
        with self.assertRaises(Exception) as raised:
            self._call([RuntimeError("HTTP 522 error code: 522") for _ in range(4)])
        error = raised.exception
        self.assertIn("522", str(error))
        self.assertTrue(getattr(error, "cooling", False))
        self.assertAlmostEqual(
            (error.cooldown_until - started).total_seconds(), COMBO_COOLDOWN_SKIP_SECONDS, delta=5
        )

    def test_a_permanent_error_is_not_cooling(self) -> None:
        with self.assertRaises(Exception) as raised:
            self._call([RuntimeError("HTTP 400 bad request")])
        self.assertFalse(getattr(raised.exception, "cooling", False))


def _cooling_error(*, until: datetime | None, only_cooling: bool = True) -> api_keys.ProviderUnavailableError:
    error = api_keys.ProviderUnavailableError(
        "No API key for NONOKA_CLOUD could be tried: 1 in a transient cooldown after recent errors.",
        cooling=True,
        cooldown_until=until,
    )
    setattr(error, "_harness_chain_only_cooling", only_cooling)
    return error


class CompleteWaitTests(unittest.TestCase):
    """`RoleClient.complete` around a stubbed chain."""

    def _client(self, outcomes: list[Any]) -> tuple[Any, list[int]]:
        client = client_module.RoleClient.__new__(client_module.RoleClient)
        calls: list[int] = []

        def routed(role, messages, **kwargs):
            calls.append(1)
            outcome = outcomes.pop(0)
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome

        client._complete_routed = routed  # type: ignore[attr-defined]
        return client, calls

    def test_waits_until_the_skip_ends_then_retries_and_says_so(self) -> None:
        until = datetime.now(timezone.utc) + timedelta(seconds=120)
        client, calls = self._client([_cooling_error(until=until), "answer"])
        recorder = _Recorder()
        with mock.patch.object(client_module.time, "sleep") as sleep, reporting_to(recorder):
            result = client.complete(LLMRole.AUDIO_MULTIMODAL, [])
        self.assertEqual(result, "answer")
        self.assertEqual(len(calls), 2)
        (delay,), _ = sleep.call_args
        self.assertGreater(delay, 115)
        self.assertLess(delay, 125)
        self.assertEqual([code for code, _ in recorder.warnings], ["llm-cooldown-wait"])
        self.assertIn("冷却", recorder.warnings[0][1])

    def test_a_provider_error_that_started_the_cooldown_is_waited_out_too(self) -> None:
        error = RuntimeError("HTTP 522 error code: 522")
        error.cooling = True  # type: ignore[attr-defined]
        error.cooldown_until = datetime.now(timezone.utc) + timedelta(seconds=300)  # type: ignore[attr-defined]
        error._harness_chain_only_cooling = True  # type: ignore[attr-defined]
        client, calls = self._client([error, "answer"])
        recorder = _Recorder()
        with mock.patch.object(client_module.time, "sleep"), reporting_to(recorder):
            self.assertEqual(client.complete(LLMRole.AUDIO_MULTIMODAL, []), "answer")
        self.assertIn("522", recorder.warnings[0][1])

    def test_a_key_left_to_a_probe_is_polled(self) -> None:
        client, calls = self._client([_cooling_error(until=None), "answer"])
        with mock.patch.object(client_module.time, "sleep") as sleep, reporting_to(_Recorder()):
            client.complete(LLMRole.AUDIO_MULTIMODAL, [])
        sleep.assert_called_once_with(client_module._COOLDOWN_PROBE_POLL_SECONDS)

    def test_a_chain_with_another_failure_is_not_waited_out(self) -> None:
        error = _cooling_error(until=datetime.now(timezone.utc), only_cooling=False)
        client, calls = self._client([error])
        with mock.patch.object(client_module.time, "sleep") as sleep:
            with self.assertRaises(api_keys.ProviderUnavailableError):
                client.complete(LLMRole.AUDIO_MULTIMODAL, [])
        sleep.assert_not_called()
        self.assertEqual(len(calls), 1)

    def test_a_missing_key_fails_at_once(self) -> None:
        error = api_keys.ProviderUnavailableError("Provider X is disabled or has no selected API key.")
        setattr(error, "_harness_chain_only_cooling", True)
        client, calls = self._client([error])
        with mock.patch.object(client_module.time, "sleep") as sleep:
            with self.assertRaises(api_keys.ProviderUnavailableError):
                client.complete(LLMRole.AUDIO_MULTIMODAL, [])
        sleep.assert_not_called()

    def test_gives_up_once_the_budget_is_spent(self) -> None:
        budget = client_module._COOLDOWN_WAIT_BUDGET_SECONDS
        poll = client_module._COOLDOWN_PROBE_POLL_SECONDS
        rounds = int(budget // poll)
        client, calls = self._client([_cooling_error(until=None) for _ in range(rounds + 1)])
        with mock.patch.object(client_module.time, "sleep") as sleep, reporting_to(_Recorder()):
            with self.assertRaises(api_keys.ProviderUnavailableError):
                client.complete(LLMRole.AUDIO_MULTIMODAL, [])
        self.assertEqual(sleep.call_count, rounds)
        self.assertEqual(len(calls), rounds + 1)


class ChainMarkingTests(unittest.TestCase):
    """The real candidate loop marks whether only cooldowns stopped it."""

    def _client(self, endpoints: list[ModelEndpoint]) -> Any:
        config = RoleModelConfig(
            role=LLMRole.GENERAL_CAPABLE,
            endpoint_chain=tuple(endpoints),
            test_endpoint=endpoints[0],
        )
        return client_module.RoleClient(
            role_configs={LLMRole.GENERAL_CAPABLE: config},
            rate_limiter=ModelRateLimiter(enabled=False),
        )

    def _run(self, endpoints: list[ModelEndpoint], failures: dict[str, BaseException]) -> BaseException:
        client = self._client(endpoints)

        def chat_complete(messages, **kwargs):
            raise failures[kwargs["provider_tier"]]

        with mock.patch.object(model_router, "provider_enabled", return_value=True), mock.patch.object(
            llm_runtime, "chat_complete", side_effect=chat_complete
        ):
            with self.assertRaises(BaseException) as raised:
                client._complete_routed(LLMRole.GENERAL_CAPABLE, [{"role": "user", "content": "hi"}])
        return raised.exception

    @staticmethod
    def _endpoint(tier: str) -> ModelEndpoint:
        return ModelEndpoint(tier, "m", backend="openai_compat", unverified=True)

    def test_a_lone_cooling_candidate_is_marked(self) -> None:
        error = self._run(
            [self._endpoint("NONOKA_CLOUD")],
            {"NONOKA_CLOUD": _cooling_error(until=None, only_cooling=False)},
        )
        self.assertTrue(getattr(error, "_harness_chain_only_cooling"))
        self.assertIsNotNone(client_module._cooldown_wait_seconds(error, waited=0))

    def test_an_earlier_real_failure_unmarks_the_chain(self) -> None:
        error = self._run(
            [self._endpoint("NONOKA_OPENAI"), self._endpoint("NONOKA_CLOUD")],
            {
                "NONOKA_OPENAI": RuntimeError("HTTP 503 upstream unavailable"),
                "NONOKA_CLOUD": _cooling_error(until=None, only_cooling=False),
            },
        )
        self.assertIsInstance(error, api_keys.ProviderUnavailableError)
        self.assertFalse(getattr(error, "_harness_chain_only_cooling"))
        self.assertIsNone(client_module._cooldown_wait_seconds(error, waited=0))


if __name__ == "__main__":
    unittest.main()
