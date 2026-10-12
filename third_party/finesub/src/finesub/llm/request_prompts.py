"""Correction window as one task with an in-conversation request turn (plan §3, stage B).

Wraps :func:`prompts.build_correction_csv_messages` -- the shipped correction
prompt stays byte-identical (its goldens are the review surface) -- and adds
the task-flow shape on top:

* ``request_channel``: the system prompt gains the "获取缺失输入" section
  (search queries and/or entry requests per the grant, plus the knowledge
  index the requests are picked from), and the user message says nothing
  was retrieved yet. The reply is either the deliverable or one request
  batch.
* ``style_catalog`` (``--style auto`` on a fast window, auto-style-gaps plan
  A): the pick is part of the window's contract. Batched, the request turn
  must carry it (a direct delivery is malformed); with nothing else to ask
  for, the request turn exists only to pick (A2). Agentic, the delivery
  carries it after the executor read the picked style in full (A1).
* ``request_reply``: the batch happened. The model's own request turn is
  replayed verbatim as the assistant message and the results follow as the
  next user turn (:func:`build_correction_fill_message`); the media clip
  rides on the last user message, exactly where ``attach_file_to_messages``
  puts it.
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping

from .prepare_prompts import render_style_selection_section
from .prompts import (
    _load_prompt_template,
    _r1_entry_blocks,
    _search_queries_rules,
    _search_results_usage,
    build_correction_csv_messages,
    render_knowledge_indexes,
)
from .prompt_variants import resolve_variant
from .routing.profiles import DEFAULT_PROFILE
from .routing.config import (
    CapabilityTier,
    KB_WINDOW_NEW_REQUEST_MAX_ENTRIES,
    MAX_WINDOW_SEARCH_QUERIES,
)

NOT_RETRIEVED_YET = "（本轮尚未检索；需要由 harness 代为取得的输入时按 system 指令先提请求）"
SELF_RETRIEVED = "（本任务由你用授权的工具自行检索，没有回填轮）"

def render_agentic_window_note(*, search: bool, knowledge: bool) -> str:
    """Agentic strategy (stage C): the window runs on a tool session with the
    harness server's `web_search` / `web_fetch` (only under retrieval=local --
    the session carries no web tools otherwise) and `kb_*` tools, so it
    fetches for itself; no request batch, no fill turn."""

    tools = "、".join(
        part
        for part, on in (
            ("联网检索 `web_search` / `web_fetch`", search),
            ("知识库 `kb_search` / `kb_read`", knowledge),
        )
        if on
    )
    return _load_prompt_template("fragment_window_agentic_acquire_v1.md", tools=tools).rstrip() + "\n"


def _any_index(indexes: Mapping[str, str] | None) -> bool:
    return any((text or "").strip() for text in (indexes or {}).values())


def render_window_request_channel(
    *,
    knowledge_indexes: Mapping[str, str] | None,
    search: bool = True,
    native_search: bool = False,
    max_search_queries: int = MAX_WINDOW_SEARCH_QUERIES,
    max_requested_entries: int = KB_WINDOW_NEW_REQUEST_MAX_ENTRIES,
    style_pick: bool = False,
) -> str:
    """The system-side request section for one window: harness search where
    ``retrieval=local`` (``search``), entry requests where the base is
    readable, or both. The opening sentence names only what was granted, one
    fragment per grant (the ``local`` two are the wording that shipped before
    none/native had a channel); under ``native`` it adds that the model's own
    search stays its own (the correction prompt's retrieval slot describes
    that tool). ``style_pick``: the first reply must pick a style, so "no
    input needed" no longer means "deliver now"."""

    knowledge = _any_index(knowledge_indexes)
    if search:
        acquire = (
            "fragment_window_acquire_search_knowledge_v1.md"
            if knowledge
            else "fragment_window_acquire_search_v1.md"
        )
        acquire_clause = _load_prompt_template(acquire).strip()
    else:
        acquire_clause = _load_prompt_template(
            "fragment_window_acquire_knowledge_v1.md",
            native_search_note=(
                _load_prompt_template("fragment_window_native_search_note_v1.md").strip()
                if native_search
                else ""
            ),
        ).strip()
    return _load_prompt_template(
        "fragment_window_request_channel_v1.md",
        acquire_clause=acquire_clause,
        no_request_clause=_load_prompt_template(
            "fragment_window_no_request_style_v1.md"
            if style_pick
            else "fragment_window_no_request_deliver_v1.md"
        ).strip(),
        style_block_clause=(
            _load_prompt_template("fragment_window_request_style_block_v1.md").strip()
            if style_pick
            else ""
        ),
        entry_request_blocks=(
            _r1_entry_blocks(max_requested_entries, keep_source_block="`<entry_details>`") + "\n"
            if knowledge
            else ""
        ),
        search_queries_rules=(
            _search_queries_rules(max_search_queries, knowledge_enabled=knowledge)
            if search
            else ""
        ),
        knowledge_index_block=(
            "\n知识库索引（`<requested_entries>` 从这里挑；`<keep_entries>` 只能写首条 user 消息 "
            "`<entry_details>` 里实际可见的词条）：\n" + render_knowledge_indexes(knowledge_indexes)
            if knowledge
            else ""
        ),
    ).rstrip() + "\n"


