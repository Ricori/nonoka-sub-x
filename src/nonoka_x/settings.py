"""Local settings, exposed without revealing secret values.

Stored in Nonoka's own `settings.json` and `secrets.json` (`nonoka_x.config`,
WP-R4); the files FineSub reads are derived from them (`FineSubSettings`).
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import os
from pathlib import Path
import re
import shutil
import sys
from typing import Any, Mapping
from urllib.parse import urlsplit

from .model_specs import spec_for as model_spec_for

KEY_SPECS: tuple[dict[str, str], ...] = (
    {"name": "GEMINI_FREE", "label": "Gemini 免费池", "purpose": "LLM 纠错、翻译与 Gemma 搜索"},
    {"name": "GEMINI_PAID", "label": "Gemini 付费池", "purpose": "质量优先的纠错与翻译"},
    {"name": "EXA_KEYS", "label": "Exa", "purpose": "联网检索"},
    {"name": "TAVILY_KEYS", "label": "Tavily", "purpose": "联网检索回退"},
    {"name": "ANTHROPIC_API_KEY", "label": "Anthropic", "purpose": "本地代理高级配置"},
    {"name": "OPENAI_API_KEY", "label": "OpenAI", "purpose": "本地代理高级配置"},
    {"name": "DEEPSEEK_API_KEY", "label": "DeepSeek", "purpose": "DeepSeek 模型调用"},
    {"name": "KIMI_API_KEY", "label": "Kimi", "purpose": "Kimi 模型调用"},
    {"name": "DASHSCOPE_API_KEY", "label": "阿里云百炼", "purpose": "阿里云百炼模型调用"},
    {"name": "OPENAI_COMPAT_API_KEY", "label": "OpenAI 兼容提供商", "purpose": "自定义 OpenAI 兼容端点"},
    {"name": "ANTHROPIC_COMPAT_API_KEY", "label": "Anthropic 兼容提供商", "purpose": "自定义 Anthropic 兼容端点"},
    {"name": "HF_TOKEN", "label": "Hugging Face", "purpose": "受限模型下载"},
)
KEY_NAMES = frozenset(spec["name"] for spec in KEY_SPECS)

BASE_URL_SPECS: tuple[dict[str, str], ...] = (
    {
        "name": "GEMINI_BASE_URL",
        "label": "Gemini",
        "defaultValue": "https://generativelanguage.googleapis.com/v1beta",
    },
    {
        "name": "OPENAI_BASE_URL",
        "label": "OpenAI",
        "defaultValue": "https://api.openai.com/v1",
    },
    {
        "name": "ANTHROPIC_BASE_URL",
        "label": "Anthropic",
        "defaultValue": "https://api.anthropic.com",
    },
    {
        "name": "OPENAI_COMPAT_BASE_URL",
        "label": "OpenAI 兼容提供商",
        "defaultValue": "",
    },
    {
        "name": "ANTHROPIC_COMPAT_BASE_URL",
        "label": "Anthropic 兼容提供商",
        "defaultValue": "",
    },
)
BASE_URL_NAMES = frozenset(spec["name"] for spec in BASE_URL_SPECS)
SETTING_NAMES = KEY_NAMES | BASE_URL_NAMES

# Providers whose models run on a local agent CLI instead of an API key: the
# tier picks the packaged targets, the command is what has to be on PATH. The
# engine ships Claude Code and dsh tiers as well; only the ones listed here
# are offered as a desktop route.
#
# `note` is prose the settings page shows under the detection line, for a
# provider whose behaviour a person has to know *before* selecting it. Only
# CodeBuddy carries one today, and what it says costs money.
LOCAL_AGENT_PROVIDERS: Mapping[str, dict[str, Any]] = {
    "local-codex": {
        "tier": "LOCAL_CODEX",
        "label": "本地 Codex",
        "command": "codex",
        # Which models the desktop offers on Codex, in display order -- the
        # first is what selecting the provider fills in. GPT-6.2 rows are
        # declared in `LOCAL_AGENT_OVERLAY_MODELS` until FineSub packages them.
        "models": (
            "gpt-6.2-sol",
            "gpt-6.2-astra",
            "gpt-6.1-sol",
            "gpt-6-luna",
        ),
        # The Codex app keeps its CLI in a content-hashed directory under
        # %LOCALAPPDATA% and never puts it on PATH, so `codex` is a name
        # nothing can resolve on a machine that has it installed. Windows only
        # on purpose: the npm and Homebrew installs land on PATH by themselves.
        "windows_app_globs": ("OpenAI/Codex/bin/*/codex.exe",),
    },
    "local-agy": {
        "tier": "LOCAL_AGY",
        "label": "本地 Antigravity",
        "command": "agy",
        # Gemini 4 (Hydrogen, Neon, Argon) and Gemini 3.8 Flash all accept
        # media; the two Claude rows are text only and share agy's Claude
        # allowance. Hydrogen and Neon are declared in
        # `LOCAL_AGENT_OVERLAY_MODELS` until FineSub packages them.
        "models": (
            "gemini-4-hydrogen",
            "gemini-4-neon",
            "gemini-4-argon",
            "gemini-3.8-flash",
            "claude-sonnet-5-5",
            "claude-opus-5-5",
        ),
        # `agy` is a native Go binary installed by its own script
        # (`irm https://antigravity.google/cli/install.ps1 | iex`), *not* by
        # the Antigravity IDE — that Electron install carries no CLI at all.
        # The script normally registers its directory on PATH; these globs
        # only cover the install that did not, and the two spellings the
        # vendor has shipped it under.
        "windows_app_globs": ("agy/bin/agy.exe", "Antigravity/agy.exe"),
    },
    "local-codebuddy": {
        "tier": "LOCAL_CODEBUDDY",
        "label": "本地 CodeBuddy",
        # The standalone CodeBuddy Code CLI (FineSub 0.6.0 stopped using the
        # copy inside the WorkBuddy desktop app). It installs through npm, so
        # its shim lands on PATH and the engine finds it by name.
        "command": "codebuddy",
        # GLM-5.3 Flash leads on the engine's own recommendation (it heads
        # the packaged `codebuddy-capable` group): the row whose thinking tier
        # was lowered so it stops looping on a correction window, and the
        # cheapest in credits. Its 32000-token output ceiling is the trade.
        #
        # ⚠ Which of these an account can actually reach is the account's
        # business, not the CLI version's: the vendor answers a wrong `--model`
        # with the list that login is entitled to. Rows beyond the four
        # FineSub 0.7.0 packages are declared in `LOCAL_AGENT_OVERLAY_MODELS`.
        "models": (
            "glm-5.3-flash",
            "glm-5.3",
            "hy3",
            "hy4-preview",
            "deepseek-v4.1-flash",
            "deepseek-v4-pro",
            "kimi-k3",
            "minimax-m3",
        ),
        "note": (
            "Hunyuan 3 每天有免费额度；用光之后会自动切到同一条线的付费额度"
            "（hy3-x）把当次任务跑完，并在结束时提示切过。其余模型均按积分计费。"
            "需要独立安装并登录 CodeBuddy Code CLI（2.164.0 或更新），不再使用 WorkBuddy 桌面端。"
        ),
    },
}

#: Local-agent models Nonoka offers on top of FineSub 0.7.0's quality-locked
#: `model_catalog.toml`. Each entry is `(fact_id, catalog_row)` in FineSub's
#: `model_catalog.toml` schema; `FineSubSettings.llm_context` emits a row into
#: the derived `model_catalog.toml` only when a saved route selects it, and
#: FineSub's `load_effective_catalog` + `_wire_unclaimed_catalog_target` merge
#: and wire it into a first-class `local_agent` target without modifying any
#: locked engine file.
LOCAL_AGENT_OVERLAY_MODELS: Mapping[str, Mapping[str, tuple[str, Mapping[str, Any]]]] = {
    "local-codex": {
        "gpt-6.2-sol": (
            "local-codex-gpt-6_2-sol",
            {
                "provider_tier": "LOCAL_CODEX",
                "display_name": "GPT-6.2 Sol via Codex",
                "api_model_id": "gpt-6.2-sol",
                "max_input_tokens": 272_000,
                "context_window": 272_000,
                "thinking": ["high", "high", "low"],
                "capability": "high",
            },
        ),
        "gpt-6.2-astra": (
            "local-codex-gpt-6_2-astra",
            {
                "provider_tier": "LOCAL_CODEX",
                "display_name": "GPT-6.2 Astra via Codex",
                "api_model_id": "gpt-6.2-astra",
                "max_input_tokens": 272_000,
                "context_window": 272_000,
                "thinking": ["high", "high", "low"],
                "capability": "high",
            },
        ),
    },
    "local-agy": {
        "gemini-4-hydrogen": (
            "local-agy-gemini-4-hydrogen",
            {
                "provider_tier": "LOCAL_AGY",
                "display_name": "Gemini 4 Hydrogen via agy",
                "api_model_id": "gemini-4-hydrogen",
                "max_input_tokens": 1_048_576,
                "context_window": 1_048_576,
                "supports_audio": True,
                "supports_video": True,
                "video_high_resolution_only": True,
                "thinking": ["medium", "medium", "low"],
                "capability": "medium",
                "quota_pool": "AGY_HYDROGEN",
            },
        ),
        "gemini-4-neon": (
            "local-agy-gemini-4-neon",
            {
                "provider_tier": "LOCAL_AGY",
                "display_name": "Gemini 4 Neon via agy",
                "api_model_id": "gemini-4-neon",
                "max_input_tokens": 1_048_576,
                "context_window": 1_048_576,
                "supports_audio": True,
                "supports_video": True,
                "video_high_resolution_only": True,
                "thinking": ["medium", "medium", "low"],
                "capability": "high",
                "quota_pool": "AGY_NEON",
            },
        ),
    },
    "local-codebuddy": {
        "glm-5.3": (
            "local-codebuddy-glm-5_3",
            {
                "provider_tier": "LOCAL_CODEBUDDY",
                "display_name": "GLM-5.3 via CodeBuddy",
                "api_model_id": "glm-5.3",
                "max_input_tokens": 300_000,
                "max_output_tokens": 65_536,
                "context_window": 300_000,
                "thinking": ["high", "low", "low"],
                "capability": "high",
                "quota_pool": "CODEBUDDY_CREDITS",
            },
        ),
        "hy4-preview": (
            "local-codebuddy-hy4-preview",
            {
                "provider_tier": "LOCAL_CODEBUDDY",
                "display_name": "Hunyuan 4 Preview via CodeBuddy",
                "api_model_id": "hy4-preview",
                "max_input_tokens": 300_000,
                "max_output_tokens": 64_000,
                "context_window": 300_000,
                "thinking": ["high", "high", "low"],
                "capability": "medium",
                "quota_pool": "CODEBUDDY_CREDITS",
            },
        ),
        "kimi-k3": (
            "local-codebuddy-kimi-k3",
            {
                "provider_tier": "LOCAL_CODEBUDDY",
                "display_name": "Kimi K3 via CodeBuddy",
                "api_model_id": "kimi-k3",
                "max_input_tokens": 256_000,
                "max_output_tokens": 65_536,
                "context_window": 256_000,
                "thinking": ["high", "low", "low"],
                "capability": "high",
                "quota_pool": "CODEBUDDY_CREDITS",
            },
        ),
        "minimax-m3": (
            "local-codebuddy-minimax-m3",
            {
                "provider_tier": "LOCAL_CODEBUDDY",
                "display_name": "MiniMax-M3 via CodeBuddy",
                "api_model_id": "minimax-m3",
                "max_input_tokens": 200_000,
                "max_output_tokens": 65_536,
                "context_window": 200_000,
                "thinking": ["high", "low", "low"],
                "capability": "medium",
                "quota_pool": "CODEBUDDY_CREDITS",
            },
        ),
    },
}

#: Provider ids a saved route may still carry from before a rename.
LEGACY_PROVIDER_IDS: Mapping[str, str] = {"local-workbuddy": "local-codebuddy"}

#: Packaged models a sync removed, and the one a saved route moves to. A model
#: that is gone with no entry here falls back to the provider's default.
LEGACY_LOCAL_MODELS: Mapping[str, Mapping[str, str]] = {
    "local-codex": {
        "gpt-5.6-luna": "gpt-6-luna",
        "gpt-5.6-terra": "gpt-6-luna",
        "gpt-5.6-sol": "gpt-6.1-sol",
        "gpt-6-sol": "gpt-6.1-sol",
        "gpt-6-astra": "gpt-6.1-sol",
    },
    "local-agy": {
        "claude-opus-4-6-thinking": "claude-sonnet-5-5",
        "gemini-3.7-flash": "gemini-3.8-flash",
        "gemini-3.6-flash": "gemini-3.8-flash",
    },
    "local-codebuddy": {
        "deepseek-v4-flash": "deepseek-v4.1-flash",
    },
}

# Providers reached over HTTP with an API key, in the order the desktop offers
# them. `keyName` is what gates selecting the provider at all; `baseUrlName` is
# the endpoint its transport reads. The two compat entries exist so a
# third-party endpoint can be routed to without overwriting the address of the
# official service that speaks the same dialect. `fixedBaseUrl` is an internal
# constant for named services; it has no editable setting or snapshot field.
#
# `groupId` folds several rows into one line of the desktop's provider list.
# The two Gemini pools are one service to the person configuring it -- same
# console, same endpoint, one Key each -- but they cannot be one provider here:
# the packaged catalog gives them different tiers and different rosters (the
# free pool carries seven models, the paid one two), and a route is stored as
# the tier it runs on. So they stay two providers with one label, and the
# desktop asks which pool is the enabled one rather than which of two Geminis
# this is.
API_PROVIDER_SPECS: tuple[dict[str, Any], ...] = (
    {"id": "gemini-free", "label": "Gemini 免费池", "mode": "select", "keyName": "GEMINI_FREE", "baseUrlName": "GEMINI_BASE_URL", "customEndpoint": False, "groupId": "gemini", "groupLabel": "Gemini", "tierLabel": "免费池"},
    {"id": "gemini-paid", "label": "Gemini 付费池", "mode": "select", "keyName": "GEMINI_PAID", "baseUrlName": "GEMINI_BASE_URL", "customEndpoint": False, "groupId": "gemini", "groupLabel": "Gemini", "tierLabel": "付费池"},
    {"id": "openai", "label": "OpenAI", "mode": "input", "keyName": "OPENAI_API_KEY", "baseUrlName": "OPENAI_BASE_URL", "customEndpoint": False},
    {"id": "anthropic", "label": "Anthropic", "mode": "input", "keyName": "ANTHROPIC_API_KEY", "baseUrlName": "ANTHROPIC_BASE_URL", "customEndpoint": False},
    {"id": "deepseek", "label": "DeepSeek", "mode": "input", "keyName": "DEEPSEEK_API_KEY", "baseUrlName": "", "customEndpoint": False, "fixedBaseUrl": "https://api.deepseek.com"},
    {"id": "kimi", "label": "Kimi", "mode": "input", "keyName": "KIMI_API_KEY", "baseUrlName": "", "customEndpoint": False, "fixedBaseUrl": "https://api.moonshot.cn/v1"},
    {"id": "aliyun-bailian", "label": "阿里云百炼", "mode": "input", "keyName": "DASHSCOPE_API_KEY", "baseUrlName": "", "customEndpoint": False, "fixedBaseUrl": "https://dashscope.aliyuncs.com/compatible-mode/v1"},
    {"id": "openai-compat", "label": "OpenAI 兼容提供商", "mode": "input", "keyName": "OPENAI_COMPAT_API_KEY", "baseUrlName": "OPENAI_COMPAT_BASE_URL", "customEndpoint": True},
    {"id": "anthropic-compat", "label": "Anthropic 兼容提供商", "mode": "input", "keyName": "ANTHROPIC_COMPAT_API_KEY", "baseUrlName": "ANTHROPIC_COMPAT_BASE_URL", "customEndpoint": True},
)
API_PROVIDER_BY_ID = {spec["id"]: spec for spec in API_PROVIDER_SPECS}
LLM_KEY_NAMES = frozenset(spec["keyName"] for spec in API_PROVIDER_SPECS)
# Providers whose endpoint is the user's own: without a base URL there is
# nothing to call, and the generated catalog row would not even parse.
CUSTOM_ENDPOINT_PROVIDERS = frozenset(
    spec["id"] for spec in API_PROVIDER_SPECS if spec["customEndpoint"]
)
# Which packaged HTTP transport carries each provider, and the tier its
# generated catalog rows are grouped under.
_HTTP_TRANSPORTS: Mapping[str, tuple[str, str]] = {
    "openai": ("openai_compat", "NONOKA_OPENAI"),
    "anthropic": ("anthropic", "NONOKA_ANTHROPIC"),
    "deepseek": ("openai_compat", "NONOKA_DEEPSEEK"),
    "kimi": ("openai_compat", "NONOKA_KIMI"),
    "aliyun-bailian": ("openai_compat", "NONOKA_DASHSCOPE"),
    "openai-compat": ("openai_compat", "NONOKA_OPENAI_COMPAT"),
    "anthropic-compat": ("anthropic", "NONOKA_ANTHROPIC_COMPAT"),
    "nonoka-cloud": ("openai_compat", "NONOKA_CLOUD"),
}

# The free model a signed-in Nonoka Cloud account is served at `/v1/llm` on the
# Tokyo server (nonoka-sub-cloud/tokyo_llm), an OpenAI-compatible relay that
# checks the key against the backend. Nobody types its
# credentials: the desktop hands the signed-in account over whenever it changes
# (`FineSubSettings.set_cloud_account`), so the two settings are neither in
# KEY_SPECS nor in SETTING_NAMES and SaveKeys refuses them. Signed out, both are
# empty, the provider is unavailable and its catalog row is not emitted.
#
# `models` is what the desktop offers, the default first. The backend only
# serves the models in its `LLM_MODEL` allow list and rewrites any other name
# to its own default; this list is kept to that allow list, in the same order,
# so the limits the engine plans windows against are the ones of the model that
# answers. GPT-6.1 Terra leads since 2026-11-07: GPT-6 Luna through the relay
# hung and dropped often enough to fail long correction runs. Luna was then
# withdrawn on 2026-11-08: the relay's Responses-to-chat converter answers a
# streamed, reasoning, full-size request with `502 unsupported native Responses
# stream event` (code `native_response_conversion_failed`), and the backend
# always streams. A saved route naming it moves to the default
# (`migrate_model_config`).
CLOUD_PROVIDER_ID = "nonoka-cloud"
CLOUD_KEY_NAME = "NONOKA_CLOUD_KEY"
CLOUD_BASE_URL_NAME = "NONOKA_CLOUD_BASE_URL"
CLOUD_LLM_BASE_URL = "https://cloud-jp.nonoka.online/v1/llm"
CLOUD_PROVIDER: Mapping[str, Any] = {
    "label": "Nonoka Cloud",
    "models": (
        "gpt-6.1-terra",
        "MiniMax-M3",
        "MiniMax-M3.1-flash",
        "MiniMax-M3.1",
        "mimo-v2.6-flash",
        "glm-5.3-flash",
        "deepseek-v4.1-flash",
        "doubao-seed-2.0-lite",
        "kimi-k2.6",
        "qwen3.8-flash",
    ),
    "note": "由 Nonoka Cloud 账号免费提供，无需 API Key，也不消耗转写余量；退出登录后不可用。",
}
#: What a cloud key may contain: the backend's own key pattern. Anything else
#: could not have signed in, and a `,` or `:` would split in the env file.
_CLOUD_KEY_PATTERN = re.compile(r"^[A-Za-z0-9._~-]{1,256}$")

#: The saved keys each half of FineSub is handed, as process variables (plan 4.3
#: item 23): the transcription half only the Hugging Face token; the correction
#: half the LLM and search keys and the Nonoka Cloud key. Nonoka's own engine
#: keys (`nonoka_x.config` registrations) are never handed to FineSub.
TRANSCRIPTION_SECRET_NAMES: tuple[str, ...] = ("HF_TOKEN",)
CORRECTION_SECRET_NAMES: tuple[str, ...] = (
    *(spec["name"] for spec in KEY_SPECS if spec["name"] not in TRANSCRIPTION_SECRET_NAMES),
    CLOUD_KEY_NAME,
)

#: The packaged catalog tier of each Gemini key pool.
_GEMINI_TIERS: Mapping[str, str] = {"gemini-free": "GEMINI_FREE", "gemini-paid": "GEMINI_PAID"}

#: Gemini API models Nonoka offers on top of FineSub 0.7.0's quality-locked
#: `model_catalog.toml`. Each entry is `(fact_id, catalog_row)` in FineSub's
#: `model_catalog.toml` schema, shown ahead of the packaged rows in each pool.
#: `FineSubSettings.llm_context` emits a row into the derived
#: `model_catalog.toml` only when a saved route selects it; `bridge.py` wires
#: unclaimed Gemini rows as `gemini_rest` targets and enables `thinkingLevel`
#: for Gemini 4 without modifying any locked engine file.
GEMINI_OVERLAY_MODELS: Mapping[str, Mapping[str, tuple[str, Mapping[str, Any]]]] = {
    "gemini-free": {
        "gemini/gemini-4-hydrogen": (
            "gemini-free-4-hydrogen",
            {
                "provider_tier": "GEMINI_FREE",
                "display_name": "4 Hydrogen",
                "api_model_id": "gemini/gemini-4-hydrogen",
                "max_input_tokens": 194_000,
                "supports_audio": True,
                "supports_video": True,
                "supports_native_search": False,
                "thinking": ["medium", "medium", "low"],
                "rpm": 5,
                "tpm": 250_000,
                "rpd": 20,
                "is_free": True,
                "capability": "medium",
            },
        ),
    },
    "gemini-paid": {
        "gemini/gemini-4-hydrogen": (
            "gemini-paid-4-hydrogen",
            {
                "provider_tier": "GEMINI_PAID",
                "display_name": "4 Hydrogen",
                "api_model_id": "gemini/gemini-4-hydrogen",
                "max_input_tokens": 1_048_576,
                "context_window": 1_048_576,
                "supports_audio": True,
                "supports_video": True,
                "thinking": ["medium", "medium", "low"],
                "rpm": 1000,
                "capability": "medium",
            },
        ),
        "gemini/gemini-4-neon": (
            "gemini-paid-4-neon",
            {
                "provider_tier": "GEMINI_PAID",
                "display_name": "4 Neon",
                "api_model_id": "gemini/gemini-4-neon",
                "max_input_tokens": 1_048_576,
                "context_window": 1_048_576,
                "supports_audio": True,
                "supports_video": True,
                "thinking": ["medium", "medium", "low"],
                "rpm": 1000,
                "capability": "high",
            },
        ),
        "gemini/gemini-4-argon": (
            "gemini-paid-4-argon",
            {
                "provider_tier": "GEMINI_PAID",
                "display_name": "4 Argon",
                "api_model_id": "gemini/gemini-4-argon",
                "max_input_tokens": 1_048_576,
                "context_window": 1_048_576,
                "supports_audio": True,
                "supports_video": True,
                "thinking": ["medium", "medium", "low"],
                "rpm": 1000,
                "capability": "high",
            },
        ),
        "gemini/gemini-4-carbon": (
            "gemini-paid-4-carbon",
            {
                "provider_tier": "GEMINI_PAID",
                "display_name": "4 Carbon",
                "api_model_id": "gemini/gemini-4-carbon",
                "max_input_tokens": 1_048_576,
                "context_window": 1_048_576,
                "supports_audio": True,
                "supports_video": True,
                "thinking": ["medium", "medium", "low"],
                "rpm": 1000,
                "capability": "high",
            },
        ),
    },
}

#: Older packaged Gemini models removed from the desktop picker, and the model
#: a saved route moves to. Their rows stay intact in FineSub's locked catalog
#: for internal uses (Gemma search, media probe).
LEGACY_GEMINI_MODELS: Mapping[str, Mapping[str, str]] = {
    "gemini-free": {
        "gemini/gemini-3.1-flash-lite": "gemini/gemini-3.5-flash-lite",
        "gemini/gemini-2.5-flash": "gemini/gemini-3.5-flash-lite",
        "gemini/gemma-4-31b-it": "gemini/gemini-3.5-flash-lite",
    },
}

MODEL_PROVIDERS = frozenset(set(API_PROVIDER_BY_ID) | set(LOCAL_AGENT_PROVIDERS) | {CLOUD_PROVIDER_ID})
MODEL_ROUTE_SPECS: tuple[dict[str, str], ...] = (
    {"id": "correction", "label": "纠错与翻译"},
    {"id": "research", "label": "背景准备与资料研究"},
    {"id": "knowledge", "label": "知识处理"},
)
#: One desktop row can own more than one engine task group. `correction`
#: splits into a `-mm` and a `-text` cell upstream, and a pin binds *both*:
#: `[llm.preferred_targets]` replaces the bound chain outright
#: (`_preferred_bindings`, owner decision 2026-08-28), so leaving the `-mm`
#: half unpinned would quietly serve a multimodal correction from a provider
#: the user did not choose. The desktop instead refuses the combination up
#: front, in the transcription dialog.
#:
#: FineSub 0.6.0 folded the window planning and search-judge rounds into the
#: preparation task, which routes through `research`; those two groups no
#: longer exist (see `LEGACY_ROUTES`).
TASK_GROUPS_BY_ROUTE: dict[str, tuple[str, ...]] = {
    "correction": ("correction-mm", "correction-text"),
    "research": ("research",),
    "knowledge": ("knowledge",),
}
#: Routes an older build saved, with the engine task groups it pinned. Their
#: keys are cleared from the config so no stale pin names a group the engine
#: no longer knows; a saved `planning` choice moves to `research`, which now
#: does that work, when `research` has none of its own.
LEGACY_ROUTES: Mapping[str, tuple[str, ...]] = {
    "planning": ("planning-mm", "planning-text"),
    "search_judge": ("search_judge",),
}

MODEL_SETTING_NAMES = frozenset(
    {"LLM_DEFAULT_PROVIDER", "LLM_DEFAULT_MODEL"}
    | {
        f"LLM_ROUTE_{spec['id'].upper()}_{field}"
        for spec in MODEL_ROUTE_SPECS
        for field in ("PROVIDER", "MODEL")
    }
)
SETTING_NAMES = SETTING_NAMES | MODEL_SETTING_NAMES

_MODEL_SETTING_TO_CONFIG = {
    "LLM_DEFAULT_PROVIDER": "default_provider",
    "LLM_DEFAULT_MODEL": "default_model",
    **{
        f"LLM_ROUTE_{spec['id'].upper()}_{field.upper()}": f"{spec['id']}_{field}"
        for spec in MODEL_ROUTE_SPECS
        for field in ("provider", "model")
    },
}

def _catalog_thinking(value: str) -> bool | list[str] | list[int]:
    """A `ModelSpec.thinking` string as the TOML catalog's typed `thinking` key."""

    if value in {"true", "false"}:
        return value == "true"
    parts = [part.strip() for part in value.split(",")]
    return [int(part) for part in parts] if all(part.isdigit() for part in parts) else parts


