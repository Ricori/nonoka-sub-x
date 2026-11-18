"""The engine host: finds the engine packages, starts their processes, speaks to them.

`docs/transcription-engines-plan.md` 4.6 item 33. An engine package is a
directory under the engines root with a `PACKAGE.json` (`discover`). The core
never imports a package's code: it starts the package's entry module in a
process of its own (`PackageProcess`) and speaks a line protocol with it --
one JSON object per line, in both directions, the shape the task worker's own
events already have:

- core -> package: first `{"context": ...}`, the process's neutral launch
  context (R0 document 1.15: its directories, the model layer, its keys by
  name, the LLM routes, the proxy); then one `{"op": ..., ...}` per request;
- package -> core: `{"type": "reporter", "method", "args", "kwargs"}`, a call
  of the engine's reporter, which the core makes on the one reporter that
  hears every half of a task (so a warning said in both halves is said once,
  as when they shared a process); `{"type": "event", "event"}`, a worker event
  passed on as it is; and, ending each request, `{"type": "result", "payload"}`
  or `{"type": "error", "payload"}`.

Which packages exist is the only thing the core learns about an engine before
it runs it, so what it needs to know without starting a process is declared in
`PACKAGE.json`: the engine's spec, the GPU tiers it accepts, the keys it wants,
what its readiness depends on (`readiness`), the interpreter it runs on.
Removing a package's directory removes the engine (plan 4.6 item 33).

The run directories of the processes the core starts (`EngineRun`,
`sweep_runs`, D6) are managed here too: a package derives its own files into
the directory it is handed and never chooses or cleans one.
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import subprocess
import sys
import threading
from collections.abc import Iterable, Mapping
from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .runtime import fsops

PACKAGE_FILE = "PACKAGE.json"
PACKAGE_SCHEMA = 1
KINDS = ("transcription", "correction", "knowledge")
INTERPRETERS = ("python-layer", "launcher")
#: Overrides where the packages are looked for (tests, a development checkout elsewhere).
ENGINES_ENVIRONMENT = "NONOKA_X_ENGINES"
#: Held by the process that owns a run directory for as long as the directory is live.
OWNER_LOCK_NAME = "owner.lock"
#: A run directory name kept out of the start-up sweep.
HOST_RUN_NAME = "host"


class PackageError(RuntimeError):
    """A package process failed a request: its own message, as it formatted it."""

    def __init__(self, payload: Mapping[str, Any]) -> None:
        self.payload = dict(payload)
        self.formatted = str(payload.get("formatted") or payload.get("message") or "engine package failed")
        self.detail = str(payload.get("detail") or "")
        self.kind = str(payload.get("kind") or "")
        super().__init__(str(payload.get("message") or self.formatted))


class PackageUnavailable(RuntimeError):
    """The package needed for this is not installed, or its process could not be started."""


# --- the packages --------------------------------------------------------------------------------


@dataclass(frozen=True)
class Package:
    """One engine package, as its `PACKAGE.json` declares it."""

    root: Path
    manifest: Mapping[str, Any]

    @property
    def id(self) -> str:
        return str(self.manifest["id"])

    @property
    def kind(self) -> str:
        return str(self.manifest["kind"])

    @property
    def label(self) -> str:
        return str(self.manifest.get("label") or self.id)

    @property
    def entry(self) -> str:
        return str(self.manifest["entry"])

    @property
    def interpreter(self) -> str:
        return str(self.manifest.get("interpreter") or "launcher")

    @property
    def paths(self) -> tuple[Path, ...]:
        """The package's import roots, in order: its own code first."""

        return tuple((self.root / relative).resolve() for relative in self.manifest.get("paths") or ())

    @property
    def secrets(self) -> tuple[str, ...]:
        return tuple(str(name) for name in self.manifest.get("secrets") or ())

    @property
    def engine_commit(self) -> str:
        return str((self.manifest.get("upstream") or {}).get("commit") or "")

    @property
    def upstream(self) -> Mapping[str, Any]:
        return dict(self.manifest.get("upstream") or {})

    @property
    def readiness(self) -> Mapping[str, Any]:
        return dict(self.manifest.get("readiness") or {})

    @property
    def gpu_tiers(self) -> tuple[str, ...]:
        return tuple(str(name) for name in self.manifest.get("gpu_tiers") or ())

    def engine_spec(self) -> Any:
        """The transcription engine this package provides (`EngineSpec`)."""

        from .transcription import EngineSpec

        return EngineSpec.from_dict(self.manifest["engine"])

    def correction_spec(self) -> Any:
        """The correction engine this package provides (`CorrectionSpec`)."""

        from .correction import CorrectionSpec

        return CorrectionSpec.from_dict(self.manifest["engine"])


