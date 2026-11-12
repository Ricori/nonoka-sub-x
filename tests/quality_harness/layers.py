"""The prebuilt layers against `RUNTIME_PINS.json` (plan section 0 rule 5, WP-Q table "预制层", WP-E1).

`check(paths)` takes what `scripts/quality_gate.py --layers` is given, each one of:

- a build output directory (`dist/layers/`, holding `<layer>/<version>/{layer.tar.zst,manifest.json}`):
  every archive is unpacked into a scratch directory with Windows' `tar.exe` and checked file by file;
- one version directory of it (`dist/layers/python/<version>/`): the same, for that layer;
- an unpacked layer directory (it carries `.nonoka-layer.json` at its root): checked in place,
  every file hashed again;
- a manifest file alone: only what the manifest itself states is checked (its sources against the
  pins); the files are not, and the result says so.

What is checked (4.2 item 17):

- every layer: the manifest's version is the one its directory is named for; with files at hand,
  the tree matches the manifest exactly (no file more or less, sizes, sha256, no links);
- the Python environment layer: the interpreter build and its sha256 against the pins; every package
  the lock installs on Windows is there (`*.dist-info` name and version), installed from the locked
  wheel whose sha256 the build checked (`sources.packages` against the pins); every file a package
  installed matches its `RECORD`; every other file is the interpreter archive's (same sha256),
  bytecode compiled from a source in the layer, or a listed exception, and exceptions are only
  `Scripts\\*.exe` and `Scripts\\numba`;
- the model layer: exactly the models `layer_manifest.MODEL_LAYER_IDS` names, at the pinned repo and
  revision, every pinned file there with the pinned size and sha256; nothing but snapshot files, the
  engine's markers and the separator files (no `accel\\`, no `blobs\\`); the markers say what the
  engine expects, and the stamped separator files carry `FIXED_MTIME`;
- the tool layer: every pinned tool but uv, at the pinned version, address and sha256, each in its
  own directory with its required files.
"""

from __future__ import annotations

import base64
import csv
import io
import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from . import ROOT

SYSTEM_TAR = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "tar.exe"


def _modules() -> tuple[Any, Any]:
    import sys

    for path in (str(ROOT / "src"), str(ROOT)):
        if path not in sys.path:
            sys.path.insert(0, path)
    from nonoka_x.runtime import layer_manifest

    from scripts import build_layers

    return layer_manifest, build_layers


def pins() -> dict[str, Any]:
    return json.loads((ROOT / "src" / "nonoka_x" / "resources" / "RUNTIME_PINS.json").read_text(encoding="utf-8"))


def _record_hash(hex_digest: str) -> str:
    return "sha256=" + base64.urlsafe_b64encode(bytes.fromhex(hex_digest)).decode().rstrip("=")


# --- what was given ------------------------------------------------------------------------------------


class Target:
    """One layer to check: its manifest, and where its files are (None: manifest only)."""

    def __init__(self, label: str, manifest_path: Path | None, root: Path | None, archive: Path | None) -> None:
        self.label, self.manifest_path, self.root, self.archive = label, manifest_path, root, archive


def targets(paths: list[Path]) -> list[Target]:
    lm, _ = _modules()
    found: list[Target] = []
    for path in paths:
        path = Path(path)
        if path.is_file():
            found.append(Target(str(path), path, None, None))
        elif (path / lm.MANIFEST_NAME).is_file():
            found.append(Target(str(path), path / lm.MANIFEST_NAME, path, None))
        elif (path / "manifest.json").is_file():
            archive = path / "layer.tar.zst"
            found.append(Target(str(path), path / "manifest.json", None, archive if archive.is_file() else None))
        elif path.is_dir():
            # `dist/layers` (every layer) or `dist/layers/<layer>` (every version of one).
            versions = sorted(path.glob("*/*/manifest.json")) or sorted(path.glob("*/manifest.json"))
            if not versions:
                raise FileNotFoundError(f"no layer, manifest or build output at {path}")
            for manifest in versions:
                archive = manifest.parent / "layer.tar.zst"
                found.append(Target(str(manifest.parent), manifest, None, archive if archive.is_file() else None))
        else:
            raise FileNotFoundError(f"nothing at {path}")
    return found


