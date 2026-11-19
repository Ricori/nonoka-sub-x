"""Local MOSS (`--asr moss-local`): open MOSS-Transcribe-Diarize on this machine's GPU, with run-time rescues for
its known failures, as one more joint-ASR backend.

The source's mix -> ffmpeg 16 kHz float -> `rescue.transcribe` (passes cut to the VRAM budget, rescues, stitching)
-> a segment `model.Transcript`, the shape MOSS Pro's takes, so the stage builds its lines the same way (S5b / S6b).
The model is loaded for the task and unloaded when the step ends, as the Whisper pool is. Everything this step does
to the process -- the allocator cap, the SDPA swap during prefill (`decoder`), the GPU lock -- is undone in
`finally`: a batch goes on to its next item in the same process after a failure.

Why it is built this way and how it compares with MOSS Pro: `docs/joint-asr.md`.
"""

from __future__ import annotations

import contextlib
import gc
import json
import os
import subprocess
import sys
import threading
import time
import traceback
from collections import Counter
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterator, Mapping

from ....runtime import ASR_RUNTIME
from ... import (
    DIARIZATION_LEVELS,
    TRANSCRIPT_FILENAME,
    EngineEnvironment,
    EngineFeatures,
    EngineSpec,
    Issue,
    Readiness,
    ReadinessContext,
    Reporter,
    TranscriptionJob,
    TranscriptionResult,
     audio,
    lines as L,
    write_transcript,
)
from ...enhance import GPU_LOCK, missing_modules, unavailable
from ...reporting import reporting_to
from ...segmentation import dp as seg
from ...units import Transcript, Unit, segment_turns
from .. import MOSS_LOCAL, JointAsrError
from ..cloud import MEDIA, EngineCancelled, document, length_scale, media_issues

if TYPE_CHECKING:
    import numpy as np

#: What the metadata records as the model; the revision goes beside it.
MODEL_NAME = "moss-transcribe-diarize"
ENGINE_ID = MOSS_LOCAL
LABEL = "MOSS 0.9B"

SPEC = EngineSpec(
    id=ENGINE_ID,
    label=LABEL,
    kind="local-gpu",
    features=EngineFeatures(
        speakers=True,
        word_timestamps=False,
        separate=False,
        diarization_levels=DIARIZATION_LEVELS,
    ),
    keys=(),
    environment=EngineEnvironment(
        components=(ASR_RUNTIME, MEDIA),
        models=(MODEL_NAME, "speaker-embedding"),
        gpu="required",
    ),
    billing="",
)


def transcribe(source: Path, *, language: str | None, duration: float, gpu_tier: str | None,
               device: str | None) -> tuple[Transcript, dict[str, Any]]:
    """(transcript, what the metadata records about the run). Raises `JointAsrError` when this run cannot use
    local MOSS (no suitable card, `--gpu-tier cpu`, `--device cpu`, no usable transformers)."""
    from ...enhance import model_dir
    from ...enhance.device import moss_local_unusable_reason
    from ...enhance.resources import get_resource_profile
    from .. import JointAsrError
    from . import decoder as D

    reason = moss_local_unusable_reason(gpu_tier, device)
    if reason is not None:
        raise JointAsrError(reason)
    try:
        D.check_transformers()
    except RuntimeError as e:
        raise JointAsrError(str(e)) from e
    if not language:
        print("Warning: --asr moss-local without --language: the default (Chinese) prompt, and no check that the "  # product output
              "transcript is in the source's language; an English source may come out in another language. "
              "Give --language.", file=sys.stderr)
    budget_gib = get_resource_profile(gpu_tier, device=device).usable_gib - D.CONTEXT_GIB
    directory = model_dir(D.MANIFEST_ID)
    if directory is None:
        raise JointAsrError(f"local MOSS needs the {D.MANIFEST_ID} model, which is not installed")
    audio = _decode(source)
    with GPU_LOCK, _allocator_capped(budget_gib), _triton_cache_in_checkout(), _quiet_transformers():
        result = _decode_with_model(directory, audio, language, budget_gib)
    units = [Unit.from_dict(s) for s in result.segments if s["end"] > s["start"]]
    transcript = Transcript(MOSS_LOCAL, MODEL_NAME, duration, "segment", segment_turns(units))
    return transcript, {"revision": directory.name, "kv": result.passes, "events": event_counts(result.events)}


