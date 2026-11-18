"""The launch contexts the core hands the engine packages' processes (R0 document 1.15; plan 4.3 item 22).

Neutral: what a process is, not how an engine reads it. Each package's bridge
turns its context into the engine's own variables and files, inside the
package's process; nothing here names an engine's variable or file.

- every package: `paths.package_data` (`engines\\<package>\\`, its data
  directory: where a compatibility `finesub_bootstrap` puts what the original
  puts under `%LOCALAPPDATA%\\FineSub`, D21), `paths.run` (its own run directory, `engines\\<package>\\run\\<process>\\`,
  claimed here and released when the caller closes it), `paths.state_file`,
  `paths.knowledge_root` (both still in the main data directory until the
  migration moves them, WP-M2);
- a transcription package: `runtime` (the model layer, this package's cache
  directory under the big-file root, the Python layer's interpreter), the keys
  its `PACKAGE.json` names, the LLM routes (its preparation reads the route
  presets), the proxy;
- a correction package: the keys it names, the LLM routes, endpoints and pins
  (with the provider id of Nonoka Cloud and the route -> task-group table the
  engine's pins use), the proxy;
- the knowledge package: only the paths.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .engine_host import EngineRun, Package


def proxy_context() -> dict[str, Any]:
    """Whether the app's proxy policy applies in this process, and the route it takes now."""

    from .runtime import fetch

    policy = fetch.app_policy_installed()
    return {"policy": policy, "url": fetch.live_proxy() if policy else None}


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
            "state_file": str(paths.data / "state"),
            "knowledge_root": str(paths.data / "knowledge"),
        },
    }
    if package.kind == "knowledge":
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
            **({"python": str(python)} if python else {}),
        }
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


__all__ = ["LaunchContexts", "build", "package_context", "proxy_context"]
