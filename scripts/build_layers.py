#!/usr/bin/env python3
"""Build the prebuilt environment layers (docs/transcription-engines-plan.md 4.2 items 14 and 17, WP-E1).

    python scripts/build_layers.py build [--layer python|models|tools ...] [--clean]
    python scripts/build_layers.py compare <first> <second>
    python scripts/build_layers.py stage <dist/layers> --into <dir>   # unpack, and lay out an install root for --real

Windows only: the Python environment layer, the model layer and the tool layer. The only inputs are
Nonoka's manifests (`src/nonoka_x/resources/`, read through `nonoka_x.runtime.manifests`) and
`RUNTIME_PINS.json`; every download is checked against them, and nothing is built when the two
disagree. Nothing is read from a user-level cache: uv runs with `--no-config` and its cache, the
downloads, the staging trees and the temporary directory of every child all live in the build
directory, by default `<main checkout>/.tmp/layer-build/`. The results go to
`<main checkout>/dist/layers/<layer>/<version>/`: `layer.tar.zst` (zstd -3, unpacks with Windows'
own `tar.exe`), `manifest.json` (the same bytes as the layer's own `.nonoka-layer.json`) and
`summary.json` (sizes, digests and timings; not part of the version).

The Python environment layer: python-build-standalone as pinned, every package of the lock installed
straight into its `Lib\\site-packages` (no venv). The wheels are downloaded first and each one checked
against the lock's sha256 here; uv then installs from those local files (`uv --no-config pip install
--link-mode copy --offline` on a copy of the lock that names them, hashes unchanged), so uv checks them
again (owner's choice 2026-11-12). pip and `Lib\\site-packages\\README.txt` go, every `__pycache__` is
dropped and all of it is compiled again with `compileall --invalidation-mode unchecked-hash`, the source paths stripped
so the bytecode does not name the build machine. Every installed file is checked against its
package's `RECORD`, and every file is accounted for: the interpreter's archive, a `RECORD`, compiled
bytecode, or a listed exception (`Scripts\\*.exe` and `Scripts\\numba`, which name the interpreter's
build path; 4.2 item 17).

The model layer: the models `layer_manifest.MODEL_LAYER_IDS` names, fetched from their official
sources at the pinned revision (Hugging Face's own tree API and `resolve` URLs; no `huggingface_hub`,
no engine code), every pinned file checked against the manifest and every other one against the
digest the Hub states for it. Laid out the way the engine reads a models root -- `audio-separator\\`
and `huggingface\\hub\\models--*\\snapshots\\<revision>\\` -- as real files: no links, no `blobs\\`, no
`accel\\`. It carries the markers the engine reads: `.finesub-verified.json` per repository and a
`.verified` stamp beside each separator file the engine verifies.

The tool layer: every tool of the tool manifest but uv, unpacked flat as `<directory>\\<archive
contents>` (owner's choice 2026-11-12).

Reproducible: every file's time is `FIXED_MTIME`, the archive lists files in sorted order. The same
inputs built in the same place give the same manifests; `compare` checks two builds. The Python layer
is always staged at the same path under the build directory, because the entry-point launchers write
that path into themselves; a second build empties the build directory first (`--clean`).
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterable
from urllib.parse import quote, unquote, urlparse

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from nonoka_x.runtime import fetch, manifests  # noqa: E402
from nonoka_x.runtime import layer_manifest as lm  # noqa: E402

PINS_FILE = ROOT / "src" / "nonoka_x" / "resources" / "RUNTIME_PINS.json"
SYSTEM_TAR = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "tar.exe"
HF = "https://huggingface.co"
ZSTD_LEVEL = 3

#: Files a build-machine path may appear in (4.2 item 17): uv's entry-point launchers and numba's
#: script, whose shebang names the interpreter. Both are in their packages' RECORD, so they stay.
ABSOLUTE_PATH_ALLOWED = (re.compile(r"Scripts/[^/]+\.exe"), re.compile(r"Scripts/numba"))

#: What the interpreter archive brings that the layer drops (4.2 item 14).
INTERPRETER_DROPPED = ("Lib/site-packages/README.txt",)


class BuildError(RuntimeError):
    pass


def log(message: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


def main_checkout() -> Path:
    """The main checkout of this repository (a worktree's build output must not live in the worktree)."""

    try:
        common = subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
            cwd=ROOT, capture_output=True, text=True, check=True,
        ).stdout.strip()
        return Path(common).parent
    except (OSError, subprocess.CalledProcessError):
        return ROOT


# --- inputs ----------------------------------------------------------------------------------------


def _subset(left: dict[str, Any], right: dict[str, Any], keys: Iterable[str]) -> bool:
    return all(left.get(key) == right.get(key) for key in keys)


