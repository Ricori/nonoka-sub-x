"""Small diagnostic queue. Never persist raw task errors or content."""

from __future__ import annotations

import json
import os
import re
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROUTES = ("correction", "planning", "research", "search_judge", "knowledge")
CODES = {
    "quota_exceeded", "auth_failed", "missing_llm_key", "missing_gpu", "missing_model",
    "insufficient_disk", "missing_dependency", "agent_failed", "engine_failed",
    "invalid_request", "invalid_state", "runtime_not_ready", "sidecar_unavailable",
    "rate_limited", "timeout", "network_error", "out_of_memory", "unknown",
}
STAGES = {"unknown", "media", "separate", "separation", "vocal", "vad", "asr", "align", "aligned",
          "stable", "raw-srt", "final-srt", "correction", "planning", "research", "knowledge", "translation"}


def timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def identifier(value: object) -> str:
    text = str(value or "")
    if (not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:/+-]{0,127}", text)
            or "://" in text or re.match(r"^[A-Za-z]:/", text) or ".." in text.split("/") or text.lower().startswith(("sk-", "bearer", "eyj"))):
        return ""
    return text


def model_snapshot(config: dict, request: dict) -> dict | None:
    if request.get("target") not in {"translated-srt", "final-srt"}:
        return None

    def route(prefix: str) -> dict | None:
        provider = identifier(config.get(prefix + "_provider"))
        model = identifier(config.get(prefix + "_model"))
        return {"provider": provider, "model": model} if provider and model else None

    result = {"default": route("default"), "overrides": {}}
    for name in ROUTES:
        if value := route(name):
            result["overrides"][name] = value
    override = request.get("llm_model")
    # Mirror the engine's [group=]target syntax without importing its runtime.
    if isinstance(override, str) and override.strip():
        name, separator, target = override.strip().partition("=")
        override = {name.strip(): target.strip()} if separator else {"default": name}
    if isinstance(override, dict):
        def group(value: object) -> dict:
            return {"provider": "model-group", "model": identifier(value) or "unresolved-override"}

        if "default" in override:
            result = {"default": group(override["default"]), "overrides": {}}
        for name in ROUTES:
            values = [group(override[key])["model"] for key in (name, name + "-mm", name + "-text") if key in override]
            if values:
                result["overrides"][name] = group("+".join(sorted(set(values))))
    return result if result["default"] or result["overrides"] else None


def safe_error(code: str, phase: str, stage: str = "unknown") -> dict:
    code = code if code in CODES else "unknown"
    return {"phase": phase, "stage": stage if stage in STAGES else "unknown",
            "code": code, "message": "Task failed: " + code}


class Telemetry:
    def __init__(self, root: Path) -> None:
        self.path = root / "telemetry-sidecar.json"
        self.lock = threading.RLock()

    def _read(self) -> list[dict]:
        try:
            items = json.loads(self.path.read_text("utf-8"))
            cutoff = datetime.now(timezone.utc).timestamp() - 86400
            return [v for v in items if datetime.fromisoformat(v["occurred_at"]).timestamp() >= cutoff][-50:]
        except (OSError, ValueError, TypeError, KeyError):
            return []

    def _write(self, items: list[dict]) -> None:
        temporary = self.path.with_suffix(".tmp")
        with temporary.open("w", encoding="utf-8") as output:
            json.dump(items[-50:], output, ensure_ascii=False)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, self.path)

    def pending(self) -> list[dict]:
        try:
            with self.lock:
                items = self._read()
                self._write(items)
                return items
        except OSError:
            return []

    def ack(self, ids: list[str]) -> None:
        try:
            with self.lock:
                self._write([v for v in self._read() if v["sample_id"] + ":" + v["type"] not in ids])
        except OSError:
            pass

    def context(self, settings, request: dict) -> dict | None:
        try:
            models = settings.telemetry_models(request) if settings is not None else None
            return {"sample_id": str(uuid.uuid4()), "models": models, "execution_provider": "local"}
        except Exception:
            return None

    def emit(self, context: dict | None, kind: str, *, error: dict | None = None, at: str | None = None) -> bool:
        try:
            with self.lock:
                if not context or (kind == "task_model_config" and not context.get("models")):
                    return True
                items = self._read()
                if any(v["sample_id"] == context["sample_id"] and v["type"] == kind for v in items):
                    return True
                event = {**context, "type": kind, "occurred_at": at or timestamp()}
                if error:
                    event["error"] = error
                items.append(event)
                self._write(items)
                return True
        except Exception:
            return False


def classify_error(code: str, message: str) -> str:
    lowered = message.lower()
    for result, phrases in (
        ("out_of_memory", ("out of memory", "cuda oom")),
        ("rate_limited", ("429", "rate limit", "too many requests")),
        ("timeout", ("timed out", "timeout")),
        ("network_error", ("connection refused", "connection reset", "dns", "network unreachable")),
    ):
        if any(phrase in lowered for phrase in phrases):
            return result
    return code if code in CODES else "unknown"
