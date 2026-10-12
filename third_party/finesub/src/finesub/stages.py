"""The conversion itself: one source, stage by stage.

    source audio -> vocal separation -> VAD + ASR alignment -> stabilization -> SRT
    (-> LLM correction/translation, when the target stage asks for it)

`run_pipeline` is the whole contract: it derives every artifact path from the
final SRT, skips a stage whose output already exists, and records what it did in
the run metadata sidecar. It knows nothing about command lines, manifests or how
many sources are in flight -- `pipeline.py` is the front end that builds requests
and presents results. It imports this module, never the reverse: that direction
is also what keeps `python -m finesub.pipeline` from executing its own module a
second time under a different name.

Not to be confused with `llm/stages/`, which are the stages *inside* the LLM
correction step -- one stage of this one.
"""

from __future__ import annotations

from collections.abc import Callable
import os
import sys
import time
from pathlib import Path
from typing import Any, Mapping, NamedTuple, Optional, Sequence

from . import settings
from .reporting import current_reporter
from .speech.recognition import transcribe as asr_align
from .speech.postprocessing import stabilization as asr_stabilize
from .speech.runtime.resources import AUTO_GPU_TIER
from .run_metadata import (
    metadata_path_for_output,
    stage_record,
    summarize_llm_rounds,
    update_run_metadata,
)
from .subtitles import rendering as to_srt
from .speech.recognition import vad_asr_stage as vad_asr
from .speech.preprocessing.separator import separation as vocal_separation


class PipelinePaths(NamedTuple):
    vocal_audio: Path
    vad_json: Path
    vad_energy_npz: Path
    aligned_json: Path
    stable_json: Path
    #: The speaker timeline. Its own artifact because it is the one thing here
    #: that a rerun may not reproduce: the same audio down the same numerical
    #: path is bit-reproducible, but a changed input or a changed path gives a
    #: different-but-equally-valid answer, because a hard argmax turns last-bit
    #: float differences into flipped labels (`docs/diarization-findings.md`
    #: 5.1 and 5.2). So it is cached rather than recomputed, and `metadata.backend`
    #: records which backend made it -- provenance, not a compatibility key.
    #: The labels derived from it live on `stable_json`.
    diar_json: Path
    #: The LLM speaker judge's verdicts (`speech/diarization/judgments.py`): a
    #: record like `diar_json`, read over the acoustic labels by the `spk` column
    #: and the per-speaker exports, never folded into `stable_json`.
    speaker_judge_json: Path
    raw_srt: Path
    translated_srt: Path
    final_srt: Path
    task_artifact_dir: Path
    metadata_json: Path

    @property
    def srt(self) -> Path:
        return self.final_srt

    def resolve_vocal_audio(self) -> Path:
        """Return the existing vocal file, preferring the ASR delivery.

        The pipeline always asks separation for `.ogg` (16 kHz mono, what every
        reader of it resamples to anyway). A `.flac` beside it is the lossless
        delivery -- separation's other mode, produced by a direct call -- and
        reading it is equally valid, just larger.
        """
        if self.vocal_audio.exists():
            return self.vocal_audio
        flac_fallback = self.vocal_audio.with_suffix(".flac")
        if flac_fallback.exists():
            return flac_fallback
        return self.vocal_audio


#: `--diarize`: off; the acoustic labels (and confidence marks); those plus the LLM speaker judge,
#: reading the rows or also hearing them (owner 2026-10-07, 2026-10-09: one switch, four levels).
DIARIZE_LEVELS = ("off", "acoustics", "judge-text", "judge-audio")


def judge_of(diarize: str) -> str:
    """The speaker judge a `--diarize` level asks for: `off`, `text` or `audio`."""
    return diarize.removeprefix("judge-") if diarize.startswith("judge-") else "off"


PIPELINE_STAGE_ORDER = {
    "vocal": 1,
    "aligned": 2,
    "stable": 3,
    "raw-srt": 4,
    "translated-srt": 5,
    "final-srt": 6,
}

_VIDEO_EXTENSIONS = frozenset({
    ".mp4", ".mkv", ".avi", ".webm", ".mov", ".flv", ".wmv", ".m4v", ".mpg", ".mpeg", ".ts",
})


def profile_asr_workers(items: Sequence[Mapping[str, Any]]) -> int:
    """One ASR task at a time; the file itself supplies the parallelism.

    Concurrency used to come from the asr bin running ``wt_instances`` files
    side by side, each pinned to one shard. That kept the GPU busy but put N
    files' worth of non-model state in one process at once, and the 8GB profile
    was already measured over its RAM budget with a single file. Running one
    file with full shard and separator width bounds the live state to that file
    while keeping the same number of models resident.

    Reverses an earlier file-level design; docs/wt-parallelism.md records that
    trade, and the intra-file sharding it refers to has since been removed too
    (ASR is single-worker).

    ``items`` is unused: the answer no longer depends on the profile mix. It
    stays in the signature because callers pass the merged rows.
    """

    return 1


def default_output_path(input_path: Path) -> Path:
    # Group every artifact of one input under out/<stem>/ so a run's outputs
    # stay together instead of scattering across out/.
    return Path("out") / input_path.stem / f"{input_path.stem}.srt"


def resolve_llm_media_for_source(
    source_path: Path,
    *,
    stage: str,
    llm_media: str,
    llm_video: str | Path | None,
) -> tuple[str, str | Path | None, str]:
    """Pick the effective LLM media switch / video file for a local input.

    A video switch needs a video track: a local video file becomes the default
    ``llm_video``. When the input is audio-only, ``--llm-media video``
    downgrades to audio with a notice. Only meaningful once an LLM stage
    actually runs, so earlier stages are left untouched. Returns
    ``(llm_media, llm_video, notice)``; an empty notice means silence.
    """
    if PIPELINE_STAGE_ORDER[stage] < PIPELINE_STAGE_ORDER["translated-srt"]:
        return llm_media, llm_video, ""
    if llm_media != "video":
        return llm_media, llm_video, ""
    if source_path.suffix.lower() in _VIDEO_EXTENSIONS:
        return llm_media, llm_video or source_path, ""
    if llm_video:
        # Audio-only ASR source, but the caller supplied the video separately
        # -- that is exactly what --llm-video is for (transcribe the .wav,
        # show the .mp4 to the LLM). Availability is about the video *source*,
        # not about the ASR input's suffix.
        return llm_media, llm_video, ""
    return (
        "audio",
        llm_video,
        "Note: input is audio-only; downgrading --llm-media video → audio.",
    )


def default_pipeline_paths(
    input_path: str | Path,
    output_path: str | Path | None = None,
) -> PipelinePaths:
    input_path = Path(input_path).expanduser()
    srt_path = Path(output_path).expanduser() if output_path else default_output_path(input_path)
    if srt_path.suffix == "":
        srt_path = srt_path.with_suffix(".srt")
    base = srt_path.with_suffix("")
    return PipelinePaths(
        vocal_audio=base.with_name(f"{base.name}-vocal.ogg"),
        vad_json=base.with_name(f"{base.name}-vad.json"),
        vad_energy_npz=base.with_name(f"{base.name}-vad-energy.npz"),
        aligned_json=base.with_name(f"{base.name}-aligned.json"),
        stable_json=base.with_name(f"{base.name}-stable.json"),
        diar_json=base.with_name(f"{base.name}-diar.json"),
        speaker_judge_json=base.with_name(f"{base.name}-speaker-judge.json"),
        raw_srt=base.with_name(f"{base.name}-raw.srt"),
        translated_srt=base.with_name(f"{base.name}-translated.srt"),
        final_srt=srt_path,
        task_artifact_dir=base.with_name(f"{base.name}.llm-artifacts"),
        metadata_json=metadata_path_for_output(srt_path),
    )


