"""agy's PreToolUse guards read the hook payload as UTF-8, the engine half.

Promises kept by `patches/finesub/0008-agy-guard-utf8-stdin.patch`: agy writes
the payload in UTF-8, while a text stdin decodes with the locale code page.
Under cp936 a CJK character right before an escaped quote swallows the
backslash, the guard dies on the JSON, and agy refuses the call -- a correction
`submit` was refused five times in one window (2026-10-13), and the first run
only got through on an all-ASCII placeholder answer.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from finesub.llm.agent.local_agent import (
    AGY_GUARD_SCRIPT,
    AGY_NATIVE_GUARD_SCRIPT,
    AGY_TOOL_GUARD_SCRIPT,
    AGY_TOOL_NATIVE_GUARD_SCRIPT,
)

GUARDS = {
    "media": AGY_GUARD_SCRIPT,
    "native": AGY_NATIVE_GUARD_SCRIPT,
    "tool": AGY_TOOL_GUARD_SCRIPT,
    "tool-native": AGY_TOOL_NATIVE_GUARD_SCRIPT,
}

# Lifted from the refused submits: "不明确" is nine UTF-8 bytes that cp936 reads
# as four pairs plus one byte, which takes the backslash escaping the quote.
SUBMIT_PAYLOAD = json.dumps(
    {
        "toolCall": {
            "name": "call_mcp_tool",
            "args": {
                "ServerName": "finesub",
                "ToolName": "submit",
                "Arguments": {"payload": '{"uncertainties":["不明确"]}'},
            },
        }
    },
    ensure_ascii=False,
).encode("utf-8")


def _run_guard(script: str, payload: bytes) -> subprocess.CompletedProcess[bytes]:
    with tempfile.TemporaryDirectory() as temp:
        path = Path(temp) / ".agents" / "scripts" / "guard_view_file.py"
        path.parent.mkdir(parents=True)
        path.write_text(script, encoding="utf-8")
        # The locale code page of a Chinese Windows, on any machine.
        env = dict(os.environ, PYTHONIOENCODING="gbk")
        env.pop("PYTHONUTF8", None)
        return subprocess.run(
            [sys.executable, str(path)], input=payload, capture_output=True, env=env
        )


class AgyGuardEncodingTests(unittest.TestCase):
    def test_the_payload_breaks_a_cp936_text_read(self) -> None:
        with self.assertRaises(json.JSONDecodeError):
            json.loads(SUBMIT_PAYLOAD.decode("gbk", "surrogateescape"))

    def test_every_guard_decides_on_a_cjk_payload(self) -> None:
        for kind, script in GUARDS.items():
            with self.subTest(guard=kind):
                result = _run_guard(script, SUBMIT_PAYLOAD)
                self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", "replace"))
                decision = json.loads(result.stdout)
                self.assertIn(decision["decision"], {"allow", "deny"})

    def test_tool_guards_still_allow_the_finesub_server(self) -> None:
        for kind in ("tool", "tool-native"):
            with self.subTest(guard=kind):
                result = _run_guard(GUARDS[kind], SUBMIT_PAYLOAD)
                self.assertEqual(json.loads(result.stdout)["decision"], "allow")


if __name__ == "__main__":
    unittest.main()