def _entries(value: str) -> list[tuple[str, str]]:
    cleaned = value.strip()
    if cleaned.startswith("{") and cleaned.endswith("}"):
        cleaned = cleaned[1:-1]
    if not cleaned:
        return []
    parts = [part.strip() for part in cleaned.split(",") if part.strip()]
    mapped: list[tuple[str, str]] = []
    for part in parts:
        if ":" not in part:
            mapped = []
            break
        label, key = part.split(":", 1)
        label = label.strip().strip("\"'")
        key = key.strip().strip("\"'")
        if label and key:
            mapped.append((label, key))
    if mapped and len(mapped) == len(parts):
        return mapped
    return [("", part.strip("\"'")) for part in parts if part.strip("\"'")]


def _secrets_module():
    from nonoka_x.config import secrets

    return secrets


#: Where the packaged model catalog comes from (`engine_catalog`): set by the sidecar to ask its
#: resident correction process; when unset, a short-lived correction process is asked once.
_CATALOG_SOURCE: list[Any] = []
_CATALOG: list[dict[str, Any]] = []


def set_catalog_source(source: Any) -> None:
    """Answer `engine_catalog()` with `source()` from now on (and forget what was read before)."""

    _CATALOG_SOURCE[:] = [source] if source is not None else []
    _CATALOG.clear()
    _local_agent_targets.cache_clear()


