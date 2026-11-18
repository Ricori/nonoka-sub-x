"""FineSub as a transcription engine: one job in, `transcript.json` and FineSub's own products out.

Moved from the core (`nonoka_x.transcription.engines.finesub`, WP4a) into the
transcription package (WP-P1, plan 4.6 item 33). It runs FineSub's own
recognition chain -- separation, VAD + ASR, stabilization and, when the request
asks for speakers, the acoustic speaker timeline -- through
`finesub.stages.run_transcription`, and hands back `transcript.json` projected
from the stable plus FineSub's products as opaque `native` attachments
(`finesub.stable_json`, `finesub.diar_json`, ...).

The job is the core's `TranscriptionJob` as JSON (`to_dict`). Its `options`
are the request's fields for this half, already resolved by the core in
TaskRequest terms (WP4a, owner 2026-11-11):

- `title`: names the products (`<native>/<title>.srt` and its siblings);
- `native_dir`: this package's directory in the task, where FineSub's files
  live (plan 4.6 item 34);
- `record_dir`: the correction's record directory (`llm-artifacts`): whether
  the correction has begun decides whether the speaker rows may be split;
- `device`, `gpu_tier` (resolved), `separate`, `diarization` (as the provider
  normalized it), `extra_info` (the picked knowledge subjects written in, the
  ASR referee's context is chosen from it), `knowledge` (the effective
  switch), `task_id`.

From these the engine builds exactly the keywords `run_pipeline` was given for
the same request, so the algorithms receive the same arguments
(`tests/test_engine_finesub.py` proves it against the call baselines).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

ENGINE_ID = "finesub"
#: `transcript.json`'s schema (the core's `nonoka_x.transcription.model.SCHEMA`).
TRANSCRIPT_SCHEMA = 1
TRANSCRIPT_FILENAME = "transcript.json"

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


class TranscriptError(ValueError):
    """A product the transcript format rejects."""


# --- the job -> run_transcription's keywords -----------------------------------------


def engine_stage(target: str) -> str:
    """The stage `run_transcription` runs to for a TaskRequest `target`."""

    if target not in TARGETS:
        raise ValueError(f"unknown target {target!r}; known: {', '.join(TARGETS)}")
    return target if target in ENGINE_STAGES else ENGINE_STAGES[-1]


def engine_device(value: Any) -> tuple[str, str | None]:
    """A TaskRequest `device` as FineSub's device and the `CUDA_VISIBLE_DEVICES` it implies.

    FineSub takes only `cuda` or `cpu`; which card is chosen through the environment.
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
    """The speaker keywords for a normalized `diarization` block.

    `off` is passed explicitly: an unset level is the engine's own default,
    which depends on the stage it is asked for.
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
    #: The card the request named, if it named one: this process's `CUDA_VISIBLE_DEVICES`.
    cuda_visible_devices: str | None

    @property
    def stage(self) -> str:
        return str(self.keywords["stage"])


def native_dir(job: Mapping[str, Any]) -> Path:
    options = job.get("options") or {}
    return Path(options.get("native_dir") or Path(job["task_dir"]) / "native" / "finesub-transcribe")


def run_plan(job: Mapping[str, Any]) -> RunPlan:
    options = job.get("options") or {}
    title = str(options.get("title") or Path(job["source"]).stem)
    device, visible = engine_device(options.get("device"))
    directory = native_dir(job)
    record_dir = options.get("record_dir") or directory / "llm-artifacts"
    keywords: dict[str, Any] = {
        "output_path": directory / f"{title}.srt",
        "stage": engine_stage(job["target"]),
        "language": job.get("language", "ja"),
        "device": device,
        "gpu_tier": str(options.get("gpu_tier") or AUTO_GPU_TIER),
        "separate": bool(options.get("separate", True)),
        # Read by the ASR referee's context (`build_asr_context`), and that
        # context is read only when `knowledge` is not `none`.
        "extra_info": str(options.get("extra_info") or ""),
        "knowledge": options.get("knowledge", "update"),
        "task_id": str(options.get("task_id") or Path(job["task_dir"]).name),
        # The correction's record directory: whether the correction has begun
        # decides whether the speaker rows may be split (`split_choice`).
        "task_artifact_dir": Path(record_dir),
        "resume": True,
        **diarize_keywords(options.get("diarization")),
    }
    return RunPlan(input_path=str(job["source"]), keywords=keywords, cuda_visible_devices=visible)


# --- products ------------------------------------------------------------------------


def evidence_path(diar_json: Path) -> Path:
    """The acoustic evidence beside the speaker timeline (`speech.diarization.evidence.evidence_path`)."""

    return diar_json.with_name(diar_json.name.removesuffix("-diar.json") + "-diar-evidence.npz")


def native_products(paths: Any) -> dict[str, str]:
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
    return {f"{ENGINE_ID}.{name}": str(path) for name, path in candidates.items() if path.is_file()}


# --- transcript.json -----------------------------------------------------------------


def _number(value: Any, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TranscriptError(f"{where} must be a number")
    number = float(value)
    if number != number or number in (float("inf"), float("-inf")):
        raise TranscriptError(f"{where} must be finite")
    return number


def _text(value: Any, where: str) -> str:
    if not isinstance(value, str):
        raise TranscriptError(f"{where} must be a string")
    return value


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


def _duration(vad_json: Path | None, lines: list[Mapping[str, Any]]) -> float:
    if vad_json is not None and vad_json.is_file():
        value = json.loads(vad_json.read_text(encoding="utf-8")).get("audio_duration")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
    return max((line["end"] for line in lines), default=0.0)


def project_transcript(
    product: Path,
    *,
    stage: str,
    language: str,
    vad_json: Path | None = None,
) -> dict[str, Any]:
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
    lines: list[dict[str, Any]] = []
    for index, segment in enumerate(segments):
        where = f"{product.name} segments[{index}]"
        if not isinstance(segment, Mapping):
            raise TranscriptError(f"{where} must be an object")
        if "words" in segment and not isinstance(segment["words"], list):
            raise TranscriptError(f"{where}.words must be a list")
        line: dict[str, Any] = {
            "id": str(index + 1),
            "start": _number(segment.get("start"), f"{where}.start"),
            "end": _number(segment.get("end"), f"{where}.end"),
            "text": _text(segment.get("text"), f"{where}.text"),
        }
        if line["start"] < 0 or line["start"] > line["end"]:
            raise TranscriptError(f"{where}: start {line['start']} is not within [0, end {line['end']}]")
        if "words" in segment:
            words = []
            for number, word in enumerate(segment["words"]):
                if not isinstance(word, Mapping):
                    raise TranscriptError(f"{where}.words[{number}] must be an object")
                item = {
                    "text": _text(word.get("word"), f"{where}.words[{number}].text"),
                    "start": _number(word.get("start"), f"{where}.words[{number}].start"),
                    "end": _number(word.get("end"), f"{where}.words[{number}].end"),
                }
                if item["start"] > item["end"]:
                    raise TranscriptError(f"{where}.words[{number}]: start is after end")
                words.append(item)
            line["words"] = words
        lines.append(line)
    model = (metadata.get("asr_align") or {}).get("model")
    out: dict[str, Any] = {"schema": TRANSCRIPT_SCHEMA, "engine": ENGINE_ID}
    if isinstance(model, str) and model:
        out["model"] = model
    out["language"] = _language(language, metadata, segments)
    out["duration"] = _duration(vad_json, lines)
    out["lines"] = lines
    out["metadata"] = {
        "stage": stage,
        "projected_from": f"{ENGINE_ID}.{stage}_json",
        "sha256": hashlib.sha256(data).hexdigest(),
    }
    return out


def write_transcript(document: Mapping[str, Any], path: Path) -> Path:
    """Write atomically, the way the core writes one (`nonoka_x.transcription.model.write_transcript`)."""

    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(f".{path.name}.part")
    partial.write_text(json.dumps(document, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    os.replace(partial, path)
    return path


# --- the run -------------------------------------------------------------------------


# audio-separator logs that ONNXruntime has no CUDA provider; FineSub separates
# with PyTorch, so the warning is irrelevant and reads as if the GPU were unused.
_MISLEADING_ONNX_CUDA_WARNING = "CUDAExecutionProvider not available in ONNXruntime"


class _MuteSeparatorONNXWarning(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return _MISLEADING_ONNX_CUDA_WARNING not in record.getMessage()


_SEPARATOR_FILTER = _MuteSeparatorONNXWarning()


def mute_separator_onnx_warning() -> None:
    for name in ("separator", "audio_separator", "audio_separator.separator.separator"):
        logger = logging.getLogger(name)
        if _SEPARATOR_FILTER not in logger.filters:
            logger.addFilter(_SEPARATOR_FILTER)


def transcribe(job: Mapping[str, Any], reporter: Any) -> dict[str, Any]:
    """Run the job under `reporter` (FineSub's protocol); the core's `TranscriptionResult` as JSON."""

    plan = run_plan(job)
    # The card the request named is this process's `CUDA_VISIBLE_DEVICES`,
    # set here, before anything loads CUDA, and unconditionally: the process
    # was started with nothing inherited (plan 4.5 item 27; WP-H question H6).
    if plan.cuda_visible_devices is not None:
        os.environ["CUDA_VISIBLE_DEVICES"] = plan.cuda_visible_devices
    Path(plan.keywords["output_path"]).parent.mkdir(parents=True, exist_ok=True)
    mute_separator_onnx_warning()

    from finesub.reporting import quieted_libraries, reporting_to
    from finesub.stages import run_transcription

    with reporting_to(reporter), quieted_libraries("normal"):
        paths = run_transcription(plan.input_path, **plan.keywords)

    native = native_products(paths)
    stage = plan.stage
    target = Path(job["task_dir"]) / TRANSCRIPT_FILENAME
    written = False
    if stage in ("aligned", "stable"):
        document = project_transcript(
            Path(getattr(paths, f"{stage}_json")),
            stage=stage,
            language=str(job.get("language", "ja")),
            vad_json=Path(paths.vad_json),
        )
        write_transcript(document, target)
        written = True
    return {
        "transcript_json": str(target),
        "native": native,
        "metadata": {"stage": stage, "transcript": written},
    }
