"""Which GPU tier a task request asks the engine for.

FineSub 0.5.0 replaced `--gpu-budget-gb 4|8|12|16` with `--gpu-tier`, whose
names say what a machine *is* rather than how many gigabytes to spend:
`cpu`, `entry`, `standard`, `standard_large_vram`, `high`, and `auto`, which
detects. The engine owns that list (`finesub.speech.runtime.resources`), so
this module never restates it -- it validates against it and falls back to
`auto`, which is also the engine's own default.

The legacy mapping is here because Nonoka X's own stored state outlives the
engine's option surface: a task queued before this upgrade, a saved default
request in the desktop's settings, and the cloud's serialized job payloads all
carry `gpu_budget_gb`. Dropping such a request would fail a task the user
already started, so the number is read once, converted, and said out loud.
"""

from __future__ import annotations

from typing import Any, Callable, Mapping


#: What each retired budget meant on the hardware it was offered for. `4` was
#: the "weak card" choice and `entry` is its successor; `8` was the default and
#: matches `standard`; `12` and `16` both land on the tier upstream added for
#: cards from 12GB up. Nothing maps to `high`: it now requires a card that
#: reports >=24GB, which no `--gpu-budget-gb` value ever claimed.
LEGACY_BUDGET_TIERS: dict[int, str] = {
    4: "entry",
    8: "standard",
    12: "standard_large_vram",
    16: "standard_large_vram",
}
AUTO = "auto"


def _known_tiers() -> frozenset[str] | None:
    """The engine's own list of tier names, or None when it cannot be asked.

    Not restated here: the names are the engine's, and a copy would go stale
    the next time upstream adds a tier (`standard_large_vram` arrived in
    0.5.0). When the engine is not importable -- an adapter test that stubs
    it, a sidecar started before the runtime is installed -- validation is
    skipped rather than guessed at, and an unknown name travels on to
    `resolve_gpu_tier`, whose error names the choices it does know.
    """

    try:
        from finesub.speech.runtime.resources import gpu_tier_cli_choices
    except Exception:  # noqa: BLE001 - no engine is a valid answer here
        return None
    return frozenset(gpu_tier_cli_choices())


def resolve_request_gpu_tier(
    request: Mapping[str, Any],
    *,
    warn: Callable[[str], None] | None = None,
) -> str:
    """The `gpu_tier` to hand `run_pipeline`, migrating a legacy request.

    `warn` is called at most once, with a message meant for the task log, when
    a request arrives carrying only the retired `gpu_budget_gb`.
    """

    tier = str(request.get("gpu_tier") or "").strip()
    known = _known_tiers()
    if tier:
        if known is None or tier in known:
            return tier
        if warn is not None:
            warn(
                f"未知的显卡档位 {tier!r}，改用 {AUTO}；"
                f"可选：{'、'.join(sorted(known))}"
            )
        return AUTO

    budget = request.get("gpu_budget_gb")
    if budget is None:
        return AUTO
    try:
        mapped = LEGACY_BUDGET_TIERS[int(budget)]
    except (TypeError, ValueError, KeyError):
        if warn is not None:
            warn(f"无法识别的显存预算 {budget!r}，改用显卡档位 {AUTO}")
        return AUTO
    if known is not None and mapped not in known:
        return AUTO
    if warn is not None:
        warn(f"显存预算 {budget} GB 已改为显卡档位 {mapped}")
    return mapped
