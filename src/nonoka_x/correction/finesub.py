"""finesub correction: FineSub's half after the stable, as a correction engine.

`docs/transcription-engines-plan.md` WP4b. Everything after `transcript.json`
-- the raw SRT, the speaker labels and exports, the LLM correction and
translation, the post-processing -- is FineSub's `run_post_stable`, called
here and nowhere else in Nonoka. Its input is FineSub's stable JSON, obtained
the way plan 5.2 says:

- the transcription handed one over (`native["finesub.stable_json"]`, FineSub
  transcribed this task, here or in the cloud): that file is used as it is,
  copied byte for byte under the name FineSub resumes from, together with the
  speaker timeline and whatever FineSub derived from it. The speakers are the
  acoustic timeline's and the speaker judge hears the vocal track -- exactly
  what `run_pipeline` does (`speaker_source="acoustic"`, `judge_audio="vocal"`);
- otherwise the lines of `transcript.json` are turned into a stable JSON
  (`stable_from_transcript`) and the speakers are each line's own label
  (`speaker_source="line-labels"`); there is no vocal track, so the judge hears
  the source mix (`judge_audio="source"`).

`run_source_text` is the other way in: a source-text subtitle axis
(`nonoka_x.axis`), whose rows go straight to the correction task.

Written to be moved into the correction engine package whole (plan 4.6, items
33-35): besides the standard library it imports the correction and
transcription contracts only, and FineSub lazily, inside the functions that run
it, so importing this module needs neither FineSub nor numpy. It never imports
a transcription engine.
"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from nonoka_x.correction import CorrectionJob, CorrectionResult, CorrectionSpec
from nonoka_x.transcription import (
    EngineEnvironment,
    Issue,
    Readiness,
    ReadinessContext,
    Reporter,
    TranscriptDocument,
)

ENGINE_ID = "finesub"

#: The native artifacts this engine reads, under the keys FineSub's
#: transcription hands them over with (plan 5.1). Only the stable is required;
#: the speaker timeline, its evidence sidecar and the per-line labels made from
#: them come along when the transcription made them.
NATIVE_STABLE = "finesub.stable_json"
NATIVE_DIAR = "finesub.diar_json"
NATIVE_DIAR_EVIDENCE = "finesub.diar_evidence_npz"
NATIVE_DIAR_ROWS = "finesub.diar_rows_json"

#: What `metadata["input"]` of a result says the run continued from.
INPUT_NATIVE = "native-stable"
INPUT_TRANSCRIPT = "transcript"

#: The record a converted stable names the transcription that labelled its
#: lines under: FineSub reads the backend and model of `-diar-rows.json` from
#: it (`stages._LINE_LABEL_RECORD`, the key the joint-ASR stage wrote).
LINE_LABEL_RECORD = "joint_asr"

SPEC = CorrectionSpec(
    id=ENGINE_ID,
    label="finesub correction",
    # The launcher environment is enough: no runtime layer, no model, no GPU.
    environment=EngineEnvironment(gpu="none"),
)


# --- where FineSub keeps a task's files -----------------------------------------


def workspace(task_dir: str | Path) -> Path:
    """The directory a task's subtitles and FineSub's own files live in."""

    return Path(task_dir) / "workspace"


def output_path(task_dir: str | Path, title: str) -> Path:
    """The final subtitle file; every other file is named after it."""

    return workspace(task_dir) / f"{title}.srt"


def artifact_dir(task_dir: str | Path) -> Path:
    """The correction task's own records (windows, checkpoints, mode)."""

    return workspace(task_dir) / "llm-artifacts"


def place_native(source: str | Path, output: str | Path, native: Mapping[str, str | Path]) -> Any:
    """Put a transcription's FineSub files where `run_post_stable` looks for them.

    Each is copied byte for byte, and only when absent: a retry resumes on the
    bytes its first attempt read, and FineSub never rewrites a stable. Returns
    FineSub's `PipelinePaths` for this output.
    """

    from finesub.stages import default_pipeline_paths

    paths = default_pipeline_paths(source, output)
    _copy_once(Path(native[NATIVE_STABLE]), Path(paths.stable_json))
    if native.get(NATIVE_DIAR):
        _copy_once(Path(native[NATIVE_DIAR]), Path(paths.diar_json))
        if native.get(NATIVE_DIAR_EVIDENCE):
            from finesub.speech.diarization.evidence import evidence_path

            _copy_once(Path(native[NATIVE_DIAR_EVIDENCE]), evidence_path(paths.diar_json))
    if native.get(NATIVE_DIAR_ROWS):
        _copy_once(Path(native[NATIVE_DIAR_ROWS]), Path(paths.diar_rows_json))
    return paths


def _copy_once(origin: Path, target: Path) -> None:
    if target.exists() or _same_file(origin, target):
        return
    partial = target.with_name(f".{target.name}.part")
    shutil.copyfile(origin, partial)
    os.replace(partial, target)


