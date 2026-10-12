"""The diarization stage: vocal audio -> a speaker timeline -> labels on stable.

Split in two on purpose, because the two halves have opposite properties:

* `run_diarization` is expensive (GPU, minutes) and numerically sensitive.
  Identical decoded input and execution path reproduce exactly in our tests,
  but even a tiny input perturbation can change cache selection and later tracks
  (docs/diarization-findings.md 5.1). A different result is not necessarily equally
  accurate. Its `*-diar.json` is a task artifact, cached by existence like other
  stage output. It is *not* the label carrier.
* `apply_to_stable` is cheap and deterministic: it reads that cache, joins it to
  the subtitle grid, and writes `speaker` onto `*-stable.json`, which is where
  the owner decided labels live.

Nothing here names anyone. Tracks are relative ids, valid inside one run; the
semantic layer and the human turn them into identities (P15's ordered chain).
"""

from __future__ import annotations

import json
from contextlib import suppress
from dataclasses import dataclass, replace
from functools import cached_property
from pathlib import Path
from typing import Any, Sequence

from . import diarizen_backend, marks, nemotron3_backend, sortformer_backend
from .annotate import annotate_payload, speaker_metadata
from .assign import AssignmentParameters, assign_all
from ...reporting import current_reporter
from .backend import ARTIFACT_ENV, Backend, BackendOutput, DiarizationUnavailable
from .evidence import evidence_path, read_if_present, timeline_key
from .labels import SegmentSpeaker
from .sortformer_backend import MODEL_NAME, resolve_artifacts
from .segments import (
    FRAME_SECONDS,
    SmoothingParameters,
    TrackSegment,
    pair_co_activity,
    segment_overlap_ratio,
    segments_from_activity,
    track_seconds,
    union_seconds,
)

#: The backend a run uses when nobody names one. The default lives here and not in argparse
#: (`README_DEV.md` 开发原则): the front end passes `None` and this answers.
#:
#: ⚠ **Nemotron-3 + vbx** (owner 2026-09-25). Against DiariZen, the previous default: less hand
#: fixing on both in-domain corpora (edit cost, DiariZen minus vbx, hina +55.7 [+19.1, +100.8],
#: 姐姐咚咚 +34.1 [+20.1, +49.1]), better past eight people, about 3.5 times faster, and usable
#: commercially. The owner accepted its one measured loss, far-field meeting detection (findings
#: 2.6). ⚠ Those readings came from 34 files that were also used to design vbx, so they show it
#: works there, not that it generalises. DiariZen and Sortformer stay, named explicitly, for research.
DEFAULT_BACKEND = "nemotron3"

#: What a `-diar.json` without a `backend` field came from: it predates the seam, when Sortformer
#: was the only backend. Not the default -- a reader's assumption about an old file.
LEGACY_BACKEND = "sortformer"


#: Every backend this build knows about. ⚠ The only place the adapter modules are named: another
#: backend is a new module plus one line here, and nothing else in the package changes.
BACKENDS: dict[str, Backend] = {
    nemotron3_backend.BACKEND.name: nemotron3_backend.BACKEND,
    diarizen_backend.BACKEND.name: diarizen_backend.BACKEND,
    sortformer_backend.BACKEND.name: sortformer_backend.BACKEND,
}


def model_for(backend: str) -> str:
    """Which model a backend name stands for, in the run record.

    ⚠ One function because there are two writers: `-diar.json` and `-stable.json`. They had
    drifted -- the timeline named the real backend while `apply_to_stable` hardcoded Sortformer's
    checkpoint, so every DiariZen run recorded the wrong model in `-stable.json`, which is a
    RECORD artifact and is never cleaned up. A name from a timeline written before backends
    existed resolves to the historical default rather than raising.
    """
    backend = BACKENDS.get(backend)
    return backend.model if backend is not None else MODEL_NAME


def resolve_backend(name: str | None) -> Backend:
    """The backend for this run: the one named, else `DEFAULT_BACKEND`.

    No probing and no fallback: an explicit request that cannot be satisfied says so, and the
    default fetches what it needs (`nemotron3_backend.ensure`) rather than quietly becoming
    something else.
    """
    chosen = DEFAULT_BACKEND if name is None else name
    try:
        return BACKENDS[chosen]
    except KeyError:
        raise ValueError(
            f"unknown diarization backend {chosen!r}; available: {', '.join(sorted(BACKENDS))}"
        ) from None


