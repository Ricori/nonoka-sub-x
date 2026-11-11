"""The transcription engines. This package imports none of its modules: each is imported where it is used.

Moved out of FineSub's `speech/joint/` (WP1; it was the "joint ASR" backends' package): the three cloud
backends as they were -- `mai2` (MAI-Transcribe-2), `gemini` (Gemini 3.5 Transcribe, "35t"), `moss_pro`
(MOSS Pro) -- with `joint_stage`, FineSub's stage that ran them, and local MOSS (`moss_local`), which is not
registered. WP3 wraps each cloud backend as a `TranscriptionEngine`; the FineSub transcription engine
(`finesub`, WP4a) lives here too.

Each backend turns the source audio into a `units.Transcript` -- the provider's words (or, for the two MOSS, its
segments) grouped into speaker turns in the order the provider emitted them. The cloud ones cache every call's
raw answer in the task directory (`cache.CallCache`), so a rerun spends no quota; local MOSS caches nothing.
What happens to the turns after that (line building, timing, overlap rules) is `lines.py`.

What each provider does and why it is wired this way: FineSub's `docs/joint-asr.md`.
"""

from __future__ import annotations

LOCAL = "local"
MAI2 = "mai2"
GEMINI_35T = "gemini-35t"
MOSS_PRO = "moss-pro"
MOSS_LOCAL = "moss-local"

BACKENDS = (LOCAL, MAI2, GEMINI_35T, MOSS_PRO, MOSS_LOCAL)
#: Backends whose transcript comes in segments (segment times, one speaker each, overlapping across speakers):
#: their units are timed by Silero (S5b) or the forced aligner (S6b), and each turn is split on its own.
SEGMENT_BACKENDS = (MOSS_PRO, MOSS_LOCAL)
#: Owner 2026-10-30: the short names follow the provider's current model, so a
#: newer model moves the alias instead of renaming the option. `moss` is MOSS Pro.
ALIASES = {"gemini": GEMINI_35T, "moss": MOSS_PRO}
CHOICES = (*BACKENDS, *ALIASES)


def resolve_backend(name: str) -> str:
    """The full backend name for an option value (aliases resolved); metadata records this."""

    value = name.strip().lower()
    value = ALIASES.get(value, value)
    if value not in BACKENDS:
        raise ValueError(f"unknown ASR backend {name!r}; choose one of {', '.join(CHOICES)}")
    return value