def _ask_correction_package() -> dict[str, Any]:
    """The correction package's packaged catalog, from a process of its own (`catalog`)."""

    from . import engine_host

    try:
        return dict(engine_host.ask_once("correction", "catalog") or {})
    except engine_host.PackageUnavailable:
        return {}


def engine_catalog() -> dict[str, Any]:
    """The correction engine's packaged model catalog: `gemini` entries and the `local_agent` roster by tier.

    Read once per process (the packaged declaration does not change while
    the package is installed); empty when no correction package is installed,
    so the settings page offers none of its models.
    """

    if not _CATALOG:
        source = _CATALOG_SOURCE[0] if _CATALOG_SOURCE else _ask_correction_package
        try:
            _CATALOG.append(dict(source() or {}))
        except Exception as error:  # noqa: BLE001 - no catalog is an answer the page can show
            print(f"engine catalog unavailable: {error}", file=sys.stderr, flush=True)
            return {}
    return _CATALOG[0]


@dataclass(frozen=True)
class CatalogModel:
    """One packaged model of the correction engine's catalog, as the settings page reads it."""

    api_model_id: str
    display_name: str
    supports_audio: bool
    supports_video: bool
    supports_native_search: bool


@lru_cache(maxsize=4)
def _local_agent_targets(provider: str) -> tuple[tuple[Any, str], ...]:
    """(model, target id) for the models a local-agent provider offers, in the
    order its roster lists them. Since FineSub 0.7.0 a model has one target,
    used with or without search. Empty when the correction engine is not
    installed.
    """

    spec = LOCAL_AGENT_PROVIDERS[provider]
    # No user overlay: the packaged declaration is the only source of these
    # targets, and reading it this way keeps a broken user route out of the
    # settings snapshot.
    roster = (engine_catalog().get("local_agent") or {}).get(spec["tier"])
    if roster is None:
        return ()
    by_model = {
        item["api_model_id"]: (
            CatalogModel(
                api_model_id=item["api_model_id"],
                display_name=item["display_name"],
                supports_audio=bool(item["supports_audio"]),
                supports_video=bool(item["supports_video"]),
                supports_native_search=bool(item["supports_native_search"]),
            ),
            item["target_id"],
        )
        for item in roster
    }
    for api_model_id, (fact_id, row) in LOCAL_AGENT_OVERLAY_MODELS.get(provider, {}).items():
        if api_model_id in by_model:
            continue
        by_model[api_model_id] = (
            CatalogModel(
                api_model_id=api_model_id,
                display_name=str(row.get("display_name") or api_model_id),
                supports_audio=bool(row.get("supports_audio", False)),
                supports_video=bool(row.get("supports_video", False)),
                supports_native_search=bool(row.get("supports_native_search", True)),
            ),
            fact_id,
        )
    missing = [model for model in spec["models"] if model not in by_model]
    if missing:
        raise ValueError(
            f"{provider} offers models the engine catalog does not declare: {missing}"
        )
    return tuple(by_model[model] for model in spec["models"])


