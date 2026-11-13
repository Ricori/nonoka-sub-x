"""The task worker. Stdin is one TaskRequest; stdout is NDJSON events.

Only orchestration (`docs/transcription-engines-plan.md`, WP4c): the request's
transcription engine (`transcription.engine`, `finesub` when absent) turns the
source into a `TranscriptionResult`, and, for a target past `stable`, the
request's correction engine (`correction.engine`) takes it from there. Both
are looked up by id in their registries; nothing here knows which engine it
is talking to. A result already in hand -- the cloud's recognition, which a
`cloud_stable` source continues from -- takes the transcription's place.
A source-text axis (`axis.kind == "ja"`) needs no transcription at all and
goes straight to the shared axis run (`nonoka_x.axis`).
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import hashlib
import importlib
import inspect
import json
import logging
import os
import sys
import traceback
from pathlib import Path
from typing import Any, Iterator, Mapping

from .axis import AxisTranslation, translate_axis as translate_rows
from .download_route import install_proxy_route_policy
from .fast_mode import resolve_request_fast
from .gpu_tier import resolve_request_gpu_tier


def emit(event_type: str, payload: Mapping[str, Any] | None = None) -> None:
    print(json.dumps({"type": event_type, "payload": dict(payload or {})}, ensure_ascii=False, separators=(",", ":")), flush=True)


# FineSub 0.5.0 records every LLM call, local agent call and web retrieval as
# one debug line, which is the right shape for the run log it was written for
# -- and the wrong shape for a task log the user is watching, where a single
# correction pass would scroll past hundreds of successes. The failures are
# the part worth surfacing, and they are the ones that carry `why`: the
# endpoint's own words (`llm_runtime`), which is exactly what tells a rate
# limit apart from a spent key apart from a hung stage. Successes stay in the
# run log and the per-call artifacts, where nothing is lost -- except the slow
# ones: a call that took minutes is where a stage's time went, and without it a
# 30-minute preparation task showed no line at all between two timestamps.
_API_CALL_MESSAGE = "llm api call"
_SLOW_API_CALL_SECONDS = 60.0

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


def _is_slow_call(fields: Mapping[str, Any]) -> bool:
    try:
        return float(fields.get("sec") or 0) >= _SLOW_API_CALL_SECONDS
    except (TypeError, ValueError):
        return False


class NonokaXReporter:
    """FineSub's reporter protocol (`finesub.reporting.Reporter`) as worker events.

    One instance hears every half of a task -- the worker binds it around the
    transcription and the correction alike, and both FineSub adapters keep a
    reporter their caller bound -- so a warning either half raises is said
    once. `announce_completion=False` leaves the final progress line to the
    caller: a run in two halves completes twice, and only the task's own end
    is "字幕已完成".
    """

    def __init__(self, *, announce_completion: bool = True) -> None:
        self._seen_warnings: set[tuple[str, str]] = set()
        self._seen_preset_cores: set[str] = set()
        self._announce_completion = announce_completion

    def planned(self, stages) -> None:
        return

    def stage_started(self, stage: str, *, reused: bool = False, detail: str = "") -> None:
        emit("stage", {"stage": stage, "message": "已有结果，跳过" if reused else (detail or "正在处理"), "reused": reused})

    def progress(self, stage: str, *, completed: int, total: int | None = None, unit: str = "", detail: str = "") -> None:
        emit("progress", {"stage": stage, "completed": completed, "total": total, "unit": unit, "message": detail})

    def summary(self, stage: str, metrics) -> None:
        emit("log", {"message": f"{stage}: " + "，".join(f"{key} {value}" for key, value in metrics.items())})

    def warning(self, code: str, message: str, *, impact: str = "", action: str = "") -> None:
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
        if message == _API_CALL_MESSAGE and not values.get("why") and not _is_slow_call(values):
            return
        emit("log", {"message": message, "fields": values})

    def completed(self, output, elapsed_sec: float) -> None:
        if self._announce_completion:
            emit_completion()

    def failed(self, stage: str, message: str) -> None:
        emit("failed", {"stage": stage, "message": message})


def emit_completion() -> None:
    emit("progress", {"completed": 100, "total": 100, "unit": "%", "message": "字幕已完成"})


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
    if knowledge not in ("read", "update"):
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


def prepare_knowledge_subjects(
    request: Mapping[str, Any], task_id: str, *, knowledge_root: str | Path | None = None
) -> dict[str, Any]:
    """Resolve the picked subjects to engine names, creating new ones first.

    Existing subjects travel by store id, which survives a rename; the engine
    is handed `category/key`, the qualified form its human lookup accepts
    without ambiguity. A subject without an id is created -- the provider
    only lets that through under `update` -- unless an entry of that name
    already exists, which is then reused rather than duplicated.

    The store is the knowledge package's (plan 4.6 item 35a): the lookup and
    the creation run there (`finesub_knowledge.manage.prepare_subjects`).
    """

    subjects = knowledge_subjects(request)
    if not subjects:
        return {"names": [], "created": []}

    from finesub_knowledge.client import prepare_subjects

    return prepare_subjects(
        [dict(item) for item in subjects], knowledge=request.get("knowledge"), task_id=task_id,
        knowledge_root=knowledge_root,
    )


def knowledge_package_problem(request: Mapping[str, Any]) -> str | None:
    """Why this request cannot run without the knowledge package, or None.

    A request that reads or writes the base needs it, and so does its ASR
    context, which picks its names from the base (`stages.resolve_asr_context`
    turns it on whenever `knowledge` is not `none`). Refused before anything
    runs, not left to fail later or to run silently without the base (plan
    4.6 item 35a, appendix B question 39).
    """

    if request.get("knowledge", "update") == "none":
        return None
    from finesub_knowledge.client import package_problem

    problem = package_problem()
    if problem is None:
        return None
    return f"{problem}：这个任务用到知识库（knowledge={request.get('knowledge', 'update')}），请安装知识库包或关闭知识库后重试"


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


def diarization_kwargs(request: Mapping[str, Any]) -> dict[str, Any]:
    """`run_pipeline`'s speaker keys for this request.

    The provider has already validated and normalized the block
    (`local_provider.normalize_diarization`), so this only translates it.
    `off` is passed explicitly: since engine 0.7.0 an unset level at the
    `quality` difficulty turns the speaker judge on.
    """

    block = request.get("diarization")
    level = str(block.get("level") or "off") if isinstance(block, Mapping) else "off"
    kwargs: dict[str, Any] = {"diarize": level}
    if level == "off":
        return kwargs
    if block.get("max_speakers") is not None:
        kwargs["diarize_max_speakers"] = int(block["max_speakers"])
    return kwargs


def for_task_mode(request: Mapping[str, Any], artifact_dir: Path) -> dict[str, Any]:
    """The request without what a Chinese-source task cannot use.

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

    recorded = recorded_choice(artifact_dir)
    if not (recorded.correct_only if recorded is not None else not started(artifact_dir)):
        return dict(request)
    return {
        **request,
        "knowledge": "none",
        "correction": {**(request.get("correction") or {}), "extra_style": ""},
    }


