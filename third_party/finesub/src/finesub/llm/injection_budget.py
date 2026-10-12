"""Token-budgeted rendering of harness-injected prompt blocks.

Search results, extract results, and knowledge entries all share the same
budget scheme: each unit (one query's results, one extracted URL, one
knowledge entry) is rendered as a *section* capped at a per-section token
limit, and the whole block is capped at a block limit derived from the
round's unit cap (``config.injection_block_token_limit``). Sections are
consumed in priority order; the section that overflows the block budget is
truncated if enough budget remains, otherwise it and everything after it is
dropped (later sections never jump the queue past a dropped one).

The caller learns exactly which sections were fully included, truncated, or
dropped — the search loop uses this to avoid decrementing fact priorities
for queries whose results never (fully) reached the model.

Knowledge entries are budgeted differently (`render_knowledge_entries_block`,
plan kb-entry-size): each entry is capped at its category's
``max_entry_tokens``, a full block is shared out longest-first ("water
filling": short entries stay whole, only the longest are cut), and an entry
is cut by whole lines -- its head (H1, intro, the identity section) and its
metadata always stay, so a cut entry is still recognisably that entry.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, Collection, Mapping, Sequence

from .token_budget import HeuristicTokenCounter, local_counter_available_for
from .token_truncate import truncate_to_token_window

SECTION_JOINER = "\n\n"
# Budget slice set aside for the trailing harness notice that lists
# truncated/dropped sections (only rendered when something was cut). Small
# blocks reserve block_limit // 3 instead so most of the budget stays usable.
NOTICE_RESERVE_TOKENS = 300
# When the remaining block budget falls below this, a section is dropped
# instead of being truncated to a uselessly small fragment.
MIN_PARTIAL_SECTION_TOKENS = 200
_NOTICE_LABEL_LIMIT = 6
_NOTICE_LABEL_CHARS = 30


@dataclass(frozen=True)
class RenderedBlock:
    """A budget-rendered block plus the fate of every input section."""

    text: str
    tokens: int
    included: tuple[str, ...] = ()
    truncated: tuple[str, ...] = ()
    dropped: tuple[str, ...] = ()
    #: Knowledge entries only: per truncated entry, how many content lines of
    #: each section made it in (a section missing from the map: none did), and
    #: why it was cut -- ``cap`` (longer than its category's injection cap) or
    #: ``block`` (shared out of a full block).
    kept: Mapping[str, Mapping[str, int]] = field(default_factory=dict)
    causes: Mapping[str, str] = field(default_factory=dict)

    @property
    def injected(self) -> tuple[str, ...]:
        """Labels that reached the prompt, whole or cut."""

        return self.included + self.truncated

    def report(self) -> dict:
        out = {
            "tokens": self.tokens,
            "included": list(self.included),
            "truncated": list(self.truncated),
            "dropped": list(self.dropped),
        }
        if self.kept:
            out["kept"] = {label: dict(sections) for label, sections in self.kept.items()}
        if self.causes:
            out["causes"] = dict(self.causes)
        return out


EMPTY_BLOCK = RenderedBlock(text="", tokens=0)


def _notice_text(truncated: Sequence[str], dropped: Sequence[str]) -> str:
    def _labels(labels: Sequence[str]) -> str:
        shown = [label[:_NOTICE_LABEL_CHARS] for label in labels[:_NOTICE_LABEL_LIMIT]]
        text = "、".join(shown)
        if len(labels) > _NOTICE_LABEL_LIMIT:
            text += f" 等 {len(labels)} 项"
        return text

    parts = []
    if truncated:
        parts.append(f"以下条目因超出 token 预算被截断：{_labels(truncated)}")
    if dropped:
        parts.append(f"以下条目被整体丢弃：{_labels(dropped)}")
    if not parts:
        return ""
    return "（注入预算说明：" + "；".join(parts) + "。）"


def render_budgeted_block(
    sections: Sequence[tuple[str, str]],
    *,
    count_tokens: Callable[[str], int],
    heuristic_count: Callable[[str], int] | None = None,
    section_limit: int,
    block_limit: int,
    min_partial_tokens: int = MIN_PARTIAL_SECTION_TOKENS,
) -> RenderedBlock:
    """Assemble ``(label, text)`` sections under per-section and block budgets.

    ``heuristic_count`` explicitly overrides budget estimation. Otherwise a
    runnable local tokcount binary is used directly; when it is unavailable,
    the upper-bound estimator skips API counting for sections with ample
    headroom.
    """

    use_local_exact = (
        heuristic_count is None and local_counter_available_for(count_tokens)
    )
    estimate_tokens = (
        count_tokens
        if use_local_exact
        else heuristic_count or HeuristicTokenCounter().count_text
    )
    truncation_heuristic = None if use_local_exact else estimate_tokens
    sections = [(label, (text or "").strip()) for label, text in sections]
    sections = [(label, text) for label, text in sections if text]
    if not sections or block_limit <= 0:
        dropped = tuple(label for label, _ in sections)
        if not dropped:
            return EMPTY_BLOCK
        notice = _notice_text((), dropped)
        return RenderedBlock(
            text=notice, tokens=estimate_tokens(notice), dropped=dropped
        )

    joiner_tokens = estimate_tokens(SECTION_JOINER)
    budget = block_limit - min(NOTICE_RESERVE_TOKENS, block_limit // 3)

    parts: list[str] = []
    included: list[str] = []
    truncated: list[str] = []
    dropped: list[str] = []
    used = 0

    for pos, (label, text) in enumerate(sections):
        join_cost = joiner_tokens if parts else 0
        remaining = budget - used - join_cost
        effective_limit = min(section_limit, remaining)
        if effective_limit < min_partial_tokens:
            # Not enough budget left for a meaningful fragment: drop this and
            # every later section (priority order is preserved).
            dropped.extend(lbl for lbl, _ in sections[pos:])
            break
        result = truncate_to_token_window(
            text,
            effective_limit,
            count_tokens,
            keep="head",
            heuristic_count=truncation_heuristic,
        )
        if not result.text:
            dropped.extend(lbl for lbl, _ in sections[pos:])
            break
        parts.append(result.text)
        used += result.tokens + join_cost
        if result.text == text:
            included.append(label)
        else:
            truncated.append(label)

    if not parts:
        notice = _notice_text(truncated, dropped)
        return RenderedBlock(
            text=notice,
            tokens=estimate_tokens(notice) if notice else 0,
            truncated=tuple(truncated),
            dropped=tuple(dropped),
        )

    text = SECTION_JOINER.join(parts)
    if truncated or dropped:
        notice = _notice_text(truncated, dropped)
        text = f"{text}\n{notice}"
        used += estimate_tokens(notice) + joiner_tokens
    return RenderedBlock(
        text=text,
        tokens=used,
        included=tuple(included),
        truncated=tuple(truncated),
        dropped=tuple(dropped),
    )


# --- knowledge entries --------------------------------------------------------

_HANDLE_RE = re.compile(r"<!-- (@k\d+) -->")


@dataclass
class _Section:
    name: str
    heading: list[str]
    lines: list[str]


@dataclass
class _Entry:
    """One injected entry split along its rendering: ``head`` (everything up
    to the first section that may be cut, the identity section included --
    it is what makes a cut entry still that entry), the cuttable
    ``sections``, and the metadata ``tail``. Built from the TEXT, so every
    caller keeps handing in ``key -> body`` as before."""

    label: str
    text: str
    head: list[str]
    sections: list[_Section]
    tail: list[str]


def _split_entry(label: str, text: str) -> _Entry:
    from .knowledge.node.model import METADATA_SECTION
    from .knowledge.node.presets import head_section_names

    head_names = head_section_names()
    head: list[str] = []
    sections: list[_Section] = []
    tail: list[str] = []
    mode = "head"
    in_comment = False
    for line in text.strip().split("\n"):
        if line.startswith("## ") and not in_comment:
            name = line[3:].strip()
            if name == METADATA_SECTION:
                mode = "tail"
            elif name in head_names and mode != "tail":
                # the identity section is head wherever a reordered entry put
                # it -- `signals.injected_node_ids` counts it as shown
                mode = "head"
                if head and head[-1].strip():
                    head.append("")
            elif mode != "tail":
                mode = "section"
                sections.append(_Section(name, [line], []))
                continue
        if mode == "head":
            head.append(line)
            continue
        if mode == "tail":
            tail.append(line)
            continue
        section = sections[-1]
        stripped = line.strip()
        if in_comment or stripped.startswith("<!--"):
            # guidance comments sit under the heading, before any line
            in_comment = not stripped.endswith("-->")
            (section.lines if section.lines else section.heading).append(line)
            continue
        if not stripped:
            if not section.lines:
                section.heading.append(line)  # the blank under the heading
            continue  # separators between sections are rebuilt
        section.lines.append(line)
    while head and not head[-1].strip():
        head.pop()
    return _Entry(label, text.strip(), head, sections, tail)


@dataclass
class _Costed:
    entry: _Entry
    head: int
    tail: int
    headings: list[int]
    lines: list[list[int]]
    pinned: bool = False

    @property
    def floor(self) -> int:
        return self.head + self.tail

    @property
    def full(self) -> int:
        return self.floor + sum(h + sum(ls) for h, ls in zip(self.headings, self.lines))


def _cost(entry: _Entry, estimate: Callable[[str], int], pinned: bool) -> _Costed:
    def block(lines: Sequence[str]) -> int:
        return estimate("\n".join(lines) + "\n") if lines else 0

    return _Costed(
        entry,
        head=block(entry.head),
        tail=block(["", *entry.tail]) if entry.tail else 0,
        headings=[block(["", *s.heading]) for s in entry.sections],
        lines=[[estimate(line + "\n") for line in s.lines] for s in entry.sections],
        pinned=pinned,
    )


def _fill(costed: _Costed, allowance: int) -> tuple[str, dict[str, int] | None]:
    """The entry's text within ``allowance``: whole if it fits; else its
    head, then the sections in order line by line until a line does not fit
    (it and everything after it stay out), then its metadata. Returns
    ``(text, kept)``; ``kept`` is None when the entry went in whole."""

    if costed.full <= allowance:
        return costed.entry.text, None
    entry = costed.entry
    used = costed.floor
    out = list(entry.head)
    kept: dict[str, int] = {}
    for section, heading_cost, line_costs in zip(entry.sections, costed.headings, costed.lines):
        if not section.lines:
            continue
        if used + heading_cost + line_costs[0] > allowance:
            break
        used += heading_cost
        body: list[str] = []
        for line, cost in zip(section.lines, line_costs):
            if used + cost > allowance:
                break
            used += cost
            body.append(line)
        out += ["", *section.heading, *body]
        kept[section.name] = len(body)
        if len(body) < len(section.lines):
            break
    if entry.tail:
        out += ["", *entry.tail]
    return "\n".join(out), kept


def _water_level(costed: Sequence[_Costed], caps: Sequence[int], budget: int) -> int | None:
    """The largest C with sum(max(floor, min(cap, C))) <= budget; None when
    everything fits whole and no level is needed."""

    if sum(caps) <= budget:
        return None
    low, high = 0, max(caps)
    while low < high:
        mid = (low + high + 1) // 2
        if sum(max(c.floor, min(cap, mid)) for c, cap in zip(costed, caps)) <= budget:
            low = mid
        else:
            high = mid - 1
    return low


def _cut_notice(
    cut: Sequence[tuple[_Costed, dict[str, int]]],
    dropped: Sequence[str],
    *,
    limit: int = NOTICE_RESERVE_TOKENS,
    estimate: Callable[[str], int] = len,
) -> str:
    """What the budget left out, within ``limit``: as many cut entries as fit,
    each with up to three of its sections, then "N more"; dropped entries by
    name as room allows."""

    def describe(costed: _Costed, kept: dict[str, int]) -> str:
        if not kept:
            return f"{costed.entry.label[:_NOTICE_LABEL_CHARS]}：只注入了条目头"
        missing: list[str] = []
        for section in costed.entry.sections:
            if not section.lines:
                continue
            got = kept.get(section.name, 0)
            name = section.name[:_NOTICE_LABEL_CHARS]
            if got == 0:
                missing.append(f"「{name}」整节未注入")
            elif got < len(section.lines):
                missing.append(f"「{name}」节后 {len(section.lines) - got} 行")
        more = f" 等 {len(missing)} 处" if len(missing) > 3 else ""
        return f"{costed.entry.label[:_NOTICE_LABEL_CHARS]}：" + "、".join(missing[:3]) + more

    def assemble(described: list[str], cut_more: int, shown_dropped: list[str]) -> str:
        parts: list[str] = []
        if described or cut_more:
            body = "；".join(described) + (f"；另有 {cut_more} 条" if described and cut_more else "")
            parts.append("以下条目未完整注入——" + body if described else f"另有 {cut_more} 个条目未完整注入")
        if dropped:
            names = "、".join(shown_dropped)
            rest = len(dropped) - len(shown_dropped)
            parts.append(
                f"以下条目被整体丢弃：{names}" + (f" 等 {len(dropped)} 项" if rest else "")
                if shown_dropped
                else f"{len(dropped)} 个条目被整体丢弃"
            )
        return "（注入预算说明：" + "。".join(parts) + "。）" if parts else ""

    described: list[str] = []
    for costed, kept in cut:
        trial = assemble([*described, describe(costed, kept)], len(cut) - len(described) - 1, [])
        if estimate(trial) > limit:
            break
        described.append(describe(costed, kept))
    shown: list[str] = []
    for label in dropped[:_NOTICE_LABEL_LIMIT]:
        trial = assemble(described, len(cut) - len(described), [*shown, label[:_NOTICE_LABEL_CHARS]])
        if estimate(trial) > limit:
            break
        shown.append(label[:_NOTICE_LABEL_CHARS])
    return assemble(described, len(cut) - len(described), shown)


def entry_injection_cap(_label: str = "") -> int | None:
    """The cap for entries injected BY KEY -- the matchable categories, which
    share one value today. The smallest of theirs, so no category is served
    more than its own cap if they ever differ."""

    from .knowledge.node.presets import MATCHABLE_CATEGORIES, preset_for_category

    caps = [preset_for_category(c).max_entry_tokens for c in MATCHABLE_CATEGORIES]
    known = [cap for cap in caps if cap]
    return min(known) if known else None


def render_knowledge_entries_block(
    entry_details: Mapping[str, str],
    *,
    count_tokens: Callable[[str], int],
    heuristic_count: Callable[[str], int] | None = None,
    block_limit: int,
    entry_limit: int | None | Callable[[str], int | None] = entry_injection_cap,
    pinned: Collection[str] = (),
) -> RenderedBlock:
    """Render knowledge entries (``label -> body``, in priority order).

    * Each entry is first capped at ``entry_limit`` (its category's
      ``max_entry_tokens``; a callable is asked per label).
    * ``pinned`` labels (the knowledge update's own style entries, entries it
      is asked to compress) are served first, each whole up to its cap or not
      at all, in priority order, together within half the block; one that
      does not fit joins the shared rest and is cut there.
    * The rest share what is left longest-first: a water level C such that
      every entry gets min(its length, C), but never less than its head and
      metadata. When the heads alone do not fit, the lowest-priority entries
      are dropped whole.
    * Entries are cut by whole lines (`_fill`); the trailing notice names
      which sections of which entries lost how many lines.

    Invariant: the rendered block never exceeds ``block_limit``.
    """

    use_local_exact = heuristic_count is None and local_counter_available_for(count_tokens)
    estimate = count_tokens if use_local_exact else (heuristic_count or HeuristicTokenCounter().count_text)
    cap_of = entry_limit if callable(entry_limit) else (lambda _label: entry_limit)
    pinned_set = set(pinned)
    entries = [
        _cost(_split_entry(label, body), estimate, label in pinned_set)
        for label, body in entry_details.items()
        if (body or "").strip()
    ]
    if not entries:
        return EMPTY_BLOCK
    if block_limit <= 0:
        dropped = tuple(c.entry.label for c in entries)
        notice = _cut_notice((), dropped, estimate=estimate)
        return RenderedBlock(text=notice, tokens=estimate(notice), dropped=dropped)

    def capped(c: _Costed) -> int:
        cap = cap_of(c.entry.label)
        return c.full if cap is None else max(c.floor, min(c.full, int(cap)))

    joiner = estimate(SECTION_JOINER)

    def allocate(budget: int) -> tuple[dict[str, int], list[str]]:
        allowance: dict[str, int] = {}
        dropped_now: list[str] = []
        pins = [c for c in entries if c.pinned]
        rest = [c for c in entries if not c.pinned]
        # Pinned entries go in WHOLE (up to their cap) or not at all, in
        # priority order, together within half the block: a pinned entry is
        # one the reader must see entire (the update's own style; an entry it
        # is asked to compress). One that does not fit joins the shared rest,
        # ahead of it, and is cut like any other there.
        pin_budget = budget // 2 if rest else budget
        used = 0
        bumped: list[_Costed] = []
        for c in pins:
            want = capped(c) + joiner
            if used + want <= pin_budget:
                allowance[c.entry.label] = capped(c)
                used += want
            else:
                bumped.append(c)
        rest = bumped + rest
        remaining = budget - used
        while rest:
            caps = [capped(c) for c in rest]
            room = remaining - joiner * len(rest)
            if sum(c.floor for c in rest) > room:
                dropped_now.append(rest.pop().entry.label)
                continue
            # No MIN_PARTIAL floor here: an entry never gets less than its
            # head, which is already a useful unit (its name, intro, aliases).
            level = _water_level(rest, caps, room)
            for c, cap in zip(rest, caps):
                allowance[c.entry.label] = cap if level is None else max(c.floor, min(cap, level))
            break
        return allowance, dropped_now

    budget = block_limit - min(NOTICE_RESERVE_TOKENS, block_limit // 3)
    for _attempt in range(8):
        allowance, dropped = allocate(budget)
        parts: list[str] = []
        included: list[str] = []
        truncated: list[str] = []
        kept_map: dict[str, dict[str, int]] = {}
        causes: dict[str, str] = {}
        cut: list[tuple[_Costed, dict[str, int]]] = []
        for c in entries:
            label = c.entry.label
            if label not in allowance:
                continue
            text, kept = _fill(c, allowance[label])
            parts.append(text)
            if kept is None:
                included.append(label)
                continue
            truncated.append(label)
            kept_map[label] = kept
            causes[label] = "cap" if allowance[label] >= capped(c) else "block"
            cut.append((c, kept))
        notice = _cut_notice(cut, dropped, estimate=estimate)
        text = SECTION_JOINER.join(parts)
        if notice:
            text = f"{text}\n{notice}" if text else notice
        # Counted with what allocated it. Without the local exact counter
        # that is the heuristic UPPER bound, additive over lines, so a fit by
        # it is a fit -- and the caller's counter would be a countTokens round
        # trip per block. With the exact counter the per-line sum can run a
        # little low against the joined text: give the overshoot back.
        tokens = estimate(text) if text else 0
        if tokens <= block_limit:
            break
        budget -= tokens - block_limit + 50
    return RenderedBlock(
        text=text,
        tokens=tokens,
        included=tuple(included),
        truncated=tuple(truncated),
        dropped=tuple(dropped),
        kept=kept_map,
        causes=causes,
    )


def fit_entry_text(
    label: str, body: str, limit: int, count_tokens: Callable[[str], int]
) -> tuple[str, str]:
    """One entry cut by lines to ``limit`` (the style block's use), plus the
    one-line note saying what was left out ("" when it fit)."""

    block = render_knowledge_entries_block(
        {label: body}, count_tokens=count_tokens, heuristic_count=count_tokens,
        block_limit=limit + NOTICE_RESERVE_TOKENS, entry_limit=limit,
    )
    if label in block.included:
        return body.strip(), ""
    if label in block.truncated:
        text, _, notice = block.text.rpartition("\n")
        return text, notice
    return "", block.text  # even its head does not fit: only the note


def retain_injected_handles(handles, text: str) -> None:  # HandleMap; untyped to keep the import edge one-way
    """Unbind the node handles of lines the budget cut: a handle the model
    never saw must not resolve (plan kb-entry-size, rule 4)."""

    present = set(_HANDLE_RE.findall(text))
    for handle in [h for h in handles.nodes if h not in present]:
        ident, _rev = handles.nodes.pop(handle)
        handles._reverse.pop(f"k:{ident}", None)