def local_agent_executable(provider: str) -> Path | None:
    """The CLI a local-agent provider runs on, PATH first.

    Whether the CLI is *usable* is not decided here: the engine probes the
    executable for the isolation flags a call needs. This only answers where
    it is — and it has to answer for installs PATH cannot see, because the
    engine resolves its driver by name (`shutil.which("codex")`). An install
    found off PATH is therefore put back on the worker's PATH by
    `local_agent_path_entries`, or the engine would still miss it.
    """

    spec = LOCAL_AGENT_PROVIDERS[provider]
    found = shutil.which(spec["command"])
    if found:
        return Path(found)
    patterns = spec.get("windows_app_globs") or ()
    root = os.environ.get("LOCALAPPDATA", "")
    if not patterns or os.name != "nt" or not root:
        return None
    candidates = [
        path
        for pattern in patterns
        for path in Path(root).glob(pattern)
        # A half-written update stages itself next to the live install.
        if path.is_file() and not path.parent.name.startswith(".staging")
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda path: path.stat().st_mtime)


def local_agent_path_entries() -> list[str]:
    """Directories the worker needs on PATH to reach the local agent CLIs.

    Only installs that are not on PATH already: everything else is the
    environment the user's shell would give the CLI anyway.
    """

    entries: list[str] = []
    for provider, spec in LOCAL_AGENT_PROVIDERS.items():
        # `path_entry: False` is for a CLI whose entry point is not an
        # executable the engine could launch from PATH; adding its directory
        # would only advertise a name nothing can run.
        if not spec.get("path_entry", True):
            continue
        if shutil.which(spec["command"]):
            continue
        executable = local_agent_executable(provider)
        if executable is None:
            continue
        directory = str(executable.parent)
        if directory not in entries:
            entries.append(directory)
    return entries


