"""Isolated FineSub worker. Stdin is one TaskRequest; stdout is NDJSON events."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sys
import traceback
from pathlib import Path
from typing import Any, Mapping

from .axis import AxisTranslation, translate_axis as translate_rows
from .gpu_tier import resolve_request_gpu_tier


def _normalize_engine_device(device_value: Any) -> tuple[str, str | None]:
    """Map TaskRequest device into (finesub_device, cuda_visible_devices).

    FineSub's CLI and runtime contract accepts only 'cuda' or 'cpu'. Specific
    GPU indexing is controlled by the CUDA_VISIBLE_DEVICES environment variable.
    """
    raw = str(device_value or "cuda").strip()
    lowered = raw.lower()
    if lowered.startswith("cuda:"):
        gpu_index = lowered.split(":", 1)[1].strip()
        if gpu_index.isdigit():
            return "cuda", gpu_index
        return "cuda", None
    if lowered == "cpu":
        return "cpu", None
    return ("cuda" if lowered.startswith("cuda") else raw), None


def emit(event_type: str, payload: Mapping[str, Any] | None = None) -> None:
    print(json.dumps({"type": event_type, "payload": dict(payload or {})}, ensure_ascii=False, separators=(",", ":")), flush=True)


# The engine warns once per run when the switch vector is outside its six
# measured combinations (finesub `capabilities.CALIBRATED_VECTORS`). Most of
# Nonoka X's switch matrix is, so it fires on nearly every task -- and there is
# nothing the user can do about it: `c` is derived by the same per-axis sum as
# the measured vectors, and a window that does overrun its output envelope is
# split in half and retried by the correction loop. Dropped rather than
# downgraded, per product decision. The sibling `routing-profile` warnings --
# continuity=parallel, and a vector whose measurement went stale -- still speak.
# Matching on the text is deliberate: an upstream rewording lets the line back
# in (noise, not breakage), and the vendor snapshot is hash-pinned, so a sync
# surfaces the change as a diff.
_UNCALIBRATED_VECTOR_NOTICE = "未标定：输出预算系数"

# FineSub 0.5.0 records every LLM call, local agent call and web retrieval as
# one debug line, which is the right shape for the run log it was written for
# -- and the wrong shape for a task log the user is watching, where a single
# correction pass would scroll past hundreds of successes. The failures are
# the part worth surfacing, and they are the ones that carry `why`: the
# endpoint's own words (`llm_runtime`), which is exactly what tells a rate
# limit apart from a spent key apart from a hung stage. Successes stay in the
# run log and the per-call artifacts, where nothing is lost.
_API_CALL_MESSAGE = "llm api call"

# audio-separator checks ONNXruntime execution providers on initialization and
# logs a warning if onnxruntime-gpu is missing. FineSub uses PyTorch CUDA and
# AOTInductor for BS-Roformer, not ONNXruntime, so this warning is irrelevant
# and misleads users into believing GPU acceleration is disabled.
_MISLEADING_ONNX_CUDA_WARNING = "CUDAExecutionProvider not available in ONNXruntime"


class _MuteSeparatorONNXWarningFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return _MISLEADING_ONNX_CUDA_WARNING not in record.getMessage()


def _install_separator_logging_filters() -> None:
    mute_filter = _MuteSeparatorONNXWarningFilter()
    for name in ("separator", "audio_separator", "audio_separator.separator.separator"):
        logging.getLogger(name).addFilter(mute_filter)


_install_separator_logging_filters()


class NonokaXReporter:
    def __init__(self) -> None:
        self._seen_warnings: set[tuple[str, str]] = set()
        self._seen_preset_cores: set[str] = set()

    def planned(self, stages) -> None:
        return

    def stage_started(self, stage: str, *, reused: bool = False, detail: str = "") -> None:
        emit("stage", {"stage": stage, "message": "已有结果，跳过" if reused else (detail or "正在处理"), "reused": reused})

    def progress(self, stage: str, *, completed: int, total: int | None = None, unit: str = "", detail: str = "") -> None:
        emit("progress", {"stage": stage, "completed": completed, "total": total, "unit": unit, "message": detail})

    def summary(self, stage: str, metrics) -> None:
        emit("log", {"message": f"{stage}: " + "，".join(f"{key} {value}" for key, value in metrics.items())})

    def warning(self, code: str, message: str, *, impact: str = "", action: str = "") -> None:
        if code == "routing-profile" and _UNCALIBRATED_VECTOR_NOTICE in message:
            return
        if code == "srt-line-budget":
            emit("log", {"code": code, "message": message, "impact": impact, "action": action})
            return
        warning_key = (code, message)
        if warning_key in self._seen_warnings:
            return
        self._seen_warnings.add(warning_key)

        # FineSub checks all (task_group x difficulty) combinations for routing presets.
        # For non-audio models, "组内没有成员支持音频..." fires 6 times across difficulties/groups.
        # Deduplicate and compact them so the user only sees one clean warning.
        if code == "routing-preset" and ": " in message:
            _prefix, core = message.split(": ", 1)
            if "组内没有成员支持音频" in core:
                if core in self._seen_preset_cores:
                    return
                self._seen_preset_cores.add(core)
                message = f"多模态组: {core}"

        emit("warning", {"code": code, "message": message, "impact": impact, "action": action})

    def debug(self, message: str, fields=None) -> None:
        values = dict(fields or {})
        if message == _API_CALL_MESSAGE and not values.get("why"):
            return
        emit("log", {"message": message, "fields": values})

    def completed(self, output, elapsed_sec: float) -> None:
        emit("progress", {"completed": 100, "total": 100, "unit": "%", "message": "字幕已完成"})

    def failed(self, stage: str, message: str) -> None:
        emit("failed", {"stage": stage, "message": message})


def artifact(path: Path) -> dict[str, Any]:
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return {"uri": path.resolve().as_uri(), "sha256": digest, "bytes": path.stat().st_size}


def translate_axis(request: Mapping[str, Any], axis: Mapping[str, Any], output: Path, task_id: str, task_artifact_dir: Path) -> AxisTranslation:
    """Local entry to the shared source-text-axis run.

    The run itself lives in `nonoka_x.axis` because the cloud's LLM container
    performs exactly the same one -- keeping a second copy here is how the two
    modes would drift into producing different subtitles from the same axis.
    The media file never left this machine and is still on disk, so an audio or
    video correction reference works here as it does after a real recognition
    run; the windows are simply cut on the user's own timings.
    """

    return translate_rows(
        axis["rows"],
        output_path=output,
        task_id=task_id,
        task_artifact_dir=task_artifact_dir,
        correction=request.get("correction") or {},
        knowledge=request.get("knowledge", "none"),
        source_path=Path(request["source"]["path"]),
        on_notice=lambda notice: emit("log", {"message": notice}),
    )


def install_llm_model_override(request: Mapping[str, Any]) -> None:
    """Apply this run's `llm_model` pin, or clear whatever a previous one left.

    The engine's equivalent of `--llm-model`: a model group or route target
    that replaces the bound chain for one run, without touching the saved
    settings. Upstream installs it from its own CLI entry points, which this
    worker is not one of, so the call has to happen here -- and unconditionally,
    because the overlay is process-global. One task per process makes a leak
    impossible today; installing only when the field is present would make that
    a property of the process model rather than of this function.

    A bare string pins every task group. A table may use either the desktop's
    route names (`{"correction": "..."}`) or upstream's exact task groups;
    the two desktop media routes expand to both their ``-mm`` and ``-text``
    cells before the engine validates the result.
    """

    try:
        from finesub.llm.routing.model_routes import (
            install_runtime_preferred,
            parse_llm_model_args,
        )
    except ImportError:
        # No engine, nothing to override -- the caller fails on its own import.
        return
    value = request.get("llm_model")
    if isinstance(value, Mapping):
        overlay = {str(key): str(item) for key, item in value.items()}
    elif isinstance(value, str) and value.strip():
        overlay = parse_llm_model_args([value.strip()])
    else:
        overlay = {}
    from .settings import TASK_GROUPS_BY_ROUTE

    expanded: dict[str, str] = {}
    for key, target in overlay.items():
        groups = TASK_GROUPS_BY_ROUTE.get(key, (key,))
        for group in groups:
            previous = expanded.get(group)
            if previous is not None and previous != target:
                raise ValueError(
                    f"llm_model assigns conflicting targets to {group}: "
                    f"{previous!r} and {target!r}"
                )
            expanded[group] = target
    install_runtime_preferred(expanded)


def extract_execution_failure_detail(exc: BaseException) -> str:
    """Extract vendor/CLI diagnostic details from agent execution attempts or capsules."""
    attempts = getattr(exc, "_harness_execution_attempts", None) or []
    for attempt in attempts:
        if isinstance(attempt, Mapping):
            vendor_err = attempt.get("vendor_error")
            if vendor_err and isinstance(vendor_err, str) and vendor_err.strip():
                return vendor_err.strip()

    try:
        from finesub.llm.agent.agent_paths import vendor_error_text

        text = vendor_error_text(exc)
        if text and text.strip():
            return text.strip()
    except Exception:
        pass

    current = getattr(exc, "__cause__", None) or getattr(exc, "__context__", None)
    while current is not None:
        sub = extract_execution_failure_detail(current)
        if sub:
            return sub
        current = getattr(current, "__cause__", None) or getattr(current, "__context__", None)

    return ""


def format_worker_exception(exc: BaseException) -> str:
    vendor_detail = extract_execution_failure_detail(exc)
    exc_type = type(exc).__name__
    exc_msg = str(exc).strip()
    if vendor_detail:
        if "inspect events/stderr.log" in exc_msg or "exited with status" in exc_msg:
            return f"{exc_type}: 本地模型 CLI 调用失败: {vendor_detail}"
        return f"{exc_type}: {exc_msg} ({vendor_detail})"
    return f"{exc_type}: {exc_msg}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--task-dir", type=Path, required=True)
    parser.add_argument("--vendor", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        request = json.loads(sys.stdin.readline())
        install_llm_model_override(request)
        source = request["source"]
        correction = request.get("correction") or {}
        title = str(source.get("title") or Path(source["path"]).stem)
        output = args.task_dir / "workspace" / f"{title}.srt"
        output.parent.mkdir(parents=True, exist_ok=True)
        engine_device, visible_devices = _normalize_engine_device(request.get("device"))
        if visible_devices is not None and "CUDA_VISIBLE_DEVICES" not in os.environ:
            os.environ["CUDA_VISIBLE_DEVICES"] = visible_devices
        from finesub.reporting import quieted_libraries, reporting_to

        axis = request.get("axis") if isinstance(request.get("axis"), dict) else None
        artifact_dir = args.task_dir / "workspace" / "llm-artifacts"
        if axis is not None and axis.get("kind") == "ja":
            with reporting_to(NonokaXReporter()), quieted_libraries("normal"):
                translated = translate_axis(request, axis, output, args.task_id, artifact_dir)
            candidates = {
                "stable_json": translated.stable_json,
                "annotated_csv": translated.annotated_csv,
                "final_srt": translated.final_srt,
            }
        else:
            from finesub.stages import run_pipeline

            reporter = NonokaXReporter()
            gpu_tier = resolve_request_gpu_tier(
                request,
                warn=lambda message: reporter.warning("gpu-tier", message),
            )
            with reporting_to(reporter), quieted_libraries("normal"):
                paths = run_pipeline(
                    source["path"],
                    output_path=output,
                    stage=request["target"],
                    language=request.get("language", "ja"),
                    device=engine_device,
                    gpu_tier=gpu_tier,
                    separate=bool(request.get("separate", True)),
                    llm_media=correction.get("media", "audio"),
                    llm_retrieval=correction.get("retrieval", "local"),
                    llm_difficulty=correction.get("difficulty", "quality"),
                    llm_fast=correction.get("fast", "auto"),
                    extra_info=correction.get("extra_info", ""),
                    extra_style=correction.get("extra_style", ""),
                    knowledge=request.get("knowledge", "update"),
                    task_id=args.task_id,
                    task_artifact_dir=artifact_dir,
                    resume=True,
                )
            # Every artifact the pipeline names, not only the four the editor
            # projects from: a caller that asked for one stage wants that
            # stage's output, and a stage that did not run simply leaves its
            # file absent -- the manifest below records what exists.
            candidates = {
                "vocal_audio": paths.resolve_vocal_audio(),
                "vad_json": Path(paths.vad_json),
                "vad_energy_npz": Path(paths.vad_energy_npz),
                "aligned_json": Path(paths.aligned_json),
                "stable_json": Path(paths.stable_json),
                "raw_srt": Path(paths.raw_srt),
                "translated_srt": Path(paths.translated_srt),
                "annotated_csv": Path(paths.final_srt).with_name(f"{Path(paths.final_srt).stem}-annotated.csv"),
                "final_srt": Path(paths.final_srt),
                "metadata_json": Path(paths.metadata_json),
            }
        upstream = json.loads((args.vendor / "UPSTREAM.json").read_text(encoding="utf-8"))
        manifest = {
            "schema": 1,
            "task_id": args.task_id,
            "engine_commit": upstream["commit"],
            "artifacts": {name: artifact(path) for name, path in candidates.items() if path.is_file()},
        }
        emit("completed", {"artifacts": manifest})
        return 0
    except Exception as exc:
        formatted_message = format_worker_exception(exc)
        emit("failed", {"message": formatted_message})
        vendor_detail = extract_execution_failure_detail(exc)
        if os.environ.get("NONOKA_DEBUG") or (not vendor_detail and not type(exc).__name__.startswith("LocalAgent")):
            traceback.print_exc(file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
