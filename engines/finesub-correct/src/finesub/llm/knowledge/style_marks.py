"""Refined-alignment marks on a style's conventions (owner 2026-10-01).

In a ``refined_aligned`` knowledge update the executor has the raw text, our
output and the human's refined subtitles in hand, so in the same call it
judges each line of the pinned style's convention section: the refinement
kept it (``confirmed``) or overturned it (``refuted``); a convention this
material had no use for is not marked. The answer travels in a
``<style_marks>`` block next to ``<knowledge_proposals>``, one mark per line:

    句柄|confirmed 或 refuted|精修行的开始时间|一句理由

The harness checks every mark before it counts. The handle must name a line
of the pinned style's convention section (not an example line, not another
entry); the start time must be one this very call rendered in its
``<refined_csv>`` -- the update is grouped by window, and a time from another
group (or the whole SRT) would let a guess pass. A mark that fails is dropped
and counted, never fatal to the update.

What passes is booked as ``evidence`` with ``evidence_kind="judged"``: a
model's judgement, kept apart from the deterministic ``refined_srt`` rows,
never exported to the share server, and only ever read by the report. The
cited time rides ``span``, which is part of the dedupe key: the same mark on
the same refined row counts once per task, different rows count separately.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable

from .node.model import digest
from .node.presets import preset_for_category
from .node.render import HandleMap
from .node.signals import EVIDENCE_KIND_JUDGED, record_evidence
from .node.store import KnowledgeStore

STYLE_MARKS_ALGO_VERSION = "style-marks-1"
VERDICTS: tuple[str, ...] = ("confirmed", "refuted")

_BLOCK_RE = re.compile(r"<style_marks>(?P<body>.*?)</style_marks>", re.S)
#: Models write the full-width bar as often as the ASCII one.
_SEPARATOR_RE = re.compile(r"[|｜]")


@dataclass(frozen=True)
class StyleMark:
    raw: str
    handle: str
    verdict: str
    start: str
    reason: str


def parse_style_marks(text: str) -> list[StyleMark | str]:
    """The block's rows: a `StyleMark` per well-formed line, the raw line for
    one with fewer than four fields. No block, no rows."""

    match = _BLOCK_RE.search(text or "")
    if match is None:
        return []
    rows: list[StyleMark | str] = []
    for line in match.group("body").splitlines():
        raw = line.strip()
        if not raw:
            continue
        parts = [part.strip() for part in _SEPARATOR_RE.split(raw, maxsplit=3)]
        if len(parts) < 4:
            rows.append(raw)
            continue
        handle, verdict, start, reason = parts
        if handle and not handle.startswith("@"):
            handle = f"@{handle}"
        rows.append(StyleMark(raw, handle, verdict.lower(), start, reason))
    return rows


def convention_section(category: str) -> str | None:
    """The section a style's examples point back to -- its conventions. Read
    from the preset (`labels_from`), not a literal."""

    preset = preset_for_category(category)
    return next((spec.labels_from for spec in preset.sections if spec.labels_from), None)


def refined_start_times(refined_csvs: Iterable[str]) -> set[str]:
    """The start-time cells exactly as this call's ``<refined_csv>`` blocks
    rendered them (``_format_csv_seconds``, 0.1 s)."""

    from .materials import REFINED_CSV_HEADER

    out: set[str] = set()
    for text in refined_csvs:
        for line in (text or "").splitlines():
            if not line.strip() or line.strip() == REFINED_CSV_HEADER.strip():
                continue
            cell = line.split("|", 1)[0].strip()
            if cell:
                out.add(cell)
    return out


def book_style_marks(
    store: KnowledgeStore,
    text: str,
    *,
    handles: HandleMap,
    style_subject_id: str,
    refined_starts: set[str],
    read_rev: int,
    task_id: str,
) -> dict[str, Any]:
    """Check and book one response's marks. Returns ``{"booked", "duplicates",
    "dropped": [{"line", "reason"}]}`` for the task report."""

    report: dict[str, Any] = {"booked": 0, "duplicates": 0, "dropped": []}
    rows = parse_style_marks(text)
    if not rows:
        return report
    subject = store.node(style_subject_id, read_rev)
    section = convention_section(str(subject.payload.get("category") or "")) if subject else None
    preset = preset_for_category(str(subject.payload.get("category") or "")) if subject else None

    def drop(line: str, reason: str) -> None:
        report["dropped"].append({"line": line[:200], "reason": reason})

    for row in rows:
        if isinstance(row, str):
            drop(row, "malformed: needs 句柄|verdict|开始时间|理由")
            continue
        if row.verdict not in VERDICTS:
            drop(row.raw, f"verdict must be one of {VERDICTS}")
            continue
        bound = handles.nodes.get(row.handle)
        if bound is None:
            drop(row.raw, "handle not rendered in this call")
            continue
        node_id = bound[0]
        node = store.node(node_id, read_rev)
        in_conventions = (
            node is not None
            and section is not None
            and preset is not None
            and any(
                membership.parent_id == style_subject_id
                and preset.section(membership.section) is preset.section(section)
                for membership in store.parents(node_id, read_rev)
            )
        )
        if not in_conventions:
            drop(row.raw, "not a line of the pinned style's convention section")
            continue
        if row.start not in refined_starts:
            drop(row.raw, "start time is not one of this call's refined rows")
            continue
        value = str(node.payload.get("text") or "")  # type: ignore[union-attr]
        if record_evidence(
            store,
            node_id=node_id,
            field_path="payload.text",
            value_hash=digest(value),
            verdict=row.verdict,
            evidence_kind=EVIDENCE_KIND_JUDGED,
            task_id=task_id,
            span=row.start,
            algo_version=STYLE_MARKS_ALGO_VERSION,
        ):
            report["booked"] += 1
        else:
            report["duplicates"] += 1
    return report
