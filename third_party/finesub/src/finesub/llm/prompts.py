"""Chinese prompt templates for subtitle correction and translation."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from typing import Any, Dict, List, Mapping, Sequence

from .chunking import (
    SubtitleWindow,
    render_window_preceding_as_csv,
    render_window_segments_as_csv,
)
from .routing.config import (
    DEFAULT_RESEARCH_SEARCH_QUERIES,
    CapabilityTier,
    KB_TRANSFER_MAX_ENTRIES,
    KB_WINDOW_NEW_REQUEST_MAX_ENTRIES,
    KB_WINDOW_TOTAL_ENTRIES,
    MAX_WINDOW_SEARCH_QUERIES,
    WINDOW_CONTEXT_MAX_TOKENS,
)
from .routing.profiles import DEFAULT_PROFILE, TranslationProfile
from .token_budget import TokenCounter, default_token_counter
from .token_truncate import truncate_to_token_window
from .prompt_compose import (
    PROMPT_TEMPLATE_DIR,
    PROMPT_VERSION,
    compose_correction_system,
    compose_correction_user,
    ensure_csv_block_headers,
    load_prompt_template as _load_prompt_template,
    reasoning_clause,
    render_knowledge_indexes,
    render_knowledge_ops_contract,
)


def _any_index(indexes: Mapping[str, str] | None) -> bool:
    """Is there any index to read at all?

    "No index" is a real state with prompt consequences: with nothing to pick
    entries off, the request rules come out of the system prompt entirely
    (`docs/llm_prompts.md`). It used to be spelled `streamer_index.strip() or
    common_index.strip()` at each site, which is the two-category assumption in
    miniature -- a third matchable category would simply not have counted.
    """

    return any(str(text).strip() for text in (indexes or {}).values())


def _query_style_rules() -> str:
    """Query-writing style rules shared by both search emission fragments."""

    return _load_prompt_template("fragment_query_style_v1.md").strip()


def _search_queries_rules(max_queries: int, *, knowledge_enabled: bool = True) -> str:
    # The knowledge-owned tail of the "don't waste queries" rule leaves with
    # the knowledge switch: it argues against entries a knowledge-off round
    # will never be shown (docs/llm_prompts.md, injection reality).
    return _load_prompt_template(
        "fragment_search_queries_output_v1.md",
        max_queries=max_queries,
        query_style=_query_style_rules(),
        knowledge_query_note=(
            "已注入的知识库条目足以回答的主题也不要花 query（尤其主播本人相关），"
            "只为其增量动态、时效信息或条目标注「待定」的点发 query。"
            "index 中有但尚未注入的条目：请求词条与发 query **并行**进行，"
            "不要等词条注入再决定；若 index 简介已表明词条大概率覆盖该主题，"
            "可降低对应 query 的优先级或数量。"
            if knowledge_enabled
            else ""
        ),
    ).strip()


def _search_results_usage() -> str:
    return _load_prompt_template("fragment_search_results_usage_v1.md").strip()


def _feedback_schema(*, local_source_ids: bool) -> str:
    source_ids_rule = (
        "`source_ids`：支撑该线索的本窗口局部字幕序号（字符串数组，只能用 "
        "`<asr_result>` 中从 1 开始的正整数），可省略；harness 会映射回稳定源字幕行号。"
        if local_source_ids
        else "`source_ids`：支撑该线索的稳定源字幕序号（字符串数组，可引用 transcript "
        "中的源序号），可省略。"
    )
    return _load_prompt_template(
        "fragment_task_feedback_schema_v3.md",
        source_ids_rule=source_ids_rule,
    ).strip()


def _research_task_feedback_block(*, local_source_ids: bool) -> str:
    """Feedback-collection addendum for the preparation task's delivery."""

    return "\n" + _load_prompt_template(
        "research_task_feedback_v1.md",
        feedback_schema=_feedback_schema(local_source_ids=local_source_ids),
    ).strip() + "\n"


# Appended to the round's closing reminder when feedback collection is on.
TASK_FEEDBACK_REMINDER = "，随后按 system 要求输出一个 `<task_update_feedback>` 块"


@dataclass(frozen=True)
class WindowContextNote:
    window_id: str
    first_source_id: str
    last_source_id: str
    context: str

    def to_dict(self) -> dict[str, str]:
        return {
            "window_id": self.window_id,
            "first_source_id": self.first_source_id,
            "last_source_id": self.last_source_id,
            "context": self.context,
        }


