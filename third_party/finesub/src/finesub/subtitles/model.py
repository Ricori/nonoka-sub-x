"""SRT parsing, validation, and rendering."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import List, Sequence, Tuple

from .metrics import weighted_char_count


TIMESTAMP_RE = re.compile(
    r"(?P<start>\d{2}:\d{2}:\d{2},\d{3})\s*-->\s*"
    r"(?P<end>\d{2}:\d{2}:\d{2},\d{3})"
)
#: Layout limit the validator measures a line against (weighted characters).
DEFAULT_MAX_LINE_CHARS = 25


@dataclass(frozen=True)
class SrtSegment:
    index: int
    start: float
    end: float
    text: str


@dataclass(frozen=True)
class SrtValidationResult:
    ok: bool
    segments: List[SrtSegment]
    errors: List[str]
    #: `(segment index, weighted length of its longest line)` for every segment
    #: with a line over the budget. Data, not messages: how to report it
    #: depends on the stage (see `warn_on_invalid_srt`).
    over_budget: List[Tuple[int, float]]


def parse_srt_timestamp(value: str) -> float:
    hours = int(value[0:2])
    minutes = int(value[3:5])
    seconds = int(value[6:8])
    millis = int(value[9:12])
    return hours * 3600 + minutes * 60 + seconds + millis / 1000.0


def format_srt_timestamp(seconds: float) -> str:
    total_ms = max(0, int(round(seconds * 1000.0)))
    hours, rem = divmod(total_ms, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    secs, ms = divmod(rem, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{ms:03d}"


def parse_srt(text: str) -> List[SrtSegment]:
    normalized = (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        return []
    lines = [line.rstrip() for line in normalized.splitlines()]
    segments: List[SrtSegment] = []
    i = 0
    while i < len(lines):
        while i < len(lines) and not lines[i].strip():
            i += 1
        if i >= len(lines):
            break
        index = len(segments) + 1
        if (
            lines[i].strip().isdigit()
            and i + 1 < len(lines)
            and TIMESTAMP_RE.fullmatch(lines[i + 1].strip())
        ):
            index = int(lines[i].strip())
            i += 1
        if i >= len(lines):
            raise ValueError(f"SRT segment {index} is missing a timing line.")
        match = TIMESTAMP_RE.fullmatch(lines[i].strip())
        if not match:
            raise ValueError(f"SRT segment {index} has an invalid timing line.")
        i += 1
        body_lines: List[str] = []
        while i < len(lines):
            stripped = lines[i].strip()
            if not stripped:
                i += 1
                break
            next_is_timing = i + 1 < len(lines) and TIMESTAMP_RE.fullmatch(lines[i + 1].strip())
            if (stripped.isdigit() and next_is_timing) or TIMESTAMP_RE.fullmatch(stripped):
                break
            body_lines.append(lines[i])
            i += 1
        if not body_lines:
            raise ValueError(f"SRT segment {index} is missing subtitle text.")
        segments.append(
            SrtSegment(
                index=index,
                start=parse_srt_timestamp(match.group("start")),
                end=parse_srt_timestamp(match.group("end")),
                text="\n".join(body_lines).strip(),
            )
        )
    return segments


def render_srt(segments: Sequence[SrtSegment], *, reindex: bool = True) -> str:
    lines: List[str] = []
    for i, segment in enumerate(segments, start=1):
        idx = i if reindex else segment.index
        lines.append(str(idx))
        lines.append(
            f"{format_srt_timestamp(segment.start)} --> "
            f"{format_srt_timestamp(segment.end)}"
        )
        lines.extend(segment.text.splitlines() or ['""'])
        lines.append("")
    return "\n".join(lines).strip() + "\n"


def validate_srt_text(
    text: str,
    *,
    max_line_chars: float = DEFAULT_MAX_LINE_CHARS,
    max_lines_per_segment: int = 2,
) -> SrtValidationResult:
    errors: List[str] = []
    over_budget: List[Tuple[int, float]] = []
    try:
        segments = parse_srt(text)
    except ValueError as exc:
        return SrtValidationResult(
            ok=False, segments=[], errors=[str(exc)], over_budget=[]
        )

    previous_end = -1.0
    for segment in segments:
        if segment.end <= segment.start:
            errors.append(f"Segment {segment.index} end must be greater than start.")
        if segment.start < previous_end:
            errors.append(
                f"Segment {segment.index} starts before the previous segment ends."
            )
        previous_end = segment.end
        body_lines = segment.text.splitlines()
        if len(body_lines) > max_lines_per_segment:
            errors.append(
                f"Segment {segment.index} has more than {max_lines_per_segment} lines."
            )
        longest = max((weighted_char_count(line) for line in body_lines), default=0.0)
        if longest > max_line_chars:
            over_budget.append((segment.index, longest))
    if not segments:
        errors.append("SRT contains no segments.")
    return SrtValidationResult(
        ok=not errors,
        segments=segments,
        errors=errors,
        over_budget=over_budget,
    )

def over_budget_buckets(
    over_budget: Sequence[Tuple[int, float]],
    *,
    max_line_chars: float = DEFAULT_MAX_LINE_CHARS,
    above: float | None = None,
) -> List[Tuple[str, List[int]]]:
    """Group over-budget segments into 5-character bands, worst band open-ended.

    With the default limit the bands are 26-30, 31-35, 36-40, 41-45 and 46+.
    Lengths are half-integers, so "26-30" holds everything above 25 up to and
    including 30. Empty bands are left out. `above` drops segments at or under
    that length first (a multiple of 5 keeps the bands whole: 35 starts at 36).
    """

    first = int(max_line_chars) + 1
    edges = [max_line_chars + step for step in (5, 10, 15, 20)]
    labels = [f"{first + 5 * n}-{first + 5 * n + 4}" for n in range(len(edges))]
    labels.append(f"{int(edges[-1]) + 1}+")
    bands: List[List[int]] = [[] for _ in labels]
    for index, length in over_budget:
        if above is not None and length <= above:
            continue
        band = next((n for n, edge in enumerate(edges) if length <= edge), len(edges))
        bands[band].append(index)
    return [(label, ids) for label, ids in zip(labels, bands) if ids]


def warn_on_invalid_srt(
    srt_text: str,
    *,
    where: str,
    budget_stage: str | None = None,
    budget_above: float = DEFAULT_MAX_LINE_CHARS,
) -> None:
    """Report what the SRT validator finds, without blocking the write.

    Warn-only on purpose. The validator flags an empty file, a cue whose end is
    not after its start, an overlap and a segment over the line budget -- and
    only the second of those is unambiguously a broken cue. An empty result can
    be an honest "nobody spoke", an overlap has already been through
    `resolve_overlaps`, and the line budget is a layout policy the global DP
    splitter owns. Turning any of them into a hard gate would mean a run that
    produced two hours of usable subtitles writes nothing, which is a worse
    failure than the one being reported.

    Escalating `end <= start` to an error is the one change worth discussing
    later; it is left warn-only here so this wiring changes no behaviour.

    The line budget is not a warning at all. A 98-minute run put 168 raw lines
    over it -- nothing anyone could act on, and enough to bury the findings
    that matter. It is reported only when the caller names a stage
    (`budget_stage`), as one summary that lists the segment numbers per band.
    Raw ASR text is before any layout policy, so it asks for `budget_above`
    35: only the lines that are far over.
    """

    from ..reporting import current_reporter

    result = validate_srt_text(srt_text)
    for message in result.errors:
        current_reporter().warning(
            "srt-invalid", f"{where}: {message}"
        )
    if budget_stage is None or not result.over_budget:
        return
    bands = over_budget_buckets(result.over_budget, above=budget_above)
    if not bands:
        return
    current_reporter().summary(
        budget_stage,
        {
            f"单行超 {int(budget_above)} 字的段（编号，按段内最长行分档）": "；".join(
                f"{label} 字：{'、'.join(map(str, ids))}" for label, ids in bands
            )
        },
    )
