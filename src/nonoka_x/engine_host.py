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


# --- the environment a package's process starts in (plan 4.5 items 27-29) -------------------------------------
#
# Built from nothing, never inherited (owner decision, plan appendix B question
# 6): every variable a package's process has is in the table below, in one of
# three categories --
#
# - `system`: copied by name, as it is, from the process that starts the
#   package (what Windows, Python, torch, DPAPI and child processes need to
#   exist at all); never a wildcard;
# - `nonoka`: written here unconditionally from Nonoka's own configuration,
#   the launch context, the task request and the data layout;
# - `absent`: known to be read by something in a package's process, and kept
#   out on purpose.
#
# Anything not in the table does not exist in the process either. This is the
# neutral half of the table; what an engine reads of its own (`FINESUB_*`,
# its model libraries' variables) is in each package's bridge, which writes it
# from the launch context inside the process (`CLASSIFICATION` in
# `engines/*/adapter/*/bridge.py`). Together the two are the one source the
# coverage scan, the poisoning test and the read audit check against
# (`tests/test_engine_hermetic.py`).
#
# Which process gets what is said per entry: `transcription` (sandboxed,
# plan 4.5 item 28), `correction` (not sandboxed, the local agent CLIs'
# variables, item 29), `knowledge` (as the correction, without the agent
# CLIs': owner 2026-11-18, WP-H question H2).

#: Process kinds, as a package's `kind`.
TRANSCRIPTION, CORRECTION, KNOWLEDGE = "transcription", "correction", "knowledge"
ALL_PROCESSES = frozenset(KINDS)
LAUNCHER_PROCESSES = frozenset((CORRECTION, KNOWLEDGE))


@dataclass(frozen=True)
class Variable:
    """One row of the classification table."""

    name: str
    category: str  # "system" | "nonoka" | "absent"
    processes: frozenset[str]
    reason: str
    #: The platforms the row applies to ("win32", "darwin"); empty for both.
    platforms: tuple[str, ...] = ()

    def applies(self, kind: str, platform: str | None = None) -> bool:
        current = platform or sys.platform
        return kind in self.processes and (not self.platforms or current in self.platforms)


def _rows(category: str, processes: frozenset[str], reason: str, *names: str, platforms: tuple[str, ...] = ()) -> tuple[Variable, ...]:
    return tuple(Variable(name, category, processes, reason, platforms) for name in names)


#: The user's own profile directories: the real ones for a process outside the
#: sandbox, the sandbox's for the transcription (item 28; `_sandbox_variables`).
PROFILE_NAMES = ("USERPROFILE", "APPDATA", "LOCALAPPDATA", "TEMP", "TMP")
#: Spelled out for a child: the proxy, or "direct" (D17).
PROXY_NAMES = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")
NO_PROXY_NAMES = ("NO_PROXY", "no_proxy")
LOCAL_HOSTS = "localhost,127.0.0.1,::1"
#: The agent CLIs' own configuration and certificates (D16), for the correction only.
AGENT_CLI_NAMES = (
    "CLAUDE_CONFIG_DIR", "CODEBUDDY_CONFIG_DIR", "CODEBUDDY_INTERNET_ENVIRONMENT",
    "CODEBUDDY_INTERNET_ENVIROMENT", "CODEX_HOME", "DSH_HOME", "CODEX_CA_CERTIFICATE",
    "NODE_EXTRA_CA_CERTS", "SSL_CERT_FILE", "SSL_CERT_DIR",
)

