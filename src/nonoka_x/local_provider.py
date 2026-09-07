"""Persistent headless Local ExecutionProvider and isolated worker lifecycle."""

from __future__ import annotations

import json
import importlib.util
import locale
import os
import shutil
import signal
import subprocess
import sys
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import unquote, urlparse
from urllib.request import url2pathname

from .axis import AxisError, axis_projection, conform_to_axis, normalize_axis
from .document_store import (
    DocumentNotFound,
    DocumentStore,
    RevisionConflict,
    _atomic_json,
    _read_json_when_free,
)
from .peaks import generate_peaks
from .projector import ProjectionError, project_edit_document
from .provision import RuntimeProvisionError, RuntimeProvisioner
from .settings import (
    FineSubSettings,
    LOCAL_AGENT_PROVIDERS,
    local_agent_executable,
    local_agent_path_entries,
)


STATES = {"queued", "running", "completed", "failed", "cancelled", "interrupted"}
TERMINAL = {"completed", "failed", "cancelled"}
ARTIFACT_NAMES = {"stable_json", "raw_srt", "annotated_csv", "final_srt"}

# FineSub's own `stages.PIPELINE_STAGE_ORDER`, restated because this process
# cannot import the engine -- it runs on the bootstrap interpreter, while
# finesub lives in the managed venv the worker is spawned into. A sync that
# changes the engine's stage list has to change this one; `tests/
# test_vendor_contract.py` is where that pairing is checked.
PIPELINE_TARGETS = ("vocal", "aligned", "stable", "raw-srt", "translated-srt", "final-srt")
# Which readiness stage of `runtime_report` a target has to clear. Everything
# up to the raw SRT is ASR work; the two LLM stages need a configured model.
TARGET_READINESS = {
    "vocal": "raw-srt",
    "aligned": "raw-srt",
    "stable": "raw-srt",
    "raw-srt": "raw-srt",
    "translated-srt": "final-srt",
    "final-srt": "final-srt",
}
# The text-only roles of `finesub.llm.routing.config.LLMRole`. The two
# multimodal roles are omitted deliberately: they exist to put a clip in front
# of a model, and this entry point has no way to pass one, so asking for them
# would only route text at a more expensive model.
LLM_ROLES = ("lightweight", "general_capable")
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


