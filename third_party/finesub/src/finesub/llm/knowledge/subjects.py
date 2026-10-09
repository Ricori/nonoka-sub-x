"""Knowledge subjects a task names explicitly, injected whole into every window.

The ordinary paths reach an entry only when something asks for it: a keyword
in the user note, research round 1's judgment, or a window's
`<requested_entries>`. A front end that lets the user say "this video is about
these subjects" needs the entries present regardless of whether any of those
fire, so the selection here is **task-static**, the same contract
`style.py` gives a translation style: the task names the subjects, nothing is
matched, and the model never picks.

Two consumers read the same selection:

* the correction windows, through `render_subject_block` (placed next to the
  style block in the system prompt, so every window carries it);
* the post-task knowledge update, which pins the resolved keys into every
  chunk's `<kb_entries>` so the model can propose into them
  (`entries.pin_subject_entries`).

Names come from a human, so they take the human lookup: `streamer/某主播`
addresses a category explicitly, and a bare name that exists in several raises
rather than picking a side.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

from ..injection_budget import render_knowledge_entries_block
from ..routing.config import INJECTION_SECTION_MAX_TOKENS, injection_block_token_limit
from .node.repo import AmbiguousName, KnowledgeRepo
from .style import STYLE_CATEGORY, parse_style_names

#: How many subjects one task may name. Every window carries all of them, so
#: this is a prompt-size bound as much as a UI one; the block budget below
#: scales with it.
MAX_KNOWLEDGE_SUBJECTS = 8


class SubjectSelectionError(ValueError):
    """A named subject that cannot be resolved to exactly one entry.

    Loud for the same reason as `StyleSelectionError`: a run that silently
    drops a subject the user picked looks exactly like the subject having no
    effect.
    """


@dataclass(frozen=True)
class PinnedSubject:
    category: str
    key: str
    text: str


def parse_subject_names(value: str | Sequence[str] | None) -> tuple[str, ...]:
    """Same shape as `--style`: a comma list or a sequence, order kept."""

    return parse_style_names(value)


def load_subject_entries(
    knowledge_root: str | Path,
    names: Sequence[str],
    *,
    rev: int | None = None,
) -> list[PinnedSubject]:
    """Resolve each name to one entry and read its prompt projection."""

    names = parse_subject_names(names)
    if not names:
        return []
    if len(names) > MAX_KNOWLEDGE_SUBJECTS:
        raise SubjectSelectionError(
            f"最多指定 {MAX_KNOWLEDGE_SUBJECTS} 个知识主体，本次为 {len(names)} 个"
        )
    repo = KnowledgeRepo.open(knowledge_root)
    at = repo.rev if rev is None else rev
    out: list[PinnedSubject] = []
    seen: set[tuple[str, str]] = set()
    for name in names:
        try:
            resolved = repo.resolve_qualified(name, at)
        except AmbiguousName as exc:
            raise SubjectSelectionError(str(exc)) from exc
        if resolved is None:
            raise SubjectSelectionError(f"知识主体 {name!r}：知识库里没有这个条目")
        if resolved.category == STYLE_CATEGORY:
            raise SubjectSelectionError(
                f"知识主体 {name!r} 指到的是风格条目；风格请用 --style 指定"
            )
        pair = (resolved.category, resolved.key)
        if pair in seen:
            continue
        seen.add(pair)
        out.append(
            PinnedSubject(
                category=resolved.category,
                key=resolved.key,
                text=repo.entry_injection_text(resolved.subject_id, at),
            )
        )
    return out


def resolve_subject_keys(
    knowledge_root: str | Path,
    names: Sequence[str],
    *,
    rev: int | None = None,
) -> list[tuple[str, str]]:
    """`(category, key)` of the named subjects -- what the update pins."""

    return [
        (subject.category, subject.key)
        for subject in load_subject_entries(knowledge_root, names, rev=rev)
    ]


def render_subject_block(
    knowledge_root: str | Path,
    names: Sequence[str],
    *,
    count_tokens: Callable[[str], int],
    rev: int | None = None,
) -> str:
    """The system-prompt block, or `""` when the task named no subject.

    Budgeted with the same per-entry and per-block caps as every other
    knowledge injection, so a large entry is truncated with a notice instead
    of crowding the window out.
    """

    subjects = load_subject_entries(knowledge_root, names, rev=rev)
    if not subjects:
        return ""
    block = render_knowledge_entries_block(
        {f"{subject.category}/{subject.key}": subject.text for subject in subjects},
        count_tokens=count_tokens,
        entry_limit=INJECTION_SECTION_MAX_TOKENS,
        block_limit=injection_block_token_limit(len(subjects)),
    )
    if not block.text.strip():
        return ""
    return (
        "\n本任务指定的知识主体（由用户明确选择，词条常驻每个窗口，无需再通过 "
        "requested_entries 索取；其中的固定译名与设定按知识库词条对待）：\n"
        f"<pinned_entries>\n{block.text.strip()}\n</pinned_entries>\n"
    )
