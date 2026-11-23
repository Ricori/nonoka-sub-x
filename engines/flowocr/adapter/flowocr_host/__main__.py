"""The on-screen text package's process: `python -m flowocr_host` (started by the core's engine host).

Before any request: the process joins a kill-on-close job (its FlowOCR
children end with it), and the launch context becomes FlowOCR's variables
(`bridge.apply`). Then `check` and `recognize` are answered (`ops`).
"""

from __future__ import annotations

import os
import sys
from typing import Any, Mapping

from . import bridge, jobobject, ops
from .wire import ForwardingReporter, Wire, serve


def apply_context(context: Mapping[str, Any], wire: Wire) -> None:
    jobobject.bind()
    bridge.apply(context)


def describe_failure(error: BaseException) -> Mapping[str, Any]:
    """A failure as the task shows it: FlowOCR's own short message, the detail for the log."""

    if isinstance(error, ops.OcrFailure):
        return {"kind": error.kind, "message": str(error), "formatted": str(error), "detail": error.detail,
                "traceback": False}
    return {}


def main(argv: list[str] | None = None) -> int:
    wire = Wire()
    reporter = ForwardingReporter(wire)

    def check(request: Mapping[str, Any]) -> Any:
        return ops.check(request)

    def recognize(request: Mapping[str, Any]) -> Any:
        payload = dict(request.get("job") or request)
        payload.setdefault("engine", ops.engine_info())
        device = str(payload.get("device") or "")
        if device.startswith("cuda:"):
            # The card the request names, before anything loads CUDA (as the transcription package does).
            os.environ["CUDA_VISIBLE_DEVICES"] = device.split(":", 1)[1]
            payload["device"] = "cuda"
        return ops.recognize(payload, reporter)

    def inpaint(request: Mapping[str, Any]) -> Any:
        payload = dict(request.get("job") or request)
        device = str(payload.get("device") or "")
        if device.startswith("cuda:"):
            os.environ["CUDA_VISIBLE_DEVICES"] = device.split(":", 1)[1]
            payload["device"] = "cuda"
        return ops.inpaint(payload, reporter)

    return serve({"check": check, "recognize": recognize, "inpaint": inpaint}, on_context=apply_context,
                 describe_error=describe_failure, wire=wire)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