def _check_manifest(manifest: Any, root: Path) -> Mapping[str, Any]:
    if not isinstance(manifest, Mapping) or manifest.get("schema") != PACKAGE_SCHEMA:
        raise ValueError(f"{root / PACKAGE_FILE}: not a schema {PACKAGE_SCHEMA} package manifest")
    for name in ("id", "kind", "entry"):
        if not isinstance(manifest.get(name), str) or not manifest[name]:
            raise ValueError(f"{root / PACKAGE_FILE}: `{name}` is missing")
    if manifest["kind"] not in KINDS:
        raise ValueError(f"{root / PACKAGE_FILE}: kind {manifest['kind']!r} is not one of {', '.join(KINDS)}")
    if manifest.get("interpreter", "launcher") not in INTERPRETERS:
        raise ValueError(f"{root / PACKAGE_FILE}: interpreter {manifest.get('interpreter')!r} is unknown")
    if manifest["kind"] in ("transcription", "correction") and not isinstance(manifest.get("engine"), Mapping):
        raise ValueError(f"{root / PACKAGE_FILE}: a {manifest['kind']} package declares its `engine`")
    if manifest["id"] != root.name:
        raise ValueError(f"{root / PACKAGE_FILE}: id {manifest['id']!r} is not the directory's name {root.name!r}")
    return manifest


def default_engines_root() -> Path:
    """Where the packages are: `NONOKA_X_ENGINES`, else `engines\\` beside the core's `src\\`.

    The same in a source checkout and in the desktop's staged resources: both
    hold `src\\nonoka_x\\` and `engines\\` side by side.
    """

    named = os.environ.get(ENGINES_ENVIRONMENT, "").strip()
    if named:
        return Path(named).expanduser().resolve()
    return Path(__file__).resolve().parents[2] / "engines"


def discover(root: str | Path | None = None) -> dict[str, Package]:
    """Every package under `root` (`default_engines_root()`), by id.

    A directory without a `PACKAGE.json` is not a package; one whose manifest
    is broken is reported on stderr and left out, as if it were not there.
    """

    base = Path(root) if root is not None else default_engines_root()
    found: dict[str, Package] = {}
    try:
        entries = sorted(base.iterdir())
    except OSError:
        return found
    for directory in entries:
        manifest_path = directory / PACKAGE_FILE
        if not directory.is_dir() or not manifest_path.is_file():
            continue
        try:
            manifest = _check_manifest(json.loads(manifest_path.read_text(encoding="utf-8")), directory)
        except (OSError, ValueError) as error:
            print(f"engine package skipped: {error}", file=sys.stderr, flush=True)
            continue
        found[manifest["id"]] = Package(directory.resolve(), manifest)
    return found


def of_kind(packages: Mapping[str, Package], kind: str) -> list[Package]:
    return [package for package in packages.values() if package.kind == kind]


def knowledge_package(packages: Mapping[str, Package]) -> Package | None:
    found = of_kind(packages, "knowledge")
    return found[0] if found else None


def correction_package(packages: Mapping[str, Package], engine_id: str | None = None) -> Package | None:
    """The package providing correction engine `engine_id` (the first one when not named)."""

    for package in of_kind(packages, "correction"):
        if engine_id is None or package.manifest["engine"].get("id") == engine_id:
            return package
    return None