CLASSIFICATION: tuple[Variable, ...] = (
    # -- system: what a Windows process needs to run at all -------------------------------------------------
    *_rows("system", ALL_PROCESSES,
           "Windows itself: where the system is, which Python, torch's DLL loading and every child process need",
           "SystemRoot", "windir", "SystemDrive", "ComSpec", "PATHEXT", "OS", platforms=("win32",)),
    *_rows("system", ALL_PROCESSES,
           "where programs and shared data live: native libraries and the CUDA driver look there",
           "ProgramData", "ProgramFiles", "ProgramFiles(x86)", "ProgramW6432",
           "CommonProgramFiles", "CommonProgramFiles(x86)", "CommonProgramW6432", platforms=("win32",)),
    *_rows("system", ALL_PROCESSES,
           "the machine: thread pools (OpenMP, MKL, torch) size themselves from the processor count",
           "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE", "PROCESSOR_IDENTIFIER", "COMPUTERNAME",
           platforms=("win32",)),
    *_rows("system", ALL_PROCESSES,
           "the account: DPAPI needs the loaded profile, not a variable, but torch inductor names a directory "
           "after USERNAME and child processes expect the pair",
           "USERNAME", "USERDOMAIN", platforms=("win32",)),
    *_rows("system", LAUNCHER_PROCESSES,
           "the user's home drive and path: only read where USERPROFILE is missing (Python's expanduser); "
           "the real ones outside the sandbox",
           "HOMEDRIVE", "HOMEPATH", platforms=("win32",)),
    *_rows("system", LAUNCHER_PROCESSES,
           "the real profile directories: the correction and the knowledge process run outside the sandbox "
           "(plan 4.5 item 29) and the local agent CLIs keep their login there",
           *PROFILE_NAMES, platforms=("win32",)),
    *_rows("system", ALL_PROCESSES, "the POSIX basics", "USER", "LANG", platforms=("darwin",)),
    *_rows("system", LAUNCHER_PROCESSES, "the POSIX home and temporary directory, outside the sandbox",
           "HOME", "TMPDIR", platforms=("darwin",)),
    *_rows("system", frozenset((CORRECTION,)),
           "the local agent CLIs' own configuration directories and certificates (D16); the CLI's login is "
           "a boundary of the hermetic run (plan 4.5 item 31)",
           *AGENT_CLI_NAMES),
    # -- nonoka: written here ---------------------------------------------------------------------------------
    Variable("PATH", "nonoka", ALL_PROCESSES,
             "the tool layer's ffmpeg / ffprobe / tokcount directories, for the correction the local agent CLIs' "
             "directories and the node a script CLI runs on (owner H1), then the system directories derived "
             "from SystemRoot; never the parent's PATH"),
    Variable("PYTHONPATH", "nonoka", ALL_PROCESSES,
             "the package's own roots, for the children the engine starts without -I (its MCP server; D9)"),
    Variable("PYTHONNOUSERSITE", "nonoka", ALL_PROCESSES,
             "1: no user site-packages in those children either (D9)"),
    Variable("PYTHONIOENCODING", "nonoka", ALL_PROCESSES,
             "utf-8 for the children's standard streams (the process itself is -I and reconfigures its own)"),
    Variable("PYTHONUTF8", "nonoka", ALL_PROCESSES,
             "0: locale decoding of native tools' output stays on the ANSI code page (R0 1.10)"),
    *_rows("nonoka", ALL_PROCESSES, "the proxy the core resolved for this process, or none (D17)", *PROXY_NAMES),
    *_rows("nonoka", ALL_PROCESSES,
           "`*` for a direct route, else the local hosts: written either way, so neither Python nor a library "
           "falls back to the Windows system proxy (D17)", *NO_PROXY_NAMES),
    Variable("CUDA_VISIBLE_DEVICES", "nonoka", frozenset((TRANSCRIPTION,)),
             "the card a `cuda:N` request names, set unconditionally by the transcription package's adapter "
             "before anything loads CUDA (owner H6: where it always was, so no baseline moves); no restriction "
             "(absent) for `cuda` and `cpu`. Never handed in from here"),
    *_rows("absent", LAUNCHER_PROCESSES, "no card: the correction and the knowledge process load no CUDA",
           "CUDA_VISIBLE_DEVICES"),
    *_rows("nonoka", frozenset((TRANSCRIPTION,)),
           "the sandbox's home (shared between runs: the CUDA and torch kernel caches stay warm, plan 4.5 item 28)",
           "USERPROFILE", "HOME", "APPDATA", "LOCALAPPDATA"),
    *_rows("nonoka", frozenset((TRANSCRIPTION,)),
           "the sandbox's home split into drive and path, like USERPROFILE (owner 2026-11-18, appendix C item 2, "
           "WP-H question H8: measured to change nothing, kept consistent with the rest of the profile)",
           "HOMEDRIVE", "HOMEPATH", platforms=("win32",)),
    *_rows("nonoka", frozenset((TRANSCRIPTION,)),
           "the sandbox's temporary directory of this one process, with its anchor held open (item 28)",
           "TEMP", "TMP"),
    *_rows("nonoka", frozenset((TRANSCRIPTION,)), "the sandbox's temporary directory of this one process",
           "TMPDIR", platforms=("darwin",)),
    # -- absent: read by something in a package's process, kept out -------------------------------------------
    *_rows("absent", ALL_PROCESSES,
           "the interpreter's own switches: the process starts with -I, and its children must not inherit them",
           "PYTHONHOME", "PYTHONSTARTUP", "PYTHONDONTWRITEBYTECODE", "PYTHONPYCACHEPREFIX", "PYTHONSAFEPATH",
           "PYTHONWARNINGS", "PYTHONINSPECT", "PYTHONVERBOSE", "PYTHONOPTIMIZE", "PYTHONDEBUG",
           "PYTHONCASEOK", "PYTHONHASHSEED", "PYTHONEXECUTABLE", "PYTHONLEGACYWINDOWSSTDIO",
           "PYTHONPLATLIBDIR", "PYTHONUSERBASE", "PYTHONBREAKPOINT", "PYTHONFAULTHANDLER",
           "PYTHONTRACEMALLOC", "PYTHONMALLOC", "PYTHONDEVMODE", "PYTHONINTMAXSTRDIGITS",
           "PYTHONWARNDEFAULTENCODING", "VIRTUAL_ENV", "CONDA_PREFIX", "SETUPTOOLS_USE_DISTUTILS"),
    *_rows("absent", ALL_PROCESSES,
           "the standard library's own look-ups (zoneinfo's time zone path, sysconfig's build directory): "
           "their defaults are the interpreter's own",
           "PYTHONTZPATH", "_PYTHON_PROJECT_BASE", "_PYTHON_HOST_PLATFORM", "_PYTHON_SYSCONFIGDATA_NAME"),
    *_rows("absent", ALL_PROCESSES,
           "the compiler toolchain: compiled acceleration is off and MSVC is no dependency (plan 2.8)",
           "INCLUDE", "LIB", "LIBPATH", "VSINSTALLDIR", "VCINSTALLDIR", "VCToolsInstallDir", "WindowsSdkDir",
           "DevEnvDir"),
    *_rows("absent", ALL_PROCESSES,
           "a CUDA toolkit on the machine: the wheels carry their own runtime, and removing these was checked "
           "to change nothing (plan 2.6)",
           "CUDA_PATH", "CUDA_HOME", "NVTOOLSEXT_PATH", "CUDNN_PATH"),
    *_rows("absent", ALL_PROCESSES,
           "the user's certificate bundles for Python clients: only the agent CLIs' are handed on (D16)",
           "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE"),
    *_rows("absent", ALL_PROCESSES, "an XDG directory: the engine's fall-backs are named explicitly",
           "XDG_STATE_HOME", "XDG_CACHE_HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME"),
    *_rows("absent", ALL_PROCESSES, "editors, for an interactive command no task runs", "EDITOR", "VISUAL"),
    *_rows("absent", ALL_PROCESSES,
           "a terminal's, an IDE's or a build system's, read by libraries to size output, name a user or pin "
           "timestamps (found by the read audit of the real pipeline): their defaults",
           "COLUMNS", "LINES", "LOGNAME", "LNAME", "PYCHARM_HOSTED", "TEST_TMPDIR", "SOURCE_DATE_EPOCH",
           "PYTHONDOCS", "ANDROID_DATA", "PROCESSOR_ARCHITEW6432", "WIN_PD_OVERRIDE_LOCAL_APPDATA"),
    *_rows("absent", ALL_PROCESSES, "the POSIX user name: Windows has USERNAME", "USER", platforms=("win32",)),
    *_rows("absent", ALL_PROCESSES,
           "would make a program lookup skip the working directory; Windows' default (unset) is kept",
           "NoDefaultCurrentDirectoryInExePath", platforms=("win32",)),
    *_rows("absent", frozenset((TRANSCRIPTION, KNOWLEDGE)),
           "the local agent CLIs' variables: only the correction runs an agent CLI (D16)", *AGENT_CLI_NAMES),
    *_rows("absent", ALL_PROCESSES,
           "Python's `tempfile` reads TMPDIR before TEMP on every platform: on Windows it would bypass the "
           "process's TEMP (the sandbox's, for the transcription)", "TMPDIR", platforms=("win32",)),
)


