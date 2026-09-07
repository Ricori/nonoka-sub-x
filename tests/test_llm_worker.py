from __future__ import annotations

import contextlib
import io
import json
import sys
import types
import unittest
from enum import Enum
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from nonoka_x import llm_worker


class Role(str, Enum):
    LIGHTWEIGHT = "lightweight"
    GENERAL_CAPABLE = "general_capable"


class FakeClient:
    """Counts its own construction: one client is the point of the worker."""

    built = 0
    calls: list[tuple] = []

    def __init__(self) -> None:
        type(self).built += 1
        self.closed = False

    def complete(self, role, messages, **kwargs):
        type(self).calls.append((role, messages, kwargs))
        if messages[-1]["content"] == "boom":
            raise RuntimeError("the endpoint refused: daily quota spent")
        return types.SimpleNamespace(
            content="answer",
            model="fixture-model",
            backend="fixture",
            fallback_used=False,
        )

    def close(self) -> None:
        self.closed = True


@contextlib.contextmanager
def engine():
    """The two engine modules the worker imports, and nothing else.

    The real ones sit behind `finesub.llm`, which drags in httpx and the whole
    routing package; the worker only ever touches `RoleClient`, `LLMRole` and
    the quota classifier, so those are what stand in here.
    """

    client_module = types.ModuleType("finesub.llm.client")
    client_module.RoleClient = FakeClient
    client_module.is_quota_or_rate_limit_error = lambda exc: "quota" in str(exc)
    config = types.ModuleType("finesub.llm.routing.config")
    config.LLMRole = Role
    modules = {
        "finesub": types.ModuleType("finesub"),
        "finesub.llm": types.ModuleType("finesub.llm"),
        "finesub.llm.client": client_module,
        "finesub.llm.routing": types.ModuleType("finesub.llm.routing"),
        "finesub.llm.routing.config": config,
    }
    for name in ("finesub", "finesub.llm", "finesub.llm.routing"):
        modules[name].__path__ = []
    previous = {name: sys.modules.get(name) for name in modules}
    sys.modules.update(modules)
    FakeClient.built = 0
    FakeClient.calls = []
    try:
        yield
    finally:
        for name, value in previous.items():
            if value is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = value


def run(requests: list[dict]) -> list[dict]:
    output = io.StringIO()
    old_stdin, old_stderr = sys.stdin, sys.stderr
    try:
        sys.stdin = io.StringIO("".join(json.dumps(item) + "\n" for item in requests))
        sys.stderr = io.StringIO()
        with contextlib.redirect_stdout(output):
            llm_worker.main()
    finally:
        sys.stdin, sys.stderr = old_stdin, old_stderr
    return [json.loads(line) for line in output.getvalue().splitlines()]


def call(prompt: str, role: str = "lightweight") -> dict:
    return {"role": role, "messages": [{"role": "user", "content": prompt}], "max_tokens": 128}


class LLMWorkerTests(unittest.TestCase):
    def test_repeated_calls_share_one_client(self) -> None:
        with engine():
            replies = run([call("one"), call("two")])
        self.assertEqual([reply["content"] for reply in replies], ["answer", "answer"])
        self.assertEqual(FakeClient.built, 1)

    def test_a_failed_call_is_a_reply_and_the_next_one_still_runs(self) -> None:
        """A spent key must not cost the caller the worker.

        Building the client is the expensive part, and the failure says nothing
        about the requests queued behind it -- so the exception becomes this
        call's answer and the loop carries on.
        """

        with engine():
            replies = run([call("boom"), call("after")])
        self.assertFalse(replies[0]["ok"])
        self.assertEqual(replies[0]["error"]["code"], "quota_exceeded")
        self.assertIn("daily quota spent", replies[0]["error"]["message"])
        self.assertTrue(replies[1]["ok"])

    def test_an_unlisted_role_is_refused_without_reaching_the_engine(self) -> None:
        with engine():
            replies = run([call("one", role="audio_multimodal")])
        self.assertFalse(replies[0]["ok"])
        self.assertIn("role must be one of", replies[0]["error"]["message"])
        self.assertEqual(FakeClient.built, 0)

    def test_call_arguments_reach_the_client_unchanged(self) -> None:
        with engine():
            run([{**call("one", role="general_capable"), "temperature": 0.25}])
            role, messages, kwargs = FakeClient.calls[0]
        self.assertEqual(role, Role.GENERAL_CAPABLE)
        self.assertEqual(messages, [{"role": "user", "content": "one"}])
        self.assertEqual(kwargs, {"max_tokens": 128, "temperature": 0.25})


if __name__ == "__main__":
    unittest.main()