def _decode_with_model(directory, audio: np.ndarray, language: str | None, budget_gib: float):
    import torch

    from . import decoder as D
    from . import rescue

    loaded = None
    try:
        loaded = D.load(directory)
        return rescue.transcribe(*loaded, audio, language, budget_gib)
    except BaseException as e:
        # The traceback's frames -- of this exception and of every one in its chain -- hold the model and a pass's
        # K/V buffers until whoever catches it is done; cleared, the `finally` below frees them before the GPU lock
        # goes to the next user.
        _clear_chain(e)
        raise
    finally:
        loaded = None
        gc.collect()
        torch.cuda.empty_cache()


def _clear_chain(error: BaseException) -> None:
    seen: set[int] = set()
    todo: list[BaseException | None] = [error]
    while todo:
        e = todo.pop()
        if e is None or id(e) in seen:
            continue
        seen.add(id(e))
        traceback.clear_frames(e.__traceback__)
        todo += [e.__cause__, e.__context__]


def event_counts(events: list[dict]) -> dict[str, int]:
    """How often each event kind came up, over every pass, part and gap window (their events nest): the rescues,
    plus the token budget running out (`cap`) and acoustic-event segments (`acoustic`, once per pass that had any).
    The passes' K/V records are left out (they go to `kv`)."""
    counts: Counter[str] = Counter()

    def walk(evs: list[dict]) -> None:
        for e in evs:
            if e["kind"] not in ("kv", "part"):
                counts[e["kind"]] += 1
            walk(e.get("events") or [])

    walk(events)
    return dict(sorted(counts.items()))


def _decode(source: Path) -> "np.ndarray":
    """The whole source as 16 kHz mono float32: the input the model was measured on (through int16 it differs)."""
    import numpy as np

    from ...audio import decode_pcm16k

    return np.frombuffer(decode_pcm16k(source, sample_format="f32le"), dtype=np.float32).copy()


@contextlib.contextmanager
def _allocator_capped(budget_gib: float) -> Iterator[None]:
    """PyTorch's allocator held to the budget on the current card while local MOSS runs, then released: the cap is
    process-wide, and the stages after it (separation, the referee, the aligner) have budgets of their own."""
    import torch

    from ...enhance.device import total_vram_gib

    card = torch.cuda.current_device()
    total = total_vram_gib()
    torch.cuda.set_per_process_memory_fraction(min(1.0, budget_gib / total) if total else 1.0, card)
    try:
        yield
    finally:
        torch.cuda.set_per_process_memory_fraction(1.0, card)


@contextlib.contextmanager
def _quiet_transformers() -> Iterator[None]:
    """Without transformers' notice that the remote processor's `feature_extractor_class` is deprecated, and
    without its weight-loading bar: neither is something a user acts on."""
    import logging

    from transformers.utils import logging as hf_logging

    def keep(record: logging.LogRecord) -> bool:
        return "_class = '" not in record.getMessage()

    logger = logging.getLogger("transformers.processing_utils")
    logger.addFilter(keep)
    bar_was_on = hf_logging.is_progress_bar_enabled()
    hf_logging.disable_progress_bar()
    try:
        yield
    finally:
        logger.removeFilter(keep)
        if bar_was_on:
            hf_logging.enable_progress_bar()


@contextlib.contextmanager
def _triton_cache_in_checkout() -> Iterator[None]:
    """Triton's compiled kernels in `<runtime cache>/moss-local/triton` (or a directory of their own under the
    temporary directory when no managed runtime is bound) instead of `~/.triton`, unless the user set
    TRITON_CACHE_DIR; Triton keys the files by source, version and card itself. Only while this step runs:
    Inductor reads the same variable, and the separator's and the referee's kernels belong in their own keyed
    directories. Every Triton compile of local MOSS (the probes, each bucket's graph capture) is inside it."""
    if os.environ.get("TRITON_CACHE_DIR"):
        yield
        return
    import tempfile

    manager = audio.current_runtime()
    layers = getattr(manager, "layers", None) if manager is not None else None
    cache_root = getattr(getattr(layers, "paths", None), "cache", None)
    if isinstance(cache_root, Path):
        path = cache_root / "moss-local" / "triton"
    else:
        path = Path(tempfile.gettempdir()) / "nonoka-moss-local-triton"
    path.mkdir(parents=True, exist_ok=True)
    os.environ["TRITON_CACHE_DIR"] = str(path)
    try:
        yield
    finally:
        os.environ.pop("TRITON_CACHE_DIR", None)


