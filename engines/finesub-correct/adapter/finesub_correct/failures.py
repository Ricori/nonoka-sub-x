"""A failed request as the task's failure message (moved from the core's `nonoka_x.worker`).

Every engine package carries its own copy of this module, word for word: the
message a task fails with is the one the worker formatted before the engine
moved into its packages -- the exception's type and words, and the vendor's or
CLI's own diagnostic when the engine recorded one.
"""

from __future__ import annotations

import os
from typing import Any, Mapping


def extract_execution_failure_detail(exc: BaseException) -> str:
    """Extract vendor/CLI diagnostic details from agent execution attempts or capsules."""

    attempts = getattr(exc, "_harness_execution_attempts", None) or []
    for attempt in attempts:
        if isinstance(attempt, Mapping):
            vendor_err = attempt.get("vendor_error")
            if vendor_err and isinstance(vendor_err, str) and vendor_err.strip():
                return vendor_err.strip()

    try:
        from finesub.llm.agent.agent_paths import vendor_error_text

        text = vendor_error_text(exc)
        if text and text.strip():
            return text.strip()
    except Exception:
        pass

    current = getattr(exc, "__cause__", None) or getattr(exc, "__context__", None)
    while current is not None:
        sub = extract_execution_failure_detail(current)
        if sub:
            return sub
        current = getattr(current, "__cause__", None) or getattr(current, "__context__", None)

    return ""


def format_worker_exception(exc: BaseException) -> str:
    vendor_detail = extract_execution_failure_detail(exc)
    exc_type = type(exc).__name__
    exc_msg = str(exc).strip()
    if vendor_detail:
        if "inspect events/stderr.log" in exc_msg or "exited with status" in exc_msg:
            return f"{exc_type}: 本地模型 CLI 调用失败: {vendor_detail}"
        return f"{exc_type}: {exc_msg} ({vendor_detail})"
    return f"{exc_type}: {exc_msg}"


def describe_failure(exc: BaseException) -> dict[str, Any]:
    """The error payload of the line protocol: the formatted message, the detail, and whether to print the traceback.

    The traceback goes to stderr as the worker printed it: always under
    `NONOKA_DEBUG`, otherwise only when there is no vendor detail and it is
    not a local agent's own failure.
    """

    detail = extract_execution_failure_detail(exc)
    return {
        "formatted": format_worker_exception(exc),
        "message": str(exc) or type(exc).__name__,
        "detail": detail,
        "type": type(exc).__name__,
        "traceback": bool(os.environ.get("NONOKA_DEBUG")) or (
            not detail and not type(exc).__name__.startswith("LocalAgent")
        ),
    }
