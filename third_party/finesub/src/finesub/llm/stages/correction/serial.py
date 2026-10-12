"""Serial dispatch: one window at a time, each reading the last one's notes.

This is the mode that has continuity. Each window's `<next_advice>` enters the
advice ledger the next window is shown, and its `<keep_entries>` is the only
continuation point of the knowledge transfer chain -- which is why a window
answered from the resume cache still has to re-derive both.
"""

from __future__ import annotations

import sys
from typing import Dict, List

from finesub.reporting import current_reporter
from ...chunking import SubtitleWindow
from ...routing.config import (
    ADVICE_LEDGER_MAX_TOKENS,
    CapabilityTier,
    KB_TRANSFER_MAX_ENTRIES,
    KB_WINDOW_TOTAL_ENTRIES,
    injection_block_token_limit,
)
from ...injection_budget import render_knowledge_entries_block
from ...knowledge.base import append_task_artifact, load_entry_texts
from ...output_protocol import validate_correction_window_output
from ...prompt_variants import resolve_variant
from ...token_truncate import truncate_text_only
from .attempts import _extract_next_advice, run_window_attempts
from .commit import _entry_details_signature, _window_input_hash
from .context import CorrectionRun, RequestTurnProduct
from .request_turn import run_window_request_turn


def run_serial_windows(run: CorrectionRun, windows: List[SubtitleWindow]) -> None:
    """Execute `windows` in order, growing the list when one splits."""

    i = 0
    while i < len(windows):
        current = windows[i]
        transfer_found = run.carried.entry_bodies()
        if run.ledger.holds(current.chunk_id):
            record = run.ledger.records[current.chunk_id]
            split_into = record.get("split_into")
            if isinstance(split_into, list) and split_into:
                # A parent that split has no result of its own; re-derive
                # the halves (deterministic) and let each hit its record.
                halves = run.geometry.split(current)
                if halves is not None and [
                    half.chunk_id for half in halves
                ] == [str(part) for part in split_into]:
                    windows[i : i + 1] = list(halves)
                    continue
            if record.get("input_hash_core") == _window_input_hash(current):
                cached_content = str(record.get("content") or "")
                cached_tier = CapabilityTier(
                    record.get("capability_tier", CapabilityTier.CAPABLE.value)
                )
                # Validate against the variant the window *really* used
                # (recorded per window, model-routing v2); pre-record caches
                # fall back to the cached tier's default.
                cached_variant = resolve_variant(
                    record.get("variant") or None, cached_tier
                )
                cached_validation = validate_correction_window_output(
                    cached_content,
                    current,
                    variant=cached_variant,
                )
                if cached_validation.ok:
                    # Replaying a parallel-produced window in serial mode is
                    # allowed but lossy: parallel windows never wrote advice,
                    # so the ledger later live windows read starts short of
                    # what a first serial run would have built. Say so rather
                    # than silently changing what those windows see.
                    cached_continuity = str(record.get("continuity") or "")
                    if (
                        run.profile.continuity == "serial"
                        and cached_continuity == "parallel"
                        and current.chunk_id not in run.warned_parallel_replays
                    ):
                        run.warned_parallel_replays.add(current.chunk_id)
                        current_reporter().warning(
                            "correction-advice-missing",
                            f"window {current.chunk_id} was produced in parallel "
                            "mode; its advice was never recorded",
                            impact="串行的 advice ledger 从此处起缺这一条",
                        )
                    run.commit_window(
                        current,
                        cached_validation,
                        _extract_next_advice(
                            cached_content, count_tokens=run.token_counter.count_text
                        ),
                    )
                    if run.task_artifact_dir:
                        append_task_artifact(
                            run.task_artifact_dir,
                            kind="correction_window_cached",
                            task_id=run.task_id,
                            payload={
                                "chunk_id": current.chunk_id,
                                "source_ids": list(current.source_ids),
                                "row_count": len(cached_validation.segments),
                                "reused_from": cached_continuity or "cache",
                            },
                        )
                    # Replay continues the transfer chain from the cached
                    # keep list so later windows hash consistently.
                    run.carried.keys = [
                        key
                        for key in (record.get("keep_entries") or [])
                        if isinstance(key, str) and key
                    ][:run.carried.cap]
                    i += 1
                    continue
        # Below the resume check on purpose. Priming the next window's clip
        # is only worth it when this window is actually going to call the
        # model; a replay that answers every window from the cache used to
        # run one ffmpeg extraction and one Gemini Files upload per window
        # anyway -- ~19 of each for a 20-window task that made no LLM call
        # at all, each logged as an api_call and each spending upload quota.
        run.media.prefetch_next_correction(windows, i)
        # One exchange block per executing window, claimed in window
        # order (cached replays claim none), so serial numbering matches
        # the parallel scheme and stays deterministic across reruns.
        window_block = (
            run.exchange_logger.reserve_block() if run.exchange_logger else None
        )
        # Cumulative ledger cap: keep the most recent windows' advice
        # (front-truncated) within the token budget.
        previous_advice = truncate_text_only(
            run.carried.rendered_advice(),
            ADVICE_LEDGER_MAX_TOKENS,
            run.token_counter.count_text,
            keep="tail",
            prefer_natural_boundary=True,
        )
        request_product = RequestTurnProduct()
        if run.request_channel:
            # No search client only means nothing gets searched: an
            # entries-only channel (`retrieval=none|native`) still runs its turn.
            base_chunk_id = current.chunk_id.split("-", 1)[0]
            if base_chunk_id not in run.request_turn_cache:
                carried_text = ""
                if transfer_found:
                    carried_block = render_knowledge_entries_block(
                        transfer_found,
                        count_tokens=run.token_counter.count_text,
                        block_limit=injection_block_token_limit(
                            KB_TRANSFER_MAX_ENTRIES
                        ),
                    )
                    carried_text = carried_block.text
                run.request_turn_cache[base_chunk_id] = run_window_request_turn(
                    run,
                    current,
                    previous_advice=previous_advice,
                    carried_entries_text=carried_text,
                    carried_key_count=len(transfer_found),
                    exchange_logger=window_block,
                )
            request_product = run.request_turn_cache[base_chunk_id]
        # v17: one unified render — transfers first, then this window's new
        # requests, capped to the window total. Order is priority: a full
        # block shares out longest-first, and when even the entries' heads do
        # not fit, the last ones drop (`render_knowledge_entries_block`).
        injected_keys: List[str] = []
        window_entry_details = ""
        union: Dict[str, str] = dict(transfer_found)
        fresh_keys = [
            key
            for key in request_product.requested_entry_keys
            if key not in union
        ]
        if fresh_keys:
            fresh_found, _fresh_missing = load_entry_texts(
                run.knowledge_root, fresh_keys
            )
            for key, body in fresh_found.items():
                if key not in union:
                    union[key] = body
        union = dict(list(union.items())[:KB_WINDOW_TOTAL_ENTRIES])
        injected_extent: Dict[str, Dict[str, int]] = {}
        if union:
            window_entry_block = render_knowledge_entries_block(
                union,
                count_tokens=run.token_counter.count_text,
                block_limit=injection_block_token_limit(
                    KB_WINDOW_TOTAL_ENTRIES
                ),
            )
            window_entry_details = window_entry_block.text
            # what the budget let in: an entry it dropped was never shown
            injected_keys = list(window_entry_block.injected)
            injected_extent = {
                key: dict(kept) for key, kept in window_entry_block.kept.items()
            }
        outcome = run_window_attempts(
            run,
            current,
            previous_advice=previous_advice,
            window_entry_details=window_entry_details,
            window_entry_sig=_entry_details_signature(window_entry_details),
            injected_keys=injected_keys,
            injected_extent=injected_extent,
            chained=True,
            add_tail=lambda half: (
                windows.insert(i + 1, half),
                run.progress and run.progress.add_units(1),
            ),
            exchange_block=window_block,
        )
        if outcome.restart_halves is not None:
            # Final-attempt truncation split: both halves re-enter the
            # window list as fresh units, with their own retry budgets
            # and their own resume-replay checks.
            windows[i : i + 1] = list(outcome.restart_halves)
            if run.progress is not None:
                run.progress.add_units(len(outcome.restart_halves) - 1)
            continue
        run.commit_window(outcome.window, outcome.validation, outcome.next_advice)
        run.carried.keys = outcome.next_transfer
        i += 1
