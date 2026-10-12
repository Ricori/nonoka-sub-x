"""Fragment selection and assembly for correction / fast-mode prompts.

Prompt text lives in ``prompt_templates/*.md``; this module only decides which
fragments a given :class:`TranslationProfile` gets and stitches them into the
correction skeletons. Fragment inventory and the slot selection table are
documented in ``docs/llm_prompts.md``.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from pathlib import Path
from string import Template
from typing import Callable, Dict, Mapping, Sequence, Tuple

from .chunking import ASR_RESULT_CSV_HEADER
from .routing.config import CapabilityTier, KB_TRANSFER_MAX_ENTRIES
from .output_protocol import OUTPUT_CSV_HEADER, OUTPUT_CSV_HEADER_WITH_START
from .example_builder import build_examples
from .prompt_constants import threshold_params
from .prompt_variants import resolve_variant
from .routing.profiles import DEFAULT_PROFILE, TranslationProfile

PROMPT_VERSION = "zh-subtitle-correction-csv-v93"

# v17: every session must OPEN with a visible <reasoning> block (soft
# requirement: a missing block never fails validation or retries; parsers
# ignore its content).
#
# The depth tiering behind it is gone (owner decision 2026-08-12). What this
# clause is *for* is the case where the model's own thinking does not happen or
# degrades -- non-thinking models (nearly extinct) and the flash-lite family
# skipping the thinking phase -- so explicit visible reasoning carries the
# quality. That purpose does not scale with the thinking knob; if anything it
# is inverse, and the old (difficulty, retrieval) tiering encoded a *content*
# rationale ("no context pack -> reason harder") that was never measured.
# One neutral wording, always on, is both honest and one less uncalibrated
# axis. Rewording it to follow *what was actually injected* is a prompt-
# iteration item -- see docs/llm_followups.md.


# Soft requirement shared by every session (owner request 2026-08-12). The
# visible block exists to carry the reasoning when the model's own thinking
# does not happen or degrades -- when it *did* happen, re-deriving it in the
# open is pure duplicated output. Stated as "no need to" rather than a
# prohibition: a model without native thinking must still reason here.
_THINKING_NOTE = (
    "若你已在内部思考（thinking）中推演过，块内不必重复推演，写梳理后的结论即可；"
)


def reasoning_clause(*, bounded: bool = False) -> str:
    """The mandatory opening-<reasoning> clause.

    ``bounded`` swaps in the BASIC-tier wording: weak models that write an
    open-ended plan first tend to substitute the plan for the work (round-46:
    a thorough reasoning block followed by placeholder singles), so the block
    is hard-capped and forbidden from rehearsing output lines. That one stays
    conditional because it guards a *measured* failure mode, not a depth tier.
    """

    if bounded:
        return (
            "回复必须以有且仅有一个 `<reasoning>...</reasoning>` 块开头：只写不超过 "
            "8 行要点（专名决定、高风险区间、分组难点），禁止逐行预演输出、复述流程"
            "或写任何字幕行草稿——推理的产出只能是接下来完整写出的字幕行本身；"
            f"{_THINKING_NOTE}写完要点后立即开始输出规定的标签块。"
        )
    return (
        "回复必须以有且仅有一个 `<reasoning>...</reasoning>` 块开头：在其中梳理本次"
        "输入的疑点、候选写法与取舍依据，想清楚再落笔；"
        f"{_THINKING_NOTE}随后再输出规定的标签块。"
    )


PROMPT_TEMPLATE_DIR = Path(__file__).resolve().parent / "prompt_templates"


def render_knowledge_indexes(indexes: Mapping[str, str] | None) -> str:
    """The ``<{category}_index>`` blocks every knowledge-bearing prompt carries.

    One rendered block instead of the ``streamer_index`` / ``common_index``
    pair the templates used to name. Four templates spelled that pair out by
    hand, so adding a category meant editing prompt TEXT -- which is why a
    category could never be added from data alone
    (2026-09-07; the wiring now: `docs/knowledge.md`, 「加一个类别就是两步」).

    The tag names stay ``<{category}_index>``: `exchange_metadata` counts each
    block's tokens by extracting it from the rendered user text, and that keeps
    working per category.

    Empty means ``（空）``, decided here rather than at each call site. Five
    sites wrote ``or "（空）"`` and one (fast round 1) passed the empty string
    through -- a divergence nobody chose.
    """

    from .knowledge.node.presets import MATCHABLE_CATEGORIES

    supplied = indexes or {}
    blocks = []
    for category in MATCHABLE_CATEGORIES:
        body = str(supplied.get(category) or "").strip() or "（空）"
        blocks.append(f"<{category}_index>\n{body}\n</{category}_index>")
    return "\n".join(blocks)


def render_knowledge_ops_contract(allow_categories: Sequence[str], *, split: bool = False) -> str:
    """The proposal grammar, with its ``category`` enum bound to the grant.

    Every caller of ``fragment_knowledge_output_v1`` also hands an
    ``allow_categories`` to ``translate_model_proposals``; rendering the enum
    from that same tuple is what stops the prompt and the gate from
    disagreeing. They did: the template named `streamer|common` outright, so a
    new category reached the store, the index and the structure table and was
    still declared illegal three lines later -- and the two repair sessions,
    which have always applied with every category, were reading an enum that
    named two of them (review 2026-09-07, second round).
    """

    from .knowledge.node.render import render_writable_categories

    names = tuple(allow_categories)
    # `split_entry` is offered only when an entry in front of the model is
    # asked to compress and may split (plan kb-entry-size): the op is one
    # line of JSON and compressing is many, so a standing offer would win
    # over the compression the notice asks for first.
    return load_prompt_template(
        "fragment_knowledge_output_v1.md",
        strict=True,
        split_op_row=load_prompt_template("fragment_knowledge_split_op_row_v1.md") if split else "",
        split_op_note=load_prompt_template("fragment_knowledge_split_op_note_v1.md") if split else "",
        reasoning_clause=reasoning_clause(),
        term_line_rules=render_term_line_rules(),
        writable_categories=render_writable_categories(names),
        # The "do not concatenate two enums" counter-example has to name a real
        # category to land, and a hand-written one is the same stale copy in
        # miniature.
        concat_counterexample=f"{names[0] if names else 'common'}|游戏",
    ).strip()


def render_term_line_rules() -> str:
    """The four-column term-line rules, shared by the full proposal grammar
    and the share conflict repair's own short op section (cold read Q7: that
    session may only rewrite existing lines, so it does not get the grammar)."""

    return load_prompt_template("fragment_knowledge_term_lines_v1.md").strip()


def load_prompt_template(name: str, *, strict: bool = False, **values: object) -> str:
    """Render one template file with ``safe_substitute``.

    ``strict`` checks placeholder coverage at the TEMPLATE layer -- every
    identifier the template declares must be supplied -- and never by scanning
    the rendered text: injected material (knowledge entries, user-supplied
    repair text) may legally contain ``$WORD``. Knowledge-session assembly
    always passes strict; a missed parameter raises instead of shipping a
    literal ``$reasoning_clause`` to the model."""

    path = PROMPT_TEMPLATE_DIR / name
    template = Template(path.read_text(encoding="utf-8"))
    defaults = {"prompt_version": PROMPT_VERSION}
    defaults.update({key: str(value) for key, value in values.items()})
    if strict:
        missing = sorted(set(template.get_identifiers()) - set(defaults))
        if missing:
            raise PromptAssemblyError(
                f"prompt template {name} rendered without values for: "
                + ", ".join(missing)
            )
    return template.safe_substitute(defaults)


# Phrase-level parameters, one layer per switch axis (docs/llm_prompts.md). Structural
# differences stay whole fragment files ("变体优先做整文件"). Every key belongs
# to exactly one layer; phrases that used to weld several axes together --
# `verify_basis` said "音频、背景资料和搜索结果" regardless of whether audio or
# search results were actually coming -- are now composed from per-layer pieces
# by `_modal_params`, so the wording can only name inputs that really arrive.
_MEDIA_PARAMS: Dict[str, str] = {
    "csv_time_col_name": "剪辑内开始时间",
    "csv_time_note": "`剪辑内开始时间` 以你收到的剪辑音频的 0 秒为基准，可直接用来在剪辑中定位对应语音。",
    "merge_connect_basis": "音频和语义",
    "correction_basis": "重听",
    "speaker_basis": "音视频/语境",
    "insert_type_clause": "；不要输出其他 type 值",
    "insert_position_clause": "",
    "insert_duration_clause": "",
    "insert_note_clause": "",
    "discard_insert_clause": (
        "\n5. 丢弃取舍：打算以 discard|<源序号> 丢弃之前，先重听该区间音频——"
        "能辨识出实词或短语的，应修正后保留，而不是整体丢弃；只有音频本身"
        "也没有语义内容（纯哭声、喘息、感叹）时才写 discard 行丢弃。"
    ),
    "noisy_span_handling": (
        "重听该区间逐句处理：能听出实词、短语的，按听到的内容重写并按停顿拆条；"
        "确认只是喊叫、喘息等无语义感叹的，以 discard|<源序号> 显式丢弃"
    ),
    "preceding_audibility_note": (
        "最贴近窗口的前文行可能落在剪辑开头几秒内、其语音可听，但它们同样是只读背景，"
        "不要为其输出字幕。"
    ),
    "paren_rule": (
        "括注采用正证据门槛：只有音频中能独立确认存在实际的非语音声响（有声响、没有"
        "可转写话语）时，才可写极简中性括注（如「（提示音）」），不夹带解释；若音频中"
        "没有对应声响或无法确认声响性质，不得把文本改写成括注。可听见的系统语音仍按话语处理。"
    ),
}
_TEXT_MEDIA_PARAMS: Dict[str, str] = {
    "csv_time_col_name": "窗口内开始时间",
    "csv_time_note": "`窗口内开始时间` 以本窗口第一条字幕为 0 秒基准，仅用于把握说话节奏与间隔。",
    "merge_connect_basis": "语义与时间间隔",
    "correction_basis": "音近与上下文推断",
    "speaker_basis": "语境/文本线索",
    "insert_type_clause": "；不要输出其他 type 值",
    "insert_position_clause": "",
    "insert_duration_clause": "",
    "insert_note_clause": "",
    "discard_insert_clause": "",
    "noisy_span_handling": (
        "在无法核听的前提下逐条取舍：有语义的照常纠错翻译，确认无语义的重复/感叹以"
        " discard|<源序号> 显式丢弃；证据不足时保留原文并标低可信度，绝不能凭上下文编造或「还原」台词"
    ),
    "preceding_audibility_note": "",
    "paren_rule": (
        "本次没有音频，禁止把 ASR 文本改写成非语音声响括注；确信为幻觉就写 discard|<源序号> 丢弃，"
        "不确信就保留纠错并在 note 标记「疑似幻觉」。系统语音等可转写文本仍按话语处理。"
    ),
}


# media=video is its own layer on top of audio, not a role addendum bolted onto
# an otherwise audio-worded prompt (docs/llm_prompts.md): once frames are attached, the
# phrases that enumerate what to judge from have to name them.
_VIDEO_PARAM_OVERRIDES: Dict[str, str] = {
    "merge_connect_basis": "音画和语义",
    "noisy_span_handling": (
        "重听该区间音频、必要时看画面逐句处理：能听出实词、短语的，按听到的内容重写"
        "并按停顿拆条；确认只是喊叫、喘息等无语义感叹的，以 discard|<源序号> 显式丢弃"
    ),
}


# The retrieval layer contributes only the material it actually injects.
_VERIFY_BASIS_MEDIA = {"text": "上下文", "audio": "音频", "video": "音画"}
_VERIFY_BASIS_RETRIEVAL = {
    "local": ["背景资料", "搜索结果"],
    "native": ["你自己检索到的资料"],
    "none": [],
}
# What the correction window is actually handed, named per axis. The role
# fragments used to write "背景资料" into their input list unconditionally --
# true only under retrieval=local (docs/llm_prompts.md, injection reality).
_INPUT_INVENTORY_MEDIA = {
    "text": "ASR 源字幕片段",
    "audio": "音频、ASR 源字幕片段",
    "video": "音频与视频画面、ASR 源字幕片段",
}


def _join_cn(parts: list[str]) -> str:
    if len(parts) == 1:
        return parts[0]
    return "、".join(parts[:-1]) + "和" + parts[-1]


def _verify_basis(profile: TranslationProfile) -> str:
    """"Cross-check against ..." -- named from what this call really receives.

    Composed rather than written out per combination: a profile with no
    retrieval must not be told to cross-check against search results it will
    never see (injection reality, plan §5.2). These helpers phrase the
    *correction window's* prompt, so the media layer reads
    ``correction_media`` (model-routing v2).
    """

    return _join_cn(
        [
            _VERIFY_BASIS_MEDIA[profile.correction_media],
            *_VERIFY_BASIS_RETRIEVAL[profile.retrieval],
        ]
    )


def _input_inventory(profile: TranslationProfile, *, knowledge_enabled: bool) -> str:
    """The "you will be given ..." list in the role fragments."""

    parts = [_INPUT_INVENTORY_MEDIA[profile.correction_media]]
    if profile.retrieval != "none":
        parts.append("背景资料")
    if knowledge_enabled:
        parts.append("知识库词条")
    if profile.continuity == "serial":
        parts.append("此前窗口的累积建议台账")
    parts.append("本窗只读前文 ASR")
    return _join_cn(parts)


def _background_sources(
    profile: TranslationProfile, *, knowledge_enabled: bool
) -> str:
    """The injected material the "trust your ears" rules argue against."""

    return _join_cn(
        [
            *(["背景资料"] if profile.retrieval != "none" else []),
            *(["知识库词条"] if knowledge_enabled else []),
        ]
        or ["背景资料"]
    )


def _modal_params(
    profile: TranslationProfile, *, knowledge_enabled: bool = True
) -> Dict[str, str]:
    """Merge the per-axis layers into the substitution map."""

    params = dict(
        _MEDIA_PARAMS if profile.correction_use_audio else _TEXT_MEDIA_PARAMS
    )
    if profile.correction_use_video:
        params.update(_VIDEO_PARAM_OVERRIDES)
    params["verify_basis"] = _verify_basis(profile)
    params["input_inventory"] = _input_inventory(
        profile, knowledge_enabled=knowledge_enabled
    )
    return params


def _collapse_blank_lines(text: str) -> str:
    return re.sub(r"\n{3,}", "\n\n", text).strip() + "\n"


class PromptAssemblyError(RuntimeError):
    """A rendered prompt still carries an unresolved placeholder."""


# ``$``-substitutions go through ``Template.safe_substitute``, which leaves an
# unknown key in place instead of raising. That is what lets a renamed
# parameter ship a literal "$verify_basis" to the model, so the finished text
# is checked here (docs/llm_prompts.md).
_UNRESOLVED_TOKEN = re.compile(r"\$(?:\{\w+\}|[A-Za-z_]\w*)")


def assert_fully_substituted(text: str, *, what: str) -> str:
    leftovers = sorted(set(_UNRESOLVED_TOKEN.findall(text or "")))
    if leftovers:
        raise PromptAssemblyError(
            f"{what} still contains unresolved placeholders: " + ", ".join(leftovers)
        )
    return text


# --- Slot registry -----------------------------------------------------------
# Which fragment fills a switch-driven slot is a table, not a chain of ifs
# (docs/llm_prompts.md). Each rule carries the blocks the fragment assumes will be
# injected alongside it (``requires``); rendering asserts they really are, which
# is the injection-reality principle made structural rather than reviewed.


@dataclass(frozen=True)
class PromptContext:
    """Everything a slot predicate is allowed to look at."""

    profile: TranslationProfile
    knowledge_enabled: bool = True

    @property
    def can_request(self) -> bool:
        """A dropped entry can be asked for again in a later window
        (`task_graph.window_requests`): harness search, or a readable base --
        ``knowledge_enabled`` here already means the index is readable."""

        return self.profile.external_injection or self.knowledge_enabled

    @property
    def injected_blocks(self) -> frozenset[str]:
        """The input blocks this call will really carry."""

        blocks = {"asr_result", "preceding_context"}
        if self.profile.continuity == "serial":
            # Parallel dispatch retires the chained advice ledger; nothing is
            # accumulated across windows to inject (plan A.3/A.7).
            blocks.add("next_advice_ledger")
        if self.profile.correction_use_audio:
            blocks.add("audio")
        if self.profile.correction_use_video:
            blocks.add("video")
        if self.profile.external_injection:
            blocks.add("search_results")
        if self.can_request:
            blocks.add("window_notes")
        if self.profile.retrieval != "none":
            blocks.add("context_pack")
        if self.knowledge_enabled:
            blocks.add("entry_details")
        return frozenset(blocks)


@dataclass(frozen=True)
class SlotRule:
    """One candidate fragment for a slot, with its guard and its assumptions."""

    when: Callable[[PromptContext], bool]
    fragment: str = ""
    requires: Tuple[str, ...] = ()


class SlotRegistry:
    """Slot -> ordered rules; the first matching rule wins."""

    def __init__(self, rules: Mapping[str, Sequence[SlotRule]]) -> None:
        self._rules = {slot: tuple(items) for slot, items in rules.items()}

    def resolve(self, slot: str, context: PromptContext) -> SlotRule:
        for rule in self._rules[slot]:
            if rule.when(context):
                missing = [
                    block
                    for block in rule.requires
                    if block not in context.injected_blocks
                ]
                if missing:
                    raise PromptAssemblyError(
                        f"slot {slot!r} selected {rule.fragment or '(empty)'}, which "
                        f"assumes {', '.join(missing)} will be injected, but this "
                        f"context injects only {sorted(context.injected_blocks)}"
                    )
                return rule
        raise PromptAssemblyError(f"no rule matched slot {slot!r}")

    def slots(self) -> Tuple[str, ...]:
        return tuple(self._rules)


_ALWAYS: Callable[[PromptContext], bool] = lambda _context: True

CORRECTION_SLOTS = SlotRegistry(
    {
        # The retrieval-usage note ships only where its block can arrive. The
        # text-med special case (b2b7feb) documented an input that never showed
        # up; `requires` is what now makes that shape impossible to reintroduce.
        "retrieval": (
            SlotRule(
                when=lambda c: c.profile.external_injection,
                fragment="fragment_retrieval_injected_v1.md",
                requires=("search_results",),
            ),
            SlotRule(
                when=lambda c: c.profile.native_search,
                fragment="fragment_native_search_v1.md",
            ),
            SlotRule(when=_ALWAYS),
        ),
        # Effort wording is for profiles that work without injected material;
        # efficiency gets the cheap variant of it.
        "effort": (
            SlotRule(when=lambda c: c.profile.external_injection),
            SlotRule(
                when=lambda c: c.profile.difficulty == "efficiency",
                fragment="fragment_effort_low_v1.md",
            ),
            SlotRule(when=_ALWAYS, fragment="fragment_effort_deep_v1.md"),
        ),
        # Pruning is asked for only where it is honoured: something must be
        # injected to prune, and a later round must be able to ask it back.
        "keep_entries": (
            SlotRule(
                when=lambda c: c.knowledge_enabled
                and c.can_request
                and c.profile.continuity == "serial",
                fragment="fragment_keep_entries_v1.md",
                requires=("entry_details",),
            ),
            SlotRule(when=_ALWAYS),
        ),
        # The advice ledger is the serial chain's continuity; a parallel run
        # accumulates nothing across windows, so neither the ledger input nor
        # the <next_advice> output may be described (plan A.7).
        "advice": (
            SlotRule(
                when=lambda c: c.profile.continuity == "serial",
                fragment="fragment_advice_v1.md",
                requires=("next_advice_ledger",),
            ),
            SlotRule(when=_ALWAYS),
        ),
        "role": (
            SlotRule(
                when=lambda c: c.profile.correction_media == "text",
                fragment="fragment_corr_role_text_v1.md",
            ),
            SlotRule(
                when=_ALWAYS,
                fragment="fragment_corr_role_audio_v1.md",
                requires=("audio",),
            ),
        ),
        "goals_correction": (
            SlotRule(
                when=lambda c: c.profile.correction_media == "text",
                fragment="fragment_goals_correction_text_v1.md",
            ),
            SlotRule(
                when=_ALWAYS,
                fragment="fragment_goals_correction_audio_v1.md",
                requires=("audio",),
            ),
        ),
        "user_reminders": (
            SlotRule(
                when=lambda c: c.profile.correction_media == "text",
                fragment="fragment_user_reminders_text_v1.md",
            ),
            SlotRule(
                when=_ALWAYS,
                fragment="fragment_user_reminders_audio_v1.md",
                requires=("audio",),
            ),
        ),
        "video_role_addendum": (
            SlotRule(
                when=lambda c: c.profile.correction_use_video,
                fragment="fragment_corr_role_video_v1.md",
                requires=("video",),
            ),
            SlotRule(when=_ALWAYS),
        ),
        # Audio's "trust your ears over the injected material" rules only make
        # sense where injected material exists to conflict with.
        "background_conflict": (
            SlotRule(
                when=lambda c: c.profile.correction_use_audio
                and (c.profile.retrieval != "none" or c.knowledge_enabled),
                fragment="fragment_background_conflict_v1.md",
                requires=("audio",),
            ),
            SlotRule(when=_ALWAYS),
        ),
    }
)


def render_slot(
    slot: str, context: PromptContext, **values: object
) -> str:
    """Resolve a slot for this context and render its fragment ("" if none)."""

    rule = CORRECTION_SLOTS.resolve(slot, context)
    if not rule.fragment:
        return ""
    return load_prompt_template(rule.fragment, **values).strip()


REPAIR_ROUND_FRAGMENT = "fragment_repair_round_v1.md"


def compose_repair_turns(
    previous_output: str,
    validation_errors: Sequence[str],
) -> list[Dict[str, str]]:
    """The two turns that turn a blind retry into a repair round.

    Session-agnostic on purpose: it names the output contract, not this or that
    stage's blocks, so any stage whose output is validated can hand its own
    errors back. The previous answer goes in as an assistant turn rather than
    quoted inside the user turn -- that is what makes it "what you wrote" to
    the model, and it costs nothing to re-quote.

    Returns ``[]`` unless there is both an output to repair and something to
    say about it; a caller can therefore pass whatever it has.
    """

    errors = [
        text for text in (str(item).strip() for item in validation_errors) if text
    ]
    if not previous_output.strip() or not errors:
        return []
    return [
        {"role": "assistant", "content": previous_output},
        {
            "role": "user",
            "content": load_prompt_template(
                REPAIR_ROUND_FRAGMENT,
                validation_errors="\n".join(f"- {text}" for text in errors),
            ).strip(),
        },
    ]


def ensure_csv_block_headers(
    text: str,
    *,
    output_header: str = OUTPUT_CSV_HEADER,
    include_output_blocks: bool = True,
) -> str:
    """Insert canonical headers into line-anchored live CSV input blocks."""

    rendered = text or ""
    blocks = [("asr_result", ASR_RESULT_CSV_HEADER)]
    if include_output_blocks:
        blocks.extend((("singles", output_header), ("translated", output_header)))
    for tag, header in blocks:
        pattern = re.compile(
            rf"(?m)(^<{tag}>[ \t]*\r?\n)(?!{re.escape(header)}[ \t]*\r?$)"
        )
        rendered = pattern.sub(rf"\1{header}\n", rendered)
    return rendered


def _search_results_usage() -> str:
    return load_prompt_template("fragment_search_results_usage_v1.md").strip()


def compose_correction_system(
    profile: TranslationProfile = DEFAULT_PROFILE,
    *,
    tier: CapabilityTier = CapabilityTier.CAPABLE,
    variant: str | None = None,
    extra_style: str = "",
    style_block: str = "",
    knowledge_enabled: bool = True,
) -> str:
    """Assemble the correction system prompt for a profile.

    The ``<keep_entries>`` block only appears when the window's pruning
    decision is actually honoured: with no knowledge base there is nothing to
    prune, and with no per-window query round the harness force-transfers the
    whole set instead, because a dropped entry could never be requested back
    (docs/llm_harness_behavior.md).

    The prompt *set* comes from a :class:`CorrectionVariant`. ``tier`` classifies
    the answering endpoint and picks the default variant, after the profile's
    difficulty ceiling caps it (``intermediate``/``efficiency`` force BASIC even on a strong
    endpoint); pass ``variant`` to override by name (capableB/C or basicA/B). The variant bundles the merge fragments, the
    reasoning-bounded flag, and the contract clauses that used to be tier
    ternaries; the tier-independent translated discipline lives in
    ``fragment_translated_common_v1.md``, prepended to every variant.
    """

    v = resolve_variant(variant, tier)
    params = dict(_modal_params(profile, knowledge_enabled=knowledge_enabled))
    context = PromptContext(
        profile=profile,
        knowledge_enabled=knowledge_enabled,
    )
    role_block = render_slot(
        "role",
        context,
        input_inventory=params["input_inventory"],
        video_role_addendum=render_slot("video_role_addendum", context),
    )
    goals_correction = render_slot(
        "goals_correction",
        context,
        verify_basis=params["verify_basis"],
        background_conflict_block=render_slot(
            "background_conflict",
            context,
            background_sources=_background_sources(
                profile, knowledge_enabled=knowledge_enabled
            ),
            knowledge_convention_clause=(
                load_prompt_template("fragment_background_knowledge_convention_v1.md").strip()
                if knowledge_enabled
                else ""
            ),
        ),
    )
    retrieval_block = render_slot(
        "retrieval",
        context,
        search_results_usage=_search_results_usage(),
    )
    effort_block = render_slot("effort", context)

    extra_style_block = (
        f"\n特殊翻译风格要求：\n{extra_style.strip()}\n" if extra_style.strip() else ""
    )
    style_block_text = (
        style_block.rstrip() + "\n" if style_block.strip() else ""
    )
    weighted_char_count_rule = load_prompt_template(
        "fragment_weighted_char_count_v1.md"
    ).strip()
    # Every worked example is generated from curated material
    # (docs/llm_prompts.md); the start-bearing variants get their start
    # column from the row data, which is what retired the old text-splicing
    # post-pass.
    examples_block = build_examples(
        variant=v.name,
        with_start=v.output_has_start,
        with_comments=v.name == "capableC",
        profile=profile,
        params={
            **params,
            "output_column_count": str(
                len(
                    (
                        OUTPUT_CSV_HEADER_WITH_START
                        if v.output_has_start
                        else OUTPUT_CSV_HEADER
                    ).split("|")
                )
            ),
        },
    )
    # Variant-independent translated discipline + variant-selected merge
    # strategy. The weighted char-count algorithm is injected only via the
    # output contract; the common fragment keeps just the column discipline.
    merge_block = (
        load_prompt_template("fragment_translated_common_v1.md").strip()
        + "\n\n"
        + load_prompt_template(
            v.merge_rules_fragment,
            speaker_basis=params["speaker_basis"],
            merge_connect_basis=params["merge_connect_basis"],
            approval_reason_slot=v.approval_reason_slot,
            # The merge thresholds live in prompt_constants only; fragments
            # reference them as ${thr_*} so a changed constant propagates.
            **threshold_params(),
        ).strip()
    )

    assembled = load_prompt_template(
        "correction_main_v1.md",
        role_block=role_block,
        goals_correction_block=goals_correction.strip(),
        goals_translation_block=load_prompt_template(
            "fragment_goals_translation_v1.md",
            paren_rule=params["paren_rule"],
            granule_record_clause=v.granule_record_clause,
        ).strip(),
        extra_style_block=extra_style_block,
        style_block=style_block_text,
        retrieval_block=retrieval_block,
        csv_input_block=load_prompt_template(
            "fragment_csv_input_v1.md",
            csv_time_col_name=params["csv_time_col_name"],
            csv_time_note=params["csv_time_note"],
        ).strip(),
        output_contract_block=load_prompt_template(
            v.output_contract_fragment,
            **threshold_params(),
            reasoning_clause=reasoning_clause(bounded=v.reasoning_bounded),
            insert_type_clause=params["insert_type_clause"],
            insert_position_clause=params["insert_position_clause"],
            insert_duration_clause=params["insert_duration_clause"],
            insert_note_clause=params["insert_note_clause"],
            weighted_char_count_rule=weighted_char_count_rule,
            output_csv_header=(
                OUTPUT_CSV_HEADER_WITH_START if v.output_has_start else OUTPUT_CSV_HEADER
            ),
            output_column_count=(10 if v.output_has_start else 9),
            output_start_clause=(
                "\n   - `start`：直接抄该行首源在 `<asr_result>` 中的 start；"
                "合并行抄首源 start；保留 1 位小数。"
                if v.output_has_start
                else ""
            ),
            translated_position_clause=v.translated_position_clause,
            translated_merge_rule=v.translated_merge_rule,
            pacing_merge_clause=v.pacing_merge_clause,
            singles_note_style=v.singles_note_style,
            note_gap_clause=v.note_gap_clause,
        ).strip(),
        advice_block=render_slot("advice", context),
        keep_block=render_slot(
            "keep_entries", context, max_keep_entries=KB_TRANSFER_MAX_ENTRIES
        ),
        alignment_block=load_prompt_template("fragment_alignment_v1.md").strip(),
        window_block=load_prompt_template(
            "fragment_window_overlap_v1.md",
            preceding_audibility_note=params["preceding_audibility_note"],
        ).strip(),
        merge_block=merge_block,
        hallucination_block=load_prompt_template(
            "fragment_hallucination_v1.md",
            discard_insert_clause=params["discard_insert_clause"],
        ).strip(),
        examples_block=examples_block,
        effort_block=effort_block,
    )
    return assert_fully_substituted(
        _collapse_blank_lines(assembled), what="correction system prompt"
    )


def compose_correction_user(
    profile: TranslationProfile = DEFAULT_PROFILE,
    *,
    general_context_json: str,
    window_context: str,
    entry_details: str,
    previous_advice: str,
    pre_round_notes: str,
    search_results: str,
    preceding_context_csv: str,
    current_asr_csv: str,
    current_asr_row_count: int,
    tier: CapabilityTier = CapabilityTier.CAPABLE,
    variant: str | None = None,
    knowledge_enabled: bool = True,
    overlap_candidates_block: str = "",
    style_pick_reminder: str = "",
) -> str:
    """``style_pick_reminder`` leads the first reminder item and the closing
    reminder when the window carries a style pick: the reminders otherwise
    describe a direct delivery, which a request-turn pick forbids."""

    v = resolve_variant(variant, tier)
    params = dict(_modal_params(profile))
    serial = profile.continuity == "serial"
    reminders_block = render_slot(
        "user_reminders",
        PromptContext(profile=profile),
        # The <next_advice> demand is the ledger's output half; it leaves with
        # the axis (plan A.7).
        next_advice_reminder_item=(
            load_prompt_template("fragment_next_advice_reminder_v1.md").strip()
            if serial
            else ""
        ),
    )
    output_header = (
        OUTPUT_CSV_HEADER_WITH_START if v.output_has_start else OUTPUT_CSV_HEADER
    )
    # Same predicates as the system-side slots: the ledger section and the
    # <next_advice>/<keep_entries> demands exist only where something reads
    # them back (docs/llm_harness_behavior.md).
    trailing_blocks_reminder = (
        load_prompt_template(
            "fragment_trailing_blocks_serial_v1.md",
            keep_entries_reminder=(
                "、`<keep_entries>`"
                if knowledge_enabled
                and PromptContext(profile=profile, knowledge_enabled=knowledge_enabled).can_request
                else ""
            ),
        ).strip()
        if serial
        else load_prompt_template("fragment_trailing_blocks_parallel_v1.md").strip()
    )
    rendered = load_prompt_template(
        v.user_template,
        general_context_json=general_context_json,
        window_context=window_context,
        entry_details=entry_details,
        advice_input_block=(
            load_prompt_template(
                "fragment_advice_input_v1.md",
                previous_advice=previous_advice,
            ).strip() + "\n\n"
            if serial
            else ""
        ),
        # Only session replay still fills the slot (the task flow's request
        # turn keeps its notes in its own reply): an empty slot is not shown.
        pre_round_notes_block=(
            load_prompt_template(
                "fragment_pre_round_notes_input_v1.md",
                verify_basis=params["verify_basis"],
                pre_round_notes=pre_round_notes.strip(),
            ).strip() + "\n\n"
            if pre_round_notes.strip()
            else ""
        ),
        search_results=search_results,
        reminder_tail=reminders_block,
        preceding_context_csv=preceding_context_csv,
        current_asr_csv=current_asr_csv,
        current_asr_row_count=current_asr_row_count,
        overlap_candidates_block=overlap_candidates_block,
        merge_reminder=v.merge_reminder,
        mid_reminder_merge_rule=v.mid_reminder_merge_rule,
        singles_note_reminder=v.singles_note_reminder,
        trailing_blocks_reminder=trailing_blocks_reminder,
        style_pick_reminder=style_pick_reminder,
        output_csv_header=output_header,
        output_column_count=len(output_header.split("|")),
    )
    return ensure_csv_block_headers(
        rendered,
        output_header=output_header,
        include_output_blocks=True,
    )