def transcription_package(packages: Mapping[str, Package], engine_id: str) -> Package | None:
    for package in of_kind(packages, "transcription"):
        if package.manifest["engine"].get("id") == engine_id:
            return package
    return None


# --- run directories ----------------------------------------------------------------------------------


class EngineRun:
    """One engine process's run directory, `engines\\<package>\\run\\<process>\\` (D6).

    Claimed before the process starts and closed when it has ended: closing
    releases the owner lock and deletes the directory. The lock is what makes
    a directory left behind by a crash recognisable (`sweep_runs`).
    """

    def __init__(self, directory: Path, stack: ExitStack) -> None:
        self.directory = directory
        self._stack = stack

    @classmethod
    def claim(cls, package_dir: Path) -> "EngineRun":
        directory = package_dir / "run" / f"{os.getpid()}-{secrets.token_hex(6)}"
        directory.mkdir(parents=True)
        stack = ExitStack()
        try:
            stack.enter_context(fsops.holding_lock(directory / OWNER_LOCK_NAME, timeout=0))
        except BaseException:
            stack.close()
            fsops.remove_tree(directory)
            raise
        return cls(directory, stack)

    def close(self) -> None:
        self._stack.close()
        try:
            fsops.remove_tree(self.directory)
        except OSError:
            # A file still open in a process that is going away: the next
            # start-up sweep takes it.
            pass

    def __enter__(self) -> "EngineRun":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


def sweep_runs(package_dirs: Iterable[Path]) -> list[Path]:
    """Delete run directories whose owner is gone; returns what was removed.

    A directory is live while its owner holds `owner.lock`; one without the
    lock file never got that far.
    """

    removed: list[Path] = []
    for package_dir in package_dirs:
        root = package_dir / "run"
        try:
            entries = sorted(root.iterdir())
        except OSError:
            continue
        for entry in entries:
            if entry.name == HOST_RUN_NAME or not entry.is_dir() or fsops.is_directory_link(entry):
                continue
            if not fsops.try_lock(entry / OWNER_LOCK_NAME):
                continue
            try:
                fsops.remove_tree(entry)
            except OSError:
                continue
            removed.append(entry)
    return removed


# --- starting a package's process ------------------------------------------------------------------------

def _core_roots() -> set[str]:
    """The import roots that hold the core itself: a package's process must not see them."""

    here = Path(__file__).resolve().parent.parent
    return {os.path.normcase(str(here))}


def package_environment(package: Package, base: Mapping[str, str] | None = None) -> dict[str, str]:
    """The environment a package's process starts in: this one's, with its import path its own.

    `PYTHONPATH` is the package's roots, then whatever else this process was
    handed (the video tool's module directory, say) except the core's own
    source, so a package can never import the core. What an engine reads of its
    own is not the core's to know: the package's bridge drops whatever of it
    was inherited and writes it from the launch context.
    """

    environment = dict(os.environ if base is None else base)
    core = _core_roots()
    inherited = [
        part for part in environment.get("PYTHONPATH", "").split(os.pathsep)
        if part and os.path.normcase(str(Path(part).resolve())) not in core
    ]
    roots = [str(path) for path in package.paths]
    environment["PYTHONPATH"] = os.pathsep.join([*roots, *(part for part in inherited if part not in roots)])
    environment["PYTHONIOENCODING"] = "utf-8"
    environment.setdefault("PYTHONUTF8", "0")
    return environment


def package_command(package: Package, python: str | Path, *arguments: str) -> list[str]:
    """How a package's process is started: its entry module on `python`."""

    return [str(python), "-m", package.entry, *arguments]


def interpreter_for(package: Package, context: Mapping[str, Any] | None) -> str:
    """The interpreter a package runs on: the Python layer's, or the launcher's (this one's)."""

    runtime = (context or {}).get("runtime") or {}
    if package.interpreter == "python-layer" and runtime.get("python"):
        return str(runtime["python"])
    launcher = (context or {}).get("launcher")
    return str(launcher or sys.executable)


