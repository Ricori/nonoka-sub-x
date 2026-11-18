"""`correction.json`: a correction engine's result in Nonoka's format, read and checked (WP-L).

`docs/provider-spec.md`, "correction.json". The correction engine writes it in
the task directory beside `transcript.json`; the projection reads it, and the
cloud library syncs it. Rules:

- `schema` is 1, `engine` a non-empty string, `mode` `stable` or `final`,
  `correct_only` (optional) a boolean, `lines` a list;
- each line has numeric `start <= end` (`0 <= start`) and a string `source`;
  `translation` (optional) is a string; `speaker` (optional) a non-empty
  string; `low_confidence` (optional) a boolean; `words` (optional) a list of
  objects, the engine's own word timings, carried to the document as they are;
- unknown fields are kept and ignored.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Mapping

SCHEMA = 1
FILENAME = "correction.json"
MODES = ("stable", "final")


class CorrectionDocumentError(ValueError):
    """A `correction.json` that breaks the format."""


def _number(value: Any, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise CorrectionDocumentError(f"{where} must be a finite number")
    return float(value)


def check_document(document: Any) -> dict[str, Any]:
    """`document` when it follows the format; `CorrectionDocumentError` naming the first problem otherwise."""

    if not isinstance(document, Mapping):
        raise CorrectionDocumentError("correction.json must be an object")
    if document.get("schema") != SCHEMA:
        raise CorrectionDocumentError(f"correction.json schema must be {SCHEMA}")
    if not isinstance(document.get("engine"), str) or not document["engine"]:
        raise CorrectionDocumentError("correction.json engine must be a non-empty string")
    if document.get("mode") not in MODES:
        raise CorrectionDocumentError("correction.json mode must be one of: " + ", ".join(MODES))
    if "correct_only" in document and not isinstance(document["correct_only"], bool):
        raise CorrectionDocumentError("correction.json correct_only must be a boolean")
    lines = document.get("lines")
    if not isinstance(lines, list):
        raise CorrectionDocumentError("correction.json lines must be a list")
    for index, line in enumerate(lines):
        where = f"correction.json lines[{index}]"
        if not isinstance(line, Mapping):
            raise CorrectionDocumentError(f"{where} must be an object")
        start, end = _number(line.get("start"), f"{where}.start"), _number(line.get("end"), f"{where}.end")
        if start < 0 or start > end:
            raise CorrectionDocumentError(f"{where}: start {start} is not within [0, end {end}]")
        if not isinstance(line.get("source"), str):
            raise CorrectionDocumentError(f"{where}.source must be a string")
        if "translation" in line and not isinstance(line["translation"], str):
            raise CorrectionDocumentError(f"{where}.translation must be a string")
        if "speaker" in line and (not isinstance(line["speaker"], str) or not line["speaker"]):
            raise CorrectionDocumentError(f"{where}.speaker must be a non-empty string")
        if "low_confidence" in line and not isinstance(line["low_confidence"], bool):
            raise CorrectionDocumentError(f"{where}.low_confidence must be a boolean")
        words = line.get("words")
        if "words" in line and (not isinstance(words, list) or not all(isinstance(word, Mapping) for word in words)):
            raise CorrectionDocumentError(f"{where}.words must be a list of objects")
    return dict(document)


def read_document(path: str | Path) -> dict[str, Any]:
    """A `correction.json` from disk, checked."""

    try:
        document = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CorrectionDocumentError(f"cannot read correction.json: {exc}") from exc
    return check_document(document)


__all__ = ["FILENAME", "MODES", "SCHEMA", "CorrectionDocumentError", "check_document", "read_document"]