def emit_request_notices(request: Mapping[str, Any]) -> None:
    """Say once what the provider adjusted in this request, and why."""

    if request.get("legacy_knowledge") == "collect":
        emit("log", {
            "message": "知识库模式「collect」已更名为「read」：只读取知识库，不再收集反馈；"
                       "需要写回请选择自动更新",
        })
    block = request.get("diarization")
    if diarization_kwargs(request)["diarize"] != "off" and diarization_models_missing():
        emit("log", {
            "message": "首次使用说话人分离：需要下载约 450MB 模型，将在说话人分离阶段自动下载并校验",
        })
    if isinstance(block, Mapping) and block.get("requested_level"):
        emit("warning", {
            "code": "diarize-judge-skipped",
            "message": f"说话人裁判（{block['requested_level']}）只在完整翻译流程中运行，"
                       "本次只做声学说话人分离",
            "impact": "",
            "action": "",
        })


def diarization_models_missing() -> bool:
    """Whether the default diarization backend still has weights to fetch.

    The engine fetches them itself, with its own mirror routing and checks,
    the first time the stage runs -- but that is a silent wait of several
    hundred megabytes, so the task log says so before it starts. Unknown
    counts as present: this only decides whether to warn.
    """

    models_root = os.environ.get("FINESUB_MODEL_DIR")
    if not models_root:
        return False
    try:
        from .model_install import DIARIZATION_MODEL_IDS, missing_managed_models

        return bool(missing_managed_models(Path(models_root), DIARIZATION_MODEL_IDS))
    except Exception:  # noqa: BLE001 - a notice, never a reason to fail the run
        return False


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