# --- per layer -------------------------------------------------------------------------------------------


def _files(manifest: Any) -> dict[str, Any]:
    return manifest.by_path()


def check_python(manifest: Any, root: Path | None, recorded: dict[str, Any]) -> list[str]:
    lm, build_layers = _modules()
    problems: list[str] = []
    sources = manifest.sources
    interpreter = sources.get("interpreter", {})
    pinned = recorded["interpreter"]
    for key in ("build", "version", "url", "size", "sha256"):
        if interpreter.get(key) != pinned.get(key):
            problems.append(f"interpreter {key}: {interpreter.get(key)!r}, pinned {pinned.get(key)!r}")
    locked = {build_layers.normalized(item["name"]): item for item in recorded["python"]["global"]}
    expected = {name for name, item in locked.items() if build_layers.marker_applies(item.get("marker"))}
    installed = {build_layers.normalized(item["name"]): item for item in sources.get("packages", [])}
    problems += [f"locked package not in the layer: {name}" for name in sorted(expected - installed.keys())]
    problems += [f"package not locked for Windows: {name}" for name in sorted(installed.keys() - expected)]
    for name in sorted(installed.keys() & expected):
        item, record = installed[name], locked[name]
        artefacts = {(wheel["url"], wheel["sha256"]) for wheel in record.get("wheels", [])}
        archive = record.get("archive")
        if archive:
            artefacts.add((archive["url"], archive["hashes"]["sha256"]))
        if item.get("version") != record.get("version"):
            problems.append(f"{name}: version {item.get('version')}, locked {record.get('version')}")
        if (item.get("url"), item.get("sha256")) not in artefacts:
            problems.append(f"{name}: installed from {item.get('wheel')} ({item.get('sha256')}), not a locked artefact")
    for exception in manifest.exceptions:
        if not any(pattern.fullmatch(exception["path"]) for pattern in build_layers.ABSOLUTE_PATH_ALLOWED):
            problems.append(f"exception not allowed (4.2 item 17): {exception['path']}")
    files = _files(manifest)
    problems += [f"exception not a file of the layer: {item['path']}" for item in manifest.exceptions
                 if item["path"] not in files]
    if root is None:
        return problems

    site = root / "Lib" / "site-packages"
    owned: set[str] = set()
    seen_packages: set[str] = set()
    for dist_info in sorted(site.glob("*.dist-info")):
        metadata = (dist_info / "METADATA").read_text(encoding="utf-8", errors="replace")
        name = build_layers.normalized(re.search(r"^Name: (.+)$", metadata, re.M).group(1).strip())
        version = re.search(r"^Version: (.+)$", metadata, re.M).group(1).strip()
        seen_packages.add(name)
        if name not in installed or installed[name].get("version") != version:
            problems.append(f"installed distribution not in the build record: {dist_info.name}")
        with open(dist_info / "RECORD", encoding="utf-8", newline="") as source:
            rows = [row for row in csv.reader(source) if row]
        for row in rows:
            entry, digest = row[0], row[1] if len(row) > 1 else ""
            size = row[2] if len(row) > 2 else ""
            target = lm.relative(root, Path(os.path.normpath(site / entry)))
            owned.add(target)
            item = files.get(target)
            if item is None:
                problems.append(f"{dist_info.name}: RECORD names {entry}, not in the layer")
            elif digest and (_record_hash(item.sha256) != digest or (size and int(size) != item.size)):
                problems.append(f"{dist_info.name}: {entry} differs from RECORD")
    problems += [f"build record names a package with no distribution: {name}"
                 for name in sorted(installed.keys() - seen_packages)]
    members = {item["path"]: item for item in interpreter.get("files", [])}
    exceptions = {item["path"] for item in manifest.exceptions}
    for path, item in sorted(files.items()):
        if path in owned:
            continue
        if path.endswith(".pyc"):
            source = build_layers.compiled_source(path)
            if source is not None and source in files:
                continue
            problems.append(f"bytecode with no source in the layer: {path}")
            continue
        member = members.get(path)
        if member is not None:
            if (member["size"], member["sha256"]) != (item.size, item.sha256):
                problems.append(f"interpreter file differs from the archive: {path}")
            continue
        if path not in exceptions:
            problems.append(f"file of no package and not the interpreter's: {path}")
    for path, member in sorted(members.items()):
        if path not in files and path not in owned and not member.get("package"):
            problems.append(f"interpreter file missing: {path}")
    return problems