def classification_for(kind: str, platform: str | None = None) -> dict[str, Variable]:
    """The neutral table's rows for one process kind on `platform` (this one), by name."""

    return {row.name: row for row in CLASSIFICATION if row.applies(kind, platform)}


def system_names(kind: str, platform: str | None = None) -> tuple[str, ...]:
    return tuple(row.name for row in CLASSIFICATION if row.category == "system" and row.applies(kind, platform))


def _process_kind(package: Package) -> str:
    return package.kind if package.kind in KINDS else CORRECTION


def sandboxed(package: Package) -> bool:
    """Whether a package's process runs in the process-level sandbox: the transcription's (plan 4.5 item 28)."""

    return package.kind == TRANSCRIPTION


def _system_directories(parent: Mapping[str, str]) -> list[str]:
    """The system's own program directories, derived from `SystemRoot` (or the POSIX defaults)."""

    if os.name != "nt":
        return ["/usr/bin", "/bin", "/usr/sbin", "/sbin"]
    root = _lookup(parent, "SystemRoot") or _lookup(parent, "windir") or r"C:\Windows"
    system32 = str(Path(root) / "System32")
    return [system32, root, str(Path(system32) / "Wbem"), str(Path(system32) / "WindowsPowerShell" / "v1.0"),
            str(Path(system32) / "OpenSSH")]


