"""Persistent headless Local ExecutionProvider and isolated worker lifecycle."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import unquote, urlparse
from urllib.request import url2pathname

from .axis import (
    GENERATED_TRACK_PREFIXES,
    AxisError,
    axis_projection,
    conform_to_axis,
    normalize_axis,
    split_diarized_tracks,
)
from .document_store import (
    DocumentNotFound,
    DocumentStore,
    RevisionConflict,
    _atomic_json,
    _read_json_when_free,
)
from .peaks import generate_peaks
from .projector import ProjectionError, hide_source_lanes, project_edit_document
from .provision import RuntimeProvisionError, RuntimeProvisioner
from .settings import (
    FineSubSettings,
    LOCAL_AGENT_PROVIDERS,
    local_agent_executable,
    local_agent_path_entries,
)


STATES = {"queued", "running", "completed", "failed", "cancelled", "interrupted"}
TERMINAL = {"completed", "failed", "cancelled"}
#: What a document is projected from and the cloud library syncs (WP-L): Nonoka's
#: formats and the subtitles, with the file names a projection keeps them under.
ARTIFACT_SUFFIXES = {
    "transcript_json": ".transcript.json",
    "correction_json": ".correction.json",
    "raw_srt": ".raw.srt",
    "final_srt": ".srt",
}
ARTIFACT_NAMES = frozenset(ARTIFACT_SUFFIXES)

# 每个视频自带一份 [V4+ Styles] 原文（字幕长什么样跟着视频走）。上限与本机默认模板
# 那份保持一致：样式表就是一行 Format 加几十行 Style，超出这个量级的多半是误粘。
_MAX_STYLES_BYTES = 1 << 20

# FineSub's own `stages.PIPELINE_STAGE_ORDER`, restated because this process
# cannot import the engine -- it runs on the bootstrap interpreter, while
# finesub lives in the managed venv the worker is spawned into. A sync that
# changes the engine's stage list has to change this one; `tests/
# test_vendor_contract.py` is where that pairing is checked.
PIPELINE_TARGETS = ("vocal", "aligned", "stable", "raw-srt", "translated-srt", "final-srt")
# Which readiness stages of `runtime_report` a target has to clear. Everything
# up to the raw SRT is ASR work; the two LLM stages need a configured model on
# top of it, since a run from media has to recognise before it can correct.
TARGET_READINESS = {
    "vocal": ("raw-srt",),
    "aligned": ("raw-srt",),
    "stable": ("raw-srt",),
    "raw-srt": ("raw-srt",),
    "translated-srt": ("raw-srt", "final-srt"),
    "final-srt": ("raw-srt", "final-srt"),
}
# The same, for a run that continues from the transcript.json the cloud recognised.
# Nothing here transcribes, so none of it needs the ASR runtime: the raw SRT
# is rendered by the harness alone, and the LLM stages add the model. A cloud
# API engine clears these too, after its own `raw-srt` tier (its readiness:
# the media tools and its keys), since nothing of the ASR stack runs here.
CONTINUATION_READINESS = {
    "raw-srt": ("harness",),
    "translated-srt": ("final-srt",),
    "final-srt": ("final-srt",),
}
# The text-only roles plugins may ask for, mapped onto engine roles by the
# correction package (its `llm.ENGINE_ROLES`). The multimodal role is omitted
# deliberately: it exists to put a clip in front of a model, and this entry
# point has no way to pass one, so asking for it would only route text at a
# more expensive model.
LLM_ROLES = ("lightweight", "general_capable")
# FineSub's `routing.profiles.KNOWLEDGE_MODES` (0.6.0 renamed `collect` to
# `read`), restated and checked against the engine in test_vendor_contract.py.
KNOWLEDGE_MODES = ("none", "read", "update")
# One call may take a while on a reasoning model behind a local agent CLI, but
# not forever: the sidecar thread serving it is blocked for the duration.
LLM_CALL_TIMEOUT_SEC = 300.0
# The worker holds a routed client, its rate limiter and any agent sessions
# open. Idle that long and the next call can afford to build them again.
LLM_WORKER_IDLE_SEC = 600.0


class ProviderError(RuntimeError):
    def __init__(self, code: str, message: str, *, http_status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.http_status = http_status


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _engine_upstream(packages: Mapping[str, Any]) -> dict[str, Any]:
    """The engine release the installed packages carry (`PACKAGE.json` `upstream`); empty when none is installed."""

    from . import engine_host

    for kind in ("correction", "transcription", "knowledge"):
        for package in engine_host.of_kind(packages, kind):
            if package.upstream.get("commit"):
                return dict(package.upstream)
    return {}


def _issue(code: str, message: str) -> dict[str, str]:
    return {"code": code, "message": message}


_IGNORED_WORKER_LOG_SUBSTRINGS = (
    "CUDAExecutionProvider not available in ONNXruntime",
)


def _media_issues(provisioner: Any | None) -> list[dict[str, str]]:
    """The `media` tier: the sidecar's own probe (each engine checks the tools it needs itself).

    FFmpeg and FFprobe count only from the tool layer, as for the engines (appendix B question 94):
    one on the user's PATH does not make the tier ready.
    """

    managed = provisioner.status() if provisioner is not None else None
    managed_runtime_ready = managed is not None and managed["runtime"].get("state") == "ready"
    media_issues: list[dict[str, str]] = []
    if sys.version_info < (3, 12) and not managed_runtime_ready:
        media_issues.append(_issue("missing_python", "需要 Python 3.12 或更高版本"))
    managed_ffmpeg = provisioner.tool_path("ffmpeg") if provisioner is not None else None
    managed_ffprobe = provisioner.tool_path("ffprobe") if provisioner is not None else None
    if managed_ffmpeg is None or managed_ffprobe is None:
        from .engine_host import missing_ffmpeg_message

        media_issues.append(_issue("missing_ffmpeg", missing_ffmpeg_message(managed)))
    return media_issues


class _StatusOnce:
    """A provisioner whose `status()` is read once: one report asks it of every engine.

    The status walks the model directories, and a report asks for it once per
    engine and tier.
    """

    def __init__(self, provisioner: Any) -> None:
        self._provisioner = provisioner
        self._status: Any = None

    def status(self) -> Any:
        if self._status is None:
            self._status = self._provisioner.status()
        return self._status

    def __getattr__(self, name: str) -> Any:
        return getattr(self._provisioner, name)


def _issues_of(readiness: Any) -> list[dict[str, str]]:
    return [issue.to_dict() for issue in readiness.issues]


def _gpu_findings(spec: Any) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """What a missing card means for an engine, from `spec.environment.gpu`: (issues, warnings).

    `optional`: it runs on the CPU too, only slowly, so the card is a warning
    -- an issue would make `ready` false and grey the whole local execution
    card out. Only asked on Windows, the one platform the local GPU stack runs on.
    """

    need = spec.environment.gpu
    if need == "none" or sys.platform != "win32" or shutil.which("nvidia-smi"):
        return [], []
    if need == "required":
        return [_issue("missing_gpu", f"{spec.label} 需要 NVIDIA 显卡，未检测到 NVIDIA 驱动或 GPU")], []
    return [], [_issue("missing_gpu", "未检测到 NVIDIA 驱动或 GPU，将以 CPU 模式运行，速度显著低于显卡")]


def _local_agent() -> str:
    # A routable provider first — the Codex app hides its CLI in a directory
    # PATH never sees, so a plain `which` reports "no agent" on a machine that
    # has one. The remaining vendors have no desktop route yet, so PATH is the
    # whole question for them.
    return next(
        (
            LOCAL_AGENT_PROVIDERS[provider]["command"]
            for provider in LOCAL_AGENT_PROVIDERS
            if local_agent_executable(provider) is not None
        ),
        "",
    ) or next((name for name in ("codex", "claude", "agy") if shutil.which(name)), "")


def built_engines(
    settings: FineSubSettings | None, provisioner: Any | None, packages: Mapping[str, Any] | None = None
) -> tuple[list[Any], list[Any]]:
    """Every engine (the installed packages' and the core's own, `worker.build_engines`), on the
    config and runtime facades of `settings` and `provisioner`: (transcription, correction)."""

    from .config import ConfigStore
    from .runtime import RuntimeManager
    from .worker import build_engines

    store = ConfigStore(settings) if settings is not None else None
    return build_engines(store=store, runtime=RuntimeManager(provisioner), packages=packages)


def _find(engines: list[Any], engine_id: str) -> Any | None:
    return next((engine for engine in engines if engine.spec.id == engine_id), None)


def _engine_readiness(engine: Any | None, engine_id: str, context: Any) -> list[dict[str, str]]:
    if engine is None:
        return [_issue("missing_engine", f"转写引擎 {engine_id} 未安装")]
    return _issues_of(engine.readiness(context))


def runtime_report(
    settings: FineSubSettings | None = None,
    provisioner: RuntimeProvisioner | None = None,
    *,
    engine: str | None = None,
    context: Any | None = None,
    packages: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The readiness tiers (`runtime.stages`) and what the `ready` of local execution means.

    Each tier is asked of the engine it belongs to (WP5): `raw-srt` of the
    transcription engine `engine` (the default one, FineSub, unless a task
    picked another), with `context`; `harness` and the LLM tiers of the
    correction engine. Only `media` is the sidecar's own probe.
    """

    return _report(settings, provisioner, engine=engine, context=context, packages=packages)[0]


def _report(
    settings: FineSubSettings | None,
    provisioner: RuntimeProvisioner | None,
    *,
    engine: str | None = None,
    context: Any | None = None,
    packages: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], list[Any], list[Any]]:
    """`runtime_report`, and the engines it asked: (report, transcription engines, correction engines)."""

    from .correction import DEFAULT_ENGINE as DEFAULT_CORRECTION
    from .transcription import DEFAULT_ENGINE, ReadinessContext

    if provisioner is not None:
        provisioner = _StatusOnce(provisioner)
    managed = provisioner.status() if provisioner is not None else None
    transcription_engines, correction_engines = built_engines(settings, provisioner, packages)
    engine_id = engine or DEFAULT_ENGINE
    transcriber = _find(transcription_engines, engine_id)
    media_issues = _media_issues(provisioner)
    asr_issues = _engine_readiness(transcriber, engine_id, context or ReadinessContext(target="raw-srt"))
    asr_warnings: list[dict[str, str]] = []
    if transcriber is not None:
        gpu_issues, asr_warnings = _gpu_findings(transcriber.spec)
        asr_issues += gpu_issues

    corrector = _find(correction_engines, DEFAULT_CORRECTION)
    if corrector is None:
        missing = [_issue("missing_engine", f"纠错引擎 {DEFAULT_CORRECTION} 未安装")]
        harness_issues = llm_issues = knowledge_issues = video_issues = missing
    else:
        harness_issues = _issues_of(corrector.readiness(ReadinessContext(target="raw-srt"), {}))
        llm_issues = _issues_of(corrector.readiness(ReadinessContext(target="final-srt"), {"knowledge": "none"}))
        # The knowledge base is a versioned SQLite store. Its update path needs a
        # configured LLM, but Git has not been part of persistence since the node
        # store migration; keep the readiness stage aligned with that contract.
        knowledge_issues = _issues_of(
            corrector.readiness(ReadinessContext(target="final-srt"), {"knowledge": "update"})
        )
        video_issues = _issues_of(corrector.readiness(
            ReadinessContext(target="final-srt"), {"knowledge": "none", "correction": {"media": "video"}}
        ))
        from . import engine_host
        from .worker import STYLE_NEEDS_KNOWLEDGE

        found = engine_host.discover() if packages is None else packages
        if getattr(corrector, "package", None) is not None and engine_host.knowledge_package(found) is None:
            # The package's correction reads its style catalog from the knowledge base even with the
            # base switched off, so without the knowledge package none of its LLM tiers can run
            # (appendix B question 94); `harness` (rendering the raw SRT) is unaffected.
            style = [_issue("missing_knowledge_package", STYLE_NEEDS_KNOWLEDGE)]
            llm_issues = [*llm_issues, *style]
            knowledge_issues = [*knowledge_issues, *style]
            video_issues = [*video_issues, *style]

    def stage(stage_id: str, label: str, issues: list[dict[str, str]]) -> dict[str, Any]:
        return {"id": stage_id, "label": label, "ready": not issues, "issues": issues}

    stages = [
        stage("media", "媒体探测", media_issues),
        stage("raw-srt", "生成原始字幕", asr_issues),
        stage("harness", "云端识别后本地续跑", harness_issues),
        stage("final-srt", "纠错与翻译", llm_issues),
        stage("knowledge", "知识库更新", knowledge_issues),
        stage("video-multimodal", "视频多模态纠错", video_issues),
    ]
    report = {
        "ready": not asr_issues,
        "issues": asr_issues,
        "warnings": asr_warnings,
        "stages": stages,
        "localAgent": _local_agent(),
        "managed": managed,
    }
    return report, transcription_engines, correction_engines