def _same_file(a: Path, b: Path) -> bool:
    try:
        return a.resolve() == b.resolve()
    except OSError:
        return False


# --- transcript.json -> FineSub's stable JSON -------------------------------------


def stable_from_transcript(document: TranscriptDocument) -> dict[str, Any]:
    """FineSub's stable JSON for the lines of a `transcript.json`.

    The shape FineSub's own cloud transcriptions wrote (the joint-ASR stage):

    - one segment per line, in FineSub's order (`line_groups.in_order`: by start,
      same start by speaker), with `start`, `end`, `text`; `words` as FineSub
      spells them (`word`, `start`, `end`) when the line has any; the line's
      `speaker` as `line_speaker`, the label `run_post_stable` reads with
      `speaker_source="line-labels"`; and `lang`, the document's language;
    - `metadata.asr_align` says what language the source is (the correction
      reads it to decide whether it translates or only corrects) and how long;
    - `metadata.joint_asr` names the engine and model that labelled the lines,
      which `-diar-rows.json` then records as its backend.

    Line ids are not carried: FineSub numbers a stable's lines itself, as it
    does for its own. Fields FineSub has no use for (unknown line and word
    fields, the document's metadata) are left out.
    """

    from finesub.subtitles.line_groups import in_order

    segments = []
    for line in document.lines:
        segment: dict[str, Any] = {"start": line.start, "end": line.end, "text": line.text}
        if line.words:
            segment["words"] = [{"word": word.text, "start": word.start, "end": word.end} for word in line.words]
        if line.speaker is not None:
            segment["line_speaker"] = line.speaker
        segment["lang"] = document.language
        segments.append(segment)
    model = document.model or document.engine
    return {
        "segments": in_order(segments),
        "metadata": {
            "asr_align": {
                "backend": document.engine,
                "language": document.language,
                "audio_duration": document.duration,
            },
            LINE_LABEL_RECORD: {"backend": document.engine, "model": model},
        },
    }


def write_stable(payload: Mapping[str, Any], path: Path) -> Path:
    """Write a stable JSON atomically, only when absent: FineSub never rewrites one."""

    if path.exists():
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(f".{path.name}.part")
    partial.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(partial, path)
    return path


# --- the engine --------------------------------------------------------------------


def speakers_for(native: Mapping[str, Any]) -> tuple[str, str]:
    """`run_post_stable`'s `(speaker_source, judge_audio)` for this input."""

    if native.get(NATIVE_STABLE):
        return "acoustic", "vocal"
    return "line-labels", "source"


class _ReporterAdapter:
    """FineSub's reporter interface, delivered to a Nonoka `Reporter`."""

    def __init__(self, report: Reporter) -> None:
        self._report = report

    def planned(self, stages: Sequence[str]) -> None:
        return

    def stage_started(self, stage: str, *, reused: bool = False, detail: str = "") -> None:
        self._report.stage(stage, "已有结果，跳过" if reused else (detail or "正在处理"))

    def progress(self, stage: str, *, completed: int, total: int | None = None, unit: str = "",
                 detail: str = "") -> None:
        self._report.progress(stage, completed=completed, total=total, unit=unit, message=detail)

    def summary(self, stage: str, metrics: Mapping[str, object]) -> None:
        self._report.log(f"{stage}: " + "，".join(f"{key} {value}" for key, value in metrics.items()))

    def warning(self, code: str, message: str, *, impact: str = "", action: str = "") -> None:
        self._report.warning(code, message)

    def debug(self, message: str, fields: Mapping[str, object] | None = None) -> None:
        self._report.log(message, dict(fields or {}))

    def completed(self, output: str | Path, elapsed_sec: float) -> None:
        return

    def failed(self, stage: str, message: str) -> None:
        return


