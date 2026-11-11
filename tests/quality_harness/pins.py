"""What the runtime is pinned to, and whether an installed runtime matches (plan WP-Q item 5).

`src/nonoka_x/resources/RUNTIME_PINS.json` is the record of the pins as they
stand before the runtime moves to Nonoka's own manifests (WP-R1): every
package of both runtime locks with its version and artefact hashes, every tool
with its version, address and hash, every model with its revision and file
hashes, and the download sources. `derive()` reads them from the files
`finesub_bootstrap` ships today; `tests/test_runtime_pins.py` holds the record
to them, and WP-R1 points the same comparison at Nonoka's copies.

`measure()` looks at an installed runtime on this machine -- the interpreter's
installed distributions, the model files on disk, the tool directories -- and
compares it with the record. It reads only; nothing is installed or fixed.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tomllib
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlparse

from . import ROOT, VENDOR_SOURCE

PINS_FILE = ROOT / "src" / "nonoka_x" / "resources" / "RUNTIME_PINS.json"
BOOTSTRAP = VENDOR_SOURCE / "finesub_bootstrap"
LOCKS = {"global": "pylock.win-py312.toml", "cn": "pylock.win-py312.cn.toml"}
MEASURED_FILE = ROOT / "test_data" / "quality-baseline" / "runtime-measured.json"


def _artifact(entry: dict[str, Any]) -> dict[str, Any]:
    return {
        "file": PurePosixPath(urlparse(entry["url"]).path).name,
        "url": entry["url"],
        "size": entry.get("size"),
        "sha256": (entry.get("hashes") or {}).get("sha256"),
    }


def _lock_packages(path: Path) -> list[dict[str, Any]]:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    packages = []
    for package in data.get("packages", []):
        record: dict[str, Any] = {"name": package["name"], "version": package.get("version")}
        if package.get("marker"):
            record["marker"] = package["marker"]
        record["wheels"] = [_artifact(wheel) for wheel in package.get("wheels", [])]
        if package.get("sdist"):
            record["sdist"] = _artifact(package["sdist"])
        for key in ("vcs", "directory", "archive"):
            if key in package:
                record[key] = package[key]
        packages.append(record)
    return sorted(packages, key=lambda item: (item["name"], item.get("marker", "")))


def derive(bootstrap: Path = BOOTSTRAP) -> dict[str, Any]:
    """The pins, as the bootstrap's own files state them."""

    runtime = json.loads((bootstrap / "runtime-manifest.json").read_text(encoding="utf-8"))
    models = json.loads((bootstrap / "model-manifest.json").read_text(encoding="utf-8"))
    sources = json.loads((bootstrap / "download-sources.json").read_text(encoding="utf-8"))
    return {
        "schema": 1,
        "rule": (
            "What the FineSub runtime is pinned to. Changing any pin is a separate engine change the "
            "owner approves (docs/transcription-engines-plan.md section 0, rules 5 and 7)."
        ),
        "python": {
            "requires": tomllib.loads((bootstrap / LOCKS["global"]).read_text(encoding="utf-8")).get("requires-python"),
            **{lock: _lock_packages(bootstrap / name) for lock, name in LOCKS.items()},
        },
        "tools": {
            "platform": runtime.get("platform"),
            "resources": [
                {
                    "id": item["id"],
                    "version": item["version"],
                    "url": item["asset"]["url"],
                    "size": item["asset"].get("size"),
                    "sha256": item["asset"].get("sha256"),
                    "required_files": item.get("required_files", []),
                }
                for item in runtime["resources"]
            ],
        },
        "models": {
            model_id: {
                **{key: spec[key] for key in ("repo", "revision", "listedOnly") if key in spec},
                "files": [
                    {key: item[key] for key in ("name", "url", "size", "sha256") if key in item}
                    for item in spec.get("files", [])
                ],
            }
            for model_id, spec in sorted(models["models"].items())
        },
        "download_sources": {key: value for key, value in sources.items() if not key.startswith("_")},
    }


#: `finesub_bootstrap` modules whose constants decide which model the engine
#: loads or what its products are called -- values, not behaviour.
CONSTANT_MODULES = ("finesub_bootstrap.model_caches", "finesub_bootstrap.artifacts")


def engine_constants() -> dict[str, Any]:
    """The JSON-able upper-case constants of `CONSTANT_MODULES`, by module."""

    import importlib
    import sys

    if str(VENDOR_SOURCE) not in sys.path:
        sys.path.insert(0, str(VENDOR_SOURCE))
    found: dict[str, Any] = {}
    for module_name in CONSTANT_MODULES:
        module = importlib.import_module(module_name)
        values = {}
        for name in sorted(vars(module)):
            if not re.fullmatch(r"[A-Z][A-Z0-9_]*", name):
                continue
            value = getattr(module, name)
            if isinstance(value, (frozenset, set)):
                value = sorted(value)
            try:
                # Round-tripped, so a tuple compares equal to its recorded list.
                values[name] = json.loads(json.dumps(value))
            except TypeError:
                continue
        found[module_name] = values
    return found


