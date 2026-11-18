"""The environment and generated config a FineSub worker is started with.

Replays what `nonoka_x.sidecar.main` does at startup -- name the preferences
file, install the proxy policy, bind `FineSubSettings`, save fixed settings,
refresh the saved routes, build the `RuntimeProvisioner` and the
`LocalProvider` -- in a child interpreter with a scrubbed environment, then
records:

- compared: every variable FineSub reads (`finesub_facing`) in the
  environment its engine processes run in, with paths recorded by role so
  the data directories can move -- since WP-R5 that includes the keys, which
  are handed as variables (D3); and the content of the files
  `FINESUB_CONFIG_FILE`, `FINESUB_MODEL_CATALOG` and `FINESUB_ENV_FILE` point
  at. Since WP-P1 the two halves run in two processes (the transcription and
  the correction package's), each environment made by its package's bridge
  from the launch context the worker builds; what is compared is the two put
  together the way one process ran both halves before (the transcription's,
  then the correction's over it), and each one is recorded beside it;
- recorded but not compared: the launcher's part (Python variables, PATH,
  the interpreter), which the hermetic launcher (WP-H) rebuilds.

The machine-dependent inputs are pinned: no preferences proxy and the
download route pinned direct (otherwise the Windows system proxy leaks in),
and the agent CLI directories found on this machine replaced by a placeholder. Run as
`python -m tests.quality_harness.environment --child <spec.json>`.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from . import ENGINES, FIXTURES, ROOT
from .calls import KNOWLEDGE_ENTRY, SETTINGS_PROFILES, Normalizer, bind_settings, hermetic_environment, render

BASELINE = FIXTURES / "environment.json"
#: The generated files, by the variable that points the engine at each.
GENERATED_FILE_VARIABLES = ("FINESUB_CONFIG_FILE", "FINESUB_MODEL_CATALOG", "FINESUB_ENV_FILE")


#: What FineSub, its model loaders and its HTTP clients read from the
#: environment. Everything else the worker gets is the launcher's business.
FINESUB_FACING_PREFIXES = ("FINESUB_", "HF_", "HUGGINGFACE", "TRANSFORMERS_", "TORCH_HOME")
FINESUB_FACING_NAMES = frozenset(name.lower() for name in (
    "CUDA_VISIBLE_DEVICES", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
    # numba's cache, which would otherwise be written into the Python layer (WP-R5).
    "NUMBA_CACHE_DIR",
    # The keys and endpoints FineSub reads by name; since WP-R5 the keys are
    # handed as process variables (D3). Every name the settings page stores,
    # so one that should not reach the engine shows up as well.
    "GEMINI_FREE", "GEMINI_PAID", "EXA_KEYS", "TAVILY_KEYS", "ANTHROPIC_API_KEY", "OPENAI_API_KEY",
    "DEEPSEEK_API_KEY", "KIMI_API_KEY", "DASHSCOPE_API_KEY", "OPENAI_COMPAT_API_KEY",
    "ANTHROPIC_COMPAT_API_KEY", "HF_TOKEN", "NONOKA_CLOUD_KEY",
    "GEMINI_BASE_URL", "OPENAI_BASE_URL", "ANTHROPIC_BASE_URL", "OPENAI_COMPAT_BASE_URL",
    "ANTHROPIC_COMPAT_BASE_URL", "NONOKA_CLOUD_BASE_URL",
))
#: Set by this harness for its own determinism, not by Nonoka. Empty since WP-R5:
#: `FINESUB_ENV_PROTECT` is the bridge's now (D22) and compared like the rest.
HARNESS_SETTINGS: frozenset[str] = frozenset()


def finesub_facing(name: str) -> bool:
    return name.upper().startswith(FINESUB_FACING_PREFIXES) or name.lower() in FINESUB_FACING_NAMES


def _role_of(value: str, environment: dict[str, str]) -> Any:
    """A path by what it is, not where: data directories may be renamed or moved (plan 4.17).

    What has to stay is the relation the locked engine relies on -- the model
    caches inside the model directory -- and what kind of thing each variable
    points at. The generated files are compared by content separately.
    """

    if not value or not Path(value).is_absolute():
        return value
    path = Path(value)
    model_dir = environment.get("FINESUB_MODEL_DIR")
    if model_dir:
        try:
            relative = path.relative_to(model_dir)
            return "$MODEL_DIR" + ("" if str(relative) == "." else "/" + relative.as_posix())
        except ValueError:
            pass
    kind = "file" if path.is_file() else "directory" if path.is_dir() else "absent"
    return f"<a Nonoka-managed path: {kind}>"


def _read_if_file(value: str | None) -> str | None:
    if not value or not Path(value).is_file():
        return None
    return Path(value).read_text(encoding="utf-8")


def dump_hook(spec: dict[str, Any], entry: str, context: dict[str, Any]) -> None:
    """In an engine package's process, once its bridge ran: what it now has, as `<out>.<entry>.json`."""

    environment = dict(os.environ)
    record = {
        "environment": environment,
        "files": {variable: _read_if_file(environment.get(variable)) for variable in GENERATED_FILE_VARIABLES},
        "roles": {name: _role_of(value, environment) for name, value in environment.items() if finesub_facing(name)},
    }
    Path(f"{spec['out']}.{entry}.json").write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")


