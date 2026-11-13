"""FineSub as a transcription engine (`docs/transcription-engines-plan.md`, WP4a).

One `TranscriptionEngine`: it runs FineSub's own recognition chain --
separation, VAD + ASR, stabilization and, when the request asks for speakers,
the acoustic speaker timeline -- through `finesub.stages.run_transcription`,
and hands back `transcript.json` projected from the stable plus FineSub's own
products as opaque `native` attachments (`finesub.stable_json`,
`finesub.diar_json`, ...). It never touches the correction half: nothing here
imports `nonoka_x.correction`, and correction never imports this.

Written to move into the transcription engine package as it is (plan 4.6,
item 35): apart from the protocol types of `nonoka_x.transcription`, nothing of
`nonoka_x` is imported, and `finesub` only inside `transcribe`, so the spec and
the readiness check work where FineSub is not importable at all.

The job's `options` (owner 2026-11-11) are the request's fields for this half,
already resolved by the caller in TaskRequest terms:

- `title`: names the products (`<task_dir>/workspace/<title>.srt` and its
  siblings); the source's stem when absent;
- `device`: the request's value (`cuda`, `cuda:1`, `cpu`);
- `gpu_tier`: resolved, a legacy `gpu_budget_gb` already migrated;
- `separate`;
- `diarization`: `{level, max_speakers}` as the provider normalized it;
- `extra_info`: with the picked knowledge subjects already written in --
  the ASR referee's context is chosen from it;
- `knowledge`: the effective switch (a correct-only Chinese task's `none`),
  which decides whether that context is read at all;
- `task_id`.

From these the engine builds exactly the keywords the worker passes
`run_pipeline` for the same request -- the ones the transcription stages read
-- so the algorithms receive the same arguments
(`tests/test_engine_finesub.py` proves it against the call baselines).
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import hashlib
import importlib.util
import json
import logging
import os
from pathlib import Path
import shutil
import sys
from typing import Any, Mapping

from nonoka_x.transcription import (
    DIARIZATION_LEVELS,
    EngineEnvironment,
    EngineFeatures,
    EngineSpec,
    Issue,
    Readiness,
    ReadinessContext,
    Reporter,
    TranscriptDocument,
    TranscriptError,
    TranscriptLine,
    TranscriptWord,
    TranscriptionJob,
    TranscriptionResult,
    write_transcript,
)

ENGINE_ID = "finesub"

#: Runtime components and models by the runtime facade's ids (`nonoka_x.runtime`):
#: the Python environment, the media tools, the recognition models; the speaker
#: models only when the request asks for speakers.
COMPONENTS = ("asr-runtime", "media")
MODELS = ("separator", "whisper", "qwen-referee")
SPEAKER_MODELS = ("nemotron3", "speaker-embedding")

SPEC = EngineSpec(
    id=ENGINE_ID,
    label="FineSub",
    kind="local-gpu",
    features=EngineFeatures(
        speakers=True,
        word_timestamps=True,
        separate=True,
        diarization_levels=DIARIZATION_LEVELS,
    ),
    environment=EngineEnvironment(
        components=COMPONENTS,
        models=MODELS,
        optional_models=SPEAKER_MODELS,
        # Runs on the CPU as well (`device=cpu`, the `cpu` tier), only slowly.
        gpu="optional",
    ),
)

#: The engine's own stages, in order; a later target stops at the last one.
ENGINE_STAGES = ("vocal", "aligned", "stable")
#: Every TaskRequest target, in order.
TARGETS = ("vocal", "aligned", "stable", "raw-srt", "translated-srt", "final-srt")

#: What `run_pipeline` defaults `gpu_tier` to (`finesub.speech.runtime.resources.AUTO_GPU_TIER`).
AUTO_GPU_TIER = "auto"

#: `native` keys -> the `PipelinePaths` product each one is.
NATIVE_PRODUCTS = (
    "vocal_audio",
    "vad_json",
    "vad_energy_npz",
    "aligned_json",
    "stable_json",
    "diar_json",
    "diar_rows_json",
    "diar_evidence_npz",
    "metadata_json",
)


class EngineCancelled(RuntimeError):
    """The task was cancelled before the engine started."""


# --- the job -> run_transcription's keywords -----------------------------------------


def engine_stage(target: str) -> str:
    """The stage `run_transcription` runs to for a TaskRequest `target`."""

    if target not in TARGETS:
        raise ValueError(f"unknown target {target!r}; known: {', '.join(TARGETS)}")
    return target if target in ENGINE_STAGES else ENGINE_STAGES[-1]


def engine_device(value: Any) -> tuple[str, str | None]:
    """A TaskRequest `device` as FineSub's device and the `CUDA_VISIBLE_DEVICES` it implies.

    FineSub takes only `cuda` or `cpu`; which card is chosen through the
    environment. The worker's `_normalize_engine_device`, restated.
    """

    raw = str(value or "cuda").strip()
    lowered = raw.lower()
    if lowered.startswith("cuda:"):
        index = lowered.split(":", 1)[1].strip()
        return "cuda", index if index.isdigit() else None
    if lowered == "cpu":
        return "cpu", None
    return ("cuda" if lowered.startswith("cuda") else raw), None


def diarize_keywords(block: Any) -> dict[str, Any]:
    """The speaker keywords for a normalized `diarization` block (the worker's `diarization_kwargs`).

    `off` is passed explicitly, as the worker does: an unset level is the
    engine's own default, which depends on the stage it is asked for.
    """

    level = str(block.get("level") or "off") if isinstance(block, Mapping) else "off"
    keywords: dict[str, Any] = {"diarize": level}
    if level != "off" and block.get("max_speakers") is not None:
        keywords["diarize_max_speakers"] = int(block["max_speakers"])
    return keywords


@dataclass(frozen=True)
class RunPlan:
    """One call of `run_transcription`: its input, keywords, and the card it may see."""

    input_path: str
    keywords: Mapping[str, Any]
    #: Set for the run when the request named a card and nothing set it before.
    cuda_visible_devices: str | None

    @property
    def stage(self) -> str:
        return str(self.keywords["stage"])


def workspace(task_dir: Path) -> Path:
    """Where FineSub's products of a task live (the worker's layout)."""

    return Path(task_dir) / "workspace"


def run_plan(job: TranscriptionJob) -> RunPlan:
    options = job.options
    title = str(options.get("title") or Path(job.source).stem)
    device, visible = engine_device(options.get("device"))
    keywords: dict[str, Any] = {
        "output_path": workspace(job.task_dir) / f"{title}.srt",
        "stage": engine_stage(job.target),
        "language": job.language,
        "device": device,
        "gpu_tier": str(options.get("gpu_tier") or AUTO_GPU_TIER),
        "separate": bool(options.get("separate", True)),
        # Read by the ASR referee's context (`build_asr_context`), and that
        # context is read only when `knowledge` is not `none`.
        "extra_info": str(options.get("extra_info") or ""),
        "knowledge": options.get("knowledge", "update"),
        "task_id": str(options.get("task_id") or Path(job.task_dir).name),
        # The correction's record directory: whether the correction has begun
        # decides whether the speaker rows may be split (`split_choice`).
        "task_artifact_dir": workspace(job.task_dir) / "llm-artifacts",
        "resume": True,
        **diarize_keywords(options.get("diarization")),
    }
    return RunPlan(input_path=str(job.source), keywords=keywords, cuda_visible_devices=visible)


# --- products ------------------------------------------------------------------------


def evidence_path(diar_json: Path) -> Path:
    """The acoustic evidence beside the speaker timeline (`speech.diarization.evidence.evidence_path`)."""

    return diar_json.with_name(diar_json.name.removesuffix("-diar.json") + "-diar-evidence.npz")


def native_products(paths: Any) -> dict[str, Path]:
    """FineSub's products that exist, by `native` key."""

    candidates = {
        "vocal_audio": Path(paths.resolve_vocal_audio()),
        "vad_json": Path(paths.vad_json),
        "vad_energy_npz": Path(paths.vad_energy_npz),
        "aligned_json": Path(paths.aligned_json),
        "stable_json": Path(paths.stable_json),
        "diar_json": Path(paths.diar_json),
        "diar_rows_json": Path(paths.diar_rows_json),
        "diar_evidence_npz": evidence_path(Path(paths.diar_json)),
        "metadata_json": Path(paths.metadata_json),
    }
    return {f"{ENGINE_ID}.{name}": path for name, path in candidates.items() if path.is_file()}


