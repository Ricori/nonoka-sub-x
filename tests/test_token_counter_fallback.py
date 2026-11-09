"""Token counting never waits on the network or a stuck binary (vendor patch 0016).

A 2026-11-09 desktop correction spent 39 of its 40 minutes on connect timeouts
to Gemini's `countTokens`, once per uncached text, on a machine with no
`tokcount` and no route to Google. These pin the patch: the chain is the local
binary then the estimate, an error falls through at once, and a binary that
hangs costs one bounded wait for the whole process.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "third_party" / "finesub" / "src"))

from finesub.llm import token_budget  # noqa: E402
from finesub.llm.token_budget import (  # noqa: E402
    FallbackTokenCounter,
    HeuristicTokenCounter,
    LocalGeminiTokenCounter,
    default_token_counter,
)

TIMEOUT = 0.5
# Far above the timeout, far below the 60 s a second wait would add in practice.
BOUND = 3.0

STUB = """
import json, sys, time
mode = sys.argv[1]
if mode == "exit":
    sys.exit(1)
if mode == "mute":
    time.sleep(60)
for line in sys.stdin:
    if mode == "answer":
        print(json.dumps({"tokens": len(json.loads(line)["text"])}), flush=True)
"""


@pytest.fixture
def launch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Make the counter start a Python stub in place of the Go binary."""

    stub = tmp_path / "stub.py"
    stub.write_text(STUB, encoding="utf-8")
    exe = tmp_path / "tokcount.exe"
    exe.write_bytes(b"")
    exe.chmod(0o755)
    real_popen = subprocess.Popen
    spawned: list[subprocess.Popen] = []

    def start(mode: str) -> tuple[LocalGeminiTokenCounter, list[subprocess.Popen]]:
        def popen(_args, **kwargs):
            process = real_popen([sys.executable, "-I", str(stub), mode], **kwargs)
            spawned.append(process)
            return process

        monkeypatch.setattr(token_budget.subprocess, "Popen", popen)
        return LocalGeminiTokenCounter(exe_path=str(exe), timeout_seconds=TIMEOUT), spawned

    yield start
    for process in spawned:
        if process.poll() is None:
            process.kill()
            process.wait()
    token_budget._shutdown_local_counter_services()


def elapsed(call) -> float:
    started = time.monotonic()
    try:
        call()
    except RuntimeError:
        pass
    return time.monotonic() - started


def test_the_chain_is_the_local_binary_then_the_estimate() -> None:
    assert [type(counter) for counter in default_token_counter().counters] == [
        LocalGeminiTokenCounter,
        HeuristicTokenCounter,
    ]
    assert not hasattr(token_budget, "GeminiCountTokensCounter")


def test_a_working_binary_is_used(launch) -> None:
    counter, _spawned = launch("answer")
    assert counter.count_text("abc") == 3 + token_budget.LOCAL_COUNTER_API_OFFSET


def test_a_binary_that_dies_falls_through_at_once(launch) -> None:
    local, _spawned = launch("exit")
    chain = FallbackTokenCounter(counters=(local, HeuristicTokenCounter()))
    started = time.monotonic()
    assert chain.count_text("some subtitle text") > 0
    assert time.monotonic() - started < BOUND
    assert chain.source == HeuristicTokenCounter.source


def test_a_hang_costs_one_wait_for_every_later_text(launch) -> None:
    local, spawned = launch("hang")
    chain = FallbackTokenCounter(counters=(local, HeuristicTokenCounter()))
    started = time.monotonic()
    for index in range(20):
        assert chain.count_text(f"distinct text {index}") > 0
    assert time.monotonic() - started < BOUND
    assert len(spawned) == 1
    spawned[0].wait(timeout=BOUND)


def test_a_binary_that_stops_reading_cannot_block_the_write(launch) -> None:
    counter, _spawned = launch("mute")
    # Larger than any pipe buffer, so the write itself is what blocks.
    assert elapsed(lambda: counter.count_text("x" * (8 * 1024 * 1024))) < BOUND


def test_a_hang_is_reported_once(launch) -> None:
    counter, _spawned = launch("hang")
    reporter = mock.Mock()
    with mock.patch.object(token_budget, "current_reporter", return_value=reporter):
        for index in range(3):
            elapsed(lambda text=f"text {index}": counter.count_text(text))
    reporter.warning.assert_called_once()
    assert reporter.warning.call_args.args[0] == "tokcount-timeout"
