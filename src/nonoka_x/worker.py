"""The task worker. Stdin is one TaskRequest; stdout is NDJSON events.

Only orchestration (`docs/transcription-engines-plan.md`, WP4c, WP-P1): the
request's transcription engine (`transcription.engine`, `finesub` when absent)
turns the source into a `TranscriptionResult`, and, for a target past
`stable`, the request's correction engine (`correction.engine`) takes it from
there. Both are looked up by id in their registries; nothing here knows which
engine it is talking to. A result already in hand -- the cloud's recognition,
which a `cloud_transcript` source continues from -- takes the transcription's
place. A source-text axis (`axis.kind == "ja"`) needs no transcription at all
and goes straight to the correction engine's axis run.

The engines an engine package provides run in the package's own processes
(`nonoka_x.engine_host`, plan 4.6 item 33): the worker starts them, hands each
its launch context (`nonoka_x.engine_contexts`), and hears what they report
through the one reporter that hears every half of the task. A request that
reads or writes the knowledge base gets one knowledge process for the task,
started here, which the engine processes reach through their contexts. The
cloud API engines are the core's own and run in this process.
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import importlib
import inspect
import json
import os
import shutil
import sys
import traceback
from pathlib import Path
from typing import Any, Mapping

from . import engine_host
from .download_route import install_app_proxy_policy
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

def _is_slow_call(fields: Mapping[str, Any]) -> bool:
    try:
        return float(fields.get("sec") or 0) >= _SLOW_API_CALL_SECONDS
    except (TypeError, ValueError):
        return False


class NonokaXReporter:
    """An engine's reporter protocol (FineSub's `Reporter`) as worker events.

    One instance hears every half of a task -- the engine packages' processes
    make their reporter's calls on it through the engine host -- so a warning
    either half raises is said once. `announce_completion=False` leaves the final progress line to the
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


def knowledge_package_problem(request: Mapping[str, Any], packages: Mapping[str, Any]) -> str | None:
    """Why this request cannot run without the knowledge package, or None.

    A request that reads or writes the base needs it, and so does its ASR
    context, which picks its names from the base (on whenever `knowledge` is
    not `none`). Refused before anything runs, not left to fail later or to
    run silently without the base (plan 4.6 item 35a, appendix B question 39).
    """

    if request.get("knowledge", "update") == "none":
        return None
    if engine_host.knowledge_package(packages) is not None:
        return None
    return (
        f"知识库包未安装：这个任务用到知识库（knowledge={request.get('knowledge', 'update')}），"
        "请安装知识库包或关闭知识库后重试"
    )


def needs_knowledge_process(request: Mapping[str, Any], corrector: Any | None) -> bool:
    """Whether this task's engine processes may reach the knowledge base.

    One that reads or writes it, or picks its ASR context from it; and every
    correction a package runs, which reads its style catalog from it whatever
    the `knowledge` switch says (`--style auto`).
    """

    from .correction import CORRECTION_TARGETS

    if request.get("knowledge", "update") != "none":
        return True
    if getattr(corrector, "package", None) is None:
        return False
    axis = request.get("axis")
    return request.get("target") in CORRECTION_TARGETS or (isinstance(axis, Mapping) and axis.get("kind") == "ja")


def subject_request(request: Mapping[str, Any], task_id: str) -> dict[str, Any] | None:
    """What the knowledge package is asked to prepare for this task, or None when it picked no subject.

    Existing subjects travel by store id, which survives a rename; the engine
    is handed `category/key`, the qualified form its human lookup accepts
    without ambiguity. A subject without an id is created there -- the
    provider only lets that through under `update` -- unless an entry of that
    name already exists, which is then reused rather than duplicated
    (`finesub_knowledge.manage.prepare_subjects`, in the knowledge package).
    """

    subjects = knowledge_subjects(request)
    if not subjects:
        return None
    return {
        "subjects": [dict(item) for item in subjects],
        "knowledge": request.get("knowledge"),
        "task_id": task_id,
        "knowledge_root": None,
    }


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