def check_inputs(pins: dict[str, Any]) -> list[str]:
    """Every input the build uses, held to `RUNTIME_PINS.json`; empty when they agree."""

    problems: list[str] = []
    interpreter = manifests.interpreter()
    recorded = pins.get("interpreter", {})
    asset = interpreter["asset"]
    if not (_subset(interpreter, recorded, ("implementation", "version", "build"))
            and (asset["url"], asset.get("size"), asset.get("sha256"))
            == (recorded.get("url"), recorded.get("size"), recorded.get("sha256"))):
        problems.append("interpreter differs from RUNTIME_PINS.json")
    pinned_packages = {(item["name"], item.get("marker", "")): item for item in pins["python"]["global"]}
    lock = manifests.runtime_lock()
    if lock.get("requires-python") != pins["python"].get("requires"):
        problems.append("requires-python differs from RUNTIME_PINS.json")
    seen = set()
    for package in lock["packages"]:
        key = (package["name"], package.get("marker", ""))
        seen.add(key)
        record = pinned_packages.get(key)
        if record is None or record.get("version") != package.get("version"):
            problems.append(f"package not pinned as locked: {package['name']} {package.get('version')}")
            continue
        wheels = {(item["url"], item["sha256"]) for item in record.get("wheels", [])}
        for wheel in package.get("wheels", []):
            if (wheel["url"], wheel["hashes"]["sha256"]) not in wheels:
                problems.append(f"wheel not pinned: {wheel['url']}")
        if package.get("archive") != record.get("archive"):
            problems.append(f"archive not pinned: {package['name']}")
    problems += [f"pinned package missing from the lock: {name}" for name, _ in sorted(pinned_packages.keys() - seen)]
    tools = {item["id"]: item for item in pins["tools"]["resources"]}
    for resource in manifests.tool_manifest()["resources"]:
        record = tools.get(resource["id"], {})
        if (resource["version"], resource["asset"]["url"], resource["asset"].get("size"), resource["asset"].get("sha256"),
                resource.get("required_files", [])) != (record.get("version"), record.get("url"), record.get("size"),
                                                        record.get("sha256"), record.get("required_files", [])):
            problems.append(f"tool differs from RUNTIME_PINS.json: {resource['id']}")
    models = manifests.model_manifest()["models"]
    for model_id in lm.MODEL_LAYER_IDS:
        spec, record = models.get(model_id), pins["models"].get(model_id)
        if spec is None or record is None:
            problems.append(f"model not in the manifest and the pins: {model_id}")
            continue
        files = [{key: item[key] for key in ("name", "url", "size", "sha256") if key in item} for item in spec["files"]]
        if not _subset(spec, record, ("repo", "revision", "listedOnly")) or files != record["files"]:
            problems.append(f"model differs from RUNTIME_PINS.json: {model_id}")
        if any(not item.get("sha256") or item.get("size") is None for item in spec["files"]):
            problems.append(f"model file without size or sha256: {model_id}")
    return problems


# --- downloads -------------------------------------------------------------------------------------


def _client() -> Any:
    import httpx

    route = fetch.network_routes()[0]
    return fetch.create_client(route, timeout=httpx.Timeout(connect=fetch.CONNECT_TIMEOUT_SEC, read=120.0,
                                                               write=30.0, pool=20.0))


def remote_size(url: str) -> int:
    with _client() as client:
        response = client.head(url)
        response.raise_for_status()
        length = response.headers.get("content-length")
    if length is None:
        raise BuildError(f"no size for {url}")
    return int(length)


def download(url: str, destination: Path, sha256: str, size: int | None = None) -> Path:
    """`url` into `destination`, resumable, size and sha256 checked (`nonoka_x.runtime.fetch`)."""

    if size is None:
        size = remote_size(url)
    return fetch.download_file(fetch.DownloadAsset(url=url, size=int(size), sha256=sha256), destination)


