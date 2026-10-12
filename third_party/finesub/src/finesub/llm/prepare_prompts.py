"""Prompts of the global preparation task (plan §2.1: one task, one logical conversation).

The old research session was two heterogeneous prompts -- R1 "ask" and R2
"digest", each with its own system prompt and its own parser. The
preparation task is ONE contract: the first message states the whole task
and the deliverable; the model either delivers ``<context_pack>`` at once or
first emits one batch of tool requests (the same ``<search_queries>`` /
``<requested_entries>`` / ``<keep_entries>`` blocks R1 used, so their parsers
and caps carry over). The harness executes the batch and appends the results
as the next user turn (:func:`build_prepare_results_message`); the same
conversation then delivers.

Capabilities shape *what may be requested*, never whether the task exists
(plan §4): with no search and no readable index there is simply no request
channel, and the prompt says the pack must not dress inference up as
verified fact. Under the agentic strategy (stage C) the request channel is
replaced by the executor's own tools; ``--style auto`` (stage E,
``docs/knowledge.md``, 「style 类别」) adds a candidate catalog and a
``<style_selection>`` block to the delivery.

Every sentence the model reads lives in ``prompt_templates/``; this module
only chooses and fills fragments.
"""

from __future__ import annotations

from typing import Dict, List, Mapping

from .prompts import (
    _R1_BACKGROUND_COMMON,
    _R1_BACKGROUND_KNOWLEDGE,
    TASK_FEEDBACK_REMINDER,
    _load_prompt_template,
    _numbered,
    _r1_entry_blocks,
    _research_task_feedback_block,
    _search_queries_rules,
    _search_results_usage,
    reasoning_clause,
    render_knowledge_indexes,
)
from .knowledge.style import catalog_offers_default
from .routing.config import (
    DEFAULT_RESEARCH_SEARCH_QUERIES,
    KB_TRANSFER_MAX_ENTRIES,
    KB_WINDOW_TOTAL_ENTRIES,
)

STYLE_SELECTION_TAG = "style_selection"


def _tool_list(*, search: bool, knowledge: bool) -> str:
    return "、".join(
        part
        for part, on in (
            ("联网检索 `web_search` / `web_fetch`", search),
            ("知识库 `kb_search` / `kb_read`", knowledge),
        )
        if on
    )


def _acquire_section(
    *,
    emits_queries: bool,
    emits_entries: bool,
    native_search: bool,
    max_search_queries: int,
    max_requested_entries: int,
    agentic: bool = False,
) -> str:
    """The "how to get what you lack" section, per grant and strategy."""

    parts: List[str] = []
    if agentic and (emits_queries or emits_entries):
        return _load_prompt_template(
            "fragment_prepare_agentic_acquire_v1.md",
            tools=_tool_list(search=emits_queries, knowledge=emits_entries),
        )
    if native_search:
        parts.append(
            _load_prompt_template("fragment_native_search_research_v1.md").strip()
        )
    request_blocks: List[str] = []
    if emits_entries:
        request_blocks.append(_r1_entry_blocks(max_requested_entries, keep_source_block="`<preinjected_entries>`"))
    if emits_queries:
        request_blocks.append(
            _search_queries_rules(max_search_queries, knowledge_enabled=emits_entries)
        )
    if request_blocks:
        what = "、".join(
            part
            for part, on in (("知识库词条", emits_entries), ("联网搜索", emits_queries))
            if on
        )
        parts.append(
            _load_prompt_template(
                "fragment_prepare_request_channel_v1.md",
                what=what,
                request_blocks="\n".join(request_blocks),
            ).rstrip()
        )
    if not parts:
        parts.append(_load_prompt_template("fragment_prepare_no_evidence_v1.md").strip())
    return "\n\n".join(parts) + "\n"


#: Where the `<style_selection>` block goes, per shape of the asking task
#: (auto-style-gaps plan A2: "按授权拼"). ``request``: a batched window whose
#: request turn can also ask for search / entries; ``style_only``: one whose
#: request turn exists only to pick; ``agentic``: a tool-session window, which
#: picks in its delivery.
STYLE_OUTPUT_CLAUSES: Dict[str, str] = {
    "prepare": "fragment_style_output_prepare_v1.md",
    "request": "fragment_style_output_request_v1.md",
    "style_only": "fragment_style_output_style_only_v1.md",
    "agentic": "fragment_style_output_agentic_v1.md",
}
#: ``optional``: an agentic preparation task may read a candidate in full;
#: ``required``: an agentic window must read the one it picks before it
#: translates, and the harness checks that it did (A1 step 3).
STYLE_READ_CLAUSES: Dict[str, str] = {
    "optional": "fragment_style_read_optional_v1.md",
    "required": "fragment_style_read_required_v1.md",
}