def engine_processes(provider: Any, worker_environment: dict[str, str], out: Path) -> dict[str, Any]:
    """Start the transcription and the correction package as a task's worker would, and read what each has."""

    from nonoka_x import engine_host

    from .package_child import use_in_worker

    use_in_worker({"hook": "tests.quality_harness.environment:dump_hook", "out": str(out)}, out.with_suffix(".child.json"))
    packages = provider.packages()
    seen: dict[str, Any] = {}
    with provider.launch_contexts(packages) as built:
        for kind in ("transcription", "correction"):
            for package in engine_host.of_kind(packages, kind):
                if package.entry == KNOWLEDGE_ENTRY:
                    continue
                with engine_host.PackageProcess(
                    package, built.contexts[package.id], environment=worker_environment
                ) as process:
                    spawned = dict(process.environment)
                item = json.loads(Path(f"{out}.{package.entry}.json").read_text(encoding="utf-8"))
                item["environment"] = _as_handed(spawned, item["environment"])
                seen[kind] = item
    return seen


def _as_handed(spawned: dict[str, str], inside: dict[str, str]) -> dict[str, str]:
    """The environment a process was handed (`spawned`), with what its bridge changed in it (`inside`).

    The view the baseline was recorded in: the mapping handed to the engine's
    process, spelled as it was handed. Inside a Windows process names are
    case-insensitive (`NO_PROXY` and `no_proxy` are one variable), so a name
    handed in two spellings is kept in both, with the value the process has.
    """

    inside_upper = {name.upper(): value for name, value in inside.items()}
    view = {name: inside_upper[name.upper()] for name in spawned if name.upper() in inside_upper}
    spelled = {name.upper() for name in view}
    view.update({name: value for name, value in inside.items() if name.upper() not in spelled})
    return view


def child_main(spec_path: Path) -> int:
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    root = Path(spec["root"])
    data_dir = root / "data"
    from . import stubs

    sys.meta_path.insert(0, stubs.RefuseHeavyImports())
    # Set only so this child could import the code; the sidecar starts without them.
    for name in ("PYTHONPATH", "PYTHONIOENCODING", "PYTHONDONTWRITEBYTECODE"):
        os.environ.pop(name, None)
    starting = dict(os.environ)

    from nonoka_x.download_route import PREFERENCES_ENVIRONMENT, install_app_proxy_policy

    os.environ.setdefault(PREFERENCES_ENVIRONMENT, str(data_dir / "preferences.json"))
    install_app_proxy_policy()
    settings = bind_settings(data_dir, spec["profile"])

    from nonoka_x import local_provider
    from nonoka_x.provision import RuntimeProvisioner

    provisioner = RuntimeProvisioner(data_dir, ENGINES, None)
    provider = local_provider.LocalProvider(
        data_dir / "tasks", ENGINES, settings=settings, provisioner=provisioner, issues=[]
    )
    agent_dirs = local_provider.local_agent_path_entries()
    worker_environment = provider._worker_environment()
    processes = engine_processes(provider, worker_environment, Path(spec["out"]).with_suffix(".engine"))
    # The two halves together, as one process ran them before: the
    # transcription's, then the correction's over it.
    environment = dict(processes["transcription"]["environment"])
    environment.update(processes["correction"]["environment"])

    starting_path = starting.get("PATH", "")
    path = environment.get("PATH", "")
    prepended = path[: -len(starting_path)] if starting_path and path.endswith(starting_path) else path
    path_entries = ["$AGENT_DIR" if entry in agent_dirs else entry for entry in prepended.split(os.pathsep) if entry]
    changed = {
        name: value
        for name, value in sorted(environment.items())
        if name != "PATH" and starting.get(name) != value
    }
    removed = sorted(name for name in starting if name not in environment)
    # By role as each process saw it (its run directory is gone by now).
    roles = {name.upper(): role for name, role in processes["transcription"]["roles"].items()}
    roles.update({name.upper(): role for name, role in processes["correction"]["roles"].items()})
    facing = {
        name: roles[name.upper()]
        for name in sorted(environment)
        if finesub_facing(name) and name not in HARNESS_SETTINGS
    }
    result = {
        # Compared: what FineSub itself reads, paths by role (see `_role_of`).
        "finesub_environment": facing,
        # Recorded, not compared: how the launcher starts Python and where it
        # keeps its own things. WP-H rebuilds exactly this part.
        "launcher_not_compared": {
            "changed": {name: value for name, value in changed.items() if not finesub_facing(name)},
            "removed": removed,
            "path_prepended": path_entries,
            "path_keeps_the_rest": path.endswith(starting_path),
            "worker_python": str(provisioner.worker_python()),
            # What each engine process reads of its own, by role.
            "per_process": {kind: item["roles"] for kind, item in processes.items()},
        },
        # Read through the variables the worker gets, not from where the
        # settings happen to write today: what is compared is what the engine
        # is pointed at, wherever a later bridge puts it.
        "files": dict(processes["correction"]["files"]),
    }
    Path(spec["out"]).write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    return 0