def render_window_style_channel() -> str:
    """The request section of a window whose only request is the style pick
    (auto-style-gaps plan A2): the opening speaks of choosing a style alone --
    no search, no entries, nothing to request but the pick."""

    return _load_prompt_template("fragment_window_style_channel_v1.md").rstrip() + "\n"


def build_correction_fill_message(
    *,
    entry_details: str = "",
    search_results: str = "",
    requested_entries: bool = False,
    requested_search: bool = False,
    serial: bool = True,
    style_block: str = "",
    entry_channel: bool = True,
    singles: bool = False,
) -> Dict[str, str]:
    """The fill turn: what the batch produced, then the order to deliver.

    ``style_block`` is the full projection of the style the request turn
    picked under ``--style auto`` (auto-style plan §4: the rules must be in
    front of the model before it translates, never claimed after the fact).
    ``entry_channel`` False is a request turn with no entry channel (search
    only, or the style pick alone): there is no transfer chain to continue,
    so the closing order does not ask for ``<keep_entries>``. ``singles``
    is the variant's own block order: only a variant that requires the
    full-window ``<singles>`` block is told to write one.
    """

    knowledge_input = ""
    if style_block.strip():
        knowledge_input += "\n" + style_block.strip() + "\n"
    if requested_entries:
        body = entry_details.strip() or "（请求的词条在知识库中均不存在）"
        knowledge_input += (
            "\n知识库条目详情（你请求的词条与此前透传的词条，全文）：\n<entry_details>\n"
            + body
            + "\n</entry_details>\n"
        )
    search_input = ""
    if requested_search:
        body = search_results.strip() or "（本地搜索代理没有返回任何结果）"
        search_input = (
            "\n本窗口搜索结果（本地搜索代理对你的 query 的返回）：\n<search_results>\n"
            + body
            + "\n</search_results>\n"
        )
    usage = _search_results_usage() if requested_search and search_results.strip() else ""
    content = _load_prompt_template(
        "correction_fill_v1.md",
        knowledge_entries_input=knowledge_input,
        search_results_input=search_input,
        usage_rules=usage,
        singles_step="再完整 `<singles>`，随后 " if singles else "再 ",
        trailing_reminder=(
            ("，然后 `<next_advice>`（及 `<keep_entries>`）" if entry_channel else "，然后 `<next_advice>`")
            if serial
            else ""
        ),
    )
    return {"role": "user", "content": content}


#: The reminder line a style pick adds to the window's user message, by the
#: shape of the pick (`render_style_selection_section`'s ``shape``).
STYLE_PICK_REMINDERS: Dict[str, str] = {
    "request": "fragment_style_pick_reminder_request_v1.md",
    "style_only": "fragment_style_pick_reminder_style_only_v1.md",
    "agentic": "fragment_style_pick_reminder_agentic_v1.md",
}


def _style_pick_reminder(style_catalog: str, shape: str) -> str:
    if not style_catalog.strip():
        return ""
    return _load_prompt_template(STYLE_PICK_REMINDERS[shape]).strip()