# --- orchestration -----------------------------------------------------------

#: The built-in implementations, as `module:factory`, registered by
#: `register_engines` (owner 2026-11-12). A stand-in for the engine host until
#: the engine packages are discovered from `engines/` (WP-P1): an id that is
#: not in its registry is simply not available, the way a removed package
#: will be. Tests replace these to run with other engines, or without one.
#: A factory is handed whichever of `store` (the config facade) and `runtime`
#: (the runtime facade) it takes by name (`build_engines`).
TRANSCRIPTION_ENGINES: tuple[str, ...] = (
    "nonoka_x.transcription.engines.finesub:FineSubEngine",
    "nonoka_x.transcription.engines.mai2:Mai2Engine",
    "nonoka_x.transcription.engines.gemini:GeminiEngine",
    "nonoka_x.transcription.engines.moss_pro:MossProEngine",
)
CORRECTION_ENGINES: tuple[str, ...] = ("nonoka_x.correction.finesub:FineSubCorrection",)

#: Every TaskRequest target, in order; the transcription's reach ends at `stable`.
TARGETS = ("vocal", "aligned", "stable", "raw-srt", "translated-srt", "final-srt")
TRANSCRIPTION_TARGETS = TARGETS[: TARGETS.index("stable") + 1]

#: What a `cloud_stable` source carries, as the `native` attachments of the
#: transcription the cloud's FineSub made (plan 5.1): its stable and, when it
#: diarized, its speaker timeline.
CLOUD_NATIVE = (("stable_json", "finesub.stable_json"), ("diar_json", "finesub.diar_json"))


def _construct(factory: Any, context: Mapping[str, Any]) -> Any:
    """`factory` called with the items of `context` it names as parameters."""

    try:
        parameters = inspect.signature(factory).parameters
    except (TypeError, ValueError):
        return factory()
    if any(parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters.values()):
        return factory(**context)
    return factory(**{name: value for name, value in context.items() if name in parameters})


def build_engines(*, store: Any | None = None, runtime: Any | None = None) -> tuple[list[Any], list[Any]]:
    """One instance of every built-in implementation: (transcription engines, correction engines).

    The sidecar builds its own set for readiness and capabilities; the worker
    registers one (`register_engines`).
    """

    context = {"store": store, "runtime": runtime}
    built: list[list[Any]] = []
    for entries in (TRANSCRIPTION_ENGINES, CORRECTION_ENGINES):
        engines = []
        for entry in entries:
            module_name, _, factory = entry.partition(":")
            engines.append(_construct(getattr(importlib.import_module(module_name), factory), context))
        built.append(engines)
    return built[0], built[1]


def register_engines(*, store: Any | None = None, runtime: Any | None = None) -> None:
    """Register every built-in implementation whose id is not registered yet."""

    from . import correction, transcription

    transcription_engines, correction_engines = build_engines(store=store, runtime=runtime)
    for registry, engines in (
        (transcription.registry, transcription_engines),
        (correction.registry, correction_engines),
    ):
        for engine in engines:
            if engine.spec.id not in registry:
                registry.register(engine)


class _Deferred:
    """What `build()` returns, built the first time anything is asked of it.

    The worker's config and runtime facades: only a cloud engine's run reads
    them, and opening the runtime installs download policies a FineSub run
    must not see.
    """

    def __init__(self, build: Any) -> None:
        self._deferred_build = build
        self._deferred_value: Any = None

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_deferred_"):
            raise AttributeError(name)
        if self._deferred_value is None:
            self._deferred_value = self._deferred_build()
        return getattr(self._deferred_value, name)


def engine_context(
    data_dir: Path | None, install_dir: Path | None, vendor: Path
) -> dict[str, Any]:
    """The `store` and `runtime` the built-in engines are built with in this worker (WP5)."""

    if data_dir is None:
        return {"store": None, "runtime": None}

    def store() -> Any:
        from .config import ConfigStore

        return ConfigStore.open(data_dir)

    def runtime() -> Any:
        from .runtime import RuntimeManager

        return RuntimeManager.open(data_dir, vendor, install_dir)

    return {"store": _Deferred(store), "runtime": _Deferred(runtime)}