def _load_upstream(vendor: Path) -> dict[str, Any]:
    try:
        value = json.loads((vendor / "UPSTREAM.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProviderError("missing_engine", f"FineSub snapshot is unavailable: {exc}", http_status=503) from exc
    if not isinstance(value, dict) or not value.get("commit"):
        raise ProviderError("invalid_engine", "FineSub UPSTREAM.json is invalid", http_status=503)
    return value


def _issue(code: str, message: str) -> dict[str, str]:
    return {"code": code, "message": message}


_IGNORED_WORKER_LOG_SUBSTRINGS = (
    "CUDAExecutionProvider not available in ONNXruntime",
)


def _clear_legacy_separator_decode_probes(environment: Mapping[str, str]) -> None:
    """Retry AOT builds whose recorded failure was a fixed toolchain or locale bug."""

    models_root = environment.get("FINESUB_MODEL_DIR")
    if not models_root:
        return
    accel_root = Path(models_root).expanduser() / "audio-separator" / "accel"
    try:
        probes = tuple(accel_root.glob("*/probe.json"))
    except OSError:
        return
    for probe in probes:
        try:
            value = json.loads(probe.read_text(encoding="utf-8"))
            reason = str(value.get("reason") or "") if isinstance(value, Mapping) else ""
            if isinstance(value, Mapping) and value.get("aoti") == "unavailable":
                if (
                    ("UnicodeDecodeError" in reason and "utf-8" in reason)
                    or "array" in reason
                    or "C1083" in reason
                    or "C2059" in reason
                    or "__triton_launcher" in reason
                    or "autotuning" in reason
                ):
                    probe.unlink(missing_ok=True)
        except (OSError, json.JSONDecodeError):
            continue


def _prepare_msvc_environment(
    environment: dict[str, str],
    *,
    _run: Callable[..., Any] = subprocess.run,
    _windows: bool = os.name == "nt",
) -> None:
    """Populate MSVC and Windows SDK variables when only cl.exe is on PATH."""

    if not _windows:
        return
    include_dirs = [Path(item) for item in environment.get("INCLUDE", "").split(os.pathsep) if item]
    if any((directory / "array").is_file() for directory in include_dirs):
        return

    program_files_x86 = environment.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    vswhere = Path(program_files_x86) / "Microsoft Visual Studio" / "Installer" / "vswhere.exe"
    if not vswhere.is_file():
        return
    try:
        located = _run(
            [
                str(vswhere),
                "-latest",
                "-products",
                "*",
                "-requires",
                "Microsoft.VisualStudio.Component.VC.Tools.x86.x64",
                "-property",
                "installationPath",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            env=environment,
        )
        roots = located.stdout.strip().splitlines() if located.returncode == 0 else []
        if not roots:
            return
        vcvars = Path(roots[0]) / "VC" / "Auxiliary" / "Build" / "vcvars64.bat"
        if not vcvars.is_file():
            return
        activated = _run(
            f'call "{vcvars}" >nul && set',
            shell=True,
            capture_output=True,
            text=True,
            encoding=locale.getencoding(),
            errors="replace",
            timeout=30,
            env=environment,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if activated.returncode != 0:
            return
    except (OSError, subprocess.SubprocessError):
        return

    for line in activated.stdout.splitlines():
        key, separator, value = line.partition("=")
        if separator and key:
            environment[key] = value


def runtime_report(settings: FineSubSettings | None = None, provisioner: RuntimeProvisioner | None = None) -> dict[str, Any]:
    managed = provisioner.status() if provisioner is not None else None
    managed_runtime_ready = managed is not None and managed["runtime"].get("state") == "ready"
    media_issues: list[dict[str, str]] = []
    if sys.version_info < (3, 12) and not managed_runtime_ready:
        media_issues.append(_issue("missing_python", "需要 Python 3.12 或更高版本"))
    managed_ffmpeg = provisioner.tool_path("ffmpeg") if provisioner is not None else None
    managed_ffprobe = provisioner.tool_path("ffprobe") if provisioner is not None else None
    if not (shutil.which("ffmpeg") or managed_ffmpeg) or not (shutil.which("ffprobe") or managed_ffprobe):
        media_issues.append(_issue("missing_ffmpeg", "缺少项目必备依赖 FFmpeg 或 FFprobe"))

    asr_issues = list(media_issues)
    if sys.platform != "win32":
        asr_issues.append(_issue("unsupported_platform", "本地 GPU 流水线当前仅支持 Windows x64/NVIDIA"))
    elif not shutil.which("nvidia-smi"):
        asr_issues.append(_issue("missing_gpu", "未检测到 NVIDIA 驱动或 GPU"))
    missing_modules = [
        name for name in ("torch", "faster_whisper", "audio_separator")
        if importlib.util.find_spec(name) is None
    ]
    if missing_modules and not managed_runtime_ready:
        asr_issues.append(_issue("missing_runtime", "AI 运行时缺少：" + "、".join(missing_modules)))
    if managed is not None:
        missing_models = [
            str(item.get("id"))
            for item in managed.get("models", [])
            if item.get("state") != "ready"
        ]
        if missing_models:
            asr_issues.append(_issue("missing_model", "ASR 所需模型尚未安装：" + "、".join(missing_models)))

    settings_snapshot = settings.snapshot() if settings is not None else {"llmReady": False, "retrievalKeyConfigured": False}
    # A routable provider first — the Codex app hides its CLI in a directory
    # PATH never sees, so a plain `which` reports "no agent" on a machine that
    # has one. The remaining vendors have no desktop route yet, so PATH is the
    # whole question for them.
    local_agent = next(
        (
            LOCAL_AGENT_PROVIDERS[provider]["command"]
            for provider in LOCAL_AGENT_PROVIDERS
            if local_agent_executable(provider) is not None
        ),
        "",
    ) or next((name for name in ("codex", "claude", "agy") if shutil.which(name)), "")
    llm_issues = list(asr_issues)
    # 装了 CLI 不等于配好了模型：只有设置里保存下来的全局模型（提供商 + 模型，
    # 且凭据或 CLI 到位）才放行 LLM 环节。
    if not settings_snapshot.get("llmReady"):
        llm_issues.append(_issue("missing_llm_key", "尚未配置模型提供商，请在设置里选择提供商与全局模型并保存"))

    knowledge_issues = list(llm_issues)
    managed_resources = {item["id"]: item for item in managed.get("resources", [])} if managed else {}
    managed_git = managed_resources.get("git", {}).get("state") in {"ready", "outdated"}
    if not shutil.which("git") and not managed_git:
        knowledge_issues.append(_issue("missing_git", "知识库自动更新需要 Git"))

    def stage(stage_id: str, label: str, issues: list[dict[str, str]]) -> dict[str, Any]:
        return {"id": stage_id, "label": label, "ready": not issues, "issues": issues}

    stages = [
        stage("media", "媒体探测", media_issues),
        stage("raw-srt", "生成原始字幕", asr_issues),
        stage("final-srt", "纠错与翻译", llm_issues),
        stage("knowledge", "知识库更新", knowledge_issues),
        stage("video-multimodal", "视频多模态纠错", llm_issues),
    ]
    return {
        "ready": not asr_issues,
        "issues": asr_issues,
        "stages": stages,
        "localAgent": local_agent,
        "managed": managed,
    }


def runtime_issues() -> list[dict[str, str]]:
    return runtime_report()["issues"]


def detect_devices() -> list[dict[str, Any]]:
    executable = shutil.which("nvidia-smi")
    if not executable:
        return []
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
        return []
    devices: list[dict[str, Any]] = []
    for line in result.stdout.splitlines():
        fields = [field.strip() for field in line.split(",", 2)]
        if len(fields) != 3:
            continue
        try:
            devices.append({"id": f"cuda:{int(fields[0])}", "name": fields[1], "memory_mb": int(fields[2])})
        except ValueError:
            continue
    return devices


def capabilities(
    vendor: Path,
    *,
    issues: list[dict[str, str]] | None = None,
    settings: FineSubSettings | None = None,
    provisioner: RuntimeProvisioner | None = None,
) -> dict[str, Any]:
    upstream = _load_upstream(vendor)
    runtime = runtime_report(settings, provisioner)
    if issues is not None:
        runtime["issues"] = issues
        runtime["ready"] = not issues
    return {
        "provider": "local",
        "adapter_schema": 1,
        "artifact_schema": 1,
        "engine": {
            "name": "finesub",
            "version": upstream["engine_version"],
            "commit": upstream["commit"],
            "bundle_id": upstream["engine_bundle_id"],
        },
        "features": {
            "raw_srt": True,
            "translation": True,
            "video_multimodal": True,
            "knowledge": True,
            "resume": True,
            "diarization": False,
        },
        "devices": detect_devices(),
        "runtime": runtime,
        "settings": {
            "llm_key_configured": bool(settings.snapshot().get("llmKeyConfigured")) if settings else False
        },
    }


def validate_request(value: Mapping[str, Any]) -> dict[str, Any]:
    request = json.loads(json.dumps(value))
    if request.get("schema") != 1:
        raise ProviderError("unsupported_schema", "TaskRequest.schema must be 1")
    if request.get("provider") != "local":
        raise ProviderError("unsupported_provider", "Local Provider only accepts provider=local")
    source = request.get("source")
    if not isinstance(source, dict) or source.get("kind") != "local_file":
        raise ProviderError("invalid_source", "Local Provider requires source.kind=local_file")
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
    vocal_profile = request.setdefault("vocal_profile", "quality")
    if vocal_profile != "quality":
        raise ProviderError(
            "invalid_request", "Local Provider only supports vocal_profile=quality"
        )
    request.setdefault("knowledge", "update")
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
    elif axis["kind"] != "ja":
        raise ProviderError(
            "invalid_axis", f"axis.kind={axis['kind']} does not start a task; import it instead"
        )
    elif request["target"] != "final-srt":
        raise ProviderError("invalid_axis", "a source-text axis only runs the final-srt target")
    else:
        request["axis"] = axis
    return request


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
    return {
        "role": role,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": float(temperature),
    }


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


def _safe_component(value: str) -> bool:
    return bool(value) and len(value) <= 80 and all(character.isalnum() or character in "_-" for character in value)


def classify_failure(message: str) -> str:
    lowered = message.lower()
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
        vendor: str | Path,
        *,
        worker_command: WorkerCommand | None = None,
        llm_worker_command: Callable[[], list[str]] | None = None,
        issues: list[dict[str, str]] | None = None,
        settings: FineSubSettings | None = None,
        provisioner: RuntimeProvisioner | None = None,
    ) -> None:
        self.root = Path(root).expanduser().resolve()
        self.vendor = Path(vendor).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._upstream = _load_upstream(self.vendor)
        self._issues_override = issues is not None
        self._issues = runtime_issues() if issues is None else issues
        self._settings = settings
        self._provisioner = provisioner
        self.documents = DocumentStore(self.root.parent / "documents")
        self._worker_command = worker_command or self._default_worker_command
        self._llm_worker_command = llm_worker_command or self._default_llm_worker_command
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
        # Set once by `shutdown`. Without it, killing the worker out from under
        # an in-flight call reads as "the worker died" and the retry starts a
        # new one -- an orphan process, spawned while the app is closing.
        self._closed = False
        self._separator_probe_checked = False
        self._recover_interrupted()

    def _default_worker_command(self, task_id: str, task_dir: Path) -> list[str]:
        managed_python = self._provisioner.worker_python() if self._provisioner is not None else None
        return [
            str(managed_python or sys.executable),
            "-m",
            "nonoka_x.worker",
            "--task-id",
            task_id,
            "--task-dir",
            str(task_dir),
            "--vendor",
            str(self.vendor),
        ]

    def _default_llm_worker_command(self) -> list[str]:
        managed_python = self._provisioner.worker_python() if self._provisioner is not None else None
        return [str(managed_python or sys.executable), "-m", "nonoka_x.llm_worker"]

    def _engine_environment(self) -> dict[str, str]:
        """The environment every process that imports FineSub is spawned into.

        Shared by the task worker and the LLM worker rather than written twice:
        the two would drift, and each item here is a bug someone already had to
        find -- an agent CLI that PATH cannot see, the vendored engine that is
        not installed anywhere, MSVC output in the ANSI code page.
        """

        environment = self._provisioner.worker_environment() if self._provisioner is not None else os.environ.copy()
        # The engine resolves an agent CLI by name off PATH, so an install that
        # is not on PATH has to be put there for the worker.
        agent_dirs = local_agent_path_entries()
        if agent_dirs:
            environment["PATH"] = os.pathsep.join([*agent_dirs, environment.get("PATH", "")])
        project_src = Path(__file__).resolve().parents[1]
        python_paths = [str(project_src), str(self.vendor / "src")]
        if environment.get("PYTHONPATH"):
            python_paths.append(environment["PYTHONPATH"])
        environment["PYTHONPATH"] = os.pathsep.join(python_paths)
        # The worker's own NDJSON must be UTF-8, but stderr also carries native
        # compiler and library output in the Windows ANSI code page. Keep the
        # Python standard streams on UTF-8 while leaving locale-based subprocess
        # decoding on that native code page. PYTHONUTF8=1 would make PyTorch
        # decode MSVC's GBK diagnostics as UTF-8 during AOT compilation.
        environment["PYTHONUTF8"] = "0"
        environment["PYTHONIOENCODING"] = "utf-8"
        _prepare_msvc_environment(environment)
        with self._lock:
            if not self._separator_probe_checked:
                _clear_legacy_separator_decode_probes(environment)
                self._separator_probe_checked = True
        return environment

    def get_capabilities(self) -> dict[str, Any]:
        issues = self._issues if self._issues_override else runtime_report(self._settings, self._provisioner)["issues"]
        return capabilities(self.vendor, issues=issues, settings=self._settings, provisioner=self._provisioner)

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

    def remove_runtime_group(self, group: str) -> dict[str, Any]:
        if self._provisioner is None:
            raise ProviderError("runtime_unavailable", "FineSub runtime installer is unavailable", http_status=503)
        with self._lock:
            if any(process.poll() is None for process in self._processes.values()):
                raise ProviderError("runtime_in_use", "有本地任务正在运行，请先停止任务再卸载工具", http_status=409)
        try:
            return self._provisioner.remove_tool_group(group)
        except RuntimeProvisionError as exc:
            raise ProviderError("runtime_remove_unavailable", str(exc), http_status=409) from exc
        except OSError as exc:
            raise ProviderError("runtime_remove_failed", f"卸载工具失败：{exc}", http_status=409) from exc

    def get_settings(self) -> dict[str, Any]:
        if self._settings is None:
            raise ProviderError("settings_unavailable", "FineSub settings are unavailable", http_status=503)
        return self._settings.snapshot()

    def update_keys(self, updates: Mapping[str, Any]) -> dict[str, Any]:
        if self._settings is None:
            raise ProviderError("settings_unavailable", "FineSub settings are unavailable", http_status=503)
        if not isinstance(updates, Mapping):
            raise ProviderError("invalid_request", "keys must be an object")
        try:
            return self._settings.update_keys(updates)
        except ValueError as exc:
            raise ProviderError("invalid_settings", str(exc)) from exc

    def start(self, request: Mapping[str, Any]) -> dict[str, Any]:
        validated = validate_request(request)
        if self._issues_override and self._issues:
            issue = self._issues[0]
            raise ProviderError(issue["code"], issue["message"], http_status=503)
        if not self._issues_override:
            stage_id = TARGET_READINESS[validated["target"]]
            if validated.get("knowledge") == "update" and stage_id == "final-srt":
                stage_id = "knowledge"
            stage = next(
                item for item in runtime_report(self._settings, self._provisioner)["stages"]
                if item["id"] == stage_id
            )
            if not stage["ready"]:
                issue = stage["issues"][0]
                raise ProviderError(issue["code"], issue["message"], http_status=503)
        task_id = uuid.uuid4().hex
        now = utc_now()
        snapshot = {
            "schema": 1,
            "task_id": task_id,
            "provider": "local",
            "state": "queued",
            "stage": "",
            "progress": None,
            "engine": {"version": self._upstream["engine_version"], "commit": self._upstream["commit"]},
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
        document = self.documents.create(video_id, projection, replace_default=True)
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
        try:
            return self.documents.save(
                video_id,
                expected_rev=int(value.get("rev", -1)),
                subtitles=list(value.get("subtitles") or []),
                tracks=list(value.get("tracks") or []),
                track_meta=value.get("track_meta") if isinstance(value.get("track_meta"), Mapping) else None,
                effects=list(raw_effects or []),
                title=str(value["title"]) if "title" in value else None,
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
        suffixes = {"stable_json": ".stable.json", "raw_srt": ".raw.srt", "annotated_csv": ".annotated.csv", "final_srt": ".srt"}
        for name, content in artifacts.items():
            if name not in ARTIFACT_NAMES or not isinstance(content, str):
                raise ProviderError("invalid_artifacts", f"Invalid artifact {name!r}")
            path = directory / (name + suffixes[name])
            path.write_text(content, encoding="utf-8")
            manifest["artifacts"][name] = {"uri": path.resolve().as_uri(), "bytes": path.stat().st_size}
        document = self._project_manifest(
            video_id,
            value,
            manifest,
            relaxed_srt=bool(value.get("relaxed_srt")),
        )
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
            if not stage["ready"]:
                issue = stage["issues"][0]
                raise ProviderError(issue["code"], issue["message"], http_status=503)
        with self._llm_lock:
            response = self._llm_exchange(request)
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

    def _llm_exchange(self, request: Mapping[str, Any]) -> dict[str, Any]:
        """Send one request to the LLM worker and read its one response.

        Caller holds `_llm_lock`. A worker that died between calls -- the CLI it
        drives crashed, the machine slept -- is replaced once and the request
        retried, because the failure has nothing to do with this request.
        """

        for attempt in range(2):
            process = self._llm_worker()
            assert process.stdin is not None and process.stdout is not None
            try:
                process.stdin.write(json.dumps(request, ensure_ascii=False) + "\n")
                process.stdin.flush()
            except (BrokenPipeError, OSError, ValueError):
                self._stop_llm_worker()
                if attempt == 0:
                    continue
                raise ProviderError("llm_unavailable", "The LLM worker could not be started", http_status=503)
            line = _read_line_with_timeout(process, LLM_CALL_TIMEOUT_SEC)
            if line is None:
                self._stop_llm_worker()
                raise ProviderError(
                    "llm_timeout",
                    f"The LLM call did not answer within {int(LLM_CALL_TIMEOUT_SEC)}s",
                    http_status=504,
                )
            if not line.strip():
                # EOF: the worker is gone. Its stderr went to the sidecar's.
                self._stop_llm_worker()
                if attempt == 0:
                    continue
                raise ProviderError("llm_unavailable", "The LLM worker stopped unexpectedly", http_status=503)
            self._schedule_llm_idle_stop()
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ProviderError("llm_protocol", "The LLM worker sent an unreadable response", http_status=502) from exc
            if not isinstance(value, dict):
                raise ProviderError("llm_protocol", "The LLM worker sent an unreadable response", http_status=502)
            return value
        raise ProviderError("llm_unavailable", "The LLM worker stopped unexpectedly", http_status=503)

    def _llm_worker(self) -> subprocess.Popen[str]:
        if self._closed:
            raise ProviderError("llm_unavailable", "The application is shutting down", http_status=503)
        process = self._llm_process
        if process is not None and process.poll() is None:
            return process
        kwargs: dict[str, Any] = {
            "stdin": subprocess.PIPE,
            "stdout": subprocess.PIPE,
            "text": True,
            "encoding": "utf-8",
            "errors": "replace",
            "env": self._engine_environment(),
        }
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        try:
            self._llm_process = subprocess.Popen(self._llm_worker_command(), **kwargs)
        except OSError as exc:
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
            return
        for stream in (process.stdin, process.stdout):
            if stream is not None and not stream.closed:
                try:
                    stream.close()
                except OSError:
                    pass
        if process.poll() is None:
            self._terminate(process)

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
        thread = threading.Thread(target=self._run_worker, args=(task_id,), daemon=True, name=f"nonoka-{task_id[:8]}")
        with self._lock:
            self._threads[task_id] = thread
        thread.start()

    def _run_worker(self, task_id: str) -> None:
        task_dir = self._task_dir(task_id)
        request = _read_json_when_free(task_dir / "request.json")
        command = self._worker_command(task_id, task_dir)
        environment = self._engine_environment()
        request_device = str(request.get("device") or "").strip().lower()
        if request_device.startswith("cuda:"):
            gpu_index = request_device.split(":", 1)[1].strip()
            if gpu_index.isdigit():
                environment["CUDA_VISIBLE_DEVICES"] = gpu_index
        kwargs: dict[str, Any] = {"stdin": subprocess.PIPE, "stdout": subprocess.PIPE, "stderr": subprocess.STDOUT, "text": True, "encoding": "utf-8", "errors": "replace", "env": environment}
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        process: subprocess.Popen[str] | None = None
        try:
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
        *,
        relaxed_srt: bool = False,
    ) -> dict[str, Any]:
        if not _safe_component(video_id):
            raise ProviderError("invalid_document", "Invalid document id")
        entries = manifest.get("artifacts")
        if not isinstance(entries, Mapping) or "stable_json" not in entries:
            raise ProjectionError("FineSub manifest has no stable_json artifact")

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

        stable = artifact_path("stable_json")
        annotated = artifact_path("annotated_csv")
        final = artifact_path("final_srt")
        projection = project_edit_document(
            stable,
            annotated_csv=annotated if annotated is not None and final is not None else None,
            final_srt=final if annotated is not None and final is not None else None,
            video_id=video_id,
            title=str(metadata.get("title") or video_id),
            source=str(metadata.get("path") or metadata.get("source_path") or ""),
            fingerprint=str(metadata.get("fingerprint") or "") or None,
            relaxed_srt=relaxed_srt,
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
        return self.documents.create(video_id, projection, artifacts=manifest, replace_default=True)

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