# --- transcript.json -----------------------------------------------------------------


def _language(requested: str, metadata: Mapping[str, Any], segments: list[Any]) -> str:
    if requested and requested.strip().lower() != "auto":
        return requested
    recorded = (metadata.get("asr_align") or {}).get("language") if isinstance(metadata, Mapping) else None
    if isinstance(recorded, str) and recorded.strip() and recorded.strip().lower() != "auto":
        return recorded
    counted = Counter(
        segment["lang"]
        for segment in segments
        if isinstance(segment, Mapping) and isinstance(segment.get("lang"), str) and segment["lang"]
    )
    return counted.most_common(1)[0][0] if counted else "auto"


def _duration(vad_json: Path | None, lines: tuple[TranscriptLine, ...]) -> float:
    if vad_json is not None and vad_json.is_file():
        value = json.loads(vad_json.read_text(encoding="utf-8")).get("audio_duration")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
    return max((line.end for line in lines), default=0.0)


def project_transcript(
    product: Path,
    *,
    stage: str,
    language: str,
    vad_json: Path | None = None,
) -> TranscriptDocument:
    """`transcript.json` from FineSub's `-stable.json` (or `-aligned.json`), which is only read.

    Rules (owner 2026-11-11): one line per segment, in order, `id` its 1-based
    position; `start`, `end`, `text` as they are; `words` with `word` as text,
    and their `start` and `end`; no `speaker` (FineSub's speakers stay in its
    `-diar*.json`); `language` the requested one, for `auto` the recognizer's
    recorded language, then the segments' most common `lang`; `duration` the
    VAD record's `audio_duration`, the last line's end without one; `model` the
    recognizer's; `metadata` the stage and the projected file's sha256. A
    segment the format rejects is an error, not something to repair.
    """

    data = product.read_bytes()
    document = json.loads(data.decode("utf-8"))
    if not isinstance(document, Mapping) or not isinstance(document.get("segments"), list):
        raise TranscriptError(f"{product.name} has no segments list")
    segments = document["segments"]
    metadata = document.get("metadata") if isinstance(document.get("metadata"), Mapping) else {}
    lines = []
    for index, segment in enumerate(segments):
        where = f"{product.name} segments[{index}]"
        if not isinstance(segment, Mapping):
            raise TranscriptError(f"{where} must be an object")
        if "words" in segment and not isinstance(segment["words"], list):
            raise TranscriptError(f"{where}.words must be a list")
        try:
            words = None
            if "words" in segment:
                words = tuple(
                    TranscriptWord(text=word.get("word"), start=word.get("start"), end=word.get("end"))
                    for word in segment["words"]
                )
            lines.append(
                TranscriptLine(
                    id=str(index + 1),
                    start=segment.get("start"),
                    end=segment.get("end"),
                    text=segment.get("text"),
                    words=words,
                )
            )
        except (TranscriptError, AttributeError) as error:
            raise TranscriptError(f"{where}: {error}") from None
    model = (metadata.get("asr_align") or {}).get("model")
    line_tuple = tuple(lines)
    return TranscriptDocument(
        engine=ENGINE_ID,
        model=model if isinstance(model, str) and model else None,
        language=_language(language, metadata, segments),
        duration=_duration(vad_json, line_tuple),
        lines=line_tuple,
        metadata={
            "stage": stage,
            "projected_from": f"{ENGINE_ID}.{stage}_json",
            "sha256": hashlib.sha256(data).hexdigest(),
        },
    )


