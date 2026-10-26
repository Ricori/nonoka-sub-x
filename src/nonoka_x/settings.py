"""FineSub-native local settings, exposed without revealing secret values."""

from __future__ import annotations

from functools import lru_cache
import hashlib
import os
from pathlib import Path
import re
import shutil
import tomllib
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
        # Which of the tier's packaged models the desktop offers, in display
        # order — the first is what selecting the provider fills in. The same
        # two the engine packages (FineSub 0.6.0; Codex CLI 0.160.0 or newer).
        "models": ("gpt-6.1-sol", "gpt-6-luna"),
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
        # Gemini 3.7 Flash remains the default used by the packaged agy groups.
        # The Flash rows can take media; the two Claude rows are text only and
        # share agy's Claude allowance (agy 1.2.16 or newer).
        "models": (
            "gemini-3.7-flash",
            "gemini-3.8-flash",
            "gemini-3.6-flash",
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
        # with the list that login is entitled to. These four are the rows the
        # engine packages; a plan carrying others needs a catalog row.
        "models": (
            "glm-5.3-flash",
            "hy3",
            "deepseek-v4.1-flash",
            "deepseek-v4-pro",
        ),
        "note": (
            "Hunyuan 3 每天有免费额度；用光之后会自动切到同一条线的付费额度"
            "（hy3-x）把当次任务跑完，并在结束时提示切过。不想自动扣费就选 "
            "GLM-5.3 Flash 或 DeepSeek 两行，它们本来就按积分计费。需要独立安装并"
            "登录 CodeBuddy Code CLI（2.164.0 或更新），不再使用 WorkBuddy 桌面端。"
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
    "local-agy": {"claude-opus-4-6-thinking": "claude-sonnet-5-5"},
    "local-codebuddy": {
        "hy4-preview": "hy3",
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

# The free model a signed-in Nonoka Cloud account is served at the backend's
# `/v1/llm` (nonoka-sub-cloud), an OpenAI-compatible relay. Nobody types its
# credentials: the desktop hands the signed-in account over whenever it changes
# (`FineSubSettings.set_cloud_account`), so the two settings are neither in
# KEY_SPECS nor in SETTING_NAMES and SaveKeys refuses them. Signed out, both are
# empty, the provider is unavailable and its catalog row is not emitted.
#
# `models` is what the desktop offers. The backend may pin every request to a
# model of its own (`LLM_MODEL`); this list is kept to that model so the limits
# the engine plans windows against are the ones of the model that answers.
CLOUD_PROVIDER_ID = "nonoka-cloud"
CLOUD_KEY_NAME = "NONOKA_CLOUD_KEY"
CLOUD_BASE_URL_NAME = "NONOKA_CLOUD_BASE_URL"
CLOUD_PROVIDER: Mapping[str, Any] = {
    "label": "Nonoka Cloud",
    "models": ("gpt-6-luna",),
    "note": "由 Nonoka Cloud 账号免费提供，无需 API Key，也不消耗转写余量；退出登录后不可用。",
}
#: What a cloud key may contain: the backend's own key pattern. Anything else
#: could not have signed in, and a `,` or `:` would split in the env file.
_CLOUD_KEY_PATTERN = re.compile(r"^[A-Za-z0-9._~-]{1,256}$")

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

_MODEL_CATALOG_HEADER = (
    "fact_id|provider_tier|provider_kind|base_url|key_env|display_name|"
    "api_model_id|max_input_tokens|max_output_tokens|supports_audio|"
    "supports_video|supports_native_search|thinking|token_scale|quality_score"
)


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
    from finesub_bootstrap import secrets

    return secrets


def _read_toml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    with path.open("rb") as handle:
        data = tomllib.load(handle)
    return data if isinstance(data, dict) else {}


def native_search_target(routes: Any, target_id: str) -> str | None:
    """The selected target, or its same-model twin with a search tool wired."""
    target = routes.targets[target_id]
    if not routes.target_fact(target_id).supports_native_search:
        return None
    if routes.target_profile(target_id).native_search_tool:
        return target_id
    if target.backend != "local_agent":
        return None
    return next(
        (
            candidate.id
            for candidate in routes.targets.values()
            if candidate.fact_id == target.fact_id
            and candidate.backend == target.backend
            and candidate.enabled_by == target.enabled_by
            and routes.target_profile(candidate.id).native_search_tool
        ),
        None,
    )


@lru_cache(maxsize=4)
def _local_agent_targets(provider: str) -> tuple[tuple[Any, str], ...]:
    """(fact, target id) for the models a local-agent provider offers.

    Two filters, for two different reasons. The provider's own roster decides
    *which* models are offered, in the order it lists them. The execution
    profile decides which of a model's two packaged targets is pinnable: the
    search-entitled twin stays out of saved settings. For `retrieval=native`,
    the worker selects that same model's search twin for this run only.
    """

    from finesub.llm.routing.model_routes import load_model_routes

    spec = LOCAL_AGENT_PROVIDERS[provider]
    # No user overlay: the packaged declaration is the only source of these
    # targets, and reading it this way keeps a broken user route out of the
    # settings snapshot.
    routes = load_model_routes(user_config={})
    by_model: dict[str, tuple[Any, str]] = {}
    for target in routes.targets.values():
        if target.backend != "local_agent":
            continue
        fact = routes.facts.get(target.fact_id)
        if fact is None or fact.provider_tier != spec["tier"]:
            continue
        profile = routes.execution_profiles.get(target.execution_profile)
        if profile is None or profile.native_search_tool:
            continue
        by_model[fact.api_model_id] = (fact, target.id)
    missing = [model for model in spec["models"] if model not in by_model]
    if missing:
        raise ValueError(
            f"{provider} offers models the engine catalog does not declare: {missing}"
        )
    return tuple(by_model[model] for model in spec["models"])


def _local_agent_model_map(provider: str) -> dict[str, str]:
    return {fact.api_model_id: target_id for fact, target_id in _local_agent_targets(provider)}


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
    from finesub.llm.routing.model_catalog import default_model_catalog

    tiers = {"GEMINI_FREE": "gemini-free", "GEMINI_PAID": "gemini-paid"}
    result: dict[str, list[dict[str, Any]]] = {provider: [] for provider in MODEL_PROVIDERS}
    for entry in default_model_catalog():
        provider = tiers.get(entry.provider_tier)
        if provider is None or entry.self_reported:
            continue
        result[provider].append(
            {
                "id": entry.api_model_id,
                "label": entry.display_name,
                "supportsAudio": entry.supports_audio,
                "supportsVideo": entry.supports_video,
                "supportsNativeSearch": entry.supports_native_search,
            }
        )
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
    stale, so applying it again is a no-op. Three kinds of staleness, all left
    by an engine sync rather than by the user:

    * a route that no longer exists (`LEGACY_ROUTES`) -- cleared, its
      `planning` choice moved to `research` when that has none;
    * a renamed provider (`LEGACY_PROVIDER_IDS`);
    * a local-agent model the engine stopped packaging -- moved to its named
      successor (`LEGACY_LOCAL_MODELS`), else to the provider's default, so a
      saved route never names a target the routing table cannot build.
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
        spec = LOCAL_AGENT_PROVIDERS.get(provider)
        if spec is None or not route["model"]:
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
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.env_file = self.root / "finesub.env"
        self.config_file = self.root / "finesub.toml"
        self.model_catalog_file = self.root / "model_catalog.psv"

    def bind_environment(self) -> None:
        os.environ["FINESUB_ENV_FILE"] = str(self.env_file)
        os.environ["FINESUB_CONFIG_FILE"] = str(self.config_file)
        os.environ["FINESUB_MODEL_CATALOG"] = str(self.model_catalog_file)
        os.environ["FINESUB_CHECKOUT_DATA"] = "0"
        os.environ.setdefault("FINESUB_KNOWLEDGE_ROOT", str(self.root / "knowledge"))
        os.environ.setdefault("FINESUB_STATE_DIR", str(self.root / "state"))

    def refresh_model_catalog(self) -> None:
        """Rewrite the generated catalog from the routes already on disk.

        The catalog is normally written when settings are saved, so an install
        that was configured before the model facts shipped keeps whatever the
        old code claimed -- and a wrong `max_output_tokens` is what turns every
        correction window into a split-and-retry ladder. Regenerating at startup
        makes the fix arrive with the update instead of with the user's next
        visit to the settings page. Idempotent: same routes, same file.
        """

        self._sync_model_routing(_secrets_module().read_env_file(self.env_file))

    def telemetry_models(self, request: dict) -> dict | None:
        from .telemetry import model_snapshot

        config = _read_toml(self.config_file).get("nonoka_models", {})
        return model_snapshot(_migrated(config) if isinstance(config, dict) else {}, request)

    def snapshot(self) -> dict[str, Any]:
        secrets = _secrets_module()
        values = secrets.read_env_file(self.env_file)
        states = secrets.env_status(self.env_file)
        config = _read_toml(self.config_file)
        model_config = config.get("nonoka_models", {})
        if not isinstance(model_config, Mapping):
            model_config = {}
        # What the routes mean after an engine sync, even before startup has
        # written the migration back (`refresh_model_catalog`).
        model_config = _migrated(model_config)
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

    def update_keys(self, updates: Mapping[str, Any]) -> dict[str, Any]:
        secrets = _secrets_module()
        unknown = sorted(set(updates) - SETTING_NAMES)
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
        if normalized:
            secrets.update_env_file(self.env_file, normalized)
        env_values = secrets.read_env_file(self.env_file)
        if model_updates:
            self._update_model_routing(model_updates, env_values)
        self._sync_model_routing(env_values)
        try:
            self.env_file.chmod(0o600)
        except OSError:
            # Windows protects values with DPAPI; POSIX additionally narrows
            # the fallback plaintext file to the current account.
            pass
        return self.snapshot()

    def _update_model_routing(self, updates: Mapping[str, str | None], env_values: Mapping[str, str]) -> None:
        from nonoka_x.config_file import update_config_file

        current = _read_toml(self.config_file).get("nonoka_models", {})
        if not isinstance(current, Mapping):
            current = {}
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
        update_config_file(self.config_file, {"nonoka_models": updates})

    def set_cloud_account(self, backend: str, key: str) -> dict[str, Any]:
        """Record the signed-in Nonoka Cloud account, or clear it when both are empty.

        The desktop calls this on login, logout and session refresh, so it is
        a no-op when nothing changed: the env file and the generated catalog
        are only rewritten when the account actually moved.
        """

        secrets = _secrets_module()
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
            CLOUD_BASE_URL_NAME: f"{backend}/v1/llm" if backend else None,
        }
        values = secrets.read_env_file(self.env_file)
        if all((values.get(name) or None) == value for name, value in wanted.items()):
            return {"changed": False}
        secrets.update_env_file(self.env_file, wanted)
        self._sync_model_routing(secrets.read_env_file(self.env_file))
        return {"changed": True}

    @staticmethod
    def _custom_target(provider: str, model: str) -> str:
        digest = hashlib.sha256(f"{provider}\0{model}".encode("utf-8")).hexdigest()[:12]
        return f"nonoka-{provider}-{digest}"

    def _sync_model_routing(self, env_values: Mapping[str, str]) -> None:
        from finesub_bootstrap.fsops import write_atomic

        from nonoka_x.config_file import update_config_file

        data = _read_toml(self.config_file)
        model_config = data.get("nonoka_models", {})
        if not isinstance(model_config, Mapping):
            model_config = {}
        migration = migrate_model_config(model_config)
        if migration:
            update_config_file(self.config_file, {"nonoka_models": migration})
            model_config = _migrated(model_config)
        options = _builtin_model_options()
        gemini_targets = {
            "gemini-free": {item["id"]: f"gemini-free-{item['id'].split('/')[-1].replace('.', '_')}" for item in options["gemini-free"]},
            "gemini-paid": {item["id"]: f"gemini-paid-{item['id'].split('/')[-1].replace('.', '_')}" for item in options["gemini-paid"]},
        }
        # Catalog fact IDs are authoritative; derive the Gemini target map from
        # the packaged entries instead of relying on display/model spelling.
        from finesub.llm.routing.model_catalog import default_model_catalog
        for entry in default_model_catalog():
            provider = {"GEMINI_FREE": "gemini-free", "GEMINI_PAID": "gemini-paid"}.get(entry.provider_tier)
            if provider and not entry.self_reported:
                gemini_targets[provider][entry.api_model_id] = entry.fact_id

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
        catalog_lines = [_MODEL_CATALOG_HEADER]
        emitted: set[str] = set()
        for provider, model in custom_models:
            target = self._custom_target(provider, model)
            kind, tier = _HTTP_TRANSPORTS[provider]
            if provider == CLOUD_PROVIDER_ID:
                key_env = CLOUD_KEY_NAME
                base_url = env_values.get(CLOUD_BASE_URL_NAME, "").strip().rstrip("/")
            else:
                key_env = API_PROVIDER_BY_ID[provider]["keyName"]
                url_name = API_PROVIDER_BY_ID[provider]["baseUrlName"]
                base_url = (
                    API_PROVIDER_BY_ID[provider].get("fixedBaseUrl")
                    or env_values.get(url_name)
                    or default_urls.get(url_name, "")
                ).strip().rstrip("/")
            if not base_url:
                # A compat provider whose endpoint was cleared behind the
                # route's back, or Nonoka Cloud after signing out: emitting the
                # row would make the whole catalog unparseable, so drop it and
                # let routing fall back.
                continue
            emitted.add(target)
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
            catalog_lines.append(
                "|".join(
                    [
                        target,
                        tier,
                        kind,
                        base_url,
                        key_env,
                        spec.display_name or model,
                        model,
                        str(spec.max_input_tokens),
                        str(spec.max_output_tokens),
                        "false",
                        "false",
                        "false",
                        spec.thinking,
                        f"{spec.token_scale:g}",
                        str(spec.quality_score),
                    ]
                )
            )
        write_atomic(self.model_catalog_file, "\n".join(catalog_lines) + "\n", newline="")

        def target_for(route: Mapping[str, str]) -> str | None:
            provider, model = route.get("provider", ""), route.get("model", "")
            if not provider or not model:
                return None
            if provider in gemini_targets:
                return gemini_targets[provider].get(model)
            if provider in LOCAL_AGENT_PROVIDERS:
                return _local_agent_model_map(provider).get(model)
            if provider in _HTTP_TRANSPORTS:
                # Only if the row survived above: pointing a route at a target
                # the catalog does not carry is worse than falling back.
                target = self._custom_target(provider, model)
                return target if target in emitted else None
            return None

        # `[llm.preferred_targets]` is upstream 0.5.0's own overlay; the
        # `[llm] default_target` / `task_route_*` keys this used to write were
        # read by `0003-desktop-model-routing.patch`, which the sync to 0.5.0
        # dropped. They are cleared rather than left behind: a stale key in a
        # hand-edited file reads like a live setting.
        preferred: dict[str, str | None] = {"default": target_for(routes["default"])}
        for spec in MODEL_ROUTE_SPECS:
            target = target_for(routes[spec["id"]])
            for group in TASK_GROUPS_BY_ROUTE[spec["id"]]:
                preferred[group] = target
        # Task groups an older engine had: a pin left on one would name a
        # group the routing table refuses to load.
        for groups in LEGACY_ROUTES.values():
            for group in groups:
                preferred[group] = None
        update_config_file(
            self.config_file,
            {
                "llm.preferred_targets": preferred,
                "llm": {
                    "default_target": None,
                    **{
                        f"task_route_{route}": None
                        for route in (*TASK_GROUPS_BY_ROUTE, *LEGACY_ROUTES)
                    },
                },
            },
        )
        try:
            from finesub.config import clear_config_cache
            from finesub.llm.routing.model_catalog import default_model_catalog
            from finesub.llm.routing.model_routes import default_model_routes

            clear_config_cache()
            default_model_catalog.cache_clear()
            default_model_routes.cache_clear()
        except ImportError:
            pass