def _first_model(models: list[dict[str, Any]]) -> str:
    return str(models[0]["id"]) if models else ""


def _local_agent_detected(provider: str) -> bool:
    return local_agent_executable(provider) is not None


def _builtin_model_options() -> dict[str, list[dict[str, Any]]]:
    tiers = {"GEMINI_FREE": "gemini-free", "GEMINI_PAID": "gemini-paid"}
    result: dict[str, list[dict[str, Any]]] = {provider: [] for provider in MODEL_PROVIDERS}
    packaged_gemini = engine_catalog().get("gemini") or ()
    for entry in packaged_gemini:
        provider = tiers.get(entry["provider_tier"])
        if provider is None or entry["self_reported"]:
            continue
        if entry["api_model_id"] in LEGACY_GEMINI_MODELS.get(provider, {}):
            continue
        result[provider].append(
            {
                "id": entry["api_model_id"],
                "label": entry["display_name"],
                "supportsAudio": entry["supports_audio"],
                "supportsVideo": entry["supports_video"],
                "supportsNativeSearch": entry["supports_native_search"],
            }
        )
    if packaged_gemini:
        for provider, overlays in GEMINI_OVERLAY_MODELS.items():
            existing = {item["id"] for item in result[provider]}
            prepended = [
                {
                    "id": api_model_id,
                    "label": str(row.get("display_name") or api_model_id),
                    "supportsAudio": bool(row.get("supports_audio", False)),
                    "supportsVideo": bool(row.get("supports_video", False)),
                    "supportsNativeSearch": bool(row.get("supports_native_search", True)),
                }
                for api_model_id, (_fact_id, row) in overlays.items()
                if api_model_id not in existing
            ]
            result[provider] = prepended + result[provider]
    result[CLOUD_PROVIDER_ID] = [
        {
            "id": model,
            "label": model_spec_for(model).display_name or model,
            # The relay is text-only, like every HTTP transport here.
            "supportsAudio": False,
            "supportsVideo": False,
            "supportsNativeSearch": False,
        }
        for model in CLOUD_PROVIDER["models"]
    ]
    for provider in LOCAL_AGENT_PROVIDERS:
        result[provider] = [
            {
                "id": fact.api_model_id,
                "label": fact.display_name,
                "supportsAudio": fact.supports_audio,
                "supportsVideo": fact.supports_video,
                "supportsNativeSearch": fact.supports_native_search,
            }
            for fact, _target_id in _local_agent_targets(provider)
        ]
    return result


def _route_from_config(config: Mapping[str, Any], prefix: str) -> dict[str, str]:
    provider = str(config.get(f"{prefix}_provider") or "").strip()
    model = str(config.get(f"{prefix}_model") or "").strip()
    return {"provider": provider, "model": model}


def migrate_model_config(config: Mapping[str, Any]) -> dict[str, str | None]:
    """The `[nonoka_models]` writes that bring a saved config up to date.

    Pure: the caller decides whether to persist them. Empty when nothing is
    stale, so applying it again is a no-op. Four kinds of staleness, all left
    by an engine sync rather than by the user:

    * a route that no longer exists (`LEGACY_ROUTES`) -- cleared, its
      `planning` choice moved to `research` when that has none;
    * a renamed provider (`LEGACY_PROVIDER_IDS`);
    * a local-agent model the engine stopped packaging -- moved to its named
      successor (`LEGACY_LOCAL_MODELS`), else to the provider's default, so a
      saved route never names a target the routing table cannot build;
    * a Nonoka Cloud model the backend no longer serves -- moved to the cloud
      default, which is the model that would answer anyway, so the windows
      are planned against its limits and the next save is not refused.
    """

    current = {key: value for key, value in config.items() if value not in (None, "")}
    updates: dict[str, str | None] = {}
    research = _route_from_config(current, "research")
    planning = _route_from_config(current, "planning")
    if not research["provider"] and planning["provider"] and planning["model"]:
        updates["research_provider"] = planning["provider"]
        updates["research_model"] = planning["model"]
    for route in LEGACY_ROUTES:
        for field in ("provider", "model"):
            if f"{route}_{field}" in current:
                updates[f"{route}_{field}"] = None
    current.update(updates)
    for prefix in ("default", *(spec["id"] for spec in MODEL_ROUTE_SPECS)):
        route = _route_from_config(current, prefix)
        provider = LEGACY_PROVIDER_IDS.get(route["provider"], route["provider"])
        if provider != route["provider"]:
            updates[f"{prefix}_provider"] = provider
        if not route["model"]:
            continue
        retired_gemini = LEGACY_GEMINI_MODELS.get(provider, {}).get(route["model"])
        if retired_gemini is not None:
            updates[f"{prefix}_model"] = retired_gemini
            continue
        spec = CLOUD_PROVIDER if provider == CLOUD_PROVIDER_ID else LOCAL_AGENT_PROVIDERS.get(provider)
        if spec is None:
            continue
        model = LEGACY_LOCAL_MODELS.get(provider, {}).get(route["model"], route["model"])
        if model not in spec["models"]:
            model = spec["models"][0]
        if model != route["model"]:
            updates[f"{prefix}_model"] = model
    return updates