def transcription_in_hand(request: Mapping[str, Any], task_dir: Path) -> Any | None:
    """The transcription a continuation already has, or None when it is still to be made.

    A `cloud_stable` source is the cloud's recognition of this media. Its
    files are attachments the core does not parse; the correction decides
    what to do with them. There is no `transcript.json` for it yet (the cloud
    delivers one after WP-CL), so the result names the path one would have.
    """

    from .transcription import TRANSCRIPT_FILENAME, TranscriptionResult

    source = request["source"]
    if source.get("kind") != "cloud_stable":
        return None
    return TranscriptionResult(
        transcript_json=task_dir / TRANSCRIPT_FILENAME,
        native={key: Path(source[field]) for field, key in CLOUD_NATIVE if source.get(field)},
        metadata={"source": "cloud_stable"},
    )


def transcription_job(
    request: Mapping[str, Any], task_dir: Path, task_id: str, *, title: str, gpu_tier: str
) -> Any:
    """The transcription engine's job: the request's fields for that half, resolved.

    `options` carry them in TaskRequest terms (WP4a, owner 2026-11-11): the
    device as requested, the GPU tier resolved, the speaker block as the
    provider normalized it, the extra-info with the picked knowledge subjects
    written in, the effective knowledge switch. `transcription.options` (the
    engine's own, e.g. a cloud engine's `split_length_scale`) lies
    underneath, so a resolved field always wins.
    """

    from .transcription import TranscriptionJob, requested_options

    source = request["source"]
    return TranscriptionJob(
        source=Path(source["path"]),
        language=request.get("language", "ja"),
        task_dir=task_dir,
        cache_dir=task_dir / "cache",
        target=request["target"],
        options={
            **requested_options(request),
            "title": title,
            "device": request.get("device"),
            "gpu_tier": gpu_tier,
            "separate": bool(request.get("separate", True)),
            "diarization": request.get("diarization"),
            "extra_info": str((request.get("correction") or {}).get("extra_info") or ""),
            "knowledge": request.get("knowledge", "update"),
            "task_id": task_id,
        },
    )


def correction_job(
    request: Mapping[str, Any],
    task_dir: Path,
    task_id: str,
    transcription: Any,
    *,
    title: str,
    fast: str,
    proofread: str,
    subject_names: list[str],
) -> Any:
    """The correction engine's job (WP4b): the correction block with `fast` and `proofread` resolved."""

    from .correction import CorrectionJob

    return CorrectionJob(
        transcription=transcription,
        source=Path(request["source"]["path"]),
        target=request["target"],
        task_dir=task_dir,
        options={**(request.get("correction") or {}), "fast": fast, "proofread": proofread},
        knowledge={"knowledge": request.get("knowledge", "update"), "knowledge_subjects": list(subject_names)},
        diarization_level=diarization_kwargs(request)["diarize"],
        title=title,
        task_id=task_id,
        task_summary=knowledge_task_summary(request, title),
    )


@contextlib.contextmanager
def engine_reporting(reporter: NonokaXReporter) -> Iterator[None]:
    """Bind `reporter` as FineSub's reporter for the engines run inside, when FineSub is there to hear it.

    The FineSub adapters keep a reporter their caller bound; any other engine
    reports through the `Reporter` it is handed.
    """

    try:
        from finesub.reporting import quieted_libraries, reporting_to
    except ImportError:
        yield
        return
    with reporting_to(reporter), quieted_libraries("normal"):
        yield


def manifest_candidates(transcription: Any, correction: Any | None) -> dict[str, Path]:
    """The task's artifacts by manifest name: the transcription's attachments, then the correction's.

    An attachment `<engine>.<name>` is listed as `<name>`; where the
    correction names the same artifact, its file is the one the task's
    subtitles were made from (a continuation's copy of the cloud stable).
    """

    candidates = {key.split(".", 1)[1]: Path(path) for key, path in transcription.native.items()}
    if correction is not None:
        candidates.update(correction.artifacts)
    return candidates