class FineSubCorrection:
    """The `CorrectionEngine` that runs FineSub's correction and translation."""

    spec = SPEC

    def readiness(self, context: ReadinessContext, request: Mapping[str, Any]) -> Readiness:
        import importlib.util

        if importlib.util.find_spec("finesub") is None:
            return Readiness((Issue("engine_missing", "FineSub 纠错引擎不可用"),))
        return Readiness()

    def run(self, job: CorrectionJob, report: Reporter) -> CorrectionResult:
        """`run_post_stable` for this job, under whatever reporter the caller bound.

        A caller that bound a FineSub reporter of its own keeps it; otherwise
        FineSub's reports go to `report`.
        """

        from finesub.reporting import current_reporter, reporter_delivers, reporting_to

        bound = current_reporter()
        with reporting_to(bound if reporter_delivers(bound) else _ReporterAdapter(report)):
            return self._run(job, report)

    def _run(self, job: CorrectionJob, report: Reporter) -> CorrectionResult:
        from finesub.stages import default_pipeline_paths, run_post_stable

        native = dict(job.transcription.native)
        title = job.title or job.source.stem
        output = output_path(job.task_dir, title)
        output.parent.mkdir(parents=True, exist_ok=True)
        speaker_source, judge_audio = speakers_for(native)
        if speaker_source == "acoustic":
            paths = place_native(job.source, output, native)
            report.log("纠错输入：FineSub 转写的原生 stable，原样使用")
            if job.diarization_level != "off" and Path(paths.diar_json).exists():
                _label_rows_from_timeline(paths, job.diarization_level)
        else:
            paths = default_pipeline_paths(job.source, output)
            write_stable(stable_from_transcript(job.transcription.read_transcript()), Path(paths.stable_json))
            report.log("纠错输入：由 transcript.json 转成的 stable，说话人来自各行标签")

        options = job.options
        subjects = list(job.knowledge.get("knowledge_subjects") or ())
        paths = run_post_stable(
            str(job.source),
            speaker_source=speaker_source,
            judge_audio=judge_audio,
            stage=job.target,
            output_path=output,
            # Unset switches stay None: the engine resolves them through the
            # active preset's defaults, then its own.
            llm_media=options.get("media"),
            llm_retrieval=options.get("retrieval"),
            llm_difficulty=options.get("difficulty"),
            llm_proofread=str(options.get("proofread") or "auto"),
            llm_fast=str(options.get("fast") or "auto"),
            extra_info=options.get("extra_info", ""),
            extra_style=options.get("extra_style", ""),
            knowledge=job.knowledge.get("knowledge", "update"),
            task_summary=job.task_summary,
            task_id=job.task_id,
            task_artifact_dir=artifact_dir(job.task_dir),
            resume=True,
            # The editor builds its own speaker tracks from the speaker labels,
            # so the engine's per-speaker exports stay off.
            speaker_tracks="off",
            diarize=job.diarization_level,
            # Passed only when there is something to pin, so a run without
            # subjects calls the engine with exactly upstream's arguments.
            **({"knowledge_subjects": subjects} if subjects else {}),
        )
        return CorrectionResult(
            artifacts={name: path for name, path in _artifacts(paths, job.task_dir).items() if path.is_file()},
            metadata={
                "input": INPUT_NATIVE if speaker_source == "acoustic" else INPUT_TRANSCRIPT,
                "speaker_source": speaker_source,
                "judge_audio": judge_audio,
            },
        )


def _label_rows_from_timeline(paths: Any, diarize: str) -> None:
    """The speaker labels of each line, from a speaker timeline the transcription made.

    `run_pipeline` writes them in its transcription half, after the stable
    (`_run_diarization_stage`); `run_post_stable` with `speaker_source="acoustic"`
    only reads them. A transcription that handed over the timeline without the
    labels -- the cloud, which keeps only the stable and the timeline -- would
    otherwise reach the correction with no speakers at all. This is FineSub's
    own stage, entered only with the timeline on disk: then it labels from the
    cached timeline (or finds the labels current and reuses them), and never
    runs a diarization model or touches the GPU.
    """

    from finesub.reporting import current_reporter
    from finesub.stages import _run_diarization_stage

    reporter = current_reporter()

    def announce(stage_key: str, status: str, *, detail: str = "") -> None:
        reporter.stage_started(stage_key, reused=status == "reused", detail=detail)

    # Same arguments as `run_pipeline` gives it; device and tier are only read
    # to run a diarization model, which a cached timeline never does.
    _run_diarization_stage(paths, diarize=diarize, device="cuda", gpu_tier="auto", announce=announce,
                           backend=None, max_speakers=None, diarize_split=True)


def _artifacts(paths: Any, task_dir: Path) -> dict[str, Path]:
    """Every artifact the run may have left, under today's manifest names."""

    from finesub.speech.diarization.evidence import evidence_path

    final_srt = Path(paths.final_srt)
    return {
        "stable_json": Path(paths.stable_json),
        "diar_json": Path(paths.diar_json),
        "diar_rows_json": Path(paths.diar_rows_json),
        "diar_evidence_npz": evidence_path(paths.diar_json),
        "speaker_judge_json": Path(paths.speaker_judge_json),
        "raw_srt": Path(paths.raw_srt),
        "translated_srt": Path(paths.translated_srt),
        "annotated_csv": final_srt.with_name(f"{final_srt.stem}-annotated.csv"),
        "final_srt": final_srt,
        "metadata_json": Path(paths.metadata_json),
        "correction_mode_json": artifact_dir(task_dir) / "correction-mode.json",
    }


# --- a source-text axis -----------------------------------------------------------


class NoSourceText(ValueError):
    """None of the rows has source text, so there is nothing to translate."""