def _migrated(config: Mapping[str, Any]) -> dict[str, Any]:
    """`config` with `migrate_model_config` applied, cleared keys removed."""

    merged = {**config, **migrate_model_config(config)}
    return {key: value for key, value in merged.items() if value is not None}


def _cloud_signed_in(env_values: Mapping[str, str]) -> bool:
    return bool(env_values.get(CLOUD_KEY_NAME, "").strip() and env_values.get(CLOUD_BASE_URL_NAME, "").strip())


def _provider_usable(provider: Mapping[str, Any], base_urls: Mapping[str, str]) -> bool:
    """该提供商现在能不能真的发起调用。

    API 提供商要有 Key，兼容端点还要有地址；本地 Agent 不要 Key，但 CLI 得
    真的在这台机器上；Nonoka Cloud 要求已登录云端账号。
    """
    if not provider["requiresKey"]:
        return bool(provider["available"])
    if not provider["keyConfigured"]:
        return False
    return not provider["customEndpoint"] or bool(base_urls.get(provider["baseUrlName"], ""))


def _llm_ready(
    route: Mapping[str, str],
    providers: list[dict[str, Any]],
    base_urls: Mapping[str, str],
) -> bool:
    """全局模型是否配置到位。

    「机器上装了 codex」不等于「配置了模型」：只有保存下来的全局路由既选好
    了提供商与模型、凭据或 CLI 也到位，最终字幕这类 LLM 环节才真的能跑。草稿
    没保存就不算数——快照读的是落盘的配置。
    """
    if not route.get("provider") or not route.get("model"):
        return False
    selected = next((item for item in providers if item["id"] == route["provider"]), None)
    return selected is not None and _provider_usable(selected, base_urls)