def run_engines(
    request: Mapping[str, Any],
    task_dir: Path,
    task_id: str,
    *,
    title: str,
    subject_names: list[str],
) -> dict[str, Path]:
    """Transcribe (or take the transcription in hand), then correct up to the target."""

    from . import correction as corrections
    from . import transcription as transcriptions
    from .settings import correction_proofread

    register_engines()
    target = request["target"]
    if target not in TARGETS:
        raise ValueError(f"unknown target {target!r}; known: {', '.join(TARGETS)}")
    in_hand = transcription_in_hand(request, task_dir)
    if in_hand is not None:
        emit("log", {"message": "从云端识别结果续跑：跳过人声分离与识别"})
    reporter = NonokaXReporter(announce_completion=False)
    report = transcriptions.EventReporter(emit)
    gpu_tier = resolve_request_gpu_tier(request, warn=lambda message: reporter.warning("gpu-tier", message))
    correction_block = request.get("correction") or {}
    fast = resolve_request_fast(correction_block, warn=lambda message: reporter.warning("fast-mode", message))
    proofread = correction_proofread(correction_block)

    # Both looked up before anything runs: an engine that is not there fails
    # the task now, not after the other half has run.
    engine = None if in_hand is not None else transcriptions.registry.get(transcriptions.requested_engine(request))
    corrector = None
    if target in corrections.CORRECTION_TARGETS:
        corrector = corrections.registry.get(corrections.requested_engine(request))

    with engine_reporting(reporter):
        pending = None
        if corrector is not None:
            placeholder = transcriptions.TranscriptionResult(
                transcript_json=task_dir / transcriptions.TRANSCRIPT_FILENAME
            )
            pending = correction_job(
                request, task_dir, task_id, placeholder,
                title=title, fast=fast, proofread=proofread, subject_names=subject_names,
            )
            # What the correction can refuse is refused before the
            # transcription spends anything (an engine without the hook has
            # nothing to refuse early).
            check = getattr(corrector, "check", None)
            if callable(check):
                check(pending, report)
        if in_hand is None:
            job = transcription_job(request, task_dir, task_id, title=title, gpu_tier=gpu_tier)
            result = engine.transcribe(job, report)
        else:
            # The stages up to the result in hand are done, and said to be,
            # as a run resuming from them says it.
            for stage in TRANSCRIPTION_TARGETS:
                reporter.stage_started(stage, reused=True)
            result = in_hand
        corrected = None
        if corrector is not None:
            corrected = corrector.run(dataclasses.replace(pending, transcription=result), report)
    emit_completion()
    return manifest_candidates(result, corrected)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--task-dir", type=Path, required=True)
    parser.add_argument("--vendor", type=Path, required=True)
    # Where the engines read keys and find the managed runtime (WP5); a worker
    # started without them runs only the engines that need neither.
    parser.add_argument("--data-dir", type=Path, default=None)
    parser.add_argument("--install-dir", type=Path, default=None)
    args = parser.parse_args(argv)
    try:
        request = json.loads(sys.stdin.readline())
        artifact_dir = args.task_dir / "workspace" / "llm-artifacts"
        request = for_task_mode(request, artifact_dir)
        install_proxy_route_policy()
        install_llm_model_override(request)
        emit_request_notices(request)
        source = request["source"]
        correction = dict(request.get("correction") or {})
        correction["extra_info"] = compose_knowledge_context(
            request, str(correction.get("extra_info") or "")
        )
        request["correction"] = correction
        title = str(source.get("title") or Path(source["path"]).stem)
        output = args.task_dir / "workspace" / f"{title}.srt"
        output.parent.mkdir(parents=True, exist_ok=True)

        axis = request.get("axis") if isinstance(request.get("axis"), dict) else None
        problem = knowledge_package_problem(request)
        if problem:
            raise RuntimeError(problem)
        # Before the transcription: the ASR context may pick the entries of a
        # subject created here.
        subjects = prepare_knowledge_subjects(request, args.task_id)
        for entry in subjects["created"]:
            emit("log", {
                "message": "knowledge subject initialized",
                "fields": {"entry": entry, "rev": subjects["rev"]},
            })
        if axis is not None and axis.get("kind") == "ja":
            with engine_reporting(NonokaXReporter()):
                translated = translate_axis(
                    request, axis, output, args.task_id, artifact_dir, subjects["names"]
                )
            candidates = {
                "stable_json": translated.stable_json,
                "annotated_csv": translated.annotated_csv,
                "final_srt": translated.final_srt,
            }
        else:
            register_engines(**engine_context(args.data_dir, args.install_dir, args.vendor))
            candidates = run_engines(
                request, args.task_dir, args.task_id, title=title, subject_names=subjects["names"]
            )
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