def render_style_selection_section(
    catalog: str,
    *,
    extra_style: str = "",
    shape: str = "prepare",
    read: str = "",
) -> str:
    """The ``--style auto`` section: candidates as material, one forced pick, a JSON block.

    The pick is part of the delivery contract (auto-style-gaps plan 「已裁定」 8):
    with `default_style` among the candidates "nothing fits" is answered by
    choosing it; without one, by an explicit empty pick.
    """

    if not catalog.strip():
        return ""
    return "\n" + _load_prompt_template(
        "fragment_prepare_style_selection_v1.md",
        output_clause=_load_prompt_template(STYLE_OUTPUT_CLAUSES[shape]).strip(),
        # Who brings the picked rules in front of the translation: the
        # harness everywhere except the agentic window, which reads them.
        after_pick_clause=_load_prompt_template(
            "fragment_style_after_pick_read_v1.md"
            if read == "required"
            else "fragment_style_after_pick_injected_v1.md"
        ).strip(),
        no_match_clause=_load_prompt_template(
            "fragment_style_no_match_default_v1.md"
            if catalog_offers_default(catalog)
            else "fragment_style_no_match_none_v1.md"
        ).strip(),
        extra_style_clause=(
            f"（用户本次的翻译要求：{extra_style.strip()}）" if extra_style.strip() else ""
        ),
        read_clause=(
            _load_prompt_template(STYLE_READ_CLAUSES[read]).strip() + "\n" if read else ""
        ),
        catalog=catalog.strip(),
    ).rstrip() + "\n"