def with_constants(pins: dict[str, Any]) -> dict[str, Any]:
    return {**pins, "engine_constants": engine_constants()}


def render(pins: dict[str, Any]) -> bytes:
    return (json.dumps(pins, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def write(path: Path = PINS_FILE) -> Path:
    path.write_bytes(render(with_constants(derive())))
    return path


# --- what the locked engine needs from finesub_bootstrap --------------------------

SURFACE_FILE = ROOT / "tests" / "fixtures" / "quality" / "bootstrap-surface.json"


def bootstrap_surface() -> list[str]:
    """Every `finesub_bootstrap` name the locked engine files use, as `module:name`.

    `from finesub_bootstrap.x import y` gives `finesub_bootstrap.x:y`; a module
    bound by name (`from finesub_bootstrap import secrets`) also contributes
    each attribute the file reads off it (`finesub_bootstrap.secrets:read_env_file`).
    Whatever supplies `finesub_bootstrap` to the engine has to supply all of these.
    """

    import ast

    from .lock import is_unlocked, iter_files

    surface: set[str] = set()
    for relative in iter_files():
        if is_unlocked(relative) or not relative.endswith(".py"):
            continue
        tree = ast.parse((VENDOR_SOURCE / relative).read_text(encoding="utf-8"))
        bound: dict[str, str] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module and \
                    node.module.split(".")[0] == "finesub_bootstrap":
                for alias in node.names:
                    surface.add(f"{node.module}:{alias.name}")
                    # Read off the statement, not off the disk, so the surface
                    # stays the same once finesub_bootstrap/ is gone: the engine
                    # imports only submodules from the package itself.
                    if node.module == "finesub_bootstrap":
                        bound[alias.asname or alias.name] = f"{node.module}.{alias.name}"
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.split(".")[0] == "finesub_bootstrap":
                        surface.add(f"{alias.name}:")
                        if alias.asname:
                            bound[alias.asname] = alias.name
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id in bound:
                surface.add(f"{bound[node.value.id]}:{node.attr}")
    return sorted(surface)


def unresolvable(surface: list[str]) -> list[str]:
    """The entries of `surface` that do not import here; run in a child that refuses torch."""

    import sys

    script = (
        "import importlib, importlib.abc, json, sys\n"
        "BLOCKED = {'torch', 'numpy', 'soundfile', 'ctranslate2', 'transformers'}\n"
        "class Refuse(importlib.abc.MetaPathFinder):\n"
        "    def find_spec(self, name, path=None, target=None):\n"
        "        if name.split('.')[0] in BLOCKED: raise ImportError('blocked: ' + name)\n"
        "sys.meta_path.insert(0, Refuse())\n"
        "missing = []\n"
        "for entry in json.loads(sys.stdin.read()):\n"
        "    module, _, name = entry.partition(':')\n"
        "    try:\n"
        "        loaded = importlib.import_module(module)\n"
        "        if name and not hasattr(loaded, name):\n"
        "            importlib.import_module(module + '.' + name)\n"
        "    except Exception as error:\n"
        "        missing.append(f'{entry} ({type(error).__name__}: {error})')\n"
        "print(json.dumps(missing))\n"
    )
    # Nonoka's own source first: once finesub_bootstrap/ is deleted, the
    # compatibility package that replaces it lives there (plan 4.16).
    search = os.pathsep.join([str(ROOT / "src"), str(VENDOR_SOURCE)])
    completed = subprocess.run(
        [sys.executable, "-c", script], input=json.dumps(surface), capture_output=True, text=True,
        encoding="utf-8", env={**os.environ, "PYTHONPATH": search}, timeout=120,
    )
    if completed.returncode != 0:
        return [f"<the check itself failed: {completed.stderr[-1000:]}>"]
    return json.loads(completed.stdout)


# --- an installed runtime ---------------------------------------------------------


def _canonical(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def measure(python: Path, models_dir: Path, runtime_dir: Path, pins: dict[str, Any] | None = None) -> dict[str, Any]:
    """An installed runtime against the pins. Reads only."""

    pins = pins or json.loads(PINS_FILE.read_text(encoding="utf-8"))
    listing = subprocess.run(
        [str(python), "-I", "-c",
         "import importlib.metadata as m, json; "
         "print(json.dumps(sorted({(d.metadata['Name'], d.version) for d in m.distributions()})))"],
        capture_output=True, text=True, encoding="utf-8", timeout=120,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    installed = {_canonical(name): version for name, version in json.loads(listing.stdout or "[]")}
    locked = {_canonical(item["name"]): item["version"] for item in pins["python"]["global"]}
    packages = {
        "installed_count": len(installed),
        "locked_count": len(locked),
        "version_mismatch": sorted(
            f"{name}: locked {locked[name]}, installed {installed[name]}"
            for name in locked.keys() & installed.keys() if locked[name] != installed[name]
        ),
        "locked_not_installed": sorted(locked.keys() - installed.keys()),
        "installed_not_locked": sorted(f"{name} {installed[name]}" for name in installed.keys() - locked.keys()),
        "note": "versions only: an installed wheel keeps no hash of the wheel it came from",
    }

    on_disk: dict[tuple[str, int], list[Path]] = {}
    for path in models_dir.rglob("*"):
        if path.is_file():
            try:
                on_disk.setdefault((path.name, path.stat().st_size), []).append(path)
            except OSError:
                continue
    models: dict[str, Any] = {}
    for model_id, spec in pins["models"].items():
        files = []
        for item in spec["files"]:
            if not item.get("sha256"):
                continue
            # By the file's own name (a listed name may carry its directory), and
            # by the Hugging Face blob, which is named after its sha256.
            candidates = (
                on_disk.get((PurePosixPath(item["name"]).name, item.get("size")), [])
                + on_disk.get((item["sha256"], item.get("size")), [])
            )
            if not candidates:
                files.append({"name": item["name"], "status": "absent"})
                continue
            digests = {_sha256(path) for path in candidates}
            files.append({
                "name": item["name"],
                "status": "match" if item["sha256"] in digests else "differs",
                **({} if item["sha256"] in digests else {"found": sorted(digests)}),
            })
        statuses = {entry["status"] for entry in files}
        models[model_id] = {
            "status": (
                "match" if statuses == {"match"}
                else "absent" if statuses <= {"absent"}
                else "differs" if "differs" in statuses
                else "partial"
            ),
            "files": files,
        }

    tools = {}
    for tool in pins["tools"]["resources"]:
        directory = runtime_dir / tool["id"]
        versions = sorted(path.name for path in directory.iterdir() if path.is_dir()) if directory.is_dir() else []
        required = all((directory / tool["version"] / name).is_file() for name in tool["required_files"])
        tools[tool["id"]] = {
            "pinned": tool["version"],
            "installed_versions": versions,
            "status": "match" if tool["version"] in versions and required else "absent" if not versions else "differs",
        }
    return {"python": str(python), "packages": packages, "models": models, "tools": tools}


def measure_installed() -> dict[str, Any]:
    """`measure` on the runtime `real.locate_runtime` finds; written to test_data/.

    The packages are those of `NONOKA_QUALITY_PYTHON` when it is set
    (`real.python_override`); models and tools are always the located runtime's.
    """

    from .real import locate_runtime, python_override

    import sys

    sys.path[:0] = [str(ROOT / "src"), str(VENDOR_SOURCE)]
    from nonoka_x.provision import RuntimeProvisioner

    data_dir, install_dir = locate_runtime()
    provisioner = RuntimeProvisioner(data_dir, ROOT / "third_party" / "finesub", install_dir)
    python = python_override() or provisioner.worker_python()
    if python is None:
        return {"skipped": f"no ready managed runtime under {provisioner.install_root}"}
    result = measure(Path(python), Path(provisioner.paths.models), Path(provisioner.paths.runtime))
    MEASURED_FILE.parent.mkdir(parents=True, exist_ok=True)
    MEASURED_FILE.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def measured_problems(result: dict[str, Any]) -> list[str]:
    """Every way an installed runtime departs from the pins, one line each."""

    if "skipped" in result:
        return []
    problems = []
    packages = result["packages"]
    problems += [f"package version: {line}" for line in packages["version_mismatch"]]
    problems += [f"package locked but not installed: {name}" for name in packages["locked_not_installed"]]
    problems += [f"package installed but not locked: {line}" for line in packages["installed_not_locked"]]
    for model_id, model in result["models"].items():
        for item in model["files"]:
            if item["status"] == "differs":
                problems.append(f"model {model_id}: {item['name']} hash differs")
    for tool_id, tool in result["tools"].items():
        if tool["status"] == "differs":
            problems.append(f"tool {tool_id}: pinned {tool['pinned']}, installed {tool['installed_versions']}")
    return problems


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("write", "measure"))
    args = parser.parse_args()
    if args.command == "write":
        print(write())
    else:
        print(json.dumps(measure_installed(), ensure_ascii=False, indent=2))
    os._exit(0)