def for_task_mode(request: Mapping[str, Any], correction: Any | None, record_dir: Path) -> dict[str, Any]:
    """The request without what a Chinese-source task cannot use, as the correction engine says.

    Since engine 0.7.0 such a task only corrects: no knowledge base, no extra
    style. Whether a task already began translating (and goes on doing so) is
    in the correction's own records, so the correction engine decides
    (`task_mode`). Only a source declared Chinese is asked about, and never a
    Japanese axis, which has its source text and is always translated. With no
    correction engine there is no record of a correction either: such a task
    only corrects.
    """

    axis = request.get("axis")
    if request.get("language") != "zh" or (isinstance(axis, Mapping) and axis.get("kind") == "ja"):
        return dict(request)
    if correction is None:
        return {
            **request,
            "knowledge": "none",
            "correction": {**(request.get("correction") or {}), "extra_style": ""},
        }
    return dict(correction.call("task_mode", {"request": dict(request), "record_dir": str(record_dir)}))


def emit_request_notices(request: Mapping[str, Any], models_dir: str | Path | None = None) -> None:
    """Say once what the provider adjusted in this request, and why."""

    if request.get("legacy_knowledge") == "collect":
        emit("log", {
            "message": "知识库模式「collect」已更名为「read」：只读取知识库，不再收集反馈；"
                       "需要写回请选择自动更新",
        })
    block = request.get("diarization")
    if diarization_kwargs(request)["diarize"] != "off" and diarization_models_missing(models_dir):
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


def diarization_models_missing(models_dir: str | Path | None) -> bool:
    """Whether the default diarization backend still has weights to fetch.

    The engine fetches them itself, with its own mirror routing and checks,
    the first time the stage runs -- but that is a silent wait of several
    hundred megabytes, so the task log says so before it starts. Unknown
    counts as present: this only decides whether to warn.
    """

    if not models_dir:
        return False
    models_root = str(models_dir)
    try:
        from .model_install import DIARIZATION_MODEL_IDS, missing_managed_models

        return bool(missing_managed_models(Path(models_root), DIARIZATION_MODEL_IDS))
    except Exception:  # noqa: BLE001 - a notice, never a reason to fail the run
        return False


# --- orchestration -----------------------------------------------------------

#: The core's own implementations, as `module:factory`, registered by
#: `register_engines` after the engine packages' engines (owner 2026-11-12):
#: an id that is not in its registry is simply not available, the way a
#: removed package is. Tests replace these to run with other engines. A
#: factory is handed whichever of `store` (the config facade) and `runtime`
#: (the runtime facade) it takes by name (`build_engines`).
TRANSCRIPTION_ENGINES: tuple[str, ...] = (
    "nonoka_x.transcription.engines.mai2:Mai2Engine",
    "nonoka_x.transcription.engines.gemini:GeminiEngine",
    "nonoka_x.transcription.engines.moss_pro:MossProEngine",
)
CORRECTION_ENGINES: tuple[str, ...] = ()

#: Every TaskRequest target, in order; the transcription's reach ends at `stable`.
TARGETS = ("vocal", "aligned", "stable", "raw-srt", "translated-srt", "final-srt")
TRANSCRIPTION_TARGETS = TARGETS[: TARGETS.index("stable") + 1]

#: The sources a task runs from (`local_provider.SOURCE_KINDS`).
SOURCE_KINDS = ("local_file", "cloud_transcript")

#: Where an engine package keeps its files of one task (plan 4.6 item 34).
NATIVE_DIRECTORY = "native"
#: Nonoka's own formats in a task's manifest (WP-L): what the projection reads
#: and the cloud library syncs, beside the subtitles.
TRANSCRIPT_ARTIFACT = "transcript_json"
CORRECTION_ARTIFACT = "correction_json"
#: The correction's file in the task directory, beside `transcript.json`.
CORRECTION_FILENAME = "correction.json"
#: The manifest field that says why a finished run has nothing to project: the
#: correction's products could not be lined up into its `correction.json`. The
#: provider fails the projection with it, as it failed lining them up itself
#: before WP-L; a run asked for no document is unaffected.
PROJECTION_PROBLEM = "projection_error"


def _construct(factory: Any, context: Mapping[str, Any]) -> Any:
    """`factory` called with the items of `context` it names as parameters."""

    try:
        parameters = inspect.signature(factory).parameters
    except (TypeError, ValueError):
        return factory()
    if any(parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters.values()):
        return factory(**context)
    return factory(**{name: value for name, value in context.items() if name in parameters})