def check_models(manifest: Any, root: Path | None, recorded: dict[str, Any]) -> list[str]:
    lm, build_layers = _modules()
    problems: list[str] = []
    files = _files(manifest)
    sources = {item["id"]: item for item in manifest.sources.get("models", [])}
    if list(sources) != list(lm.MODEL_LAYER_IDS):
        problems.append(f"models {sorted(sources)}, expected {list(lm.MODEL_LAYER_IDS)}")
    accounted: set[str] = set()
    stamped: list[str] = []
    expected_content: dict[str, bytes] = {}
    for model_id in lm.MODEL_LAYER_IDS:
        source, pinned = sources.get(model_id), recorded["models"].get(model_id)
        if source is None or pinned is None:
            continue
        for key in ("repo", "revision", "listedOnly"):
            if source.get(key) != pinned.get(key):
                problems.append(f"{model_id}: {key} {source.get(key)!r}, pinned {pinned.get(key)!r}")
        if pinned.get("repo"):
            cache_dir = "models--" + pinned["repo"].replace("/", "--")
            base = f"{lm.HF_HOME_DIR}/hub/{cache_dir}/snapshots/{pinned['revision']}/"
            marker = f"{lm.HF_HOME_DIR}/hub/{cache_dir}/{lm.HF_MARKER_NAME}"
            accounted.add(marker)
            expected_content[marker] = json.dumps(
                {"manifest": build_layers.engine_entry_digest(pinned), "repo": pinned["repo"],
                 "revision": pinned["revision"]}, ensure_ascii=False).encode("utf-8")
            if marker not in files:
                problems.append(f"{model_id}: no {lm.HF_MARKER_NAME}")
        else:
            base = f"{lm.SEPARATOR_DIR}/"
        listed = {item["name"] for item in source.get("files", [])}
        for item in pinned["files"]:
            path = base + item["name"]
            got = files.get(path)
            if got is None:
                problems.append(f"{model_id}: pinned file missing: {item['name']}")
            elif (got.size, got.sha256) != (item.get("size"), item.get("sha256")):
                problems.append(f"{model_id}: {item['name']} is not the pinned file")
            if not pinned.get("repo") and item["name"] not in lm.ENGINE_UNVERIFIED:
                stamp = path + lm.VERIFIED_SUFFIX
                accounted.add(stamp)
                stamped.append(path)
                expected_content[stamp] = json.dumps(
                    {"sha256": item["sha256"], "size": item["size"], "mtime_ns": lm.FIXED_MTIME * 10**9}).encode("utf-8")
                if stamp not in files:
                    problems.append(f"{model_id}: no stamp for {item['name']}")
        pinned_names = {item["name"] for item in pinned["files"]}
        if pinned.get("listedOnly") and listed != pinned_names:
            problems.append(f"{model_id}: listedOnly, but holds {sorted(listed - pinned_names)}")
        for item in source.get("files", []):
            path = base + item["name"]
            accounted.add(path)
            got = files.get(path)
            if got is None:
                problems.append(f"{model_id}: {item['name']} missing")
            elif (got.size, got.sha256) != (item["size"], item["sha256"]):
                problems.append(f"{model_id}: {item['name']} differs from what the build checked")
    for path in sorted(files):
        if "/accel/" in f"/{path}/" or "/blobs/" in f"/{path}":
            problems.append(f"not allowed in the model layer: {path}")
        elif path not in accounted:
            problems.append(f"file of no model: {path}")
    problems += [f"exception in the model layer: {item['path']}" for item in manifest.exceptions]
    if root is not None:
        for path, content in sorted(expected_content.items()):
            if (root / path).is_file() and (root / path).read_bytes() != content:
                problems.append(f"marker says something else than the engine expects: {path}")
        for path in stamped:
            if (root / path).is_file() and os.stat(root / path).st_mtime_ns != lm.FIXED_MTIME * 10**9:
                problems.append(f"modification time lost (the engine's stamp no longer holds): {path}")
    return problems