def transcript_path(task_dir: Path) -> Path:
    return Path(task_dir) / "transcript.json"


# --- reporting -----------------------------------------------------------------------


class FineSubReporter:
    """FineSub's reporter protocol (`finesub.reporting.Reporter`) onto a Nonoka `Reporter`.

    The same reading of FineSub's calls as the worker's reporter: a reused
    stage says so, each warning is said once, and what FineSub logs at debug
    goes to the task log.
    """

    def __init__(self, report: Reporter) -> None:
        self._report = report
        self._warned: set[tuple[str, str]] = set()

    def planned(self, stages: Any) -> None:
        return

    def stage_started(self, stage: str, *, reused: bool = False, detail: str = "") -> None:
        self._report.stage(stage, "已有结果，跳过" if reused else (detail or "正在处理"))

    def progress(
        self, stage: str, *, completed: int, total: int | None = None, unit: str = "", detail: str = ""
    ) -> None:
        self._report.progress(stage, completed=completed, total=total, unit=unit, message=detail)

    def summary(self, stage: str, metrics: Mapping[str, Any]) -> None:
        self._report.log(f"{stage}: " + "，".join(f"{key} {value}" for key, value in metrics.items()))

    def warning(self, code: str, message: str, *, impact: str = "", action: str = "") -> None:
        if (code, message) in self._warned:
            return
        self._warned.add((code, message))
        self._report.warning(code, message)

    def debug(self, message: str, fields: Mapping[str, Any] | None = None) -> None:
        self._report.log(message, dict(fields or {}))

    def completed(self, output: Any, elapsed_sec: float) -> None:
        self._report.log("transcription finished", {"output": str(output), "elapsed_sec": round(elapsed_sec, 3)})

    def failed(self, stage: str, message: str) -> None:
        self._report.log(f"{stage} failed", {"message": message})


