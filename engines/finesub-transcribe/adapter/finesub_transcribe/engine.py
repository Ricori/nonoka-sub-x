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

import gc
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


_MEMORY_POLICY_ATTR = "_nonoka_verification_memory_policy"
#: Minimum free physical VRAM (GiB) required before placing Qwen3-ASR-0.6B bf16
#: weights (~1.5 GiB) on CUDA. Below this on Windows WDDM, `model.to("cuda")`
#: does not raise OOM and instead spills weights into shared system RAM over
#: PCIe, stalling autoregressive decoding for tens of minutes.
_MIN_REFEREE_WEIGHTS_VRAM_GIB = 1.6
_CPU_EAGER_MAX_CLIPS = 4
_CUDA_BATCH_VRAM_THRESHOLDS: tuple[tuple[float, int], ...] = (
    (2.5, 16),
    (1.5, 8),
    (0.9, 4),
    (0.45, 2),
)


def _cuda_free_gib(device: Any = None) -> float | None:
    try:
        import torch

        if not torch.cuda.is_available():
            return None
        free_bytes, _total_bytes = (
            torch.cuda.mem_get_info(device) if device is not None else torch.cuda.mem_get_info()
        )
        return float(free_bytes) / float(1024**3)
    except Exception:  # noqa: BLE001 - unavailable CUDA query falls back cleanly
        return None


def _release_cuda_cache() -> None:
    gc.collect()
    try:
        import torch

        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:  # noqa: BLE001 - best-effort release
        pass


def _is_cuda_oom(exc: BaseException) -> bool:
    name = type(exc).__name__
    if name == "OutOfMemoryError":
        return True
    return isinstance(exc, RuntimeError) and "out of memory" in str(exc).lower()


def _eager_batch_limit(default_max_clips: int, *, device: Any, free_gib: float | None) -> int:
    """Safe clip count per eager batch given device and post-load free physical VRAM."""

    cap = max(1, int(default_max_clips))
    if not str(device).strip().lower().startswith("cuda"):
        return min(cap, _CPU_EAGER_MAX_CLIPS)
    if free_gib is None:
        return cap
    for threshold_gib, limit in _CUDA_BATCH_VRAM_THRESHOLDS:
        if free_gib >= threshold_gib:
            return min(cap, limit)
    return 1


def _install_whisper_pool_release() -> None:
    """Release CTranslate2 Whisper weights/allocator when `FwRefineModelPool.close()` runs.

    In locked `vad_asr_stage.run_vad_asr`, `with model_pool.lease() as model:`
    leaves `model` bound in the function's local frame after the `with` block
    exits. Clearing `self._idle` alone therefore never drops the refcount of
    `RefinedWhisperModel` to zero, keeping 2.1-3.9 GiB of Whisper VRAM resident
    throughout the Qwen verification pass. Explicitly unloading and detaching
    `item.model` inside `close()` frees CTranslate2's GPU memory immediately.
    """

    try:
        from finesub.speech.recognition import fw_refine_backend
    except Exception:  # noqa: BLE001 - optional in stubbed / vocal-only tests
        return
    pool_cls = getattr(fw_refine_backend, "FwRefineModelPool", None)
    original_close = getattr(pool_cls, "close", None)
    if pool_cls is None or original_close is None or getattr(original_close, _MEMORY_POLICY_ATTR, False):
        return

    def close(self: Any) -> None:
        with self._condition:
            models, self._idle, self._loaded = self._idle, [], 0
        for item in models:
            encoder_cache = getattr(item, "_encoder_cache", None)
            if hasattr(encoder_cache, "clear"):
                try:
                    encoder_cache.clear()
                except Exception:  # noqa: BLE001
                    pass
            ct2_model = getattr(item, "model", None)
            if ct2_model is not None:
                unload = getattr(ct2_model, "unload_model", None)
                if callable(unload):
                    try:
                        unload(to_cpu=False)
                    except Exception:  # noqa: BLE001
                        pass
                try:
                    item.model = None
                except Exception:  # noqa: BLE001
                    pass
        models.clear()
        _release_cuda_cache()

    setattr(close, _MEMORY_POLICY_ATTR, True)
    pool_cls.close = close