def check_tools(manifest: Any, root: Path | None, recorded: dict[str, Any]) -> list[str]:
    lm, _ = _modules()
    problems: list[str] = []
    files = _files(manifest)
    pinned = {item["id"]: item for item in recorded["tools"]["resources"] if item["id"] not in lm.TOOLS_LEFT_OUT}
    sources = {item["id"]: item for item in manifest.sources.get("tools", [])}
    problems += [f"tool missing: {tool}" for tool in sorted(pinned.keys() - sources.keys())]
    problems += [f"tool not pinned: {tool}" for tool in sorted(sources.keys() - pinned.keys())]
    directories: dict[str, str] = {}
    for tool in sorted(pinned.keys() & sources.keys()):
        source, record = sources[tool], pinned[tool]
        for key in ("version", "url", "size", "sha256", "required_files"):
            if source.get(key) != record.get(key):
                problems.append(f"{tool}: {key} {source.get(key)!r}, pinned {record.get(key)!r}")
        directory = source.get("directory", "")
        directories[directory] = tool
        for name in record.get("required_files", []):
            if f"{directory}/{name}" not in files:
                problems.append(f"{tool}: required file missing: {name}")
        count = sum(1 for path in files if path.startswith(f"{directory}/"))
        if count != source.get("files"):
            problems.append(f"{tool}: {count} files, the archive had {source.get('files')}")
    for path in sorted(files):
        if path.split("/", 1)[0] not in directories:
            problems.append(f"file of no tool: {path}")
    problems += [f"exception in the tool layer: {item['path']}" for item in manifest.exceptions]
    return problems


CHECKERS = {"python": check_python, "models": check_models, "tools": check_tools}


def unpack(archive: Path, scratch: Path) -> Path:
    destination = scratch / archive.parent.parent.name / archive.parent.name
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True)
    completed = subprocess.run([str(SYSTEM_TAR), "-x", "-f", str(archive), "-C", str(destination)],
                               capture_output=True, text=True)
    if completed.returncode != 0:
        raise RuntimeError(f"tar could not unpack {archive}: {completed.stderr.strip()[-400:]}")
    return destination


def check_target(target: Target, scratch: Path, recorded: dict[str, Any]) -> dict[str, Any]:
    lm, _ = _modules()
    manifest, data = lm.read(target.manifest_path)
    version = lm.version_of(data)
    problems: list[str] = []
    named = Path(target.manifest_path).parent.name if target.root is None else Path(target.root).name
    if re.fullmatch(r"[0-9a-f]{16}", named) and named != version:
        problems.append(f"directory {named} is not the manifest's version {version}")
    if manifest.platform != lm.PLATFORM:
        problems.append(f"platform {manifest.platform!r}")
    root, unpacked = target.root, None
    if root is None and target.archive is not None:
        root = unpacked = unpack(target.archive, scratch)
    try:
        if root is not None:
            carried, carried_data = lm.manifest_in(root)
            if carried_data != data:
                problems.append("the layer carries a different manifest")
            problems += lm.compare(manifest, root, full=True)
        problems += CHECKERS[manifest.layer](manifest, root, recorded)
    finally:
        if unpacked is not None:
            shutil.rmtree(unpacked, ignore_errors=True)
    return {
        "label": target.label,
        "layer": manifest.layer,
        "version": version,
        "files": len(manifest.files),
        "size": manifest.total_size(),
        "checked_files": root is not None,
        "problems": problems,
    }


def check(paths: list[Path], scratch: Path | None = None) -> list[dict[str, Any]]:
    _, build_layers = _modules()
    # In the main checkout even from a worktree: an unpacked Python layer is 5 GiB.
    scratch = Path(scratch) if scratch else build_layers.main_checkout() / ".tmp" / "layer-gate"
    recorded = pins()
    results = []
    for target in targets(paths):
        results.append(check_target(target, scratch, recorded))
    return results


__all__ = ["check", "check_models", "check_python", "check_tools", "targets"]


if __name__ == "__main__":  # pragma: no cover - manual use
    import sys

    out = io.StringIO()
    for result in check([Path(arg) for arg in sys.argv[1:]]):
        print(json.dumps(result, indent=1), file=out)
    print(out.getvalue())