Handler = Callable[[Mapping[str, Any]], None]


class PackageProcess:
    """One running package process and its line protocol.

    `on_message` receives every line that is not the end of a request:
    reporter calls, worker events, and any stray output (as `{"type": "log"}`).
    Requests are answered in order, one at a time.
    """

    def __init__(
        self,
        package: Package,
        context: Mapping[str, Any] | None,
        *,
        on_message: Handler | None = None,
        environment: Mapping[str, str] | None = None,
        python: str | Path | None = None,
        arguments: Iterable[str] = (),
    ) -> None:
        self.package = package
        self._on_message = on_message or (lambda _message: None)
        self._lock = threading.Lock()
        command = package_command(package, python or interpreter_for(package, context), *arguments)
        kwargs: dict[str, Any] = {}
        if os.name == "nt":
            kwargs["creationflags"] = fsops.no_window()
        try:
            self.process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=package_environment(package, environment),
                **kwargs,
            )
        except OSError as error:
            raise PackageUnavailable(f"无法启动引擎包 {package.id}：{error}") from error
        self._send({"context": dict(context or {})})

    @property
    def alive(self) -> bool:
        return self.process.poll() is None

    def _send(self, value: Mapping[str, Any]) -> None:
        assert self.process.stdin is not None
        try:
            self.process.stdin.write(json.dumps(value, ensure_ascii=False) + "\n")
            self.process.stdin.flush()
        except OSError as error:
            raise PackageUnavailable(f"引擎包 {self.package.id} 的进程已退出（{error}）") from error

    def request(self, op: str, payload: Mapping[str, Any] | None = None, *, on_message: Handler | None = None) -> Any:
        """Send one request and relay what comes back until its result; return the result's payload."""

        handler = on_message or self._on_message
        with self._lock:
            self._send({"op": op, **dict(payload or {})})
            assert self.process.stdout is not None
            for raw in self.process.stdout:
                line = raw.strip()
                if not line:
                    continue
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    handler({"type": "log", "line": line})
                    continue
                if not isinstance(message, Mapping):
                    handler({"type": "log", "line": line})
                    continue
                kind = message.get("type")
                if kind == "result":
                    return message.get("payload")
                if kind == "error":
                    raise PackageError(message.get("payload") or {})
                handler(message)
        code = self.process.wait()
        raise PackageUnavailable(f"引擎包 {self.package.id} 的进程意外退出（退出码 {code}）")

    def close(self, timeout: float = 30.0) -> int | None:
        """End the conversation: close its input and wait for the process to leave."""

        try:
            if self.process.stdin is not None and not self.process.stdin.closed:
                self.process.stdin.close()
        except OSError:
            pass
        try:
            code = self.process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            fsops.terminate_process_tree(self.process)
            code = self.process.poll()
        if self.process.stdout is not None and not self.process.stdout.closed:
            self.process.stdout.close()
        return code

    def terminate(self) -> None:
        fsops.terminate_process_tree(self.process)
        self.close(timeout=5.0)

    def detach(self) -> None:
        """End the conversation and leave the process running (a knowledge process serving others)."""

        for stream in (self.process.stdin, self.process.stdout):
            try:
                if stream is not None and not stream.closed:
                    stream.close()
            except OSError:
                pass

    def __enter__(self) -> "PackageProcess":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


# --- readiness, from what a package declares ---------------------------------------------------------------


def _issue(code: str, message: str) -> Any:
    from .transcription import Issue

    return Issue(code, message)


def _tool_ready(runtime: Any, name: str) -> bool:
    if shutil.which(name):
        return True
    return runtime is not None and runtime.tool_path(name) is not None


