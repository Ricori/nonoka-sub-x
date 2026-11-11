"""`transcript.json`: what a transcription engine hands to the correction layer.

The one artifact every engine must produce (`docs/transcription-engines-plan.md`
section 5.2). It is Nonoka's own format: no engine's native output leaks into
it, and any correction implementation can read any engine's result.

Rules (decided 2026-11-11):

- `schema` is 1; `engine`, `language`, `duration` and `lines` are required,
  `model` and `metadata` may be absent.
- A line has a non-empty `id` unique within the document, `0 <= start <= end`
  and a string `text`. `speaker`, when present, is a non-empty string; `words`,
  when present, is a list whose items each have `start <= end`.
- Lines may overlap and need not be sorted: overlapping speakers are the point
  of keeping them apart.
- Fields this version does not know are kept and written back unchanged, so a
  round trip never loses information written by a newer engine.
- Absent optional fields stay absent on write.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import math
import os
from pathlib import Path
import re
from types import MappingProxyType
from typing import Any, Iterable, Mapping

SCHEMA = 1
FILENAME = "transcript.json"

_DOCUMENT_FIELDS = ("schema", "engine", "model", "language", "duration", "lines", "metadata")
_LINE_FIELDS = ("id", "start", "end", "text", "speaker", "words")
_WORD_FIELDS = ("text", "start", "end")


class TranscriptError(ValueError):
    """A `transcript.json` that breaks the format; the message names where."""


def _frozen(value: Mapping[str, Any] | None) -> Mapping[str, Any]:
    return MappingProxyType(dict(value or {}))


def _number(value: Any, where: str) -> float:
    # bool is an int subclass; `"start": true` is a bug, not 1.0.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TranscriptError(f"{where} must be a number")
    number = float(value)
    if not math.isfinite(number):
        raise TranscriptError(f"{where} must be finite")
    return number


def _text(value: Any, where: str, *, non_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise TranscriptError(f"{where} must be a string")
    if non_empty and not value:
        raise TranscriptError(f"{where} must not be empty")
    return value


def _object(value: Any, where: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise TranscriptError(f"{where} must be an object")
    return value


def _extra(data: Mapping[str, Any], known: Iterable[str]) -> dict[str, Any]:
    known = set(known)
    return {key: value for key, value in data.items() if key not in known}


def _check_extra(extra: Mapping[str, Any], known: Iterable[str], where: str) -> None:
    clash = sorted(set(extra) & set(known))
    if clash:
        raise TranscriptError(f"{where} extra fields shadow known fields: {', '.join(clash)}")
    for key in extra:
        if not isinstance(key, str):
            raise TranscriptError(f"{where} field names must be strings")


@dataclass(frozen=True)
class TranscriptWord:
    text: str
    start: float
    end: float
    extra: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "extra", _frozen(self.extra))
        _check_extra(self.extra, _WORD_FIELDS, "word")
        _text(self.text, "word.text")
        start = _number(self.start, "word.start")
        end = _number(self.end, "word.end")
        if start > end:
            raise TranscriptError(f"word.start {start} is after word.end {end}")
        object.__setattr__(self, "start", start)
        object.__setattr__(self, "end", end)

    @classmethod
    def from_dict(cls, data: Any, where: str = "word") -> "TranscriptWord":
        data = _object(data, where)
        for name in _WORD_FIELDS:
            if name not in data:
                raise TranscriptError(f"{where}.{name} is required")
        try:
            return cls(
                text=_text(data["text"], f"{where}.text"),
                start=_number(data["start"], f"{where}.start"),
                end=_number(data["end"], f"{where}.end"),
                extra=_extra(data, _WORD_FIELDS),
            )
        except TranscriptError as exc:
            raise TranscriptError(_located(str(exc), "word", where)) from None

    def to_dict(self) -> dict[str, Any]:
        return {"text": self.text, "start": self.start, "end": self.end, **self.extra}


@dataclass(frozen=True)
class TranscriptLine:
    id: str
    start: float
    end: float
    text: str
    speaker: str | None = None
    words: tuple[TranscriptWord, ...] | None = None
    extra: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "extra", _frozen(self.extra))
        _check_extra(self.extra, _LINE_FIELDS, "line")
        _text(self.id, "line.id", non_empty=True)
        _text(self.text, "line.text")
        start = _number(self.start, "line.start")
        end = _number(self.end, "line.end")
        if start < 0:
            raise TranscriptError(f"line.start {start} is negative")
        if start > end:
            raise TranscriptError(f"line.start {start} is after line.end {end}")
        object.__setattr__(self, "start", start)
        object.__setattr__(self, "end", end)
        if self.speaker is not None:
            _text(self.speaker, "line.speaker", non_empty=True)
        if self.words is not None:
            words = tuple(self.words)
            for word in words:
                if not isinstance(word, TranscriptWord):
                    raise TranscriptError("line.words items must be TranscriptWord")
            object.__setattr__(self, "words", words)

    @classmethod
    def from_dict(cls, data: Any, where: str = "line") -> "TranscriptLine":
        data = _object(data, where)
        for name in ("id", "start", "end", "text"):
            if name not in data:
                raise TranscriptError(f"{where}.{name} is required")
        words: tuple[TranscriptWord, ...] | None = None
        if "words" in data:
            raw_words = data["words"]
            if not isinstance(raw_words, list):
                raise TranscriptError(f"{where}.words must be a list")
            words = tuple(
                TranscriptWord.from_dict(item, f"{where}.words[{index}]")
                for index, item in enumerate(raw_words)
            )
        speaker = None
        if "speaker" in data:
            speaker = _text(data["speaker"], f"{where}.speaker", non_empty=True)
        try:
            return cls(
                id=_text(data["id"], f"{where}.id", non_empty=True),
                start=_number(data["start"], f"{where}.start"),
                end=_number(data["end"], f"{where}.end"),
                text=_text(data["text"], f"{where}.text"),
                speaker=speaker,
                words=words,
                extra=_extra(data, _LINE_FIELDS),
            )
        except TranscriptError as exc:
            raise TranscriptError(_located(str(exc), "line", where)) from None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"id": self.id, "start": self.start, "end": self.end, "text": self.text}
        if self.speaker is not None:
            out["speaker"] = self.speaker
        if self.words is not None:
            out["words"] = [word.to_dict() for word in self.words]
        out.update(self.extra)
        return out


@dataclass(frozen=True)
class TranscriptDocument:
    """One `transcript.json`."""

    engine: str
    language: str
    duration: float
    lines: tuple[TranscriptLine, ...]
    model: str | None = None
    metadata: Mapping[str, Any] | None = None
    extra: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "extra", _frozen(self.extra))
        _check_extra(self.extra, _DOCUMENT_FIELDS, "transcript")
        _text(self.engine, "engine", non_empty=True)
        _text(self.language, "language", non_empty=True)
        duration = _number(self.duration, "duration")
        if duration < 0:
            raise TranscriptError(f"duration {duration} is negative")
        object.__setattr__(self, "duration", duration)
        if self.model is not None:
            _text(self.model, "model")
        if self.metadata is not None:
            object.__setattr__(self, "metadata", _frozen(_object(self.metadata, "metadata")))
        lines = tuple(self.lines)
        seen: set[str] = set()
        for index, line in enumerate(lines):
            if not isinstance(line, TranscriptLine):
                raise TranscriptError(f"lines[{index}] must be TranscriptLine")
            if line.id in seen:
                raise TranscriptError(f"lines[{index}].id {line.id!r} is not unique")
            seen.add(line.id)
        object.__setattr__(self, "lines", lines)

    @classmethod
    def from_dict(cls, data: Any) -> "TranscriptDocument":
        data = _object(data, "transcript")
        if "schema" not in data:
            raise TranscriptError("schema is required")
        schema = data["schema"]
        if isinstance(schema, bool) or schema != SCHEMA:
            raise TranscriptError(f"schema must be {SCHEMA}, got {schema!r}")
        for name in ("engine", "language", "duration", "lines"):
            if name not in data:
                raise TranscriptError(f"{name} is required")
        raw_lines = data["lines"]
        if not isinstance(raw_lines, list):
            raise TranscriptError("lines must be a list")
        lines = tuple(
            TranscriptLine.from_dict(item, f"lines[{index}]") for index, item in enumerate(raw_lines)
        )
        model = _text(data["model"], "model") if "model" in data else None
        metadata = _object(data["metadata"], "metadata") if "metadata" in data else None
        return cls(
            engine=_text(data["engine"], "engine", non_empty=True),
            language=_text(data["language"], "language", non_empty=True),
            duration=_number(data["duration"], "duration"),
            lines=lines,
            model=model,
            metadata=metadata,
            extra=_extra(data, _DOCUMENT_FIELDS),
        )

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"schema": SCHEMA, "engine": self.engine}
        if self.model is not None:
            out["model"] = self.model
        out["language"] = self.language
        out["duration"] = self.duration
        out["lines"] = [line.to_dict() for line in self.lines]
        if self.metadata is not None:
            out["metadata"] = _plain(self.metadata)
        out.update(_plain(self.extra))
        return out


def _plain(value: Any) -> Any:
    """Mapping proxies back into dicts, so `json` can write them."""

    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def _located(message: str, prefix: str, where: str) -> str:
    """Re-anchor a field error raised by `__post_init__` at the item's path."""

    if where == prefix:
        return message
    return re.sub(rf"(?<![\w\]]){re.escape(prefix)}\.", f"{where}.", message)


def parse_transcript(text: str) -> TranscriptDocument:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise TranscriptError(f"not valid JSON: {exc}") from None
    return TranscriptDocument.from_dict(data)


def dumps_transcript(document: TranscriptDocument) -> str:
    return json.dumps(document.to_dict(), ensure_ascii=False, indent=2, allow_nan=False) + "\n"


def read_transcript(path: str | Path) -> TranscriptDocument:
    return parse_transcript(Path(path).read_text(encoding="utf-8"))


def write_transcript(document: TranscriptDocument, path: str | Path) -> Path:
    """Write atomically: a reader never sees half a file."""

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(f".{target.name}.part")
    partial.write_text(dumps_transcript(document), encoding="utf-8")
    os.replace(partial, target)
    return target


__all__ = [
    "FILENAME",
    "SCHEMA",
    "TranscriptDocument",
    "TranscriptError",
    "TranscriptLine",
    "TranscriptWord",
    "dumps_transcript",
    "parse_transcript",
    "read_transcript",
    "write_transcript",
]