def git_blob_sha1(path: Path) -> str:
    digest = hashlib.sha1()
    digest.update(f"blob {path.stat().st_size}\0".encode())
    with open(path, "rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download_git_blob(url: str, destination: Path, size: int, oid: str) -> Path:
    """A small file the Hub keeps in git rather than LFS: checked against its git blob id."""

    if destination.is_file() and destination.stat().st_size == size and git_blob_sha1(destination) == oid:
        return destination
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_name(destination.name + ".part")
    with _client() as client, client.stream("GET", url) as response:
        response.raise_for_status()
        with open(partial, "wb") as target:
            for chunk in response.iter_bytes(1024 * 1024):
                target.write(chunk)
    if partial.stat().st_size != size or git_blob_sha1(partial) != oid:
        raise BuildError(f"{url}: not the blob the Hub lists ({oid})")
    os.replace(partial, destination)
    return destination


def hf_tree(repo: str, revision: str) -> list[dict[str, Any]]:
    """Every file of `repo` at `revision`, as the Hub's tree API states it (paged)."""

    url = f"{HF}/api/models/{repo}/tree/{revision}?recursive=true"
    files: list[dict[str, Any]] = []
    with _client() as client:
        while url:
            response = client.get(url)
            response.raise_for_status()
            files += [item for item in response.json() if item.get("type") == "file"]
            url = response.links.get("next", {}).get("url")
    return sorted(files, key=lambda item: item["path"])


# --- children --------------------------------------------------------------------------------------


def child_environment(build: "Build", extra: dict[str, str] | None = None) -> dict[str, str]:
    """A child's environment from scratch: the system essentials, a home and TEMP inside the build."""

    system = os.environ.get("SystemRoot", r"C:\Windows")
    home = build.home
    environment = {
        "SystemRoot": system,
        "SYSTEMDRIVE": os.environ.get("SYSTEMDRIVE", "C:"),
        "WINDIR": system,
        "COMSPEC": os.environ.get("COMSPEC", rf"{system}\System32\cmd.exe"),
        "PATHEXT": ".COM;.EXE;.BAT;.CMD",
        "PATH": os.pathsep.join([rf"{system}\System32", system, rf"{system}\System32\Wbem"]),
        "NUMBER_OF_PROCESSORS": os.environ.get("NUMBER_OF_PROCESSORS", "1"),
        "PROCESSOR_ARCHITECTURE": os.environ.get("PROCESSOR_ARCHITECTURE", "AMD64"),
        "USERPROFILE": str(home),
        "HOME": str(home),
        "APPDATA": str(home / "AppData" / "Roaming"),
        "LOCALAPPDATA": str(home / "AppData" / "Local"),
        "TEMP": str(build.tmp),
        "TMP": str(build.tmp),
        "NO_PROXY": "*",
        "PYTHONNOUSERSITE": "1",
        "PYTHONUTF8": "1",
    }
    for directory in (Path(environment["APPDATA"]), Path(environment["LOCALAPPDATA"]), build.tmp):
        directory.mkdir(parents=True, exist_ok=True)
    environment.update(extra or {})
    return environment


def run(build: "Build", command: list[str], *, log_name: str, extra: dict[str, str] | None = None,
        cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess:
    log("  $ " + " ".join(command))
    log_path = build.logs / log_name
    with open(log_path, "wb") as output:
        completed = subprocess.run(command, stdout=output, stderr=subprocess.STDOUT, cwd=cwd or build.tmp,
                                   env=child_environment(build, extra), stdin=subprocess.DEVNULL)
    if check and completed.returncode != 0:
        tail = log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-30:]
        raise BuildError(f"{command[0]} exited with {completed.returncode} (log {log_path}):\n" + "\n".join(tail))
    return completed


# --- file helpers ----------------------------------------------------------------------------------


def remove_tree(path: Path) -> None:
    def retry(function, target, _info):
        os.chmod(target, 0o700)
        function(target)

    if lm.is_link(path):
        os.unlink(path) if not path.is_dir() else os.rmdir(path)
    elif path.exists():
        shutil.rmtree(path, onexc=retry)


def set_fixed_times(root: Path) -> None:
    """Every file and directory under `root` (and `root`) to `FIXED_MTIME`, deepest first."""

    stamp = (lm.FIXED_MTIME, lm.FIXED_MTIME)
    for directory, _subdirectories, files in os.walk(root, topdown=False):
        for name in files:
            os.utime(Path(directory) / name, stamp)
        os.utime(directory, stamp)


def copy_file(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)


def record_hash(sha256_hex: str) -> str:
    """A wheel RECORD's spelling of a sha256: `sha256=` + urlsafe base64 without padding."""

    return "sha256=" + base64.urlsafe_b64encode(bytes.fromhex(sha256_hex)).decode().rstrip("=")


def unzip(archive: Path, destination: Path) -> list[str]:
    """Unpack a zip (no links, nothing outside `destination`); its member file paths."""

    names: list[str] = []
    with zipfile.ZipFile(archive) as bundle:
        for member in bundle.infolist():
            name = lm.check_path(member.filename.rstrip("/")) if member.filename.rstrip("/") else ""
            if not name:
                continue
            if (member.external_attr >> 16) & 0o170000 == 0o120000:
                raise BuildError(f"{archive.name}: link member {member.filename}")
            target = destination / name
            if member.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with bundle.open(member) as source, open(target, "wb") as output:
                shutil.copyfileobj(source, output, 1024 * 1024)
            names.append(name)
    return names


# --- wheels ----------------------------------------------------------------------------------------


def _expand_tags(filename: str) -> set[str]:
    stem = filename[: -len(".whl")]
    parts = stem.split("-")
    python, abi, platform = parts[-3:]
    return {f"{p}-{a}-{t}" for p in python.split(".") for a in abi.split(".") for t in platform.split(".")}


def wheel_filename(url: str) -> str:
    return unquote(PurePosixPath(urlparse(url).path).name)


def normalized(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _toml_string(value: str) -> str:
    return json.dumps(value)


def local_lock(lock: dict[str, Any], local: dict[str, Path]) -> str:
    """The runtime lock with every artefact replaced by its checked local copy (hashes unchanged).

    sdists are left out: the lock only ever installs wheels on Windows, and leaving them in would let
    uv build from source if a wheel did not fit.
    """

    lines = [
        f"lock-version = {_toml_string(lock['lock-version'])}",
        f"created-by = {_toml_string('nonoka-build-layers')}",
        f"requires-python = {_toml_string(lock['requires-python'])}",
        "",
    ]
    for package in lock["packages"]:
        lines += ["[[packages]]", f"name = {_toml_string(package['name'])}",
                  f"version = {_toml_string(package['version'])}"]
        if package.get("marker"):
            lines.append(f"marker = {_toml_string(package['marker'])}")
        if package.get("wheels"):
            entries = []
            for wheel in package["wheels"]:
                path = local[wheel["url"]].as_posix()
                entries.append(f"{{ path = {_toml_string(path)}, hashes = {{ sha256 = "
                               f"{_toml_string(wheel['hashes']['sha256'])} }} }}")
            lines.append("wheels = [" + ", ".join(entries) + "]")
        if package.get("archive"):
            archive = package["archive"]
            lines.append(f"archive = {{ path = {_toml_string(local[archive['url']].as_posix())}, hashes = {{ sha256 = "
                         f"{_toml_string(archive['hashes']['sha256'])} }} }}")
        lines.append("")
    return "\n".join(lines)


def lock_artefacts(lock: dict[str, Any]) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """(package, artefact) for every wheel and archive of the lock."""

    found = []
    for package in lock["packages"]:
        for wheel in package.get("wheels", []):
            found.append((package, wheel))
        if package.get("archive"):
            found.append((package, package["archive"]))
    return found


def read_record(site: Path, dist_info: Path) -> list[tuple[str, str, str]]:
    rows = []
    import csv

    with open(dist_info / "RECORD", encoding="utf-8", newline="") as source:
        for row in csv.reader(source):
            if row:
                rows.append((row[0], row[1] if len(row) > 1 else "", row[2] if len(row) > 2 else ""))
    return rows


def record_target(site: Path, root: Path, entry: str) -> str:
    """A RECORD path (relative to site-packages, maybe `../../Scripts/x.exe`) as a layer path."""

    return lm.relative(root, Path(os.path.normpath(site / entry)))


# --- the build ---------------------------------------------------------------------------------------


@dataclass
class Build:
    directory: Path
    out: Path
    check_archive: bool = True

    @property
    def cache(self) -> Path:
        return self.directory / "cache"

    @property
    def work(self) -> Path:
        return self.directory / "work"

    @property
    def tmp(self) -> Path:
        return self.directory / "tmp"

    @property
    def home(self) -> Path:
        return self.directory / "home"

    @property
    def logs(self) -> Path:
        return self.directory / "logs"

    def prepare(self) -> None:
        for directory in (self.cache, self.work, self.tmp, self.home, self.logs, self.out):
            directory.mkdir(parents=True, exist_ok=True)

    # -- python ---------------------------------------------------------------------------------------

    def uv(self) -> Path:
        resource = next(item for item in manifests.tool_manifest()["resources"] if item["id"] == "uv")
        asset = resource["asset"]
        archive = download(asset["url"], self.cache / "tools" / wheel_filename(asset["url"]), asset["sha256"], asset["size"])
        directory = self.cache / "uv" / resource["version"]
        executable = directory / "uv.exe"
        if not executable.is_file():
            remove_tree(directory)
            unzip(archive, directory)
        return executable

    def build_python(self) -> tuple[Path, dict[str, Any], list[dict[str, str]]]:
        root = self.work / "python"
        timings: dict[str, float] = {}
        started = time.perf_counter()
        interpreter = manifests.interpreter()
        asset = interpreter["asset"]
        archive = download(asset["url"], self.cache / "pbs" / wheel_filename(asset["url"]), asset["sha256"], asset["size"])
        remove_tree(root)
        members: list[dict[str, Any]] = []
        with tarfile.open(archive, "r:gz") as bundle:
            for member in bundle.getmembers():
                name = member.name
                if not (name == "python" or name.startswith("python/")):
                    raise BuildError(f"interpreter archive member outside python/: {name}")
                if member.issym() or member.islnk():
                    raise BuildError(f"interpreter archive link: {name}")
            bundle.extractall(self.work, filter="data")
        bundled = interpreter_packages(root)
        for path in sorted(root.rglob("*")):
            if path.is_file():
                name = lm.relative(root, path)
                # Bytecode is dropped and compiled again; README.txt goes (4.2 item 14).
                if name in INTERPRETER_DROPPED or name.endswith(".pyc"):
                    continue
                member = {"path": name, "size": path.stat().st_size, "sha256": lm.sha256_file(path)}
                if name in bundled:
                    # pip and setuptools as the archive ships them: the installer replaces or removes them.
                    member["package"] = bundled[name]
                members.append(member)
        for name in INTERPRETER_DROPPED:
            (root / name).unlink(missing_ok=True)
        timings["interpreter"] = time.perf_counter() - started

        started = time.perf_counter()
        lock = manifests.runtime_lock()
        local: dict[str, Path] = {}
        for package, artefact in lock_artefacts(lock):
            filename = wheel_filename(artefact["url"])
            local[artefact["url"]] = download(artefact["url"], self.cache / "wheels" / filename,
                                              artefact["hashes"]["sha256"], artefact.get("size"))
            # uv records a local file's modification time in the dist-info it installs
            # (`uv_cache.json`, listed in RECORD); a download time there would differ per build.
            os.utime(local[artefact["url"]], (lm.FIXED_MTIME, lm.FIXED_MTIME))
        timings["wheels_download"] = time.perf_counter() - started

        started = time.perf_counter()
        uv = self.uv()
        lock_path = self.tmp / "pylock.local.toml"
        lock_path.write_text(local_lock(lock, local), encoding="utf-8")
        python = root / "python.exe"
        uv_environment = {"UV_CACHE_DIR": str(self.cache / "uv-cache"), "UV_PYTHON_DOWNLOADS": "never",
                          "UV_NO_PROGRESS": "1"}
        install = [str(uv), "--no-config", "-v", "pip", "install", "--offline", "--link-mode", "copy",
                   "--python", str(python), "--requirement", str(lock_path)]
        if run(self, install, log_name="uv-install.log", extra=uv_environment, check=False).returncode != 0:
            # Seen once on this machine: "Failed to update Windows PE resources ... access denied" on an
            # entry-point launcher uv had just written (a scanner holding the new .exe). The install is
            # idempotent and everything is checked against the lock and RECORD afterwards.
            log("  uv install failed (log uv-install.log); trying once more")
            os.replace(self.logs / "uv-install.log", self.logs / "uv-install.failed.log")
            run(self, install, log_name="uv-install.log", extra=uv_environment)
        run(self, [str(uv), "--no-config", "pip", "uninstall", "--python", str(python), "pip"],
            log_name="uv-uninstall-pip.log", extra=uv_environment)
        timings["install"] = time.perf_counter() - started

        started = time.perf_counter()
        for cache in sorted(root.rglob("__pycache__"), key=lambda path: len(path.parts), reverse=True):
            if cache.is_dir():
                remove_tree(cache)
        for stray in root.rglob("*.pyc"):
            stray.unlink()
        run(self, [str(python), "-I", "-B", "-m", "compileall", "-f", "-q", "-j", "1",
                   "--invalidation-mode", "unchecked-hash", "-s", str(root), str(root)],
            log_name="compileall.log", check=False)
        timings["compile"] = time.perf_counter() - started

        started = time.perf_counter()
        packages = self.check_python(root, lock, members)
        exceptions = self.absolute_path_files(root)
        timings["check"] = time.perf_counter() - started
        sources = {
            "interpreter": {
                "build": interpreter["build"], "version": interpreter["version"], "url": asset["url"],
                "size": asset["size"], "sha256": asset["sha256"], "dropped": list(INTERPRETER_DROPPED),
                "files": members,
            },
            "packages": packages,
            "installer": {"tool": "uv", "version": next(item["version"] for item in manifests.tool_manifest()["resources"]
                                                         if item["id"] == "uv")},
            "bytecode": {"invalidation_mode": "unchecked-hash", "stripped": True},
        }
        return root, {"sources": sources, "timings": timings}, exceptions

    def check_python(self, root: Path, lock: dict[str, Any], members: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Each installed package against the lock and its RECORD; every file accounted for."""

        site = root / "Lib" / "site-packages"
        by_name = {normalized(package["name"]): package for package in lock["packages"]}
        installed: dict[str, dict[str, Any]] = {}
        owned: set[str] = set()
        problems: list[str] = []
        for dist_info in sorted(site.glob("*.dist-info")):
            metadata = (dist_info / "METADATA").read_text(encoding="utf-8", errors="replace")
            name = re.search(r"^Name: (.+)$", metadata, re.M).group(1).strip()
            version = re.search(r"^Version: (.+)$", metadata, re.M).group(1).strip()
            package = by_name.get(normalized(name))
            if package is None or package["version"] != version:
                problems.append(f"installed but not locked: {name} {version}")
                continue
            tags = {line.split(":", 1)[1].strip() for line in (dist_info / "WHEEL").read_text(encoding="utf-8").splitlines()
                    if line.startswith("Tag:")}
            artefacts = package.get("wheels", []) + ([package["archive"]] if package.get("archive") else [])
            chosen = [item for item in artefacts if _expand_tags(wheel_filename(item["url"])) == tags]
            if len(chosen) != 1:
                problems.append(f"{name}: cannot tell which locked wheel was installed (tags {sorted(tags)})")
                continue
            wheel = chosen[0]
            for entry, digest, size in read_record(site, dist_info):
                target = record_target(site, root, entry)
                owned.add(target)
                path = root / target
                if not digest:
                    if not path.is_file():
                        problems.append(f"{name}: RECORD names a missing file {entry}")
                    continue
                if not path.is_file():
                    problems.append(f"{name}: RECORD names a missing file {entry}")
                elif record_hash(lm.sha256_file(path)) != digest or (size and int(size) != path.stat().st_size):
                    problems.append(f"{name}: {entry} differs from RECORD")
            installed[normalized(name)] = {
                "name": package["name"], "version": version, "wheel": wheel_filename(wheel["url"]),
                "url": wheel["url"], "sha256": wheel["hashes"]["sha256"],
            }
        expected = {normalized(package["name"]) for package in lock["packages"] if marker_applies(package.get("marker"))}
        problems += [f"locked but not installed: {name}" for name in sorted(expected - installed.keys())]
        interpreter_files = {item["path"] for item in members}
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            name = lm.relative(root, path)
            if name in owned:
                continue
            if name.endswith(".pyc") and "/__pycache__/" in f"/{name}":
                source = compiled_source(name)
                if source is not None and (root / source).is_file():
                    continue
            if name in interpreter_files:
                continue
            problems.append(f"unaccounted for: {name}")
        missing_interpreter = [item["path"] for item in members if item["path"] not in owned
                               and not item.get("package") and not (root / item["path"]).is_file()]
        # Files the installer replaced (setuptools) or removed (pip) are owned by a RECORD now, or gone
        # on purpose; anything else of the interpreter must still be there unchanged.
        for item in members:
            path = root / item["path"]
            if item["path"] in owned or not path.is_file():
                continue
            if lm.sha256_file(path) != item["sha256"]:
                problems.append(f"interpreter file changed: {item['path']}")
        if problems:
            raise BuildError("the Python environment does not match the lock:\n  " + "\n  ".join(problems[:60]))
        if missing_interpreter:
            raise BuildError("interpreter files missing: " + ", ".join(missing_interpreter[:20]))
        return [installed[key] for key in sorted(installed)]

    def absolute_path_files(self, root: Path) -> list[dict[str, str]]:
        """Files holding the staging path; only `ABSOLUTE_PATH_ALLOWED` may (4.2 item 17)."""

        needles = []
        for text in {str(root), str(root).replace("\\", "/")}:
            for variant in {text, text.lower(), text.upper(), text[0].lower() + text[1:], text[0].upper() + text[1:]}:
                needles += [variant.encode("utf-8"), variant.encode("utf-16-le")]
        needles = sorted(set(needles), key=len, reverse=True)
        longest = max(len(needle) for needle in needles)
        found: list[dict[str, str]] = []
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            hit = False
            with open(path, "rb") as source:
                tail = b""
                for chunk in iter(lambda: source.read(8 * 1024 * 1024), b""):
                    window = tail + chunk
                    if any(needle in window for needle in needles):
                        hit = True
                        break
                    tail = window[-longest:]
            if hit:
                name = lm.relative(root, path)
                if not any(pattern.fullmatch(name) for pattern in ABSOLUTE_PATH_ALLOWED):
                    raise BuildError(f"a build-machine path in {name}, which may not hold one")
                found.append({"path": name, "reason": "names the interpreter's build path (entry-point launcher or shebang)"})
        return found

    # -- models ---------------------------------------------------------------------------------------

    def build_models(self) -> tuple[Path, dict[str, Any], list[dict[str, str]]]:
        root = self.work / "models"
        remove_tree(root)
        timings: dict[str, float] = {}
        started = time.perf_counter()
        models = manifests.model_manifest()["models"]
        sources: list[dict[str, Any]] = []
        for model_id in lm.MODEL_LAYER_IDS:
            spec = models[model_id]
            if spec.get("repo"):
                sources.append(self.hf_model(model_id, spec, root))
            else:
                sources.append(self.fixed_model(model_id, spec, root))
        timings["download"] = time.perf_counter() - started
        return root, {"sources": {"models": sources}, "timings": timings}, []

    def fixed_model(self, model_id: str, spec: dict[str, Any], root: Path) -> dict[str, Any]:
        directory = root / lm.SEPARATOR_DIR
        files = []
        for item in spec["files"]:
            cached = download(item["url"], self.cache / "models" / model_id / item["name"], item["sha256"], item["size"])
            copy_file(cached, directory / item["name"])
            files.append({"name": item["name"], "size": item["size"], "sha256": item["sha256"], "checked": "manifest"})
            if item["name"] not in lm.ENGINE_UNVERIFIED:
                stamp = {"sha256": item["sha256"], "size": item["size"], "mtime_ns": lm.FIXED_MTIME * 10**9}
                (directory / f"{item['name']}{lm.VERIFIED_SUFFIX}").write_text(json.dumps(stamp), encoding="utf-8")
        log(f"  {model_id}: {len(files)} files")
        return {"id": model_id, "directory": lm.SEPARATOR_DIR, "files": files}

    def hf_model(self, model_id: str, spec: dict[str, Any], root: Path) -> dict[str, Any]:
        repo, revision = spec["repo"], spec["revision"]
        cache_dir = "models--" + repo.replace("/", "--")
        snapshot = root / lm.HF_HOME_DIR / "hub" / cache_dir / "snapshots" / revision
        tree = {item["path"]: item for item in hf_tree(repo, revision)}
        pinned = {item["name"]: item for item in spec["files"]}
        missing = sorted(pinned.keys() - tree.keys())
        if missing:
            raise BuildError(f"{model_id}: pinned files not in {repo}@{revision}: {missing}")
        wanted = sorted(pinned) if spec.get("listedOnly") else sorted(tree)
        files = []
        for name in wanted:
            item = tree[name]
            url = f"{HF}/{repo}/resolve/{revision}/{quote(name)}"
            cached = self.cache / "models" / cache_dir / revision / name
            lfs = item.get("lfs") or {}
            if name in pinned:
                expected = pinned[name]
                if int(item["size"]) != expected["size"] or (lfs and lfs.get("oid") != expected["sha256"]):
                    raise BuildError(f"{model_id}: the Hub's {name} is not the pinned one")
                download(url, cached, expected["sha256"], expected["size"])
                entry = {"name": name, "size": expected["size"], "sha256": expected["sha256"], "checked": "manifest"}
            elif lfs:
                download(url, cached, lfs["oid"], int(item["size"]))
                entry = {"name": name, "size": int(item["size"]), "sha256": lfs["oid"], "checked": "hub-lfs-sha256"}
            else:
                download_git_blob(url, cached, int(item["size"]), item["oid"])
                entry = {"name": name, "size": int(item["size"]), "sha256": lm.sha256_file(cached),
                         "checked": "hub-git-blob", "git_blob": item["oid"]}
            copy_file(cached, snapshot / name)
            files.append(entry)
        marker = {"manifest": engine_entry_digest(spec), "repo": repo, "revision": revision}
        (root / lm.HF_HOME_DIR / "hub" / cache_dir / lm.HF_MARKER_NAME).write_text(
            json.dumps(marker, ensure_ascii=False), encoding="utf-8")
        log(f"  {model_id}: {len(files)} files from {repo}@{revision[:12]}")
        source = {"id": model_id, "repo": repo, "revision": revision, "directory": f"{lm.HF_HOME_DIR}/hub/{cache_dir}",
                  "files": files}
        if spec.get("listedOnly"):
            source["listedOnly"] = True
        return source

    # -- tools ----------------------------------------------------------------------------------------

    def build_tools(self) -> tuple[Path, dict[str, Any], list[dict[str, str]]]:
        root = self.work / "tools"
        remove_tree(root)
        started = time.perf_counter()
        sources = []
        for resource in manifests.tool_manifest()["resources"]:
            if resource["id"] in lm.TOOLS_LEFT_OUT:
                continue
            asset = resource["asset"]
            if resource.get("archive_type") != "zip":
                raise BuildError(f"{resource['id']}: archive type {resource.get('archive_type')!r} not handled")
            archive = download(asset["url"], self.cache / "tools" / wheel_filename(asset["url"]), asset["sha256"], asset["size"])
            directory = root / lm.check_path(resource["directory"])
            if directory.exists():
                raise BuildError(f"two tools in {resource['directory']}")
            members = unzip(archive, directory)
            missing = [name for name in resource.get("required_files", []) if not (directory / name).is_file()]
            if missing:
                raise BuildError(f"{resource['id']}: required files missing: {missing}")
            sources.append({"id": resource["id"], "version": resource["version"], "directory": resource["directory"],
                            "url": asset["url"], "size": asset["size"], "sha256": asset["sha256"],
                            "required_files": resource.get("required_files", []), "files": len(members)})
            log(f"  {resource['id']} {resource['version']}: {len(members)} files")
        return root, {"sources": {"tools": sources}, "timings": {"download": time.perf_counter() - started}}, []

    # -- packing --------------------------------------------------------------------------------------

    def finish(self, layer: str, root: Path, built: dict[str, Any], exceptions: list[dict[str, str]]) -> dict[str, Any]:
        timings = built["timings"]
        started = time.perf_counter()
        (root / lm.MANIFEST_NAME).unlink(missing_ok=True)
        set_fixed_times(root)
        files, empty = lm.scan(root)
        manifest = lm.LayerManifest(layer=layer, files=files, empty_dirs=empty, exceptions=exceptions,
                                    sources=built["sources"])
        data = lm.render(manifest)
        version = lm.version_of(data)
        (root / lm.MANIFEST_NAME).write_bytes(data)
        os.utime(root / lm.MANIFEST_NAME, (lm.FIXED_MTIME, lm.FIXED_MTIME))
        os.utime(root, (lm.FIXED_MTIME, lm.FIXED_MTIME))
        timings["manifest"] = time.perf_counter() - started

        started = time.perf_counter()
        destination = self.out / layer / version
        remove_tree(destination)
        destination.mkdir(parents=True)
        archive = destination / "layer.tar.zst"
        listing = self.tmp / f"{layer}.files.txt"
        listing.write_text("\n".join(lm.iter_paths(manifest)) + "\n", encoding="utf-8")
        run(self, [str(SYSTEM_TAR), "--zstd", "--options", f"zstd:compression-level={ZSTD_LEVEL}", "--no-recursion",
                   "--no-acls", "--no-xattrs", "--no-fflags", "--numeric-owner", "--uid", "0", "--gid", "0",
                   "-c", "-f", str(archive), "-C", str(root), "-T", str(listing)],
            log_name=f"tar-{layer}.log")
        (destination / "manifest.json").write_bytes(data)
        timings["archive"] = time.perf_counter() - started

        if self.check_archive:
            started = time.perf_counter()
            check = self.directory / "verify" / layer
            remove_tree(check)
            check.mkdir(parents=True)
            run(self, [str(SYSTEM_TAR), "-x", "-f", str(archive), "-C", str(check)], log_name=f"untar-{layer}.log")
            unpacked, unpacked_data = lm.manifest_in(check)
            problems = lm.compare(unpacked, check, full=True)
            if unpacked_data != data:
                problems.append("the archive's manifest is not the one built")
            remove_tree(check)
            if problems:
                raise BuildError(f"{layer}: the archive does not unpack to the layer:\n  " + "\n  ".join(problems[:40]))
            timings["archive_check"] = time.perf_counter() - started

        summary = {
            "layer": layer,
            "version": version,
            "manifest_sha256": lm.digest_of(data),
            "archive": {"file": archive.name, "size": archive.stat().st_size, "sha256": lm.sha256_file(archive)},
            "raw_size": manifest.total_size(),
            "files": len(manifest.files),
            "empty_dirs": len(manifest.empty_dirs),
            "exceptions": len(manifest.exceptions),
            "seconds": {key: round(value, 1) for key, value in timings.items()},
        }
        (destination / "summary.json").write_text(json.dumps(summary, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        log(f"{layer}: version {version}, {summary['files']} files, {summary['raw_size'] / 2**30:.2f} GiB raw, "
            f"{summary['archive']['size'] / 2**30:.2f} GiB archive -> {destination}")
        return summary


def interpreter_packages(root: Path) -> dict[str, str]:
    """Layer path -> distribution, for the files the interpreter archive's own `*.dist-info` RECORDs list."""

    import csv

    site = root / "Lib" / "site-packages"
    owned: dict[str, str] = {}
    for dist_info in sorted(site.glob("*.dist-info")):
        with open(dist_info / "RECORD", encoding="utf-8", newline="") as source:
            for row in csv.reader(source):
                if row:
                    owned[record_target(site, root, row[0])] = dist_info.name
    return owned


def compiled_source(name: str) -> str | None:
    """`a/__pycache__/b.cpython-312.pyc` -> `a/b.py`."""

    parent, separator, file = f"/{name}".rpartition("/__pycache__/")
    match = re.fullmatch(r"([^/]+)\.cpython-312(?:\.opt-[12])?\.pyc", file)
    if not separator or not match:
        return None
    parent = parent[1:]
    return f"{parent}/{match.group(1)}.py" if parent else f"{match.group(1)}.py"


def engine_entry_digest(spec: dict[str, Any]) -> str:
    """`finesub_bootstrap.hf_verify.entry_digest` for a model-manifest entry (same JSON, same digest)."""

    payload = json.dumps(
        {
            "repo": spec.get("repo", ""),
            "revision": spec.get("revision", ""),
            "files": [{"name": item["name"], "size": item.get("size"), "sha256": item.get("sha256", "")}
                      for item in spec.get("files", [])],
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


#: The marker environment a Windows x64 CPython 3.12 install evaluates the lock's markers in.
MARKER_ENVIRONMENT = {
    "sys_platform": "win32",
    "platform_system": "Windows",
    "platform_machine": "AMD64",
    "os_name": "nt",
    "implementation_name": "cpython",
    "platform_python_implementation": "CPython",
    "python_version": "3.12",
    "python_full_version": "3.12.1",
}


def marker_applies(marker: str | None) -> bool:
    """Whether a lock marker holds on Windows x64 CPython 3.12 (the operators the lock uses: ==, !=, and, or)."""

    if not marker:
        return True
    tokens = re.findall(r"\(|\)|and|or|[a-z_]+\s*(?:==|!=)\s*'[^']*'", marker)
    if "".join(tokens).replace(" ", "") != marker.replace(" ", ""):
        raise BuildError(f"marker not understood: {marker}")

    def comparison(token: str) -> str:
        if token in ("(", ")", "and", "or"):
            return token
        name, operator, value = re.fullmatch(r"([a-z_]+)\s*(==|!=)\s*'([^']*)'", token).groups()
        if name not in MARKER_ENVIRONMENT:
            raise BuildError(f"marker variable not known: {name}")
        return str((MARKER_ENVIRONMENT[name] == value) == (operator == "=="))

    return bool(eval(" ".join(comparison(token) for token in tokens), {"__builtins__": {}}, {}))  # noqa: S307


BUILDERS = {"python": Build.build_python, "models": Build.build_models, "tools": Build.build_tools}


def build(args: argparse.Namespace) -> int:
    pins = json.loads(PINS_FILE.read_text(encoding="utf-8"))
    problems = check_inputs(pins)
    if problems:
        print("The manifests and RUNTIME_PINS.json disagree; nothing is built:\n  " + "\n  ".join(problems))
        return 1
    builder = Build(Path(args.build_dir).resolve(), Path(args.out_dir).resolve(), check_archive=not args.no_check_archive)
    if args.clean:
        log(f"emptying {builder.directory}")
        remove_tree(builder.directory)
    builder.prepare()
    if not SYSTEM_TAR.is_file():
        raise BuildError(f"Windows tar.exe not found at {SYSTEM_TAR}")
    summaries = []
    for layer in args.layer or list(lm.LAYERS):
        log(f"building the {layer} layer in {builder.work / layer}")
        started = time.perf_counter()
        root, built, exceptions = BUILDERS[layer](builder)
        summary = builder.finish(layer, root, built, exceptions)
        summary["seconds"]["total"] = round(time.perf_counter() - started, 1)
        (builder.out / layer / summary["version"] / "summary.json").write_text(
            json.dumps(summary, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        summaries.append(summary)
        if not args.keep_work:
            remove_tree(root)
    print(json.dumps(summaries, indent=1))
    return 0


def _manifests_under(path: Path) -> dict[str, Path]:
    """layer -> manifest: a manifest file, a layer directory, or an output directory of layers."""

    path = Path(path)
    if path.is_file():
        manifest, _ = lm.read(path)
        return {manifest.layer: path}
    if (path / lm.MANIFEST_NAME).is_file():
        manifest, _ = lm.read(path / lm.MANIFEST_NAME)
        return {manifest.layer: path / lm.MANIFEST_NAME}
    found = {}
    if (path / "manifest.json").is_file():
        manifest, _ = lm.read(path / "manifest.json")
        return {manifest.layer: path / "manifest.json"}
    for candidate in sorted(path.glob("*/*/manifest.json")) or sorted(path.glob("*/manifest.json")):
        manifest, _ = lm.read(candidate)
        if manifest.layer in found:
            raise BuildError(f"two {manifest.layer} layers under {path}")
        found[manifest.layer] = candidate
    return found


def compare(args: argparse.Namespace) -> int:
    first, second = _manifests_under(Path(args.first)), _manifests_under(Path(args.second))
    status = 0
    for layer in sorted(first.keys() | second.keys()):
        if layer not in first or layer not in second:
            print(f"{layer}: only in {'the first' if layer in first else 'the second'}")
            status = 1
            continue
        (a, a_data), (b, b_data) = lm.read(first[layer]), lm.read(second[layer])
        if a_data == b_data:
            print(f"{layer}: identical ({len(a.files)} files, version {lm.version_of(a_data)})")
            continue
        status = 1
        found = lm.differences(a, b)
        print(f"{layer}: DIFFERENT ({len(found)} differences)")
        for line in found[:50]:
            print(f"  {line}")
    return status


def remove_staged(path: Path) -> None:
    """Remove a staged install root: its junctions are unlinked, never followed into a layer."""

    if lm.is_link(path):
        os.rmdir(path) if path.is_dir() else os.unlink(path)
        return
    if not path.exists():
        return
    if path.is_dir():
        for child in list(path.iterdir()):
            remove_staged(child)
        os.rmdir(path)
    else:
        path.unlink()


def stage(args: argparse.Namespace) -> int:
    """Unpack built layers into `--into` and lay them out as an install root the quality harness finds.

    `<into>\\layers\\<layer>\\<version>\\` gets each archive unpacked with Windows' `tar.exe` and checked
    file by file. `<into>\\install\\` is what `tests/quality_harness/real.py` takes for a managed install
    (`NONOKA_QUALITY_INSTALL_DIR`): `models` a junction to the model layer, `runtime\\<tool>\\<version>`
    a junction to the tool's directory in the tool layer with `current.json` beside it. Nothing in the
    harness changes; the interpreter comes from `NONOKA_QUALITY_PYTHON`. The layers are only read.
    """

    import _winapi

    into = Path(args.into).resolve()
    found = _manifests_under(Path(args.source))
    missing = [layer for layer in lm.LAYERS if layer not in found]
    if missing:
        raise BuildError(f"no {', '.join(missing)} layer under {args.source}")
    unpacked: dict[str, Path] = {}
    for layer in lm.LAYERS:
        manifest_path = found[layer]
        archive = manifest_path.parent / "layer.tar.zst"
        version = lm.version_of(manifest_path.read_bytes())
        target = into / "layers" / layer / version
        if not (target / lm.MANIFEST_NAME).is_file():
            remove_tree(target)
            target.mkdir(parents=True)
            started = time.perf_counter()
            completed = subprocess.run([str(SYSTEM_TAR), "-x", "-f", str(archive), "-C", str(target)],
                                       capture_output=True, text=True)
            if completed.returncode != 0:
                raise BuildError(f"tar could not unpack {archive}: {completed.stderr[-400:]}")
            log(f"{layer}: unpacked into {target} in {time.perf_counter() - started:.1f}s")
        manifest, data = lm.manifest_in(target)
        if data != manifest_path.read_bytes():
            raise BuildError(f"{target} carries another manifest than {manifest_path}")
        problems = lm.compare(manifest, target, full=True)
        if problems:
            raise BuildError(f"{target} does not match its manifest:\n  " + "\n  ".join(problems[:30]))
        unpacked[layer] = target
    install = into / "install"
    remove_staged(install)
    (install / "runtime").mkdir(parents=True)
    _winapi.CreateJunction(str(unpacked["models"]), str(install / "models"))
    tools_manifest, _ = lm.manifest_in(unpacked["tools"])
    for tool in tools_manifest.sources["tools"]:
        directory = install / "runtime" / tool["directory"]
        directory.mkdir(parents=True)
        _winapi.CreateJunction(str(unpacked["tools"] / tool["directory"]), str(directory / tool["version"]))
        (directory / "current.json").write_text(json.dumps({"current": tool["version"]}, separators=(",", ":")),
                                                encoding="utf-8")
    data_dir = into / "data"
    data_dir.mkdir(exist_ok=True)
    print(json.dumps({
        "NONOKA_QUALITY_PYTHON": str(unpacked["python"] / "python.exe"),
        "NONOKA_QUALITY_INSTALL_DIR": str(install),
        "NONOKA_QUALITY_DATA_DIR": str(data_dir),
        "layers": {layer: str(path) for layer, path in unpacked.items()},
    }, indent=1))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    checkout = main_checkout()
    making = commands.add_parser("build", help="build layers")
    making.add_argument("--layer", choices=lm.LAYERS, action="append", help="build only this layer (repeatable)")
    making.add_argument("--build-dir", default=str(checkout / ".tmp" / "layer-build"))
    making.add_argument("--out-dir", default=str(checkout / "dist" / "layers"))
    making.add_argument("--clean", action="store_true", help="empty the build directory first (downloads included)")
    making.add_argument("--keep-work", action="store_true", help="keep the staged layer trees")
    making.add_argument("--no-check-archive", action="store_true", help="skip unpacking each archive to check it")
    comparing = commands.add_parser("compare", help="compare two builds' manifests")
    comparing.add_argument("first")
    comparing.add_argument("second")
    staging = commands.add_parser("stage", help="unpack built layers and lay out an install root for --real")
    staging.add_argument("source", help="a build output directory (dist/layers)")
    staging.add_argument("--into", required=True, help="where to unpack (a path with spaces is fine)")
    args = parser.parse_args(argv)
    try:
        return {"build": build, "compare": compare, "stage": stage}[args.command](args)
    except BuildError as error:
        print(f"BUILD FAILED: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