def general_for_correction(general: Mapping[str, Any]) -> dict[str, Any]:
    """The general context without the speaker judge's `speakers` (top level, and inside a chunked
    run's per-chunk blocks): what the correction reads, and what its size is counted on."""
    return {key: ({k: v for k, v in value.items() if k != "speakers"} if isinstance(value, Mapping) else value)
            for key, value in general.items() if key != "speakers"}


@dataclass(frozen=True)
class ContextPack:
    """Research output injected into correction windows.

    ``general_context`` covers every window.  Window notes carry their original
    source-id interval so a saved research result can be reused after correction
    windows are split or otherwise re-shaped.

    ``unbound_window_contexts`` is the *transient* shape the model produces: r2
    names windows by chunk id and knows nothing about source ids, so
    :func:`parse_round2_output` lands here and the research stage calls
    :meth:`bind_window_ranges` before writing the artifact.  Unbound notes are
    never injected -- a persisted context that still has them is a stale
    artifact, and its loader rejects it rather than serving half a pack.
    """

    general_context: Mapping[str, Any] = field(default_factory=dict)
    window_contexts: tuple[WindowContextNote, ...] = ()
    unbound_window_contexts: Mapping[str, str] = field(default_factory=dict)
    source_order: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if isinstance(self.window_contexts, Mapping):
            raise TypeError(
                "ContextPack.window_contexts takes WindowContextNote records; "
                "an id->text mapping is the model's shape, so build it with "
                "ContextPack.from_dict and bind ranges before injecting."
            )

    def general_prompt_text(self) -> str:
        """The general context as the correction windows read it. `speakers` (asked only for the
        speaker judge) stays out, also inside a chunked run's per-chunk blocks: the correction's
        prompt is the same whether the judge runs or not."""
        if not self.general_context:
            return "{}"
        return json.dumps(general_for_correction(self.general_context), ensure_ascii=False, indent=2)

    @property
    def has_unbound_window_contexts(self) -> bool:
        return bool(self.unbound_window_contexts)

    def with_source_order(self, source_ids: Sequence[str]) -> "ContextPack":
        return ContextPack(
            general_context=self.general_context,
            window_contexts=self.window_contexts,
            unbound_window_contexts=self.unbound_window_contexts,
            source_order=tuple(str(source_id) for source_id in source_ids),
        )

    def bind_window_ranges(
        self,
        windows: Sequence[SubtitleWindow],
        *,
        report_sink: dict[str, Any] | None = None,
    ) -> "ContextPack":
        """Attach research-window source ranges to model output keyed by id.

        A note whose window id is not in the plan cannot be placed and is
        dropped. ``report_sink`` receives the ids that were dropped: if r2
        gets the ids systematically wrong, every note disappears and the
        artifact still looks well-formed -- bound notes, none of them, which
        the loader's unbound check cannot see. The caller is expected to
        surface a non-empty report.
        """

        by_id = {window.chunk_id: window for window in windows}
        notes = list(self.window_contexts)
        unplaceable: list[str] = []
        for window_id, context in self.unbound_window_contexts.items():
            window = by_id.get(window_id)
            body = (
                window.segments[len(window.overlap_segments) :]
                if window is not None
                else []
            )
            if not body:
                unplaceable.append(str(window_id))
                continue
            notes.append(
                WindowContextNote(
                    window_id=window_id,
                    first_source_id=body[0].id,
                    last_source_id=body[-1].id,
                    context=context,
                )
            )
        if report_sink is not None:
            report_sink["bound"] = len(notes)
            report_sink["unplaceable_window_ids"] = unplaceable
            report_sink["plan_window_ids"] = [window.chunk_id for window in windows]
        source_order = [
            segment.id
            for window in windows
            for segment in window.segments[len(window.overlap_segments) :]
        ]
        return ContextPack(
            general_context=self.general_context,
            window_contexts=tuple(notes),
            source_order=tuple(dict.fromkeys(source_order)),
        )

    def window_context_for(
        self,
        window: SubtitleWindow,
        *,
        counter: TokenCounter | None = None,
        max_tokens: int = WINDOW_CONTEXT_MAX_TOKENS,
        report_sink: dict[str, Any] | None = None,
    ) -> str:
        body = list(window.segments[len(window.overlap_segments) :])
        return self.window_context_for_source_ids(
            [segment.id for segment in body],
            chunk_id=window.chunk_id,
            counter=counter,
            max_tokens=max_tokens,
            report_sink=report_sink,
        )

    def window_context_for_source_ids(
        self,
        source_ids: Sequence[str],
        *,
        chunk_id: str = "",
        counter: TokenCounter | None = None,
        max_tokens: int = WINDOW_CONTEXT_MAX_TOKENS,
        report_sink: dict[str, Any] | None = None,
    ) -> str:
        body_ids = list(source_ids)
        if not body_ids or not self.window_contexts:
            return ""
        if not self.source_order:
            # Without the full source order a note that *contains* this window
            # has endpoints we cannot place, so the containing-note clause --
            # the one every split window depends on -- would silently return
            # nothing. Every consumer binds the current segment list first.
            raise ValueError(
                "ContextPack.window_context_for needs the source order; call "
                "with_source_order(current segment ids) after loading."
            )
        positions = {
            source_id: index for index, source_id in enumerate(self.source_order)
        }
        if any(source_id not in positions for source_id in body_ids):
            return ""
        first, last = positions[body_ids[0]], positions[body_ids[-1]]
        ranged: list[tuple[int, int, WindowContextNote]] = []
        for note in self.window_contexts:
            if (
                note.first_source_id not in positions
                or note.last_source_id not in positions
            ):
                continue
            note_first = positions[note.first_source_id]
            note_last = positions[note.last_source_id]
            if note_first <= note_last:
                ranged.append((note_first, note_last, note))

        contained = sorted(
            (item for item in ranged if first <= item[0] and item[1] <= last),
            key=lambda item: (item[0], item[1]),
        )
        if contained:
            selected = contained
        else:
            containers = [
                item for item in ranged if item[0] <= first and last <= item[1]
            ]
            selected = (
                [min(containers, key=lambda item: (item[1] - item[0], item[0]))]
                if containers
                else []
            )
        text = "\n\n".join(
            item[2].context.strip()
            for item in selected
            if item[2].context.strip()
        )
        if not text:
            return ""
        token_counter = counter or default_token_counter()
        result = truncate_to_token_window(text, max_tokens, token_counter.count_text)
        if report_sink is not None and result.text != text:
            report_sink.update(
                {
                    "chunk_id": chunk_id,
                    "selected_window_ids": [item[2].window_id for item in selected],
                    "original_tokens": token_counter.count_text(text),
                    "kept_tokens": result.tokens,
                    "limit": max_tokens,
                }
            )
        return result.text

    def to_dict(self) -> dict[str, Any]:
        return {
            "general_context": dict(self.general_context),
            "window_contexts": [
                *[note.to_dict() for note in self.window_contexts],
                *[
                    {"window_id": window_id, "context": context}
                    for window_id, context in self.unbound_window_contexts.items()
                ],
            ],
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ContextPack":
        general = data.get("general_context") or {}
        if not isinstance(general, Mapping):
            general = {"global_summary": str(general)}
        # The model emits ``[{window_id, context}]`` (prepare_task_v1.md); the
        # harness adds the source range before persisting. Anything without a
        # range therefore lands unbound and is never injected.
        raw_windows = data.get("window_contexts") or []
        if isinstance(raw_windows, Mapping):
            # The pre-interval on-disk shape. Refused rather than read as "no
            # notes", so the caller re-runs research instead of injecting a pack
            # that quietly lost every window-specific note it once had.
            raise ValueError(
                "window_contexts is an id->text mapping; that artifact predates "
                "source-interval addressing and cannot be placed on any window"
            )
        window_contexts: list[WindowContextNote] = []
        unbound: dict[str, str] = {}
        if isinstance(raw_windows, Sequence) and not isinstance(raw_windows, (str, bytes)):
            for item in raw_windows:
                if isinstance(item, Mapping) and item.get("window_id"):
                    window_id = str(item["window_id"])
                    first = str(item.get("first_source_id") or "")
                    last = str(item.get("last_source_id") or "")
                    if first and last:
                        window_contexts.append(
                            WindowContextNote(
                                window_id,
                                first,
                                last,
                                str(item.get("context", "")),
                            )
                        )
                    else:
                        unbound[window_id] = str(item.get("context", ""))
        return cls(
            general_context=dict(general),
            window_contexts=tuple(window_contexts),
            unbound_window_contexts=unbound,
        )


def render_advice_ledger(entries: Sequence[tuple[str, str]]) -> str:
    """Render the accumulated per-window ``<next_advice>`` ledger.

    Each entry is ``(chunk_id, advice)``; empty advice entries are skipped.
    The full ledger is injected into both the query round and the correction
    round, so each window sees every earlier window's advice.
    """

    parts = [
        f"[window {chunk_id}]\n{advice.strip()}"
        for chunk_id, advice in entries
        if (advice or "").strip()
    ]
    return "\n\n".join(parts)


def _numbered(items: Sequence[str]) -> str:
    """Render an ordered list whose numbering follows the items actually kept.

    The preparation task's background list is assembled per axis
    (docs/llm_harness_behavior.md), so the numbers cannot live in the template
    -- a disabled half would leave a hole.
    """

    return "\n".join(f"{index}. {item}" for index, item in enumerate(items, start=1))


# --- preparation-task axis fragments ----------------------------------------
# Each entry is owned by exactly one axis, so a disabled half removes its own
# background line together with its input section -- the injection-reality
# principle (docs/llm_prompts.md) applied to a task (``prepare_prompts``).

_R1_BACKGROUND_COMMON = (
    "ASR 文本来自 Whisper 识别，可能存在误听：专有名词可能被识别成音似的假名、汉字、"
    "英文或另一种语言。判断「这段内容在说什么」时要考虑这种可能。",
    "文本中的 `--- window N ---` 标记是后续纠错处理的窗口边界，仅用于定位，"
    "不需要在本轮输出中引用。",
    "每行格式是 `源序号|文本`。",
)
_R1_BACKGROUND_KNOWLEDGE = (
    "本地知识库收集的是公开网络中很少存在、难以进入 LLM 语料的知识；"
    "索引里的条目可能正是理解本段内容的关键。",
)
def r1_request_cap(*, downstream_can_request: bool) -> int:
    """How many entries R1 may request.

    ``KB_WINDOW_NEW_REQUEST_MAX_ENTRIES`` was sized for a *per-window increment*
    -- it leaves room for the next window to ask for more. When no window can
    request (no request channel; docs/llm_harness_behavior.md), the preparation
    task is the session's only pick and that reserve is pure loss, so it gets
    the whole shared budget.
    """

    return (
        KB_WINDOW_NEW_REQUEST_MAX_ENTRIES
        if downstream_can_request
        else KB_WINDOW_TOTAL_ENTRIES
    )


def _r1_entry_blocks(max_requested_entries: int, *, keep_source_block: str) -> str:
    """The request blocks; ``keep_source_block`` is the block the reader sees
    the entries it may keep in -- `<preinjected_entries>` in a preparation
    task, `<entry_details>` in a correction window."""

    return _load_prompt_template(
        "fragment_research_entry_blocks_v1.md",
        keep_source_block=keep_source_block,
        max_requested_entries=max_requested_entries,
        max_keep_entries=KB_TRANSFER_MAX_ENTRIES,
        max_total_entries=KB_WINDOW_TOTAL_ENTRIES,
    ).strip()


def build_correction_csv_messages(
    *,
    window: SubtitleWindow,
    context_pack: ContextPack | None = None,
    audio_file_label: str = "",
    previous_advice: str = "",
    window_notes: str = "",
    search_results: str = "",
    entry_details: str = "",
    extra_style: str = "",
    style_block: str = "",
    task_update_feedback: bool = False,
    profile: TranslationProfile = DEFAULT_PROFILE,
    tier: CapabilityTier = CapabilityTier.CAPABLE,
    variant: str | None = None,
    knowledge_enabled: bool = True,
    style_pick_reminder: str = "",
) -> List[Dict[str, Any]]:
    """Correction-round messages.

    ``window_notes`` fills the generic ``<pre_round_notes>`` slot (the
    request turn's window notes); ``entry_details`` carries the window's
    knowledge entries. Production passes ``variant`` from the resolved
    task-group cell. ``tier`` only supplies the compatibility default when a
    caller (notably old session-replay fixtures) omits that explicit variant.
    """

    pack = context_pack or ContextPack()
    current_csv = render_window_segments_as_csv(window).strip()
    # Read-only raw lines before the window (v13), rendered on the same time
    # base as current_asr_csv so they come out mostly negative.
    preceding_csv = render_window_preceding_as_csv(window).strip()
    system = compose_correction_system(
        profile,
        tier=tier,
        variant=variant,
        extra_style=extra_style,
        style_block=style_block,
        knowledge_enabled=knowledge_enabled,
    )
    if task_update_feedback:
        system = (
            f"{system.rstrip()}\n\n"
            + _load_prompt_template(
                "correction_task_update_feedback_v2.md",
                feedback_schema=_feedback_schema(local_source_ids=True),
                # The block anchors after whatever really closes the reply:
                # parallel has no <next_advice>/<keep_entries> (plan A.7).
                feedback_position_clause=(
                    " `<next_advice>` 与 `<keep_entries>` 块之后"
                    if profile.continuity == "serial"
                    else " `<translated>` 块之后"
                ),
            )
        )
    # Plan 5r B2 (experiment): empty unless FINESUB_OVERLAP_CANDIDATES is set.
    from .overlap_candidates import candidate_rows

    overlap_rows = candidate_rows(window)
    overlap_candidates_block = (
        _load_prompt_template("fragment_overlap_candidates_v1.md", candidate_rows=overlap_rows).strip() + "\n\n"
        if overlap_rows
        else ""
    )
    user = compose_correction_user(
        profile,
        knowledge_enabled=knowledge_enabled,
        general_context_json=pack.general_prompt_text(),
        window_context=pack.window_context_for(window) or "（无）",
        entry_details=entry_details.strip() or "（无）",
        previous_advice=previous_advice.strip() or "（无）",
        pre_round_notes=window_notes.strip(),
        search_results=search_results.strip() or "（无）",
        preceding_context_csv=preceding_csv,
        current_asr_csv=current_csv,
        current_asr_row_count=len(window.segments),
        overlap_candidates_block=overlap_candidates_block,
        tier=tier,
        variant=variant,
        style_pick_reminder=style_pick_reminder,
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _knowledge_update_system(
    refined: bool,
    *,
    inputs_block: str,
    style_marks_section: str | None = None,
    split: bool = False,
) -> str:
    """The knowledge-update system message: structure spec + output contract.

    Shared with the conflict-repair round, which is the same task against a
    newer revision -- it must speak the same proposal grammar and the same
    output contract, so it takes the same system message rather than a second
    description of both.
    """

    from .knowledge.node.presets import CATEGORIES, MATCHABLE_CATEGORIES
    from .knowledge.node.render import render_category_line, render_structure_spec

    # The refined-alignment marks (`knowledge/style_marks.py`) are asked for
    # only when a style is pinned -- there is nothing to mark otherwise -- and
    # never in the conflict-repair round, which re-decides proposals only.
    style_marks_contract = (
        "\n"
        + _load_prompt_template(
            "fragment_knowledge_style_marks_v1.md",
            strict=True,
            convention_section=style_marks_section,
        ).strip()
        if refined and style_marks_section
        else ""
    )
    return _load_prompt_template(
        "knowledge_update_refined_v1.md"
        if refined
        else "knowledge_update_artifacts_only_v1.md",
        strict=True,
        knowledge_inputs=inputs_block,
        **({"style_marks_contract": style_marks_contract} if refined else {}),
        knowledge_structure=_load_prompt_template(
            "fragment_knowledge_structure_v1.md",
            strict=True,
            # EVERY category, not just the matchable ones (plan §0 requirement 3).
            # ⚠ Two different category sets meet in this file and they are not
            # interchangeable: the INDEX面 is matchable-only (a standalone
            # category has no index -- `repo.index_text` raises for one), while
            # the TEMPLATE面 is all of them. Calling this with no argument
            # defaulted to the matchable set, which is how `style`'s template
            # went unseen by the maintenance session it was written for -- the
            # refined one. Without refined subtitles (and in the conflict
            # repair, which speaks this contract) there is no style evidence
            # and no style to write: the templates are the writable ones only
            # (owner 2026-10-03, cold read Q6); the roster line stays whole.
            sections_spec=render_structure_spec(CATEGORIES if refined else MATCHABLE_CATEGORIES),
            categories_line=render_category_line(CATEGORIES),
            # Only where `split_entry` is in the op table below: the conflict
            # repair round is never offered it, and a rule naming it there is
            # a dead clause the gate rejects (cold read round 2, P181).
            split_clause=(
                _load_prompt_template("fragment_knowledge_split_clause_v1.md").strip()
                if split
                else ""
            ),
        ).strip(),
        # ...and a THIRD set, narrower than both: what this session may write.
        # `apply_model_proposals` gates the round on `MATCHABLE_CATEGORIES`, so
        # the contract has to name those and only those -- a template the model
        # can read is not a category it may write into.
        knowledge_output=render_knowledge_ops_contract(MATCHABLE_CATEGORIES, split=split),
    )



def build_knowledge_conflict_repair_messages(
    *,
    task_summary: str,
    read_rev: int,
    current_rev: int,
    dropped_rows: str,
    kb_entries: str,
) -> List[Dict[str, str]]:
    """Feed a concurrent-write conflict back to the model (plan B').

    Only the knowledge proposals are repairable this way, so the round always
    speaks the ``artifacts_only`` contract (one `<knowledge_proposals>` block)
    even when the task itself is ``refined_aligned``: since the translation
    ledgers retired, every proposal is a knowledge proposal, so there is no
    other block to repair.

    No fresh material is injected on purpose -- the model is asked to re-decide
    the dropped items against the CURRENT entries, not to think of new ones it
    can no longer see the evidence for.
    """

    inputs_block = _load_prompt_template(
        "fragment_knowledge_update_inputs_v1.md", strict=True, refined_csv_bullet=""
    ).strip()
    user = _load_prompt_template(
        "knowledge_conflict_repair_v1.md",
        strict=True,
        task_summary=task_summary.strip() or "（无）",
        read_rev=read_rev,
        current_rev=current_rev,
        dropped_rows=dropped_rows.strip() or "（无）",
        kb_entries=kb_entries.strip() or "（无）",
        final_reminder=(
            "先以一个 `<reasoning>` 块开头；随后输出有且仅有一个"
            " `<knowledge_proposals>` 块（可为空块）；除上述块外不要输出任何其他文字。"
        ),
    )
    from .knowledge.entry_size import offers_split

    return [
        {"role": "system", "content": _knowledge_update_system(
            False, inputs_block=inputs_block, split=offers_split(kb_entries)
        )},
        {"role": "user", "content": user},
    ]


def build_knowledge_update_messages(
    *,
    refined: bool,
    task_summary: str,
    window_packs: str,
    general_context: str = "",
    research_feedback: str = "",
    aggregated_feedback: str = "",
    kb_entries: str = "",
    knowledge_indexes: Mapping[str, str] | None = None,
    chunk_index: int = 1,
    multi_chunk: bool = False,
    window_range: str = "",
    style_marks_section: str | None = None,
) -> List[Dict[str, str]]:
    """Unified knowledge-update prompt (docs/knowledge.md).

    ``refined`` selects the ``refined_aligned`` variant (the refined subtitles
    as evidence, plus the style entry it may propose into); the
    ``artifacts_only`` variant has neither.
    ``window_packs`` is the rendered per-window material blocks of ONE chunk;
    ``multi_chunk`` marks a 100k-budget chunked run (the notice states no
    total, since over-limit chunks may split further at run time).
    The style entry, when the run named one, arrives through ``kb_entries``
    like every other entry — same handles, same apply path.
    ``style_marks_section`` (refined variant with a pinned style) names that
    style's convention section and asks for the ``<style_marks>`` block.
    """

    inputs_block = _load_prompt_template(
        "fragment_knowledge_update_inputs_v1.md",
        strict=True,
        refined_csv_bullet=(
            (
                "\n   - `<refined_csv>`：落在该窗口时间范围内的人工精修字幕行，"
                "`start|end|text`（已按开始时间重排；可能缺失，表示该窗口没有精修行）。"
            )
            if refined
            else ""
        ),
    ).strip()
    marks = bool(refined and style_marks_section)
    from .knowledge.entry_size import offers_split

    system = _knowledge_update_system(
        refined, inputs_block=inputs_block,
        style_marks_section=style_marks_section if marks else None,
        split=offers_split(kb_entries),
    )
    if multi_chunk:
        chunk_notice = (
            f"\n材料分块说明：本任务材料按 token 预算分为多块，本次调用是第 "
            f"{chunk_index} 块，只含窗口 {window_range}；其余块会在独立调用中处理并"
            "依次写入知识库。只根据本块材料提出更新，不要为块外内容预留占位。\n"
        )
    else:
        chunk_notice = ""
    # One proposal block in both variants since the style entry took over from
    # the mistake ledger (`docs/knowledge.md`, 「两个翻译台账（已退役，2026-09-02）」): a
    # style convention is an entry update like any other, so it travels in
    # `<knowledge_proposals>`. The one extra block, `<style_marks>`, is not an
    # update but a judgement on the pinned style's conventions, and is asked
    # for only when there is one (`knowledge/style_marks.py`).
    user = _load_prompt_template(
        "knowledge_update_user_v1.md",
        strict=True,
        task_summary=task_summary.strip() or "（无）",
        chunk_notice=chunk_notice,
        general_context=general_context.strip() or "（无）",
        research_feedback=research_feedback.strip() or "（无）",
        aggregated_feedback=aggregated_feedback.strip() or "（无）",
        kb_entries=kb_entries.strip() or "（无）",
        knowledge_indexes=render_knowledge_indexes(knowledge_indexes),
        window_packs=window_packs.strip() or "（无）",
        style_marks_reminder=(
            _load_prompt_template("fragment_knowledge_style_marks_reminder_v1.md").rstrip("\n")
            if marks
            else ""
        ),
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _agent_tool_retrieval_exception(native_search: bool) -> str:
    """The carve-out rule 4 needs when the call's project entitles retrieval.

    Without it the two inputs contradict each other: the session protocol's
    native-search fragment tells the model to look things up, while the
    bootstrap forbids every tool but the harness server's. Measured on the
    2026-08-30 batch, the model obeyed the protocol, the project hook refused
    the call, and the refusal produced no result step -- so the search was
    neither performed nor recorded.
    """

    if not native_search:
        return ""
    # `rstrip()` only: the fragment is spliced mid-sentence and its leading
    # space is load-bearing.
    return _load_prompt_template("fragment_agent_tool_retrieval_v1.md").rstrip()


def agent_tool_worker_bootstrap(
    *, assignment_id: str, worker_id: str, native_search: bool = False
) -> str:
    """The prompt for a headless agent that takes and submits its task over
    the harness MCP server (docs/llm_agent_tool_protocol.md §1)."""

    return _load_prompt_template(
        "agent_tool_worker_v1.md",
        assignment_id=assignment_id,
        worker_id=worker_id,
        retrieval_exception=_agent_tool_retrieval_exception(native_search),
    )


def agent_tool_worker_session_bootstrap(
    *, assignment_id: str, worker_id: str, native_search: bool = False
) -> str:
    """The prompt for a pseudo-conversational worker: one CLI session that
    takes the run's tasks one after another over the harness MCP server
    (docs/llm_local_agent.md §12.1.3)."""

    return _load_prompt_template(
        "agent_tool_worker_session_v1.md",
        assignment_id=assignment_id,
        worker_id=worker_id,
        retrieval_exception=_agent_tool_retrieval_exception(native_search),
    )


def conversational_correction_effort() -> str:
    """The one effort note that belongs to the conversational path only.

    Not in the shared output contract, and not in the worker bootstrap:

    * the contract is what every backend is held to, and measured 2026-08-25
      the same sentence raised a REST model's thinking by 55% while buying
      nothing there -- the behaviour it prevents (counting characters by hand,
      writing a script to verify them) is one only an agent with its own tools
      can afford in the first place;
    * the bootstrap is task-agnostic. It says how to take a task and submit
      one, for whatever task the assignment queues. A note about one column of
      one session type does not belong in it.

    So it is appended to the protocol document of a correction task on that
    one road, where the first live test showed a real agent spending most of
    an hour on exactly this.
    """

    return _load_prompt_template("fragment_conversational_correction_effort_v1.md").rstrip()


def agent_worker_bootstrap(
    *,
    assignment_root: str,
    assignment_id: str,
    worker_id: str,
    task_command: str,
    watch_minutes: int,
    durable_status: str,
) -> str:
    """The control-protocol contract handed to a conversational Agent worker.

    This is prompt text a model reads and acts on, so it lives in
    ``prompt_templates/`` with the rest -- unlike the driver framing that is
    welded to one CLI's argv and belongs beside it.
    """

    return _load_prompt_template(
        "agent_worker_bootstrap_v1.md",
        assignment_root=assignment_root,
        assignment_id=assignment_id,
        worker_id=worker_id,
        task_command=task_command,
        watch_minutes=watch_minutes,
        durable_status=durable_status,
    )
