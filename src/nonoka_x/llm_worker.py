"""Long-lived FineSub LLM worker. Stdin is NDJSON requests; stdout is NDJSON replies.

One process, one routed client, many calls. The task worker (`nonoka_x.worker`)
exists for the duration of one pipeline run and reports progress as it goes;
this one is the opposite shape -- it answers single calls for as long as the
sidecar keeps it around, because building the client is the expensive part and
a caller that corrects fifty lines makes fifty calls.

It runs in the managed environment for the same reason the task worker does:
`finesub` is installed there, not in the interpreter serving the sidecar.

The protocol is one line in, one line out, in order. Errors are replies too --
a spent key or a refused prompt is an answer about that call, not a reason to
lose the client every other call is about to reuse.
"""

from __future__ import annotations

import json
import sys
import traceback
from typing import Any, Mapping

# The text-only roles. `nonoka_x.local_provider.LLM_ROLES` states the same list
# for callers that cannot import the engine; both are checked against
# `finesub.llm.routing.config.LLMRole` in tests/test_vendor_contract.py.
ROLES = ("lightweight", "general_capable")


def reply(value: Mapping[str, Any]) -> None:
    print(json.dumps(value, ensure_ascii=False, separators=(",", ":")), flush=True)


class Session:
    """The routed client, built once on first use."""

    def __init__(self) -> None:
        self._client: Any = None

    def client(self) -> Any:
        if self._client is None:
            from finesub.llm.client import RoleClient

            self._client = RoleClient()
        return self._client

    def close(self) -> None:
        if self._client is None:
            return
        try:
            self._client.close()
        except Exception:  # noqa: BLE001 - shutdown must not raise
            pass
        self._client = None

    def complete(self, request: Mapping[str, Any]) -> dict[str, Any]:
        from finesub.llm.routing.config import LLMRole

        role = str(request.get("role") or "")
        if role not in ROLES:
            raise ValueError("role must be one of: " + ", ".join(ROLES))
        messages = [
            {"role": str(item["role"]), "content": str(item["content"])}
            for item in request["messages"]
        ]
        result = self.client().complete(
            LLMRole(role),
            messages,
            max_tokens=int(request.get("max_tokens", 8192)),
            temperature=float(request.get("temperature", 1.0)),
        )
        return {
            "ok": True,
            "content": result.content,
            "model": result.model,
            "backend": result.backend,
            "fallback_used": bool(result.fallback_used),
        }


def failure(exc: BaseException) -> dict[str, Any]:
    """Turn an exception into a reply.

    The message is the vendor's own words where there are any: what separates a
    rate limit from a spent key from a CLI that is not signed in is exactly
    what the endpoint said, and the caller has no other way to learn it.
    """

    message = str(exc).strip() or type(exc).__name__
    code = "llm_failed"
    try:
        from finesub.llm.client import is_quota_or_rate_limit_error

        if is_quota_or_rate_limit_error(exc):
            code = "quota_exceeded"
    except Exception:  # noqa: BLE001 - classification is a nicety, not the answer
        pass
    return {"ok": False, "error": {"code": code, "message": message}}


def main() -> int:
    session = Session()
    try:
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                answer = session.complete(json.loads(line))
            except Exception as exc:  # noqa: BLE001 - every failure is a reply
                traceback.print_exc(file=sys.stderr)
                answer = failure(exc)
            reply(answer)
    finally:
        session.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