def _use_or_create(
    path: Path,
    step_name: str,
    create: Callable[[Path], str | Path],
) -> Path:
    if path.exists():
        # The stage line already says "已有结果，跳过"; the path itself is
        # detail for someone diagnosing which artifact was picked up.
        current_reporter().debug(
            f"skipping {step_name}", {"existing": str(path)}
        )
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.stem}.part{path.suffix}")
    temporary.unlink(missing_ok=True)
    try:
        produced = Path(create(temporary))
        if not produced.is_file():
            raise RuntimeError(
                f"{step_name} did not create its expected output: {produced}"
            )
        os.replace(produced, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return path


class _StageClock(NamedTuple):
    """Wall and CPU time taken at the same instant.

    Both, because the interesting quantity is the pair. See
    `run_metadata.stage_record` for why the ratio is telemetry and not a
    stall verdict.
    """

    wall: float
    cpu: float


def _stage_clock() -> _StageClock:
    return _StageClock(time.perf_counter(), time.process_time())


def _record_stage_time(
    paths: "PipelinePaths",
    stage_timing: dict[str, Any],
    name: str,
    started: _StageClock,
    *,
    status: str,
) -> None:
    """Write one stage's elapsed time to the sidecar, and say it under verbose.

    Every stage, not only the ones that happened to grow their own timing
    block: `verbose` promises timing, and a run where two of four stages report
    none leaves the reader to work out the difference by subtraction.

    Reused stages are recorded without an elapsed time rather than with zero --
    "did not run" and "ran instantly" are different facts.

    `skipped` is a third fact and not a synonym for either: the stage's real
    work was not asked for (`--no-separate`), but something still ran in its
    place, so it keeps its elapsed time. Asking the sidecar "did this run
    separate?" is the whole reason that answer may not be folded into
    `reused` -- that is the field a bug report is read for.
    """

    ran = status != "reused"
    now = _stage_clock()
    elapsed = now.wall - started.wall if ran else None
    cpu = now.cpu - started.cpu if ran else None
    update_run_metadata(
        paths.metadata_json,
        {
            "timing": {
                "stages": {
                    name: _stage_record_for_current_run(
                        stage_timing,
                        name,
                        stage_record(
                            status=status,
                            elapsed_sec=elapsed,
                            cpu_sec=cpu,
                        ),
                    )
                }
            }
        },
    )
    if elapsed is not None:
        current_reporter().debug(
            "stage timing", {"stage": name, "elapsed_sec": f"{elapsed:.3f}"}
        )


def _stage_record_for_current_run(
    prior_stages: dict[str, Any],
    name: str,
    record: dict[str, Any],
) -> dict[str, Any]:
    """Keep an executed stage when a later pass of the same run reuses it.

    `skipped` counts as having run for this purpose: it is a pass that did
    work and left an artifact behind, so a later pass finding that artifact
    must not overwrite the record of how it got there.
    """

    prior = prior_stages.get(name)
    if (
        record.get("status") == "reused"
        and isinstance(prior, Mapping)
        and prior.get("status") in ("executed", "skipped")
    ):
        chosen = dict(prior)
    else:
        chosen = record
    prior_stages[name] = chosen
    return chosen


def _validate_llm_configuration(
    *,
    knowledge_root: str | Path | None,
    style: str | None,
    style_mode: str | None,
    llm_difficulty: str,
    knowledge: str = "none",
    knowledge_subjects: str | Sequence[str] | None = None,
) -> None:
    """Everything about the LLM stage that is knowable before ASR runs.

    Not an optimisation. Every check below is a configuration fact -- a
    malformed `[chunking]` cap, a typo in `--style`, a bound model group whose
    members cannot hold a window -- and the correction stage finds them only
    after the expensive half of the run has already happened. A run that was
    going to fail should fail in the first second.
    """

    from finesub.llm.knowledge.base import DEFAULT_KNOWLEDGE_ROOT
    from finesub.llm.knowledge.style import resolve_style_keys, resolve_style_selection
    from finesub.llm.routing.capabilities import check_model_group_windows

    # Read again where they are used; checked here only for the early failure.
    settings.MAX_WINDOW_SUBTITLE_TOKENS.resolve()
    settings.RESEARCH_TRANSCRIPT_MAX_TOKENS.resolve()
    check_model_group_windows()
    root = knowledge_root or DEFAULT_KNOWLEDGE_ROOT
    names = resolve_style_selection(
        style, style_mode, knowledge_root=root, difficulty=llm_difficulty
    ).names
    if names:
        resolve_style_keys(root, names)
    if knowledge != "none" and knowledge_subjects:
        from finesub.llm.knowledge.subjects import resolve_subject_keys

        resolve_subject_keys(root, knowledge_subjects)


def _correction_request(
    paths: PipelinePaths,
    source_path: Path,
    stage: str,
    *,
    axes: tuple[str, str, str, str, float],
    llm_video: str | Path | None,
    refined_srt: str | Path | None,
    task_id: str,
    postprocess_profile: int,
    **options: Any,
) -> Any:
    """``run_pipeline``'s LLM keys as the one value the correction task takes,
    checked. The keys that pass through unchanged arrive in ``options`` under
    ``CorrectionRequest``'s own names, so a new one costs a line here only if
    the pipeline has to translate it."""

    from finesub.llm.correction_request import CorrectionRequest, resolved_request
    from finesub.llm.routing.profiles import resolve_profile

    media, retrieval, difficulty, continuity, output_scale = axes
    profile = resolve_profile(media, retrieval, difficulty, continuity, output_scale=output_scale)
    if llm_video and not profile.uses_video:
        raise ValueError("--llm-video only applies when a media switch is video")
    if profile.uses_video and not llm_video:
        raise ValueError("--llm-video is required when a media switch is video")
    return resolved_request(
        CorrectionRequest(
            stable_json=paths.stable_json,
            output_path=paths.final_srt,
            audio_path=source_path,
            video_path=Path(llm_video) if llm_video else None,
            profile=profile,
            refined_srt=Path(refined_srt) if refined_srt else None,
            task_id=task_id or paths.final_srt.stem,
            task_artifact_dir=paths.task_artifact_dir,
            # What the speaker judge listens to (separation may not have run yet).
            vocal_audio=paths.resolve_vocal_audio(),
            # Targeting translated-srt stops before the final postprocess.
            postprocess_profile=None if stage == "translated-srt" else postprocess_profile,
            **options,
        )
    )


def _run_speaker_export_stage(
    paths: PipelinePaths,
    *,
    speaker_tracks: str,
    announce: Callable[..., None],
) -> None:
    """The lossless exports: one SRT per speaker, and/or one ASS holding all.

    Explicitly requested, never automatic -- the single-track SRT is the product
    and these are additions to it (owner, 2026-09-05). A single track has to make
    a choice about overlapping speech; these do not.

    The acceptance here is the completeness invariant, not the labelling: a line
    written into one track too many costs a deletion, a line that reached no
    track has to be noticed, found and retyped. So a broken invariant is a
    warning on every run, not a silent shortfall.

    The speaker judge's verdicts apply when its sidecar is there and still
    belongs to this stable (`judgments.read_judgments`): a moved line goes to
    its new track, a track the judge opened gets its own file. Called after
    raw-srt and again after the LLM stage, so the files always follow the
    sidecar (the second call is `run_pipeline`'s, after the task whose judge
    the raw-srt export could not see yet); every file an earlier export wrote goes first, whichever format
    it was in, so a track that no longer exists leaves nothing behind.
    """

    if speaker_tracks not in ("off", "srt", "ass", "both"):
        raise ValueError(f"--speaker-tracks takes off/srt/ass/both, got {speaker_tracks!r}")
    if speaker_tracks == "off":
        return

    import json

    from .speech.diarization import export as speaker_export
    from .speech.diarization.judgments import judged_speakers, read_judgments
    from .speech.diarization.labels import filled_track, speaker_of

    if not paths.stable_json.exists():
        return
    payload = json.loads(paths.stable_json.read_text(encoding="utf-8"))
    subtitles = payload.get("segments", [])
    speakers = [speaker_of(segment) for segment in subtitles]
    if not any(speaker.track is not None or filled_track(speaker) is not None for speaker in speakers):
        print(  # product output
            "Warning: --speaker-tracks needs speaker labels; run with --diarize acoustics (or a judge level)",
            file=sys.stderr,
        )
        announce("speaker-tracks", "skipped", detail="没有说话人标签")
        return

    announce("speaker-tracks", "running")
    speakers = judged_speakers(subtitles, speakers, read_judgments(paths.stable_json, subtitles))
    destination = paths.stable_json.with_name(f"{paths.stable_json.name.removesuffix('-stable.json')}-speakers")
    destination.mkdir(parents=True, exist_ok=True)
    for stale in (*destination.glob(f"{destination.name}-*.srt"), destination / f"{destination.name}.ass"):
        stale.unlink(missing_ok=True)
    groups = speaker_export.group_by_speaker(subtitles, speakers)
    missing = speaker_export.check_completeness(subtitles, groups)
    if missing:
        print(  # product output
            f"Warning: {len(missing)} subtitle line(s) reached no speaker track "
            f"(first at index {missing[0]}); the tracks are incomplete",
            file=sys.stderr,
        )

    written = []
    if speaker_tracks in ("srt", "both"):
        for name, text in speaker_export.render_speaker_srts(subtitles, speakers).items():
            target = destination / f"{destination.name}-{name}.srt"
            target.write_text(text, encoding="utf-8")
            written.append(target.name)
    if speaker_tracks in ("ass", "both"):
        target = destination / f"{destination.name}.ass"
        target.write_text(speaker_export.render_ass(subtitles, speakers), encoding="utf-8")
        written.append(target.name)
    current_reporter().debug(
        "speaker tracks", {"directory": str(destination), "files": len(written)}
    )


def _planned_stages(target_order: int, *, diarize: str) -> list[str]:
    """Which stage lines a person should expect to see, reused ones included.

    Diarization is not a rung on the `--stage` ladder -- that ladder names which
    *product* to stop at, and speaker labels are an addition to the subtitles
    rather than another kind of them. It still gets its own line when it runs,
    because it costs GPU minutes and a watcher should see where they went.
    """

    planned = [name for name, order in PIPELINE_STAGE_ORDER.items() if order <= target_order]
    if diarize != "off" and "stable" in planned:
        planned.insert(planned.index("stable") + 1, "diarize")
    return planned


def _warn_on_backend_mismatch(diar_json, requested, diarization) -> None:
    """Say so when the cached timeline came from a different backend.

    Never fatal and never a reason to recompute -- see the call site. A bad read
    is not worth failing over either: the point is the label, and an unreadable
    cache produces its own error where it is actually used.

    ⚠ **Only a `Warning:` when a backend was NAMED.** Compared against the
    resolved backend either way, but the default has moved (Sortformer, then a
    probe, then DiariZen on 2026-09-21, then Nemotron-3 on 2026-09-25), and every
    older task's resume would otherwise print this for a run where nothing on the
    command line named a backend. That
    is the case the resume rule covers -- current parameters do not retroactively
    redefine finished stages -- so an implicit mismatch is reported where run
    detail lives, not on stderr.
    """
    try:
        resolved = diarization.resolve_backend(requested).name
    except ValueError:  # an unknown name is reported where it is resolved for real
        return
    try:
        cached = diarization.read_timeline(diar_json).backend
    except Exception:  # the consumer reports an unreadable timeline properly
        return
    if cached == resolved:
        return
    note = (f"{diar_json.name} was produced by the {cached} backend, not {resolved}; "
            "it is reused as-is. Delete it to recompute.")
    if requested is None:
        current_reporter().debug("diarization backend differs", {"cached": cached,
                                                                 "resolved": resolved})
        return
    print(f"Warning: {note}", file=sys.stderr)  # product output


def _diarization_device(device: str | None, gpu_tier: str) -> str:
    """Where the timeline is computed: the run's device, unless the tier or the card says CPU.

    Every backend runs on CPU, so a run without a usable GPU still gets labels -- slowly. Measured
    once for the default (2026-09-25, 8 threads): Nemotron-3 + vbx RTF 0.20 on CPU (10 min of
    hina-04) against 0.009 on CUDA (all 59 min), and on another file the CPU chain differed from
    NeMo on the GPU by 0.19% of frames. Said on stderr, because an hour of audio then costs about
    twelve minutes. (DiariZen, research only: RTF 0.86.)
    """
    from .speech.runtime.device import resolve_device
    from .speech.runtime.resources import get_resource_profile

    requested = device or "cuda"
    if not get_resource_profile(gpu_tier).gpu:
        requested = "cpu"
    chosen = resolve_device(requested, context="speaker diarization")
    if not str(chosen).startswith("cuda"):
        current_reporter().warning(
            "diarization-on-cpu",
            "说话人分离在 CPU 上跑：约为音频时长的五分之一（1 小时音频约 12 分钟）",
            impact="只影响这一阶段的耗时",
        )
    return chosen


def warn_if_the_judge_cannot_run(diarize: str, stage: str) -> None:
    """A judge level whose item stops before the LLM stage gets only the acoustic labels: say so. Called once
    per item with its target stage (`pipeline._build_item`), never from `run_pipeline`: an item past raw-srt
    runs it twice, the first time only up to raw-srt."""
    if judge_of(diarize) == "off" or PIPELINE_STAGE_ORDER[stage] >= PIPELINE_STAGE_ORDER["translated-srt"]:
        return
    print(  # product output
        f"Warning: --diarize {diarize}: the LLM speaker judge runs in the LLM stage, and this run "
        f"stops at {stage}; only the acoustic labels this time",
        file=sys.stderr,
    )
    current_reporter().warning("speaker-judge-not-run", f"--diarize {diarize} 的说话人裁判在 LLM 阶段，这次只跑到 {stage}",
                               impact="只有声学标签")


def _run_diarization_stage(
    paths: PipelinePaths,
    *,
    diarize: str,
    device: str | None,
    gpu_tier: str,
    announce: Callable[..., None],
    backend: str | None = None,
    max_speakers: int | None = None,
) -> None:
    """Speaker timeline after stabilization, when asked for.

    Two halves with opposite properties, so two existence checks rather than one:
    the timeline is cached because it cannot be reproduced, while writing labels
    onto stable.json is cheap and deterministic and may always be redone. A run
    that already has both does nothing at all.

    Failure never takes the run down. The subtitles are the product; speaker
    labels are an addition to them, and a missing model or an unusable GPU should
    cost the labels, not the subtitles -- with a `Warning:`. Labelling from a cached
    timeline needs neither the GPU nor the model. (There used to be an `auto` that
    ran "whenever this machine can"; owner 2026-09-21 removed it.)
    """

    if diarize not in DIARIZE_LEVELS:
        raise ValueError(f"--diarize takes {'/'.join(DIARIZE_LEVELS)}, got {diarize!r}")
    if diarize == "off":
        return

    from .speech.diarization import stage as diarization

    timeline_exists = paths.diar_json.exists()
    annotated = diarization.is_annotated(paths.stable_json)
    if timeline_exists:
        # Owner 2026-09-19: a cached timeline does NOT expire because the run
        # asked for a different backend. It is neither corrupt nor unreadable,
        # so backend identity is provenance and only warns -- the way to get the
        # other backend's answer is to delete the artifact, which is the rule
        # every stage already has. Warning rather than silence, because serving
        # X while the command line said Y is the part nobody would notice.
        _warn_on_backend_mismatch(paths.diar_json, backend, diarization)
    if timeline_exists and annotated:
        announce("diarize", "reused")
        return
    announce("diarize", "running")
    reporter = current_reporter()
    try:
        if not timeline_exists:
            from .speech.diarization.evidence import evidence_path

            result = diarization.run_diarization(
                paths.resolve_vocal_audio(), device=_diarization_device(device, gpu_tier),
                backend=backend,
                evidence_path=evidence_path(paths.diar_json), max_speakers=max_speakers,
            )
            diarization.write_timeline(result, paths.diar_json)
            reporter.debug(
                "diarization timeline",
                {
                    "backend": result.backend,
                    "tracks": len({segment.track for segment in result.tracks}),
                    "segments": len(result.tracks),
                    "segment_overlap_ratio": round(result.overlap, 4),
                },
            )
        diarization.apply_to_stable(paths.stable_json, paths.diar_json)
    except diarization.DiarizationUnavailable as error:
        # Documented degradation, not a crash: `Warning:` to stderr is the
        # repository's convention for "the run continues without this".
        print(f"Warning: speaker diarization skipped: {error}", file=sys.stderr)  # product output
        if judge_of(diarize) != "off":
            print(  # product output
                f"Warning: --diarize {diarize}: no speaker labels, so the LLM speaker judge will not run either",
                file=sys.stderr,
            )
        announce("diarize", "skipped", detail="模型或依赖不可用")


def _run_vocal_stage(
    paths: "PipelinePaths",
    *,
    stage: str,
    target_order: int,
    aligned_required: bool,
    source_path: Path,
    separate: bool,
    gpu_tier: str,
    device: str,
    separator_sample_rate: int | None,
    stage_timing: dict[str, Any],
    announce: Callable[..., None],
) -> None:
    """The vocal stage: decide whether it is wanted, then deliver or skip it.

    Lifted out of `run_pipeline` when `--no-separate` landed (2026-09-03): the
    branch answers three separate questions -- is the track wanted at all, is
    one already here, and does this run separate or merely transcode -- and
    reading them inside a five-hundred-line caller made the last of the three
    look like a special case of the first two.

    `announce` is the caller's, because the `[n/N]` prefix belongs to the run's
    plan rather than to any one stage.
    """

    reporter = current_reporter()
    if target_order >= PIPELINE_STAGE_ORDER["vocal"]:
        # Vocal audio only feeds VAD-ASR. A stable JSON satisfies every later
        # consumer without requiring the aligned artifact to be backfilled.
        # When stable is absent, an existing aligned JSON likewise avoids both
        # vocal separation and VAD-ASR.
        vocal_needed = stage == "vocal" or (
            aligned_required and not paths.aligned_json.exists()
        )
        if vocal_needed:
            # Either delivery counts as done, which is why this asks
            # `resolve_vocal_audio` rather than looking for the shape this stage
            # happens to write. `.ogg` is what it asks separation for; a `.flac`
            # beside it is separation's lossless mode, and every reader below
            # takes it. Checking only the `.ogg` meant a run that already held
            # the lossless track paid for the whole GPU stage again to end up
            # with a smaller copy of what it had.
            existing_vocal = paths.resolve_vocal_audio()
            vocal_existed = existing_vocal.exists()
            if vocal_existed:
                vocal_status = "reused"
            elif separate:
                vocal_status = "executed"
            else:
                vocal_status = "skipped"
            announce(
                "vocal",
                vocal_status,
                # Something does run in the skipped case -- the same 16 kHz mono
                # delivery, made by transcoding instead of separating -- so this
                # is not `reused` and the line says which of the two it is.
                detail="" if vocal_status != "skipped" else "跳过分离，直接转出 ASR 轨",
            )
            vocal_t0 = _stage_clock()
            separator_metadata: dict[str, Any] = {}
            if vocal_existed:
                reporter.debug(
                    "skipping vocal separation", {"existing": str(existing_vocal)}
                )
            elif not separate:
                # README_DEV.md, 「Pipeline 复用规则」: the
                # stage's *contract* is honoured (a 16 kHz mono track at the
                # path everything downstream reads), only the separation itself
                # is not run. Every consumer, the existence-based skip and
                # resume therefore need no special case for "no vocal file".
                reporter.debug(
                    "vocal separation not requested; transcoding the source",
                    {"source": str(source_path)},
                )
                _use_or_create(
                    paths.vocal_audio,
                    "vocal delivery",
                    lambda temporary: vocal_separation.encode_asr_delivery(
                        source_path,
                        temporary,
                        # A compressed or video source is decoded to a
                        # temporary flac first, and a *failed* encode keeps it
                        # on purpose so a retry need not decode again. Only
                        # this record can name it for the cleanup afterwards.
                        run_metadata_path=paths.metadata_json,
                    ),
                )
            else:
                _use_or_create(
                    paths.vocal_audio,
                    "vocal separation",
                    lambda temporary: vocal_separation.run_vocal_separation(
                        source_path,
                        output_path=temporary,
                        gpu_tier=gpu_tier,
                        # The request, not a resolved device: separation
                        # decides for itself (torch), the ASR stage asks
                        # CTranslate2. What they share is the user's intent.
                        device=device,
                        separator_sample_rate=separator_sample_rate,
                        metadata_sink=separator_metadata,
                        run_metadata_path=paths.metadata_json,
                    ),
                )
            _record_stage_time(
                paths,
                stage_timing,
                "vocal_separation",
                vocal_t0,
                status=vocal_status,
            )
            if separator_metadata:
                update_run_metadata(
                    paths.metadata_json,
                    {"workers": {"vocal_separation": separator_metadata}},
                )
        elif not paths.resolve_vocal_audio().exists() and aligned_required:
            # Not "reused" in the strict sense -- the vocal audio is not there
            # and is not wanted. For the progress list the two are the same
            # thing: nothing runs, and the stage is behind us.
            announce("vocal", "reused")
            reporter.debug(
                "skipping vocal separation; a later artifact already exists",
                {
                    "existing": str(
                        paths.aligned_json
                        if paths.aligned_json.exists()
                        else paths.stable_json
                    )
                },
            )
        elif not vocal_needed:
            announce("vocal", "reused")
            update_run_metadata(
                paths.metadata_json,
                {
                    "timing": {
                        "stages": {
                            "vocal_separation": _stage_record_for_current_run(
                                stage_timing,
                                "vocal_separation",
                                stage_record(status="reused"),
                            )
                        }
                    }
                },
            )


def run_pipeline(
    input_path: str | Path,
    *,
    output_path: str | Path | None = None,
    model_name: str = asr_align.DEFAULT_MODEL,
    device: str | None = "cuda",
    language: Optional[str] = None,
    gap_sec: float = asr_align.DEFAULT_GAP_SEC,
    # `auto` = ask the card. A tier names what CLASS of card this is, not a
    # cap on what the pipeline may use; `resolve_gpu_tier` owns the answer.
    gpu_tier: str = AUTO_GPU_TIER,
    separator_sample_rate: int | None = None,
    # None = follow `[separator] enabled`, then the stage's default (on).
    # `--no-separate` is for input that is already a clean vocal track: the
    # stage still delivers its 16 kHz mono artifact, it just transcodes rather
    # than separating.
    separate: bool | None = None,
    # None = follow config.toml, then the stage's default. The stage owns
    # that resolution so every front end lands on the same answer.
    vad_silero_assist: bool | None = None,
    qwen_verify: str = "auto",
    lang_redecode: str = "auto",
    # Off for 0.6.0, and `auto` behaves as off: see `_run_diarization_stage`.
    diarize: str = "off",
    # None = the stage's own `DEFAULT_BACKEND`; never a second copy here.
    diarize_backend: str | None = None,
    diarize_max_speakers: int | None = None,
    # Lossless extras, asked for explicitly: see `_run_speaker_export_stage`.
    speaker_tracks: str = "off",
    split_length_scale: float | None = None,
    asr_decode_batch: int | str | None = None,
    asr_context: str | None = None,
    word: bool = False,
    asr_stabilize_profile: int = asr_stabilize.DEFAULT_ASR_STABILIZE_PROFILE,
    stage: str = "raw-srt",
    # None = the active preset's defaults, then the built-in ones
    # (`resolve_llm_switches`); so is `knowledge` below.
    llm_media: str | None = None,
    llm_retrieval: str | None = None,
    llm_difficulty: str | None = None,
    llm_continuity: str | None = None,
    llm_parallel_windows: int | None = None,
    llm_fast: str = "auto",
    llm_output_scale: float = 1.0,
    llm_video: str | Path | None = None,
    extra_info: str = "",
    extra_style: str = "",
    #: Named style entries for the LLM correction (`--style`). `None` falls
    #: through to `[llm] style` in the config; the backend resolver decides,
    #: not argparse (README_DEV.md 开发原则's option-default chain).
    style: str | None = None,
    #: none / read / update — see `knowledge/style.py`. `None` reads
    #: `[llm] style_mode`, then defaults to `read`.
    style_mode: str | None = None,
    #: Knowledge subjects the task names (`llm/knowledge/subjects.py`).
    knowledge_subjects: str | Sequence[str] | None = None,
    download_video_source: bool = True,
    knowledge: str | None = None,
    refined_srt: str | Path | None = None,
    knowledge_root: str | Path | None = None,
    task_artifact_dir: str | Path | None = None,
    task_id: str = "",
    task_summary: str = "",
    test_profile: bool = False,
    postprocess_profile: int = 0,
    max_retries_per_window: int | None = None,
    max_replacements_per_window: int | None = None,
    resume: bool = True,
    _run_started_monotonic: float | None = None,
    _prior_timing: Mapping[str, Any] | None = None,
    _batch_workers: Mapping[str, int] | None = None,
) -> PipelinePaths:
    # Taken from the caller's binding rather than passed in: the stages deep
    # inside this call tree read it the same way, and two mechanisms for one
    # thing is how they drift apart. A front end wraps its run in
    # `reporting_to(...)`; nothing bound means nothing shown.
    reporter = current_reporter()
    run_t0 = (
        float(_run_started_monotonic)
        if _run_started_monotonic is not None
        else time.perf_counter()
    )
    stage_timing: dict[str, Any] = dict(_prior_timing or {})
    if stage not in PIPELINE_STAGE_ORDER:
        raise ValueError(f"Unknown pipeline stage: {stage}")
    if word and PIPELINE_STAGE_ORDER[stage] > PIPELINE_STAGE_ORDER["raw-srt"]:
        raise ValueError("--word can only be used through the raw-srt stage.")
    separate = settings.SEPARATE.resolve(separate)
    # Their stage resolves these itself; they are checked here too so that a
    # bad value -- a manifest row, a typo in config.toml -- fails the run
    # before separation, the heaviest stage, rather than after it.
    settings.VAD_SILERO_ASSIST.resolve(vad_silero_assist)
    settings.SPLIT_LENGTH_SCALE.resolve(split_length_scale)
    # `None` is "not chosen" -- a front end sends it so that a bare `cpu` tier
    # can be told apart from an explicit `cuda` -- and it means the code
    # default. Normalised here, once, so no stage ever sees None and reads it
    # as something other than "the user asked for the card".
    device = str(device or "cuda")
    switches, _ = resolve_llm_switches(stage, llm_media, llm_retrieval, llm_difficulty, llm_continuity,
                                       knowledge, llm_output_scale, llm_parallel_windows)
    llm_media, llm_retrieval, llm_difficulty, llm_continuity, knowledge = switches.vector()
    if PIPELINE_STAGE_ORDER[stage] > PIPELINE_STAGE_ORDER["raw-srt"]:
        _validate_llm_configuration(
            knowledge_root=knowledge_root,
            style=style,
            style_mode=style_mode,
            llm_difficulty=llm_difficulty,
            knowledge=knowledge,
            knowledge_subjects=knowledge_subjects,
        )
    # Normalize "auto" to None: whisper uses None for auto-detection;
    # the string "auto" is not a valid language code and would raise.
    if language and language.strip().lower() == "auto":
        language = None
    source_arg = str(input_path)
    input_is_url = _is_media_url(source_arg)
    if input_is_url:
        # Wall only, no CPU: a download is network wait by construction, so a
        # wall:CPU ratio here would be a large number that means nothing.
        download_t0 = time.perf_counter()
        source_path, paths, llm_video, source_extra_info = prepare_url_input(
            source_arg,
            output_path=output_path,
            llm_video=llm_video,
            download_video_source=download_video_source,
        )
        extra_info = compose_url_extra_info(
            source_arg,
            source_extra_info,
            extra_info,
            stage=stage,
            asr_context=asr_context,
        )
        stage_timing["download"] = stage_record(
            status="executed",
            elapsed_sec=time.perf_counter() - download_t0,
        )
    else:
        source_path = Path(input_path).expanduser().resolve()
        if not source_path.exists():
            raise FileNotFoundError(f"Input not found: {source_path}")
        paths = default_pipeline_paths(source_path, output_path)
        llm_media, llm_video, media_notice = resolve_llm_media_for_source(
            source_path,
            stage=stage,
            llm_media=llm_media,
            llm_video=llm_video,
        )
        if media_notice:
            # Says the LLM stage will not get the video it was asked for, which
            # changes the result rather than merely describing the run.
            current_reporter().warning("media-downgraded", media_notice)
        # A video source goes on to the stages as it is, like the URL branch
        # above. Separation decodes its own lossless copy when soundfile cannot
        # read the container, and the LLM clip cutter runs ffmpeg either way;
        # narrowing the audio here used to cost a lossy generation and a mono
        # 16 kHz downmix before separation -- which runs a 44.1 kHz stereo
        # model -- for an artifact nothing needed.
    paths.final_srt.parent.mkdir(parents=True, exist_ok=True)
    resolved_task_artifact_dir = (
        Path(task_artifact_dir).expanduser()
        if task_artifact_dir is not None
        else paths.task_artifact_dir
    )
    if resolved_task_artifact_dir != paths.task_artifact_dir:
        paths = paths._replace(task_artifact_dir=resolved_task_artifact_dir)
    # The LLM keys become the one value the correction task takes, here and not
    # at the LLM stage: building it is where a contradictory request is found
    # (`--knowledge update` on an efficiency run, a video switch without a video), and
    # finding that after ASR is finding it at the most expensive moment.
    llm_request = None
    if PIPELINE_STAGE_ORDER[stage] >= PIPELINE_STAGE_ORDER["translated-srt"]:
        llm_request = _correction_request(
            paths, source_path, stage,
            axes=(llm_media, llm_retrieval, llm_difficulty, llm_continuity, llm_output_scale),
            llm_video=llm_video, fast=llm_fast, parallel_windows=switches.parallel_windows,
            extra_info=extra_info, extra_style=extra_style, style=style, style_mode=style_mode,
            knowledge=knowledge, knowledge_root=knowledge_root, refined_srt=refined_srt,
            knowledge_subjects=knowledge_subjects,
            switch_sources=switches.sources,
            task_id=task_id, task_summary=task_summary, postprocess_profile=postprocess_profile,
            max_retries_per_window=max_retries_per_window,
            max_replacements_per_window=max_replacements_per_window,
            speaker_judge=judge_of(diarize), resume=resume, test_profile=test_profile,
        )

    from .media.ffmpeg import describe_ffmpeg

    update_run_metadata(
        paths.metadata_json,
        {
            "task_id": task_id or paths.final_srt.stem,
            "source": source_arg,
            "timing": {"stages": stage_timing},
            # The launchers may reuse a system ffmpeg instead of the managed
            # one, so which binary ran is no longer implied by the install.
            "tools": {"ffmpeg": describe_ffmpeg()},
            **(
                {"workers": {"batch": dict(_batch_workers)}}
                if _batch_workers
                else {}
            ),
        },
    )
    def announce(stage_key: str, status: str, *, detail: str = "") -> None:
        """Report which stage the run is entering, and whether it does work.

        Deliberately at the branch level rather than inside `_use_or_create`:
        a stage skipped outright never reaches that helper (see the `elif`
        arms below), the LLM stage does not go through it at all, and one of
        its call sites is not a pipeline stage in the first place. Reporting
        from the branches is also what keeps this in step with the
        executed/reused/skipped status written into the run metadata.

        ✱ `skipped` is deliberately *not* folded into `reused`: only `reused`
        means nothing ran, and the one stage that can be skipped still produces
        its artifact by another route. `detail` says which route, which is why
        this needed no third state in the reporting contract.
        """

        reporter.stage_started(stage_key, reused=status == "reused", detail=detail)

    target_order = PIPELINE_STAGE_ORDER[stage]
    # Every stage at or below the target may be announced, reused ones
    # included -- which is exactly the denominator a person watching wants.
    reporter.planned(_planned_stages(target_order, diarize=diarize))
    aligned_required = stage == "aligned" or (
        target_order >= PIPELINE_STAGE_ORDER["stable"]
        and not paths.stable_json.exists()
    )
    _run_vocal_stage(
        paths,
        stage=stage,
        target_order=target_order,
        aligned_required=aligned_required,
        source_path=source_path,
        separate=separate,
        gpu_tier=gpu_tier,
        device=device,
        separator_sample_rate=separator_sample_rate,
        stage_timing=stage_timing,
        announce=announce,
    )
    if target_order >= PIPELINE_STAGE_ORDER["aligned"] and aligned_required:
        aligned_existed = paths.aligned_json.exists()
        announce("aligned", "reused" if aligned_existed else "running")
        # Built here, not inside the stage: the selection needs the knowledge
        # base and `speech` must not import `llm`, so what crosses is a string.
        asr_context_text, asr_context_report = build_asr_context(
            asr_context,
            knowledge_root=knowledge_root,
            # The extra-info block already carries the scraped title when
            # there is one (`compose_url_extra_info`), plus whatever the user
            # wrote -- a user who names the subject is at least as good a
            # signal as a title, and costs nothing to read.
            text=extra_info,
        )
        if asr_context_text:
            update_run_metadata(
                paths.metadata_json, {"asr_context": asr_context_report}
            )
        asr_t0 = _stage_clock()
        _use_or_create(
            paths.aligned_json,
            "VAD-ASR",
            lambda temporary: vad_asr.run_vad_asr(
                paths.resolve_vocal_audio(),
                output_path=temporary,
                model_name=model_name,
                device=device,
                language=language,
                gap_sec=gap_sec,
                gpu_tier=gpu_tier,
                vad_silero_assist=vad_silero_assist,
                qwen_verify=qwen_verify,
                lang_redecode=lang_redecode,
                split_length_scale=split_length_scale,
                asr_decode_batch=asr_decode_batch,
                asr_context=asr_context_text,
                run_metadata_path=paths.metadata_json,
                vad_prefix_path=paths.vad_json,
            ),
        )
        _record_stage_time(
            paths,
            stage_timing,
            "asr",
            asr_t0,
            status="reused" if aligned_existed else "executed",
        )
    elif target_order >= PIPELINE_STAGE_ORDER["aligned"]:
        announce("aligned", "reused")
        update_run_metadata(
            paths.metadata_json,
            {
                "timing": {
                    "stages": {
                        "asr": _stage_record_for_current_run(
                            stage_timing,
                            "asr",
                            stage_record(status="reused"),
                        )
                    }
                },
            },
        )
    if target_order >= PIPELINE_STAGE_ORDER["stable"]:
        stable_existed = paths.stable_json.exists()
        announce("stable", "reused" if stable_existed else "running")
        stable_t0 = _stage_clock()
        _use_or_create(
            paths.stable_json,
            "ASR stabilization",
            lambda temporary: asr_stabilize.run_asr_stabilize(
                paths.aligned_json,
                output_path=temporary,
                profile=asr_stabilize_profile,
            ),
        )
        _record_stage_time(
            paths,
            stage_timing,
            "stabilize",
            stable_t0,
            status="reused" if stable_existed else "executed",
        )
        _run_diarization_stage(paths, diarize=diarize, device=device, gpu_tier=gpu_tier,
                               announce=announce, backend=diarize_backend,
                               max_speakers=diarize_max_speakers)
    if target_order >= PIPELINE_STAGE_ORDER["raw-srt"]:
        raw_srt_existed = paths.raw_srt.exists()
        announce("raw-srt", "reused" if raw_srt_existed else "running")
        raw_srt_t0 = _stage_clock()
        from .subtitles.model import warn_on_invalid_srt
        from .subtitles.postprocess import (
            TIMELINE_POSTPROCESS_PROFILES,
            postprocess_srt_file,
        )

        stable_audio_end = asr_stabilize.recorded_audio_end(paths.stable_json)

        def _create_raw_srt(temporary: Path) -> Path:
            # Every write below lands in `temporary`, and there are up to three
            # of them. Validating each would report one finding once per pass,
            # against a `.part` path the reader cannot open -- so the passes
            # stay quiet and the finished artifact is checked once, under the
            # name it will actually have.
            produced = to_srt.convert_json_to_srt(
                paths.stable_json,
                output_path=temporary,
                word=word,
                validate=False,
            )
            if postprocess_profile in (0, 1):
                # Timeline half of the final profile; the ASR text stays as is.
                for timeline_profile in TIMELINE_POSTPROCESS_PROFILES:
                    postprocess_srt_file(
                        produced, profile=timeline_profile, validate=False,
                        audio_end=stable_audio_end,
                    )
            produced = Path(produced)
            # Raw text is before layout: only lines far over the budget are
            # worth a mention, as one banded summary (see the validator).
            warn_on_invalid_srt(
                produced.read_text(encoding="utf-8"), where=str(paths.raw_srt),
                budget_stage="raw-srt", budget_above=35,
            )
            return produced

        _use_or_create(
            paths.raw_srt,
            "raw SRT export",
            _create_raw_srt,
        )
        _run_speaker_export_stage(
            paths, speaker_tracks=speaker_tracks, announce=announce
        )
        _record_stage_time(
            paths,
            stage_timing,
            "raw_srt",
            raw_srt_t0,
            status="reused" if raw_srt_existed else "executed",
        )
    if target_order >= PIPELINE_STAGE_ORDER["translated-srt"]:
        llm_existed = paths.translated_srt.exists() or paths.final_srt.exists()
        # One call covers what the progress list shows as two stages, and which
        # of them actually runs depends on what is already on disk -- targeting
        # final-srt from nothing produces it in a single correction pass, with
        # no translated-srt in between.
        final_done = paths.final_srt.exists()
        translated_done = final_done or paths.translated_srt.exists()
        wants_final = target_order >= PIPELINE_STAGE_ORDER["final-srt"]
        announce("translated-srt", "reused" if translated_done else "running")
        if wants_final and translated_done:
            # Nothing but the post-processing tail is left, and that is what
            # the call below will do -- so this one is announced up front.
            announce("final-srt", "reused" if final_done else "running")
        llm_t0 = _stage_clock()
        from finesub.llm.correction_translation import run_correction_task

        # The task skips what is on disk itself (and still owes the knowledge
        # update for an output an earlier run left behind).
        run_correction_task(llm_request)
        if judge_of(diarize) != "off":
            _run_speaker_export_stage(paths, speaker_tracks=speaker_tracks, announce=announce)
        if wants_final and not translated_done:
            # The single correction pass above produced the final SRT itself,
            # post-processing included. This marks the transition; there is no
            # work left behind it.
            announce("final-srt", "running")
        _record_stage_time(
            paths,
            stage_timing,
            "llm_harness",
            llm_t0,
            status="reused" if llm_existed else "executed",
        )
        update_run_metadata(
            paths.metadata_json,
            {"llm_rounds": summarize_llm_rounds(paths.task_artifact_dir)},
        )
    completed = _stage_output(paths, stage)
    total_sec = time.perf_counter() - run_t0
    update_run_metadata(
        paths.metadata_json,
        {
            "timing": {"total_sec": round(max(0.0, total_sec), 3)},
            "completed_stage": stage,
            "completed_output": str(completed),
        },
    )
    if target_order >= PIPELINE_STAGE_ORDER["translated-srt"]:
        from finesub.llm.task_report import write_task_report

        write_task_report(
            paths.task_artifact_dir,
            task_id=task_id or paths.final_srt.stem,
            outputs={
                "raw_srt": str(paths.raw_srt),
                "translated_srt": str(paths.translated_srt),
                **(
                    {"final_srt": str(paths.final_srt)}
                    if paths.final_srt.exists()
                    else {}
                ),
            },
            run_metadata_path=paths.metadata_json,
        )
    reporter.completed(completed, total_sec)
    return paths


def _is_media_url(value: str) -> bool:
    from .media.source import is_url

    return is_url(value)


def source_title_line(url: str) -> str:
    """The scraped title as one extra-info line, or ``""``.

    It sits directly under `视频来源 URL:` because it describes the same thing,
    and it is **labelled rather than pasted bare**: everything else in the
    extra-info block was either computed by us or typed by the user, and this
    one line was scraped off someone else's page. A reader (human or model)
    that cannot tell those apart will treat a wrong title as a fact about the
    recording.

    The episode subject is what the term-injection experiment found missing:
    knowing it raised agreement with the corrector from 8.3% to 33.3% against
    an oracle ceiling of 53.3%, and the pipeline recorded nothing that said
    which subject a recording belonged to (`crispasr-followups.md` P9). A title
    is the cheapest thing that does.

    ⚠ Two things this line is NOT. The measured gain came from using the
    subject to pick a term list -- injecting the subject *name* alone scored
    the same as the control -- and the number is agreement with the corrector,
    not recall or accuracy. And the experiment's consumer was the Qwen referee,
    while this line goes to the LLM layer's extra-info. So P9's own wiring
    (title -> subject -> terms -> Qwen prompt) is still missing; this only
    makes the subject available at all.

    The video *description* is deliberately not here: url extraction usually
    picks it up anyway, and descriptions carry sponsor blurbs and channel
    boilerplate that would pre-inject entries with nothing to do with the
    episode.
    """

    from .media.source import resolve_video_title
    from .paths import resolve_reference_data_root

    title = resolve_video_title(url, resolve_reference_data_root())
    return f"视频标题（yt-dlp 自动抓取，未经人工核对）: {title}" if title else ""


#: How much of the knowledge base the recogniser's referee is told about.
#:
#: ``off``    inject nothing (default: this changes what a second model hears,
#:            and the measurement that would justify a different default has
#:            not been run -- `docs/plans/crispasr-followups.md` P9).
#: ``terms``  every NAME and nothing else: labelled entries, term lines trimmed
#:            to source form / canonical / aliases. The strings a recogniser
#:            has to get right, without the prose a corrector reads.
#: ``full``   what the correction layer would see: entry bodies whole.
ASR_CONTEXT_LEVELS = ("off", "terms", "full")


def build_asr_context(
    level: str | None,
    *,
    knowledge_root: str | Path | None,
    text: str,
) -> tuple[str, dict[str, object]]:
    """Knowledge names for the ASR referee, and a report of what was picked.

    ⚠ This function is the whole reason the layering holds. `speech` must not
    import `llm`, and the referee lives in `speech`; so the selection happens
    HERE -- in the one module that already knows both sides -- and what crosses
    the boundary is a plain string. No new dependency in either direction.

    Selection reuses the retrieval the correction layer already runs rather
    than inventing a second one: entry-level keyword matching plus the
    sub-entry (term) matcher, over the same free text. ``text`` is the scraped
    title AND the user's extra-info -- a user who names the episode subject is
    at least as good a signal as a scraped title, and costs nothing to read.
    """

    chosen = str(level or "off").strip().lower()
    report: dict[str, object] = {"level": chosen}
    if chosen not in ASR_CONTEXT_LEVELS:
        chosen, report["level"] = "off", "off"
    if chosen == "off" or not (text or "").strip():
        return "", report

    from .llm.knowledge.base import (
        DEFAULT_KNOWLEDGE_ROOT,
        load_preinjected_entries,
        match_terms,
        render_term_matches,
        term_lines_only,
    )

    root = knowledge_root or DEFAULT_KNOWLEDGE_ROOT
    try:
        entries, matches = load_preinjected_entries(root, text)
        terms = match_terms(
            root, text, exclude_subjects=[match.key for match in matches]
        )
    except Exception as exc:  # a missing or unreadable store is not fatal here
        current_reporter().debug("no asr context", {"error": str(exc)})
        return "", report

    names_only = chosen == "terms"
    blocks = []
    for key, body in entries.items():
        rendered = term_lines_only(body) if names_only else str(body).strip()
        if rendered:
            blocks.append(f"# {key}\n{rendered}")
    term_block = render_term_matches(terms, names_only=names_only)
    if term_block:
        blocks.append(term_block)

    report["entries"] = list(entries)
    report["terms"] = [match.to_dict() for match in terms]
    return "\n".join(blocks), report


def stage_consumes_extra_info(stage: str, *, asr_context: str | None = None) -> bool:
    """Does a run stopping at `stage` have anything that reads extra-info?

    Two consumers, and they sit at different depths. The correction task is
    the obvious one, so a run stopping at `vocal` through `raw-srt` normally
    reads it never -- and that matters, because one line of extra-info costs a
    network probe for the title.

    ⚠ `--asr-context` is the second, and it reads extra-info at **`aligned`**.
    Miss that and the flag looks wired while the default
    `finesub URL --asr-context terms` sees only the URL and a file path: the
    title it is supposed to match subjects from was never fetched. The gate
    therefore asks about the context switch too, not only the stage.

    But it asks about BOTH. A `--stage vocal` run stops before the referee
    exists, so the switch buys nothing there and the network probe would be
    paid for a consumer that never runs -- the same waste the stage half of
    this gate exists to avoid.
    """

    reached = PIPELINE_STAGE_ORDER.get(stage, PIPELINE_STAGE_ORDER["raw-srt"])
    if (
        reached >= PIPELINE_STAGE_ORDER["aligned"]
        and str(asr_context or "off").strip().lower() not in ("", "off")
    ):
        return True
    return reached >= PIPELINE_STAGE_ORDER["translated-srt"]


def compose_url_extra_info(
    url: str,
    source_info: str,
    user_extra_info: str,
    *,
    stage: str,
    asr_context: str | None = None,
) -> str:
    """The extra-info block for a URL input, in one place.

    One place because there are two callers -- the pipeline and the batch
    runner's download stage -- and they had drifted into two copies of the same
    join. A field added to one copy simply would not exist in batch runs, with
    nothing to say so.
    """

    return "\n".join(
        part
        for part in (
            f"视频来源 URL: {url}",
            source_title_line(url)
            if stage_consumes_extra_info(stage, asr_context=asr_context)
            else "",
            source_info,
            user_extra_info,
        )
        if part
    )


def resolve_llm_switches(
    stage: str,
    llm_media: str | None,
    llm_retrieval: str | None,
    llm_difficulty: str | None,
    llm_continuity: str | None,
    knowledge: str | None,
    llm_output_scale: float,
    llm_parallel_windows: int | None = None,
) -> tuple[Any, Any]:
    """The run's LLM switches (``correction_request.resolve_switches``) and the
    profile they make, which also validates them.

    Called before anything is downloaded or separated -- by ``run_pipeline``
    and by the batch runner's download stage -- so a conflicting vector costs
    nothing, and every later use reads the resolved values.
    A run that stops at ``raw-srt`` skips the preset layers: its switches are
    only validated, and a route configuration it never uses must not fail it.
    """

    from finesub.llm.correction_request import resolve_switches
    from finesub.llm.routing.profiles import resolve_profile

    switches = resolve_switches(
        {
            "media": llm_media,
            "retrieval": llm_retrieval,
            "difficulty": llm_difficulty,
            "continuity": llm_continuity,
            "knowledge": knowledge,
            "parallel_windows": llm_parallel_windows,
        },
        presets=PIPELINE_STAGE_ORDER[stage] > PIPELINE_STAGE_ORDER["raw-srt"],
    )
    profile = resolve_profile(
        switches.media,
        switches.retrieval,
        switches.difficulty,
        switches.continuity,
        output_scale=llm_output_scale,
    )
    return switches, profile


def prepare_url_input(
    url: str,
    *,
    output_path: str | Path | None,
    llm_video: str | Path | None,
    download_video_source: bool = True,
) -> tuple[Path, PipelinePaths, str | Path | None, str]:
    """Resolve a media URL into (audio_path, paths, llm_video, extra_info).

    Public so the batch runner's download stage can reuse it; derives the
    artifact paths from the resolved video id.

    A URL fetches the **video** by default, at every stage. The subtitles this
    pipeline produces are usually going to be burned into that video, so the
    file is wanted whether or not an LLM stage runs and whether or not a media
    switch asks to *show* it to a model -- and re-fetching it later means
    resolving the same URL a second time, which is exactly what an id map
    exists to avoid. ``download_video_source=False`` (``--no-download-video``)
    takes the audio-only path for a run that really only wants a transcript.
    """
    from .media.source import (
        download_audio,
        download_video,
        resolve_video_id,
    )
    from .paths import resolve_reference_data_root

    # The switches are validated by the caller (`resolve_llm_switches`),
    # before this downloads anything.
    map_dir = resolve_reference_data_root()
    video_id = resolve_video_id(url, map_dir)
    paths = default_pipeline_paths(Path(video_id), output_path)
    media_dir = paths.final_srt.parent
    if download_video_source and not llm_video:
        resolved_id, video_path = download_video(
            url, map_dir, video_id=video_id, target_dir=media_dir
        )
        # The video is the source: separation makes its own lossless copy when it
        # needs one, and the LLM clip cutter runs ffmpeg either way. Extracting a
        # narrowed audio track here would only cost a generation.
        if resolved_id != video_id:
            paths = default_pipeline_paths(Path(resolved_id), output_path)
        return (
            video_path.resolve(),
            paths,
            video_path,
            "媒体文件: " + str(video_path) + "\nLLM 视频文件: " + str(video_path),
        )
    resolved_id, audio_path = download_audio(
        url, map_dir, video_id=video_id, target_dir=media_dir
    )
    if resolved_id != video_id:
        paths = default_pipeline_paths(Path(resolved_id), output_path)
    return audio_path.resolve(), paths, llm_video, "媒体文件: " + str(audio_path)


def _stage_output(paths: PipelinePaths, stage: str) -> Path:
    if stage == "vocal":
        return paths.vocal_audio
    if stage == "aligned":
        return paths.aligned_json
    if stage == "stable":
        return paths.stable_json
    if stage == "raw-srt":
        return paths.raw_srt
    if stage == "translated-srt":
        return paths.translated_srt
    return paths.final_srt