def build_engines(
    *,
    store: Any | None = None,
    runtime: Any | None = None,
    packages: Mapping[str, Any] | None = None,
    session: Any | None = None,
) -> tuple[list[Any], list[Any]]:
    """One instance of every engine: (transcription engines, correction engines).

    The engine packages' first (`packages`, the ones found under the engines
    root when not given), then the core's own (`TRANSCRIPTION_ENGINES`,
    `CORRECTION_ENGINES`). The sidecar builds its own set for readiness and
    capabilities; the worker registers one (`register_engines`), with the
    task's `session`.
    """

    found = engine_host.discover() if packages is None else packages
    transcription: list[Any] = [
        engine_host.PackageTranscriptionEngine(package, session, runtime=runtime)
        for package in engine_host.of_kind(found, "transcription")
    ]
    correction: list[Any] = [
        engine_host.PackageCorrectionEngine(package, session, store=store, runtime=runtime)
        for package in engine_host.of_kind(found, "correction")
    ]
    context = {"store": store, "runtime": runtime}
    for entries, engines in ((TRANSCRIPTION_ENGINES, transcription), (CORRECTION_ENGINES, correction)):
        for entry in entries:
            module_name, _, factory = entry.partition(":")
            engines.append(_construct(getattr(importlib.import_module(module_name), factory), context))
    return transcription, correction


def register_engines(
    *,
    store: Any | None = None,
    runtime: Any | None = None,
    packages: Mapping[str, Any] | None = None,
    session: Any | None = None,
) -> None:
    """Register every engine whose id is not registered yet."""

    from . import correction, transcription

    transcription_engines, correction_engines = build_engines(
        store=store, runtime=runtime, packages=packages, session=session
    )
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
    them, and opening the runtime installs download policies a run that does
    not need them must not see.
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


def engine_context(data_dir: Path | None, install_dir: Path | None, engines: Path) -> dict[str, Any]:
    """The `store` and `runtime` the core's engines are built with in this worker (WP5)."""

    if data_dir is None:
        return {"store": None, "runtime": None}

    def store() -> Any:
        from .config import ConfigStore

        return ConfigStore.open(data_dir)

    def runtime() -> Any:
        from .runtime import RuntimeManager

        return RuntimeManager.open(data_dir, engines, install_dir)

    return {"store": _Deferred(store), "runtime": _Deferred(runtime)}


def launch_contexts(packages: Mapping[str, Any], data_dir: Path | None, install_dir: Path | None, engines: Path) -> Any:
    """The launch contexts of this task's package processes (`nonoka_x.engine_contexts`), from the data directory."""

    from . import engine_contexts
    from .provision import RuntimeProvisioner
    from .runtime.paths import NonokaPaths
    from .settings import FineSubSettings

    if data_dir is None:
        return engine_contexts.LaunchContexts()
    provisioner = RuntimeProvisioner(data_dir, engines, install_dir)
    paths = getattr(provisioner, "paths", None) or NonokaPaths.for_roots(data_dir, install_dir)
    return engine_contexts.build(
        packages,
        paths=paths,
        settings=FineSubSettings(data_dir),
        provisioner=provisioner,
        proxy=engine_contexts.proxy_context(),
    )


def transcription_in_hand(request: Mapping[str, Any], task_dir: Path) -> Any | None:
    """The transcription a continuation already has, or None when it is still to be made.

    A `cloud_transcript` source is the cloud's recognition of this media, in
    the shape an engine hands its result over in (plan 4.7 item 36): its
    `transcript.json` becomes the task's, and its attachments -- by `native`
    key, never parsed here -- become the private attachments of the package
    that made them, in that package's directory of the task (plan 4.6 item
    34), each under its key. The correction decides what to do with them.
    Both are copied, so the task holds its whole input wherever the desktop
    kept the download.
    """

    from .transcription import TRANSCRIPT_FILENAME, TranscriptionResult

    source = request["source"]
    kind = source.get("kind", "local_file")
    if kind not in SOURCE_KINDS:
        # A stored request the provider no longer accepts (a retry runs it
        # unvalidated): refused, never read as media to transcribe afresh.
        raise RuntimeError(f"任务来源 {kind} 已不再支持：请重新提交这个任务")
    if kind != "cloud_transcript":
        return None
    transcript = task_dir / TRANSCRIPT_FILENAME
    task_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source["transcript_json"], transcript)
    native: dict[str, Path] = {}
    attachments = source.get("native") or {}
    if attachments:
        directory = task_dir / NATIVE_DIRECTORY / str(source["native_package"])
        directory.mkdir(parents=True, exist_ok=True)
        for key, path in sorted(attachments.items()):
            native[key] = directory / key
            shutil.copyfile(path, native[key])
    return TranscriptionResult(transcript_json=transcript, native=native, metadata={"source": "cloud_transcript"})