def build_lines(
    source: Path,
    transcript: Transcript,
    *,
    duration: float,
    params: seg.SplitParams,
    language: str | None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Build lines from local MOSS segments: Silero S5b timing when available, else character-weighted interpolation."""

    units = [u.as_dict() for u in transcript.units()]
    if unavailable("moss-timing") is None:
        try:
            import numpy as np

            from ...audio import decode_pcm16k
            from ...enhance import moss_timing

            pcm = np.frombuffer(decode_pcm16k(source, sample_format="s16le"), dtype=np.int16).copy()
            probs = moss_timing.frame_probabilities(pcm)
            timed_segments, counts = moss_timing.timed(units, probs, duration=duration, params=params)
            segments, _ = L.segment_lines(timed_segments, duration=duration, params=params, language=language)
            return segments, {"timing": "silero-s5b", **counts}
        except Exception as exc:  # noqa: BLE001 - fall back to interpolation rather than failing the finished decode
            from ...reporting import current_reporter

            current_reporter().warning(
                "moss-local-timing",
                f"Silero 段内计时不可用（{type(exc).__name__}: {exc}），回退为按字数插值",
                impact="长句内部的时间切点按字数均匀估算",
            )
    segments, _ = L.segment_lines(
        L.interpolated_timed(units, duration=duration),
        duration=duration,
        params=params,
        language=language,
    )
    return segments, {"timing": "interpolated"}


def _model_ready(runtime: Any | None, model_id: str) -> bool:
    if runtime is None:
        return False
    try:
        if runtime.model_dir(model_id) is not None:
            return True
    except LookupError:
        pass
    try:
        return bool(runtime.model_ready(model_id))
    except LookupError:
        return False


class MossLocalEngine:
    """Local MOSS 0.9B (`OpenMOSS-Team/MOSS-Transcribe-Diarize`) as a `TranscriptionEngine`."""

    spec = SPEC
    label = LABEL

    def __init__(self, store: Any | None = None, runtime: Any | None = None) -> None:
        self._store = store
        self._runtime = runtime

    def readiness(self, context: ReadinessContext) -> Readiness:
        issues: list[Issue] = []
        if sys.platform != "win32":
            issues.append(Issue("unsupported_platform", "MOSS 0.9B 本地推理当前仅支持 Windows x64 / NVIDIA 显卡"))
            return Readiness(tuple(issues))
        issues.extend(media_issues(self._runtime))
        if self._runtime is None:
            issues.append(Issue("runtime_unavailable", "无法查询本机运行环境（Python 环境层与 MOSS 模型层）"))
        else:
            try:
                asr_ready = bool(self._runtime.component_ready(ASR_RUNTIME))
            except LookupError:
                asr_ready = False
            if not asr_ready:
                issues.append(Issue("missing_component", "缺少运行环境组件：asr-runtime（Python 环境层）"))
            if not _model_ready(self._runtime, MODEL_NAME):
                issues.append(Issue("missing_model", "尚未安装 MOSS 0.9B 模型层（models-moss）"))
        device = str(context.device or "").strip().lower()
        tier = str((context.options or {}).get("gpu_tier") or "").strip().lower()
        if device == "cpu" or tier == "cpu":
            issues.append(Issue("gpu_required", "MOSS 0.9B 需要 NVIDIA 显卡（RTX 30 系及以上，约 8 GB 显存），不支持 CPU 模式"))
        return Readiness(tuple(issues))

    def transcribe(self, job: TranscriptionJob, report: Reporter) -> TranscriptionResult:
        if report.cancelled():
            raise EngineCancelled("task cancelled before transcription started")
        params = length_scale(job.options)
        language = None if (job.language or "auto").strip().lower() == "auto" else job.language
        worker_python = self._runtime.worker_python() if self._runtime is not None else None
        need_subprocess = (
            bool(missing_modules("numpy", "torch", "transformers", "silero_vad"))
            and worker_python is not None
            and Path(worker_python).is_file()
        )
        if need_subprocess:
            return self._transcribe_in_subprocess(Path(worker_python), job, report, language=language, params=params)
        return self._transcribe_in_process(job, report, language=language, params=params)

    def _transcribe_in_process(
        self,
        job: TranscriptionJob,
        report: Reporter,
        *,
        language: str | None,
        params: seg.SplitParams,
    ) -> TranscriptionResult:
        audio.use_runtime(self._runtime)
        gpu_tier = job.options.get("gpu_tier")
        device = job.options.get("device")
        report.stage("asr", f"正在使用 {self.label} 本地识别")
        with reporting_to(report):
            duration = audio.probe_media_duration(job.source)
            transcript, record = transcribe(
                job.source,
                language=language,
                duration=duration,
                gpu_tier=str(gpu_tier) if gpu_tier is not None else None,
                device=str(device) if device is not None else None,
            )
            lines, timing_meta = build_lines(
                job.source, transcript, duration=duration, params=params, language=language,
            )
        metadata: dict[str, Any] = {
            **record,
            **timing_meta,
            "segment_split": seg.split_params_metadata(params),
        }
        if transcript.log:
            metadata["log"] = transcript.log
        target = Path(job.task_dir) / TRANSCRIPT_FILENAME
        write_transcript(
            document(
                engine=self.spec.id,
                model=transcript.model,
                language=language or "auto",
                duration=duration,
                lines=lines,
                metadata=metadata,
            ),
            target,
        )
        report.log(f"{self.label} 转写完成", L.summary(lines))
        return TranscriptionResult(transcript_json=target, metadata={"stage": "transcript", "transcript": True})

    def _transcribe_in_subprocess(
        self,
        python_exe: Path,
        job: TranscriptionJob,
        report: Reporter,
        *,
        language: str | None,
        params: seg.SplitParams,
    ) -> TranscriptionResult:
        from ....runtime.fsops import no_window

        src_root = Path(__file__).resolve().parents[3]
        env = self._runtime.worker_environment() if self._runtime is not None else os.environ.copy()
        env["PYTHONPATH"] = os.pathsep.join(
            part for part in (str(src_root), env.get("PYTHONPATH", "")) if part
        )
        env["HF_HUB_OFFLINE"] = "1"
        env["PYTHONUTF8"] = "1"
        layers = getattr(self._runtime, "layers", None)
        cache_root = getattr(getattr(layers, "paths", None), "cache", None)
        if isinstance(cache_root, Path):
            triton_cache = cache_root / "moss-local" / "triton"
            triton_cache.mkdir(parents=True, exist_ok=True)
            env.setdefault("TRITON_CACHE_DIR", str(triton_cache))

        models: dict[str, str] = {}
        tools: dict[str, str] = {}
        if self._runtime is not None:
            for model_id in (MODEL_NAME, "speaker-embedding"):
                try:
                    path = self._runtime.model_dir(model_id)
                except LookupError:
                    path = None
                if path is not None:
                    models[model_id] = str(path)
            for tool_id in ("ffmpeg", "ffprobe", "tokcount"):
                try:
                    tpath = self._runtime.tool_path(tool_id)
                except LookupError:
                    tpath = None
                if tpath is not None:
                    tools[tool_id] = str(tpath)

        payload = json.dumps(
            {"job": job.to_dict(), "models": models, "tools": tools},
            ensure_ascii=False,
        ) + "\n"
        process = subprocess.Popen(
            [str(python_exe), "-s", "-B", "-m", "nonoka_x.transcription.engines.moss_local"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            creationflags=no_window(),
        )
        assert process.stdin is not None and process.stdout is not None and process.stderr is not None
        process.stdin.write(payload)
        process.stdin.close()

        result_payload: dict[str, Any] | None = None
        error_message: str | None = None

        def read_stdout() -> None:
            nonlocal result_payload, error_message
            for line in process.stdout:
                text = line.strip()
                if not text:
                    continue
                try:
                    message = json.loads(text)
                except ValueError:
                    report.log(text)
                    continue
                if not isinstance(message, Mapping):
                    continue
                kind = message.get("type")
                if kind == "reporter":
                    method = str(message.get("method") or "")
                    call = getattr(report, method, None)
                    if callable(call):
                        call(*(message.get("args") or ()), **(message.get("kwargs") or {}))
                elif kind == "result" and isinstance(message.get("payload"), Mapping):
                    result_payload = dict(message["payload"])
                elif kind == "error":
                    error_message = str(message.get("message") or "MOSS 0.9B 转写进程失败")

        reader = threading.Thread(target=read_stdout, name="nonoka-moss-local-stdout", daemon=True)
        reader.start()
        try:
            while process.poll() is None:
                if report.cancelled():
                    process.kill()
                    process.wait()
                    raise EngineCancelled("task cancelled during transcription")
                time.sleep(0.1)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
            reader.join(timeout=5)
        stderr_tail = process.stderr.read().strip()
        if error_message:
            raise JointAsrError(error_message)
        if process.returncode != 0 or result_payload is None:
            detail = stderr_tail.splitlines()[-1] if stderr_tail else f"exit code {process.returncode}"
            raise JointAsrError(f"{self.label} 子进程异常退出：{detail}")
        return TranscriptionResult.from_dict(result_payload)

