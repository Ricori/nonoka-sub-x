"""Declared facts for the models the desktop routes to over HTTP.

``FineSubSettings._sync_model_routing`` writes one ``model_catalog.psv`` row per
user-selected model, and the engine plans against what that row claims. The two
numbers that matter are not cosmetic:

* ``max_output_tokens`` is the output envelope. Windows are cut so that the
  expected output fits ``0.9 x limit - 5000`` tokens, and a call whose reported
  output + thinking tokens reaches the limit is declared truncated, thrown away
  whole, and re-run as two half windows.
* ``max_input_tokens`` is the context window the prompt budget is derived from.

A row that understates the limit therefore does not merely waste capacity -- it
manufactures a split-and-retry ladder. The placeholder row every custom model
used to get (128000/16384, thinking=true) did exactly that to DeepSeek V4, whose
chain of thought is billed inside ``completion_tokens``: every window spent its
whole 16k budget on reasoning, failed validation with a half-written
``<translated>`` block, was split in half and re-run -- 5 planned windows became
13, each attempt a full multi-minute generation discarded.

Sources (retrieved 2026-09-04), one per family:

* OpenAI    https://developers.openai.com/api/docs/models/gpt-5.6
            1,050,000 context / 128,000 output; effort
            none|low|medium|high|xhigh|max.
* DeepSeek  https://api-docs.deepseek.com/guides/thinking_mode/
            v4 family 1M context / 384,000 output; reasoning_effort high|max
            with low/medium accepted and raised to high server-side;
            reasoning_tokens are a breakdown *inside* completion_tokens.
* Qwen      https://www.qwencloud.com/models/qwen3.8-max
            1M context / 131,072 output; reasoning_effort low|medium|xhigh.
* Kimi      https://platform.kimi.ai/docs/guide/use-reasoning-effort
            kimi-k3 1M context, 131,072 default completion cap;
            reasoning_effort low|high|max.

The table is keyed by *model id*, not by provider, so it serves all four HTTP
providers the desktop offers -- ``openai``, ``anthropic``, ``openai-compat`` and
``anthropic-compat``. That is deliberate: the same model is reached through
whichever dialect its endpoint speaks (Claude through an OpenAI-compatible
relay, DeepSeek through its own Anthropic-format endpoint), and it is the model,
not the route, that decides how much it can read and write. The ``thinking``
column follows the same rule -- both transports carry an effort word, only under
different names (``reasoning_effort`` vs ``output_config.effort``).

Two fields are deliberately absent. ``supports_audio``/``supports_video`` are
always false here because the packaged HTTP transports are text-only
(``finesub.llm.provider_transports`` flattens every message to plain text) --
the catalog parser rejects a row of these kinds that claims otherwise, whatever
the model itself can do. And rate limits stay blank: they are per-account, not
per-model, so the catalog defaults (100 rpm / 4M tpm) are the honest answer.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ModelSpec:
    """One model's declared facts, in ``model_catalog.psv`` terms."""

    display_name: str
    max_input_tokens: int
    max_output_tokens: int
    # The ``thinking`` column: "true" (identity -- the harness's abstract
    # high/medium/low go out verbatim), "false" (send no reasoning parameter at
    # all), or three provider values mapping high, medium, low in that order.
    # "false" is not a preference, it is a fact about the endpoint: a provider
    # that does not take the parameter may 400 on it, and one whose thinking is
    # a model-level mode ignores it.
    thinking: str = "true"
    # Local-estimate correction (the engine's counter speaks Gemini's
    # vocabulary). >1 makes planning more conservative for a model whose
    # tokenizer splits the same text into more pieces. The CJK-first families
    # carry a modest margin: it is a deliberate cushion, not a measurement, and
    # it can only shrink a window.
    token_scale: float = 1.0
    # Advisory only (task-group floor warnings and artifacts); never routing.
    # Engine 0.5.1 also accepts a blank cell, meaning "nobody has judged this
    # model": it clears every floor and says so once as a debug note. This
    # default stays a number because the table's rows *are* judged -- the
    # blank is for a row nobody measured, which is not what these are.
    quality_score: int = 70