def _lookup(environment: Mapping[str, str], name: str) -> str | None:
    """`environment[name]`, case-insensitively on Windows (as the process itself would read it)."""

    if name in environment:
        return environment[name]
    if os.name == "nt":
        upper = name.upper()
        for key, value in environment.items():
            if key.upper() == upper:
                return value
    return None


def package_path(context: Mapping[str, Any] | None, parent: Mapping[str, str], kind: str) -> str:
    """`PATH` for a package's process: the tool layer, the agent CLIs (correction), the system directories."""

    context = context or {}
    entries: list[str] = []
    if kind == CORRECTION:
        entries += [str(entry) for entry in (context.get("agent_cli") or {}).get("path") or ()]
    entries += [str(entry) for entry in (context.get("runtime") or {}).get("tools") or ()]
    entries += _system_directories(parent)
    seen: set[str] = set()
    unique = []
    for entry in entries:
        key = os.path.normcase(entry)
        if entry and key not in seen:
            seen.add(key)
            unique.append(entry)
    return os.pathsep.join(unique)


def proxy_variables(context: Mapping[str, Any] | None) -> dict[str, str]:
    """The proxy variables for the route the core resolved (D17): always spelled out, both cases."""

    proxy = (context or {}).get("proxy") or {}
    url = proxy.get("url") if proxy.get("policy") else None
    variables = {name: str(url) for name in PROXY_NAMES} if url else {}
    for name in NO_PROXY_NAMES:
        variables[name] = LOCAL_HOSTS if url else "*"
    return variables


def package_environment(
    package: Package,
    context: Mapping[str, Any] | None = None,
    parent: Mapping[str, str] | None = None,
    *,
    sandbox: "SandboxTemp | None" = None,
) -> dict[str, str]:
    """The environment a package's process starts in, built from nothing (plan 4.5 item 27).

    `parent` (this process's environment) only supplies the `system` rows, by
    name. Everything else is written from the launch context: `PATH`, the
    import path for the engine's own children, the proxy, and, for a
    sandboxed package, the sandbox's home and this process's temporary
    directory (`sandbox`). The card a request names is not here: the
    transcription package's adapter sets it from the job (owner H6).
    """

    parent = os.environ if parent is None else parent
    kind = _process_kind(package)
    environment: dict[str, str] = {}
    for name in system_names(kind):
        value = _lookup(parent, name)
        if value is not None:
            environment[name] = value
    environment["PATH"] = package_path(context, parent, kind)
    environment["PYTHONPATH"] = os.pathsep.join(str(path) for path in package.paths)
    environment["PYTHONNOUSERSITE"] = "1"
    environment["PYTHONIOENCODING"] = "utf-8"
    environment["PYTHONUTF8"] = "0"
    environment.update(proxy_variables(context))
    if sandboxed(package):
        if sandbox is None:
            raise PackageUnavailable(f"引擎包 {package.id} 只在沙箱里运行，但没有为它准备沙箱")
        environment.update(sandbox.variables())
    return environment


