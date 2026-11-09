"""Nonoka X's pinned copy of FineSub's separator block planner.

Cloud vocal separation cuts a track into blocks with the same arithmetic the
local stage uses. Not into the same blocks: ``plan_separation_blocks`` sizes
the count to the worker count, and the cloud always runs one worker while local
runs ``min(profile.vocal_separator_instances, separator_worker_limit(dur))``.
Block edges change the separated audio -- BS-Roformer is chunk-sensitive even
across the pad, which ``plan_separation_blocks`` says outright -- so the two
tracks are equivalent, not identical. What this copy buys is that the cloud's
edges are *upstream's answer for one worker* rather than a rule invented here.

A copy rather than an import, because the cloud Vocal worker cannot import the
stage this comes from: that image carries audio-separator but deliberately no
torchaudio, and ``finesub...separator.separation`` reaches torchaudio through
its audio readers long before it reaches any planning code.

A copy rather than a vendor patch, because the planner is untouched upstream
code. It sits in the one stretch of ``separation.py`` that no patch in
``patches/finesub`` touches, and moving it there would trade forty-five lines
of arithmetic for a file-move hunk that has to be redone against new code at
every upstream sync.

What keeps a copy from drifting is ``tests/test_separator_block_plan.py``: it
compares the block below to the vendored source character for character, and
fails the moment upstream edits it. So keep the block verbatim -- upstream's
names, comments and underscores included. Anything Nonoka X has to say about it
belongs above this line or below the end marker, never inside.
"""

from __future__ import annotations

from dataclasses import dataclass


# --- begin verbatim copy: finesub/speech/preprocessing/separator/separation.py

@dataclass(frozen=True)
class _SeparationBlock:
    index: int
    block_start: int
    #: Where the block's core ends: the next block's `block_start`, or the end of the file.
    block_end: int
    read_start: int
    read_end: int


# One extra separator worker allowed per this much audio. Separation runs before
# VAD, so unlike the WT ladder it can only see wall-clock duration, never
# effective speech. Load-bearing since blocks became a multiple of the worker
# count: that removed the implicit gate (a short file used to yield one block,
# which capped workers at one all by itself).
WORKER_DURATION_THRESHOLD_SEC = 300.0


def separator_worker_limit(
    duration_sec: float,
    *,
    threshold_sec: float = WORKER_DURATION_THRESHOLD_SEC,
) -> int:
    if threshold_sec <= 0:
        return 1
    return int(max(0.0, duration_sec) // threshold_sec) + 1


def plan_separation_blocks(
    total_frames: int,
    sample_rate: int,
    *,
    workers: int,
    max_core_seconds: float,
    pad_samples: int,
) -> list[_SeparationBlock]:
    """Cut the timeline into equal blocks, a whole multiple of ``workers``.

    A fixed core length left every worker a different amount of work and a short
    final block; sizing the count to the workers instead gives each of them the
    same number of equal blocks. The cost is that separated audio now depends on
    the worker count -- block edges move, and Roformer is chunk-sensitive even
    with the pad. That is accepted (docs/gpu-profiles.md), so existing
    ``-vocal.ogg`` files are not reproducible and must be deleted to rerun.

    The ladder in ``separator_worker_limit`` doubles as the guard against
    absurdly short cores, so no separate floor is needed: at one round the core
    is ``duration / workers``, which the 300s ladder bounds below by
    ``300k / (k + 1)`` for ``k`` whole thresholds -- smallest at k=1, i.e. 150s.
    Against a 10s pad per side that is 13% redundant compute at worst.
    """

    workers = max(1, int(workers))
    core_limit = max(1, int(round(max_core_seconds * sample_rate)))
    # Smallest whole number of rounds that keeps every core within the limit.
    rounds = max(1, -(-total_frames // (core_limit * workers)))
    block_count = rounds * workers

    edges = [round(index * total_frames / block_count) for index in range(block_count)]
    edges.append(total_frames)

    blocks: list[_SeparationBlock] = []
    for index in range(block_count):
        core_start, core_end = edges[index], edges[index + 1]
        if core_end <= core_start:
            continue
        blocks.append(
            _SeparationBlock(
                index=len(blocks),
                block_start=core_start,
                block_end=core_end,
                read_start=max(0, core_start - pad_samples),
                read_end=min(total_frames, core_end + pad_samples),
            )
        )
    return blocks

# --- end verbatim copy

#: The core and pad ``run_vocal_separation`` is called with when nothing
#: overrides them. Upstream spells these as literal defaults in a signature
#: rather than as constants, so they are named here and pinned to that
#: signature by the same test that pins the block above.
DEFAULT_BLOCK_SECONDS = 600.0
DEFAULT_PAD_SECONDS = 10.0

#: Upstream's name for a planned block, re-exported without the underscore.
#: The copy keeps the private name so the comparison stays character-exact;
#: callers outside this module get one that does not read as a borrowed
#: internal.
SeparationBlock = _SeparationBlock