# What an endpoint the table does not recognize is assumed to be. Thinking is
# off rather than on because sending an unknown field is the failure that
# cannot be recovered from -- a self-hosted vLLM/Ollama endpoint rejects the
# request outright -- while not sending it only forgoes a knob.
#
# The two numbers sit at or above FineSub's own warning line, not below it, and
# that is deliberate as of engine 0.5.0. (They were exactly on it when written;
# 0.5.1 lowered the input line to 192,000 for a real model that declares that
# number, so 194,000 is now a hair above it. Unchanged on purpose -- the
# argument below is for guessing high, and being above the line is the safe
# side of it.) The engine checks every bound model before recognition starts
# (`llm.routing.capabilities.WINDOW_WARN_*` / `WINDOW_REFUSE_*`) and **stops
# the run** at `max_output < 32,000`. The old placeholder claimed 16,384, so a
# user who pinned any endpoint this table does not know would have had the task
# refused at startup with a number nobody chose -- a guess, blocking work.
#
# Guessing high is the safer direction here, because an overstatement is a
# situation the engine already handles: a window whose answer runs into the
# real ceiling is detected, split in half and re-run. An understatement is not
# symmetric -- below the refusal line it does not degrade, it stops. Users who
# know their endpoint's real limits should still say so; the settings panel
# takes them.
UNKNOWN_MODEL_SPEC = ModelSpec(
    display_name="",
    max_input_tokens=194_000,
    max_output_tokens=64_000,
    thinking="false",
    token_scale=1.0,
    quality_score=70,
)

# Prefix -> spec. Matching is longest-prefix-first over the normalized id, so a
# dated or sized snapshot (``gpt-5.5-2026-04-23``, ``qwen3.8-max-0902``,
# ``deepseek-v4-flash-vision-exp``) resolves to its family without an entry of
# its own, and a more specific family member wins over the generic rule.
_MODEL_SPECS: tuple[tuple[str, ModelSpec], ...] = (
    # -- OpenAI -----------------------------------------------------------
    # The 5.6 trio shares one envelope and differs only in price/quality.
    ("gpt-5.6-sol", ModelSpec("GPT-5.6 Sol", 1_050_000, 128_000, "true", 1.0, 90)),
    ("gpt-5.6-terra", ModelSpec("GPT-5.6 Terra", 1_050_000, 128_000, "true", 1.0, 80)),
    ("gpt-5.6-luna", ModelSpec("GPT-5.6 Luna", 1_050_000, 128_000, "true", 1.0, 70)),
    ("gpt-5.6", ModelSpec("GPT-5.6", 1_050_000, 128_000, "true", 1.0, 80)),
    ("gpt-5.5", ModelSpec("GPT-5.5", 1_050_000, 128_000, "true", 1.0, 85)),
    ("gpt-5", ModelSpec("GPT-5", 400_000, 128_000, "true", 1.0, 80)),
    ("gpt-4.1", ModelSpec("GPT-4.1", 1_047_576, 32_768, "false", 1.0, 60)),
    ("gpt-4o", ModelSpec("GPT-4o", 128_000, 16_384, "false", 1.0, 55)),
    # -- Anthropic (also reached through anthropic-compat) ----------------
    ("claude-opus-5", ModelSpec("Claude Opus 5", 1_000_000, 65_536, "true", 1.0, 88)),
    ("claude-sonnet-5", ModelSpec("Claude Sonnet 5", 1_000_000, 65_536, "true", 1.0, 77)),
    ("claude-haiku-4-5", ModelSpec("Claude Haiku 4.5", 194_000, 65_536, "true", 1.0, 70)),
    ("claude-opus-4", ModelSpec("Claude Opus 4", 200_000, 32_000, "true", 1.0, 80)),
    ("claude-sonnet-4", ModelSpec("Claude Sonnet 4", 200_000, 64_000, "true", 1.0, 72)),
    # Anything older than the 4 series: 200k context, and no effort parameter,
    # because extended thinking only exists from Claude 4 on.
    ("claude-", ModelSpec("Claude", 200_000, 32_000, "false", 1.0, 70)),
    # -- DeepSeek ---------------------------------------------------------
    # V4 thinks by default and cannot be talked out of it through
    # reasoning_effort: low/medium are accepted but raised to high server-side,
    # so the mapping spends the harness's "low" request honestly instead of
    # pretending a cheap tier exists. The real 384k output cap is what keeps the
    # chain of thought from eating the answer; the engine clamps it to its own
    # 65,536 ceiling anyway.
    ("deepseek-v4-pro", ModelSpec("DeepSeek-V4-Pro", 1_000_000, 384_000, "max,high,low", 1.15, 85)),
    ("deepseek-v4-flash", ModelSpec("DeepSeek-V4-Flash", 1_000_000, 384_000, "max,high,low", 1.15, 70)),
    ("deepseek-v4", ModelSpec("DeepSeek-V4", 1_000_000, 384_000, "max,high,low", 1.15, 75)),
    # Retired on the official API (2026-07-24) but still served under these
    # names by many OpenAI-compatible gateways, where they keep their old, much
    # smaller envelopes. ``deepseek-chat`` is the non-thinking mode.
    ("deepseek-reasoner", ModelSpec("DeepSeek Reasoner", 128_000, 65_536, "max,high,low", 1.15, 75)),
    ("deepseek-chat", ModelSpec("DeepSeek Chat", 128_000, 8_192, "false", 1.15, 65)),
    # -- Qwen -------------------------------------------------------------
    # DashScope spells the effort levels low/medium/xhigh -- "high" is not one
    # of them, so identity would send a value the endpoint does not know.
    ("qwen3.8-max", ModelSpec("Qwen3.8-Max", 1_000_000, 131_072, "xhigh,medium,low", 1.15, 80)),
    ("qwen3.8", ModelSpec("Qwen3.8", 1_000_000, 65_536, "xhigh,medium,low", 1.15, 68)),
    ("qwen3.7-max", ModelSpec("Qwen3.7-Max", 1_000_000, 65_536, "xhigh,medium,low", 1.15, 78)),
    ("qwen3.7", ModelSpec("Qwen3.7", 1_000_000, 65_536, "xhigh,medium,low", 1.15, 66)),
    ("qwen3.6", ModelSpec("Qwen3.6", 1_000_000, 65_536, "xhigh,medium,low", 1.15, 70)),
    # The 2025 flagship gates thinking behind ``enable_thinking``, not
    # reasoning_effort, so the parameter is left off.
    ("qwen3-max", ModelSpec("Qwen3-Max", 262_144, 65_536, "false", 1.15, 70)),
    ("qwen", ModelSpec("Qwen", 131_072, 16_384, "false", 1.15, 60)),
    # -- Moonshot / Kimi --------------------------------------------------
    ("kimi-k3-256k", ModelSpec("Kimi K3 256k", 256_000, 131_072, "max,high,low", 1.15, 78)),
    ("k3-256k", ModelSpec("Kimi K3 256k", 256_000, 131_072, "max,high,low", 1.15, 78)),
    ("kimi-k3", ModelSpec("Kimi K3", 1_000_000, 131_072, "max,high,low", 1.15, 80)),
    ("kimi-k2.7", ModelSpec("Kimi K2.7", 256_000, 65_536, "max,high,low", 1.15, 75)),
    ("kimi-k2.6", ModelSpec("Kimi K2.6", 256_000, 65_536, "max,high,low", 1.15, 72)),
    ("kimi-k2", ModelSpec("Kimi K2", 256_000, 32_768, "false", 1.15, 68)),
    ("kimi-latest", ModelSpec("Kimi", 256_000, 32_768, "false", 1.15, 68)),
    ("moonshot-v1", ModelSpec("Moonshot v1", 128_000, 16_384, "false", 1.15, 55)),
    # -- Gemini through an OpenAI-compatible gateway ----------------------
    # The packaged Gemini providers have their own catalog rows; this only
    # covers a Gemini model reached through a third-party compat endpoint,
    # which cannot carry media whatever the model itself supports.
    ("gemini-3", ModelSpec("Gemini 3", 1_000_000, 65_536, "true", 1.0, 75)),
    ("gemini-", ModelSpec("Gemini", 1_000_000, 65_536, "true", 1.0, 70)),
)