def native_dir(task_dir: Path, engine: Any | None) -> Path:
    """The directory of `task_dir` an engine package keeps its files of the task in (plan 4.6 item 34)."""

    package = getattr(engine, "package", None)
    return task_dir / NATIVE_DIRECTORY / (package.id if package is not None else "")


def record_dir(task_dir: Path, corrector: Any | None) -> Path:
    """The correction's own records of the task (`llm-artifacts`), in its package's directory."""

    return native_dir(task_dir, corrector) / "llm-artifacts"


def transcription_job(
    request: Mapping[str, Any],
    task_dir: Path,
    task_id: str,
    *,
    title: str,
    gpu_tier: str,
    engine: Any | None = None,
    corrector: Any | None = None,
) -> Any:
    """The transcription engine's job: the request's fields for that half, resolved.

    `options` carry them in TaskRequest terms (WP4a, owner 2026-11-11): the
    device as requested, the GPU tier resolved, the speaker block as the
    provider normalized it, the extra-info with the picked knowledge subjects
    written in, the effective knowledge switch, the task's LLM model pin (the
    preparation of a run reads the routes). A package engine also gets its own
    directory in the task and the correction's record directory.
    `transcription.options` (the engine's own, e.g. a cloud engine's
    `split_length_scale`) lies underneath, so a resolved field always wins.
    """

    from .settings import TASK_GROUPS_BY_ROUTE
    from .transcription import TranscriptionJob, requested_options

    source = request["source"]
    options: dict[str, Any] = {
        **requested_options(request),
        "title": title,
        "device": request.get("device"),
        "gpu_tier": gpu_tier,
        "separate": bool(request.get("separate", True)),
        "diarization": request.get("diarization"),
        "extra_info": str((request.get("correction") or {}).get("extra_info") or ""),
        "knowledge": request.get("knowledge", "update"),
        "task_id": task_id,
    }
    if getattr(engine, "package", None) is not None:
        options.update({
            "native_dir": str(native_dir(task_dir, engine)),
            "record_dir": str(record_dir(task_dir, corrector) if corrector is not None else native_dir(task_dir, engine) / "llm-artifacts"),
        })
        if not getattr(corrector, "started", False):
            # The task's LLM model pin is applied once per task, in the first
            # engine process of it, as the worker applied it once in its own:
            # here, unless the correction's process already did (`begin`).
            options.update({
                "llm_model": request.get("llm_model"),
                "task_groups": {route: list(groups) for route, groups in TASK_GROUPS_BY_ROUTE.items()},
                "install_llm_model": True,
            })
    return TranscriptionJob(
        source=Path(source["path"]),
        language=request.get("language", "ja"),
        task_dir=task_dir,
        cache_dir=task_dir / "cache",
        target=request["target"],
        options=options,
    )


def correction_job(
    request: Mapping[str, Any],
    task_dir: Path,
    task_id: str,
    transcription: Any,
    *,
    title: str,
    fast: str,
    subject_names: list[str],
    corrector: Any | None = None,
) -> Any:
    """The correction engine's job (WP4b): the correction block with `fast` resolved.

    Whether to proofread is the correction engine's to resolve: it depends on
    its own route table. A package engine also gets its own directory in the task.
    """

    from .correction import CorrectionJob

    options: dict[str, Any] = {**(request.get("correction") or {}), "fast": fast}
    if getattr(corrector, "package", None) is not None:
        options["native_dir"] = str(native_dir(task_dir, corrector))
    return CorrectionJob(
        transcription=transcription,
        source=Path(request["source"]["path"]),
        target=request["target"],
        task_dir=task_dir,
        options=options,
        knowledge={"knowledge": request.get("knowledge", "update"), "knowledge_subjects": list(subject_names)},
        diarization_level=diarization_kwargs(request)["diarize"],
        title=title,
        task_id=task_id,
        task_summary=knowledge_task_summary(request, title),
    )


