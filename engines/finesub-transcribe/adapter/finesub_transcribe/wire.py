"""The package's side of the engine host's line protocol (`nonoka_x.engine_host`).

Each engine package carries its own copy of this module, word for word
(`tests/test_engine_packages.py` keeps them equal): a package imports nothing
of the core's, and nothing of another package's.

One JSON object per line. The core sends `{"context": ...}` first, then one
`{"op": ...}` per request; the package answers each request with any number of
`{"type": "reporter"}` and `{"type": "event"}` lines and then exactly one
`{"type": "result"}` or `{"type": "error"}`. The protocol owns the process's
standard output: whatever else writes to it -- the engine's own prints, a
native library -- goes to standard error instead.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import traceback
from typing import Any, Callable, Mapping


class Wire:
    """This process's channel to the core."""

    def __init__(self) -> None:
        # The protocol keeps the real standard output; everything that writes
        # to file descriptor 1 or `sys.stdout` from now on reaches stderr.
        self._out = os.fdopen(os.dup(1), "w", encoding="utf-8", newline="\n")
        os.dup2(2, 1)
        sys.stdout = sys.stderr
        self._lock = threading.Lock()

    def send(self, value: Mapping[str, Any]) -> None:
        line = json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
        with self._lock:
            self._out.write(line + "\n")
            self._out.flush()

    def event(self, event_type: str, payload: Mapping[str, Any] | None = None) -> None:
        """A worker event, passed on by the core as it is."""

        self.send({"type": "event", "event": {"type": event_type, "payload": dict(payload or {})}})

    def log(self, message: str, fields: Mapping[str, Any] | None = None) -> None:
        payload: dict[str, Any] = {"message": message}
        if fields:
            payload["fields"] = dict(fields)
        self.event("log", payload)


class ForwardingReporter:
    """FineSub's reporter protocol, each call made on the core's reporter.

    The core holds the one reporter that hears every half of a task, so what
    it filters and says once is filtered and said once across processes too.
    """

    def __init__(self, wire: Wire) -> None:
        self._wire = wire

    def _call(self, method: str, *args: Any, **kwargs: Any) -> None:
        self._wire.send({"type": "reporter", "method": method, "args": list(args), "kwargs": kwargs})

    def planned(self, stages: Any) -> None:
        self._call("planned", list(stages))

    def stage_started(self, stage: str, *, reused: bool = False, detail: str = "") -> None:
        self._call("stage_started", stage, reused=reused, detail=detail)

    def progress(self, stage: str, *, completed: int, total: int | None = None, unit: str = "", detail: str = "") -> None:
        self._call("progress", stage, completed=completed, total=total, unit=unit, detail=detail)

    def summary(self, stage: str, metrics: Mapping[str, Any]) -> None:
        self._call("summary", stage, dict(metrics))

    def warning(self, code: str, message: str, *, impact: str = "", action: str = "") -> None:
        self._call("warning", code, message, impact=impact, action=action)

    def debug(self, message: str, fields: Mapping[str, Any] | None = None) -> None:
        self._call("debug", message, dict(fields or {}))

    def completed(self, output: Any, elapsed_sec: float) -> None:
        self._call("completed", str(output), elapsed_sec)

    def failed(self, stage: str, message: str) -> None:
        self._call("failed", stage, message)


Handler = Callable[[Mapping[str, Any]], Any]


def serve(
    handlers: Mapping[str, Handler],
    *,
    on_context: Callable[[Mapping[str, Any], Wire], None],
    describe_error: Callable[[BaseException], Mapping[str, Any]] | None = None,
    after_context: Callable[[Mapping[str, Any]], None] | None = None,
    wire: Wire | None = None,
) -> int:
    """Read the context, then answer requests until the core closes the input.

    `on_context` turns the launch context into what the engine reads (the
    package's bridge) before any request; `after_context` is for a caller
    that has to see the process prepared (the quality gate's stand-ins).
    `describe_error` makes an error payload of an exception: `formatted` (the
    task's failure message), `message`, `detail`.
    """

    wire = wire or Wire()
    first = sys.stdin.readline()
    if not first.strip():
        return 0
    context = json.loads(first).get("context") or {}
    on_context(context, wire)
    if after_context is not None:
        after_context(context)
    for raw in sys.stdin:
        line = raw.strip()
        if not line:
            continue
        request = json.loads(line)
        op = str(request.get("op") or "")
        handler = handlers.get(op)
        try:
            if handler is None:
                raise ValueError(f"unknown request {op!r}")
            payload = handler(request)
        except BaseException as exc:  # noqa: BLE001 - every failure is an answer
            described = dict(describe_error(exc) if describe_error is not None else {})
            described.setdefault("message", str(exc).strip() or type(exc).__name__)
            described.setdefault("formatted", f"{type(exc).__name__}: {str(exc).strip()}")
            described.setdefault("type", type(exc).__name__)
            if described.pop("traceback", True):
                traceback.print_exc(file=sys.stderr)
            wire.send({"type": "error", "payload": described})
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            continue
        wire.send({"type": "result", "payload": payload})
    return 0
