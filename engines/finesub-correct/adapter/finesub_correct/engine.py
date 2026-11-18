"""finesub correction: FineSub's half after the stable, as a correction engine.

Moved from the core (`nonoka_x.correction.finesub`, WP4b) into the correction
package (WP-P1, plan 4.6 items 33, 34). Everything after `transcript.json` --
the raw SRT, the speaker labels and exports, the LLM correction and
translation, the post-processing -- is FineSub's `run_post_stable`. Its input
is FineSub's stable JSON, obtained the way plan 5.2 says:

- the transcription handed one over (`native["finesub.stable_json"]`, FineSub
  transcribed this task, here or in the cloud): that file is used as it is,
  copied byte for byte into this package's directory under the name FineSub
  resumes from, together with everything else FineSub's transcription made
  for it (the speaker timeline and its labels, the vocal track the speaker
  judge hears, the VAD and alignment records, the run's metadata). The
  speakers are the acoustic timeline's and the judge hears the vocal track --
  what `run_pipeline` did (`speaker_source="acoustic"`, `judge_audio="vocal"`);
- otherwise the lines of `transcript.json` are turned into a stable JSON
  (`stable_from_transcript`) and the speakers are each line's own label
  (`speaker_source="line-labels"`); there is no vocal track, so the judge hears
  the source mix (`judge_audio="source"`).

`run_source_text` is the other way in: a source-text subtitle axis, whose rows
go straight to the correction task.

Jobs are the core's `CorrectionJob` as JSON. Their `options` carry this
package's directory in the task (`native_dir`) besides the correction block.
"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

ENGINE_ID = "finesub"

#: The native artifacts this engine reads, under the keys FineSub's
#: transcription hands them over with (plan 5.1). Only the stable is required.
NATIVE_STABLE = "finesub.stable_json"
NATIVE_DIAR = "finesub.diar_json"
NATIVE_DIAR_EVIDENCE = "finesub.diar_evidence_npz"
NATIVE_DIAR_ROWS = "finesub.diar_rows_json"
#: The rest of what FineSub's transcription leaves beside its stable, by the
#: `PipelinePaths` field it is: the correction finds them where it looks, as
#: when both halves shared one directory.
NATIVE_COMPANIONS = (
    ("finesub.vocal_audio", "vocal_audio"),
    ("finesub.vad_json", "vad_json"),
    ("finesub.vad_energy_npz", "vad_energy_npz"),
    ("finesub.aligned_json", "aligned_json"),
    ("finesub.metadata_json", "metadata_json"),
)

#: What `metadata["input"]` of a result says the run continued from.
INPUT_NATIVE = "native-stable"
INPUT_TRANSCRIPT = "transcript"

#: The record a converted stable names the transcription that labelled its
#: lines under: FineSub reads the backend and model of `-diar-rows.json` from
#: it (`stages._LINE_LABEL_RECORD`, the key the joint-ASR stage wrote).
LINE_LABEL_RECORD = "joint_asr"

#: The targets that need a configured LLM.
LLM_TARGETS = ("translated-srt", "final-srt")


# --- where FineSub keeps a task's files -----------------------------------------


def native_dir(job: Mapping[str, Any]) -> Path:
    """This package's directory in the task: FineSub's files and the correction's records."""

    options = job.get("options") or {}
    return Path(options.get("native_dir") or Path(job["task_dir"]) / "native" / "finesub-correct")


def output_path(job: Mapping[str, Any]) -> Path:
    """The final subtitle file; every other file is named after it."""

    title = job.get("title") or Path(job["source"]).stem
    return native_dir(job) / f"{title}.srt"


def artifact_dir(job: Mapping[str, Any]) -> Path:
    """The correction task's own records (windows, checkpoints, mode)."""

    return native_dir(job) / "llm-artifacts"


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
    for key, field in NATIVE_COMPANIONS:
        if native.get(key):
            origin = Path(native[key])
            target = Path(getattr(paths, field))
            if field == "vocal_audio":
                # The vocal track keeps its own container (`.ogg`, or the lossless `.flac`).
                target = target.with_suffix(origin.suffix)
            _copy_once(origin, target)
    return paths