def _media_issues(runtime: Any, managed: Mapping[str, Any] | None) -> list[Any]:
    """The interpreter and the media tools, which every local engine needs."""

    managed_runtime_ready = managed is not None and (managed.get("runtime") or {}).get("state") == "ready"
    issues: list[Any] = []
    if sys.version_info < (3, 12) and not managed_runtime_ready:
        issues.append(_issue("missing_python", "需要 Python 3.12 或更高版本"))
    if not _tool_ready(runtime, "ffmpeg") or not _tool_ready(runtime, "ffprobe"):
        issues.append(_issue("missing_ffmpeg", "缺少项目必备依赖 FFmpeg 或 FFprobe"))
    return issues


def _managed(runtime: Any) -> Mapping[str, Any] | None:
    if runtime is None:
        return None
    status = runtime.status()
    return status if status else None


def runtime_engine_readiness(package: Package, runtime: Any) -> Any:
    """A local engine that runs on the managed runtime: the media tools, the platform, the runtime, its models.

    `readiness` in `PACKAGE.json`: `platforms` (where it runs at all, and the
    issue elsewhere), `modules` (what the launcher has only when it is the
    runtime itself, a checkout on a full environment).
    """

    from importlib.util import find_spec

    from .transcription import Readiness

    declared = package.readiness
    if runtime is None:
        return Readiness((_issue("runtime_unavailable", f"无法查询本机运行环境，{package.engine_spec().label} 不可用"),))
    managed = _managed(runtime)
    issues = _media_issues(runtime, managed)
    platforms = declared.get("platforms")
    if platforms and sys.platform not in platforms:
        issues.append(_issue("unsupported_platform", str(declared.get("platform_message") or "当前平台不支持")))
    runtime_state = str(((managed or {}).get("runtime") or {}).get("state") or "")
    missing_modules = [name for name in declared.get("modules") or () if find_spec(name) is None]
    if runtime_state == "outdated":
        # Installed for an older dependency lock. The sidecar's own interpreter
        # never carries these modules, so listing them would read as "the
        # runtime is gone".
        issues.append(_issue(
            "runtime_outdated",
            "运行环境需要更新：引擎依赖有变化，请到「运行环境」页点击「更新」（只下载有变化的部分，约几分钟）",
        ))
    elif missing_modules and runtime_state != "ready":
        issues.append(_issue("missing_runtime", "AI 运行时缺失：" + "、".join(missing_modules)))
    if managed is not None:
        missing_models = [str(item.get("id")) for item in managed.get("models", []) if item.get("state") != "ready"]
        if missing_models:
            issues.append(_issue("missing_model", "ASR 所需模型尚未安装：" + "、".join(missing_models)))
    return Readiness(tuple(issues))


def launcher_engine_readiness(package: Package, runtime: Any, store: Any, target: str) -> Any:
    """An engine that runs on the launcher: the media tools, the launcher's modules, an LLM for its LLM targets.

    `readiness` in `PACKAGE.json`: `modules` / `windows_modules`
    (`[distribution, module]` pairs the launcher must have until the managed
    runtime is ready), `llm_targets` (the targets that need a configured LLM).
    """

    from importlib.util import find_spec

    from .transcription import Readiness

    declared = package.readiness
    managed = _managed(runtime)
    managed_runtime_ready = managed is not None and (managed.get("runtime") or {}).get("state") == "ready"
    issues: list[Any] = []
    if sys.version_info < (3, 12) and not managed_runtime_ready:
        issues.append(_issue("missing_python", "需要 Python 3.12 或更高版本"))
    if not _tool_ready(runtime, "ffmpeg") or not _tool_ready(runtime, "ffprobe"):
        issues.append(_issue("missing_ffmpeg", "缺少项目必备依赖 FFmpeg 或 FFprobe"))
    if not managed_runtime_ready:
        wanted = [*(declared.get("modules") or ())]
        if sys.platform == "win32":
            wanted += [*(declared.get("windows_modules") or ())]
        missing = [str(distribution) for distribution, module in wanted if find_spec(str(module)) is None]
        if missing:
            issues.append(_issue(
                "missing_harness",
                "本地续跑依赖缺失：" + "、".join(missing) + "；请在「运行环境」页重建 Python 启动环境，或安装完整运行环境",
            ))
    if target in (declared.get("llm_targets") or ()):
        snapshot = store.snapshot() if store is not None else {}
        # A CLI on the machine is not a configured model: only a saved global
        # model (a provider and a model, with its key or CLI) lets the LLM stages run.
        if not snapshot.get("llmReady"):
            issues.append(_issue("missing_llm_key", "尚未配置模型提供商，请在设置里选择提供商与全局模型并保存"))
    return Readiness(tuple(issues))