#: Run before anything else in a package's process: its import path is its
#: own roots (and the interpreter's own library), its standard streams UTF-8,
#: then the entry module runs as `python -m` would run it.
_BOOTSTRAP = (
    "import sys\n"
    "sys.path[0:0] = {roots!r}\n"
    "for _s, _e in ((sys.stdin, 'strict'), (sys.stdout, 'strict'), (sys.stderr, 'backslashreplace')):\n"
    "    _s.reconfigure(encoding='utf-8', errors=_e)\n"
    "import runpy\n"
    "runpy.run_module({module!r}, run_name='__main__', alter_sys=True)\n"
)


def interpreter_flags(package: Package) -> list[str]:
    """`-I` for every package (no `PYTHON*`, no user site, no script directory); `-B` too when sandboxed."""

    return ["-I", "-B"] if sandboxed(package) else ["-I"]


def package_command(
    package: Package,
    python: str | Path,
    *arguments: str,
    module: str | None = None,
    roots: Iterable[str | Path] | None = None,
) -> list[str]:
    """How a package's process is started: isolated, its entry module run after its import path is set.

    `module` and `roots` default to the package's entry and its own roots
    (the quality gate's stand-ins run another module, with one more root).
    """

    import_roots = [str(root) for root in (roots if roots is not None else package.paths)]
    code = _BOOTSTRAP.format(roots=import_roots, module=module or package.entry)
    return [str(python), *interpreter_flags(package), "-c", code, *arguments]


def package_cwd(package: Package, context: Mapping[str, Any] | None, task_dir: str | Path | None = None) -> str | None:
    """The process's working directory: the task's for the transcription, its own run directory otherwise (H3)."""

    if sandboxed(package) and task_dir is not None:
        return str(task_dir)
    run = ((context or {}).get("paths") or {}).get("run")
    if not run:
        return None
    # The bridge derives its files into it; it is there unless a caller holds
    # on to a context whose run was already released (it is made again, as
    # the bridge always made it).
    Path(run).mkdir(parents=True, exist_ok=True)
    return str(run)


# --- the transcription's sandbox (plan 4.5 item 28) ---------------------------------------------------------------

SANDBOX_DIRECTORY = "sandbox"
SANDBOX_HOME = "home"
SANDBOX_TEMP = "temp"
#: Sorts before anything a library puts in TEMP; held open while the process runs,
#: so a library that empties its TEMP (torch's AOTI loader did) stops at it.
TEMP_ANCHOR = "!!nonoka-temp-anchor"


def sandbox_root(context: Mapping[str, Any] | None) -> Path:
    """`engines\\<package>\\sandbox\\` of the package's data directory (`paths.package_data`)."""

    package_data = ((context or {}).get("paths") or {}).get("package_data")
    if not package_data:
        raise PackageUnavailable("沙箱需要引擎包的数据目录（启动上下文里没有 paths.package_data）")
    return Path(package_data) / SANDBOX_DIRECTORY


