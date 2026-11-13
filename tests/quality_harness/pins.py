"""What the runtime is pinned to, and whether an installed runtime matches (plan WP-Q item 5).

`src/nonoka_x/resources/RUNTIME_PINS.json` is the record of the pins: every
package of both runtime locks with its version and artefact hashes, the Python
interpreter build, every tool with its version, address and hash, every model
with its revision and file hashes, and the download sources.

Since WP-R1, `derive()` reads them from Nonoka's own manifests
(`nonoka_x.runtime.manifests`: the runtime lock, the tool manifest with the
interpreter, the model manifest). Three things are not Nonoka's to restate and
still come from `finesub_bootstrap`: the China-mirror lock (`python.cn`, not
copied, plan 4.2 item 13), the download sources, and the engine constants.
`write()` regenerates the record from `derive()`.

`derive_bootstrap()` reads the same pins (all but the interpreter) from the
files `finesub_bootstrap` ships. The transcription package keeps reading those
at run time -- both locks included -- so they are held to the record too
(`problems()`, `scripts/quality_gate.py`, `tests/test_runtime_pins.py`).

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


RULE = (
    "What the FineSub runtime is pinned to. Changing any pin is a separate engine change the "
    "owner approves (docs/transcription-engines-plan.md section 0, rules 5 and 7)."
)

#: The sections `finesub_bootstrap`'s own files state too; `interpreter` is Nonoka's alone.
BOOTSTRAP_SECTIONS = ("python", "tools", "models", "download_sources", "engine_constants")
SECTIONS = (*BOOTSTRAP_SECTIONS, "interpreter")


def _manifests() -> Any:
    import sys

    source = str(ROOT / "src")
    if source not in sys.path:
        sys.path.insert(0, source)
    from nonoka_x.runtime import manifests

    return manifests


def _tools(runtime: dict[str, Any]) -> dict[str, Any]:
    return {
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
    }


def _models(models: dict[str, Any]) -> dict[str, Any]:
    return {
        model_id: {
            **{key: spec[key] for key in ("repo", "revision", "listedOnly") if key in spec},
            "files": [
                {key: item[key] for key in ("name", "url", "size", "sha256") if key in item}
                for item in spec.get("files", [])
            ],
        }
        for model_id, spec in sorted(models["models"].items())
    }


def _interpreter(runtime: dict[str, Any]) -> dict[str, Any]:
    spec = runtime["interpreter"]
    return {
        "platform": runtime.get("platform"),
        **{key: spec[key] for key in ("implementation", "version", "build")},
        "url": spec["asset"]["url"],
        "size": spec["asset"].get("size"),
        "sha256": spec["asset"].get("sha256"),
    }


def _download_sources(bootstrap: Path) -> dict[str, Any]:
    sources = json.loads((bootstrap / "download-sources.json").read_text(encoding="utf-8"))
    return {key: value for key, value in sources.items() if not key.startswith("_")}


def derive() -> dict[str, Any]:
    """The pins, as Nonoka's own manifests state them (`python.cn` and the download sources: bootstrap's)."""

    manifests = _manifests()
    runtime = manifests.tool_manifest()
    return {
        "schema": 1,
        "rule": RULE,
        "python": {
            "requires": manifests.runtime_lock().get("requires-python"),
            "global": _lock_packages(manifests.RUNTIME_LOCK),
            "cn": _lock_packages(BOOTSTRAP / LOCKS["cn"]),
        },
        "interpreter": _interpreter(runtime),
        "tools": _tools(runtime),
        "models": _models(manifests.model_manifest()),
        "download_sources": _download_sources(BOOTSTRAP),
    }


def derive_bootstrap(bootstrap: Path = BOOTSTRAP) -> dict[str, Any]:
    """The pins, as `finesub_bootstrap`'s own files state them (no interpreter: it pins none)."""

    runtime = json.loads((bootstrap / "runtime-manifest.json").read_text(encoding="utf-8"))
    models = json.loads((bootstrap / "model-manifest.json").read_text(encoding="utf-8"))
    return {
        "schema": 1,
        "rule": RULE,
        "python": {
            "requires": tomllib.loads((bootstrap / LOCKS["global"]).read_text(encoding="utf-8")).get("requires-python"),
            **{lock: _lock_packages(bootstrap / name) for lock, name in LOCKS.items()},
        },
        "tools": _tools(runtime),
        "models": _models(models),
        "download_sources": _download_sources(bootstrap),
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


#: Model files Nonoka's manifest pins while `finesub_bootstrap`'s own manifest keeps them on a moving
#: address, with that address. Only the separator's upstream index (owner-approved 2026-11-12, WP-E1):
#: the model layer is built from Nonoka's manifest alone and needs a fixed copy; the engine reads its own
#: manifest, which is not Nonoka's to edit (plan 4.2 item 16), and leaves a present copy alone.
BOOTSTRAP_MOVING = {
    ("separator", "download_checks.json"):
        "https://raw.githubusercontent.com/TRvlvr/application_data/main/filelists/download_checks.json",
}


def bootstrap_view(record: dict[str, Any]) -> dict[str, Any]:
    """The record as `finesub_bootstrap`'s files must state it: `BOOTSTRAP_MOVING` back on its moving address."""

    models = json.loads(json.dumps(record.get("models")))
    for (model_id, name), url in BOOTSTRAP_MOVING.items():
        files = (models or {}).get(model_id, {}).get("files", [])
        for index, item in enumerate(files):
            if item.get("name") == name:
                files[index] = {"name": name, "url": url}
    return {**record, "models": models}


def problems(recorded: dict[str, Any] | None = None) -> list[str]:
    """Every way the record departs from Nonoka's manifests or from `finesub_bootstrap`'s files."""

    recorded = recorded if recorded is not None else json.loads(PINS_FILE.read_text(encoding="utf-8"))
    derived = with_constants(derive())
    found = [
        f"RUNTIME_PINS.json section differs from Nonoka's manifests: {section}"
        for section in sorted(derived.keys() | recorded.keys())
        if derived.get(section) != recorded.get(section)
    ]
    bootstrap = with_constants(derive_bootstrap())
    expected = bootstrap_view(recorded)
    found += [
        f"RUNTIME_PINS.json section differs from finesub_bootstrap's files: {section}"
        for section in BOOTSTRAP_SECTIONS
        if bootstrap.get(section) != expected.get(section)
    ]
    return found


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
    # The tool layer (WP-E3) holds `<directory>\<archive content>` with no version directory; its
    # manifest says which version each tool is. The older layout had `<id>\<version>\` per tool.
    layer_tools = {}
    if (runtime_dir / ".nonoka-layer.json").is_file():
        layer = json.loads((runtime_dir / ".nonoka-layer.json").read_text(encoding="utf-8"))
        layer_tools = {item["id"]: item for item in layer.get("sources", {}).get("tools", [])}
    for tool in pins["tools"]["resources"]:
        if layer_tools:
            item = layer_tools.get(tool["id"])
            directory = runtime_dir / (item["directory"] if item else tool["id"])
            versions = [item["version"]] if item and directory.is_dir() else []
            required = all((directory / name).is_file() for name in tool["required_files"])
            tools[tool["id"]] = {
                "pinned": tool["version"],
                "installed_versions": versions,
                "status": "match" if tool["version"] in versions and required else "absent" if not versions else "differs",
            }
            continue
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
    layers = provisioner.layers
    models, tools = provisioner.model_root(), layers.root("tools") if layers is not None else None
    if models is None or tools is None:
        return {"skipped": f"the model or tool layer is not ready under {provisioner.install_root}"}
    result = measure(Path(python), Path(models), Path(tools))
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
