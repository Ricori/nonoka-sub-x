"""The reporter the moved cloud-ASR code talks to, bound per thread.

The code moved out of FineSub's `speech/joint/` (WP1) reports through `current_reporter()`, as it did through
`finesub.reporting`: `warning(code, message, impact=..., action=...)` and `debug(message, fields)`. Here those
calls reach the WP0 `Reporter` the engine was handed (`transcription.Reporter`): a warning becomes its
`warning(code, message)` with the impact and action appended, a debug line its `log(message, fields)`. Nothing
bound means nothing reported.

`bind_reporter` binds for the rest of the thread (a pool's `initializer`), `reporting_to` for a block.
"""

from __future__ import annotations

from contextlib import contextmanager
import threading
from typing import Any, Iterator, Mapping


class Channel:
    """What the moved code calls; forwards to a WP0 `Reporter` (or drops everything when there is none)."""

    def __init__(self, reporter: Any | None = None) -> None:
        self.reporter = reporter

    def warning(self, code: str, message: str, *, impact: str = "", action: str = "") -> None:
        if self.reporter is None:
            return
        self.reporter.warning(code, compose(message, impact=impact, action=action))

    def debug(self, message: str, fields: Mapping[str, Any] | None = None) -> None:
        if self.reporter is not None:
            self.reporter.log(message, dict(fields) if fields else None)


def compose(message: str, *, impact: str = "", action: str = "") -> str:
    """A warning's text with FineSub's `impact` and `action` folded in."""

    parts = [message]
    if impact:
        parts.append(f"影响：{impact}")
    if action:
        parts.append(f"处理：{action}")
    return "；".join(parts)


_NULL = Channel()
_local = threading.local()


def current_reporter() -> Channel:
    """This thread's channel; a silent one when nothing bound it."""

    return getattr(_local, "channel", _NULL)


def reporter_delivers(channel: Channel) -> bool:
    """Whether `channel` carries a message anywhere (FineSub `reporting.reporter_delivers`): something that must
    reach a person once, and would otherwise mark itself said, has to ask."""

    return channel.reporter is not None


def _channel(reporter: Any | None) -> Channel:
    if reporter is None:
        return _NULL
    return reporter if isinstance(reporter, Channel) else Channel(reporter)


def bind_reporter(reporter: Any | None) -> None:
    """Bind `reporter` (a WP0 `Reporter` or a `Channel`) to this thread and leave it bound."""

    _local.channel = _channel(reporter)


@contextmanager
def reporting_to(reporter: Any | None) -> Iterator[Channel]:
    """Bind `reporter` for the duration of the block, then restore."""

    previous = getattr(_local, "channel", None)
    _local.channel = chosen = _channel(reporter)
    try:
        yield chosen
    finally:
        if previous is None:
            del _local.channel
        else:
            _local.channel = previous


__all__ = ["Channel", "bind_reporter", "compose", "current_reporter", "reporter_delivers", "reporting_to"]