class FineSubSettings:
    """The settings page's model: Nonoka's `settings.json` and `secrets.json`, and what an engine is handed.

    The two stores (`nonoka_x.config.store`, `nonoka_x.config.secrets`) are the
    only source of truth. Nothing FineSub reads is written when a setting is
    saved: each engine process gets its keys by name (`engine_secrets`) and its
    LLM routes and endpoints (`llm_context`) in its launch context, from which
    the bridge derives FineSub's files into that process's own run directory
    (each engine package's bridge; `nonoka_x.engine_contexts`).
    """

    def __init__(self, root: str | Path) -> None:
        from nonoka_x.config.secrets import SecretStore
        from nonoka_x.config.store import SettingsStore

        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.settings_store = SettingsStore.in_dir(self.root)
        self.secret_store = SecretStore.in_dir(self.root)
        self.legacy_model_catalog_file = self.root / "model_catalog.psv"

    def bind_environment(self) -> None:
        """Start-up: remove what older builds left behind.

        An old install's settings and keys are carried over by the migration
        (`nonoka_x.migration`, its `settings` step), which the sidecar runs
        before it opens any data (WP-M2); nothing here reads the old files.
        """

        from nonoka_x.migration.legacy import remove_transitional_engine_files

        remove_transitional_engine_files(self.root)
        # Generated by versions before engine 0.7.0, which refuses that format.
        self.legacy_model_catalog_file.unlink(missing_ok=True)

    def values(self) -> dict[str, str]:
        """Every saved key and endpoint by its settings name, opened -- what reading the old key file gave."""

        values = self.secret_store.read()
        values.update(self.settings_store.base_urls())
        cloud_url = self.settings_store.cloud().get("llmBaseUrl", "")
        if cloud_url:
            values[CLOUD_BASE_URL_NAME] = cloud_url
        return values

    def secret_values(self) -> dict[str, str]:
        """The saved keys only (`secrets.json`), opened."""

        return self.secret_store.read()

    def model_config(self) -> dict[str, str]:
        """The saved routes as `[nonoka_models]` keys (`default_provider`, `correction_model`, ...)."""

        return {
            f"{route}_{field}": value
            for route, entry in self.settings_store.routes().items()
            for field, value in entry.items()
        }

    def _save_model_config(self, updates: Mapping[str, str | None]) -> None:
        from nonoka_x.config.store import ROUTE_FIELDS, ROUTES, set_route_field

        def apply(document: dict[str, Any]) -> None:
            for key, value in updates.items():
                route, _, field = key.rpartition("_")
                if route in ROUTES and field in ROUTE_FIELDS:
                    set_route_field(document, route, field, value)
                # Anything else is a route an older build had (`LEGACY_ROUTES`):
                # never stored here, so clearing it is already done.

        self.settings_store.change(apply)

    def _url_values(self) -> dict[str, str]:
        """The saved endpoints by setting name (`values` without opening the keys)."""

        values = dict(self.settings_store.base_urls())
        cloud_url = self.settings_store.cloud().get("llmBaseUrl", "")
        if cloud_url:
            values[CLOUD_BASE_URL_NAME] = cloud_url
        return values

    def engine_secrets(self, names: tuple[str, ...]) -> dict[str, str]:
        """The saved keys among `names`, opened: what one engine package declared it needs."""

        values = self.secret_store.read()
        return {name: values[name] for name in names if values.get(name)}

    def refresh_model_routes(self) -> None:
        """Write back what an engine sync made stale in the saved routes (`migrate_model_config`).

        At start-up, so a route the engine no longer knows is moved before
        anything is planned against it. Idempotent: same routes, no write.
        """

        migration = migrate_model_config(self.model_config())
        if migration:
            self._save_model_config(migration)

    def telemetry_models(self, request: dict) -> dict | None:
        from .telemetry import model_snapshot

        return model_snapshot(_migrated(self.model_config()), request)

    def snapshot(self) -> dict[str, Any]:
        secrets = _secrets_module()
        values = self.values()
        states = self.secret_store.status()
        # What the routes mean after an engine sync, even before startup has
        # written the migration back (`refresh_model_routes`).
        model_config = _migrated(self.model_config())
        model_options = _builtin_model_options()
        keys: list[dict[str, Any]] = []
        for spec in KEY_SPECS:
            name = spec["name"]
            entries = _entries(values.get(name, ""))
            keys.append(
                {
                    **spec,
                    "configured": bool(entries),
                    "count": len(entries),
                    "masked": [
                        {"name": label, "value": secrets.masked(key)}
                        for label, key in entries
                    ],
                    "storage": states.get(name, "missing"),
                }
            )
        configured_states = [item["storage"] for item in keys if item["configured"]]
        if not configured_states:
            protection = "empty"
        elif all(state == "protected" for state in configured_states):
            protection = "protected"
        elif any(state == "unreadable" for state in configured_states):
            protection = "unreadable"
        else:
            protection = "plaintext"
        base_urls = [
            {
                **spec,
                "value": values.get(spec["name"], "").strip().rstrip("/"),
                "customized": bool(values.get(spec["name"], "").strip()),
            }
            for spec in BASE_URL_SPECS
        ]
        base_url_values = {item["name"]: item["value"] for item in base_urls}
        providers = [
            # First: the one model that needs nothing but the account.
            {
                "id": CLOUD_PROVIDER_ID,
                "kind": "cloud",
                "label": CLOUD_PROVIDER["label"],
                "mode": "select",
                "models": model_options[CLOUD_PROVIDER_ID],
                "defaultModel": _first_model(model_options[CLOUD_PROVIDER_ID]),
                # The account is the credential, so no key is asked for; being
                # signed in is the gate, the way the CLI is for a local agent.
                "requiresKey": False,
                "available": _cloud_signed_in(values),
                "keyName": "",
                "baseUrlName": "",
                "customEndpoint": False,
                "keyConfigured": False,
                "groupId": "",
                "groupLabel": "",
                "tierLabel": "",
                "note": CLOUD_PROVIDER["note"],
            },
            # `defaultModel` is what selecting a provider fills in. For the
            # packaged rosters it is the first entry, which is the order the
            # option lists are built in.
            *[
                {
                    "id": spec["id"],
                    "kind": "api",
                    "label": spec["label"],
                    "mode": spec["mode"],
                    "models": model_options[spec["id"]],
                    "defaultModel": _first_model(model_options[spec["id"]]),
                    "requiresKey": True,
                    "available": True,
                    "keyName": spec["keyName"],
                    "baseUrlName": spec["baseUrlName"],
                    # A compat endpoint has no official address behind it, so
                    # its Base URL is part of configuring it.
                    "customEndpoint": spec["customEndpoint"],
                    "keyConfigured": bool(_entries(values.get(spec["keyName"], ""))),
                    "groupId": spec.get("groupId", ""),
                    "groupLabel": spec.get("groupLabel", ""),
                    "tierLabel": spec.get("tierLabel", ""),
                    "note": "",
                }
                for spec in API_PROVIDER_SPECS
            ],
            *[
                {
                    "id": provider,
                    "kind": "local",
                    "label": spec["label"],
                    "mode": "select",
                    "models": model_options[provider],
                    "defaultModel": _first_model(model_options[provider]),
                    # Runs on the user's own CLI subscription, so no key is
                    # ever asked for; the CLI itself is the gate.
                    "requiresKey": False,
                    "available": _local_agent_detected(provider),
                    "keyName": "",
                    "baseUrlName": "",
                    "customEndpoint": False,
                    "keyConfigured": False,
                    "groupId": "",
                    "groupLabel": "",
                    "tierLabel": "",
                    # Prose the page shows under the detection line, for what a
                    # person has to know before selecting this provider rather
                    # than after. Empty for the ones with nothing to say.
                    "note": spec.get("note", ""),
                }
                for provider, spec in LOCAL_AGENT_PROVIDERS.items()
            ],
        ]
        default_route = _route_from_config(model_config, "default")
        return {
            "schema": 1,
            "keys": keys,
            "baseUrls": base_urls,
            "modelRouting": {
                "providers": providers,
                "defaultRoute": default_route,
                "taskRoutes": [
                    {**spec, "route": _route_from_config(model_config, spec["id"])}
                    for spec in MODEL_ROUTE_SPECS
                ],
            },
            "llmKeyConfigured": any(
                item["configured"] and item["name"] in LLM_KEY_NAMES
                for item in keys
            ),
            # 已保存的全局模型是否真的能用——前端据此放行「最终字幕」。
            "llmReady": _llm_ready(default_route, providers, base_url_values),
            "retrievalKeyConfigured": any(
                item["configured"] and item["name"] in {"EXA_KEYS", "TAVILY_KEYS", "GEMINI_FREE"}
                for item in keys
            ),
            "protection": protection,
        }

    def update_keys(self, updates: Mapping[str, Any], *, extra_key_names: frozenset[str] = frozenset()) -> dict[str, Any]:
        """Save keys, endpoints and model routes (`None` or blank clears one); returns the new snapshot.

        `extra_key_names` are keys the settings page does not list but Nonoka's
        own engines read (registered through `nonoka_x.config`): stored in
        `secrets.json` like the others, never handed to FineSub.
        """

        from nonoka_x.config.store import set_section_value

        unknown = sorted(set(updates) - SETTING_NAMES - set(extra_key_names))
        if unknown:
            raise ValueError(f"Unknown FineSub key setting(s): {', '.join(unknown)}")
        normalized: dict[str, str | None] = {}
        model_updates: dict[str, str | None] = {}
        for name, value in updates.items():
            if value is None:
                if name in MODEL_SETTING_NAMES:
                    model_updates[_MODEL_SETTING_TO_CONFIG[name]] = None
                else:
                    normalized[name] = None
                continue
            if not isinstance(value, str):
                raise ValueError(f"{name} must be a string or null")
            cleaned = value.strip()
            if name in MODEL_SETTING_NAMES:
                if len(cleaned) > 256 or "|" in cleaned or any(character.isspace() for character in cleaned):
                    raise ValueError(f"{name} must be a model/provider identifier of at most 256 characters")
                model_updates[_MODEL_SETTING_TO_CONFIG[name]] = cleaned or None
                continue
            if name in BASE_URL_NAMES and cleaned:
                if len(cleaned) > 2_048:
                    raise ValueError(f"{name} exceeds the 2048 character limit")
                if "|" in cleaned:
                    raise ValueError(f"{name} contains an unsupported character")
                parsed = urlsplit(cleaned)
                if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                    raise ValueError(f"{name} must be an http(s) URL")
                if parsed.username or parsed.password or parsed.query or parsed.fragment:
                    raise ValueError(
                        f"{name} must not contain credentials, a query, or a fragment"
                    )
                cleaned = cleaned.rstrip("/")
            elif len(cleaned) > 32_768:
                raise ValueError(f"{name} exceeds the 32768 character limit")
            normalized[name] = cleaned or None
        key_updates = {name: value for name, value in normalized.items() if name not in BASE_URL_NAMES}
        url_updates = {name: value for name, value in normalized.items() if name in BASE_URL_NAMES}
        if key_updates:
            self.secret_store.update(key_updates)
        if url_updates:
            self.settings_store.change(
                lambda document: [set_section_value(document, "baseUrls", name, value) for name, value in url_updates.items()]
            )
        env_values = self.values()
        if model_updates:
            self._update_model_routing(model_updates, env_values)
        self.refresh_model_routes()
        return self.snapshot()

    def _update_model_routing(self, updates: Mapping[str, str | None], env_values: Mapping[str, str]) -> None:
        current = self.model_config()
        # The user's edits land on the migrated config, and the migration is
        # written with them so a stale key cannot outlive this save.
        updates = {**migrate_model_config(current), **updates}
        candidate = {**current, **updates}
        options = _builtin_model_options()
        # Every provider whose models come from a closed packaged list: a typo
        # here would otherwise be written out and only fail at routing time.
        allowed_models = {
            provider: {item["id"] for item in models}
            for provider, models in options.items()
            if provider.startswith("gemini-") or provider in LOCAL_AGENT_PROVIDERS or provider == CLOUD_PROVIDER_ID
        }
        for prefix in ("default", *(spec["id"] for spec in MODEL_ROUTE_SPECS)):
            route = _route_from_config(candidate, prefix)
            provider, model = route["provider"], route["model"]
            if not provider and not model:
                continue
            if provider not in MODEL_PROVIDERS:
                raise ValueError(f"Unknown LLM provider for {prefix}: {provider or '(empty)'}")
            if not model:
                raise ValueError(f"A model is required for LLM route {prefix}")
            if provider in allowed_models and model not in allowed_models[provider]:
                raise ValueError(f"Model {model!r} is not available in {provider}")
            if provider == CLOUD_PROVIDER_ID and not _cloud_signed_in(env_values):
                raise ValueError(f"Sign in to Nonoka Cloud before {provider} can be selected")
            spec = API_PROVIDER_BY_ID.get(provider)
            if spec is None:
                continue
            # A provider nobody has given credentials to cannot be routed to;
            # the desktop refuses to offer it for the same reason.
            if not _entries(env_values.get(spec["keyName"], "")):
                raise ValueError(f"An API key is required before {provider} can be selected")
            if provider in CUSTOM_ENDPOINT_PROVIDERS and not env_values.get(spec["baseUrlName"], "").strip():
                raise ValueError(f"A base URL is required before {provider} can be selected")
        self._save_model_config(updates)

    def set_cloud_account(self, backend: str, key: str) -> dict[str, Any]:
        """Record the signed-in Nonoka Cloud account, or clear it when both are empty.

        The desktop calls this on login, logout and session refresh, so it is
        a no-op when nothing changed. A running LLM worker is restarted by the
        provider when the account moved, so it is handed the new key.
        """

        from nonoka_x.config.store import set_section_value

        backend = backend.strip().rstrip("/")
        key = key.strip()
        if bool(backend) != bool(key):
            raise ValueError("backend and key are set or cleared together")
        if backend:
            parsed = urlsplit(backend)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc or "|" in backend:
                raise ValueError("backend must be an http(s) URL")
            if parsed.username or parsed.password or parsed.query or parsed.fragment:
                raise ValueError("backend must not contain credentials, a query, or a fragment")
            if not _CLOUD_KEY_PATTERN.fullmatch(key):
                raise ValueError("key is not a Nonoka Cloud key")
        wanted = {
            CLOUD_KEY_NAME: key or None,
            CLOUD_BASE_URL_NAME: CLOUD_LLM_BASE_URL if backend else None,
        }
        values = self.values()
        if all((values.get(name) or None) == value for name, value in wanted.items()):
            return {"changed": False}
        self.secret_store.update({CLOUD_KEY_NAME: wanted[CLOUD_KEY_NAME]})
        self.settings_store.change(
            lambda document: set_section_value(document, "cloud", "llmBaseUrl", wanted[CLOUD_BASE_URL_NAME])
        )
        self.refresh_model_routes()
        return {"changed": True}

    def llm_context(self) -> dict[str, Any]:
        """The `llm` part of the correction half's launch context (R0 document 1.15; the packages' `bridge.derive_llm_files`).

        - `endpoints`: one catalog row per route over an HTTP transport, with
          the model's own declared limits (`nonoka_x.model_specs`), sorted by
          provider and model; a route whose endpoint has no address (a compat
          provider whose URL was cleared, Nonoka Cloud after signing out) gets
          none, since the row would not parse, and its pin falls back.
        - `routes`: the saved routes after `migrate_model_config`.
        - `pins`: each engine task group a route binds, in order.
        - `gemini_base_url`: only when the user changed it.
        """

        model_config = _migrated(self.model_config())
        url_values = self._url_values()
        routes = {
            "default": _route_from_config(model_config, "default"),
            **{spec["id"]: _route_from_config(model_config, spec["id"]) for spec in MODEL_ROUTE_SPECS},
        }
        custom_models = sorted(
            {
                (route["provider"], route["model"])
                for route in routes.values()
                if route["provider"] in _HTTP_TRANSPORTS and route["model"]
            }
        )
        default_urls = {spec["name"]: spec["defaultValue"] for spec in BASE_URL_SPECS}
        endpoints: list[dict[str, Any]] = []
        for provider, model in custom_models:
            kind, tier = _HTTP_TRANSPORTS[provider]
            if provider == CLOUD_PROVIDER_ID:
                key_env = CLOUD_KEY_NAME
                base_url = url_values.get(CLOUD_BASE_URL_NAME, "").strip().rstrip("/")
            else:
                key_env = API_PROVIDER_BY_ID[provider]["keyName"]
                url_name = API_PROVIDER_BY_ID[provider]["baseUrlName"]
                base_url = (
                    API_PROVIDER_BY_ID[provider].get("fixedBaseUrl")
                    or url_values.get(url_name)
                    or default_urls.get(url_name, "")
                ).strip().rstrip("/")
            if not base_url:
                continue
            # The model's own declared limits, not a placeholder: windows are
            # planned against `max_output_tokens`, and a row that understates it
            # turns every window into a split-and-retry ladder (see
            # `nonoka_x.model_specs`). An id the table does not know still gets
            # the conservative default it always had, minus the reasoning
            # parameter no unknown endpoint is guaranteed to accept.
            spec = model_spec_for(model)
            # Audio and video stay false for every row here: the packaged HTTP
            # transports are text-only, and the catalog parser rejects a
            # compat-kind row that claims media support.
            endpoints.append({
                "provider": provider,
                "model": model,
                "row": {
                    "provider_tier": tier,
                    "provider_kind": kind,
                    "base_url": base_url,
                    "key_env": key_env,
                    "display_name": spec.display_name or model,
                    "api_model_id": model,
                    "max_input_tokens": spec.max_input_tokens,
                    "max_output_tokens": spec.max_output_tokens,
                    "supports_audio": False,
                    "supports_video": False,
                    "supports_native_search": False,
                    "thinking": _catalog_thinking(spec.thinking),
                    "token_scale": spec.token_scale,
                    "capability": spec.quality_score,
                },
            })
        overlay_models = {**LOCAL_AGENT_OVERLAY_MODELS, **GEMINI_OVERLAY_MODELS}
        selected_overlays = sorted(
            {
                (route["provider"], route["model"])
                for route in routes.values()
                if route["model"] in overlay_models.get(route["provider"], {})
            }
        )
        for provider, model in selected_overlays:
            fact_id, row = overlay_models[provider][model]
            endpoints.append({
                "provider": provider,
                "model": model,
                "target": fact_id,
                "row": dict(row),
            })

        def pin(group: str, route: Mapping[str, str]) -> dict[str, Any]:
            provider = route.get("provider", "")
            entry: dict[str, Any] = {"group": group, "provider": provider, "model": route.get("model", "")}
            if provider in _GEMINI_TIERS:
                entry.update(kind="gemini", tier=_GEMINI_TIERS[provider])
                overlay = GEMINI_OVERLAY_MODELS.get(provider, {}).get(entry["model"])
                if overlay is not None:
                    entry["target"] = overlay[0]
            elif provider in LOCAL_AGENT_PROVIDERS:
                spec = LOCAL_AGENT_PROVIDERS[provider]
                entry.update(kind="local_agent", tier=spec["tier"], offered=list(spec["models"]))
                overlay = LOCAL_AGENT_OVERLAY_MODELS.get(provider, {}).get(entry["model"])
                if overlay is not None:
                    entry["target"] = overlay[0]
            elif provider in _HTTP_TRANSPORTS:
                entry.update(kind="http")
            return entry

        # `[llm.preferred_targets]` is upstream 0.5.0's own overlay. A task
        # group an older engine had (`LEGACY_ROUTES`) is never pinned: it would
        # name a group the routing table refuses to load.
        pins = [pin("default", routes["default"])]
        for spec in MODEL_ROUTE_SPECS:
            pins.extend(pin(group, routes[spec["id"]]) for group in TASK_GROUPS_BY_ROUTE[spec["id"]])
        context: dict[str, Any] = {"endpoints": endpoints, "routes": routes, "pins": pins}
        gemini_base_url = url_values.get("GEMINI_BASE_URL", "")
        if gemini_base_url:
            context["gemini_base_url"] = gemini_base_url
        return context
