"""The editor's and plugins' single LLM calls, in the correction package's resident process.

Moved from the core (`nonoka_x.llm_worker`) into the correction package
(WP-P1): one routed client, many calls. The sidecar keeps this process around
because building the client is the expensive part and a caller that corrects
fifty lines makes fifty calls. Errors are replies too -- a spent key or a
refused prompt is an answer about that call, not a reason to lose the client
every other call is about to reuse.
"""

from __future__ import annotations

from typing import Any, Mapping

# The text-only roles plugins may ask for. `nonoka_x.local_provider.LLM_ROLES`
# states the same list for callers that cannot import the engine.
ROLES = ("lightweight", "general_capable")

# The engine role each plugin role is routed through, checked against
# `finesub.llm.routing.config.LLMRole` in tests/test_vendor_contract.py.
# FineSub 0.6.0 dropped its own lightweight role (and the model group behind
# it); the name stays in the plugin API so existing plugins keep working.
ENGINE_ROLES = {"lightweight": "general_capable", "general_capable": "general_capable"}


class Session:
    """The routed client, built once on first use."""

    def __init__(self) -> None:
        self._client: Any = None
        self._cooldown_free_client: Any = None

    def client(self) -> Any:
        if self._client is None:
            from finesub.llm.client import RoleClient

            self._client = RoleClient()
        return self._client

    def cooldown_free_client(self) -> Any:
        """A client that neither honours a key's cooldown nor starts one.

        For a person asking whether a model works right now: the answer has to
        come from the provider each time, and a failed check must not cost the
        next pipeline run its only key.
        """

        if self._cooldown_free_client is None:
            from finesub.llm.client import RoleClient
            from finesub.llm.rate_limit import ModelRateLimiter

            self._cooldown_free_client = RoleClient(rate_limiter=ModelRateLimiter(enabled=False))
        return self._cooldown_free_client

    def close(self) -> None:
        for client in (self._client, self._cooldown_free_client):
            if client is None:
                continue
            try:
                client.close()
            except Exception:  # noqa: BLE001 - shutdown must not raise
                pass
        self._client = self._cooldown_free_client = None

    def complete(self, request: Mapping[str, Any]) -> dict[str, Any]:
        from finesub.llm.routing.config import LLMRole

        role = str(request.get("role") or "")
        if role not in ROLES:
            raise ValueError("role must be one of: " + ", ".join(ROLES))
        messages = [
            {"role": str(item["role"]), "content": str(item["content"])}
            for item in request["messages"]
        ]
        client = self.cooldown_free_client() if request.get("ignore_cooldown") else self.client()
        result = client.complete(
            LLMRole(ENGINE_ROLES[role]),
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