# --- the engines the core registers for the packages it found -----------------------------------------------


@dataclass
class TaskSession:
    """What the package engines of one task share: the reporter that hears every half, the launch contexts.

    `reporter` is the worker's (FineSub's reporter protocol); `emit` writes a
    worker event; `contexts` are the launch contexts by package id, as the
    sidecar built them; `knowledge` the task's knowledge process endpoint,
    once one is started.
    """

    reporter: Any = None
    emit: Callable[[str, Mapping[str, Any]], None] | None = None
    contexts: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    knowledge_endpoint: Mapping[str, Any] | None = None
    processes: list[PackageProcess] = field(default_factory=list)

    def context_for(self, package: Package) -> dict[str, Any]:
        context = dict(self.contexts.get(package.id) or {"schema": 1, "package_id": package.id})
        if self.knowledge_endpoint is not None and package.kind != "knowledge":
            context["knowledge_endpoint"] = dict(self.knowledge_endpoint)
        return context

    def relay(self, message: Mapping[str, Any]) -> None:
        """One line from a package process: a reporter call, a worker event, or stray output."""

        kind = message.get("type")
        if kind == "reporter" and self.reporter is not None:
            method = getattr(self.reporter, str(message.get("method")), None)
            if callable(method):
                method(*(message.get("args") or ()), **dict(message.get("kwargs") or {}))
            return
        if kind == "event" and self.emit is not None:
            event = message.get("event") or {}
            self.emit(str(event.get("type") or "log"), event.get("payload") or {})
            return
        line = message.get("line")
        if line is not None:
            print(line, file=sys.stderr, flush=True)

    def start(self, package: Package, *, arguments: Iterable[str] = ()) -> PackageProcess:
        process = PackageProcess(package, self.context_for(package), on_message=self.relay, arguments=arguments)
        self.processes.append(process)
        return process

    def close(self) -> None:
        for process in reversed(self.processes):
            process.close()
        self.processes.clear()


class PackageTranscriptionEngine:
    """A transcription engine package, as the core's `TranscriptionEngine`.

    `readiness` is the core's reading of what the package declares;
    `transcribe` starts the package's process for the one job.
    """

    def __init__(self, package: Package, session: TaskSession | None = None, runtime: Any | None = None) -> None:
        self.package = package
        self.spec = package.engine_spec()
        self._session = session or TaskSession()
        self._runtime = runtime

    def readiness(self, context: Any) -> Any:
        return runtime_engine_readiness(self.package, self._runtime)

    def transcribe(self, job: Any, report: Any) -> Any:
        from .transcription import TranscriptionResult

        if report.cancelled():
            raise PackageError({"message": "task cancelled before transcription started",
                                "formatted": "EngineCancelled: task cancelled before transcription started"})
        process = self._session.start(self.package)
        try:
            payload = process.request("transcribe", {"job": job.to_dict()})
        finally:
            process.close()
            if process in self._session.processes:
                self._session.processes.remove(process)
        return TranscriptionResult.from_dict(payload)