def run_profile(profile: str, scratch: Path, *, prepare: Any = None) -> dict[str, Any]:
    """One settings profile's record. `prepare(root, environment)`: WP-H's poisoning (see `calls.run_scenario`)."""

    root = (scratch / profile).resolve()
    if root.exists():
        shutil.rmtree(root)
    for directory in ("data", "tmp"):
        (root / directory).mkdir(parents=True)
    spec = {"profile": profile, "root": str(root), "out": str(root / "record.json")}
    (root / "spec.json").write_text(json.dumps(spec), encoding="utf-8")
    environment = hermetic_environment(root)
    environment["NONOKA_DOWNLOAD_DIRECT"] = "1"
    cwd = prepare(root, environment) if prepare is not None else None
    completed = subprocess.run(
        [sys.executable, "-m", "tests.quality_harness.environment", "--child", str(root / "spec.json")],
        cwd=cwd or ROOT, env=environment, capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=300,
    )
    out = root / "record.json"
    if completed.returncode != 0 or not out.is_file():
        raise RuntimeError(f"environment {profile} child failed ({completed.returncode}):\n{completed.stderr[-4000:]}")
    raw = json.loads(out.read_text(encoding="utf-8"))
    normalizer = Normalizer({"$ROOT": root, "$REPO": ROOT, "$PYTHON": Path(sys.executable).parent})
    return normalizer.value(raw)


def run_all() -> dict[str, Any]:
    scratch = Path(tempfile.mkdtemp(prefix="nonoka-quality-env-"))
    try:
        return {profile: run_profile(profile, scratch) for profile in SETTINGS_PROFILES}
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def write_baseline(record: dict[str, Any]) -> None:
    BASELINE.parent.mkdir(parents=True, exist_ok=True)
    BASELINE.write_bytes(render(record).encode("utf-8"))


def compared(record: dict[str, Any]) -> dict[str, Any]:
    return {
        profile: {key: value for key, value in item.items() if key != "launcher_not_compared"}
        for profile, item in record.items()
    }


def difference(record: dict[str, Any]) -> str:
    """A unified diff of the compared part against the baseline; empty when it matches."""

    import difflib

    actual = render(compared(record))
    expected = render(compared(json.loads(BASELINE.read_text(encoding="utf-8")))) if BASELINE.is_file() else ""
    if actual == expected:
        return ""
    return "".join(difflib.unified_diff(
        expected.splitlines(keepends=True), actual.splitlines(keepends=True),
        fromfile=f"baseline {BASELINE.relative_to(ROOT).as_posix()}", tofile="current", n=2,
    )) or f"{BASELINE.relative_to(ROOT).as_posix()} is missing"


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--child", type=Path, required=True)
    raise SystemExit(child_main(parser.parse_args().child))
