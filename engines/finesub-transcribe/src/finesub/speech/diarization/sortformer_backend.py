"""The Sortformer adapter: kept for research, run from a local export.

Streaming, four speaker slots, no embeddings -- so it produces a partition and no identity
evidence at all, and everything built on that evidence needs a clustering backend (nemotron3 or
diarizen). ⚠ Do not give this
backend a weaker substitute signal to make the two symmetrical; that is the mistake that sank
the first single-speaker veto (plan 5b, P14).

The model itself lives in `runner.py` / `chunking.py`; this file is only the seam.
"""

from __future__ import annotations

from pathlib import Path

from .backend import ARTIFACT_ENV, Backend, BackendOutput, DiarizationUnavailable, artifact_candidates
from .segments import FRAME_SECONDS

#: Model identity written into the run record. Bump when the artifacts change.
MODEL_NAME = "nvidia/diar_streaming_sortformer_4spk-v2.1"

#: The one file whose presence stands for the export. ⚠ Unlike DiariZen this checks a single
#: name, which is a real asymmetry: `SortformerRunner` opens three, and a half-copied directory
#: would get past here. Kept as it is because it has never been seen to happen and the DiariZen
#: side is where it did -- but if it ever does, the fix is the list, not a try/except.
CORE_ARTIFACT = "sortformer_core.ts"


def resolve_artifacts(explicit: str | Path | None = None) -> Path:
    """Find the exported model, or say precisely what is missing.

    No download happens here yet: the three files are a derived artifact we produce with
    `tools/sortformer_export/`, and where they are hosted is still open (plan 5a.1). Until then
    this is a local lookup with a message that names the way out rather than a bare
    FileNotFoundError.
    """
    for candidate in artifact_candidates(explicit):
        if (candidate / CORE_ARTIFACT).exists():
            return candidate
    raise DiarizationUnavailable(
        "speaker diarization needs the exported Sortformer artifacts; point "
        f"{ARTIFACT_ENV} at a directory holding sortformer_core.ts, "
        "sortformer_preprocessor.ts and sortformer_cache_update.ts "
        "(produce them with tools/sortformer_export/export_sortformer.py)"
    )


def ensure(artifacts: str | Path | None, device: str = "cuda") -> Path:
    del device  # one export serves every device
    return resolve_artifacts(artifacts)


def activity(waveform, *, handle, device, max_speakers=None) -> BackendOutput:
    del max_speakers  # Sortformer has four slots and no knob for them
    from .runner import SortformerRunner

    # ⚠ Closed, not just dropped: a batch run builds one of these per file in a single process,
    # and the three modules stay in torch's caching allocator until something asks for them back.
    with SortformerRunner(handle, device=device) as runner:
        # No evidence: this backend has no embeddings to give.
        return BackendOutput(runner.diarize(waveform))


BACKEND = Backend(
    name="sortformer",
    model=MODEL_NAME,
    frame_seconds=FRAME_SECONDS,
    ensure=ensure,
    activity=activity,
)
