"""Which fast-mode switch a task request hands the engine.

FineSub's `--fast` takes `auto`, `on` and `off`, and `on` differs from `auto`
in exactly one way: when the fused single window does not fit its budgets,
`auto` falls back to the normal multi-window flow while `on` raises. Whether it
fits depends on the recognized subtitle length, which nobody knows until ASR
has run -- so to a user `on` could only ever mean "fail the task, after paying
for recognition, whenever the video is long". The desktop no longer offers it.

The mapping stays here because requests outlive that choice: a task queued or
retried from before, a plugin that still sends `on`, a script. Each is run as
`auto`, which still takes the fast path whenever it fits.
"""

from __future__ import annotations

from typing import Any, Callable, Mapping


AUTO = "auto"
MODES = frozenset({AUTO, "off"})
RETIRED_ON = "on"


def resolve_request_fast(
    correction: Mapping[str, Any] | None,
    *,
    warn: Callable[[str], None] | None = None,
) -> str:
    """The `fast` value to hand the engine for a request's `correction` block.

    `warn` is called at most once, with a message meant for the task log, when
    the request asks for something other than `auto` or `off`.
    """

    mode = str((correction or {}).get("fast") or AUTO).strip().lower()
    if mode in MODES:
        return mode
    if warn is not None:
        if mode == RETIRED_ON:
            warn("快速模式“开启”已并入“自动”：字幕放得进单窗口时仍走快速模式，放不下时改走正常流程")
        else:
            warn(f"未知的快速模式 {mode!r}，改用 {AUTO}")
    return AUTO
