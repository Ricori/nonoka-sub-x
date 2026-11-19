"""Subprocess entrypoint for running local MOSS 0.9B inside the Python environment layer (`python -I -B -m ...`)."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Mapping

from ... import TranscriptionJob
from . import MossLocalEngine


def _emit(payload: Mapping[str, Any]) -> None:
    sys.stdout.write(json.dumps(dict(payload), ensure_ascii=False) + "\n")
    sys.stdout.flush()


class _SubprocessRuntime:
    def __init__(self, models: Mapping[str, str], tools: Mapping[str, str]) -> None:
        self._models = {str(k): Path(v) for k, v in models.items() if v}
        self._tools = {str(k): Path(v) for k, v in tools.items() if v}

    def model_dir(self, model_id: str) -> Path | None:
        path = self._models.get(model_id)
        return path if path is not None and path.is_dir() else None

    def tool_path(self, name: str) -> Path | None:
        path = self._tools.get(name)
        return path if path is not None and path.is_file() else None

    def worker_python(self) -> Path | None:
        return None


class _WireReporter:
    def stage(self, stage: str, message: str = "") -> None:
        _emit({"type": "reporter", "method": "stage", "args": [stage, message], "kwargs": {}})

    def progress(
        self,
        stage: str,
        *,
        completed: int,
        total: int | None = None,
        unit: str = "",
        message: str = "",
    ) -> None:
        _emit({
            "type": "reporter",
            "method": "progress",
            "args": [stage],
            "kwargs": {"completed": completed, "total": total, "unit": unit, "message": message},
        })

    def log(self, message: str, fields: Mapping[str, Any] | None = None) -> None:
        _emit({"type": "reporter", "method": "log", "args": [message, dict(fields or {})], "kwargs": {}})

    def warning(self, code: str, message: str, *, impact: str = "", action: str = "") -> None:
        from ...reporting import compose

        _emit({
            "type": "reporter",
            "method": "warning",
            "args": [code, compose(message, impact=impact, action=action)],
            "kwargs": {},
        })

    def debug(self, message: str, fields: Mapping[str, Any] | None = None) -> None:
        _emit({"type": "reporter", "method": "debug", "args": [message, dict(fields or {})], "kwargs": {}})

    def cancelled(self) -> bool:
        return False


def main() -> int:
    try:
        raw = sys.stdin.readline()
        request = json.loads(raw)
        job = TranscriptionJob.from_dict(request["job"])
        runtime = _SubprocessRuntime(request.get("models") or {}, request.get("tools") or {})
        engine = MossLocalEngine(runtime=runtime)
        result = engine.transcribe(job, _WireReporter())
        _emit({"type": "result", "payload": result.to_dict()})
        return 0
    except Exception as exc:  # noqa: BLE001
        _emit({"type": "error", "message": str(exc)})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
