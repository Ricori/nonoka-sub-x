"""The launch contexts the core hands the engine packages' processes (R0 document 1.15; plan 4.3 item 22).

Neutral: what a process is, not how an engine reads it. Each package's bridge
turns its context into the engine's own variables and files, inside the
package's process; nothing here names an engine's variable or file.

- every package: `paths.package_data` (`engines\\<package>\\`, its data
  directory: where a compatibility `finesub_bootstrap` puts what the original
  puts under `%LOCALAPPDATA%\\FineSub`, D21), `paths.run` (its own run directory, `engines\\<package>\\run\\<process>\\`,
  claimed here and released when the caller closes it), `paths.state_file`
  (the cross-process rate-limit state, in the correction package's directory)
  and `paths.knowledge_root` (the knowledge base, in the knowledge package's),
  where the migration moved them (WP-M2, `migration.legacy`);
- a transcription package: `runtime` (the model layer, this package's cache
  directory under the big-file root, the Python layer's interpreter), the keys
  its `PACKAGE.json` names, the LLM routes (its preparation reads the route
  presets), the proxy;
- a correction package: the keys it names, the LLM routes, endpoints and pins
  (with the provider id of Nonoka Cloud and the route -> task-group table the
  engine's pins use), the proxy;
- the knowledge package: only the paths;
- every package: `runtime.tools`, the tool layer's directories its process
  finds ffmpeg, ffprobe and tokcount in; a correction package also
  `agent_cli.path`, the directories of the local agent CLIs Nonoka detects
  and of the node a script CLI runs on. The core's engine host builds the
  process's `PATH` from these and the system directories only (plan 4.5 item
  27; owner 2026-11-18, WP-H question H1).
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .engine_host import EngineRun, Package
from .migration.legacy import knowledge_root, limit_state_file


def proxy_context() -> dict[str, Any]:
    """Whether the app's proxy policy applies in this process, and the route it takes now."""

    from .runtime import fetch

    policy = fetch.app_policy_installed()
    return {"policy": policy, "url": fetch.live_proxy() if policy else None}


#: The tools a package's process finds by name on `PATH`.
PATH_TOOLS = ("ffmpeg", "ffprobe", "tokcount")
#: What a script-shim CLI (an npm install's `.cmd`) runs on, found where the user's PATH finds it.
SCRIPT_RUNTIME = "node"


def tool_directories(provisioner: Any) -> list[str]:
    """The tool layer's directories holding `PATH_TOOLS`, in that order; empty without a tool layer."""

    tool_path = getattr(provisioner, "tool_path", None)
    if tool_path is None:
        return []
    directories: list[str] = []
    for name in PATH_TOOLS:
        executable = tool_path(name)
        if executable is not None and str(Path(executable).parent) not in directories:
            directories.append(str(Path(executable).parent))
    return directories


def agent_cli_directories() -> list[str]:
    """Where the local agent CLIs Nonoka detects are, and the node a script CLI needs (owner H1).

    Every detected CLI's directory, on the user's PATH or not: the process's
    PATH is built from nothing, so one the engine finds by name has to be
    named. A CLI that is not a native executable (an npm `.cmd` shim) runs on
    node, which the engine also looks up by name (`shutil.which("node.exe")`),
    so node's directory -- the one the user's own PATH finds -- is added too.
    """

    import shutil

    from .settings import LOCAL_AGENT_PROVIDERS, local_agent_executable

    directories: list[str] = []
    needs_script_runtime = False
    for provider in LOCAL_AGENT_PROVIDERS:
        executable = local_agent_executable(provider)
        if executable is None:
            continue
        if str(executable.parent) not in directories:
            directories.append(str(executable.parent))
        if executable.suffix.lower() != ".exe":
            needs_script_runtime = True
    if needs_script_runtime:
        node = shutil.which(SCRIPT_RUNTIME)
        if node is not None and str(Path(node).parent) not in directories:
            directories.append(str(Path(node).parent))
    return directories


def _call(settings: Any, name: str, default: Any, *args: Any) -> Any:
    """`settings.<name>(*args)`, or `default` for a settings object that has no such call (tests' stand-ins)."""

    call = getattr(settings, name, None)
    return call(*args) if call is not None else default


def _llm(settings: Any) -> dict[str, Any]:
    from .settings import CLOUD_PROVIDER_ID, TASK_GROUPS_BY_ROUTE

    llm = dict(_call(settings, "llm_context", {}))
    llm["cloud_provider"] = CLOUD_PROVIDER_ID
    llm["task_groups"] = {route: list(groups) for route, groups in TASK_GROUPS_BY_ROUTE.items()}
    return llm


def package_context(
    package: Package,
    *,
    paths: Any,
    run: Path,
    settings: Any = None,
    provisioner: Any = None,
    proxy: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """One package's launch context. `paths` is the data layout (`nonoka_x.runtime.paths.NonokaPaths`)."""

    context: dict[str, Any] = {
        "schema": 1,
        "package_id": package.id,
        "paths": {
            "package_data": str(paths.engine(package.id)),
            "run": str(run),
            "state_file": str(limit_state_file(paths)),
            "knowledge_root": str(knowledge_root(paths)),
        },
    }
    tools = tool_directories(provisioner)
    if package.kind == "knowledge":
        context["runtime"] = {"tools": tools}
        return context
    context["secrets"] = dict(_call(settings, "engine_secrets", {}, package.secrets))
    context["llm"] = _llm(settings)
    context["proxy"] = dict(proxy) if proxy is not None else {"policy": False, "url": None}
    if package.kind == "transcription":
        model_dir = getattr(provisioner, "engine_model_dir", None)
        models = model_dir() if model_dir is not None else paths.layer("models") / "unavailable"
        worker_python = getattr(provisioner, "worker_python", None)
        python = worker_python() if worker_python is not None else None
        context["runtime"] = {
            "models": str(models),
            "cache": str(paths.cache / package.id),
            "tools": tools,
            **({"python": str(python)} if python else {}),
        }
    else:
        context["runtime"] = {"tools": tools}
        context["agent_cli"] = {"path": agent_cli_directories()}
    return context


class LaunchContexts:
    """The contexts of one set of package processes, and the run directories they own.

    `close()` releases every run directory claimed here; call it once the
    processes have ended.
    """

    def __init__(self) -> None:
        self.contexts: dict[str, dict[str, Any]] = {}
        self.runs: list[EngineRun] = []

    def close(self) -> None:
        for run in reversed(self.runs):
            run.close()
        self.runs.clear()

    def __enter__(self) -> "LaunchContexts":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


def build(
    packages: Mapping[str, Package],
    *,
    paths: Any,
    settings: Any = None,
    provisioner: Any = None,
    proxy: Mapping[str, Any] | None = None,
) -> LaunchContexts:
    """A context, and a fresh run directory, for each of `packages`."""

    built = LaunchContexts()
    try:
        for package in packages.values():
            run = EngineRun.claim(paths.engine(package.id))
            built.runs.append(run)
            built.contexts[package.id] = package_context(
                package, paths=paths, run=run.directory, settings=settings, provisioner=provisioner, proxy=proxy,
            )
    except BaseException:
        built.close()
        raise
    return built


__all__ = ["LaunchContexts", "build", "knowledge_root", "limit_state_file", "package_context", "proxy_context"]