def build_window_task_messages(
    *,
    request_channel: bool = False,
    request_reply: str = "",
    knowledge_indexes: Mapping[str, str] | None = None,
    carried_entries: str = "",
    requested_entries: bool = False,
    requested_search: bool = False,
    max_search_queries: int = MAX_WINDOW_SEARCH_QUERIES,
    agentic: bool = False,
    style_catalog: str = "",
    extra_style_hint: str = "",
    style_fill_block: str = "",
    **correction_kwargs: Any,
) -> List[Dict[str, Any]]:
    """Correction messages, in the shape the task flow needs.

    ``correction_kwargs`` are :func:`build_correction_csv_messages`'s own.
    Without ``request_channel`` this is that function, unchanged. With it
    and no ``request_reply`` yet: the two opening messages of the task
    (system + request section, user with ``NOT_RETRIEVED_YET`` in the
    retrieval slots and only the *carried* entries visible). With a
    ``request_reply``: those two plus the replayed assistant turn and the
    fill turn carrying ``search_results`` / the full ``entry_details``.
    ``agentic`` (stage C) is the tool-session shape instead: the same two
    opening messages, but the acquire section names the executor's own
    tools and no fill ever comes.
    """

    profile = correction_kwargs.get("profile") or DEFAULT_PROFILE
    search = profile.external_injection
    knowledge = _any_index(knowledge_indexes)
    if agentic and not request_channel:
        correction_kwargs["style_pick_reminder"] = _style_pick_reminder(style_catalog, "agentic")
        style_section = render_style_selection_section(
            style_catalog, extra_style=extra_style_hint, shape="agentic", read="required"
        )
        if not (search or knowledge):
            # Only the style pick to fetch (A1 with the narrow `kb_tools:
            # "style"` grant): the correction prompt itself, with no acquire
            # note -- there is nothing to verify names against -- and the pick
            # appended. Without a catalog (pinned or none) this is exactly the
            # shape every window without a request has.
            messages = build_correction_csv_messages(**correction_kwargs)
            if not style_section:
                return messages
            system = dict(messages[0])
            system["content"] = str(system["content"]).rstrip() + "\n\n" + style_section.lstrip("\n")
            return [system, *messages[1:]]
        opening_kwargs = dict(correction_kwargs)
        opening_kwargs.pop("search_results", None)
        opening_kwargs.pop("window_notes", None)
        system, user = build_correction_csv_messages(
            search_results=SELF_RETRIEVED, window_notes="",
            **opening_kwargs,
        )
        system = dict(system)
        system["content"] = (
            str(system["content"]).rstrip()
            + "\n\n"
            + render_agentic_window_note(search=search, knowledge=knowledge)
            + style_section
        )
        return [system, user]
    if not request_channel:
        return build_correction_csv_messages(**correction_kwargs)
    # The request turn exists only for the pick: nothing to search, no base
    # to request entries from (A2).
    style_only = not (search or knowledge)
    opening_kwargs = dict(correction_kwargs)
    opening_kwargs["style_pick_reminder"] = _style_pick_reminder(
        style_catalog, "style_only" if style_only else "request"
    )
    search_results = str(opening_kwargs.pop("search_results", "") or "")
    entry_details = str(opening_kwargs.pop("entry_details", "") or "")
    opening_kwargs.pop("window_notes", None)
    system, user = build_correction_csv_messages(
        search_results=NOT_RETRIEVED_YET,
        window_notes="",
        entry_details=carried_entries,
        **opening_kwargs,
    )
    system = dict(system)
    system["content"] = (
        str(system["content"]).rstrip()
        + "\n\n"
        + (
            render_window_style_channel()
            if style_only
            else render_window_request_channel(
                knowledge_indexes=knowledge_indexes,
                search=search,
                native_search=profile.native_search,
                max_search_queries=max_search_queries,
                style_pick=bool(style_catalog.strip()),
            )
        )
        # `--style auto` on the fast window (auto-style plan §4): the
        # pick happens in the request turn, the picked rules ride in the fill.
        + render_style_selection_section(
            style_catalog,
            extra_style=extra_style_hint,
            shape="style_only" if style_only else "request",
        )
    )
    messages = [system, user]
    if not request_reply:
        return messages
    serial = profile.continuity == "serial"
    messages.append({"role": "assistant", "content": request_reply})
    messages.append(
        build_correction_fill_message(
            entry_details=entry_details,
            search_results=search_results,
            requested_entries=requested_entries,
            requested_search=requested_search,
            serial=serial,
            style_block=style_fill_block,
            entry_channel=knowledge,
            singles=resolve_variant(
                correction_kwargs.get("variant"),
                correction_kwargs.get("tier", CapabilityTier.CAPABLE),
            ).require_full_singles,
        )
    )
    return messages
