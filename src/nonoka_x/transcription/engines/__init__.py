"""The transcription engines. This package imports none of its modules: each is imported where it is used.

- `finesub`: FineSub's recognition chain (WP4a).
- `mai2` (MAI-Transcribe-2), `gemini` (Gemini 3.5 Transcribe, "35t"), `moss_pro` (MOSS Pro): the three cloud API
  engines Nonoka maintains (WP3), each a backend moved out of FineSub's `speech/joint/` (WP1) plus its
  `TranscriptionEngine`; what they share is `cloud.py`. They run in the launcher's environment only.
- `moss_local`: local MOSS, moved with them; not registered, not an engine yet (plan 4.1 item 8).

Each backend turns the source audio into a `units.Transcript` -- the provider's words (or, for the two MOSS, its
segments) grouped into speaker turns in the order the provider emitted them. The cloud ones cache every call's
raw answer in the task's cache directory (`cache.CallCache`), so a rerun spends no quota; local MOSS caches
nothing. What happens to the turns after that (line building, timing, overlap rules) is `lines.py`; the cloud
engines write the lines to `transcript.json` (`cloud.py`).

FineSub's joint-ASR stage that ran the backends and wrote its `-aligned.json` (`joint_stage.py`) is gone: the
engines hand on `transcript.json` (WP3). What each provider does and why it is wired this way: FineSub's
`docs/joint-asr.md`.
"""

from __future__ import annotations

#: The backends' names: their cache directories (`<cache>/<name>/`) and their ledger rows (`keys.KeyLedger`).
MAI2 = "mai2"
GEMINI_35T = "gemini-35t"
MOSS_PRO = "moss-pro"
MOSS_LOCAL = "moss-local"


class JointAsrError(RuntimeError):
    """Local MOSS cannot run as asked (no suitable card, no model, no usable transformers)."""