def stable_from_source_rows(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """FineSub's stable JSON for rows of written source text (`t0`, `t1`, `ja`).

    Rows with no source text are dropped rather than carried as empty segments:
    the correction windows are built from this list, and a blank row is one the
    model would be asked to correct into something. Each segment keeps its
    row's position (1-based) as its id.
    """

    segments = []
    for index, row in enumerate(rows, start=1):
        text = str(row.get("ja") or "").strip()
        if not text:
            continue
        segments.append(
            {"id": str(index), "start": float(row["t0"]), "end": float(row["t1"]), "text": text}
        )
    if not segments:
        raise NoSourceText("this axis has no source text to translate")
    return {"segments": segments}


@dataclass(frozen=True)
class SourceTextRun:
    """Where a source-text run left its artifacts."""

    stable_json: Path
    final_srt: Path
    annotated_csv: Path


def run_source_text(
    rows: Sequence[Mapping[str, Any]],
    *,
    output_path: str | Path,
    task_id: str,
    task_artifact_dir: str | Path,
    task_summary: str = "",
    options: Mapping[str, Any],
    knowledge: str = "none",
    source_path: str | Path | None = None,
    knowledge_root: str | Path | None = None,
    knowledge_subjects: Sequence[str] | None = None,
    on_notice: Callable[[str], None] | None = None,
) -> SourceTextRun:
    """Only the LLM stage, over subtitles a person already wrote.

    Written source text has everything separation, VAD and ASR would produce,
    so FineSub's whole first half is skipped -- no GPU, no models, no audio
    decode -- and so is the raw SRT: the rows become a stable JSON, which is the
    correction task's own input, and `run_correction_task` runs on it, the same
    task `run_post_stable` runs for a recognized source.

    `options` is the request's correction block as for `CorrectionJob.options`:
    `fast` and `proofread` already resolved. With `source_path` (the desktop,
    which has the media) an audio or video reference stays available; without
    it (no media at hand) the reference is text either way.
    """

    from finesub.llm.correction_request import CorrectionRequest
    from finesub.llm.correction_translation import run_correction_task
    from finesub.llm.routing.profiles import resolve_profile

    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    stable_json = output.with_name(f"{output.stem}.stable.json")
    stable_json.write_text(
        json.dumps(stable_from_source_rows(rows), ensure_ascii=False, indent=2), encoding="utf-8"
    )

    media = str(options.get("media") or "text")
    video_path: str | Path | None = None
    if source_path is not None and media != "text":
        from finesub.stages import resolve_llm_media_for_source

        media, video_path, notice = resolve_llm_media_for_source(
            Path(source_path), stage="final-srt", llm_media=media, llm_video=None
        )
        if notice and on_notice is not None:
            on_notice(notice)
    elif source_path is None:
        media = "text"

    profile = resolve_profile(
        media,
        str(options.get("retrieval") or "none"),
        str(options.get("difficulty") or "quality"),
        "serial",
    )
    extra: dict[str, Any] = {} if knowledge_root is None else {"knowledge_root": knowledge_root}
    if knowledge_subjects:
        extra["knowledge_subjects"] = tuple(knowledge_subjects)
    outcome = run_correction_task(CorrectionRequest(
        stable_json=stable_json,
        output_path=output,
        # A text reference reads nothing but the stable JSON, so the media file
        # is not opened at all -- which is what lets a caller without one run this.
        audio_path=Path(source_path) if source_path is not None and media != "text" else None,
        video_path=Path(video_path) if video_path else None,
        profile=profile,
        fast=str(options.get("fast") or "auto"),
        extra_info=str(options.get("extra_info") or ""),
        extra_style=str(options.get("extra_style") or ""),
        knowledge=knowledge,
        proofread=str(options.get("proofread") or "auto"),
        task_id=task_id,
        task_summary=task_summary,
        task_artifact_dir=Path(task_artifact_dir),
        resume=True,
        **extra,
    ))
    if outcome.result is None:
        raise RuntimeError("the correction task produced no subtitle file")
    final_srt = Path(outcome.result)
    return SourceTextRun(
        stable_json=stable_json,
        final_srt=final_srt,
        annotated_csv=final_srt.with_name(f"{final_srt.stem}-annotated.csv"),
    )


__all__ = [
    "ENGINE_ID",
    "INPUT_NATIVE",
    "INPUT_TRANSCRIPT",
    "NATIVE_DIAR",
    "NATIVE_DIAR_EVIDENCE",
    "NATIVE_DIAR_ROWS",
    "NATIVE_STABLE",
    "SPEC",
    "FineSubCorrection",
    "NoSourceText",
    "SourceTextRun",
    "artifact_dir",
    "output_path",
    "place_native",
    "run_source_text",
    "speakers_for",
    "stable_from_source_rows",
    "stable_from_transcript",
    "workspace",
    "write_stable",
]