def build_prepare_task_messages(
    *,
    transcript: str,
    extra_info: str = "",
    note_url_extracts: str = "",
    knowledge_indexes: Mapping[str, str] | None = None,
    preinjected_entries: str = "",
    max_search_queries: int = DEFAULT_RESEARCH_SEARCH_QUERIES,
    emits_queries: bool = True,
    emits_entries: bool = True,
    native_search: bool = False,
    max_requested_entries: int = KB_WINDOW_TOTAL_ENTRIES,
    collect_task_feedback: bool = False,
    emits_keep: bool = True,
    agentic: bool = False,
    style_catalog: str = "",
    extra_style: str = "",
    speakers: bool = False,
) -> List[Dict[str, str]]:
    """The task's first message: the whole contract, plus the request channel.

    ``emits_queries`` is a local search agent behind the harness
    (``retrieval=local``); ``native_search`` is the model searching inside the
    call (``retrieval=native``); ``emits_entries`` is a readable knowledge
    base. ``emits_keep`` asks the delivery for ``<keep_entries>`` -- only
    meaningful where a later round can request entries again. ``agentic``
    swaps the request channel for the executor's own tools (stage C): the
    same grant, fetched by the model inside its tool session, delivered in
    one reply. ``style_catalog`` (non-empty under ``--style auto``) adds the
    candidate list and asks the delivery for a ``<style_selection>`` block.
    ``speakers`` (only with the speaker judge on) also asks, lightly, for
    the people who speak in the material: the judge's candidates. Off, the
    prompt is what it always was.
    """

    can_request = (emits_queries or emits_entries) and not agentic
    input_sources = []
    if emits_entries:
        # Agentic: the index is a required block fetched with `kb_index` (the
        # submit gate checks that read), so it is not inlined as well -- the
        # same as the agentic window (owner 2026-10-03, cold read Q2).
        input_sources.append(
            "harness 根据用户备注关键词预注入的知识库条目全文（可能为空；知识库索引用 `kb_index` 读）"
            if agentic
            else "本地知识库的索引，以及 harness 根据用户备注关键词预注入的知识库条目全文（可能为空）"
        )
    background = [
        *_R1_BACKGROUND_COMMON,
        "你的输出会直接注入后续纠错与翻译调用（可能含音频，也可能是纯文本），"
        "是它理解内容的主要背景来源；请输出紧凑、可直接使用的信息，而不是长篇解释。",
        "transcript 的源序号只用于本轮定位；后续单窗口调用会重新编号。"
        "`context_pack` 不要引用裸源序号，改用原文短语、实体或事件描述作为锚点。",
        *(_R1_BACKGROUND_KNOWLEDGE if emits_entries else ()),
        *(
            (
                "你自己没有联网搜索能力。你提出的搜索 query 会由本地搜索代理执行，"
                "结果回填到本对话供你交付时使用。",
            )
            if emits_queries and not agentic
            else ()
        ),
    ]
    uncertainty_sources = "、".join(
        part
        for part, on in (
            ("知识库", emits_entries),
            ("检索结果", emits_queries or native_search),
        )
        if on
    )
    style_section = render_style_selection_section(
        style_catalog, extra_style=extra_style, read="optional" if agentic else ""
    )
    system = _load_prompt_template(
        "prepare_task_v1.md",
        input_sources=("、" + "".join(input_sources)) if input_sources else "",
        background_items=_numbered(background),
        acquire_section=_acquire_section(
            emits_queries=emits_queries,
            emits_entries=emits_entries,
            native_search=native_search,
            max_search_queries=max_search_queries,
            max_requested_entries=max_requested_entries,
            agentic=agentic,
        ),
        style_section=style_section,
        uncertainty_sources=(uncertainty_sources + "都") if uncertainty_sources else "",
        evidence_rule=(
            ""
            if (can_request or native_search or agentic)
            else "本任务没有外部证据：`sources` 留空，推断一律进 `uncertainties` 或标「未核实」。"
        ),
        reasoning_clause=reasoning_clause(),
        speakers_duty=(
            "\n" + _load_prompt_template("fragment_prepare_speakers_duty_v1.md").rstrip("\n") if speakers else ""
        ),
        speakers_field=(
            "\n" + _load_prompt_template("fragment_prepare_speakers_field_v1.md").rstrip("\n") if speakers else ""
        ),
        delivery_lead=("交付时" if can_request else ""),
        keep_entries_block=(
            _load_prompt_template(
                "fragment_research_keep_entries_v1.md",
                max_keep_entries=KB_TRANSFER_MAX_ENTRIES,
                # Where the keys it may keep came from, as wide as the
                # delivery check (`stages/prepare_task.py`): batched, the fill
                # turn's block plus the preinjected one; agentic, the
                # preinjected entries plus whatever it read with `kb_read` --
                # there is no fill turn, so `<knowledge_entries>` would name a
                # block that does not exist.
                key_source=(
                    "预注入的或你在本任务里用 `kb_read` 读过的"
                    if agentic
                    else " `<preinjected_entries>` 或 `<knowledge_entries>` 中出现的"
                ),
                ignored_clause=(
                    "知识库里没有的 key 会被忽略"
                    if agentic
                    else "这两处之外的 key 会被忽略"
                ),
            ).rstrip()
            + "\n"
            if emits_keep and emits_entries
            else ""
        ),
        task_update_feedback_block=(
            _research_task_feedback_block(local_source_ids=False)
            if collect_task_feedback
            else ""
        ),
    )
    duties = [
        "理解整段内容并交付 `<context_pack>`",
        *(("需要时先 request 尚未注入的词条、keep 仍需使用的预注入词条",) if emits_entries and not agentic else ()),
        *(("需要时先提出值得联网查证的搜索 query",) if emits_queries and not agentic else ()),
        *(("需要查证的内容用授权的工具自己取得",) if agentic and (emits_queries or emits_entries) else ()),
        *(("按 system 的候选选定本次翻译风格",) if style_section else ()),
    ]
    closing = (
        "需要外部输入时按 system 指令只输出请求块并结束；否则直接输出有且仅有一个 "
        "`<context_pack>` 块"
        if can_request
        else "输出有且仅有一个 `<context_pack>` 块"
    )
    if emits_keep and emits_entries:
        closing += "，交付时随后输出 `<keep_entries>` 块"
    if style_section:
        closing += f"，交付时随后输出 `<{STYLE_SELECTION_TAG}>` 块"
    if collect_task_feedback:
        closing += TASK_FEEDBACK_REMINDER
    knowledge_inputs = (
        _load_prompt_template(
            "fragment_knowledge_inputs_v1.md",
            index_block=(
                ""
                if agentic
                else "知识库索引：\n" + render_knowledge_indexes(knowledge_indexes) + "\n\n"
            ),
            preinjected_entries=preinjected_entries.strip() or "（无）",
        )
        if emits_entries
        else "\n"
    )
    user = _load_prompt_template(
        "prepare_task_user_v1.md",
        task_summary="、".join(duties),
        extra_info=extra_info or "（无）",
        note_url_extracts=note_url_extracts.strip() or "（无）",
        knowledge_inputs=knowledge_inputs,
        transcript=transcript,
        closing_reminder=closing,
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def build_prepare_results_message(
    *,
    entry_details_text: str = "",
    search_results: str = "",
    requested_entries: bool = False,
    requested_search: bool = False,
    collect_task_feedback: bool = False,
    emits_keep: bool = True,
    style_selection: bool = False,
) -> Dict[str, str]:
    """The fill turn: what the batch produced, and the order to deliver.

    A channel the model asked for that came back empty is said so explicitly
    -- an execution failure or a miss is a result, not silence (plan §2.1).
    """

    knowledge_input = ""
    if requested_entries:
        body = entry_details_text.strip() or "（请求的词条在知识库中均不存在）"
        knowledge_input = (
            "\n你请求的知识库条目详情：\n<knowledge_entries>\n" + body + "\n</knowledge_entries>\n"
        )
    search_input = ""
    if requested_search:
        body = search_results.strip() or "（本地搜索代理没有返回任何结果）"
        search_input = "\n本地搜索代理对你的 query 返回的结果：\n<search_results>\n" + body + "\n</search_results>\n"
    usage = _search_results_usage() if requested_search and search_results.strip() else ""
    reminders = ""
    if emits_keep and requested_entries:
        reminders += "，随后输出 `<keep_entries>` 块"
    if style_selection:
        reminders += f"，随后输出 `<{STYLE_SELECTION_TAG}>` 块"
    content = _load_prompt_template(
        "prepare_task_results_v1.md",
        knowledge_entries_input=knowledge_input,
        search_results_input=search_input,
        usage_rules=usage,
        keep_entries_reminder=reminders,
        task_feedback_reminder=TASK_FEEDBACK_REMINDER if collect_task_feedback else "",
    )
    return {"role": "user", "content": content}
