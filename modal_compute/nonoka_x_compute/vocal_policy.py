"""Which sample rate and channel layout the cloud Vocal stage separates at.

``benchmark_results/full-20260825`` compared seven input policies for the
BS-Roformer checkpoint on one T4 against a 44.1 kHz stereo pseudo-reference.
The pipeline's original 16 kHz *mono* policy was the worst cell in that table
by a wide margin -- 36.42% CER against 9.45% for the reference, 21.36 seconds
of speech dropped by VAD, and a stretch of hallucinated boilerplate where the
higher tiers transcribed real lyrics.  Roughly half of that damage came from
downmixing to mono *before* separation rather than from the rate itself, so
every tier kept here is stereo.

The three tiers are the ones that survived the comparison, ordered by how
close they stay to the checkpoint's own training rate.  48 kHz is deliberately
absent: it separated 7% slower than the native rate while adding a pointless
resampling step, and it scored worse than both 32 kHz and 22.05 kHz.

Which tier a task gets is decided by how long its audio is (see
``VOCAL_TIER_LADDER``): short inputs are cheap enough to separate at the
checkpoint's own rate, and long ones buy their length back by giving up
bandwidth the 16 kHz ASR chain discards anyway.

Pure data and pure functions, importable without ``modal``: the Cloudflare
Worker that accepts the task (``nonoka-cloud-cf/src/jobs/vocalPolicy.ts``, a
port of ``tier_for_duration``), the CPU scheduler's prepare phase and the GPU
Vocal worker all have to agree on one tier, and the only way to guarantee that
across them is for the resolved tier to be pinned into the task request once
and read back from there.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping


#: The delivery contract, which no tier changes.  Energy VAD, Whisper and the
#: Qwen referee each resample to 16 kHz mono before doing anything, so the
#: separated stem is downmixed and resampled once, in the GPU worker, and every
#: later stage reads exactly what it would have produced for itself.
DELIVERY_SAMPLE_RATE = 16_000

#: Filename for the decoded source the GPU worker consumes.
#:
#: One artifact for every tier, and deliberately so: the prepare phase decodes
#: at the source's own rate and channel count, exactly as the desktop's
#: ``transcode_to_lossless_audio`` does, and the tier is applied afterwards by
#: ``demix.separate_waveform`` resampling each block on its way into the model.
#: Resampling in prepare instead put ffmpeg's resampler where the desktop has
#: librosa's ``soxr_hq``, which is a difference the ASR chain can see -- the
#: same class of difference that made the ffmpeg delivery downmix transcribe
#: differently from FineSub's.  A 44.1 kHz source never noticed, because
#: neither side resampled it; a 48 kHz one, which is most video, would have.
#:
#: FLAC at the source's own depth rather than PCM: it is what the desktop
#: writes, it is what ``load_audio_slice`` reads through soundfile on both
#: sides, and it is half the bytes of the 24-bit WAV it replaces.
#:
#: Tier-independent also means a task retried after the deployed ladder moved
#: reuses this file instead of paying for the decode again -- the opposite of
#: what the tier-keyed name it replaces had to guarantee, and correct for the
#: same reason: the file no longer depends on the tier at all.
PREPARED_SOURCE_NAME = "source-vocal-decoded.flac"

#: What the checkpoint was trained at, and what upstream separates at unless
#: told otherwise -- ``separation.DEFAULT_SEPARATOR_SAMPLE_RATE``.  Since
#: FineSub 0.4.2 the three rates this ladder chooses between are upstream's own
#: ``separation.SEPARATOR_SAMPLE_RATES``; ``test_the_ladder_stays_inside_the
#: _rates_upstream_supports`` is what keeps the two sets from drifting apart.
NATIVE_SAMPLE_RATE = 44_100

#: Pins one tier for every task, overriding the duration ladder.  For measuring
#: and for incidents, not for normal operation.  An unknown value is ignored
#: rather than fatal: a typo in a deploy variable must not take the pipeline
#: down.
TIER_ENV_VAR = "NONOKA_VOCAL_TIER"

#: Where the resolved tier is pinned inside the task request.  Prepare and
#: separation run in different containers, and a retry can start days after the
#: task was accepted, so neither may re-resolve the default for itself.
REQUEST_FIELD = "vocal_sample_rate_tier"


@dataclass(frozen=True)
class VocalTier:
    """One separator input policy: a rate, a channel count and its evidence."""

    name: str
    sample_rate: int
    channels: int
    label: str


VOCAL_TIERS: dict[str, VocalTier] = {
    tier.name: tier
    for tier in (
        VocalTier("44.1k-stereo", 44_100, 2, "44.1 kHz 立体声"),
        VocalTier("32k-stereo", 32_000, 2, "32 kHz 立体声"),
        VocalTier("22.05k-stereo", 22_050, 2, "22.05 kHz 立体声"),
    )
}

#: Duration decides the tier: spend the native rate on short inputs, where the
#: extra GPU seconds are few, and buy the length of long ones back by giving up
#: bandwidth the ASR chain never sees anyway.  Read as "up to this many seconds
#: of source, use this tier"; the last entry has no ceiling.
#:
#: Separation time is linear in duration and in rate, so the tiers turn a bill
#: that would have grown as 0.138 s per second of source at 44.1 kHz into one
#: that grows as 0.105 and then 0.072 -- roughly halving the slope by the time
#: an input reaches the API's two-hour ceiling.
VOCAL_TIER_LADDER: tuple[tuple[float, str], ...] = (
    (12 * 60.0, "44.1k-stereo"),
    (40 * 60.0, "32k-stereo"),
    (float("inf"), "22.05k-stereo"),
)

#: Used when a duration is missing or unusable.  The middle rung rather than
#: either end: it is the tier ``benchmark_results/full-20260825`` recommended on
#: quality grounds, so an unknown length degrades to the balanced choice instead
#: of to the most expensive or the cheapest one.
DEFAULT_TIER = "32k-stereo"


def tier_for_duration(audio_sec: float | None) -> tuple[VocalTier, str]:
    """Pick the tier for an input of this length; also report why.

    The reason travels with the tier because the tier is recorded on the task
    and read back by three containers, and "32k because the ladder said so" and
    "32k because nothing knew how long the audio was" are worth telling apart
    when a result looks wrong.
    """

    pinned = str(os.environ.get(TIER_ENV_VAR) or "").strip()
    if pinned:
        tier = VOCAL_TIERS.get(pinned)
        if tier is not None:
            return tier, f"{TIER_ENV_VAR}={pinned}"
        # A typo in a deploy variable must not take the cloud pipeline down.
    try:
        seconds = float(audio_sec)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        seconds = 0.0
    if seconds <= 0:
        return VOCAL_TIERS[DEFAULT_TIER], "duration unknown"
    for ceiling, name in VOCAL_TIER_LADDER:
        if seconds < ceiling:
            return VOCAL_TIERS[name], f"{seconds:.0f}s"
    return VOCAL_TIERS[DEFAULT_TIER], "duration unknown"


def tier_for_request(request: Mapping[str, object]) -> VocalTier:
    """Read back the tier the Worker pinned into this task's request.

    Never re-derives from a duration: prepare and separation run in different
    containers and a retry can start days later, so the ladder is applied once,
    at reservation, and everything downstream reads the answer.
    """

    name = str(request.get(REQUEST_FIELD) or "").strip()
    return VOCAL_TIERS.get(name, VOCAL_TIERS[DEFAULT_TIER])