def timeline_hints(tracks: Sequence[TrackSegment]) -> dict[str, Any]:
    """The acoustic facts a proofreader is told (plan 5l.11).

    Hints, not labels: nothing here changes which track a line is on.

    `fourth_track_share` is how much of the speech the fourth-busiest track holds.
    It stands where a "four slots full, maybe a fifth person" flag was meant to:
    no threshold on it told >=5 speakers from 2-4 well enough (plan 5o.5 -- 20 s
    of each track fired on 43.5% vs 6.2% of VoxConverse files, and on four of six
    AMI meetings that have exactly four people), so it is left a number.

    `co_activity` is reported the same way, as numbers only. A `not_same` list used
    to turn it into "these two are different people" (share > 5% and both tracks
    >= 20 s, calibrated on 23 same-person pairs). On six episodes of a Japanese
    three-person show with per-line speaker truth it flagged all 36 pairs, the six
    same-person pairs among them (co-activity 9.0-33.7%), and on four episodes a
    different-person pair sounded together less than the same-person pair -- no
    threshold separates them, and its errors all fall on the side that would block
    merging a person split over two tracks (plan 5q). Owner 2026-09-12: report the
    share, drop the verdict, keep collecting.
    """
    pairs = sorted(pair_co_activity(tracks).items())
    busiest = sorted(track_seconds(tracks).values(), reverse=True)
    speech = union_seconds(tracks)
    return {
        "co_activity": {f"s{a}-s{b}": round(value, 4) for (a, b), value in pairs},
        "fourth_track_share": round(busiest[3] / speech, 4) if len(busiest) >= 4 and speech else 0.0,
    }


