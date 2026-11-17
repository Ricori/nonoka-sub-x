"""Which GPU tier a task request asks the engine for.

FineSub 0.5.0 replaced `--gpu-budget-gb 4|8|12|16` with `--gpu-tier`, whose
names say what a machine *is* rather than how many gigabytes to spend:
`cpu`, `entry`, `standard`, `standard_large_vram`, `high`, and `auto`, which
detects. The engine owns that list, so this module never restates it: the
transcription package declares it in its `PACKAGE.json` (`gpu_tiers`, checked
against the engine by `tests/test_engine_packages.py`), the caller hands it
over (`known`), and a request is validated against it and falls back to
`auto`, which is also the engine's own default.

The legacy mapping is here because Nonoka X's own stored state outlives the
engine's option surface: a task queued before this upgrade, a saved default
request in the desktop's settings, and the cloud's serialized job payloads all
carry `gpu_budget_gb`. Dropping such a request would fail a task the user
already started, so the number is read once, converted, and said out loud.
"""

from __future__ import annotations

from typing import Any, Callable, Iterable, Mapping


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


def resolve_request_gpu_tier(
    request: Mapping[str, Any],
    *,
    warn: Callable[[str], None] | None = None,
    known: Iterable[str] | None = None,
) -> str:
    """The `gpu_tier` to hand the engine, migrating a legacy request.

    `known` is the engine's own list of tier names; None when there is no
    engine to ask (no transcription package installed, a test that stubs
    it), and then validation is skipped rather than guessed at: an unknown
    name travels on to the engine, whose error names the choices it does know.
    `warn` is called at most once, with a message meant for the task log, when
    a request arrives carrying only the retired `gpu_budget_gb`.
    """

    tier = str(request.get("gpu_tier") or "").strip()
    known = frozenset(known) if known is not None else None
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
