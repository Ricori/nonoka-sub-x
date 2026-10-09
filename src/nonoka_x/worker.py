"""Isolated FineSub worker. Stdin is one TaskRequest; stdout is NDJSON events."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import sys
import traceback
from pathlib import Path
from typing import Any, Mapping

from .axis import AxisTranslation, translate_axis as translate_rows
from .fast_mode import resolve_request_fast
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


_KIND_LABELS = {
    "streamer": "主播 / 频道",
    "work": "作品 / 节目",
    "topic": "其他知识主题",
}


def knowledge_subjects(request: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """The subjects the user picked, as the provider normalized them.

    Empty whenever the base is not read at all. A request queued before the
    list existed carries one `knowledge_context`, which only ever applied
    under `update`.
    """

    knowledge = request.get("knowledge")
    if knowledge not in ("collect", "update"):
        return []
    subjects = request.get("knowledge_subjects")
    if subjects is None and knowledge == "update":
        legacy = request.get("knowledge_context")
        subjects = [legacy] if isinstance(legacy, Mapping) else []
    return [
        item
        for item in (subjects or [])
        if isinstance(item, Mapping) and str(item.get("subject") or "").strip()
    ]


def compose_knowledge_context(request: Mapping[str, Any], extra_info: str = "") -> str:
    """Turn the desktop's structured subject fields into engine prompt context.

    FineSub's correction/research prompts collect the structured feedback that
    later becomes knowledge proposals. Feeding the identities through their
    existing ``extra_info`` inlet lets every route see them -- and, because the
    names are written out, research round 1's keyword pre-injection finds
    their entries too -- without a second, desktop-only knowledge
    implementation. The entries themselves reach every window through the
    engine's pinned-subject block (`knowledge_subjects=`).
    """

    subjects = knowledge_subjects(request)
    if not subjects:
        return str(extra_info or "")
    lines = ["【本任务指定的知识主体（用户明确选择）】"]
    for item in subjects:
        kind_label = _KIND_LABELS.get(str(item.get("kind") or ""), _KIND_LABELS["topic"])
        line = f"- {kind_label}：{str(item.get('subject') or '').strip()}"
        aliases = str(item.get("aliases") or "").strip()
        description = " ".join(str(item.get("description") or "").split())
        if aliases:
            line += f"（别名 / 常用译名：{aliases}）"
        if description:
            line += f"；{description}"
        lines.append(line)
    if request.get("knowledge") == "update":
        lines.append(
            "请将本次确认的新名称、关系和事实归入上述主体中最贴切的一个；"
            "不属于任何一个时再按常规新建条目，不要与同名主体混淆。"
        )
    original = str(extra_info or "").strip()
    return "\n".join(lines) + (f"\n\n【其他背景信息】\n{original}" if original else "")


def knowledge_task_summary(request: Mapping[str, Any], title: str) -> str:
    """Task summary visible to the post-correction knowledge updater."""

    identity = compose_knowledge_context(request)
    return f"字幕任务：{title}" + (f"\n\n{identity}" if identity else "")


def _split_aliases(value: Any, subject: str) -> list[str]:
    aliases: list[str] = []
    for alias in re.split(r"[、,，;；\n]+", str(value or "")):
        alias = alias.strip()
        if alias and alias != subject and alias not in aliases:
            aliases.append(alias)
    return aliases


def _create_knowledge_subject(repo: Any, item: Mapping[str, Any], task_id: str) -> Any:
    """Create one user-described subject; returns its resolved entry.

    The subject fields are explicit user input, not a guess extracted from a
    transcript. Persisting that small scaffold makes an empty repository
    usable immediately; the model-owned update still decides which video
    facts are reliable enough to append.
    """

    from finesub.llm.knowledge.node.proposals import apply_model_proposals

    subject = str(item.get("subject") or "").strip()
    kind = str(item.get("kind") or "streamer")
    category = "streamer" if kind == "streamer" else "common"
    kind_label = _KIND_LABELS.get(kind, _KIND_LABELS["topic"])
    description = " ".join(str(item.get("description") or "").split())
    intro = (description or f"用户在字幕任务中指定的{kind_label}知识主体。")[:240]
    proposal = {
        "op": "create_entry",
        "category": category,
        "entry": subject,
        "intro": intro,
        "entry_type": "其他" if category == "common" else "",
        "aliases": _split_aliases(item.get("aliases"), subject),
        "reason": "用户在任务设置中明确指定此主体；初始化词条以承接本次及后续任务知识。",
    }
    proposal_text = (
        "<knowledge_proposals>\n"
        + json.dumps(proposal, ensure_ascii=False, separators=(",", ":"))
        + "\n</knowledge_proposals>"
    )
    report = apply_model_proposals(
        proposal_text,
        repo=repo,
        task_id=f"{task_id}:subject-bootstrap",
        knowledge_read_rev=repo.rev,
    )
    # A concurrent task may have created it after our first resolve; either
    # way the entry must exist now.
    resolved = repo.resolve(subject)
    if resolved is None:
        reasons = "; ".join(record.reason for record in report.skipped) or "unknown reason"
        raise RuntimeError(f"知识主体「{subject}」初始化失败：{reasons}")
    return resolved


def prepare_knowledge_subjects(
    request: Mapping[str, Any], task_id: str, *, knowledge_root: str | Path | None = None
) -> dict[str, Any]:
    """Resolve the picked subjects to engine names, creating new ones first.

    Existing subjects travel by store id, which survives a rename; the engine
    is handed `category/key`, the qualified form its human lookup accepts
    without ambiguity. A subject without an id is created here -- the provider
    only lets that through under `update` -- unless an entry of that name
    already exists, which is then reused rather than duplicated.
    """

    subjects = knowledge_subjects(request)
    if not subjects:
        return {"names": [], "created": []}

    from finesub.llm.knowledge.base import knowledge_root_path
    from finesub.llm.knowledge.node.repo import KnowledgeRepo

    repo = KnowledgeRepo.open(knowledge_root_path(knowledge_root))
    names: list[str] = []
    created: list[str] = []
    for item in subjects:
        subject = str(item.get("subject") or "").strip()
        ident = str(item.get("id") or "").strip()
        if ident:
            node = next((s for s in repo.subjects() if s.local_id == ident), None)
            if node is None:
                raise RuntimeError(f"知识主体「{subject}」已不在知识库中，请重新选择")
            category, key = str(node.payload["category"]), str(node.payload["surface"])
        else:
            resolved = repo.resolve(subject)
            if resolved is None:
                if request.get("knowledge") != "update":
                    raise RuntimeError(f"知识库里没有「{subject}」，只有自动更新模式可以新建主体")
                resolved = _create_knowledge_subject(repo, item, task_id)
                created.append(resolved.key)
            category, key = resolved.category, resolved.key
        name = f"{category}/{key}"
        if name not in names:
            names.append(name)
    return {"names": names, "created": created, "rev": repo.rev}


def translate_axis(
    request: Mapping[str, Any],
    axis: Mapping[str, Any],
    output: Path,
    task_id: str,
    task_artifact_dir: Path,
    subject_names: list[str] | None = None,
) -> AxisTranslation:
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
        task_summary=knowledge_task_summary(request, str(request["source"].get("title") or output.stem)),
        correction=request.get("correction") or {},
        knowledge=request.get("knowledge", "none"),
        source_path=Path(request["source"]["path"]),
        knowledge_subjects=subject_names or None,
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
    if (request.get("correction") or {}).get("retrieval") == "native":
        from finesub.llm.routing.model_routes import default_model_routes
        from .settings import native_search_target

        routes = default_model_routes()
        difficulty = (request.get("correction") or {}).get("difficulty") or "quality"
        # Saved CLI routes pin the ordinary target. Entitle the same model's
        # search tool only for native correction/research calls in this run.
        # Explicit multi-model groups retain their declared routing semantics.
        for group_id in ("correction-mm", "correction-text", "research"):
            group, _cell = routes.resolve_binding(routes.active_preset_id, group_id, difficulty)
            if len(group.target_ids) != 1:
                continue
            target_id = group.target_ids[0]
            native_id = native_search_target(routes, target_id)
            if native_id and native_id != target_id:
                expanded[group_id] = native_id
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
        correction = dict(request.get("correction") or {})
        correction["extra_info"] = compose_knowledge_context(
            request, str(correction.get("extra_info") or "")
        )
        request["correction"] = correction
        title = str(source.get("title") or Path(source["path"]).stem)
        output = args.task_dir / "workspace" / f"{title}.srt"
        output.parent.mkdir(parents=True, exist_ok=True)
        engine_device, visible_devices = _normalize_engine_device(request.get("device"))
        if visible_devices is not None and "CUDA_VISIBLE_DEVICES" not in os.environ:
            os.environ["CUDA_VISIBLE_DEVICES"] = visible_devices
        from finesub.reporting import quieted_libraries, reporting_to

        axis = request.get("axis") if isinstance(request.get("axis"), dict) else None
        artifact_dir = args.task_dir / "workspace" / "llm-artifacts"
        subjects = prepare_knowledge_subjects(request, args.task_id)
        for entry in subjects["created"]:
            emit("log", {
                "message": "knowledge subject initialized",
                "fields": {"entry": entry, "rev": subjects["rev"]},
            })
        # Passed only when there is something to pin, so a run without
        # subjects calls the engine with exactly upstream's arguments.
        subject_kwargs = {"knowledge_subjects": subjects["names"]} if subjects["names"] else {}
        if axis is not None and axis.get("kind") == "ja":
            with reporting_to(NonokaXReporter()), quieted_libraries("normal"):
                translated = translate_axis(
                    request, axis, output, args.task_id, artifact_dir, subjects["names"]
                )
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
                    llm_fast=resolve_request_fast(
                        correction, warn=lambda message: reporter.warning("fast-mode", message)
                    ),
                    extra_info=correction.get("extra_info", ""),
                    extra_style=correction.get("extra_style", ""),
                    knowledge=request.get("knowledge", "update"),
                    task_summary=knowledge_task_summary(request, title),
                    task_id=args.task_id,
                    task_artifact_dir=artifact_dir,
                    resume=True,
                    **subject_kwargs,
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