def manifest_candidates(transcription: Any, correction: Any | None) -> dict[str, Path]:
    """The task's artifacts by manifest name: the transcription's attachments, then the correction's.

    An attachment `<engine>.<name>` is listed as `<name>`; where the
    correction names the same artifact, its file is the one the task's
    subtitles were made from (its own copy of a FineSub stable). Nonoka's own
    formats are listed under their names too: `transcript_json` (when the
    transcription wrote one) and the correction's `correction_json` (WP-L).
    """

    candidates = {key.split(".", 1)[1]: Path(path) for key, path in transcription.native.items()}
    candidates[TRANSCRIPT_ARTIFACT] = Path(transcription.transcript_json)
    if correction is not None:
        candidates.update(correction.artifacts)
    return candidates


def known_gpu_tiers(packages: Mapping[str, Any]) -> frozenset[str] | None:
    """The GPU tier names the transcription packages declare, or None when none is installed."""

    tiers = {tier for package in engine_host.of_kind(packages, "transcription") for tier in package.gpu_tiers}
    return frozenset(tiers) if tiers else None


def run_engines(
    request: Mapping[str, Any],
    task_dir: Path,
    task_id: str,
    *,
    title: str,
    subject_names: list[str],
    session: Any,
    packages: Mapping[str, Any],
    corrector: Any | None,
    problems: dict[str, str] | None = None,
) -> dict[str, Path]:
    """Transcribe (or take the transcription in hand), then correct up to the target.

    `problems` receives `PROJECTION_PROBLEM` when the correction ran but could
    not write its `correction.json`.
    """

    from . import correction as corrections
    from . import transcription as transcriptions

    target = request["target"]
    if target not in TARGETS:
        raise ValueError(f"unknown target {target!r}; known: {', '.join(TARGETS)}")
    in_hand = transcription_in_hand(request, task_dir)
    if in_hand is not None:
        emit("log", {"message": "从云端识别结果续跑：跳过人声分离与识别"})
    reporter = NonokaXReporter(announce_completion=False)
    session.reporter = reporter
    report = transcriptions.EventReporter(emit)
    gpu_tier = resolve_request_gpu_tier(
        request, warn=lambda message: reporter.warning("gpu-tier", message), known=known_gpu_tiers(packages)
    )
    correction_block = request.get("correction") or {}
    fast = resolve_request_fast(correction_block, warn=lambda message: reporter.warning("fast-mode", message))

    # Both looked up before anything runs: an engine that is not there fails
    # the task now, not after the other half has run.
    engine = None if in_hand is not None else transcriptions.registry.get(transcriptions.requested_engine(request))
    # The task's correction engine: the transcription's record directory is
    # its own, and its process may already have applied the task's LLM pin.
    task_corrector = corrector
    core_raw_srt = core_renders_raw_srt(request, packages)
    if core_raw_srt:
        # The core renders the raw SRT from `transcript.json`, the cloud's included.
        corrector = None
    elif target in corrections.CORRECTION_TARGETS:
        corrector = corrections.registry.get(corrections.requested_engine(request))
        task_corrector = corrector
    else:
        corrector = None

    pending = None
    if corrector is not None:
        # A result in hand is the correction's input already, so its check sees
        # what its run will; otherwise the transcript's path, still to be written.
        placeholder = in_hand if in_hand is not None else transcriptions.TranscriptionResult(
            transcript_json=task_dir / transcriptions.TRANSCRIPT_FILENAME
        )
        pending = correction_job(
            request, task_dir, task_id, placeholder,
            title=title, fast=fast, subject_names=subject_names, corrector=corrector,
        )
        # What the correction can refuse is refused before the transcription
        # spends anything (an engine without the hook has nothing to refuse early).
        check = getattr(corrector, "check", None)
        if callable(check):
            check(pending, report)
    if in_hand is None:
        job = transcription_job(
            request, task_dir, task_id, title=title, gpu_tier=gpu_tier, engine=engine, corrector=task_corrector
        )
        result = engine.transcribe(job, report)
    else:
        # The stages up to the result in hand are done, and said to be,
        # as a run resuming from them says it.
        for stage in TRANSCRIPTION_TARGETS:
            reporter.stage_started(stage, reused=True)
        result = in_hand
    corrected = None
    if corrector is not None:
        if not result.native:
            # The transcript is what the correction reads: checked here, as
            # reading it was before the correction moved into its package.
            result.read_transcript()
        corrected = corrector.run(dataclasses.replace(pending, transcription=result), report)
        if problems is not None and CORRECTION_ARTIFACT not in corrected.artifacts:
            error = corrected.metadata.get("correction_error")
            if error:
                problems[PROJECTION_PROBLEM] = str(error)
    candidates = manifest_candidates(result, corrected)
    if core_raw_srt:
        from .transcription.srt import RAW_SRT_FILENAME, write_raw_srt

        reporter.stage_started("raw-srt")
        candidates["raw_srt"] = write_raw_srt(result.transcript_json, task_dir / RAW_SRT_FILENAME)
    emit_completion()
    return candidates


