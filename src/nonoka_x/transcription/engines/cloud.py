"""What the three cloud API engines share: readiness, the run around a backend, `transcript.json`.

`docs/transcription-engines-plan.md` WP3 (4.1 items 4, 6). MAI-Transcribe-2 (`mai2`), Gemini 3.5 Transcribe
(`gemini`) and MOSS Pro (`moss_pro`) each wrap their backend as a `TranscriptionEngine` on top of `CloudEngine`.
They run in the launcher's environment only -- httpx and the managed ffmpeg/ffprobe (the `media` tools) -- and
import nothing of `enhance/`, torch, numpy or FineSub: the first version wires no enhancement (4.1 item 6).

One run (`CloudEngine.transcribe`):

1. the source's duration (ffprobe);
2. the backend's transcript, every provider call cached under `job.cache_dir` (`cache.CallCache`), so a rerun
   spends no quota. The cache is keyed by the audio's fingerprint and **discards the answers of another
   recording**, so the directory must be the task's own (one per task, never shared);
3. lines through `lines.py` (production's DP), then `transcript.json` in the task directory.

Before the first upload the run says once that the audio leaves this machine; before every call it asks the
reporter whether the task was cancelled.

Lines in `transcript.json` (owner 2026-11-12, WP3 question 8): one per line `lines.py` built, in its order,
`id` the 1-based position; `start`, `end`, `text` as built; `words` with their `text`, `start`, `end`;
`speaker` the line's `line_speaker` -- the provider's own label as the stitching left it (`S0`, `w0:S1`,
`c1:S01`, ...), `unknown` for a line without one -- exactly what FineSub's joint-ASR stage wrote into
`-aligned.json`, so finesub correction gets the same line labels as before.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .. import (
    TRANSCRIPT_FILENAME,
    EngineSpec,
    Issue,
    Readiness,
    ReadinessContext,
    Reporter,
    TranscriptDocument,
    TranscriptLine,
    TranscriptWord,
    TranscriptionJob,
    TranscriptionResult,
    write_transcript,
)
from .. import audio, keys
from .. import lines as L
from ..reporting import current_reporter, reporting_to
from ..segmentation import dp as seg
from ..units import Transcript

#: The runtime facade's id of ffmpeg + ffprobe (`nonoka_x.runtime.MEDIA`).
MEDIA = "media"
#: `job.options` keys every cloud engine reads.
OPTION_LENGTH_SCALE = "split_length_scale"


class EngineCancelled(RuntimeError):
    """The task was cancelled while the engine ran."""


class CloudEngineError(RuntimeError):
    """A cloud engine cannot run as asked (no key, a bad option)."""


# --- readiness ---------------------------------------------------------------------------------------------


def media_issues(runtime: Any | None) -> list[Issue]:
    """The media tools (ffmpeg, ffprobe) through the runtime facade; never `PATH`."""

    if runtime is None:
        return [Issue("runtime_unavailable", "无法查询本机运行环境（媒体工具 ffmpeg / ffprobe）")]
    try:
        ready = bool(runtime.component_ready(MEDIA))
    except LookupError:
        ready = False
    return [] if ready else [Issue("missing_component", "缺少运行环境组件：media（ffmpeg / ffprobe）")]


def has_key(store: Any, name: str) -> bool:
    return bool(keys.pool(name, store=store))


# --- the run -----------------------------------------------------------------------------------------------


def length_scale(options: Mapping[str, Any]) -> seg.SplitParams:
    """The DP's split parameters for `options["split_length_scale"]` (FineSub's `[segmentation] length_scale`,
    default 1.0, 0.6-1.6)."""

    value = options.get(OPTION_LENGTH_SCALE)
    try:
        scale = seg.LENGTH_SCALE_DEFAULT if value is None else seg.check_length_scale(value)
    except ValueError as error:
        raise CloudEngineError(str(error)) from None
    return seg.split_params_for_length_scale(scale)


def guarded(caller: Callable[..., Any], *, label: str, report: Reporter) -> Callable[..., Any]:
    """`caller` asking `report.cancelled()` before every call, and saying before the first one that the audio
    goes to `label` (FineSub's joint stage said it the same way, once per run)."""

    said = False

    def call(*args: Any, **kwargs: Any) -> Any:
        nonlocal said
        if report.cancelled():
            raise EngineCancelled("task cancelled during transcription")
        if not said:
            said = True
            current_reporter().warning("cloud-asr-upload", f"音频发给 {label} 转写", impact="音频离开本机")
        return caller(*args, **kwargs)

    return call


def document(*, engine: str, model: str, language: str, duration: float, lines: Sequence[Mapping[str, Any]],
             metadata: Mapping[str, Any]) -> TranscriptDocument:
    """`transcript.json` from the lines `lines.py` built (module docstring)."""

    out = []
    for index, line in enumerate(lines):
        words = tuple(TranscriptWord(text=w["word"], start=w["start"], end=w["end"]) for w in line.get("words") or ())
        out.append(TranscriptLine(id=str(index + 1), start=line["start"], end=line["end"], text=line["text"],
                                  speaker=line.get(L.LINE_SPEAKER) or None, words=words or None))
    return TranscriptDocument(engine=engine, model=model, language=language, duration=duration, lines=tuple(out),
                              metadata=dict(metadata))


class CloudEngine:
    """The `TranscriptionEngine` around one cloud backend; subclasses give `spec`, `label`, `key_issues`,
    `run` and `build_lines`.

    `store` is Nonoka's key store (a `nonoka_x.config.ConfigStore`: `key_pool`, `state_file`), `runtime` the
    runtime facade (`nonoka_x.runtime.RuntimeManager`: `component_ready`, `tool_path`).
    """

    spec: EngineSpec
    #: The provider's name as the user sees it (the upload notice, the log).
    label: str

    def __init__(self, store: Any | None = None, runtime: Any | None = None) -> None:
        self._store = store
        self._runtime = runtime

    # readiness

    def readiness(self, context: ReadinessContext) -> Readiness:
        issues = media_issues(self._runtime)
        if self._store is None:
            issues.append(Issue("config_unavailable", f"无法读取密钥，{self.spec.label} 不可用"))
        else:
            issues.extend(self.key_issues(self._store))
        return Readiness(tuple(issues))

    def key_issues(self, store: Any) -> list[Issue]:
        raise NotImplementedError

    # transcription

    def transcribe(self, job: TranscriptionJob, report: Reporter) -> TranscriptionResult:
        if report.cancelled():
            raise EngineCancelled("task cancelled before transcription started")
        if self._store is None:
            raise CloudEngineError(f"{self.spec.label}: no key store")
        params = length_scale(job.options)
        # `auto` (or none): the provider detects it, as `--language` left out did; transcript.json says `auto`.
        language = None if (job.language or "auto").strip().lower() == "auto" else job.language
        keys.use_store(self._store)
        audio.use_runtime(self._runtime)
        with reporting_to(report):
            duration = audio.probe_media_duration(job.source)
            transcript, record = self.run(job, report, duration=duration, language=language)
            lines = self.build_lines(transcript, duration=duration, params=params, language=language)
        metadata: dict[str, Any] = {**record, "segment_split": seg.split_params_metadata(params)}
        if transcript.log:
            metadata["log"] = transcript.log
        target = Path(job.task_dir) / TRANSCRIPT_FILENAME
        write_transcript(document(engine=self.spec.id, model=transcript.model, language=language or "auto",
                                  duration=duration, lines=lines, metadata=metadata), target)
        report.log(f"{self.label} 转写完成", L.summary(lines))
        return TranscriptionResult(transcript_json=target, metadata={"stage": "transcript", "transcript": True})

    def run(self, job: TranscriptionJob, report: Reporter, *, duration: float,
            language: str | None) -> tuple[Transcript, dict[str, Any]]:
        """The backend's transcript and what `transcript.json`'s metadata records about the route."""
        raise NotImplementedError

    def build_lines(self, transcript: Transcript, *, duration: float, params: seg.SplitParams,
                    language: str | None) -> list[dict]:
        raise NotImplementedError


__all__ = [
    "MEDIA",
    "OPTION_LENGTH_SCALE",
    "CloudEngine",
    "CloudEngineError",
    "EngineCancelled",
    "document",
    "guarded",
    "has_key",
    "length_scale",
    "media_issues",
]