def _copy_once(origin: Path, target: Path) -> None:
    if target.exists() or _same_file(origin, target):
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(f".{target.name}.part")
    shutil.copyfile(origin, partial)
    os.replace(partial, target)


def _same_file(a: Path, b: Path) -> bool:
    try:
        return a.resolve() == b.resolve()
    except OSError:
        return False


# --- transcript.json -> FineSub's stable JSON -------------------------------------


def read_transcript(path: str | Path) -> dict[str, Any]:
    """A `transcript.json` the core already checked, with its numbers as the core reads them (floats)."""

    document = json.loads(Path(path).read_text(encoding="utf-8"))
    for line in document.get("lines") or ():
        line["start"], line["end"] = float(line["start"]), float(line["end"])
        for word in line.get("words") or ():
            word["start"], word["end"] = float(word["start"]), float(word["end"])
    document["duration"] = float(document["duration"])
    return document


def stable_from_transcript(document: Mapping[str, Any]) -> dict[str, Any]:
    """FineSub's stable JSON for the lines of a `transcript.json`.

    The shape FineSub's own cloud transcriptions wrote (the joint-ASR stage):

    - one segment per line, in FineSub's order (`line_groups.in_order`: by start,
      same start by speaker), with `start`, `end`, `text`; `words` as FineSub
      spells them (`word`, `start`, `end`) when the line has any; the line's
      `speaker` as `line_speaker`, the label `run_post_stable` reads with
      `speaker_source="line-labels"`; and `lang`, the document's language;
    - `metadata.asr_align` says what language the source is and how long;
    - `metadata.joint_asr` names the engine and model that labelled the lines,
      which `-diar-rows.json` then records as its backend.

    Line ids are not carried: FineSub numbers a stable's lines itself.
    """

    from finesub.subtitles.line_groups import in_order

    language = document["language"]
    segments = []
    for line in document.get("lines") or ():
        segment: dict[str, Any] = {"start": line["start"], "end": line["end"], "text": line["text"]}
        if line.get("words"):
            segment["words"] = [{"word": word["text"], "start": word["start"], "end": word["end"]} for word in line["words"]]
        if line.get("speaker") is not None:
            segment["line_speaker"] = line["speaker"]
        segment["lang"] = language
        segments.append(segment)
    engine = document["engine"]
    model = document.get("model") or engine
    return {
        "segments": in_order(segments),
        "metadata": {
            "asr_align": {
                "backend": engine,
                "language": language,
                "audio_duration": document["duration"],
            },
            LINE_LABEL_RECORD: {"backend": engine, "model": model},
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


def speakers_for(native: Mapping[str, Any]) -> tuple[str, str]:
    """`run_post_stable`'s `(speaker_source, judge_audio)` for this input."""

    if native.get(NATIVE_STABLE):
        return "acoustic", "vocal"
    return "line-labels", "source"


# --- the request's correction settings ------------------------------------------------


def correction_proofread(correction: Mapping[str, Any], cloud_provider: str) -> str:
    """The engine's `proofread` setting for this task's correction (moved from `nonoka_x.settings`).

    `auto` is upstream's: proofread at the `quality` difficulty. A correction
    bound to Nonoka Cloud stays at one pass, because every model that provider
    offers is free to the user and a second pass over each window is load on
    the shared relay rather than a cost the user chose.
    """

    from finesub.llm.routing.model_routes import default_model_routes

    routes = default_model_routes()
    difficulty = str(correction.get("difficulty") or "quality")
    cloud_targets = f"nonoka-{cloud_provider}-"
    for group_id in ("correction-mm", "correction-text"):
        group, _cell = routes.resolve_binding(routes.active_preset_id, group_id, difficulty)
        if any(target_id.startswith(cloud_targets) for target_id in group.target_ids):
            return "off"
    return "auto"


def for_task_mode(request: Mapping[str, Any], record_dir: Path) -> dict[str, Any]:
    """The request without what a Chinese-source task cannot use (moved from `nonoka_x.worker`).

    Since 0.7.0 such a task only corrects: it neither reads nor writes the
    knowledge base and ignores `extra_style`. The engine turns those off itself
    but warns about each one, and it validates pinned subjects before it gets
    that far, so a subject it would never have read could still fail the run.

    Follows the engine's own decision (`three_pass.task_correct_only`): a task
    whose record or windows say it translated -- one begun before 0.7.0, say --
    goes on translating, with the knowledge settings it started with. A source
    only detected as Chinese (`language=auto`) cannot be known here and is left
    to the engine. A Japanese axis has its source text and is always translated.
    """

    axis = request.get("axis")
    if request.get("language") != "zh" or (isinstance(axis, Mapping) and axis.get("kind") == "ja"):
        return dict(request)
    from finesub.llm.correction_mode import recorded_choice, started

    recorded = recorded_choice(record_dir)
    if not (recorded.correct_only if recorded is not None else not started(record_dir)):
        return dict(request)
    return {
        **request,
        "knowledge": "none",
        "correction": {**(request.get("correction") or {}), "extra_style": ""},
    }


# --- the engine --------------------------------------------------------------------


class FineSubCorrection:
    """FineSub's correction and translation for one task's jobs, under one FineSub reporter."""

    def __init__(self, reporter: Any, *, log: Callable[[str], None], cloud_provider: str = "") -> None:
        self._reporter = reporter
        self._log = log
        self._cloud_provider = cloud_provider

    def _options(self, job: Mapping[str, Any]) -> dict[str, Any]:
        options = dict(job.get("options") or {})
        if not options.get("proofread"):
            options["proofread"] = correction_proofread(options, self._cloud_provider)
        return options

    def check(self, job: Mapping[str, Any]) -> None:
        """Fail now on what this job would fail on only once the transcription is done.

        `run_pipeline` checks the whole request before separation -- the LLM
        configuration, the style and knowledge subjects it names, a video
        switch with no video, a speaker judge the correction would refuse --
        because finding it after ASR is finding it at the most expensive
        moment. This makes them up front: FineSub's own preparation of the run
        (`_prepare_run`, as `run_pipeline` calls it: acoustic speakers, the
        judge on the vocal track), which resolves and checks everything and
        runs no stage. Only an LLM target has anything to check.
        """

        if job["target"] == "raw-srt":
            return
        from finesub.reporting import quieted_libraries, reporting_to
        from finesub.stages import _pipeline_options, _prepare_run

        with reporting_to(self._reporter), quieted_libraries("normal"):
            output = output_path(job)
            output.parent.mkdir(parents=True, exist_ok=True)
            _prepare_run(
                str(job["source"]),
                _pipeline_options(stage=job["target"], **_post_stable_keywords(job, self._options(job), output)),
                scope="pipeline",
                speaker_source="acoustic",
                judge_audio="vocal",
            )

    def run(self, job: Mapping[str, Any]) -> dict[str, Any]:
        """`run_post_stable` for this job; the core's `CorrectionResult` as JSON."""

        from finesub.reporting import quieted_libraries, reporting_to
        from finesub.stages import default_pipeline_paths, run_post_stable

        with reporting_to(self._reporter), quieted_libraries("normal"):
            options = self._options(job)
            transcription = job["transcription"]
            native = dict(transcription.get("native") or {})
            output = output_path(job)
            output.parent.mkdir(parents=True, exist_ok=True)
            speaker_source, judge_audio = speakers_for(native)
            if speaker_source == "acoustic":
                paths = place_native(job["source"], output, native)
                self._log("纠错输入：FineSub 转写的原生 stable，原样使用")
                # A transcription that labelled the lines itself (FineSub's, here)
                # did this already in this run; asking again would only announce
                # the stage a second time as reused.
                level = str(job.get("diarization_level") or "off")
                if level != "off" and Path(paths.diar_json).exists() and not native.get(NATIVE_DIAR_ROWS):
                    _label_rows_from_timeline(paths, level)
            else:
                paths = default_pipeline_paths(job["source"], output)
                write_stable(
                    stable_from_transcript(read_transcript(transcription["transcript_json"])), Path(paths.stable_json)
                )
                self._log("纠错输入：由 transcript.json 转成的 stable，说话人来自各行标签")

            paths = run_post_stable(
                str(job["source"]),
                speaker_source=speaker_source,
                judge_audio=judge_audio,
                stage=job["target"],
                **_post_stable_keywords(job, options, output),
            )
        artifacts = {name: path for name, path in _artifacts(paths, job).items() if path.is_file()}
        metadata = {
            "input": INPUT_NATIVE if speaker_source == "acoustic" else INPUT_TRANSCRIPT,
            "speaker_source": speaker_source,
            "judge_audio": judge_audio,
        }
        from .document import ProjectionError

        try:
            artifacts["correction_json"] = correction_result(job, artifacts)
        except ProjectionError as error:
            # Products that cannot be lined up fail the task's projection, as
            # they did when the core lined them up; the run itself is done.
            metadata["correction_error"] = f"ProjectionError: {error}"
        return {
            "artifacts": {name: str(path) for name, path in artifacts.items()},
            "metadata": metadata,
        }


def _post_stable_keywords(job: Mapping[str, Any], options: Mapping[str, Any], output: Path) -> dict[str, Any]:
    """The keywords this job gives FineSub's stages after the stable (besides the speakers and the stage)."""

    knowledge = job.get("knowledge") or {}
    subjects = list(knowledge.get("knowledge_subjects") or ())
    return {
        "output_path": output,
        # Unset switches stay None: the engine resolves them through the
        # active preset's defaults, then its own.
        "llm_media": options.get("media"),
        "llm_retrieval": options.get("retrieval"),
        "llm_difficulty": options.get("difficulty"),
        "llm_proofread": str(options.get("proofread") or "auto"),
        "llm_fast": str(options.get("fast") or "auto"),
        "extra_info": options.get("extra_info", ""),
        "extra_style": options.get("extra_style", ""),
        "knowledge": knowledge.get("knowledge", "update"),
        "task_summary": job.get("task_summary", ""),
        "task_id": job.get("task_id", ""),
        "task_artifact_dir": artifact_dir(job),
        "resume": True,
        # The editor builds its own speaker tracks from the speaker labels,
        # so the engine's per-speaker exports stay off.
        "speaker_tracks": "off",
        "diarize": str(job.get("diarization_level") or "off"),
        # Passed only when there is something to pin, so a run without
        # subjects calls the engine with exactly upstream's arguments.
        **({"knowledge_subjects": subjects} if subjects else {}),
    }


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


def _artifacts(paths: Any, job: Mapping[str, Any]) -> dict[str, Path]:
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
        "correction_mode_json": artifact_dir(job) / "correction-mode.json",
    }


# --- what the projection reads ------------------------------------------------------


def correction_result(job: Mapping[str, Any], artifacts: Mapping[str, Path]) -> Path:
    """`correction.json` for this run's products, in the task directory beside `transcript.json` (WP-L)."""

    from .document import FILENAME, write_correction

    return write_correction(
        Path(job["task_dir"]) / FILENAME,
        artifacts["stable_json"],
        annotated_csv=artifacts.get("annotated_csv"),
        final_srt=artifacts.get("final_srt"),
        correction_mode=artifacts.get("correction_mode_json"),
    )


def product_rows(stable_json: str | Path) -> list[dict[str, Any]]:
    """A task's stable lines with their speakers, as FineSub reads its own products (`document.product_rows`)."""

    from .document import product_rows as rows

    return rows(stable_json)


# --- a source-text axis -----------------------------------------------------------


class NoSourceText(ValueError):
    """None of the rows has source text, so there is nothing to translate."""


def stable_from_source_rows(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """FineSub's stable JSON for rows of written source text (`t0`, `t1`, `ja`).

    Rows with no source text are dropped rather than carried as empty segments.
    Each segment keeps its row's position (1-based) as its id.
    """

    segments = []
    for index, row in enumerate(rows, start=1):
        text = str(row.get("ja") or "").strip()
        if not text:
            continue
        segments.append({"id": str(index), "start": float(row["t0"]), "end": float(row["t1"]), "text": text})
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

    The rows become a stable JSON, which is the correction task's own input,
    and `run_correction_task` runs on it, the same task `run_post_stable` runs
    for a recognized source. `options` is the request's correction block with
    `fast` and `proofread` resolved. With `source_path` an audio or video
    reference stays available; without it the reference is text either way.
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