def core_renders_raw_srt(request: Mapping[str, Any], packages: Mapping[str, Any]) -> bool:
    """Whether the core renders this task's raw SRT itself (WP-P1, question 10).

    Only for a `raw-srt` task with no correction package installed: then a
    transcription engine still delivers subtitles, rendered from its
    `transcript.json` (`nonoka_x.transcription.srt`). With a correction
    package installed its engine writes the raw SRT, whichever engine
    transcribed, as before the packages.
    """

    from . import correction as corrections

    return (
        request.get("target") == "raw-srt"
        and engine_host.correction_package(packages) is None
        and corrections.requested_engine(request) == corrections.DEFAULT_ENGINE
    )


def _engine_commit(packages: Mapping[str, Any], *engines: Any) -> str:
    """The engine release a task's artifacts came from: the package that ran it, else any installed one."""

    for engine in engines:
        package = getattr(engine, "package", None)
        if package is not None and package.engine_commit:
            return package.engine_commit
    return next((package.engine_commit for package in packages.values() if package.engine_commit), "")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--task-dir", type=Path, required=True)
    # Where the engine packages are (`engine_host.default_engines_root()` when not given).
    parser.add_argument("--engines", type=Path, default=None)
    # Accepted and ignored: a sidecar from before the engine packages still names the vendored engine.
    parser.add_argument("--vendor", type=Path, default=None, help=argparse.SUPPRESS)
    # Where the engines read keys and find the managed runtime (WP5); a worker
    # started without them runs only the engines that need neither.
    parser.add_argument("--data-dir", type=Path, default=None)
    parser.add_argument("--install-dir", type=Path, default=None)
    args = parser.parse_args(argv)
    engines_root = (args.engines or engine_host.default_engines_root()).resolve()
    session = engine_host.TaskSession(emit=emit)
    contexts = None
    try:
        from . import correction as corrections
        from .settings import TASK_GROUPS_BY_ROUTE

        request = json.loads(sys.stdin.readline())
        args.task_dir.mkdir(parents=True, exist_ok=True)
        packages = engine_host.discover(engines_root)
        install_app_proxy_policy()
        contexts = launch_contexts(packages, args.data_dir, args.install_dir, engines_root)
        session.contexts = contexts.contexts
        register_engines(**engine_context(args.data_dir, args.install_dir, engines_root),
                         packages=packages, session=session)
        requested_correction = corrections.requested_engine(request)
        corrector = corrections.registry.get(requested_correction) if requested_correction in corrections.registry else None
        if corrector is not None and hasattr(corrector, "begin"):
            # The task's LLM model pin, applied in the correction's process
            # before anything of it runs (it is process-global there).
            corrector.begin = {"llm_model": request.get("llm_model"),
                               "task_groups": {route: list(groups) for route, groups in TASK_GROUPS_BY_ROUTE.items()}}
        request = for_task_mode(request, corrector, record_dir(args.task_dir, corrector))
        from . import transcription as transcriptions

        transcriber = engine_host.transcription_package(packages, transcriptions.requested_engine(request))
        models_dir = ((session.contexts.get(transcriber.id) or {}).get("runtime") or {}).get("models") if transcriber else None
        emit_request_notices(request, models_dir)
        source = request["source"]
        correction = dict(request.get("correction") or {})
        correction["extra_info"] = compose_knowledge_context(
            request, str(correction.get("extra_info") or "")
        )
        request["correction"] = correction
        title = str(source.get("title") or Path(source["path"]).stem)

        axis = request.get("axis") if isinstance(request.get("axis"), dict) else None
        problems: dict[str, str] = {}
        problem = knowledge_package_problem(request, packages)
        if problem:
            raise RuntimeError(problem)
        # Before the transcription: the ASR context may pick the entries of a
        # subject created here.
        subjects: dict[str, Any] = {"names": [], "created": []}
        knowledge_package = engine_host.knowledge_package(packages)
        if knowledge_package is not None and needs_knowledge_process(request, corrector):
            prepared = engine_host.start_knowledge_service(
                session, knowledge_package, subjects=subject_request(request, args.task_id)
            )
            if prepared is not None:
                subjects = prepared
        for entry in subjects["created"]:
            emit("log", {
                "message": "knowledge subject initialized",
                "fields": {"entry": entry, "rev": subjects["rev"]},
            })
        if axis is not None and axis.get("kind") == "ja":
            if corrector is None:
                raise RuntimeError(f"纠错引擎 {requested_correction} 未安装：日文轴只能交给纠错引擎翻译")
            session.reporter = NonokaXReporter()
            directory = native_dir(args.task_dir, corrector)
            translated = corrector.call("axis", {
                "rows": axis["rows"],
                "output_path": str(directory / f"{title}.srt"),
                "task_id": args.task_id,
                "task_artifact_dir": str(record_dir(args.task_dir, corrector)),
                "task_summary": knowledge_task_summary(request, str(source.get("title") or title)),
                "options": {
                    **correction,
                    "fast": resolve_request_fast(correction, warn=lambda notice: emit("log", {"message": notice})),
                },
                "knowledge": request.get("knowledge", "none"),
                "source_path": str(source["path"]),
                "knowledge_subjects": subjects["names"] or None,
                "correction_json": str(args.task_dir / CORRECTION_FILENAME),
            })
            candidates = {
                "stable_json": Path(translated["stable_json"]),
                "annotated_csv": Path(translated["annotated_csv"]),
                "final_srt": Path(translated["final_srt"]),
            }
            if translated.get("correction_json"):
                candidates[CORRECTION_ARTIFACT] = Path(translated["correction_json"])
            elif translated.get("correction_error"):
                problems[PROJECTION_PROBLEM] = str(translated["correction_error"])
            ran: tuple[Any, ...] = (corrector,)
        else:
            candidates = run_engines(
                request, args.task_dir, args.task_id, title=title, subject_names=subjects["names"],
                session=session, packages=packages, corrector=corrector, problems=problems,
            )
            requested = transcriptions.requested_engine(request)
            transcriber_engine = transcriptions.registry.get(requested) if requested in transcriptions.registry else None
            ran = (corrector, transcriber_engine)
        manifest = {
            "schema": 1,
            "task_id": args.task_id,
            "engine_commit": _engine_commit(packages, *ran),
            "artifacts": {name: artifact(path) for name, path in candidates.items() if path.is_file()},
            **problems,
        }
        emit("completed", {"artifacts": manifest})
        return 0
    except engine_host.PackageError as exc:
        # The package formatted its failure, and printed its traceback, itself.
        emit("failed", {"message": exc.formatted})
        return 1
    except Exception as exc:
        emit("failed", {"message": f"{type(exc).__name__}: {str(exc).strip()}"})
        if os.environ.get("NONOKA_DEBUG") or not type(exc).__name__.startswith("LocalAgent"):
            traceback.print_exc(file=sys.stderr)
        return 1
    finally:
        session.close()
        if contexts is not None:
            contexts.close()


if __name__ == "__main__":
    raise SystemExit(main())