class PackageCorrectionEngine:
    """A correction engine package, as the core's `CorrectionEngine`.

    One process serves the whole task: `check` before the transcription,
    `run` after it, so whatever the engine keeps per process (an LLM model
    override, its knowledge connection) is what one process kept before.
    """

    def __init__(
        self,
        package: Package,
        session: TaskSession | None = None,
        store: Any | None = None,
        runtime: Any | None = None,
    ) -> None:
        self.package = package
        self.spec = package.correction_spec()
        self._session = session or TaskSession()
        self._store = store
        self._runtime = runtime
        self._process: PackageProcess | None = None
        #: Sent as `begin` when the process starts: what the task fixes for the whole process.
        self.begin: Mapping[str, Any] | None = None

    def readiness(self, context: Any, request: Mapping[str, Any]) -> Any:
        return launcher_engine_readiness(self.package, self._runtime, self._store, context.target)

    def process(self) -> PackageProcess:
        if self._process is None or not self._process.alive:
            self._process = self._session.start(self.package)
            if self.begin is not None:
                self._process.request("begin", self.begin)
        return self._process

    @property
    def started(self) -> bool:
        """Whether this task's correction process is running (it began with `begin`)."""

        return self._process is not None and self._process.alive

    def call(self, op: str, payload: Mapping[str, Any] | None = None) -> Any:
        return self.process().request(op, payload)

    def check(self, job: Any, report: Any) -> None:
        self.call("check", {"job": job.to_dict()})

    def run(self, job: Any, report: Any) -> Any:
        from .correction import CorrectionResult

        return CorrectionResult.from_dict(self.call("run", {"job": job.to_dict()}))

    def close(self) -> None:
        if self._process is not None:
            self._process.close()
            if self._process in self._session.processes:
                self._session.processes.remove(self._process)
            self._process = None


def start_knowledge_service(
    session: TaskSession,
    package: Package,
    *,
    subjects: Mapping[str, Any] | None = None,
    owner: int | None = None,
) -> Any:
    """The task's knowledge process: one for every engine process of the task, as one worker had one.

    With `subjects` (`prepare_subjects`' arguments) it first prepares the
    task's picked subjects, as the worker's own knowledge process did before
    anything ran, and returns what that answered. Its endpoint goes into the
    launch context of every package process started after this
    (`TaskSession.context_for`). It serves until its owner (this process) has
    exited and its last connection is closed.
    """

    process = PackageProcess(package, session.context_for(package), on_message=session.relay)
    try:
        if subjects is not None:
            prepared = process.request("prepare_subjects", subjects)
        else:
            prepared = None
        endpoint = process.request("serve", {"owner": owner or os.getpid()})
    except BaseException:
        process.terminate()
        raise
    process.detach()
    session.knowledge_endpoint = dict(endpoint)
    # An engine process started before this one (a correction asked what a
    # Chinese-source task may use) learns it now, before it reaches the base.
    for running in session.processes:
        if running.alive and running.package.kind != "knowledge":
            running.request("knowledge", {"endpoint": dict(endpoint)})
    return prepared


def ask_once(kind: str, op: str, payload: Mapping[str, Any] | None = None, *, root: Path | None = None) -> Any:
    """One request of the first installed `kind` package, from a short-lived process of its own.

    For a question whose answer the package keeps in its code (its packaged
    catalog, how it reads its own products): the process gets a fresh run
    directory, which is also its data directory for the question (nothing it
    might write there is kept), and no key. `PackageUnavailable` when no such
    package is installed.
    """

    import tempfile

    found = of_kind(discover(root), kind)
    if not found:
        raise PackageUnavailable(f"没有安装{kind}引擎包")
    package = found[0]
    with tempfile.TemporaryDirectory(prefix="nonoka-engine-") as run:
        context = {"schema": 1, "package_id": package.id, "paths": {"package_data": run, "run": run}}
        with PackageProcess(package, context) as process:
            return process.request(op, payload)


__all__ = [
    "ENGINES_ENVIRONMENT",
    "ask_once",
    "EngineRun",
    "Package",
    "PackageCorrectionEngine",
    "PackageError",
    "PackageProcess",
    "PackageTranscriptionEngine",
    "PackageUnavailable",
    "TaskSession",
    "correction_package",
    "default_engines_root",
    "discover",
    "interpreter_for",
    "knowledge_package",
    "launcher_engine_readiness",
    "of_kind",
    "package_command",
    "package_environment",
    "runtime_engine_readiness",
    "start_knowledge_service",
    "sweep_runs",
    "transcription_package",
]