# audio-separator logs that ONNXruntime has no CUDA provider; FineSub separates
# with PyTorch, so the warning is irrelevant and reads as if the GPU were unused.
_MISLEADING_ONNX_CUDA_WARNING = "CUDAExecutionProvider not available in ONNXruntime"


class _MuteSeparatorONNXWarning(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return _MISLEADING_ONNX_CUDA_WARNING not in record.getMessage()


_SEPARATOR_FILTER = _MuteSeparatorONNXWarning()


def _mute_separator_onnx_warning() -> None:
    for name in ("separator", "audio_separator", "audio_separator.separator.separator"):
        logger = logging.getLogger(name)
        if _SEPARATOR_FILTER not in logger.filters:
            logger.addFilter(_SEPARATOR_FILTER)


# --- the engine ----------------------------------------------------------------------


#: Modules of the ASR stack the launcher interpreter has only when it is the
#: runtime itself (a source checkout run on a full environment).
ASR_MODULES = ("torch", "faster_whisper", "audio_separator")


def managed_status(runtime: Any) -> Mapping[str, Any] | None:
    """The runtime facade's status payload, None when nothing is managed here."""

    status = runtime.status()
    return status if status else None


def media_issues(runtime: Any, managed: Mapping[str, Any] | None) -> list[Issue]:
    """The interpreter and the media tools: the sidecar's `media` tier (WP5, moved as it was)."""

    managed_runtime_ready = managed is not None and (managed.get("runtime") or {}).get("state") == "ready"
    issues: list[Issue] = []
    if sys.version_info < (3, 12) and not managed_runtime_ready:
        issues.append(Issue("missing_python", "需要 Python 3.12 或更高版本"))
    if not (shutil.which("ffmpeg") or runtime.tool_path("ffmpeg")) or not (
        shutil.which("ffprobe") or runtime.tool_path("ffprobe")
    ):
        issues.append(Issue("missing_ffmpeg", "缺少项目必备依赖 FFmpeg 或 FFprobe"))
    return issues


class FineSubEngine:
    """FineSub's recognition chain behind `TranscriptionEngine`.

    `runtime` is the runtime facade (`nonoka_x.runtime.RuntimeManager`:
    `status`, `tool_path`). Readiness is the sidecar's `raw-srt` tier as it
    was before WP5 moved it here, word for word: the media tools, the
    platform, the runtime and its models. Whether there is a card to run on is
    the caller's to say, from `spec.environment.gpu` (`optional`: a missing
    card is a warning, never an issue).
    """

    spec = SPEC

    def __init__(self, runtime: Any | None = None) -> None:
        self._runtime = runtime

    def readiness(self, context: ReadinessContext) -> Readiness:
        if self._runtime is None:
            return Readiness((Issue("runtime_unavailable", "无法查询本机运行环境，FineSub 不可用"),))
        managed = managed_status(self._runtime)
        issues = media_issues(self._runtime, managed)
        if sys.platform != "win32":
            issues.append(Issue("unsupported_platform", "本地 GPU 流水线当前仅支持 Windows x64/NVIDIA"))
        runtime_state = str(((managed or {}).get("runtime") or {}).get("state") or "")
        missing_modules = [name for name in ASR_MODULES if importlib.util.find_spec(name) is None]
        if runtime_state == "outdated":
            # Installed for an older dependency lock (an engine upgrade changed the
            # pinned packages). The sidecar's own interpreter never carries these
            # modules, so listing them would read as "the runtime is gone".
            issues.append(Issue(
                "runtime_outdated",
                "运行环境需要更新：引擎依赖有变化，请到「运行环境」页点击「更新」（只下载有变化的部分，约几分钟）",
            ))
        elif missing_modules and runtime_state != "ready":
            issues.append(Issue("missing_runtime", "AI 运行时缺失：" + "、".join(missing_modules)))
        if managed is not None:
            missing_models = [
                str(item.get("id")) for item in managed.get("models", []) if item.get("state") != "ready"
            ]
            if missing_models:
                issues.append(Issue("missing_model", "ASR 所需模型尚未安装：" + "、".join(missing_models)))
        return Readiness(tuple(issues))

    def transcribe(self, job: TranscriptionJob, report: Reporter) -> TranscriptionResult:
        if report.cancelled():
            raise EngineCancelled("task cancelled before transcription started")
        plan = run_plan(job)
        if plan.cuda_visible_devices is not None and "CUDA_VISIBLE_DEVICES" not in os.environ:
            os.environ["CUDA_VISIBLE_DEVICES"] = plan.cuda_visible_devices
        Path(plan.keywords["output_path"]).parent.mkdir(parents=True, exist_ok=True)
        _mute_separator_onnx_warning()

        from finesub.reporting import current_reporter, quieted_libraries, reporter_delivers, reporting_to
        from finesub.stages import run_transcription

        # A caller that bound a FineSub reporter of its own keeps it -- the
        # worker does, so that one reporter hears both halves of a task and
        # says each warning once -- as finesub correction does; otherwise
        # FineSub's reports go to `report`.
        bound = current_reporter()
        reporter = bound if reporter_delivers(bound) else FineSubReporter(report)
        with reporting_to(reporter), quieted_libraries("normal"):
            paths = run_transcription(plan.input_path, **plan.keywords)

        native = native_products(paths)
        stage = plan.stage
        target = transcript_path(job.task_dir)
        written = False
        if stage in ("aligned", "stable"):
            document = project_transcript(
                Path(getattr(paths, f"{stage}_json")),
                stage=stage,
                language=job.language,
                vad_json=Path(paths.vad_json),
            )
            write_transcript(document, target)
            written = True
        return TranscriptionResult(
            transcript_json=target,
            native=native,
            metadata={"stage": stage, "transcript": written},
        )


__all__ = [
    "COMPONENTS",
    "ENGINE_ID",
    "ENGINE_STAGES",
    "MODELS",
    "NATIVE_PRODUCTS",
    "SPEAKER_MODELS",
    "SPEC",
    "EngineCancelled",
    "FineSubEngine",
    "FineSubReporter",
    "RunPlan",
    "diarize_keywords",
    "engine_device",
    "engine_stage",
    "native_products",
    "project_transcript",
    "run_plan",
    "transcript_path",
]