class SandboxTemp:
    """The sandbox of one transcription process: the shared home, and a temporary directory of its own.

    The home is shared between runs (the CUDA ComputeCache and torch's kernel
    cache stay warm; owner, appendix B question 18). The temporary directory
    is `sandbox\\temp\\<process>\\`, made before the process starts, with
    `TEMP_ANCHOR` in it held (an advisory lock on an open file, which Windows
    also refuses to delete) until the process has ended; `close` then
    deletes the directory. `sweep_sandbox` deletes the ones whose holder is
    gone.
    """

    def __init__(self, root: Path) -> None:
        self.root = root
        self.home = root / SANDBOX_HOME
        for directory in self._home_directories().values():
            directory.mkdir(parents=True, exist_ok=True)
        self.temp = root / SANDBOX_TEMP / f"{os.getpid()}-{secrets.token_hex(6)}"
        self.temp.mkdir(parents=True)
        self._stack = ExitStack()
        try:
            self._stack.enter_context(fsops.holding_lock(self.temp / TEMP_ANCHOR, timeout=0))
        except BaseException:
            self._stack.close()
            fsops.remove_tree(self.temp)
            raise

    def _home_directories(self) -> dict[str, Path]:
        if os.name != "nt":
            return {"HOME": self.home}
        return {
            "USERPROFILE": self.home,
            "HOME": self.home,
            "APPDATA": self.home / "AppData" / "Roaming",
            "LOCALAPPDATA": self.home / "AppData" / "Local",
        }

    def variables(self) -> dict[str, str]:
        variables = {name: str(path) for name, path in self._home_directories().items()}
        if os.name == "nt":
            # The home again, as a drive and a path (owner H8).
            drive, rest = os.path.splitdrive(str(self.home))
            variables.update({"HOMEDRIVE": drive, "HOMEPATH": rest or "\\"})
        temp = str(self.temp)
        if os.name == "nt":
            variables.update({"TEMP": temp, "TMP": temp})
        else:
            variables["TMPDIR"] = temp
        return variables

    def close(self) -> None:
        self._stack.close()
        try:
            fsops.remove_tree(self.temp)
        except OSError:
            # A file a dying child still holds: the next sweep takes it.
            pass

    def __enter__(self) -> "SandboxTemp":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


def sweep_sandbox(package_dirs: Iterable[Path]) -> list[Path]:
    """Delete the sandbox temporary directories of processes that are gone; returns what was removed."""

    removed: list[Path] = []
    for package_dir in package_dirs:
        root = package_dir / SANDBOX_DIRECTORY / SANDBOX_TEMP
        try:
            entries = sorted(root.iterdir())
        except OSError:
            continue
        for entry in entries:
            if not entry.is_dir() or fsops.is_directory_link(entry):
                continue
            if not fsops.try_lock(entry / TEMP_ANCHOR):
                continue
            try:
                fsops.remove_tree(entry)
            except OSError:
                continue
            removed.append(entry)
    return removed


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
        task_dir: str | Path | None = None,
    ) -> None:
        """Start the package's process, hermetic (plan 4.5).

        `environment` is the parent environment the `system` variables are
        taken from (this process's when None); `task_dir` the transcription's
        working directory.
        """

        self.package = package
        self._on_message = on_message or (lambda _message: None)
        self._lock = threading.Lock()
        self.sandbox: SandboxTemp | None = None
        command = package_command(package, python or interpreter_for(package, context), *arguments)
        kwargs: dict[str, Any] = {}
        if os.name == "nt":
            kwargs["creationflags"] = fsops.no_window()
        try:
            if sandboxed(package):
                root = sandbox_root(context)
                sweep_sandbox([root.parent])
                self.sandbox = SandboxTemp(root)
            #: What the process was handed, exactly (the quality gate's environment record reads it).
            self.environment = package_environment(package, context, environment, sandbox=self.sandbox)
            self.process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=self.environment,
                cwd=package_cwd(package, context, task_dir),
                **kwargs,
            )
        except OSError as error:
            self._release_sandbox()
            raise PackageUnavailable(f"无法启动引擎包 {package.id}：{error}") from error
        except BaseException:
            self._release_sandbox()
            raise
        self._send({"context": dict(context or {})})

    def _release_sandbox(self) -> None:
        sandbox, self.sandbox = self.sandbox, None
        if sandbox is not None:
            sandbox.close()

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
        if code is not None:
            # Only once the process is gone: its TEMP goes with the anchor.
            self._release_sandbox()
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
    """Whether the tool layer has `name`: a package's process finds its tools there only (owner H7)."""

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

    def start(self, package: Package, *, arguments: Iterable[str] = (), task_dir: Any = None) -> PackageProcess:
        process = PackageProcess(
            package, self.context_for(package), on_message=self.relay, arguments=arguments, task_dir=task_dir,
        )
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
        # The task's directory is the process's working directory (plan 4.5 item 28).
        process = self._session.start(self.package, task_dir=job.task_dir)
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
    "CLASSIFICATION",
    "ENGINES_ENVIRONMENT",
    "SandboxTemp",
    "Variable",
    "ask_once",
    "classification_for",
    "package_cwd",
    "package_path",
    "proxy_variables",
    "sandboxed",
    "sweep_sandbox",
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
