"""Stand-in for `nonoka_x.llm_worker`: the protocol without the engine.

The real worker imports FineSub, which is not installed in the interpreter
running the tests. What the provider is responsible for is the channel around
it -- one line in, one line out, a dead worker replaced, a call that never
answers timed out -- so this fake answers that protocol and nothing else.

The request's first message picks the behaviour, which keeps the fixture out of
argv and lets one worker serve several calls in a test.
"""

from __future__ import annotations

import json
import sys
import time


def main() -> int:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        request = json.loads(line)
        mode = request["messages"][0]["content"]
        if mode == "hang":
            time.sleep(30)
        if mode == "die":
            return 1
        if mode == "refuse":
            print(
                json.dumps({"ok": False, "error": {"code": "quota_exceeded", "message": "daily quota spent"}}),
                flush=True,
            )
            continue
        print(
            json.dumps(
                {
                    "ok": True,
                    "content": f"answered {mode} as {request['role']}",
                    "model": "fixture-model",
                    "backend": "fixture",
                    "fallback_used": False,
                }
            ),
            flush=True,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
