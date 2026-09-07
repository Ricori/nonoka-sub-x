"""What the task log says when an LLM call goes wrong -- and what it does not.

Before FineSub 0.5.0 the engine said nothing at all about the calls a run is
almost entirely made of, so a provider that was rate limiting, out of balance
or refusing every key looked from the desktop like a stage that had stopped
moving. Nonoka X carried two patches for that (0009 and 0010); upstream took
the job over and now records *every* LLM call, local agent call and web
retrieval as one `debug` line, with the endpoint's own words under `why` when
the call failed.

That fixes the silence and introduces the opposite problem. `NonokaXReporter`
forwards `debug` straight into the task log the user is watching, and one
correction pass makes hundreds of successful calls. So the filter is now
Nonoka X's half of the contract, and it is what this file pins:

- a failed call still reaches the task log, with the provider's own words;
- a successful one does not reach it at all;
- nothing else the engine logs at `debug` is affected by the rule.

The upstream half is pinned too, because it is a sync away from moving: the
message name and the `why` field are what the filter keys on, and a rename
upstream would silently turn the log back off.
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "third_party/finesub/src"))

from nonoka_x.worker import _API_CALL_MESSAGE, NonokaXReporter  # noqa: E402


def _events(call) -> list[dict]:
    """The NDJSON events one reporter call writes to stdout."""

    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        call()
    return [json.loads(line) for line in output.getvalue().splitlines()]


class ApiCallLogFilterTests(unittest.TestCase):
    def test_a_failed_call_reaches_the_task_log_with_the_providers_words(self) -> None:
        reporter = NonokaXReporter()
        fields = {
            "model": "gemini-3.7-flash",
            "tier": "GEMINI_PAID",
            "key": "key-2",
            "code": "429",
            "sec": "0.412",
            "n": 3,
            "why": "You exceeded your current quota",
        }
        events = _events(lambda: reporter.debug(_API_CALL_MESSAGE, fields))

        self.assertEqual(len(events), 1)
        payload = events[0]["payload"]
        self.assertEqual(events[0]["type"], "log")
        self.assertEqual(payload["message"], _API_CALL_MESSAGE)
        # The endpoint's own sentence, not one of ours: a bare 429 does not
        # separate "this key is spent today" from "slow down".
        self.assertEqual(payload["fields"]["why"], "You exceeded your current quota")
        self.assertEqual(payload["fields"]["code"], "429")

    def test_a_successful_call_says_nothing(self) -> None:
        """The flood this filter exists for.

        A successful call carries no `why`, and there are hundreds of them per
        correction pass. They stay in the engine's run log and in the per-call
        artifacts, so nothing is lost by keeping them out of the task log.
        """

        reporter = NonokaXReporter()
        events = _events(
            lambda: reporter.debug(
                _API_CALL_MESSAGE,
                {"model": "gemini-3.7-flash", "tier": "GEMINI_PAID", "code": "200"},
            )
        )

        self.assertEqual(events, [])

    def test_an_empty_reason_is_not_a_failure_either(self) -> None:
        reporter = NonokaXReporter()
        events = _events(
            lambda: reporter.debug(_API_CALL_MESSAGE, {"code": "200", "why": ""})
        )

        self.assertEqual(events, [])

    def test_every_other_debug_line_is_untouched(self) -> None:
        """The rule is about one message, not about `debug` in general."""

        reporter = NonokaXReporter()
        events = _events(
            lambda: reporter.debug("qwen finalize timing", {"device": "cpu"})
        )

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["payload"]["message"], "qwen finalize timing")
        self.assertEqual(events[0]["payload"]["fields"], {"device": "cpu"})

    def test_a_warning_is_never_filtered(self) -> None:
        reporter = NonokaXReporter()
        events = _events(
            lambda: reporter.warning("gpu-tier", "显存预算 8 GB 已改为显卡档位 standard")
        )

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["type"], "warning")


class UpstreamContractTests(unittest.TestCase):
    """The engine side of the filter, which a sync could move underneath it."""

    def test_the_engine_still_logs_api_calls_under_that_name(self) -> None:
        source = (
            ROOT / "third_party/finesub/src/finesub/llm/llm_runtime.py"
        ).read_text(encoding="utf-8")
        self.assertIn(f'current_reporter().debug("{_API_CALL_MESSAGE}"', source)

    def test_the_engine_still_carries_the_reason_under_why(self) -> None:
        """`why` is the field the filter reads, and the one users need.

        Upstream sets it only when the call failed, which is exactly the
        distinction the filter turns into "worth showing".
        """

        source = (
            ROOT / "third_party/finesub/src/finesub/llm/llm_runtime.py"
        ).read_text(encoding="utf-8")
        self.assertIn('fields["why"]', source)


if __name__ == "__main__":
    unittest.main()