def _wrap_audio_encoder_per_clip(model: Any) -> None:
    """Encode multi-clip batches clip-by-clip with trailing zero-chunk trimming.

    `Qwen3ASREncoder` processes each clip independently, so encoding one clip
    at a time (mirroring `FixedShapeDecoder._prefill_embeddings`) produces the
    same features while cutting the eager conv front-end peak from +1.73 GiB
    (at B=16) down to ~0.12 GiB and skipping padded silence chunks.
    """

    inner = getattr(model, "model", None)
    original_get = getattr(inner, "get_audio_features", None)
    if inner is None or original_get is None or getattr(original_get, _MEMORY_POLICY_ATTR, False):
        return

    def get_audio_features(
        input_features: Any,
        input_features_mask: Any,
        **kwargs: Any,
    ) -> Any:
        if (
            input_features is None
            or input_features_mask is None
            or getattr(input_features, "ndim", 0) < 3
            or int(input_features.shape[0]) <= 1
            or kwargs.get("output_attentions")
            or kwargs.get("output_hidden_states")
            or kwargs.get("cu_seqlens") is not None
        ):
            return original_get(input_features, input_features_mask, **kwargs)

        import torch

        audio_tower = getattr(inner, "audio_tower", None)
        n_window = int(getattr(audio_tower, "n_window", 50) or 50)
        chunk_len = n_window * 2
        padded_len = int(input_features.shape[-1])
        return_dict = kwargs.get("return_dict", True)
        call_kwargs = dict(kwargs)
        call_kwargs["return_dict"] = True

        hidden_parts = []
        pooler_parts = []
        for idx in range(int(input_features.shape[0])):
            mask_row = input_features_mask[idx]
            nonzero = mask_row.nonzero(as_tuple=False)
            if nonzero.numel() > 0 and chunk_len > 0:
                last_valid = int(nonzero[-1].item()) + 1
                trimmed = ((last_valid + chunk_len - 1) // chunk_len) * chunk_len
                trimmed = min(max(trimmed, chunk_len), padded_len)
            else:
                trimmed = padded_len
            part = original_get(
                input_features[idx : idx + 1, :, :trimmed],
                input_features_mask[idx : idx + 1, :trimmed],
                **call_kwargs,
            )
            hidden_parts.append(part.last_hidden_state)
            pooler_parts.append(part.pooler_output)

        last_hidden_state = torch.cat(hidden_parts, dim=0)
        pooler_output = torch.cat(pooler_parts, dim=0)
        if not return_dict:
            return (last_hidden_state, pooler_output)
        from transformers.modeling_outputs import BaseModelOutputWithPooling

        return BaseModelOutputWithPooling(
            last_hidden_state=last_hidden_state,
            pooler_output=pooler_output,
        )

    setattr(get_audio_features, _MEMORY_POLICY_ATTR, True)
    inner.get_audio_features = get_audio_features


def _generate_batch_with_oom_split(referee: Any, clips: Any, batch: list[int], budget: int) -> list[Any]:
    pending: list[list[int]] = [list(batch)]
    out: list[Any] = []
    while pending:
        chunk = pending.pop(0)
        inputs = referee._transcription_inputs([clips[i] for i in chunk], referee._processor)
        try:
            replies = referee._generate(inputs, budget, compiled=False)
        except Exception as exc:
            del inputs
            if len(chunk) <= 1 or not _is_cuda_oom(exc):
                raise
            _release_cuda_cache()
            mid = len(chunk) // 2
            pending[0:0] = [chunk[:mid], chunk[mid:]]
            continue
        del inputs
        out.extend(replies)
    return out


def _install_qwen_referee_memory_policy() -> None:
    """Keep eager Qwen3-ASR verification inside physical VRAM without editing locked files."""

    try:
        from finesub.speech.verification import qwen_referee
    except Exception:  # noqa: BLE001 - optional in stubbed / vocal-only tests
        return
    referee_cls = getattr(qwen_referee, "QwenReferee", None)
    if referee_cls is None:
        return

    original_load = getattr(referee_cls, "_load_model", None)
    if original_load is not None and not getattr(original_load, _MEMORY_POLICY_ATTR, False):
        def _load_model(self: Any) -> Any:
            requested = str(getattr(self, "_device", "cpu"))
            if requested.startswith("cuda"):
                _release_cuda_cache()
                free_gib = _cuda_free_gib(requested)
                if free_gib is not None and free_gib < _MIN_REFEREE_WEIGHTS_VRAM_GIB:
                    try:
                        from finesub.reporting import current_reporter

                        current_reporter().warning(
                            "cpu-fallback",
                            f"Qwen referee falling back to CPU (only {free_gib:.2f} GiB free VRAM)",
                            impact="第二模型校验会明显变慢",
                        )
                    except Exception:  # noqa: BLE001
                        pass
                    self._device = "cpu"
                    try:
                        model = original_load(self)
                    finally:
                        self._device = requested
                    _wrap_audio_encoder_per_clip(model)
                    return model
            model = original_load(self)
            _wrap_audio_encoder_per_clip(model)
            return model

        setattr(_load_model, _MEMORY_POLICY_ATTR, True)
        referee_cls._load_model = _load_model

    original_transcribe = getattr(referee_cls, "transcribe_batch", None)
    if original_transcribe is not None and not getattr(original_transcribe, _MEMORY_POLICY_ATTR, False):
        def transcribe_batch(
            self: Any,
            clips: Any,
            *,
            max_new_tokens: int | None = None,
            on_batch: Any = None,
        ) -> list[tuple[str, str | None]]:
            if not clips:
                return []
            accel_off = (
                getattr(self, "_accel", "auto") == "off"
                or bool(getattr(self, "_accel_failed", False))
                or os.environ.get(qwen_referee.ACCEL_ENV, "").strip() == "0"
            )
            if not accel_off:
                return original_transcribe(
                    self, clips, max_new_tokens=max_new_tokens, on_batch=on_batch
                )
            budget = int(max_new_tokens or qwen_referee.MAX_NEW_TOKENS)
            results: list[tuple[str, str | None]] = [("", None)] * len(clips)
            lengths = [len(clip) for clip in clips]
            with self._lock, qwen_referee.phase_timing.phase("qwen.infer"):
                self._ensure_model()
                device = getattr(self._model, "device", self._device)
                if str(device).startswith("cuda"):
                    _release_cuda_cache()
                    free_gib = _cuda_free_gib(device)
                else:
                    free_gib = None
                max_clips = _eager_batch_limit(
                    qwen_referee.BATCH_CLIPS, device=device, free_gib=free_gib
                )
                batches = qwen_referee.plan_batches(lengths, max_clips=max_clips)
                done = 0
                for batch in batches:
                    replies = _generate_batch_with_oom_split(self, clips, batch, budget)
                    for index, reply in zip(batch, replies):
                        results[index] = reply
                    done += len(batch)
                    if on_batch is not None:
                        on_batch(done, len(clips))
            return results

        setattr(transcribe_batch, _MEMORY_POLICY_ATTR, True)
        referee_cls.transcribe_batch = transcribe_batch


def install_verification_memory_policy() -> None:
    """Install idempotent Whisper-release and eager-Qwen VRAM guards without touching locked FineSub files."""

    _install_whisper_pool_release()
    _install_qwen_referee_memory_policy()


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
    if plan.stage in ("aligned", "stable"):
        install_verification_memory_policy()

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