@dataclass(frozen=True)
class DiarizationResult:
    tracks: tuple[TrackSegment, ...]
    frame_seconds: float
    overlap: float
    #: `ELBO_K - ELBO_1` from the clustering: large means the model prefers several speakers,
    #: near zero means the evidence for more than one is weak. ⚠ ⚠ **Not calibrated on the failure it is for.** What it should catch is genuinely single-speaker material being over-split, and the bed's single-speaker arm always clustered to K=1, where this ratio is identically 0 by construction -- a perfect separation that demonstrates nothing (identity plan A.4, gap tracked as Q13).
    #: ⚠ **A number, not a verdict** -- nothing is suppressed
    #: on it, and it is `None` for a backend that computes none. ⚠ A **total**: it grows with the
    #: length of the file, so comparing two files means dividing each by `log_bayes_embeddings`.
    single_speaker_log_bayes: "float | None" = None
    #: How many embeddings the number above was summed over -- its divisor, and the only thing
    #: that makes readings from different files belong on one axis.
    log_bayes_embeddings: "int | None" = None
    #: One clean span per track, `[{"track", "start", "end", "closeness"}]`: where that track
    #: sounds alone and most like itself. What a person listens to in order to name it.
    samples: "tuple[dict, ...]" = ()
    #: Which backend produced this. Written into `-diar.json` and read back so a
    #: later run can say "the cache came from X" instead of quietly serving it
    #: as if it were Y. Defaults to the historical value so a timeline written
    #: before backends existed still reads.
    backend: str = LEGACY_BACKEND

    def __post_init__(self) -> None:
        # Held at the precision `-diar.json` records, so the run that writes the
        # timeline and every later read of it derive their hints from the same
        # numbers (plan 5o.1 K2). Frames are 80 ms; a millisecond is noise.
        object.__setattr__(
            self,
            "tracks",
            tuple(TrackSegment(s.track, round(s.start, 3), round(s.end, 3)) for s in self.tracks),
        )

    @cached_property
    def hints(self) -> dict[str, Any]:
        facts = timeline_hints(self.tracks)
        # ⚠ Hints in the strict sense the function above defines: nothing here changes which track
        # a line is on. They are here rather than only in the sidecar because this is the file a
        # person opens, and a number nobody sees decides nothing.
        if self.single_speaker_log_bayes is not None:
            published = round(self.single_speaker_log_bayes, 1)
            facts["single_speaker_log_bayes"] = published
            # ⚠ Published beside it, never instead of it: the total is the quantity with a
            # meaning, and this is the only way a reader can put two files on the same axis.
            # ⚠ Divided from the PUBLISHED total, not the one in memory, so that reading the
            # file back and asking for its hints gives the same two numbers. That round trip is
            # the product path (`apply_to_stable` reads the timeline it just wrote), so a
            # recomputation that drifts in the third decimal is a real mismatch, not a nicety.
            if self.log_bayes_embeddings:
                facts["single_speaker_log_bayes_per_embedding"] = round(
                    published / self.log_bayes_embeddings, 3)
        if self.samples:
            facts["samples"] = [dict(sample) for sample in self.samples]
        return facts

    def to_payload(self, *, artifacts_revision: str, parameters: dict[str, Any]) -> dict[str, Any]:
        track_count = len({segment.track for segment in self.tracks})
        record: dict[str, Any] = {
            "segment_overlap_ratio": round(self.overlap, 4),
            "hints": self.hints,
            "backend": self.backend,
        }
        # ⚠ Bookkeeping, so it sits with the provenance rather than in `hints`, which is the
        # block a reader consumes. It is the divisor `single_speaker_log_bayes` is meaningless
        # without, and without it here the total cannot be restored from the file at all.
        if self.log_bayes_embeddings:
            record["log_bayes_embeddings"] = int(self.log_bayes_embeddings)
        return {
            "metadata": speaker_metadata(
                model=model_for(self.backend),
                artifacts_revision=artifacts_revision,
                track_count=track_count,
                parameters={**parameters, "frame_seconds": self.frame_seconds},
            )
            | record,
            "tracks": [
                {"track": segment.track, "start": segment.start, "end": segment.end}
                for segment in self.tracks
            ],
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "DiarizationResult":
        """Everything the file carries, including the evidence `hints` publishes.

        ⚠ The evidence fields are restored here and not left to the sidecar, because the product
        path reads a timeline back on every run: `run_diarization` writes `-diar.json`, then
        `apply_to_stable` reads it again. While this only restored tracks, the single-speaker
        evidence and the per-track samples were dropped on the FIRST run, not merely on resume --
        and `docs/manual/outputs.md` promises the same block reaches `-stable.json`.
        """
        tracks = tuple(
            TrackSegment(track=int(row["track"]), start=float(row["start"]), end=float(row["end"]))
            for row in payload.get("tracks", ())
        )
        metadata = payload.get("metadata", {})
        hints = metadata.get("hints", {}) or {}
        evidence = hints.get("single_speaker_log_bayes")
        return cls(
            tracks=tracks,
            frame_seconds=float(metadata.get("parameters", {}).get("frame_seconds", FRAME_SECONDS)),
            overlap=float(metadata.get("segment_overlap_ratio", segment_overlap_ratio(tracks))),
            # A timeline written before backends existed has no field and was
            # Sortformer's; saying so beats reporting it as unknown.
            backend=str(metadata.get("backend", LEGACY_BACKEND)),
            single_speaker_log_bayes=None if evidence is None else float(evidence),
            log_bayes_embeddings=(int(metadata["log_bayes_embeddings"])
                                  if metadata.get("log_bayes_embeddings") else None),
            samples=tuple(dict(sample) for sample in hints.get("samples", ())),
        )


def run_diarization(
    vocal_path: str | Path,
    *,
    artifacts: str | Path | None = None,
    device: str = "cuda",
    smoothing: SmoothingParameters | None = None,
    backend: str | None = None,
    evidence_path: "str | Path | None" = None,
    max_speakers: int | None = None,
) -> DiarizationResult:
    """Read the vocal track, return its speaker timeline."""
    chosen = resolve_backend(backend)
    handle = chosen.ensure(artifacts, device)
    try:
        import soundfile
        import torch
    except ImportError as error:  # pragma: no cover - depends on the install
        raise DiarizationUnavailable(f"speaker diarization needs the [asr] extra: {error}") from error

    from ..preprocessing.audio import TARGET_SR, resample_if_needed

    signal, sample_rate = soundfile.read(str(vocal_path), dtype="float32", always_2d=True)
    # Folded before resampling, which halves what the resampler holds.
    waveform = torch.from_numpy(signal.mean(axis=1)).unsqueeze(0)
    # `resolve_vocal_audio` falls back to the separator's lossless `.flac`, which
    # keeps the model's own rate (44.1 kHz stereo). Refusing anything but 16 kHz
    # made `--diarize` fail on exactly those runs; the checkpoint wants 16 kHz,
    # so the stage brings the audio there itself.
    waveform, _ = resample_if_needed(waveform, sample_rate, TARGET_SR)
    produced = chosen.activity(waveform, handle=handle, device=device,
                               max_speakers=max_speakers)
    tracks = tuple(segments_from_activity(
        produced.activity.float().cpu().tolist(),
        frame_seconds=chosen.frame_seconds,
        parameters=smoothing,
    ))
    evidence = produced.evidence
    if evidence is not None and evidence_path is not None:
        # ⚠ Never fatal: the evidence is an extra, and it is recomputable by rerunning the stage.
        try:
            # ⚠ Bound to THIS timeline. The sidecar indexes by track id, so beside any other
            # partition its columns are confidently wrong rather than missing.
            replace(evidence, timeline=timeline_key(tracks)).save(evidence_path)
        except Exception as error:  # noqa: BLE001 - the timeline is the product, not this
            current_reporter().warning(
                "diarization-evidence-unsaved",
                f"声学证据未能写出：{error}",
                impact="单人证据、换人 hint 与逐行置信标这次都不可用；删掉时间线重跑即可重算",
            )
    elif evidence is None and evidence_path is not None and Path(evidence_path).exists():
        # ⚠ A backend that computes no evidence must not leave the LAST one lying there. It
        # indexes a timeline this run just replaced, and a stale sidecar reads as a valid one.
        with suppress(OSError):
            Path(evidence_path).unlink()
    return DiarizationResult(
        tracks=tracks,
        frame_seconds=chosen.frame_seconds,
        overlap=segment_overlap_ratio(tracks),
        backend=chosen.name,
        single_speaker_log_bayes=getattr(evidence, "single_speaker_log_bayes", None),
        log_bayes_embeddings=getattr(evidence, "log_bayes_embeddings", None),
        samples=tuple(getattr(evidence, "samples", ()) or ()),
    )


def write_timeline(result: DiarizationResult, path: str | Path, *, artifacts_revision: str = "") -> Path:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = result.to_payload(artifacts_revision=artifacts_revision, parameters={})
    destination.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return destination


def read_timeline(path: str | Path) -> DiarizationResult:
    return DiarizationResult.from_payload(json.loads(Path(path).read_text(encoding="utf-8")))


def labels_for(
    subtitles: Sequence[dict[str, Any]],
    result: DiarizationResult,
    *,
    assignment: AssignmentParameters | None = None,
) -> list[SegmentSpeaker]:
    return assign_all(subtitles, result.tracks, parameters=assignment)


def apply_to_stable(
    stable_path: str | Path,
    timeline_path: str | Path,
    *,
    assignment: AssignmentParameters | None = None,
    artifacts_revision: str = "",
) -> Path:
    """Join the timeline onto the subtitles and rewrite stable.json in place.

    Cheap and deterministic: rerunning it on the same timeline and sidecar produces
    the same bytes. Reuse the stored timeline rather than changing its numerical path.
    The confidence marks (`marks.py`) are computed here from the evidence sidecar;
    without a usable one the labels are written unmarked and `metadata.speaker.marks`
    is `None`.
    """
    stable = Path(stable_path)
    payload = json.loads(stable.read_text(encoding="utf-8"))
    result = read_timeline(timeline_path)
    subtitles = payload.get("segments", [])
    speakers = labels_for(subtitles, result, assignment=assignment)
    evidence = read_if_present(evidence_path(timeline_path), tracks=result.tracks)
    marked = marks.row_marks(subtitles, result.tracks, speakers, evidence)
    if marked is not None:
        speakers = marks.apply(speakers, marked)
    elif result.backend == nemotron3_backend.BACKEND.name:
        # Not an error: the sidecar is a deletable intermediate. But a run that silently lost the marks
        # reads as "every line is certain", so say so.
        current_reporter().warning(
            "speaker-marks-unavailable",
            "说话人置信标这次没有算：声学证据文件不在、与时间线对不上，或是旧版本写的",
            impact="逐行的低把握标与漏检补轨都没有；说话人标签本身照常",
            action="要有置信标，删掉 -diar.json 与 -diar-evidence.npz 重跑",
        )
    track_count = len({segment.track for segment in result.tracks})
    annotated = annotate_payload(
        payload,
        speakers,
        speaker_metadata(
            # ⚠ The backend the timeline records, not a constant. `-stable.json` is a RECORD
            # artifact and is never cleared, so a wrong model name here is permanent.
            model=model_for(result.backend),
            artifacts_revision=artifacts_revision,
            track_count=track_count,
            parameters={"frame_seconds": result.frame_seconds,
                        "segment_overlap_ratio": round(result.overlap, 4)},
        )
        | {"backend": result.backend, "hints": result.hints,
           # ⚠ `None` means "not computed" -- without it, a line without a `mark` would read as certain.
           "marks": marks.metadata() if marked is not None else None},
    )
    temporary = stable.with_name(f".{stable.stem}.speaker{stable.suffix}")
    temporary.write_text(json.dumps(annotated, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(stable)
    return stable


def is_annotated(stable_path: str | Path) -> bool:
    """Has this stable.json already been through `apply_to_stable`?

    The stage's own existence check: `*-stable.json` exists long before anyone
    diarizes it, so the file's presence cannot be the signal.
    """
    path = Path(stable_path)
    if not path.exists():
        return False
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return "speaker" in payload.get("metadata", {})