def runtime_issues() -> list[dict[str, str]]:
    return runtime_report()["issues"]


#: The device every machine has. Always offered, always last: the desktop picks
#: `devices[0]` as a task's default, so a machine with a card still defaults to
#: the card, and a machine without one has exactly one thing to pick.
CPU_DEVICE: dict[str, Any] = {"id": "cpu", "name": "CPU", "memory_mb": 0}


def detect_devices() -> list[dict[str, Any]]:
    """Every device a task may be sent to, fastest first.

    A machine with no usable card is not a machine that cannot run: FineSub's
    `cpu` tier is a supported mode and `resolve_device` falls back to it on its
    own. So the list is never empty -- an empty list used to leave the desktop's
    device picker with nothing but a hardcoded "auto-select NVIDIA GPU".
    """

    executable = shutil.which("nvidia-smi")
    if not executable:
        return [dict(CPU_DEVICE)]
    try:
        result = subprocess.run(
            [executable, "--query-gpu=index,name,memory.total", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return [dict(CPU_DEVICE)]
    devices: list[dict[str, Any]] = []
    for line in result.stdout.splitlines():
        fields = [field.strip() for field in line.split(",", 2)]
        if len(fields) != 3:
            continue
        try:
            devices.append({"id": f"cuda:{int(fields[0])}", "name": fields[1], "memory_mb": int(fields[2])})
        except ValueError:
            continue
    devices.append(dict(CPU_DEVICE))
    return devices


def capabilities(
    packages: Mapping[str, Any] | None = None,
    *,
    issues: list[dict[str, str]] | None = None,
    settings: FineSubSettings | None = None,
    provisioner: RuntimeProvisioner | None = None,
) -> dict[str, Any]:
    from . import engine_host
    from .correction import correction_capability
    from .transcription import ReadinessContext, engine_capability

    packages = engine_host.discover() if packages is None else packages
    upstream = _engine_upstream(packages)
    runtime, transcription_engines, correction_engines = _report(settings, provisioner, packages=packages)
    if issues is not None:
        runtime["issues"] = issues
        runtime["ready"] = not issues
    return {
        "provider": "local",
        "adapter_schema": 1,
        "artifact_schema": 1,
        # The engine release the installed packages carry; empty when none is installed.
        "engine": {
            "name": str(upstream.get("repository") or "").rstrip("/").rsplit("/", 1)[-1],
            "version": str(upstream.get("engine_version") or ""),
            "commit": str(upstream.get("commit") or ""),
            "bundle_id": str(upstream.get("engine_bundle_id") or ""),
        },
        "features": {
            "raw_srt": True,
            "translation": True,
            "video_multimodal": True,
            "knowledge": True,
            "resume": True,
            "diarization": True,
        },
        "devices": detect_devices(),
        "runtime": runtime,
        # Every engine this sidecar can run, ready or not (plan 5.3). A
        # transcription engine is asked whether it can run at all; the
        # correction engine whether it can take a task to `final-srt`.
        "transcription_engines": [
            engine_capability(engine.spec, engine.readiness(ReadinessContext()))
            for engine in transcription_engines
        ],
        "correction_engines": [
            correction_capability(engine.spec, engine.readiness(ReadinessContext(target="final-srt"), {}))
            for engine in correction_engines
        ],
        "settings": {
            "llm_key_configured": bool(settings.snapshot().get("llmKeyConfigured")) if settings else False
        },
    }


#: Mirrors the engine's `MAX_KNOWLEDGE_SUBJECTS`: every correction window
#: carries all of them, so the cap bounds prompt size, not only the picker.
MAX_KNOWLEDGE_SUBJECTS = 8
_KNOWLEDGE_SUBJECT_FIELDS = (("subject", 120), ("aliases", 300), ("description", 1200))


def _normalize_knowledge_subjects(request: Mapping[str, Any], knowledge: str) -> list[dict[str, str]]:
    """The subjects a task reads from and, under `update`, writes into.

    An item with an `id` names an existing entry; one without is a new subject
    the worker creates before the run, which only `update` may do. A request
    queued before the list existed carries a single `knowledge_context`, which
    only ever meant anything under `update`, so it is lifted into a one-item
    list there and dropped otherwise.
    """

    if knowledge == "none":
        return []
    raw = request.get("knowledge_subjects")
    if raw is None:
        legacy = request.get("knowledge_context")
        raw = [legacy] if knowledge == "update" and legacy is not None else []
    if not isinstance(raw, list):
        raise ProviderError("invalid_request", "knowledge_subjects must be a list")
    if len(raw) > MAX_KNOWLEDGE_SUBJECTS:
        raise ProviderError(
            "invalid_request", f"knowledge_subjects allows at most {MAX_KNOWLEDGE_SUBJECTS} items"
        )
    subjects: list[dict[str, str]] = []
    seen: set[str] = set()
    for index, item in enumerate(raw):
        label = f"knowledge_subjects[{index}]"
        if not isinstance(item, dict):
            raise ProviderError("invalid_request", f"{label} must be an object")
        kind = str(item.get("kind") or "").strip()
        if kind not in {"streamer", "work", "topic"}:
            raise ProviderError("invalid_request", f"{label}.kind must be streamer, work, or topic")
        subject: dict[str, str] = {"kind": kind}
        ident = item.get("id")
        if ident is not None:
            if not isinstance(ident, str) or not ident.strip():
                raise ProviderError("invalid_request", f"{label}.id must be a non-empty string")
            subject["id"] = ident.strip()
        for field, limit in _KNOWLEDGE_SUBJECT_FIELDS:
            value = item.get(field, "")
            if not isinstance(value, str):
                raise ProviderError("invalid_request", f"{label}.{field} must be a string")
            value = value.strip()
            if len(value) > limit:
                raise ProviderError("invalid_request", f"{label}.{field} exceeds {limit} characters")
            subject[field] = value
        if not subject["subject"]:
            raise ProviderError("invalid_request", f"{label}.subject is required")
        if "id" not in subject and knowledge != "update":
            raise ProviderError(
                "invalid_request", f"{label}: a new subject can only be created when knowledge=update"
            )
        identity = subject.get("id") or f"new:{subject['subject'].casefold()}"
        if identity in seen:
            continue
        seen.add(identity)
        subjects.append(subject)
    # Writing back needs somewhere to write. Absence of both fields stays
    # accepted so an already queued pre-field request can still be retried.
    if knowledge == "update" and not subjects and (
        "knowledge_subjects" in request or "knowledge_context" in request
    ):
        raise ProviderError(
            "invalid_request", "knowledge_subjects needs at least one subject when knowledge=update"
        )
    return subjects


#: What a request asks of the local GPU stack, dropped for an engine that runs
#: on none (`environment.gpu == "none"`), and of the vocal separation, dropped
#: for an engine that does not separate (WP5, owner 2026-11-13).
GPU_FIELDS = ("device", "gpu_tier", "gpu_budget_gb")
SEPARATION_FIELDS = ("separate", "vocal_profile")


def builtin_engine_specs() -> dict[str, Any]:
    """The built-in transcription engines' specs by id (`worker.TRANSCRIPTION_ENGINES`)."""

    from .worker import build_engines

    return {engine.spec.id: engine.spec for engine in build_engines()[0]}


def dropped_fields(value: Mapping[str, Any], validated: Mapping[str, Any]) -> list[str]:
    """The fields the caller sent that `validate_request` dropped for the engine."""

    return [name for name in (*SEPARATION_FIELDS, *GPU_FIELDS) if name in value and name not in validated]


def validate_request(value: Mapping[str, Any], *, engines: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """The TaskRequest, checked and normalized for this provider.

    `engines` maps the transcription engines this provider has to their
    specs (`builtin_engine_specs()` when absent). A request on the default
    engine, FineSub, comes out exactly as it did before engines were
    selectable: a `transcription` block it carries is kept as it is, and none
    is added when it has none.
    """

    request = json.loads(json.dumps(value))
    if request.get("schema") != 1:
        raise ProviderError("unsupported_schema", "TaskRequest.schema must be 1")
    if request.get("provider") != "local":
        raise ProviderError("unsupported_provider", "Local Provider only accepts provider=local")
    source = request.get("source")
    if not isinstance(source, dict) or source.get("kind") not in SOURCE_KINDS:
        raise ProviderError(
            "invalid_source", "Local Provider requires source.kind=local_file or cloud_transcript"
        )
    path_value = source.get("path")
    if not isinstance(path_value, str) or not Path(path_value).is_absolute():
        raise ProviderError("invalid_source", "Local source path must be absolute")
    source_path = Path(path_value).expanduser().resolve()
    if not source_path.is_file():
        raise ProviderError("source_not_found", f"Local source does not exist: {source_path}")
    source["path"] = str(source_path)
    video_id = source.get("video_id")
    if video_id is not None and not _safe_component(str(video_id)):
        raise ProviderError("invalid_source", "source.video_id is invalid")
    if request.get("target") not in PIPELINE_TARGETS:
        raise ProviderError(
            "unsupported_target", "target must be one of: " + ", ".join(PIPELINE_TARGETS)
        )
    continuation = source["kind"] == "cloud_transcript"
    if continuation:
        _validate_cloud_transcript(request)
    # Whether a finished run turns into the editable subtitle document. The
    # default is the app's own behaviour and the reason this is a field at all
    # is the other value: projection replaces the video's default document, so
    # a caller that only wanted one stage's artifacts -- a plugin running
    # `vocal`, say -- must be able to ask for the run without it. See
    # `_run_worker`.
    projection = request.setdefault("projection", "document")
    if projection not in {"document", "none"}:
        raise ProviderError("invalid_request", "projection must be document or none")
    correction = request.setdefault("correction", {})
    if not isinstance(correction, dict):
        raise ProviderError("invalid_request", "correction must be an object")
    if correction.get("media", "audio") not in {"text", "audio", "video"}:
        raise ProviderError("invalid_request", "correction.media must be text, audio, or video")
    request.setdefault("language", "ja")
    device = request.setdefault("device", "cuda")
    if not isinstance(device, str):
        raise ProviderError("invalid_request", "device must be a string")
    device_norm = device.strip().lower()
    if device_norm not in {"cuda", "cpu"}:
        if not (device_norm.startswith("cuda:") and device_norm.split(":", 1)[1].strip().isdigit()):
            raise ProviderError("invalid_request", f"unsupported device: {device!r}")
    # `auto` is the engine's own default and detects the card; a request
    # still carrying the retired `gpu_budget_gb` is converted at the worker
    # (`nonoka_x.gpu_tier`), so nothing is defaulted over it here.
    if "gpu_budget_gb" not in request:
        request.setdefault("gpu_tier", "auto")
    # FineSub has one separator policy and the local worker no longer passes a
    # profile through to it, so this field survives only as a contract check:
    # a request asking for the cloud's cost tier is asking for something local
    # execution does not have, and saying so beats silently running quality.
    # Separation on unless the caller says the input is already a vocal
    # track. Refused rather than coerced when it is not a bool: skipping it
    # by accident costs the whole recognition quality and reports nothing.
    separate = request.setdefault("separate", True)
    if not isinstance(separate, bool):
        raise ProviderError("invalid_request", "separate must be a boolean")
    # Optional per-run model pin, the engine's `--llm-model`. A string pins
    # every task group, a table pins the ones it names. The names themselves
    # are the route loader's to validate -- it owns the list and its error
    # says which ones it knows.
    llm_model = request.get("llm_model")
    if llm_model is not None and not isinstance(llm_model, (str, dict)):
        raise ProviderError("invalid_request", "llm_model must be a string or an object")
    if isinstance(llm_model, dict) and not all(
        isinstance(key, str) and isinstance(value, str) for key, value in llm_model.items()
    ):
        raise ProviderError("invalid_request", "llm_model entries must be strings")
    if continuation:
        # The cloud separated with its own profile -- `cost` by default -- and
        # nothing here separates again, so whatever the request carries over
        # from the cloud leg describes a stage that already ran.
        request.pop("vocal_profile", None)
    elif request.setdefault("vocal_profile", "quality") != "quality":
        raise ProviderError(
            "invalid_request", "Local Provider only supports vocal_profile=quality"
        )
    knowledge = request.setdefault("knowledge", "update")
    if knowledge == "collect":
        # FineSub 0.6.0 renamed it. A task saved or queued before the upgrade
        # still carries the old name; `read` is what it now means (the base is
        # read, no feedback is collected), and the worker says so once.
        knowledge = request["knowledge"] = "read"
        request["legacy_knowledge"] = "collect"
    if knowledge not in KNOWLEDGE_MODES:
        raise ProviderError("invalid_request", "knowledge must be none, read, or update")
    subjects = _normalize_knowledge_subjects(request, knowledge)
    request.pop("knowledge_context", None)
    if subjects:
        request["knowledge_subjects"] = subjects
    else:
        request.pop("knowledge_subjects", None)
    request.setdefault("cleanup_intermediate", False)
    # An imported axis reaches the worker only when it carries source text: the
    # `ja` shape replaces recognition outright. An empty axis is applied after
    # the run instead (see `_project_manifest`), and the finished shapes never
    # start a task at all, so both are refused here rather than accepted and
    # quietly ignored.
    try:
        axis = normalize_axis(request.get("axis"))
    except AxisError as exc:
        raise ProviderError("invalid_axis", str(exc)) from exc
    if axis is None:
        request.pop("axis", None)
    elif continuation:
        raise ProviderError(
            "invalid_axis", "a source-text axis replaces recognition; it cannot continue a cloud run"
        )
    elif axis["kind"] != "ja":
        raise ProviderError(
            "invalid_axis", f"axis.kind={axis['kind']} does not start a task; import it instead"
        )
    elif request["target"] != "final-srt":
        raise ProviderError("invalid_axis", "a source-text axis only runs the final-srt target")
    else:
        request["axis"] = axis
    diarization = normalize_diarization(
        request.get("diarization"), target=request["target"], has_axis="axis" in request
    )
    if diarization is None:
        request.pop("diarization", None)
    else:
        request["diarization"] = diarization
    _normalize_transcription(request, builtin_engine_specs() if engines is None else engines)
    return request


def _normalize_transcription(request: dict[str, Any], specs: Mapping[str, Any]) -> None:
    """Check `transcription` against the engine it names, and drop what that engine cannot use (WP5)."""

    from .transcription import DEFAULT_ENGINE, ContractError, check_id

    block = request.get("transcription")
    engine_id = DEFAULT_ENGINE
    if block is not None:
        if not isinstance(block, dict):
            raise ProviderError("invalid_request", "transcription must be an object")
        if block.get("engine") is not None:
            try:
                engine_id = check_id(block["engine"], "transcription.engine")
            except ContractError as exc:
                raise ProviderError("invalid_request", str(exc)) from exc
        options = block.get("options")
        if options is not None and not isinstance(options, dict):
            raise ProviderError("invalid_request", "transcription.options must be an object")
    if engine_id == DEFAULT_ENGINE and (request["source"]["kind"] == "cloud_transcript" or "axis" in request):
        # The cloud's FineSub transcribed, or a Japanese axis replaces the
        # recognition; nothing transcribes here, so the engine need not be
        # installed (WP-CL: a cloud task reaches its raw SRT with no FineSub
        # package; a Japanese axis only needs the correction). Installed, it
        # would change nothing: FineSub takes every level and language and
        # drops no field.
        return
    spec = specs.get(engine_id)
    if spec is None:
        raise ProviderError(
            "unsupported_engine",
            f"transcription engine {engine_id!r} is not available; available: " + (", ".join(specs) or "none"),
        )
    if engine_id != DEFAULT_ENGINE:
        # Both replace recognition: the cloud's FineSub made the one, the user
        # brought the other. Another engine would have nothing to transcribe.
        if request["source"]["kind"] == "cloud_transcript":
            raise ProviderError(
                "invalid_request",
                f"a cloud_transcript source continues the cloud's recognition; it cannot run on {engine_id}",
            )
        if "axis" in request:
            raise ProviderError(
                "invalid_request", f"a source-text axis replaces recognition; it cannot run on {engine_id}"
            )
    if spec.kind != "local-gpu" and request["target"] not in CONTINUATION_TARGETS:
        # Such an engine's one product is transcript.json; the first artifact
        # of a task on it is the raw SRT the correction renders from that.
        raise ProviderError(
            "unsupported_target",
            f"{spec.label} transcribes to transcript.json only; target must be one of: "
            + ", ".join(CONTINUATION_TARGETS),
        )
    if spec.environment.gpu == "none":
        for name in GPU_FIELDS:
            request.pop(name, None)
    if not spec.features.separate:
        for name in SEPARATION_FIELDS:
            request.pop(name, None)
    diarization = request.get("diarization")
    if diarization is not None:
        for key in ("level", "requested_level"):
            level = diarization.get(key)
            if level is not None and level not in spec.features.diarization_levels:
                raise ProviderError(
                    "invalid_request",
                    f"{spec.label} does not support diarization.level={level}; supported: "
                    + ", ".join(spec.features.diarization_levels),
                )
    languages = spec.features.languages
    if languages and request["language"] != "auto" and request["language"] not in languages:
        raise ProviderError(
            "invalid_request", f"{spec.label} does not support language={request['language']}; supported: "
            + ", ".join(languages),
        )


#: Where a task's input comes from. `local_file` is media this machine
#: recognises itself; `cloud_transcript` is media the cloud has already
#: recognised, delivered as a transcription engine delivers its result: the
#: `transcript.json` the run continues from and the engine package's own files
#: as opaque attachments (plan 4.7 item 36).
SOURCE_KINDS = ("local_file", "cloud_transcript")
#: A cloud job stops at stable, so these are what is left to run here.
CONTINUATION_TARGETS = ("raw-srt", "translated-srt", "final-srt")


def _absolute_file(value: Any, field_name: str) -> Path:
    if not isinstance(value, str) or not Path(value).is_absolute():
        raise ProviderError("invalid_source", f"{field_name} must be an absolute path")
    path = Path(value).expanduser().resolve()
    if not path.is_file():
        raise ProviderError("source_not_found", f"{field_name} does not exist: {path}")
    return path


def _validate_cloud_transcript(request: dict[str, Any]) -> None:
    """Check what a continuation carries beyond a local run's fields.

    `source.path` is still the media -- the correction cuts audio and video
    clips from it, and the editor draws its waveform -- and was checked with
    every other source. What is new is the cloud's recognition, in the shape a
    transcription engine hands its result over in (plan 5.1):

    - `source.transcript_json`: Nonoka's `transcript.json`, read here with the
      core's own reader, which the run continues from;
    - `source.native`: the engine package's own files, by `native` key
      (`<engine>.<name>`), opaque: checked to be files and nothing more. The
      worker registers them as that package's private attachments of the task
      and the correction decides what to do with them;
    - `source.native_package`: the package whose files they are, the name of
      their directory in the task (plan 4.6 item 34). Required with any.
    """

    from .transcription.model import TranscriptError, read_transcript

    source = request["source"]
    if request["target"] not in CONTINUATION_TARGETS:
        raise ProviderError(
            "unsupported_target",
            "a cloud_transcript source continues to one of: " + ", ".join(CONTINUATION_TARGETS),
        )
    transcript = _absolute_file(source.get("transcript_json"), "source.transcript_json")
    try:
        read_transcript(transcript)
    except (OSError, UnicodeDecodeError, ValueError, TranscriptError) as exc:
        raise ProviderError("invalid_source", f"transcript.json is unreadable: {exc}") from exc
    source["transcript_json"] = str(transcript)
    native = source.get("native", {})
    if not isinstance(native, dict):
        raise ProviderError("invalid_source", "source.native must be an object")
    checked: dict[str, str] = {}
    for key, value in native.items():
        engine, _, name = str(key).partition(".")
        # Each part a safe file name: the key is the attachment's name in the task.
        if not _safe_component(engine) or not _safe_component(name):
            raise ProviderError("invalid_source", f"source.native key {key!r} is not <engine>.<name>")
        checked[str(key)] = str(_absolute_file(value, f"source.native[{key!r}]"))
    package = source.get("native_package")
    if checked and (not isinstance(package, str) or not package or not _safe_component(package)):
        raise ProviderError("invalid_source", "source.native_package must name the package the attachments are of")
    if checked:
        source["native"] = checked
    else:
        source.pop("native", None)
        source.pop("native_package", None)


# FineSub's `stages.DIARIZE_LEVELS`, restated for the same reason as
# `PIPELINE_TARGETS` and checked against it in tests/test_vendor_contract.py.
DIARIZE_LEVELS = ("off", "acoustics", "judge-text", "judge-audio")
# Speaker labels are written onto stable.json, so a run has to get that far.
_DIARIZE_MIN_TARGET = "stable"
MAX_DIARIZE_SPEAKERS = 32


def normalize_diarization(
    value: Any, *, target: str, has_axis: bool
) -> dict[str, Any] | None:
    """The request's `diarization` block, checked; `None` when it asks for none.

    The two judge levels run inside the LLM correction, so a run that stops
    before it gets the acoustic labels only; `requested_level` keeps what was
    asked so the worker can say why. A source-text axis replaces recognition
    and brings its own speakers, so it cannot be combined with diarization.
    """

    if value is None:
        return None
    if not isinstance(value, dict):
        raise ProviderError("invalid_request", "diarization must be an object")
    level = value.get("level", "off")
    if level not in DIARIZE_LEVELS:
        raise ProviderError(
            "invalid_request", "diarization.level must be one of: " + ", ".join(DIARIZE_LEVELS)
        )
    max_speakers = value.get("max_speakers")
    if max_speakers is not None and (
        isinstance(max_speakers, bool)
        or not isinstance(max_speakers, int)
        or not 1 <= max_speakers <= MAX_DIARIZE_SPEAKERS
    ):
        raise ProviderError(
            "invalid_request",
            f"diarization.max_speakers must be an integer from 1 to {MAX_DIARIZE_SPEAKERS}",
        )
    if level == "off":
        return None
    if PIPELINE_TARGETS.index(target) < PIPELINE_TARGETS.index(_DIARIZE_MIN_TARGET):
        raise ProviderError(
            "invalid_request", f"diarization needs target {_DIARIZE_MIN_TARGET} or later"
        )
    if has_axis:
        raise ProviderError(
            "invalid_request", "a source-text axis runs no recognition and cannot be diarized"
        )
    block: dict[str, Any] = {"level": level, "max_speakers": max_speakers}
    if level.startswith("judge-") and target != "final-srt":
        block["level"] = "acoustics"
        block["requested_level"] = level
    return block


# Prompt bytes one call may carry. A window of the engine's own correction
# prompt is an order of magnitude under this; the cap is here so a caller
# cannot turn one call into a bill.
MAX_LLM_PROMPT_BYTES = 200_000
MAX_LLM_OUTPUT_TOKENS = 32_768
_LLM_MESSAGE_ROLES = {"system", "user", "assistant"}


def _validate_llm_request(value: Mapping[str, Any]) -> dict[str, Any]:
    role = str(value.get("role") or "").strip()
    if role not in LLM_ROLES:
        raise ProviderError("invalid_request", "role must be one of: " + ", ".join(LLM_ROLES))
    raw_messages = value.get("messages")
    if not isinstance(raw_messages, list) or not raw_messages:
        raise ProviderError("invalid_request", "messages must be a non-empty list")
    messages: list[dict[str, str]] = []
    total = 0
    for index, item in enumerate(raw_messages):
        if not isinstance(item, Mapping):
            raise ProviderError("invalid_request", f"message {index} must be an object")
        message_role = str(item.get("role") or "")
        content = item.get("content")
        if message_role not in _LLM_MESSAGE_ROLES:
            raise ProviderError("invalid_request", f"message {index} role must be system, user or assistant")
        if not isinstance(content, str):
            raise ProviderError("invalid_request", f"message {index} content must be text")
        total += len(content.encode("utf-8"))
        if total > MAX_LLM_PROMPT_BYTES:
            raise ProviderError("invalid_request", f"messages exceed {MAX_LLM_PROMPT_BYTES} bytes")
        messages.append({"role": message_role, "content": content})
    max_tokens = value.get("max_tokens", 8192)
    if not isinstance(max_tokens, int) or isinstance(max_tokens, bool) or not 1 <= max_tokens <= MAX_LLM_OUTPUT_TOKENS:
        raise ProviderError("invalid_request", f"max_tokens must be an integer between 1 and {MAX_LLM_OUTPUT_TOKENS}")
    temperature = value.get("temperature", 1.0)
    if isinstance(temperature, bool) or not isinstance(temperature, (int, float)) or not 0.0 <= float(temperature) <= 2.0:
        raise ProviderError("invalid_request", "temperature must be a number between 0 and 2")
    request: dict[str, Any] = {
        "role": role,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": float(temperature),
    }
    if "timeout_seconds" in value:
        timeout = value["timeout_seconds"]
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 1 <= timeout <= LLM_CALL_TIMEOUT_SEC:
            raise ProviderError("invalid_request", f"timeout_seconds must be a number between 1 and {LLM_CALL_TIMEOUT_SEC:g}")
        request["timeout_seconds"] = float(timeout)
    ignore_cooldown = value.get("ignore_cooldown", False)
    if not isinstance(ignore_cooldown, bool):
        raise ProviderError("invalid_request", "ignore_cooldown must be true or false")
    if ignore_cooldown:
        request["ignore_cooldown"] = True
    return request


def _llm_timeout_error(timeout: float) -> ProviderError:
    return ProviderError("llm_timeout", f"The LLM call did not answer within {timeout:g}s", http_status=504)


def _read_line_with_timeout(process: subprocess.Popen[str], timeout: float) -> str | None:
    """One line of the worker's stdout, or None once `timeout` passes.

    `readline` has no timeout and the pipe is not selectable on Windows, so the
    read happens on a thread the caller can walk away from. It is a daemon
    thread over a pipe the caller closes on timeout, so it does not outlive the
    process it was reading.
    """

    assert process.stdout is not None
    received: list[str] = []

    def read() -> None:
        try:
            received.append(process.stdout.readline())  # type: ignore[union-attr]
        except (OSError, ValueError):
            received.append("")

    reader = threading.Thread(target=read, daemon=True, name="nonoka-llm-read")
    reader.start()
    reader.join(timeout)
    if reader.is_alive():
        return None
    return received[0] if received else ""


def _core_renders_raw_srt(request: Mapping[str, Any], packages: Mapping[str, Any]) -> bool:
    from .worker import core_renders_raw_srt

    return core_renders_raw_srt(request, packages)


def _safe_component(value: str) -> bool:
    return bool(value) and len(value) <= 80 and all(character.isalnum() or character in "_-" for character in value)


def classify_failure(message: str) -> str:
    lowered = message.lower()
    # The engine's "No API key for <tier> could be tried: ..." names every
    # reason a configured key was passed over. A key in its transient cooldown
    # (or left to a sibling call probing it) is a wait of minutes, not a missing
    # key -- the free Nonoka Cloud provider has exactly one key, so one upstream
    # hiccup used to fail the run as "missing API key". Checked before quota:
    # when some keys are daily-locked and others cooling, the cooldown is the
    # one that ends first.
    if "transient cooldown" in lowered or "probing the same cooldown" in lowered:
        return "rate_limited"
    if any(phrase in lowered for phrase in ("usage limit", "quota", "insufficient_quota", "credit", "billing", "upgrade to pro", "额度")):
        return "quota_exceeded"
    if any(phrase in lowered for phrase in ("not logged in", "authenticate", "login")):
        return "auth_failed"
    if "api key" in lowered or "api_key" in lowered:
        return "missing_llm_key"
    if "cuda" in lowered or "nvidia" in lowered:
        return "missing_gpu"
    if "model" in lowered and any(word in lowered for word in ("missing", "not found", "download")):
        return "missing_model"
    if "no space" in lowered or "disk" in lowered:
        return "insufficient_disk"
    if "no module named" in lowered or "not found" in lowered:
        return "missing_dependency"
    if any(
        phrase in lowered
        for phrase in ("codex cli", "claude code", "codebuddy", "localagent")
    ):
        return "agent_failed"
    return "engine_failed"


WorkerCommand = Callable[[str, Path], list[str]]


class LocalProvider:
    def __init__(
        self,
        root: str | Path,
        engines: str | Path | None = None,
        *,
        worker_command: WorkerCommand | None = None,
        llm_worker_command: Callable[[], list[str]] | None = None,
        issues: list[dict[str, str]] | None = None,
        settings: FineSubSettings | None = None,
        provisioner: RuntimeProvisioner | None = None,
    ) -> None:
        from . import engine_host

        self.root = Path(root).expanduser().resolve()
        #: Where the engine packages are; each one present there is an engine (plan 4.6 item 33).
        self.engines = Path(engines).expanduser().resolve() if engines is not None else engine_host.default_engines_root()
        self.root.mkdir(parents=True, exist_ok=True)
        self._issues_override = issues is not None
        self._issues = runtime_issues() if issues is None else issues
        self._settings = settings
        self._provisioner = provisioner
        # The cloud ASR engines' keys (OpenRouter STT, Azure Speech and its
        # endpoint, the MOSS Pro pool): registered with the config facade so
        # `update_keys` can save them. Nonoka's own engines read them; nothing
        # derives them for FineSub (WP5).
        from .transcription.keys import register_key_specs

        register_key_specs()
        from .telemetry import Telemetry

        self.telemetry = Telemetry(self.root.parent)
        self.documents = DocumentStore(self.root.parent / "documents")
        self._worker_command = worker_command or self._default_worker_command
        #: A command for the resident process instead of the correction package's (tests' stand-ins).
        self._llm_worker_command = llm_worker_command
        self._lock = threading.RLock()
        self._processes: dict[str, subprocess.Popen[str]] = {}
        self._threads: dict[str, threading.Thread] = {}
        # One LLM worker for the whole sidecar, held across calls: building a
        # routed client reads the route table, the model catalog and the key
        # file, and a caller correcting fifty lines would pay for all of it
        # fifty times. `_llm_lock` is what makes one process enough -- the
        # channel is one request, one response.
        self._llm_process: subprocess.Popen[str] | None = None
        self._llm_lock = threading.Lock()
        self._llm_idle_timer: threading.Timer | None = None
        # Set when keys or routes are saved. The worker read both when it was
        # spawned, so the next call must start a fresh one -- otherwise a model
        # test right after saving a new key would still use the old one.
        self._llm_config_stale = False
        # Set once by `shutdown`. Without it, killing the worker out from under
        # an in-flight call reads as "the worker died" and the retry starts a
        # new one -- an orphan process, spawned while the app is closing.
        self._closed = False
        # The launch context (and run directory) of the resident correction
        # process, released with it.
        self._llm_run: Any | None = None
        self._sweep_engine_runs()
        self._recover_interrupted()

    def packages(self) -> dict[str, Any]:
        """The engine packages installed now (read each time: a removed package is a removed engine)."""

        from . import engine_host

        return engine_host.discover(self.engines)

    def _sweep_engine_runs(self) -> None:
        """Remove run directories a previous sidecar or worker left behind (D6)."""

        from . import engine_host

        try:
            paths = self._paths()
            package_dirs = [paths.engine(package) for package in self.packages()]
            engine_host.sweep_runs(package_dirs)
            # And the sandbox temporary directories of transcription processes
            # that are gone (plan 4.5 item 28).
            engine_host.sweep_sandbox(package_dirs)
        except Exception as exc:  # noqa: BLE001 - leftovers only cost disk space
            print(f"engine run sweep skipped: {exc}", file=sys.stderr, flush=True)

    def _default_worker_command(self, task_id: str, task_dir: Path) -> list[str]:
        # The core's worker only orchestrates: it runs on the launcher, like
        # the sidecar, and starts each engine package's process on the
        # interpreter that package runs on (`engine_host.interpreter_for`).
        command = [
            sys.executable,
            "-m",
            "nonoka_x.worker",
            "--task-id",
            task_id,
            "--task-dir",
            str(task_dir),
            "--engines",
            str(self.engines),
            # Where the worker's engines read keys and find the runtime (WP5).
            "--data-dir",
            str(self._data_dir()),
        ]
        install_root = getattr(self._provisioner, "install_root", None)
        if install_root is not None:
            command += ["--install-dir", str(install_root)]
        return command

    def _data_dir(self) -> Path:
        """The data directory: the settings' own, else the one the task root sits in."""

        root = getattr(self._settings, "root", None)
        return Path(root) if root is not None else self.root.parent

    def _engine_specs(self) -> dict[str, Any]:
        return {
            engine.spec.id: engine.spec
            for engine in built_engines(self._settings, self._provisioner, self.packages())[0]
        }

    def knowledge(self, request: dict) -> dict:
        """The knowledge page's list, read or save, in a short-lived knowledge package process."""

        from . import engine_contexts, engine_host

        package = engine_host.knowledge_package(self.packages())
        if package is None:
            raise ProviderError("missing_knowledge_package", "知识库包未安装", http_status=503)
        try:
            with engine_contexts.build({package.id: package}, paths=self._paths()) as built:
                with engine_host.PackageProcess(
                    package, built.contexts[package.id], environment=self._worker_environment()
                ) as process:
                    response = process.request("manage", {"request": request})
        except (OSError, engine_host.PackageError, engine_host.PackageUnavailable) as exc:
            raise ProviderError("knowledge_unavailable", "知识库服务不可用，请稍后重试", http_status=503) from exc
        if "error" in response:
            raise ProviderError("knowledge_error", response["error"], http_status=409)
        return response["result"]

    def _worker_environment(self) -> dict[str, str]:
        """The environment of a process the core starts for its engines: the task worker, an engine package's.

        Shared by every spawn rather than written for each: the items here are
        bugs someone already had to find -- an agent CLI that PATH cannot see,
        native output in the ANSI code page. What an engine reads of its own
        is not here: its package's bridge derives that from its launch context
        (`nonoka_x.engine_contexts`), inside the package's process.
        """

        environment = self._provisioner.worker_environment() if self._provisioner is not None else os.environ.copy()
        # The engine resolves an agent CLI by name off PATH, so an install that
        # is not on PATH has to be put there for the worker.
        agent_dirs = local_agent_path_entries()
        if agent_dirs:
            environment["PATH"] = os.pathsep.join([*agent_dirs, environment.get("PATH", "")])
        project_src = Path(__file__).resolve().parents[1]
        python_paths = [str(project_src)]
        if environment.get("PYTHONPATH"):
            python_paths.append(environment["PYTHONPATH"])
        environment["PYTHONPATH"] = os.pathsep.join(python_paths)
        # The worker's own NDJSON must be UTF-8, but stderr also carries native
        # library output in the Windows ANSI code page. Keep the Python
        # standard streams on UTF-8 while leaving locale-based subprocess
        # decoding on that native code page.
        environment["PYTHONUTF8"] = "0"
        environment["PYTHONIOENCODING"] = "utf-8"
        return environment

    def launch_contexts(self, packages: Mapping[str, Any] | None = None) -> Any:
        """Launch contexts (and run directories) for `packages` (every installed one), as a worker builds them."""

        from . import engine_contexts

        return engine_contexts.build(
            self.packages() if packages is None else packages,
            paths=self._paths(),
            settings=self._settings,
            provisioner=self._provisioner,
            proxy=engine_contexts.proxy_context(),
        )

    def _paths(self) -> Any:
        from .runtime.paths import NonokaPaths

        paths = getattr(self._provisioner, "paths", None)
        return paths if paths is not None else NonokaPaths.for_roots(self._data_dir())

    def _knowledge_problem(self, request: Mapping[str, Any], task_dir: Path | None = None) -> str | None:
        """`worker.knowledge_package_problem` for `request` as the worker would run it.

        A Chinese-source task's mode is the correction engine's to say
        (`task_mode`, asked of the resident correction process); a new task has
        no record yet, so `task_dir` defaults to one that does not exist.
        """

        from . import engine_host
        from .worker import knowledge_package_problem

        packages = self.packages()
        if engine_host.knowledge_package(packages) is not None:
            return None
        effective = dict(request)
        axis = request.get("axis")
        # With the base switched off the mode cannot switch it on; only whether the correction runs matters.
        if (request.get("knowledge", "update") != "none" and request.get("language") == "zh"
                and not (isinstance(axis, Mapping) and axis.get("kind") == "ja")):
            package = engine_host.correction_package(packages)
            if package is None:
                effective["knowledge"] = "none"
            else:
                directory = task_dir if task_dir is not None else self.root / f".new-{uuid.uuid4().hex}"
                record = directory / "native" / package.id / "llm-artifacts"
                effective = dict(self._service_call("task_mode", {"request": dict(request), "record_dir": str(record)}))
        return knowledge_package_problem(effective, packages)

    def get_capabilities(self) -> dict[str, Any]:
        issues = self._issues if self._issues_override else runtime_report(self._settings, self._provisioner)["issues"]
        return capabilities(self.packages(), issues=issues, settings=self._settings, provisioner=self._provisioner)

    def runtime_provision_status(self) -> dict[str, Any]:
        if self._provisioner is None:
            raise ProviderError("runtime_unavailable", "FineSub runtime installer is unavailable", http_status=503)
        return self._provisioner.status()

    def install_runtime(self, target: str) -> dict[str, Any]:
        if self._provisioner is None:
            raise ProviderError("runtime_unavailable", "FineSub runtime installer is unavailable", http_status=503)
        try:
            return self._provisioner.start(target)
        except RuntimeProvisionError as exc:
            raise ProviderError("runtime_install_unavailable", str(exc), http_status=409) from exc

    def cancel_runtime_install(self) -> dict[str, Any]:
        if self._provisioner is None:
            raise ProviderError("runtime_unavailable", "FineSub runtime installer is unavailable", http_status=503)
        try:
            return self._provisioner.cancel()
        except RuntimeProvisionError as exc:
            raise ProviderError("runtime_install_not_running", str(exc), http_status=409) from exc

    def remove_runtime(self) -> dict[str, Any]:
        if self._provisioner is None:
            raise ProviderError("runtime_unavailable", "FineSub runtime installer is unavailable", http_status=503)
        with self._lock:
            if any(process.poll() is None for process in self._processes.values()):
                raise ProviderError("runtime_in_use", "有本地任务正在运行，请先停止任务再删除环境", http_status=409)
        try:
            return self._provisioner.remove_all()
        except RuntimeProvisionError as exc:
            raise ProviderError("runtime_remove_unavailable", str(exc), http_status=409) from exc
        except OSError as exc:
            # A bare OSError reached the sidecar as an opaque HTTP 500, which the
            # desktop shell could only report as "Sidecar request failed".
            raise ProviderError("runtime_remove_failed", f"删除环境失败：{exc}", http_status=409) from exc

    def runtime_cache_status(self) -> dict[str, Any]:
        if self._provisioner is None:
            raise ProviderError("runtime_unavailable", "FineSub runtime installer is unavailable", http_status=503)
        return self._provisioner.cache_usage()

    def clear_runtime_cache(self) -> dict[str, Any]:
        if self._provisioner is None:
            raise ProviderError("runtime_unavailable", "FineSub runtime installer is unavailable", http_status=503)
        # Workers run with UV_CACHE_DIR pointing into the cache, and a file a
        # running worker has open cannot be unlinked on Windows anyway.
        with self._lock:
            if any(process.poll() is None for process in self._processes.values()):
                raise ProviderError("runtime_in_use", "有本地任务正在运行，请等任务结束后再清理缓存", http_status=409)
        try:
            return self._provisioner.clear_cache()
        except RuntimeProvisionError as exc:
            raise ProviderError("runtime_cache_unavailable", str(exc), http_status=409) from exc
        except OSError as exc:
            raise ProviderError("runtime_cache_failed", f"清理缓存失败：{exc}", http_status=409) from exc

    def _cleanup_scope(self, engine: str | None) -> tuple[set[str] | None, set[str], dict[str, Any]]:
        """For "clean up unused": (the layers kept for `engine`, the layers unfinished tasks may use, the header).

        `engine` is the desktop's current transcription engine. None (a desktop from before WP-UI) or an
        engine this sidecar does not have keeps every layer, so only old versions are offered.
        """

        from . import runtime_cleanup
        from .migration.legacy import TERMINAL_TASK_STATES
        from .runtime import LAYERS
        from .transcription import CLOUD_ENGINE, DEFAULT_ENGINE, ContractError, check_id

        transcription_engines, correction_engines = built_engines(self._settings, self._provisioner, self.packages())
        specs = {item.spec.id: item.spec for item in transcription_engines}
        correction: set[str] = set()
        for item in correction_engines:
            correction |= runtime_cleanup.spec_layers(item.spec)

        def layers_of(engine_id: str) -> set[str] | None:
            if engine_id == CLOUD_ENGINE:
                return set()
            spec = specs.get(engine_id)
            return runtime_cleanup.spec_layers(spec) if spec is not None else None

        if engine is not None:
            try:
                check_id(engine, "engine")
            except ContractError as exc:
                raise ProviderError("invalid_request", str(exc)) from exc
        engine_layers = layers_of(engine) if engine is not None else None
        needed = runtime_cleanup.needed_layers(engine_layers, correction)
        busy: set[str] = set()
        for snapshot_path in self.root.glob("*/snapshot.json"):
            try:
                snapshot = _read_json_when_free(snapshot_path)
            except (OSError, ValueError):
                continue
            if not isinstance(snapshot, Mapping) or snapshot.get("state") in TERMINAL_TASK_STATES:
                continue
            task_layers = layers_of(str(snapshot.get("transcription_engine") or DEFAULT_ENGINE))
            busy |= set(LAYERS) if task_layers is None else {*task_layers, *correction, *runtime_cleanup.CORE_LAYERS}
        header = {
            "engine": engine,
            "engine_available": engine is not None and engine_layers is not None,
            "needed": runtime_cleanup.ordered(needed) if needed is not None else None,
            "busy": runtime_cleanup.ordered(busy),
        }
        return needed, busy, header

    def runtime_cleanup_plan(self, engine: str | None = None) -> dict[str, Any]:
        """What "clean up unused" would delete for the current engine `engine`, item by item (WP-UI)."""

        if self._provisioner is None:
            raise ProviderError("runtime_unavailable", "FineSub runtime installer is unavailable", http_status=503)
        needed, busy, header = self._cleanup_scope(engine)
        return {**self._provisioner.cleanup_plan(needed, busy), **header}

    def remove_unused_runtime(self, engine: str | None = None) -> dict[str, Any]:
        """Delete what `runtime_cleanup_plan(engine)` offers (the settings page's "clean up unused").

        The layers neither `engine` nor the correction package needs, and the layer versions nothing
        uses; a layer an unfinished task may use stays (reported in `kept`).
        """

        if self._provisioner is None:
            raise ProviderError("runtime_unavailable", "FineSub runtime installer is unavailable", http_status=503)
        needed, busy, header = self._cleanup_scope(engine)
        try:
            return {**self._provisioner.cleanup(needed, busy), **header}
        except RuntimeProvisionError as exc:
            raise ProviderError("runtime_remove_unavailable", str(exc), http_status=409) from exc
        except OSError as exc:
            raise ProviderError("runtime_remove_failed", f"清理失败：{exc}", http_status=409) from exc

    def get_settings(self) -> dict[str, Any]:
        if self._settings is None:
            raise ProviderError("settings_unavailable", "FineSub settings are unavailable", http_status=503)
        from .config import ConfigStore

        # Through the facade: it adds the registered engine keys (`engineKeys`).
        return ConfigStore(self._settings).snapshot()

    def update_keys(self, updates: Mapping[str, Any]) -> dict[str, Any]:
        if self._settings is None:
            raise ProviderError("settings_unavailable", "FineSub settings are unavailable", http_status=503)
        if not isinstance(updates, Mapping):
            raise ProviderError("invalid_request", "keys must be an object")
        from .config import ConfigStore

        try:
            # Through the facade: it also takes the registered engine keys.
            result = ConfigStore(self._settings).update(updates)
        except ValueError as exc:
            raise ProviderError("invalid_settings", str(exc)) from exc
        self._llm_config_stale = True
        return result

    def set_cloud_account(self, account: Mapping[str, Any]) -> dict[str, Any]:
        if self._settings is None:
            raise ProviderError("settings_unavailable", "FineSub settings are unavailable", http_status=503)
        if not isinstance(account, Mapping):
            raise ProviderError("invalid_request", "account must be an object")
        try:
            result = self._settings.set_cloud_account(str(account.get("backend") or ""), str(account.get("key") or ""))
        except ValueError as exc:
            raise ProviderError("invalid_settings", str(exc)) from exc
        if result["changed"]:
            self._llm_config_stale = True
        return result

    def start(self, request: Mapping[str, Any]) -> dict[str, Any]:
        try:
            return self._start(request)
        except ProviderError as exc:
            from .telemetry import safe_error

            self.telemetry.emit(self.telemetry.context(self._settings, dict(request)), "task_error",
                                error=safe_error(exc.code, "start"))
            raise

    def _start(self, request: Mapping[str, Any]) -> dict[str, Any]:
        from .transcription import ReadinessContext, requested_engine, requested_options

        specs = self._engine_specs()
        validated = validate_request(request, engines=specs)
        if self._issues_override and self._issues:
            issue = self._issues[0]
            raise ProviderError(issue["code"], issue["message"], http_status=503)
        if not self._issues_override:
            engine_id = requested_engine(validated)
            # A source-text axis replaces recognition the way a cloud stable.json
            # does, so it clears the same tiers: the LLM half, not the ASR one.
            # A cloud API engine transcribes here, but with nothing of the ASR
            # stack: its own readiness (the `raw-srt` tier, keys included),
            # then the continuation's tiers.
            if validated["source"]["kind"] == "cloud_transcript" or "axis" in validated:
                readiness = CONTINUATION_READINESS[validated["target"]]
                if "axis" not in validated and _core_renders_raw_srt(validated, self.packages()):
                    # Nothing of the correction runs: the core renders the
                    # raw SRT from the cloud's transcript.json itself.
                    readiness = ()
            elif specs[engine_id].kind == "local-gpu":
                readiness = TARGET_READINESS[validated["target"]]
            elif _core_renders_raw_srt(validated, self.packages()):
                # No correction package: the core renders the raw SRT from the
                # engine's transcript.json, so the harness tier does not apply
                # (plan 4.6 item 33).
                readiness = ("raw-srt",)
            else:
                readiness = ("raw-srt", *CONTINUATION_READINESS[validated["target"]])
            context = ReadinessContext(
                target=validated["target"],
                language=validated.get("language"),
                device=validated.get("device"),
                options=dict(requested_options(validated)),
            )
            stages = {
                item["id"]: item
                for item in runtime_report(
                    self._settings, self._provisioner, engine=engine_id, context=context, packages=self.packages()
                )["stages"]
            }
            for stage_id in readiness:
                if validated.get("knowledge") == "update" and stage_id == "final-srt":
                    stage_id = "knowledge"
                # The knowledge package is judged below, for this request as the worker runs it.
                issues = [issue for issue in stages[stage_id]["issues"] if issue["code"] != "missing_knowledge_package"]
                if issues:
                    issue = issues[0]
                    raise ProviderError(issue["code"], issue["message"], http_status=503)
            # The worker refuses the same request the same way, with the same
            # check (plan 4.6 item 35a): a run that reads or writes the
            # knowledge base, or picks its ASR context from it, needs the
            # knowledge package -- after what a Chinese-source task cannot use
            # is taken out, as the worker takes it out (`worker.for_task_mode`).
            problem = self._knowledge_problem(validated)
            if problem:
                raise ProviderError("missing_knowledge_package", problem, http_status=503)
        task_id = uuid.uuid4().hex
        now = utc_now()
        snapshot = {
            "schema": 1,
            "task_id": task_id,
            "provider": "local",
            "state": "queued",
            "stage": "",
            "progress": None,
            "engine": {
                "version": str(_engine_upstream(self.packages()).get("engine_version") or ""),
                "commit": str(_engine_upstream(self.packages()).get("commit") or ""),
            },
            # The transcription engine the task runs on, for the desktop's task
            # list (WP6). Tasks written before this field carry none: FineSub.
            "transcription_engine": requested_engine(validated),
            "requested_capabilities": {
                "video_multimodal": validated["correction"].get("media") == "video",
                "target": validated["target"],
            },
            "effective_capabilities": {
                "video_multimodal": validated["correction"].get("media") == "video",
                "target": validated["target"],
            },
            "error": None,
            "last_cursor": 0,
            "created_at": now,
            "started_at": now,
            "updated_at": now,
        }
        task_dir = self._task_dir(task_id)
        _atomic_json(task_dir / "request.json", validated)
        _atomic_json(task_dir / "snapshot.json", snapshot)
        dropped = dropped_fields(request, validated)
        if dropped:
            from .transcription import requested_engine

            spec = specs[requested_engine(validated)]
            self._append_event(task_id, "log", {
                "message": f"{spec.label} 不用这些选项，已忽略：" + "、".join(dropped),
                "fields": {"engine": spec.id, "dropped": dropped},
            })
        self._spawn(task_id)
        return self.status(task_id)

    def status(self, task_id: str) -> dict[str, Any]:
        try:
            value = _read_json_when_free(self._task_dir(task_id) / "snapshot.json")
        except FileNotFoundError as exc:
            raise ProviderError("task_not_found", f"Unknown task: {task_id}", http_status=404) from exc
        if "started_at" not in value:
            value["started_at"] = value.get("created_at")
        return value

    def list_tasks(self, *, limit: int = 100) -> dict[str, Any]:
        if limit < 1 or limit > 500:
            raise ProviderError("invalid_request", "Task list limit must be between 1 and 500")
        records: list[dict[str, Any]] = []
        for snapshot_path in self.root.glob("*/snapshot.json"):
            try:
                snapshot = _read_json_when_free(snapshot_path)
                if "started_at" not in snapshot:
                    snapshot["started_at"] = snapshot.get("created_at")
                request_path = snapshot_path.with_name("request.json")
                request = _read_json_when_free(request_path) if request_path.is_file() else {}
                source = request.get("source") if isinstance(request, Mapping) else {}
                if not isinstance(source, Mapping):
                    source = {}
                records.append(
                    {
                        "snapshot": snapshot,
                        "media_id": str(source.get("video_id") or ""),
                        "title": str(source.get("title") or source.get("path") or snapshot.get("task_id") or ""),
                    }
                )
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                continue
        records.sort(key=lambda item: str(item["snapshot"].get("updated_at") or ""), reverse=True)
        return {"schema": 1, "tasks": records[:limit]}

    def events(self, task_id: str, after_cursor: int = 0, *, limit: int = 500) -> dict[str, Any]:
        snapshot = self.status(task_id)
        path = self._task_dir(task_id) / "events.jsonl"
        events: list[dict[str, Any]] = []
        if path.is_file():
            for line in path.read_text(encoding="utf-8").splitlines():
                event = json.loads(line)
                if int(event["cursor"]) > after_cursor:
                    events.append(event)
                    if len(events) >= limit:
                        break
        return {"schema": 1, "task_id": task_id, "events": events, "next_cursor": events[-1]["cursor"] if events else after_cursor, "state": snapshot["state"]}

    def artifacts(self, task_id: str) -> dict[str, Any]:
        self.status(task_id)
        path = self._task_dir(task_id) / "artifacts.json"
        if not path.is_file():
            raise ProviderError("artifacts_not_ready", "Task artifacts are not ready", http_status=409)
        return _read_json_when_free(path)

    def document(self, video_id: str) -> dict[str, Any]:
        if not _safe_component(video_id):
            raise ProviderError("invalid_document", "Invalid document id")
        try:
            return self.documents.read(video_id)
        except DocumentNotFound as exc:
            raise ProviderError("document_not_found", f"Unknown document: {video_id}", http_status=404) from exc

    def document_peaks(self, video_id: str) -> dict[str, Any]:
        self.document(video_id)
        try:
            value = _read_json_when_free(self.documents.directory(video_id) / "peaks.json")
        except FileNotFoundError as exc:
            raise ProviderError("peaks_not_found", "Waveform is not ready", http_status=404) from exc
        return value

    def _axis_path(self, video_id: str) -> Path:
        return self.documents.directory(video_id) / "axis.json"

    def _read_axis(self, video_id: str) -> dict[str, Any] | None:
        try:
            value = _read_json_when_free(self._axis_path(video_id))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return None
        try:
            return normalize_axis(value)
        except AxisError:
            return None

    def document_axis(self, video_id: str) -> dict[str, Any]:
        if not _safe_component(video_id):
            raise ProviderError("invalid_document", "Invalid document id")
        return {"schema": 1, "video_id": video_id, "axis": self._read_axis(video_id)}

    def set_document_axis(self, video_id: str, value: Mapping[str, Any]) -> dict[str, Any]:
        """Record (or clear) the axis a run on this video must land on.

        Kept beside the document rather than inside the TaskRequest because the
        projection is what consumes it, and a cloud run's artifacts are
        projected here too without its request ever reaching this process.
        """

        if not _safe_component(video_id) or not isinstance(value, Mapping):
            raise ProviderError("invalid_document", "Invalid axis payload")
        try:
            axis = normalize_axis(value.get("axis"))
        except AxisError as exc:
            raise ProviderError("invalid_axis", str(exc)) from exc
        if axis is None:
            self._axis_path(video_id).unlink(missing_ok=True)
        else:
            _atomic_json(self._axis_path(video_id), axis)
        return {"schema": 1, "video_id": video_id, "axis": axis}

    def import_axis(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Turn a finished axis into an EditDocument without running anything."""

        if not isinstance(payload, Mapping):
            raise ProviderError("invalid_document", "Invalid import payload")
        video_id = str(payload.get("video_id") or "")
        if not _safe_component(video_id):
            raise ProviderError("invalid_document", "Invalid document id")
        try:
            axis = normalize_axis(payload.get("axis"))
            if axis is None:
                raise AxisError("axis is required")
            projection = axis_projection(
                axis["rows"],
                kind=axis["kind"],
                video_id=video_id,
                title=str(payload.get("title") or video_id),
                source=str(payload.get("source_path") or ""),
                fingerprint=str(payload.get("fingerprint") or "") or None,
            )
        except AxisError as exc:
            raise ProviderError("invalid_axis", str(exc)) from exc
        document = self.documents.create(
            video_id, projection, replace_default=True, generated_track_prefixes=GENERATED_TRACK_PREFIXES
        )
        # Nothing was decoded for this video, so no waveform exists yet. The
        # editor's timeline falls back to a synthetic envelope without one,
        # which is exactly the case an import lands in most often.
        self._write_optional_peaks(
            video_id, str(payload.get("source_path") or ""), float(payload.get("duration") or 0)
        )
        self._axis_path(video_id).unlink(missing_ok=True)
        return document

    def save_document(self, video_id: str, value: Mapping[str, Any]) -> dict[str, Any]:
        if not _safe_component(video_id) or not isinstance(value, Mapping):
            raise ProviderError("invalid_document", "Invalid document payload")
        raw_effects = value.get("effects")
        if raw_effects is not None and not isinstance(raw_effects, list):
            raise ProviderError("invalid_document", "Document effects must be a list")
        raw_styles = value.get("styles")
        if raw_styles is not None and not isinstance(raw_styles, str):
            raise ProviderError("invalid_document", "Document styles must be a string")
        if raw_styles is not None and len(raw_styles.encode("utf-8")) > _MAX_STYLES_BYTES:
            raise ProviderError("invalid_document", "Document styles sheet is too large")
        try:
            return self.documents.save(
                video_id,
                expected_rev=int(value.get("rev", -1)),
                subtitles=list(value.get("subtitles") or []),
                tracks=list(value.get("tracks") or []),
                track_meta=value.get("track_meta") if isinstance(value.get("track_meta"), Mapping) else None,
                effects=list(raw_effects or []),
                title=str(value["title"]) if "title" in value else None,
                styles=raw_styles,
            )
        except RevisionConflict as exc:
            raise ProviderError("revision_conflict", str(exc), http_status=409) from exc

    def project_contents(self, value: Mapping[str, Any]) -> dict[str, Any]:
        video_id = str(value.get("video_id") or "")
        task_id = str(value.get("task_id") or "")
        artifacts = value.get("artifacts")
        if not _safe_component(video_id) or not _safe_component(task_id):
            raise ProviderError("invalid_document", "Invalid projection identifiers")
        if not isinstance(artifacts, Mapping) or not artifacts:
            raise ProviderError("invalid_artifacts", "Projection artifacts are required")
        directory = self.root / "cloud" / task_id
        directory.mkdir(parents=True, exist_ok=True)
        manifest = {"schema": 1, "task_id": task_id, "engine_commit": str(value.get("engine_commit") or ""), "artifacts": {}}
        for name, content in artifacts.items():
            if name not in ARTIFACT_NAMES or not isinstance(content, str):
                raise ProviderError("invalid_artifacts", f"Invalid artifact {name!r}")
        if not {"transcript_json", "correction_json"} & set(artifacts):
            raise ProviderError("invalid_artifacts", "Projection needs transcript_json or correction_json")
        for name, content in artifacts.items():
            path = directory / (name + ARTIFACT_SUFFIXES[name])
            path.write_text(content, encoding="utf-8")
            manifest["artifacts"][name] = {"uri": path.resolve().as_uri(), "bytes": path.stat().st_size}
        document = self._project_manifest(video_id, value, manifest)
        self._write_optional_peaks(video_id, str(value.get("source_path") or ""), float(value.get("duration") or 0))
        return document

    def llm_complete(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Run one routed LLM call on the user's configured models.

        The engine's own correction and translation calls go through the task
        worker; this is the same routing reached without a pipeline, for
        callers that have their own prompt. It deliberately exposes nothing
        about *how* the call was routed beyond the answering model's name: the
        keys, the endpoint chain and FineSub's prompt templates stay inside the
        engine.
        """

        request = _validate_llm_request(payload)
        if self._issues_override:
            if self._issues:
                issue = self._issues[0]
                raise ProviderError(issue["code"], issue["message"], http_status=503)
        else:
            stage = next(
                item for item in runtime_report(self._settings, self._provisioner)["stages"]
                if item["id"] == "final-srt"
            )
            # A call with its own prompt reads no style catalog: the knowledge package is the
            # correction's need, not this entry point's.
            issues = [issue for issue in stage["issues"] if issue["code"] != "missing_knowledge_package"]
            if issues:
                issue = issues[0]
                raise ProviderError(issue["code"], issue["message"], http_status=503)
        timeout = float(request.get("timeout_seconds", LLM_CALL_TIMEOUT_SEC))
        deadline = time.monotonic() + timeout
        # Waiting behind a call already in flight spends the same limit.
        if not self._llm_lock.acquire(timeout=timeout):
            raise _llm_timeout_error(timeout)
        try:
            response = self._llm_exchange(request, deadline)
        finally:
            self._llm_lock.release()
        if not response.get("ok"):
            error = response.get("error") if isinstance(response.get("error"), Mapping) else {}
            message = str(error.get("message") or "LLM call failed")
            raise ProviderError(str(error.get("code") or classify_failure(message)), message, http_status=502)
        return {
            "schema": 1,
            "content": str(response.get("content") or ""),
            "model": str(response.get("model") or ""),
            "backend": str(response.get("backend") or ""),
            "role": request["role"],
            "fallback_used": bool(response.get("fallback_used")),
        }

    def _llm_exchange(self, request: Mapping[str, Any], deadline: float) -> dict[str, Any]:
        """One `llm.complete` of the resident correction process, and its answer."""

        return self._exchange("llm.complete", {"request": dict(request)}, deadline,
                              timeout=float(request.get("timeout_seconds", LLM_CALL_TIMEOUT_SEC)))

    def _service_call(self, op: str, payload: Mapping[str, Any] | None = None, *, timeout: float = 120.0) -> Any:
        """One request of the resident correction process (its catalog, a task's speaker rows, a task's mode)."""

        deadline = time.monotonic() + timeout
        if not self._llm_lock.acquire(timeout=timeout):
            raise _llm_timeout_error(timeout)
        try:
            return self._exchange(op, dict(payload or {}), deadline, timeout=timeout)
        finally:
            self._llm_lock.release()

    def _exchange(self, op: str, payload: Mapping[str, Any], deadline: float, *, timeout: float) -> Any:
        """Send one request to the resident correction process and read until its answer.

        Caller holds `_llm_lock`. A process that died between calls -- the CLI
        it drives crashed, the machine slept -- is replaced once and the
        request retried, because the failure has nothing to do with this request.
        """

        for attempt in range(2):
            process = self._llm_worker()
            assert process.stdin is not None and process.stdout is not None
            try:
                process.stdin.write(json.dumps({"op": op, **payload}, ensure_ascii=False) + "\n")
                process.stdin.flush()
            except (BrokenPipeError, OSError, ValueError):
                self._stop_llm_worker()
                if attempt == 0:
                    continue
                raise ProviderError("llm_unavailable", "The LLM worker could not be started", http_status=503)
            while True:
                line = _read_line_with_timeout(process, max(0.0, deadline - time.monotonic()))
                if line is None:
                    self._stop_llm_worker()
                    raise _llm_timeout_error(timeout)
                if not line.strip():
                    break
                try:
                    value = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ProviderError("llm_protocol", "The LLM worker sent an unreadable response", http_status=502) from exc
                if not isinstance(value, dict):
                    raise ProviderError("llm_protocol", "The LLM worker sent an unreadable response", http_status=502)
                kind = value.get("type")
                if kind == "result":
                    self._schedule_llm_idle_stop()
                    return value.get("payload")
                if kind == "error":
                    self._schedule_llm_idle_stop()
                    error = value.get("payload") or {}
                    message = str(error.get("formatted") or error.get("message") or "engine request failed")
                    raise ProviderError("engine_request_failed", message, http_status=502)
                # A reporter call or a stray line: nothing the caller waits for.
            # EOF: the process is gone. Its stderr went to the sidecar's.
            self._stop_llm_worker()
            if attempt == 0:
                continue
            raise ProviderError("llm_unavailable", "The LLM worker stopped unexpectedly", http_status=503)
        raise ProviderError("llm_unavailable", "The LLM worker stopped unexpectedly", http_status=503)

    def _llm_worker(self) -> subprocess.Popen[str]:
        """The resident correction process: the correction package's entry, holding a routed client."""

        from . import engine_contexts, engine_host

        if self._closed:
            raise ProviderError("llm_unavailable", "The application is shutting down", http_status=503)
        process = self._llm_process
        if process is not None and process.poll() is None and not self._llm_config_stale:
            return process
        self._stop_llm_worker()
        self._llm_config_stale = False
        package = engine_host.correction_package(self.packages())
        if package is None and self._llm_worker_command is None:
            raise ProviderError("missing_engine", "纠错翻译包未安装", http_status=503)
        try:
            packages = {package.id: package} if package is not None else {}
            self._llm_run = engine_contexts.build(
                packages, paths=self._paths(), settings=self._settings, provisioner=self._provisioner,
                proxy=engine_contexts.proxy_context(),
            )
            context = self._llm_run.contexts.get(package.id) if package is not None else {}
            environment = self._worker_environment()
            cwd = None
            if package is not None:
                # Hermetic (plan 4.5 item 29): built from nothing, the parent
                # only supplying the system variables; its own run directory
                # as its working directory (owner H3).
                environment = engine_host.package_environment(package, context, environment)
                command = engine_host.package_command(package, engine_host.interpreter_for(package, context))
                cwd = engine_host.package_cwd(package, context)
            if self._llm_worker_command is not None:
                command = self._llm_worker_command()
        except OSError as exc:
            self._close_llm_run()
            raise ProviderError("llm_unavailable", f"The LLM worker could not be prepared: {exc}", http_status=503) from exc
        kwargs: dict[str, Any] = {
            "stdin": subprocess.PIPE,
            "stdout": subprocess.PIPE,
            "text": True,
            "encoding": "utf-8",
            "errors": "replace",
            "env": environment,
            "cwd": cwd,
        }
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        try:
            self._llm_process = subprocess.Popen(command, **kwargs)
            assert self._llm_process.stdin is not None
            self._llm_process.stdin.write(json.dumps({"context": context}, ensure_ascii=False) + "\n")
            self._llm_process.stdin.flush()
        except OSError as exc:
            self._close_llm_run()
            raise ProviderError("llm_unavailable", f"The LLM worker could not be started: {exc}", http_status=503) from exc
        return self._llm_process

    def _schedule_llm_idle_stop(self) -> None:
        if self._llm_idle_timer is not None:
            self._llm_idle_timer.cancel()
        timer = threading.Timer(LLM_WORKER_IDLE_SEC, self._stop_idle_llm_worker)
        timer.daemon = True
        self._llm_idle_timer = timer
        timer.start()

    def _stop_idle_llm_worker(self) -> None:
        # Only when nothing is mid-call: the lock is the same one `llm_complete`
        # holds for the length of an exchange.
        if self._llm_lock.acquire(blocking=False):
            try:
                self._stop_llm_worker()
            finally:
                self._llm_lock.release()

    def _stop_llm_worker(self) -> None:
        if self._llm_idle_timer is not None:
            self._llm_idle_timer.cancel()
            self._llm_idle_timer = None
        process, self._llm_process = self._llm_process, None
        if process is None:
            self._close_llm_run()
            return
        self._close_llm_stream(process.stdin)
        if process.poll() is None:
            self._terminate(process)
        # Last: closing stdout under a timed-out reader blocks until that read
        # returns, which is only once the process is gone.
        self._close_llm_stream(process.stdout)
        self._close_llm_run()

    def _close_llm_run(self) -> None:
        run, self._llm_run = self._llm_run, None
        if run is not None:
            run.close()

    @staticmethod
    def _close_llm_stream(stream: Any) -> None:
        if stream is not None and not stream.closed:
            try:
                stream.close()
            except OSError:
                pass

    def cancel(self, task_id: str) -> dict[str, Any]:
        with self._lock:
            snapshot = self.status(task_id)
            if snapshot["state"] in TERMINAL:
                return snapshot
            self._set_state(task_id, "cancelled")
            self._append_event(task_id, "cancelled", {})
            process = self._processes.get(task_id)
            if process is not None:
                self._terminate(process)
        return self.status(task_id)

    def retry(self, task_id: str) -> dict[str, Any]:
        return self._restart(task_id, {"failed", "cancelled"})

    def resume(self, task_id: str) -> dict[str, Any]:
        return self._restart(task_id, {"interrupted"})

    def shutdown(self) -> None:
        self._closed = True
        self._stop_llm_worker()
        with self._lock:
            for task_id, process in list(self._processes.items()):
                if process.poll() is None:
                    self._set_state(task_id, "interrupted")
                    self._append_event(task_id, "warning", {"code": "app_shutdown", "message": "Task interrupted by application shutdown"})
                    self._terminate(process)
            threads = list(self._threads.values())
        for thread in threads:
            if thread is not threading.current_thread():
                thread.join(timeout=6)

    def _restart(self, task_id: str, allowed: set[str]) -> dict[str, Any]:
        with self._lock:
            snapshot = self.status(task_id)
            if snapshot["state"] not in allowed:
                raise ProviderError("invalid_state", f"Cannot restart task in state {snapshot['state']}", http_status=409)
            now = utc_now()
            self._set_state(task_id, "queued", error=None, started_at=now)
            self._spawn(task_id)
        return self.status(task_id)

    def _spawn(self, task_id: str) -> None:
        try:
            request = _read_json_when_free(self._task_dir(task_id) / "request.json")
            context = self.telemetry.context(self._settings, request)
            _atomic_json(self._task_dir(task_id) / "telemetry-run.json", context)
        except Exception:
            pass
        thread = threading.Thread(target=self._run_worker, args=(task_id,), daemon=True, name=f"nonoka-{task_id[:8]}")
        with self._lock:
            self._threads[task_id] = thread
        thread.start()

    def _run_worker(self, task_id: str) -> None:
        task_dir = self._task_dir(task_id)
        request = _read_json_when_free(task_dir / "request.json")
        command = self._worker_command(task_id, task_dir)
        process: subprocess.Popen[str] | None = None
        try:
            # The card a `cuda:N` request names is the transcription process's
            # own `CUDA_VISIBLE_DEVICES`, written by the engine host when it
            # starts that process (plan 4.5 item 27); the worker uses no card.
            environment = self._worker_environment()
            kwargs: dict[str, Any] = {"stdin": subprocess.PIPE, "stdout": subprocess.PIPE, "stderr": subprocess.STDOUT, "text": True, "encoding": "utf-8", "errors": "replace", "env": environment}
            if os.name == "nt":
                kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
            else:
                kwargs["start_new_session"] = True
            process = subprocess.Popen(command, **kwargs)
            with self._lock:
                self._processes[task_id] = process
                if self.status(task_id)["state"] == "cancelled":
                    self._terminate(process)
                    return
                self._set_state(task_id, "running")
                self._append_event(task_id, "started", {})
            assert process.stdin is not None and process.stdout is not None
            process.stdin.write(json.dumps(request, ensure_ascii=False) + "\n")
            process.stdin.close()
            completed = False
            failure_message = ""
            for raw_line in process.stdout:
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    body = json.loads(line)
                    event_type = str(body["type"])
                    payload = body.get("payload") if isinstance(body.get("payload"), dict) else {}
                except (json.JSONDecodeError, KeyError, TypeError):
                    event_type, payload = "log", {"message": line}
                if event_type == "log":
                    msg = str(payload.get("message") or "")
                    if any(ignored in msg for ignored in _IGNORED_WORKER_LOG_SUBSTRINGS):
                        continue
                with self._lock:
                    current = self.status(task_id)
                    if current["state"] in {"cancelled", "interrupted"}:
                        continue
                    if event_type in {"stage", "progress"}:
                        self._update_progress(task_id, event_type, payload)
                    elif event_type == "completed":
                        manifest = payload.get("artifacts")
                        if isinstance(manifest, dict):
                            _atomic_json(task_dir / "artifacts.json", manifest)
                            video_id = str(request.get("source", {}).get("video_id") or "")
                            # `projection: none` runs the stages and stops: the
                            # projection replaces the video's default document,
                            # which is not something a caller who asked for one
                            # stage's artifacts is asking for.
                            if video_id and request.get("projection", "document") != "none":
                                self._project_manifest(video_id, request.get("source", {}), manifest)
                                self._write_optional_peaks(
                                    video_id,
                                    str(request.get("source", {}).get("path") or ""),
                                    float(request.get("source", {}).get("duration") or 0),
                                )
                        completed = True
                    elif event_type == "failed":
                        failure_message = str(payload.get("message") or "FineSub worker failed")
                    self._append_event(task_id, event_type if event_type in {"started", "stage", "progress", "warning", "log", "completed", "failed", "cancelled"} else "log", payload)
            return_code = process.wait()
            with self._lock:
                state = self.status(task_id)["state"]
                if state not in {"cancelled", "interrupted"}:
                    if completed and return_code == 0:
                        self._set_state(task_id, "completed")
                    else:
                        message = failure_message or f"FineSub worker exited with code {return_code}"
                        self._set_state(task_id, "failed", error={"code": classify_failure(message), "message": message})
                        if not failure_message:
                            self._append_event(task_id, "failed", {"message": message})
        except Exception as exc:
            with self._lock:
                if self.status(task_id)["state"] not in {"cancelled", "interrupted"}:
                    message = f"{type(exc).__name__}: {exc}"
                    self._set_state(task_id, "failed", error={"code": classify_failure(message), "message": message})
                    self._append_event(task_id, "failed", {"message": message})
        finally:
            if process is not None:
                for stream in (process.stdin, process.stdout):
                    if stream is not None and not stream.closed:
                        stream.close()
            with self._lock:
                self._processes.pop(task_id, None)
                self._threads.pop(task_id, None)

    def _project_manifest(
        self,
        video_id: str,
        metadata: Mapping[str, Any],
        manifest: Mapping[str, Any],
    ) -> dict[str, Any]:
        if not _safe_component(video_id):
            raise ProviderError("invalid_document", "Invalid document id")
        if manifest.get("projection_error"):
            # The correction ran, but its products could not be lined up into
            # its correction.json: projecting the transcript instead would
            # quietly drop everything the correction did.
            raise ProjectionError(str(manifest["projection_error"]).removeprefix("ProjectionError: "))
        entries = manifest.get("artifacts")
        if not isinstance(entries, Mapping) or not {"transcript_json", "correction_json"} & set(entries):
            raise ProjectionError("the task's manifest has neither correction_json nor transcript_json")

        if "correction_json" not in entries and set(entries) - ARTIFACT_NAMES:
            manifest = self._described(manifest)
            entries = manifest["artifacts"]

        def artifact_path(name: str) -> Path | None:
            item = entries.get(name)
            if not isinstance(item, Mapping):
                return None
            parsed = urlparse(str(item.get("uri") or ""))
            if parsed.scheme != "file" or parsed.netloc:
                raise ProjectionError(f"{name} is not a local file artifact")
            path = Path(url2pathname(unquote(parsed.path))).resolve()
            relative = path.relative_to(self.root)
            if not relative.parts:
                raise ProjectionError(f"{name} has an invalid artifact path")
            return path

        projection = project_edit_document(
            transcript_json=artifact_path("transcript_json"),
            correction_json=artifact_path("correction_json"),
            video_id=video_id,
            title=str(metadata.get("title") or video_id),
            source=str(metadata.get("path") or metadata.get("source_path") or ""),
            fingerprint=str(metadata.get("fingerprint") or "") or None,
        )
        # An imported axis outranks the engine's own segmentation: every line
        # the user timed comes back with its exact timing, whether the run was
        # local or a cloud task whose artifacts are being projected here.
        axis = self._read_axis(video_id)
        if axis is not None and axis["kind"] in {"empty", "ja"}:
            try:
                projection = conform_to_axis(projection, axis["rows"])
            except AxisError as exc:
                raise ProjectionError(f"cannot apply the imported axis: {exc}") from exc
        else:
            # No axis: the engine's speaker labels, when the run made any,
            # are the only account of who says what.
            projection = split_diarized_tracks(projection)
        if projection["projection"].get("correct_only"):
            projection = hide_source_lanes(projection)
        return self.documents.create(
            video_id,
            projection,
            artifacts=manifest,
            replace_default=True,
            generated_track_prefixes=GENERATED_TRACK_PREFIXES,
        )

    def _described(self, manifest: Mapping[str, Any]) -> Mapping[str, Any]:
        """The manifest of a task no correction took further, with the correction engine's `correction.json` added.

        A target up to `stable` runs no correction, but its document was always
        the correction package's reading of the transcription's own files
        (FineSub's speaker rows, which only that package reads): asked here,
        when the task is projected, as the projection asked for those rows
        before WP-L. The file joins the task's `artifacts.json`, so a sync
        finds it. No correction package, or nothing more to say than the
        transcript: the manifest as it was, and the transcript is projected.
        """

        task_id = str(manifest.get("task_id") or "")
        if not task_id or not all(char in "0123456789abcdef" for char in task_id):
            return manifest
        from . import engine_host

        if engine_host.correction_package(self.packages()) is None:
            return manifest
        task_dir = self._task_dir(task_id)
        artifacts = {
            name: url2pathname(unquote(urlparse(str(item.get("uri") or "")).path))
            for name, item in (manifest.get("artifacts") or {}).items() if isinstance(item, Mapping)
        }
        try:
            answer = self._service_call("describe", {"artifacts": artifacts, "output": str(task_dir / "correction.json")})
        except ProviderError as exc:
            raise ProjectionError(f"cannot read the task's speaker rows: {exc}") from exc
        answer = answer if isinstance(answer, Mapping) else {}
        if answer.get("correction_error"):
            raise ProjectionError(str(answer["correction_error"]).removeprefix("ProjectionError: "))
        if not answer.get("correction_json"):
            return manifest
        path = Path(str(answer["correction_json"])).resolve()
        described = {
            **manifest,
            "artifacts": {
                **manifest["artifacts"],
                "correction_json": {"uri": path.as_uri(), "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                                    "bytes": path.stat().st_size},
            },
        }
        if (task_dir / "artifacts.json").is_file():
            _atomic_json(task_dir / "artifacts.json", described)
        return described

    def _write_optional_peaks(self, video_id: str, source: str, duration: float) -> None:
        if not source or not Path(source).is_file():
            return
        try:
            executable = self._provisioner.tool_path("ffmpeg") if self._provisioner is not None else None
            self.documents.write_peaks(video_id, generate_peaks(source, duration, executable=executable))
        except Exception:
            return

    def _task_dir(self, task_id: str) -> Path:
        if not task_id or not all(char in "0123456789abcdef" for char in task_id):
            raise ProviderError("task_not_found", f"Unknown task: {task_id}", http_status=404)
        return self.root / task_id

    def _set_state(self, task_id: str, state: str, *, error: Any = ..., started_at: str | None = None) -> None:
        if state not in STATES:
            raise ValueError(state)
        snapshot = self.status(task_id)
        snapshot["state"] = state
        if started_at is not None:
            snapshot["started_at"] = started_at
        if state == "completed":
            if snapshot.get("progress") and isinstance(snapshot["progress"], dict):
                if snapshot["progress"].get("message") in {"正在处理", "处理中"}:
                    snapshot["progress"]["message"] = "字幕已完成"
        if error is not ...:
            snapshot["error"] = error
        snapshot["updated_at"] = utc_now()
        _atomic_json(self._task_dir(task_id) / "snapshot.json", snapshot)
        self._telemetry_state(task_id, snapshot)

    def _telemetry_state(self, task_id: str, snapshot: dict) -> None:
        from .telemetry import safe_error, classify_error

        try:
            path = self._task_dir(task_id) / "telemetry-run.json"
            context = _read_json_when_free(path)
            if not context:
                return
            kind = {"running": "task_model_config", "failed": "task_error"}.get(snapshot["state"])
            if not kind or context.get(kind):
                return
            clean = {k: context[k] for k in ("sample_id", "models", "execution_provider")}
            failure = snapshot.get("error") or {}
            code = classify_error(failure.get("code", "unknown"), failure.get("message", ""))
            error = safe_error(code, "execution", snapshot.get("stage", "unknown")) if kind == "task_error" else None
            if not self.telemetry.emit(clean, kind, error=error, at=snapshot["updated_at"]):
                return
            context[kind] = True
            _atomic_json(path, context)
        except Exception:
            pass

    def _update_progress(self, task_id: str, event_type: str, payload: Mapping[str, Any]) -> None:
        snapshot = self.status(task_id)
        if payload.get("stage"):
            snapshot["stage"] = payload["stage"]
        snapshot["progress"] = {
            "completed": payload.get("completed", 0),
            "total": payload.get("total"),
            "unit": payload.get("unit", ""),
            "message": payload.get("message") or payload.get("detail", ""),
        }
        snapshot["updated_at"] = utc_now()
        _atomic_json(self._task_dir(task_id) / "snapshot.json", snapshot)

    def _append_event(self, task_id: str, event_type: str, payload: Mapping[str, Any]) -> None:
        snapshot = self.status(task_id)
        cursor = int(snapshot.get("last_cursor", 0)) + 1
        event = {"cursor": cursor, "task_id": task_id, "type": event_type, "timestamp": utc_now(), "payload": dict(payload)}
        path = self._task_dir(task_id) / "events.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")
            stream.flush()
        snapshot["last_cursor"] = cursor
        snapshot["updated_at"] = event["timestamp"]
        _atomic_json(self._task_dir(task_id) / "snapshot.json", snapshot)

    def _recover_interrupted(self) -> None:
        for snapshot_path in self.root.glob("*/snapshot.json"):
            try:
                snapshot = _read_json_when_free(snapshot_path)
                task_id = str(snapshot["task_id"])
                if snapshot.get("state") in {"failed", "running"}:
                    self._telemetry_state(task_id, snapshot)
                if snapshot.get("state") in {"queued", "running"}:
                    snapshot["state"] = "interrupted"
                    snapshot["updated_at"] = utc_now()
                    _atomic_json(snapshot_path, snapshot)
                    self._append_event(task_id, "warning", {"code": "process_recovered", "message": "Task was interrupted before sidecar restart"})
            except Exception:
                continue

    @staticmethod
    def _terminate(process: subprocess.Popen[str]) -> None:
        if process.poll() is not None:
            return
        try:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=10,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            else:
                os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=10 if os.name == "nt" else 5)
        except (OSError, subprocess.SubprocessError):
            try:
                if os.name == "nt":
                    process.kill()
                else:
                    os.killpg(process.pid, signal.SIGKILL)
            except OSError:
                pass
