"""Stand-in for the correction package's resident process: the protocol without the engine.

The real one (`python -m finesub_correct`) imports FineSub. What the provider
is responsible for is the channel around it -- the engine host's line
protocol: the launch context first, then one `{"op": "llm.complete"}` per
call answered by one `{"type": "result"}`; a dead process replaced, a call
that never answers timed out -- so this fake answers that protocol and
nothing else.

The request's first message picks the behaviour, which keeps the fixture out of
argv and lets one process serve several calls in a test.
"""

from __future__ import annotations

import json
import sys
import time


def reply(payload: dict) -> None:
    print(json.dumps({"type": "result", "payload": payload}), flush=True)


def main() -> int:
    context = sys.stdin.readline()
    if not context.strip():
        return 0
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        message = json.loads(line)
        if message.get("op") != "llm.complete":
            print(json.dumps({"type": "error", "payload": {"message": f"unknown request {message.get('op')!r}"}}),
                  flush=True)
            continue
        request = message["request"]
        mode = request["messages"][0]["content"]
        if mode == "hang":
            time.sleep(30)
        if mode == "die":
            return 1
        if mode == "refuse":
            reply({"ok": False, "error": {"code": "quota_exceeded", "message": "daily quota spent"}})
            continue
        # What the engine reports while it works travels on the same channel, and is not the answer.
        print(json.dumps({"type": "reporter", "method": "debug", "args": ["llm api call", {}], "kwargs": {}}),
              flush=True)
        reply({
            "ok": True,
            "content": f"answered {mode} as {request['role']}",
            "model": "fixture-model",
            "backend": "fixture",
            "fallback_used": False,
        })
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