_SPECS_BY_LENGTH: tuple[tuple[str, ModelSpec], ...] = tuple(
    sorted(_MODEL_SPECS, key=lambda item: len(item[0]), reverse=True)
)


def normalize_model_id(model: str) -> str:
    """The id reduced to what the table matches on.

    Gateways decorate the same model three ways: a vendor path prefix
    (``deepseek-ai/DeepSeek-V4-Pro``, ``openai/gpt-5.6-sol``), an OpenRouter
    variant suffix (``:free``, ``:nitro``), and case. None of them change which
    model is being called.
    """

    cleaned = (model or "").strip().lower()
    if ":" in cleaned:
        cleaned = cleaned.split(":", 1)[0]
    if "/" in cleaned:
        cleaned = cleaned.rsplit("/", 1)[-1]
    return cleaned.strip()


def spec_for(model: str) -> ModelSpec:
    """The declared facts for ``model``, or the conservative unknown default."""

    normalized = normalize_model_id(model)
    if not normalized:
        return UNKNOWN_MODEL_SPEC
    for prefix, spec in _SPECS_BY_LENGTH:
        if normalized.startswith(prefix):
            return spec
    return UNKNOWN_MODEL_SPEC


def is_known_model(model: str) -> bool:
    """Whether the table recognizes ``model`` (i.e. the row is not a guess)."""

    return spec_for(model) is not UNKNOWN_MODEL_SPEC
