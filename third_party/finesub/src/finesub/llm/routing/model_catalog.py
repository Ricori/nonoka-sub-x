"""Pipe-delimited model facts: one row per callable (provider, model).

The catalog is **the** place a model's facts live -- capabilities, limits, the
endpoint that serves it and the thinking dialect it speaks -- while
``model_routes.toml`` composes them into model groups, task groups and
presets. Facts vs composition, one file each (owner decision 2026-08-12).

Two files, same format, merged by ``fact_id`` with the later one winning
(:func:`load_merged_catalog`): the **packaged default** that ships with the
code, and an optional **override** in the data root. Installed front ends put
that override in ``user-data``; a source checkout has no separate user-data, so
it is simply the checkout root -- exactly the resolution ``.env`` and
``config.toml`` already use, and it stays in the paths layer rather than here.

``tpm`` and ``tpd`` are input-token limits only (output/thinking excluded).
``rpd``/``tpd`` are informational; runtime daily caps use ``.state`` exhaustion.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Iterable, List, Sequence


CATALOG_FILENAME = "model_catalog.psv"
# Columns a row must carry: everything else has a defensible default, and a
# short header is what makes a hand-written override file practical.
# ``max_input_tokens`` is required on purpose -- windows are planned against
# it, so an optimistic default explodes every window far from its cause.
REQUIRED_COLUMNS = (
    "fact_id",
    "provider_tier",
    "api_model_id",
    "max_input_tokens",
)
CATALOG_COLUMNS = (
    "fact_id",
    "provider_tier",
    "provider_kind",
    "base_url",
    "key_env",
    "display_name",
    "api_model_id",
    "max_input_tokens",
    "max_output_tokens",
    "context_window",
    "supports_audio",
    "supports_video",
    "video_high_resolution_only",
    "supports_native_search",
    "thinking",
    "token_scale",
    "rpm",
    "tpm",
    "rpd",
    "tpd",
    "is_free",
    "quality_score",
    "hint_output_ceiling",
    "fallback_model",
    "quota_pool",
)
#: `quota_pool` sentinel: this row draws on an allowance of its own.
#: Deliberately lower case, so it cannot be mistaken for a pool identifier --
#: every real one is written in caps. Same idea as `-1` for an unlimited rate.
PER_MODEL_QUOTA_POOL = "per-model"
#: Tiers where one CLI login covers several separately metered allowances, so
#: "the provider tier" is not an answer -- an empty `quota_pool` there is a
#: question nobody answered rather than a default worth guessing. Guessing
#: either way is wrong for some row on these tiers, and the expensive wrong
#: guess (merging) takes a working line out for hours with no call left to
#: thaw it, so they are made to say.
TIERS_METERED_PER_LINE = frozenset({"LOCAL_AGY", "LOCAL_CODEBUDDY"})
# Names an older override file may still carry. They are rejected with the new
# name rather than silently ignored: a dropped `api_model_id` column would only
# surface as "this model is not in the catalog", far from the header that caused
# it. No compatibility shim -- an override file is a handful of lines a person
# wrote, and the message says exactly what to type.
RETIRED_COLUMNS = {
    "litellm_model": "api_model_id",
    "model": "display_name",
}

# Blank cell defaults (owner decision 2026-08-12: "允许某些项留空，留空时取
# 默认值"). The conservative generic RPM default is 100; provider-specific
# packaged rows continue to declare their measured/published quotas explicitly.
GEMINI_KIND = "gemini"
LOCAL_AGENT_KIND = "local_agent"
# A worker somebody is already running. It gets a catalog row for the same
# reason every other target does -- the harness has to know what it can be
# asked to do (audio? video? search?) and whether it clears the cell's quality
# floor -- but nothing here ever launches it.
CONVERSATIONAL_KIND = "conversational_agent"
OPENAI_COMPAT_KIND = "openai_compat"
ANTHROPIC_KIND = "anthropic"
PROVIDER_KINDS = (
    GEMINI_KIND,
    LOCAL_AGENT_KIND,
    CONVERSATIONAL_KIND,
    OPENAI_COMPAT_KIND,
    ANTHROPIC_KIND,
)
# A row that names no dialect speaks the one most third-party endpoints do --
# unless its provider tier is one the harness ships a transport for, in which
# case overriding a packaged row (say, to re-tune a limit) must not force the
# user to restate the dialect.
DEFAULT_PROVIDER_KIND = OPENAI_COMPAT_KIND
PACKAGED_PROVIDER_KINDS = {
    "GEMINI_FREE": GEMINI_KIND,
    "GEMINI_PAID": GEMINI_KIND,
    "LOCAL_CODEX": LOCAL_AGENT_KIND,
    "LOCAL_CLAUDE": LOCAL_AGENT_KIND,
    "LOCAL_AGY": LOCAL_AGENT_KIND,
    "LOCAL_DSH": LOCAL_AGENT_KIND,
    "LOCAL_CODEBUDDY": LOCAL_AGENT_KIND,
    "LOCAL_CONVERSATIONAL": CONVERSATIONAL_KIND,
}
DEFAULT_MAX_OUTPUT_TOKENS = 65_536
DEFAULT_RPM = 100
DEFAULT_TPM = 4_000_000
# ``quality_score`` is advisory only (plan D5): it feeds the task-group floor
# warning and the artifacts, and never enters any routing decision.
# ``correction_prompt_tier`` is gone (plan v2 D2): variant ownership lives on
# the task-group cell (and optional model-group entry overrides), not on the
# model row.
QUALITY_SCORE_RANGE = range(0, 101)
#: What an unstated ``quality_score`` counts as (owner 2026-09-04). A blank
#: cell used to become 50, which is below every shipped floor -- so the row
#: that said nothing was treated as the worst thing anyone had measured, and a
#: person adding a model they had not scored got a warning phrased as if they
#: had scored it badly. The honest reading of "unstated" is "no claim", and the
#: cheap direction is to let it through: the score gates nothing, it only
#: phrases a warning. A blank row therefore passes every floor and says so once
#: as a **note** (`preset_binding_notes`), not a warning.
UNSTATED_QUALITY_SCORE = 100

# ``thinking`` column (owner design 2026-08-11, identity default 2026-08-12):
#
# - ``true`` (and an empty cell) -- the **identity mapping**: the
#   harness's abstract high/medium/low goes out verbatim. All three dialects
#   happen to spell the levels the same way (Gemini ``thinkingLevel``,
#   OpenAI-compatible ``reasoning_effort``, Anthropic ``output_config.effort``),
#   so identity is the right default rather than a lucky coincidence per row.
# - ``false`` -- the model takes no thinking parameter at all (self-hosted
#   endpoints that 400 on unknown fields declare this).
# - three comma-separated provider values -- an explicit override mapping the
#   abstract high, medium, low requests, in that order (e.g. a provider whose
#   words are ``max,default,minimal``). For a model whose thinking is a token
#   budget (Gemini 2.5) the three values are integers: the budgets themselves,
#   because the harness's derived ones (a share of the output limit) can exceed
#   the model's own ceiling, and the API rejects the call outright.
THINKING_DISABLED = "false"
THINKING_IDENTITY = "true"
IDENTITY_THINKING_LEVELS = ("high", "medium", "low")
_ABSTRACT_THINKING_INDEX = {"high": 0, "medium": 1, "low": 2}


def parse_thinking_spec(value: str, *, owner: str) -> tuple[str, str, str] | None:
    normalized = (value or "").strip()
    if normalized.lower() == THINKING_DISABLED:
        return None
    if normalized.lower() == THINKING_IDENTITY:
        return IDENTITY_THINKING_LEVELS
    parts = [part.strip() for part in normalized.split(",")]
    if len(parts) != 3 or any(not part for part in parts):
        raise ValueError(
            f"{owner}: thinking must be true (identity), false (no thinking "
            "parameter) or three comma-separated values mapping high,medium,low"
        )
    return (parts[0], parts[1], parts[2])


def thinking_value_for(entry: "ModelCatalogEntry", level: str | None) -> str:
    """The provider value this model uses for an abstract thinking level.

    ``""`` means "send no thinking parameter" -- either the model declared
    ``none`` or the caller asked for the model default.
    """

    if entry.thinking_levels is None:
        return ""
    index = _ABSTRACT_THINKING_INDEX.get((level or "").strip().lower())
    if index is None:
        return ""
    return entry.thinking_levels[index]


def dispatched_thinking(
    entry: "ModelCatalogEntry", level: str | None, derived_budget: int
) -> tuple[str, int]:
    """``(thinking_level, thinking_budget)`` actually sent for this model.

    An integer mapping is the model's own budget for that level and replaces
    the derived one (and no level word goes out); otherwise the mapped word
    goes out next to the derived budget, as before. A model that declares
    ``false`` gets neither.
    """

    if entry.thinking_levels is None:
        return "", 0
    value = thinking_value_for(entry, level)
    if value.isdigit():
        return "", int(value)
    return value, derived_budget


@dataclass(frozen=True)
class ModelCatalogEntry:
    fact_id: str
    provider_tier: str
    # What a human calls this row (artifacts, warnings); defaults to the API id.
    display_name: str
    # What the endpoint is called by name in the request.
    api_model_id: str
    max_input_tokens: int
    max_output_tokens: int
    #: The pool input and output share, when the provider has one. Blank in the
    #: file means "this row declares no joint constraint" and is stored as
    #: ``max_input_tokens + max_output_tokens``, which makes that constraint
    #: non-binding. A single-pool provider (Codex, Anthropic) needs to state it:
    #: for them an input of ``max_input_tokens`` leaves no room for the answer.
    #:
    #: ⚠ **Nothing enforces that.** The loader cannot know the pool's shape:
    #: ``provider_kind`` says which dialect a row speaks, and no dialect implies
    #: a pool -- ``openai_compat`` covers both kinds of provider, and every
    #: local agent (agy fronts Claude, codex fronts GPT) declares ``local_agent``.
    #: So a row that omits the cell plans against ``max_input + max_output``,
    #: and only its author knows whether that is true.
    #:
    #: The agy Claude rows (``local-agy-opus-5_5``, ``local-agy-sonnet-5_5``)
    #: are deliberately left blank (owner 2026-09-03, for the Opus 4.6 row they
    #: replaced). Claude is a single pool, but nobody has checked whether agy
    #: passes its full context through, so the rows keep the envelope that row
    #: always had (194000) instead of taking the vendor's 1M on faith -- filling
    #: ``max_input`` *and* the pool with 1M would raise it to 934464 on an
    #: unverified assumption. Those rows' ``max_input`` is not a vendor number
    #: either (``docs/llm_design_notes.md``, 「模型路由 v2 的持久决策」).
    #:
    #: Gemini publishes its two limits separately, so blank is the honest
    #: reading and the **free** rows use it. The **paid** rows and agy are still
    #: filled (``ctx = max_input = 1048576``, envelope 983040) -- an owner
    #: decision (2026-09-03) to plan them as one pool rather than trust that the
    #: two published numbers can be spent at the same time.
    #:
    #: Its **only** job is to cap the input envelope (owner 2026-09-03); it
    #: enters no other decision.
    context_window: int
    supports_audio: bool
    supports_video: bool
    # True when the transport cannot request Gemini's low media-resolution
    # tier. Planning must use the most expensive member in the routed group.
    video_high_resolution_only: bool
    supports_native_search: bool
    # None = the model takes no thinking parameter; otherwise provider values
    # for the harness's (high, medium, low) requests.
    thinking_levels: tuple[str, str, str] | None
    rpm: int
    tpm: int
    rpd: int
    tpd: int
    is_free: bool
    #: ``None`` = the row states no score. Distinct from any number, because
    #: "nobody has judged this model" and "somebody judged it 50" call for
    #: different messages -- see ``UNSTATED_QUALITY_SCORE``.
    quality_score: int | None
    #: Tell the worker, in the agent bootstrap, that its per-turn output is
    #: capped at this row's ``max_output_tokens`` and that it should break a
    #: long stretch with a cheap tool call rather than run into the cap.
    #:
    #: A switch, not a number: what gets stated is always this row's own
    #: ceiling, because a *wrong* one is measurably worse than silence
    #: (2026-09-04: `glm-5.3-flash` told its true 32000 finished a window it
    #: had twice timed out on, and told 64000 timed out again). Off by default
    #: -- it changes the prompt, and it helped exactly one of the three models
    #: it was measured on; `deepseek-v4-flash` was unmoved at every number.
    hint_output_ceiling: bool = False
    #: A second model to hand the session to when this one stops answering,
    #: passed to the CLI as its own fallback flag; blank means "no fallback,
    #: fail instead".
    #:
    #: ⚠ **This spends money.** It exists for the free/paid pairs a
    #: subscription offers -- CodeBuddy's `hy3` -> `hy3-x` -- where the
    #: free line is a daily allowance and the paid one bills credits
    #: (x0.05 against the free line's x0.00, vendor product config
    #: 2026-09-04; `hy4-preview` used to be a second pair, dropped
    #: 2026-09-11 when that line started billing credits). Owner
    #: decision 2026-09-04: for that row, a spent allowance should
    #: switch and carry on rather than stop the run, with one notice
    #: per session.
    #:
    #: Left blank for every other row, including `glm-5.3-flash` and
    #: DeepSeek -- those bill credits already and have no free twin to
    #: fall back *from*.
    fallback_model: str = ""
    # Which API dialect serves this row, and where. ``gemini`` / ``local_agent``
    # are the packaged kinds and need no URL; the two text dialects do.
    provider_kind: str = DEFAULT_PROVIDER_KIND
    base_url: str = ""
    # Env name holding this provider's key. Empty means "the packaged pool for
    # this tier" (Gemini) or the ``FINESUB_KEY_<TIER>`` convention.
    key_env: str = ""
    # Local-estimate correction factor (plan v2 D14). The 3-tier counter uses
    # Gemini's vocabulary; another model's tokenizer differs, so estimates
    # carry a systematic bias. Multiplied into every local estimate, never into
    # reported usage -- reports and calibration read the API's own numbers.
    token_scale: float = 1.0
    # Which allowance this row draws on when it runs out -- an allowance is a
    # property of the line the vendor meters, and a row only *points* at one.
    #
    # Three ways to write it:
    #   ""            the provider tier, right wherever one subscription backs
    #                 every model on a tier (one Codex plan, one Claude plan);
    #   PER_MODEL     this row has an allowance of its own. For a vendor whose
    #                 free lines are metered one by one, so a name never has to
    #                 be invented and a line that stops being free is a
    #                 one-value edit rather than a rename;
    #   anything else a pool shared by every row that names it.
    #
    # The tier default is *not* right for Antigravity, where the Gemini and
    # Claude models are metered separately, nor for CodeBuddy, where each free
    # line has its own daily quota and everything payable shares one credit
    # balance. Booking those together would take a working model out of
    # service for hours because its neighbour ran dry -- which is why
    # `TIERS_METERED_PER_LINE` refuses to guess for them.
    quota_pool: str = ""

    # Whether this row came from the override file rather than the packaged
    # default. Artifacts label those numbers as user-typed, not measured.
    self_reported: bool = False

    @property
    def effective_quota_pool(self) -> str:
        if self.quota_pool == PER_MODEL_QUOTA_POOL:
            # Derived rather than written down, so a new free line needs no
            # invented name (and two rows cannot silently share one by typo).
            # Keyed on (tier, model) rather than fact_id because that is what
            # an answering model name can be looked up by -- see
            # `get_model_catalog_entry_for_tier`.
            return f"{self.provider_tier}:{self.api_model_id}"
        return self.quota_pool or self.provider_tier

    @property
    def effective_quality_score(self) -> int:
        """The score comparisons use; an unstated one passes every floor."""

        return UNSTATED_QUALITY_SCORE if self.quality_score is None else self.quality_score


def _catalog_path() -> Path:
    return Path(__file__).resolve().with_name(CATALOG_FILENAME)


def _parse_bool(value: str, *, field: str, line_number: int, default: bool) -> bool:
    normalized = value.strip().lower()
    if not normalized:
        return default
    if normalized == "true":
        return True
    if normalized == "false":
        return False
    raise ValueError(f"{CATALOG_FILENAME}:{line_number}: {field} must be true/false")


def _parse_int(value: str, *, field: str, line_number: int, default: int | None = None) -> int:
    text = value.strip()
    if not text and default is not None:
        return default
    try:
        return int(text)
    except ValueError as exc:
        raise ValueError(
            f"{CATALOG_FILENAME}:{line_number}: {field} must be an integer"
        ) from exc


def _parse_float(value: str, *, field: str, line_number: int, default: float) -> float:
    text = value.strip()
    if not text:
        return default
    try:
        number = float(text)
    except ValueError as exc:
        raise ValueError(
            f"{CATALOG_FILENAME}:{line_number}: {field} must be a number"
        ) from exc
    if not number > 0:
        raise ValueError(
            f"{CATALOG_FILENAME}:{line_number}: {field} must be positive"
        )
    return number


def _entry_from_row(
    row: Dict[str, str], *, line_number: int, self_reported: bool
) -> ModelCatalogEntry:
    for key in REQUIRED_COLUMNS:
        if not row.get(key, "").strip():
            raise ValueError(f"{CATALOG_FILENAME}:{line_number}: missing {key}")
    fallback_model = str(row.get("fallback_model", "") or "").strip()
    hint_output_ceiling = _parse_bool(
        row.get("hint_output_ceiling", ""),
        field="hint_output_ceiling",
        line_number=line_number,
        default=False,
    )
    raw_quality = row.get("quality_score", "").strip()
    quality_score: int | None = None
    if raw_quality:
        quality_score = _parse_int(
            raw_quality,
            field="quality_score",
            line_number=line_number,
            default=0,
        )
        if quality_score not in QUALITY_SCORE_RANGE:
            raise ValueError(
                f"{CATALOG_FILENAME}:{line_number}: quality_score must be in "
                f"[{QUALITY_SCORE_RANGE.start}, {QUALITY_SCORE_RANGE[-1]}]"
            )
    provider_tier = row["provider_tier"].strip()
    provider_kind = row.get("provider_kind", "").strip().lower() or (
        PACKAGED_PROVIDER_KINDS.get(provider_tier, DEFAULT_PROVIDER_KIND)
    )
    if provider_kind not in PROVIDER_KINDS:
        raise ValueError(
            f"{CATALOG_FILENAME}:{line_number}: provider_kind must be one of "
            f"{sorted(PROVIDER_KINDS)}"
        )
    base_url = row.get("base_url", "").strip().rstrip("/")
    if provider_kind in (OPENAI_COMPAT_KIND, ANTHROPIC_KIND) and not base_url:
        raise ValueError(
            f"{CATALOG_FILENAME}:{line_number}: base_url is required for "
            f"provider_kind={provider_kind}"
        )
    if provider_kind in (GEMINI_KIND, LOCAL_AGENT_KIND, CONVERSATIONAL_KIND) and base_url:
        raise ValueError(
            f"{CATALOG_FILENAME}:{line_number}: provider_kind={provider_kind} "
            "takes no base_url (the packaged transports own their endpoint)"
        )
    supports_video = _parse_bool(
        row.get("supports_video", ""),
        field="supports_video",
        line_number=line_number,
        default=False,
    )
    video_high_resolution_only = _parse_bool(
        row.get("video_high_resolution_only", ""),
        field="video_high_resolution_only",
        line_number=line_number,
        default=False,
    )
    if video_high_resolution_only and not supports_video:
        raise ValueError(
            f"{CATALOG_FILENAME}:{line_number}: "
            "video_high_resolution_only=true requires supports_video=true"
        )
    # Custom HTTP media remains unsupported: the two text transports flatten
    # messages to plain strings, so a row claiming media on one of them would
    # pass capability filtering and then have its clip
    # silently dropped -- the exact silent degradation the design forbids.
    # Declare it here rather than trusting the writer to know.
    if provider_kind in (OPENAI_COMPAT_KIND, ANTHROPIC_KIND):
        for field_name in ("supports_audio", "supports_video"):
            if _parse_bool(
                row.get(field_name, ""),
                field=field_name,
                line_number=line_number,
                default=False,
            ):
                raise ValueError(
                    f"{CATALOG_FILENAME}:{line_number}: {field_name}=true is not "
                    f"available on provider_kind={provider_kind} (custom HTTP "
                    "media is unsupported; use --media text to "
                    "put a text model on media-bearing material)"
                )
    max_input = _parse_int(
        row["max_input_tokens"], field="max_input_tokens", line_number=line_number
    )
    if max_input <= 0:
        raise ValueError(
            f"{CATALOG_FILENAME}:{line_number}: max_input_tokens must be positive"
        )
    max_output = _parse_int(
        row.get("max_output_tokens", ""),
        field="max_output_tokens",
        line_number=line_number,
        default=DEFAULT_MAX_OUTPUT_TOKENS,
    )
    if max_output <= 0:
        raise ValueError(
            f"{CATALOG_FILENAME}:{line_number}: max_output_tokens must be positive"
        )
    # Blank -- the *cell*, not the value zero -- means "this row declares no
    # joint constraint", which the sum expresses exactly: the constraint can
    # then never bind. An explicit number is checked instead of clamped: a pool
    # smaller than either half it must hold is a declaration error, and
    # clamping it would silently plan against a window the provider does not
    # have. `0` therefore fails that check rather than reading as absent, which
    # is why the emptiness test is on the text.
    context_cell = (row.get("context_window") or "").strip()
    if not context_cell:
        context_window = max_input + max_output
    else:
        context_window = _parse_int(
            context_cell, field="context_window", line_number=line_number
        )
        if context_window < max(max_input, max_output):
            raise ValueError(
                f"{CATALOG_FILENAME}:{line_number}: context_window "
                f"({context_window}) is smaller than max_input_tokens "
                f"({max_input}) or max_output_tokens ({max_output}); leave the "
                "cell empty when the row declares no joint constraint"
            )
    api_model_id = row["api_model_id"].strip()
    return ModelCatalogEntry(
        fact_id=row["fact_id"].strip(),
        provider_tier=provider_tier,
        display_name=row.get("display_name", "").strip() or api_model_id,
        api_model_id=api_model_id,
        max_input_tokens=max_input,
        max_output_tokens=max_output,
        context_window=context_window,
        supports_audio=_parse_bool(
            row.get("supports_audio", ""),
            field="supports_audio",
            line_number=line_number,
            default=False,
        ),
        supports_video=supports_video,
        video_high_resolution_only=video_high_resolution_only,
        supports_native_search=_parse_bool(
            row.get("supports_native_search", ""),
            field="supports_native_search",
            line_number=line_number,
            default=False,
        ),
        thinking_levels=parse_thinking_spec(
            row.get("thinking", "") or THINKING_IDENTITY,
            owner=f"{CATALOG_FILENAME}:{line_number}",
        ),
        rpm=_parse_int(
            row.get("rpm", ""), field="rpm", line_number=line_number, default=DEFAULT_RPM
        ),
        tpm=_parse_int(
            row.get("tpm", ""), field="tpm", line_number=line_number, default=DEFAULT_TPM
        ),
        rpd=_parse_int(
            row.get("rpd", ""), field="rpd", line_number=line_number, default=-1
        ),
        tpd=_parse_int(
            row.get("tpd", ""), field="tpd", line_number=line_number, default=-1
        ),
        is_free=_parse_bool(
            row.get("is_free", ""),
            field="is_free",
            line_number=line_number,
            default=False,
        ),
        quality_score=quality_score,
        hint_output_ceiling=hint_output_ceiling,
        fallback_model=fallback_model,
        quota_pool=row.get("quota_pool", "").strip(),
        provider_kind=provider_kind,
        base_url=base_url,
        key_env=row.get("key_env", "").strip(),
        token_scale=_parse_float(
            row.get("token_scale", ""),
            field="token_scale",
            line_number=line_number,
            default=1.0,
        ),
        self_reported=self_reported,
    )


def load_model_catalog(
    path: str | Path | None = None, *, self_reported: bool = False
) -> List[ModelCatalogEntry]:
    """Parse one catalog file.

    The header declares which columns the file carries: an override file may
    list just the four required ones plus whatever it wants to state, and
    blank cells inside a declared column fall back to the same defaults.
    Unknown column names are rejected rather than ignored -- a typo in a
    header is otherwise a silently missing fact.
    """

    catalog_path = Path(path).expanduser() if path is not None else _catalog_path()
    lines = catalog_path.read_text(encoding="utf-8").splitlines()
    # Comments and blanks are skipped below the header, so they are skipped
    # above it too: an override file is meant to be hand-written, and the
    # first thing a person writes is a line saying what the file is for.
    header_index = next(
        (
            index
            for index, raw in enumerate(lines)
            if raw.strip() and not raw.strip().startswith("#")
        ),
        None,
    )
    if header_index is None:
        raise ValueError(f"{catalog_path} is empty")
    columns = tuple(part.strip() for part in lines[header_index].split("|"))
    retired = [name for name in columns if name in RETIRED_COLUMNS]
    if retired:
        pairs = ", ".join(f"{name} -> {RETIRED_COLUMNS[name]}" for name in retired)
        raise ValueError(
            f"{catalog_path}: the header uses column names this version retired "
            f"({pairs}). Rename them in the header row; the cells underneath are "
            "unchanged."
        )
    unknown = [name for name in columns if name not in CATALOG_COLUMNS]
    missing = [name for name in REQUIRED_COLUMNS if name not in columns]
    if unknown or missing:
        raise ValueError(
            f"{catalog_path}: header must use known columns "
            f"({'|'.join(CATALOG_COLUMNS)}) and include {list(REQUIRED_COLUMNS)}; "
            f"unknown={unknown}, missing={missing}"
        )
    entries: List[ModelCatalogEntry] = []
    for line_number, raw in enumerate(
        lines[header_index + 1 :], start=header_index + 2
    ):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("|")
        if len(parts) != len(columns):
            raise ValueError(
                f"{catalog_path}:{line_number}: expected {len(columns)} fields"
            )
        entries.append(
            _entry_from_row(
                dict(zip(columns, parts)),
                line_number=line_number,
                self_reported=self_reported,
            )
        )
    _validate_catalog_table(entries, catalog_path)
    return entries


def _validate_catalog_table(
    entries: Sequence[ModelCatalogEntry], where: Any
) -> None:
    """Every check a whole table has to pass, in one call.

    One entry point on purpose: the effective catalog is reached by two paths
    -- the packaged file alone when there is no override, and the merged table
    when there is -- and a check wired into only one of them is a check the
    other silently skips.
    """

    _reject_duplicates(entries, where)
    _reject_bad_quota_pools(entries, where)


def _reject_bad_quota_pools(
    entries: Sequence[ModelCatalogEntry], where: Any
) -> None:
    """Two things a table of allowances must not say.

    Neither is detectable later: a wrong pool produces no error, just a freeze
    booked against the wrong line -- or a working line taken out for hours.
    """

    by_pool: dict[str, set[bool]] = {}
    for entry in entries:
        if (
            entry.provider_tier in TIERS_METERED_PER_LINE
            and not entry.quota_pool
        ):
            raise ValueError(
                f"{where}: {entry.fact_id!r} leaves quota_pool empty, but "
                f"{entry.provider_tier} meters several allowances behind one "
                f"login, so the tier is not an answer. Write "
                f"{PER_MODEL_QUOTA_POOL!r} if this line has a quota of its "
                f"own, or the name of the pool it shares (e.g. "
                f"CODEBUDDY_CREDITS)."
            )
        by_pool.setdefault(entry.effective_quota_pool, set()).add(entry.is_free)
    # A free line and a paid one cannot draw on the same allowance, so this
    # says the table is wrong -- but it *warns*, and the asymmetry is the
    # point. `is_free` drives nothing else and never has, so its values across
    # the table are unverified annotations; promoting one to a load-blocking
    # gate would stop a person's working configuration over a column nobody
    # had reason to keep accurate. It found a real one the first time it ran
    # (a free row sharing the tier pool with paid ones), which is exactly the
    # service it should render: say so, let the owner decide.
    for pool, free_flags in by_pool.items():
        if len(free_flags) > 1:
            sharing = sorted(
                entry.fact_id
                for entry in entries
                if entry.effective_quota_pool == pool
            )
            _warn_catalog(
                f"{where}: quota_pool {pool!r} is shared by rows that "
                f"disagree about is_free ({', '.join(sharing)}). A free line "
                f"and a paid one cannot draw on the same allowance, so one of "
                f"them is booked against a quota it does not spend."
            )


def _warn_catalog(message: str) -> None:
    from finesub.reporting import current_reporter

    current_reporter().warning("model-catalog", message)


def _reject_duplicates(
    entries: Sequence[ModelCatalogEntry], where: Any
) -> None:
    fact_ids: set[str] = set()
    endpoint_keys: set[tuple[str, str]] = set()
    for entry in entries:
        if entry.fact_id in fact_ids:
            raise ValueError(f"{where}: duplicate fact_id {entry.fact_id!r}")
        # (provider, model) is the rate limiter's accounting key and the
        # fallback fact lookup, so two rows on one endpoint would share a
        # bucket and resolve to whichever came first.
        endpoint_key = (entry.provider_tier, entry.api_model_id)
        if endpoint_key in endpoint_keys:
            raise ValueError(
                f"{where}: duplicate provider/model fact "
                f"{entry.provider_tier}|{entry.api_model_id}"
            )
        fact_ids.add(entry.fact_id)
        endpoint_keys.add(endpoint_key)


def merge_catalogs(
    packaged: Sequence[ModelCatalogEntry],
    override: Sequence[ModelCatalogEntry],
) -> List[ModelCatalogEntry]:
    """Packaged defaults with the override file layered on top, by ``fact_id``.

    Overriding a packaged id replaces that row in place (order preserved, so a
    re-tuned limit does not move the model); a new id appends.
    """

    merged = {entry.fact_id: entry for entry in packaged}
    for entry in override:
        merged[entry.fact_id] = entry
    result = list(merged.values())
    _validate_catalog_table(result, "merged catalog")
    return result


@lru_cache(maxsize=1)
def default_model_catalog() -> tuple[ModelCatalogEntry, ...]:
    """The effective catalog: packaged defaults + the data-root override."""

    from finesub.paths import resolve_model_catalog_override

    packaged = load_model_catalog()
    override_path = resolve_model_catalog_override()
    if override_path is None:
        return tuple(packaged)
    override = load_model_catalog(override_path, self_reported=True)
    return tuple(merge_catalogs(packaged, override))


def catalog_by_api_model_id(
    entries: Iterable[ModelCatalogEntry] | None = None,
) -> Dict[str, ModelCatalogEntry]:
    result: Dict[str, ModelCatalogEntry] = {}
    for entry in entries or default_model_catalog():
        if entry.api_model_id not in result:
            result[entry.api_model_id] = entry
    return result


def catalog_by_fact_id(
    entries: Iterable[ModelCatalogEntry] | None = None,
) -> Dict[str, ModelCatalogEntry]:
    return {
        entry.fact_id: entry for entry in entries or default_model_catalog()
    }


def get_model_catalog_entry_by_fact(
    fact_id: str,
) -> ModelCatalogEntry | None:
    return catalog_by_fact_id().get(fact_id)


def get_model_catalog_entry(api_model_id: str) -> ModelCatalogEntry | None:
    return catalog_by_api_model_id().get(api_model_id)


def get_model_catalog_entry_for_tier(
    api_model_id: str,
    provider_tier: str,
) -> ModelCatalogEntry | None:
    for entry in default_model_catalog():
        if entry.api_model_id == api_model_id and entry.provider_tier == provider_tier:
            return entry
    return None


def provider_tier_for_model(api_model_id: str, fallback: str = "") -> str:
    entry = get_model_catalog_entry(api_model_id)
    return entry.provider_tier if entry is not None else fallback


def supports_reasoning(api_model_id: str, default: bool = True) -> bool:
    entry = get_model_catalog_entry(api_model_id)
    return default if entry is None else entry.thinking_levels is not None


def quality_floor_warnings(
    entries: Iterable[ModelCatalogEntry],
    *,
    floor_score: int,
    reference_model: str,
    owner: str,
) -> List[str]:
    """Advisory warnings for members scoring below a task-group floor.

    ``quality_score`` never enters routing decisions (plan v2 D5); this only
    phrases the warning that preset binding emits once per cell (§5.4). The
    message is quantified and names the member, so the user knows what to move
    rather than being told "the gap is too large".
    """

    return [
        f"{owner}: 成员 {entry.fact_id} 的 quality_score={entry.quality_score} "
        f"低于下限 {floor_score}（参考模型 {reference_model}）"
        for entry in entries
        if entry.effective_quality_score < floor_score
    ]


def unstated_quality_notes(
    entries: Iterable[ModelCatalogEntry], *, owner: str
) -> List[str]:
    """A note per member whose row states no ``quality_score``.

    Deliberately not a warning: nothing is wrong, and the run is going to
    proceed on the most permissive reading of the blank cell
    (``UNSTATED_QUALITY_SCORE``). Saying it once per bound group is what stops
    that permissiveness from being invisible -- a floor that never fires
    because nobody filled the column in looks exactly like a floor that passed.
    """

    return [
        f"{owner}: 成员 {entry.fact_id} 未声明 quality_score，"
        f"按 {UNSTATED_QUALITY_SCORE} 计（不触发任何质量下限）"
        for entry in entries
        if entry.quality_score is None
    ]
