"""The layer manager: download, verify, unpack, switch, repair and clean the prebuilt layers.

`docs/transcription-engines-plan.md` 4.2 item 19, WP-E3. What this version of Nonoka wants is
`resources/layers.json` (written by WP-E2's `scripts/publish_layers.py`): for each of the three layers
(`layer_manifest.LAYERS`) its version, and where its manifest and archive are, relative to `baseUrl`,
each with its size and sha256. The version must be the first 16 hex digits of the manifest's sha256;
that is checked whenever the index is read.

On disk (4.4 item 24), under the big-file root:

    layers\\<layer>\\<version>\\          one unpacked version; its manifest at the root (`.nonoka-layer.json`)
    layers\\<layer>\\current.json         `{"schema": 1, "layer": ..., "version": ...}`: the version in use
    layers\\<layer>\\<version>.unpack\\   a version being unpacked (removed and redone if found)
    layers\\<layer>\\<version>.retired\\  a version moved aside before it is deleted
    cache\\downloads\\                    archives and manifests while they download (resumable `.part`)

Installing a layer: fetch its manifest (the offline package first, then the network) and check it
against the index; refuse when the deepest file would end up longer than `MAX_PATH_CHARS` (counted
with the unpack directory's name, whatever the system's long-path setting); fetch the archive (the
offline package first, read where it is; else a resumable download through Nonoka's proxy setting,
`runtime.fetch`) and check its sha256; unpack it with Windows' own `tar.exe` (which keeps the
modification times the archive carries -- the separator's `.verified` stamps state them) into the
unpack directory; check the tree against the manifest; rename it into place and only then switch
`current.json`. The downloaded archive is deleted once the pointer is switched (owner 2026-11-13); the
offline package is only ever read. A process killed at any point leaves either the old pointer or a
finished version: the next attempt removes a half-unpacked directory, resumes a half-downloaded
archive, and adopts a finished version the pointer never reached once it checks out.

Readiness (`state`): the pointer names the version the index wants, that directory exists, its
manifest is the one the index names, and the tree matches it -- the quick check compares the file list
and sizes, the full check (repair, migration, on request) hashes every file. Files the manifest does not
list do not make a layer unready: they are reported by name (`LayerState.extra`; numba writes its
compile cache next to librosa's sources until the worker environment sets `NUMBA_CACHE_DIR`, WP-R5 /
WP-H) and go away with a repair. Missing, resized or changed files, and links, do make it unready.

Old versions -- anything but the wanted version and the one the pointer names -- are deleted when
nothing is using them (`in_use`; by default, no unfinished task in the data directory). A version is
renamed aside before it is deleted, so one whose files are still open is left alone rather than
half-deleted.

Model completion for the migration (4.4 item 25, owner 2026-11-13): `complete_models` checks a
directory of moved models against the model layer's manifest, keeps every file that matches, and only
when something is missing or wrong unpacks just those files from the model layer archive; then the
directory matches the manifest exactly and is registered as the current model layer version.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import sys
import threading
import time
from typing import Any, BinaryIO
from urllib.parse import urljoin, urlparse

from nonoka_x.runtime import layer_manifest as lm
from nonoka_x.runtime.fetch import (
    DownloadAsset,
    DownloadPaused,
    DownloadProgress,
    NetworkRoute,
    download_file,
)
from nonoka_x.runtime.fsops import holding_lock, no_window, remove_tree, replace_path, write_atomic
from nonoka_x.runtime.paths import NonokaPaths

#: This version of Nonoka's layers (WP-E2 writes it).
INDEX_FILE = Path(__file__).resolve().parents[1] / "resources" / "layers.json"
INDEX_SCHEMA = 1
POINTER_SCHEMA = 1

#: The longest full path a layer file may get (owner 2026-11-13): Windows' MAX_PATH less the NUL.
MAX_PATH_CHARS = 259
PATH_TOO_DEEP_MESSAGE = "安装位置太深，请换一个路径更短的位置"

UNPACK_SUFFIX = ".unpack"
RETIRED_SUFFIX = ".retired"
#: Under the big-file root's `cache\\`: downloads in progress.
DOWNLOADS_DIRECTORY = "downloads"
#: Beside each layer's versions: serializes installs of that layer across processes.
LOCK_NAME = ".install.lock"
#: The moved model directory WP-M1 leaves (`migration.hooks.MIGRATED_MODELS_NAME`): never a leftover.
MIGRATED_NAME = "migrated"

#: Set by the desktop for the sidecar and its children (`download_route.OFFLINE_PACK_ENVIRONMENT`).
OFFLINE_PACK_ENVIRONMENT = "NONOKA_OFFLINE_PACK"
OFFLINE_PACK_MARKER = "pack.json"

def _default_tar() -> Path:
    if os.name == "nt":
        return Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "tar.exe"
    if sys.platform == "darwin":
        mac_tar = Path("/usr/bin/tar")
        if mac_tar.is_file():
            return mac_tar
    found = shutil.which("tar")
    return Path(found) if found else Path("/usr/bin/tar")


SYSTEM_TAR = _default_tar()

#: How long a quick check's answer is reused within one process.
QUICK_CHECK_SECONDS = 30.0

LABELS = {"python": "Python 运行环境", "models": "模型", "models-moss": "MOSS 模型", "tools": "工具",
          "screen-text": "文字识别层"}


class LayerIndexError(lm.LayerError):
    """`layers.json` is missing or cannot be what it claims."""


class PathTooDeep(lm.LayerError):
    def __init__(self, layer: str, longest: int, path: str) -> None:
        self.layer, self.longest, self.path = layer, longest, path
        self.excess = longest - MAX_PATH_CHARS
        super().__init__(
            f"{PATH_TOO_DEEP_MESSAGE}：{LABELS.get(layer, layer)}里最深的文件路径会有 {longest} 个字符，"
            f"超出 Windows 的上限（{MAX_PATH_CHARS} 个字符）{self.excess} 个字符"
        )


class LayerMismatch(lm.LayerError):
    """An unpacked tree, a manifest or an archive is not what the index says."""


class LayerBusy(lm.LayerError):
    """A version cannot be moved or deleted because something still has its files open."""


# --- the index (layers.json) ------------------------------------------------------------------------


def _hex64(value: Any, what: str) -> str:
    text = str(value or "").lower()
    if len(text) != 64 or any(character not in "0123456789abcdef" for character in text):
        raise LayerIndexError(f"{what}: not a sha256: {value!r}")
    return text


@dataclass(frozen=True)
class ObjectRef:
    """One immutable object of the index: a path relative to `baseUrl` (or the offline package)."""

    path: str
    size: int
    sha256: str

    @classmethod
    def from_dict(cls, data: Any, what: str) -> "ObjectRef":
        if not isinstance(data, Mapping):
            raise LayerIndexError(f"{what}: not an object")
        try:
            path = lm.check_path(str(data.get("path") or ""))
            size = int(data.get("size"))
        except (lm.LayerError, TypeError, ValueError) as error:
            raise LayerIndexError(f"{what}: {error}") from error
        if size < 0:
            raise LayerIndexError(f"{what}: negative size")
        return cls(path, size, _hex64(data.get("sha256"), what))


@dataclass(frozen=True)
class LayerEntry:
    layer: str
    version: str
    manifest: ObjectRef
    archive: ObjectRef


@dataclass(frozen=True)
class LayerIndex:
    platform: str
    base_url: str
    layers: Mapping[str, LayerEntry]
    #: `files.layers` / `files.path`: layers whose files are also stored one by one. Empty since
    #: 2026-11-13 (plan appendix B, question 63), and then there is no per-file download at all.
    file_layers: tuple[str, ...] = ()
    file_path: str = ""

    def url(self, path: str) -> str:
        return urljoin(self.base_url, path)

    def entry(self, layer: str) -> LayerEntry:
        try:
            return self.layers[layer]
        except KeyError:
            raise LayerIndexError(f"layers.json has no {layer!r} layer") from None


def parse_index(data: bytes) -> LayerIndex:
    """`layers.json` as WP-E2 writes it (plan, the quoted block at the head of WP-E2)."""

    try:
        body = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as error:
        raise LayerIndexError(f"layers.json is not JSON: {error}") from error
    if not isinstance(body, Mapping) or body.get("schema") != INDEX_SCHEMA:
        raise LayerIndexError(f"unknown layers.json schema: {body.get('schema') if isinstance(body, Mapping) else body!r}")
    platform = body.get("platform")
    if platform != lm.PLATFORM:
        raise LayerIndexError(f"layers.json is for {platform!r}, not {lm.PLATFORM}")
    base_url = str(body.get("baseUrl") or "")
    parsed = urlparse(base_url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc or not base_url.endswith("/"):
        raise LayerIndexError(f"layers.json baseUrl must be an http(s) URL ending in '/': {base_url!r}")
    raw_layers = body.get("layers")
    if not isinstance(raw_layers, Mapping):
        raise LayerIndexError("layers.json has no layers")
    layers: dict[str, LayerEntry] = {}
    for layer, item in raw_layers.items():
        if layer not in lm.LAYERS:
            raise LayerIndexError(f"layers.json names an unknown layer: {layer!r}")
        if not isinstance(item, Mapping):
            raise LayerIndexError(f"layers.json {layer}: not an object")
        manifest = ObjectRef.from_dict(item.get("manifest"), f"{layer}.manifest")
        archive = ObjectRef.from_dict(item.get("archive"), f"{layer}.archive")
        version = str(item.get("version") or "")
        if version != manifest.sha256[:16]:
            raise LayerIndexError(
                f"layers.json {layer}: version {version!r} is not the first 16 digits of its manifest's sha256"
            )
        layers[layer] = LayerEntry(layer, version, manifest, archive)
    files = body.get("files") or {}
    if not isinstance(files, Mapping):
        raise LayerIndexError("layers.json files: not an object")
    file_layers = tuple(str(item) for item in files.get("layers") or ())
    if any(item not in layers for item in file_layers):
        raise LayerIndexError(f"layers.json files.layers names a layer it does not list: {file_layers}")
    return LayerIndex(lm.PLATFORM, base_url, layers, file_layers, str(files.get("path") or ""))


def load_index(path: Path | None = None) -> LayerIndex:
    source = Path(path) if path is not None else INDEX_FILE
    try:
        data = source.read_bytes()
    except OSError as error:
        raise LayerIndexError(
            f"这个版本没有登记运行环境层（layers.json 不可读：{source}：{error.strerror or error}）"
        ) from error
    return parse_index(data)


def offline_pack() -> Path | None:
    """The offline package this application came with (`NONOKA_OFFLINE_PACK` holding `pack.json`), or None.

    It stores the index's objects under the same relative paths (plan WP-E2); it is only ever read.
    """

    value = os.environ.get(OFFLINE_PACK_ENVIRONMENT, "").strip()
    if not value:
        return None
    root = Path(value)
    return root if (root / OFFLINE_PACK_MARKER).is_file() else None


# --- checking a tree ----------------------------------------------------------------------------------------


@dataclass
class Inspection:
    """How a tree departs from a manifest, kind by kind (paths are manifest paths)."""

    missing: list[str] = field(default_factory=list)
    resized: list[str] = field(default_factory=list)
    changed: list[str] = field(default_factory=list)
    links: list[str] = field(default_factory=list)
    extra: list[str] = field(default_factory=list)
    extra_dirs: list[str] = field(default_factory=list)
    missing_dirs: list[str] = field(default_factory=list)

    @property
    def broken(self) -> list[str]:
        """What makes the tree unusable: everything but extra files and directories."""

        return (
            [f"missing: {path}" for path in self.missing]
            + [f"size differs: {path}" for path in self.resized]
            + [f"sha256 differs: {path}" for path in self.changed]
            + [f"link (layers hold only regular files): {path}" for path in self.links]
            + [f"empty directory missing: {path}" for path in self.missing_dirs]
        )

    @property
    def problems(self) -> list[str]:
        """Every departure, extras included: an exact match has none."""

        return (
            self.broken
            + [f"not in the manifest: {path}" for path in self.extra]
            + [f"empty directory not in the manifest: {path}" for path in self.extra_dirs]
        )

    @property
    def wrong(self) -> list[str]:
        """Manifest files that are not there as listed: what a completion has to fetch."""

        return sorted(set(self.missing) | set(self.resized) | set(self.changed) | set(self.links))


_REPARSE_POINT = 0x400


def _scan(root: Path) -> tuple[dict[str, int], list[str], list[str]]:
    """Every regular file (manifest path -> size), empty directory and link under `root`, links not followed.

    `layer_manifest.walk` in one pass: the sizes and the reparse-point flag come from the directory
    entries themselves (on Windows `scandir` carries them), so a 45 000-file layer costs no `stat` per
    file. The manifest at the root is not a file of the layer.
    """

    files: dict[str, int] = {}
    empty: list[str] = []
    links: list[str] = []
    pending: list[tuple[str, str]] = [(str(root), "")]
    while pending:
        directory, prefix = pending.pop()
        with os.scandir(directory) as entries:
            children = list(entries)
        if not children and prefix:
            empty.append(prefix.rstrip("/"))
        for entry in children:
            name = prefix + entry.name
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError:
                links.append(name)
                continue
            if entry.is_symlink() or getattr(info, "st_file_attributes", 0) & _REPARSE_POINT:
                links.append(name)
            elif entry.is_dir(follow_symlinks=False):
                pending.append((entry.path, name + "/"))
            elif entry.is_file(follow_symlinks=False):
                if not prefix and entry.name == lm.MANIFEST_NAME:
                    continue
                files[name] = info.st_size
            else:
                links.append(name)
    return files, sorted(empty), sorted(links)


def inspect(manifest: lm.LayerManifest, root: Path, *, full: bool, report: Callable[[int], None] | None = None) -> Inspection:
    """`layer_manifest.compare`, but sorted into kinds so extra files can be told from broken ones.

    `report` is told the bytes covered so far (sizes on the quick check, hashed bytes on the full one).
    """

    root = Path(root)
    found = Inspection()
    present, empty, links = _scan(root)
    found.links = links
    expected = manifest.by_path()
    found.missing = sorted(expected.keys() - present.keys() - set(found.links))
    found.extra = sorted(present.keys() - expected.keys())
    covered = 0
    for count, name in enumerate(sorted(expected.keys() & present.keys()), 1):
        item = expected[name]
        if present[name] != item.size:
            found.resized.append(name)
        elif full and lm.sha256_file(root / name) != item.sha256:
            found.changed.append(name)
        covered += item.size
        # Every file while hashing; every 500 on the quick check, which takes well under a second.
        if report is not None and (full or count % 500 == 0):
            report(covered)
    if report is not None:
        report(covered)
    empty_now = set(empty)
    found.extra_dirs = sorted(empty_now - set(manifest.empty_dirs))
    found.missing_dirs = sorted(path for path in set(manifest.empty_dirs) - empty_now if not (root / path).is_dir())
    found.missing.sort()
    return found


def longest_path(root: Path, manifest: lm.LayerManifest) -> tuple[int, str]:
    """The longest full Windows path a file or directory of the layer gets under `root`."""

    names = [lm.MANIFEST_NAME, *(item.path for item in manifest.files), *manifest.empty_dirs]
    deepest = max(names, key=len)
    full = os.path.join(str(root), deepest.replace("/", os.sep))
    return len(full), full


def check_path_length(layer: str, root: Path, manifest: lm.LayerManifest) -> None:
    length, path = longest_path(root, manifest)
    if length > MAX_PATH_CHARS:
        raise PathTooDeep(layer, length, path)


# --- unpacking ----------------------------------------------------------------------------------------------

_ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"
_ZSTD_CHUNK = 256 * 1024


def _open_zstd_reader(stream: BinaryIO) -> Any | None:
    """Return a streaming zstd decompressor for `stream`, or `None` if not zstd or unavailable.

    Windows 10 and older Windows 11 builds ship a `bsdtar` (`System32\\tar.exe`) compiled without
    `libzstd`, which then fails trying to spawn an external `zstd -d -qq`. Decompressing in Python
    and piping raw tar bytes to `tar.exe` works on every Windows 10/11 build while preserving
    `bsdtar`'s mtime handling, path-traversal checks, and pure-ASCII argv workaround.
    """

    head = stream.read(len(_ZSTD_MAGIC))
    stream.seek(0)
    if head != _ZSTD_MAGIC:
        return None
    try:
        import zstandard  # type: ignore[import-not-found]
    except ImportError:
        return None
    return zstandard.ZstdDecompressor().stream_reader(stream, closefd=False)


def _start_zstd_feeder(
    zstd_reader: Any,
    sink: BinaryIO,
    stop: threading.Event,
) -> tuple[threading.Thread, list[str]]:
    errors: list[str] = []

    def feed() -> None:
        try:
            while not stop.is_set():
                chunk = zstd_reader.read(_ZSTD_CHUNK)
                if not chunk:
                    break
                try:
                    sink.write(chunk)
                except (BrokenPipeError, OSError):
                    break
        except Exception as error:
            if not stop.is_set():
                errors.append(f"zstd: {error}")
        finally:
            try:
                zstd_reader.close()
            except Exception:
                pass
            try:
                sink.close()
            except OSError:
                pass

    thread = threading.Thread(target=feed, name="nonoka-layer-zstd", daemon=True)
    thread.start()
    return thread, errors


def unpack(
    archive: Path,
    destination: Path,
    *,
    members: Iterable[str] | None = None,
    sizes: Mapping[str, int] | None = None,
    report: Callable[[int], None] | None = None,
    should_pause: Callable[[], bool] | None = None,
    tar: Path | None = None,
) -> None:
    """Unpack a layer archive (`tar.zst`) into `destination` with Windows' `tar.exe`.

    bsdtar keeps the modification times the archive carries (it is never given `-m`), refuses
    absolute and `..` member names, and is the tool WP-E1 built and checked the archives with.
    `members` limits it to those manifest paths. `report` gets the bytes unpacked so far, counted
    from `sizes` as tar names each member. A pause stops tar; the caller removes what it left.
    """

    destination.mkdir(parents=True, exist_ok=True)
    # Windows' built-in bsdtar uses ANSI argv, so non-ASCII paths (e.g. Chinese on an English locale)
    # get mangled if passed via -f / -C / -T. Passing destination as cwd (CreateProcessW Unicode),
    # streaming the archive on stdin (-f -), and keeping the member list as a local ASCII filename
    # keeps every argv token pure ASCII.
    command = [str(tar or SYSTEM_TAR), "-x", "-v", "-f", "-"]
    listing: Path | None = None
    if members is not None:
        listing = destination / ".nonoka-unpack-members.txt"
        listing.write_text("\n".join(members) + "\n", encoding="utf-8")
        command += ["-T", listing.name]
    archive_stream = archive.open("rb")
    zstd_reader = _open_zstd_reader(archive_stream)
    stdin_target: Any = subprocess.PIPE if zstd_reader is not None else archive_stream
    try:
        process = subprocess.Popen(
            command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, stdin=stdin_target,
            cwd=str(destination), creationflags=no_window(),
        )
    except Exception:
        if zstd_reader is not None:
            zstd_reader.close()
        archive_stream.close()
        if listing is not None:
            listing.unlink(missing_ok=True)
        raise
    tail: list[str] = []
    done = 0
    stop_feed = threading.Event()
    feeder: threading.Thread | None = None
    feeder_errors: list[str] = []
    if zstd_reader is not None:
        assert process.stdin is not None
        feeder, feeder_errors = _start_zstd_feeder(zstd_reader, process.stdin, stop_feed)

    def read() -> None:
        nonlocal done
        assert process.stderr is not None
        for raw in process.stderr:
            line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
            name = line[2:] if line.startswith("x ") else ""
            if name and sizes is not None and name in sizes:
                done += sizes[name]
                if report is not None:
                    report(done)
            elif not name:
                tail.append(line)
                del tail[:-20]

    reader = threading.Thread(target=read, name="nonoka-layer-unpack", daemon=True)
    reader.start()
    try:
        while process.poll() is None:
            if should_pause is not None and should_pause():
                stop_feed.set()
                process.kill()
                process.wait()
                raise DownloadPaused("Unpacking paused")
            time.sleep(0.1)
    finally:
        stop_feed.set()
        if process.poll() is None:
            process.kill()
            process.wait()
        reader.join(timeout=5)
        if feeder is not None:
            feeder.join(timeout=5)
        archive_stream.close()
        if listing is not None:
            listing.unlink(missing_ok=True)
    if process.returncode != 0 or feeder_errors:
        details = [*feeder_errors, *tail[-5:]]
        raise LayerMismatch(f"tar could not unpack {archive.name} ({process.returncode}): " + " | ".join(details))


def read_embedded_manifest(archive: Path, *, tar: Path | None = None) -> bytes:
    """Read `.nonoka-layer.json` (the first entry) straight from a layer archive without unpacking it."""

    command = [str(tar or SYSTEM_TAR), "-x", "-q", "-O", "-f", "-", lm.MANIFEST_NAME]
    with archive.open("rb") as archive_stream:
        zstd_reader = _open_zstd_reader(archive_stream)
        if zstd_reader is None:
            completed = subprocess.run(
                command,
                capture_output=True,
                stdin=archive_stream,
                creationflags=no_window(),
            )
            if completed.returncode != 0:
                detail = completed.stderr.decode("utf-8", errors="replace").strip()[-240:]
                raise LayerMismatch(
                    f"无法从离线包 {archive.name} 读取清单（{lm.MANIFEST_NAME}）：{detail or completed.returncode}"
                )
            return completed.stdout

        try:
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.PIPE,
                creationflags=no_window(),
            )
        except Exception:
            zstd_reader.close()
            raise
        stop_feed = threading.Event()
        assert process.stdin is not None and process.stdout is not None and process.stderr is not None
        feeder, feeder_errors = _start_zstd_feeder(zstd_reader, process.stdin, stop_feed)
        stderr_chunks: list[bytes] = []

        def drain_stderr() -> None:
            assert process.stderr is not None
            stderr_chunks.append(process.stderr.read())

        stderr_reader = threading.Thread(target=drain_stderr, name="nonoka-layer-manifest-err", daemon=True)
        stderr_reader.start()
        try:
            stdout_data = process.stdout.read()
            process.wait()
        finally:
            stop_feed.set()
            if process.poll() is None:
                process.kill()
                process.wait()
            stderr_reader.join(timeout=5)
            feeder.join(timeout=5)
        if process.returncode != 0:
            stderr_text = b"".join(stderr_chunks).decode("utf-8", errors="replace").strip()
            detail = " | ".join([*feeder_errors, *([stderr_text] if stderr_text else [])])[-240:]
            raise LayerMismatch(
                f"无法从离线包 {archive.name} 读取清单（{lm.MANIFEST_NAME}）：{detail or process.returncode}"
            )
        return stdout_data


# --- the manager --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class LayerProgress:
    layer: str
    stage: str  # manifest | downloading | offline | verifying | unpacking | checking | completed
    message: str
    completed: int = 0
    total: int = 0
    bytes_per_second: float = 0.0


ProgressCallback = Callable[[LayerProgress], None]
PauseCheck = Callable[[], bool]


@dataclass(frozen=True)
class LayerState:
    layer: str
    #: ready | missing | outdated (the pointer names another version) | damaged
    state: str
    wanted: str
    current: str | None
    path: Path | None
    #: What makes it unusable (damaged), one line each.
    problems: tuple[str, ...] = ()
    #: Files and empty directories the manifest does not list (they do not make it unready).
    extra: tuple[str, ...] = ()
    full: bool = False

    @property
    def ready(self) -> bool:
        return self.state == "ready"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.layer,
            "state": self.state,
            "version": self.wanted,
            "current": self.current,
            "path": str(self.path) if self.path is not None else None,
            "problems": list(self.problems),
            "extra": list(self.extra),
            "checked": "full" if self.full else "quick",
        }


@dataclass(frozen=True)
class UnusedVersion:
    layer: str
    name: str
    path: Path
    bytes: int

    def to_dict(self) -> dict[str, Any]:
        return {"layer": self.layer, "name": self.name, "path": str(self.path), "bytes": self.bytes}


@dataclass(frozen=True)
class CompletionReport:
    """What `complete_models` did."""

    version: str
    kept: int
    #: Manifest paths taken from the archive.
    fetched: tuple[str, ...]
    #: Manifest paths written from the manifest itself (markers whose bytes it pins).
    rebuilt: tuple[str, ...]
    #: Paths removed because the manifest does not list them.
    removed: tuple[str, ...]
    #: Bytes downloaded (0 when nothing was fetched or the offline package had the archive).
    downloaded: int
    source: str  # "" | "offline" | "network"

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version, "kept": self.kept, "fetched": list(self.fetched), "rebuilt": list(self.rebuilt),
            "removed": list(self.removed), "downloaded": self.downloaded, "source": self.source,
        }


def _no_task_running(paths: NonokaPaths) -> Callable[[], bool]:
    def in_use() -> bool:
        from nonoka_x.migration.legacy import running_tasks

        return bool(running_tasks(paths.data))

    return in_use


def _tree_bytes(path: Path) -> int:
    total = 0
    pending = [path]
    while pending:
        directory = pending.pop()
        try:
            entries = list(os.scandir(directory))
        except OSError:
            continue
        for entry in entries:
            try:
                if lm.is_link(Path(entry.path)):
                    continue
                if entry.is_dir(follow_symlinks=False):
                    pending.append(Path(entry.path))
                elif entry.is_file(follow_symlinks=False):
                    total += entry.stat(follow_symlinks=False).st_size
            except OSError:
                continue
    return total


def _set_fixed_times(root: Path, manifest: lm.LayerManifest) -> None:
    for item in manifest.files:
        os.utime(root / item.path, (lm.FIXED_MTIME, lm.FIXED_MTIME))


class LayerManager:
    """The layers of one big-file root, against one index."""

    def __init__(
        self,
        paths: NonokaPaths,
        index: LayerIndex,
        *,
        in_use: Callable[[], bool] | None = None,
        offline: Callable[[], Path | None] = offline_pack,
        routes: Callable[[], list[NetworkRoute]] | None = None,
        tar: Path | None = None,
    ) -> None:
        self.paths = paths
        self.index = index
        self._in_use = in_use if in_use is not None else _no_task_running(paths)
        self._offline = offline
        self._routes = routes
        self._tar = tar
        self._checked: dict[tuple[str, str], tuple[float, LayerState]] = {}
        self._manifests: dict[tuple[str, str], lm.LayerManifest] = {}
        self._sizes: dict[tuple[str, int], int] = {}
        self._lock = threading.RLock()

    # -- where things are ------------------------------------------------------------------------------------

    @property
    def downloads(self) -> Path:
        return self.paths.cache / DOWNLOADS_DIRECTORY

    def pointer(self, layer: str) -> str | None:
        """The version `current.json` names, or None."""

        try:
            record = json.loads(self.paths.layer_pointer(layer).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(record, Mapping) or record.get("layer") != layer:
            return None
        version = record.get("version")
        return version if isinstance(version, str) and version else None

    def _write_pointer(self, layer: str, version: str) -> None:
        write_atomic(
            self.paths.layer_pointer(layer),
            json.dumps({"schema": POINTER_SCHEMA, "layer": layer, "version": version}, sort_keys=True) + "\n",
        )

    def wanted_dir(self, layer: str) -> Path:
        return self.paths.layer_version(layer, self.index.entry(layer).version)

    def root(self, layer: str) -> Path | None:
        """The directory of the wanted version when it is the current one and ready; else None."""

        if layer not in self.index.layers:
            return None
        state = self.state(layer)
        return state.path if state.ready else None

    def _installed_manifest(self, layer: str) -> lm.LayerManifest | None:
        root = self.root(layer)
        if root is None:
            return None
        key = (layer, self.index.entry(layer).version)
        with self._lock:
            cached = self._manifests.get(key)
        if cached is None:
            try:
                cached, _ = lm.manifest_in(root)
            except (OSError, ValueError, lm.LayerError):
                return None
            with self._lock:
                self._manifests[key] = cached
        return cached

    # -- readiness -------------------------------------------------------------------------------------------

    def forget(self, layer: str | None = None) -> None:
        """Drop remembered quick checks (all, or one layer's)."""

        with self._lock:
            for key in [key for key in self._checked if layer is None or key[0] == layer]:
                del self._checked[key]

    def state(self, layer: str, *, full: bool = False, fresh: bool = False) -> LayerState:
        entry = self.index.entry(layer)
        key = (layer, entry.version)
        if not full and not fresh:
            with self._lock:
                remembered = self._checked.get(key)
            if remembered is not None and time.monotonic() - remembered[0] < QUICK_CHECK_SECONDS:
                if self.pointer(layer) == remembered[1].current:
                    return remembered[1]
        result = self._state(layer, entry, full=full)
        with self._lock:
            self._checked[key] = (time.monotonic(), result)
        return result

    def _state(self, layer: str, entry: LayerEntry, *, full: bool) -> LayerState:
        current = self.pointer(layer)
        base = dict(layer=layer, wanted=entry.version, current=current, full=full)
        if current is None:
            return LayerState(state="missing", path=None, **base)
        try:
            directory = self.paths.layer_version(layer, current)
        except ValueError:
            return LayerState(state="damaged", path=None, problems=(f"current.json names {current!r}",), **base)
        if current != entry.version:
            return LayerState(state="outdated", path=directory if directory.is_dir() else None, **base)
        if not directory.is_dir():
            return LayerState(state="missing", path=None, **base)
        try:
            manifest, data = lm.manifest_in(directory)
        except (OSError, ValueError, lm.LayerError) as error:
            return LayerState(state="damaged", path=directory, problems=(f"manifest: {error}",), **base)
        if lm.digest_of(data) != entry.manifest.sha256 or manifest.layer != layer:
            return LayerState(state="damaged", path=directory, problems=("manifest is not the one layers.json names",), **base)
        try:
            found = inspect(manifest, directory, full=full)
        except OSError as error:
            return LayerState(state="damaged", path=directory, problems=(f"unreadable: {error}",), **base)
        extra = tuple(found.extra + [f"{path}/" for path in found.extra_dirs])
        if found.broken:
            return LayerState(state="damaged", path=directory, problems=tuple(found.broken), extra=extra, **base)
        return LayerState(state="ready", path=directory, extra=extra, **base)

    def states(self, *, full: bool = False) -> dict[str, LayerState]:
        return {layer: self.state(layer, full=full) for layer in self.index.layers}

    # -- what is in the layers ---------------------------------------------------------------------------------

    def python_executable(self, layer: str = "python") -> Path | None:
        """The interpreter of a Python layer (`python`, or `screen-text`, which carries its own)."""

        root = self.root(layer)
        if root is None:
            return None
        executable = root / ("python.exe" if os.name == "nt" else "bin/python3")
        return executable if executable.is_file() else None

    def tool_entry(self, tool_id: str) -> Mapping[str, Any] | None:
        manifest = self._installed_manifest("tools")
        if manifest is None:
            return None
        for item in manifest.sources.get("tools") or ():
            if item.get("id") == tool_id:
                return item
        return None

    def tool_dir(self, tool_id: str) -> Path | None:
        """`<tools layer>\\<tool directory>\\` of one tool of the tool layer, or None."""

        entry = self.tool_entry(tool_id)
        root = self.root("tools")
        if entry is None or root is None:
            return None
        directory = root / lm.check_path(str(entry.get("directory") or tool_id))
        return directory if directory.is_dir() else None

    def tool_file(self, name: str) -> Path | None:
        """The executable `name` (`ffmpeg`, `ffprobe`, `tokcount`, ...): a required file of a tool that is named so."""

        manifest = self._installed_manifest("tools")
        root = self.root("tools")
        if manifest is None or root is None:
            return None
        wanted = {name.lower(), f"{name.lower()}.exe"}
        for item in manifest.sources.get("tools") or ():
            for required in item.get("required_files") or ():
                if PurePosixPath(required).name.lower() in wanted:
                    path = root / lm.check_path(str(item.get("directory") or item.get("id"))) / lm.check_path(required)
                    if path.is_file():
                        return path
        return None

    def model_dir(self, model_id: str) -> Path | None:
        """Where model `model_id`'s files are inside an installed model layer, or None.

        The snapshot directory for a Hugging Face model, the model's directory for the others; only
        when the layer is ready and holds the model.
        """

        for layer in lm.MODEL_LAYERS:
            manifest = self._installed_manifest(layer)
            root = self.root(layer)
            if manifest is None or root is None:
                continue
            for item in manifest.sources.get("models") or ():
                if item.get("id") != model_id:
                    continue
                directory = root / lm.check_path(str(item["directory"]))
                if item.get("repo") and item.get("revision"):
                    directory = directory / "snapshots" / lm.check_path(str(item["revision"]))
                if directory.is_dir():
                    return directory
        return None

    def model_ids(self) -> tuple[str, ...]:
        seen: list[str] = []
        for layer in lm.MODEL_LAYERS:
            manifest = self._installed_manifest(layer)
            if manifest is None:
                continue
            for item in manifest.sources.get("models") or ():
                model_id = str(item.get("id"))
                if model_id not in seen:
                    seen.append(model_id)
        return tuple(seen)

    # -- fetching ----------------------------------------------------------------------------------------------

    def _offline_object(self, ref: ObjectRef) -> Path | None:
        root = self._offline()
        if root is None:
            return None
        candidate = root / ref.path
        try:
            return candidate if candidate.is_file() and candidate.stat().st_size == ref.size else None
        except OSError:
            return None

    def _download(self, ref: ObjectRef, suffix: str, progress: Callable[[DownloadProgress], None] | None,
                  should_pause: PauseCheck | None) -> Path:
        destination = self.downloads / f"{ref.sha256}{suffix}"
        return download_file(
            DownloadAsset(url=self.index.url(ref.path), size=ref.size, sha256=ref.sha256),
            destination, progress, should_pause, routes=self._routes,
        )

    def fetch_manifest(self, layer: str, *, should_pause: PauseCheck | None = None) -> tuple[lm.LayerManifest, bytes]:
        """The wanted version's manifest, checked against the index (offline package first)."""

        entry = self.index.entry(layer)
        installed = self.paths.layer_version(layer, entry.version) / lm.MANIFEST_NAME
        candidates = [path for path in (installed, self._offline_object(entry.manifest)) if path is not None]
        data = b""
        for path in candidates:
            try:
                data = path.read_bytes()
            except OSError:
                continue
            if lm.digest_of(data) == entry.manifest.sha256:
                break
        else:
            downloaded = self._download(entry.manifest, ".json", None, should_pause)
            data = downloaded.read_bytes()
            downloaded.unlink(missing_ok=True)
            downloaded.with_suffix(downloaded.suffix + ".lock").unlink(missing_ok=True)
        if lm.digest_of(data) != entry.manifest.sha256 or len(data) != entry.manifest.size:
            raise LayerMismatch(f"{layer}: the manifest's sha256 is not the one layers.json names")
        if lm.version_of(data) != entry.version:
            raise LayerMismatch(f"{layer}: manifest version {lm.version_of(data)} is not {entry.version}")
        manifest = lm.parse(data)
        if manifest.layer != layer or manifest.platform != lm.PLATFORM:
            raise LayerMismatch(f"{layer}: the manifest is for {manifest.layer} on {manifest.platform}")
        return manifest, data

    def fetch_archive(self, layer: str, progress: ProgressCallback, should_pause: PauseCheck | None) -> tuple[Path, bool]:
        """The wanted version's archive, sha256 checked: `(path, downloaded)`.

        From the offline package when it has it (read in place, never deleted); otherwise a resumable
        download into `cache\\downloads\\` through Nonoka's proxy route, which the caller deletes once
        the layer is switched.
        """

        entry = self.index.entry(layer)
        label = LABELS.get(layer, layer)
        offline = self._offline_object(entry.archive)
        if offline is not None:
            progress(LayerProgress(layer, "offline", f"正在校验离线包里的{label}", 0, entry.archive.size))
            if self._hash_with_progress(offline, entry.archive.size, layer, "offline", f"正在校验离线包里的{label}",
                                        progress, should_pause) == entry.archive.sha256:
                return offline, False
        message = f"正在下载{label}"
        progress(LayerProgress(layer, "downloading", message, 0, entry.archive.size))

        def downloaded(value: DownloadProgress) -> None:
            stage = "verifying" if value.downloaded >= value.total and value.total else "downloading"
            text = f"正在校验{label}的压缩包" if stage == "verifying" else message
            progress(LayerProgress(layer, stage, text, value.downloaded, value.total, value.bytes_per_second))

        return self._download(entry.archive, ".tar.zst", downloaded, should_pause), True

    @staticmethod
    def _hash_with_progress(path: Path, size: int, layer: str, stage: str, message: str,
                            progress: ProgressCallback, should_pause: PauseCheck | None) -> str:
        import hashlib

        digest = hashlib.sha256()
        done = 0
        started = time.perf_counter()
        with open(path, "rb") as source:
            for chunk in iter(lambda: source.read(8 * 1024 * 1024), b""):
                if should_pause is not None and should_pause():
                    raise DownloadPaused("Verification paused")
                digest.update(chunk)
                done += len(chunk)
                progress(LayerProgress(layer, stage, message, done, size, done / max(time.perf_counter() - started, 1e-6)))
        return digest.hexdigest()

    def _free_space_check(self, layer: str, manifest: lm.LayerManifest, archive_needed: int) -> None:
        self.paths.layers.mkdir(parents=True, exist_ok=True)
        free = shutil.disk_usage(self.paths.layers).free
        needed = manifest.total_size() + archive_needed + 256 * 1024 * 1024
        if free < needed:
            raise lm.LayerError(
                f"磁盘空间不足：安装{LABELS.get(layer, layer)}需要约 {needed / 2**30:.1f} GiB，"
                f"{self.paths.runtime} 所在的磁盘只剩 {free / 2**30:.1f} GiB"
            )

    def _verify_offline_archive(
        self,
        layer: str,
        archive_path: Path,
        progress: ProgressCallback,
        should_pause: PauseCheck | None,
    ) -> tuple[lm.LayerManifest, bytes]:
        """Verify a user-picked `.tar.zst` archive against `layers.json` and read its embedded manifest (0 network)."""

        entry = self.index.entry(layer)
        label = LABELS.get(layer, layer)
        if not archive_path.is_file():
            raise LayerMismatch(f"找不到离线包文件：{archive_path}")
        actual_size = archive_path.stat().st_size
        if actual_size != entry.archive.size:
            other = next(
                (name for name, item in self.index.layers.items() if name != layer and item.archive.size == actual_size),
                None,
            )
            if other is not None:
                other_label = LABELS.get(other, other)
                raise LayerMismatch(
                    f"所选文件大小与{label}不符，它看起来是「{other_label}」的离线包，请在「{other_label}」行选择离线安装"
                )
            raise LayerMismatch(
                f"所选离线包与当前版本所需的{label}不符（文件大小 {actual_size} 字节，需要 {entry.archive.size} 字节，"
                f"所需版本 {entry.version}）"
            )
        message = f"正在校验{label}离线包"
        progress(LayerProgress(layer, "offline", message, 0, entry.archive.size))
        digest = self._hash_with_progress(
            archive_path, entry.archive.size, layer, "offline", message, progress, should_pause,
        )
        if digest != entry.archive.sha256:
            other = next(
                (name for name, item in self.index.layers.items() if name != layer and item.archive.sha256 == digest),
                None,
            )
            if other is not None:
                other_label = LABELS.get(other, other)
                raise LayerMismatch(
                    f"所选文件是「{other_label}」的离线包，请在「{other_label}」行选择离线安装"
                )
            raise LayerMismatch(
                f"所选离线包校验失败：文件可能不完整、已损坏，或不是当前版本（{entry.version}）对应的{label}离线包"
            )
        progress(LayerProgress(layer, "manifest", f"正在读取{label}离线包里的文件清单"))
        data = read_embedded_manifest(archive_path, tar=self._tar)
        if lm.digest_of(data) != entry.manifest.sha256 or len(data) != entry.manifest.size:
            raise LayerMismatch(f"{label}离线包内的清单与当前版本（{entry.version}）不符")
        if lm.version_of(data) != entry.version:
            raise LayerMismatch(f"{label}离线包内的清单版本为 {lm.version_of(data)}，需要 {entry.version}")
        manifest = lm.parse(data)
        if manifest.layer != layer or manifest.platform != lm.PLATFORM:
            raise LayerMismatch(f"离线包内的清单属于 {manifest.layer}（{manifest.platform}），不是当前所需的 {layer}")
        return manifest, data

    def _cleanup_partial_download(self, entry: LayerEntry) -> None:
        base = self.downloads / f"{entry.archive.sha256}.tar.zst"
        partial = base.with_name(base.name + ".part")
        for candidate in (base, partial, partial.with_name(partial.name + ".pieces"),
                          partial.with_name(partial.name + ".expect"), base.with_suffix(base.suffix + ".lock")):
            try:
                candidate.unlink(missing_ok=True)
            except OSError:
                pass

    # -- installing --------------------------------------------------------------------------------------------

    def install(
        self,
        layer: str,
        *,
        progress: ProgressCallback | None = None,
        should_pause: PauseCheck | None = None,
        full: bool = False,
        force: bool = False,
        archive_path: Path | None = None,
    ) -> LayerState:
        """Make the wanted version of `layer` the current one; a no-op when it already is and checks out.

        `force` reinstalls even a ready layer (repair). `full` hashes every unpacked file before the
        switch; otherwise the archive's sha256 vouches for the content and the tree is checked by list
        and sizes. When `archive_path` is given, the archive is verified and unpacked in place without
        touching the network or deleting the caller's file.
        """

        report = progress if progress is not None else (lambda _value: None)
        entry = self.index.entry(layer)
        label = LABELS.get(layer, layer)
        with holding_lock(self.paths.layer(layer) / LOCK_NAME, should_pause=should_pause,
                          on_pause=lambda: DownloadPaused("Install paused")):
            self.forget(layer)
            if not force:
                state = self.state(layer, full=full, fresh=True)
                if state.ready:
                    return state
            target = self.paths.layer_version(layer, entry.version)
            staging = target.with_name(target.name + UNPACK_SUFFIX)
            if archive_path is not None:
                source_archive = Path(archive_path)
                manifest, data = self._verify_offline_archive(layer, source_archive, report, should_pause)
                check_path_length(layer, staging, manifest)
                check_path_length(layer, target, manifest)
                if not force and target.is_dir() and self.pointer(layer) != entry.version:
                    if not self._mismatch(target, manifest, data, full=True, layer=layer, report=report):
                        state = self._switch(layer, entry, downloaded=None)
                        self._cleanup_partial_download(entry)
                        return state
                self._free_space_check(layer, manifest, 0)
                archive, downloaded = source_archive, False
            else:
                report(LayerProgress(layer, "manifest", f"正在获取{label}的文件清单"))
                manifest, data = self.fetch_manifest(layer, should_pause=should_pause)
                check_path_length(layer, staging, manifest)
                check_path_length(layer, target, manifest)

                # A version unpacked and renamed into place by a process that died before the pointer moved.
                if not force and target.is_dir() and self.pointer(layer) != entry.version:
                    if not self._mismatch(target, manifest, data, full=True, layer=layer, report=report):
                        return self._switch(layer, entry, downloaded=None)

                partial = self.downloads / f"{entry.archive.sha256}.tar.zst"
                have = partial.stat().st_size if partial.is_file() else 0
                if self._offline_object(entry.archive) is None:
                    self._free_space_check(layer, manifest, max(0, entry.archive.size - have))
                else:
                    self._free_space_check(layer, manifest, 0)
                archive, downloaded = self.fetch_archive(layer, report, should_pause)

            remove_tree(staging)
            sizes = {item.path: item.size for item in manifest.files}
            total = manifest.total_size()
            report(LayerProgress(layer, "unpacking", f"正在解压{label}", 0, total))
            unpack(archive, staging, sizes=sizes, should_pause=should_pause, tar=self._tar,
                   report=lambda done: report(LayerProgress(layer, "unpacking", f"正在解压{label}", done, total)))
            problems = self._mismatch(staging, manifest, data, full=full, layer=layer, report=report, exact=True)
            if problems:
                remove_tree(staging)
                raise LayerMismatch(f"{label}解压后与清单不符：" + "；".join(problems[:10]))
            self._retire(target)
            replace_path(staging, target)
            state = self._switch(layer, entry, downloaded=archive if downloaded else None)
            if archive_path is not None:
                self._cleanup_partial_download(entry)
            return state

    def _mismatch(self, root: Path, manifest: lm.LayerManifest, data: bytes, *, full: bool, layer: str,
                  report: ProgressCallback, exact: bool = False) -> list[str]:
        """How the tree at `root` departs from the manifest (`exact`: extra files count too); empty when it matches."""

        label = LABELS.get(layer, layer)
        try:
            carried = (root / lm.MANIFEST_NAME).read_bytes()
        except OSError:
            return [f"no {lm.MANIFEST_NAME}"]
        if carried != data:
            return [f"{lm.MANIFEST_NAME} is not the manifest layers.json names"]
        total = manifest.total_size()
        message = f"正在核对{label}的文件"
        report(LayerProgress(layer, "checking", message, 0, total))
        found = inspect(manifest, root, full=full,
                        report=lambda done: report(LayerProgress(layer, "checking", message, done, total)))
        return found.problems if exact else found.broken

    def _switch(self, layer: str, entry: LayerEntry, *, downloaded: Path | None) -> LayerState:
        self._write_pointer(layer, entry.version)
        self.forget(layer)
        with self._lock:
            self._manifests.pop((layer, entry.version), None)
        if downloaded is not None:
            downloaded.unlink(missing_ok=True)
            try:
                downloaded.with_suffix(downloaded.suffix + ".lock").unlink(missing_ok=True)
            except OSError:
                pass
        try:
            self.remove_unused(layer)
        except OSError:
            pass
        state = self.state(layer, fresh=True)
        return state

    def _retire(self, directory: Path) -> None:
        """Move `directory` aside and delete it; refuse (`LayerBusy`) when its files are open."""

        if not os.path.lexists(directory):
            return
        retired = directory.with_name(directory.name + RETIRED_SUFFIX)
        remove_tree(retired)
        try:
            os.replace(directory, retired)
        except PermissionError as error:
            raise LayerBusy(f"{directory} 正在被使用，结束正在运行的任务后再试") from error
        remove_tree(retired)

    def ensure(self, layers: Iterable[str], *, progress: ProgressCallback | None = None,
               should_pause: PauseCheck | None = None) -> dict[str, LayerState]:
        return {layer: self.install(layer, progress=progress, should_pause=should_pause) for layer in layers}

    def repair(self, layer: str, *, progress: ProgressCallback | None = None,
               should_pause: PauseCheck | None = None) -> LayerState:
        """Check `layer` in full; download and unpack it again when anything is off (extra files included)."""

        state = self.state(layer, full=True, fresh=True)
        if state.ready and not state.extra:
            return state
        return self.install(layer, progress=progress, should_pause=should_pause, full=True, force=True)

    # -- cleaning ----------------------------------------------------------------------------------------------

    def unused(self, layer: str | None = None) -> list[UnusedVersion]:
        """Versions no one points at or wants, and leftovers of interrupted installs, with their sizes."""

        found: list[UnusedVersion] = []
        for name in (layer,) if layer is not None else lm.LAYERS:
            directory = self.paths.layer(name)
            keep = {self.pointer(name)}
            if name in self.index.layers:
                keep.add(self.index.entry(name).version)
            try:
                entries = sorted(directory.iterdir())
            except OSError:
                continue
            for entry in entries:
                if entry.name in keep or entry.name == MIGRATED_NAME or entry.name.startswith("."):
                    continue
                if not entry.is_dir() or lm.is_link(entry):
                    continue
                if entry.name.endswith(UNPACK_SUFFIX) and self._installing(name):
                    continue
                found.append(UnusedVersion(name, entry.name, entry, self._size_of(entry)))
        return found

    def _size_of(self, directory: Path) -> int:
        """What a version directory holds: its manifest's total when it has one, else a walk; remembered."""

        try:
            key = (str(directory), directory.stat().st_mtime_ns)
        except OSError:
            return 0
        with self._lock:
            if key in self._sizes:
                return self._sizes[key]
        try:
            manifest, _ = lm.manifest_in(directory)
            size = manifest.total_size()
        except (OSError, ValueError, lm.LayerError):
            size = _tree_bytes(directory)
        with self._lock:
            self._sizes[key] = size
        return size

    def _installing(self, layer: str) -> bool:
        from nonoka_x.runtime.fsops import try_lock

        return not try_lock(self.paths.layer(layer) / LOCK_NAME)

    def remove_unused(self, layer: str | None = None) -> dict[str, Any]:
        """Delete `unused()` when nothing is using the layers; report what went and what stayed."""

        candidates = self.unused(layer)
        if not candidates:
            return {"removed": [], "kept": [], "freed": 0}
        if self._in_use():
            return {"removed": [], "kept": [item.to_dict() | {"reason": "有未结束的任务"} for item in candidates], "freed": 0}
        removed, kept, freed = [], [], 0
        for item in candidates:
            try:
                self._retire(item.path)
            except (LayerBusy, OSError) as error:
                kept.append(item.to_dict() | {"reason": str(error)})
                continue
            removed.append(item.to_dict())
            freed += item.bytes
        return {"removed": removed, "kept": kept, "freed": freed}

    def _version_dirs(self, layer: str) -> list[Path]:
        """Every version directory of `layer` (current, wanted, old, leftovers); never `migrated` or links."""

        directory = self.paths.layer(layer)
        if not directory.is_dir():
            return []
        return [
            entry for entry in sorted(directory.iterdir())
            if not entry.name.startswith(".") and entry.is_dir() and not lm.is_link(entry) and entry.name != MIGRATED_NAME
        ]

    def versions(self, layer: str) -> list[UnusedVersion]:
        """What removing `layer` as a whole would delete: each version directory with its size (WP-UI)."""

        return [UnusedVersion(layer, entry.name, entry, self._size_of(entry)) for entry in self._version_dirs(layer)]

    def remove_layer(self, layer: str) -> dict[str, Any]:
        """Delete every version of `layer` and its pointer: a layer no engine in use needs (WP-UI's "clean up unused").

        Refused (`LayerBusy`) while an install of the layer holds its lock. Each version is renamed aside
        first, so one whose files are open stays whole and is reported in `kept`; the pointer goes only
        when every version did.
        """

        if layer not in lm.LAYERS:
            raise ValueError(f"no layer {layer!r}")
        if self._installing(layer):
            raise LayerBusy(f"{LABELS.get(layer, layer)}层正在安装，等安装结束后再清理")
        removed, kept, freed = [], [], 0
        for item in self.versions(layer):
            try:
                self._retire(item.path)
            except (LayerBusy, OSError) as error:
                kept.append(item.to_dict() | {"reason": str(error)})
                continue
            removed.append(item.to_dict())
            freed += item.bytes
        if not kept:
            self.paths.layer_pointer(layer).unlink(missing_ok=True)
        self.forget(layer)
        with self._lock:
            for key in [key for key in self._manifests if key[0] == layer]:
                del self._manifests[key]
        return {"removed": removed, "kept": kept, "freed": freed}

    def remove_all(self) -> list[str]:
        """Delete every version of every layer and the pointers; what could not go, one line each.

        Each version is renamed aside first, so one whose files are open stays whole (and reported).
        """

        failures: list[str] = []
        for layer in lm.LAYERS:
            for entry in self._version_dirs(layer):
                try:
                    self._retire(entry)
                except (LayerBusy, OSError) as error:
                    failures.append(f"{layer}/{entry.name}（{error}）")
            if not any(item.startswith(f"{layer}/") for item in failures):
                self.paths.layer_pointer(layer).unlink(missing_ok=True)
        self.forget()
        with self._lock:
            self._manifests.clear()
        return failures

    def overview(self) -> dict[str, Any]:
        """For the runtime page and WP-UI's "clean up unused": every layer's state and size, and what is unused."""

        layers = []
        for layer, entry in self.index.layers.items():
            state = self.state(layer)
            layers.append(state.to_dict() | {
                "label": LABELS.get(layer, layer),
                "archive_bytes": entry.archive.size,
                "bytes": self._layer_bytes(layer, state),
            })
        unused = self.unused()
        return {
            "layers": layers,
            "unused": [item.to_dict() for item in unused],
            "unused_bytes": sum(item.bytes for item in unused),
        }

    def _layer_bytes(self, layer: str, state: LayerState) -> int:
        return 0 if state.path is None else self._size_of(state.path)

    # -- migration: completing moved models --------------------------------------------------------------------

    def complete_models(
        self,
        directory: Path,
        *,
        progress: ProgressCallback | None = None,
        should_pause: PauseCheck | None = None,
        log: Callable[[str], None] = lambda _message: None,
    ) -> CompletionReport:
        """Check moved models against the model layer, fetch only what is missing or wrong, register it.

        Every file is hashed. A file the manifest lists and that matches is kept (its modification time
        set to the layer's, which the separator stamps state); a marker or stamp whose bytes the
        manifest pins and Nonoka's own manifests determine is written afresh when it is the only thing
        off; whatever is still missing or wrong is unpacked from the model layer archive (offline
        package first), and only those members. Files the manifest does not list are removed. When the
        tree then matches the manifest exactly it is renamed to the version and `current.json` points
        at it. Safe to run again after an interruption.
        """

        layer = "models"
        report = progress if progress is not None else (lambda _value: None)
        entry = self.index.entry(layer)
        target = self.paths.layer_version(layer, entry.version)
        directory = Path(directory)
        with holding_lock(self.paths.layer(layer) / LOCK_NAME, should_pause=should_pause,
                          on_pause=lambda: DownloadPaused("Completion paused")):
            if not directory.is_dir():
                if self.state(layer, fresh=True).ready:
                    return CompletionReport(entry.version, 0, (), (), (), 0, "")
                raise lm.LayerError(f"没有要核对的模型目录：{directory}")
            manifest, data = self.fetch_manifest(layer, should_pause=should_pause)
            check_path_length(layer, target, manifest)
            check_path_length(layer, directory, manifest)
            if os.path.lexists(target) and target.resolve() != directory.resolve():
                raise lm.LayerError(f"模型层的目标位置已经存在：{target}")
            self._unlink_file_links(directory, manifest, log)
            total = manifest.total_size()
            report(LayerProgress(layer, "checking", "正在核对搬过来的模型", 0, total))
            found = inspect(manifest, directory, full=True,
                            report=lambda done: report(LayerProgress(layer, "checking", "正在核对搬过来的模型", done, total)))
            removed = self._remove_extra(directory, found, log)
            wrong = found.wrong
            rebuilt = self._rebuild_markers(directory, manifest, wrong)
            wrong = [path for path in wrong if path not in rebuilt]
            downloaded, source, fetched = 0, "", tuple(wrong)
            if wrong:
                log(f"model layer: {len(wrong)} file(s) to fetch: {', '.join(wrong[:10])}")
                self._free_space_check(layer, lm.LayerManifest(layer, [item for item in manifest.files if item.path in wrong]),
                                       0 if self._offline_object(entry.archive) else entry.archive.size)
                archive, was_downloaded = self.fetch_archive(layer, report, should_pause)
                source = "network" if was_downloaded else "offline"
                downloaded = entry.archive.size if was_downloaded else 0
                staging = directory.with_name(directory.name + ".complete")
                remove_tree(staging)
                sizes = {item.path: item.size for item in manifest.files}
                needed = sum(sizes[path] for path in wrong)
                unpack(archive, staging, members=wrong, sizes=sizes, should_pause=should_pause, tar=self._tar,
                       report=lambda done: report(LayerProgress(layer, "unpacking", "正在解出缺少的模型文件", done, needed)))
                expected = manifest.by_path()
                for path in wrong:
                    source_file = staging / path
                    if not source_file.is_file() or lm.sha256_file(source_file) != expected[path].sha256:
                        raise LayerMismatch(f"模型层压缩包里的 {path} 与清单不符")
                    destination = directory / path
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    if os.path.lexists(destination) and not destination.is_file():
                        remove_tree(destination)
                    replace_path(source_file, destination)
                remove_tree(staging)
                if was_downloaded:
                    archive.unlink(missing_ok=True)
            (directory / lm.MANIFEST_NAME).write_bytes(data)
            os.utime(directory / lm.MANIFEST_NAME, (lm.FIXED_MTIME, lm.FIXED_MTIME))
            _set_fixed_times(directory, manifest)
            # Every kept file was hashed above, every fetched or rebuilt one on its way in.
            final = inspect(manifest, directory, full=False)
            if final.problems:
                raise LayerMismatch("补齐后的模型目录仍与清单不符：" + "；".join(final.problems[:10]))
            if directory.resolve() != target.resolve():
                replace_path(directory, target)
            self._write_pointer(layer, entry.version)
            self.forget(layer)
            kept = len(manifest.files) - len(fetched) - len(rebuilt)
            return CompletionReport(entry.version, kept, fetched, tuple(rebuilt), tuple(removed), downloaded, source)

    def _unlink_file_links(self, directory: Path, manifest: lm.LayerManifest, log: Callable[[str], None]) -> None:
        """A Hugging Face snapshot entry that links to its blob becomes a copy of the blob (when the blob is inside)."""

        root = directory.resolve()
        for item in manifest.files:
            path = directory / item.path
            if not lm.is_link(path):
                continue
            try:
                target = path.resolve(strict=True)
                target.relative_to(root)
            except (OSError, ValueError):
                path.unlink(missing_ok=True)
                continue
            if not target.is_file():
                path.unlink(missing_ok=True)
                continue
            temporary = path.with_name(path.name + ".copy")
            shutil.copyfile(target, temporary)
            path.unlink()
            os.replace(temporary, path)
            log(f"model layer: {item.path} was a link; replaced by a copy of what it pointed at")

    @staticmethod
    def _remove_extra(directory: Path, found: Inspection, log: Callable[[str], None]) -> list[str]:
        removed: list[str] = []
        for path in found.extra:
            if path == lm.MANIFEST_NAME:
                continue
            (directory / path).unlink(missing_ok=True)
            removed.append(path)
        for path in found.links:
            target = directory / path
            if lm.is_link(target):
                try:
                    os.unlink(target)
                except OSError:
                    os.rmdir(target)
        # Directories emptied by the removal, deepest first.
        for current, subdirectories, files in sorted(os.walk(directory), key=lambda item: -len(item[0])):
            path = Path(current)
            if path != directory and not any(path.iterdir()):
                path.rmdir()
        if removed:
            log(f"model layer: removed {len(removed)} file(s) the manifest does not list")
        return removed

    @staticmethod
    def _rebuild_markers(directory: Path, manifest: lm.LayerManifest, wrong: list[str]) -> list[str]:
        """Write the engine's markers whose content the manifest pins, where that is all that is off.

        A separator `.verified` stamp states its file's sha256, size and modification time, and the
        layer's carry `FIXED_MTIME`; an engine that verified the moved file stamped the time it found.
        A Hugging Face `.finesub-verified.json` states the repo, revision and a digest of the model's
        manifest entry. Both are derived the way the build derives them (`scripts/build_layers.py`)
        and written only if their sha256 is the manifest's; anything else is fetched.
        """

        expected = manifest.by_path()
        rebuilt: list[str] = []
        sources = {str(item.get("directory")): item for item in manifest.sources.get("models") or ()}
        for path in wrong:
            content = _marker_content(path, expected, sources)
            if content is None or lm.digest_of(content) != expected[path].sha256 or len(content) != expected[path].size:
                continue
            target = directory / path
            target.parent.mkdir(parents=True, exist_ok=True)
            if os.path.lexists(target) and not target.is_file():
                continue
            temporary = target.with_name(target.name + ".tmp")
            temporary.write_bytes(content)
            os.replace(temporary, target)
            rebuilt.append(path)
        return rebuilt


def _marker_content(path: str, expected: Mapping[str, lm.LayerFile], sources: Mapping[str, Any]) -> bytes | None:
    pure = PurePosixPath(path)
    if pure.name.endswith(lm.VERIFIED_SUFFIX) and pure.parent.as_posix() == lm.SEPARATOR_DIR:
        stamped = pure.parent / pure.name[: -len(lm.VERIFIED_SUFFIX)]
        item = expected.get(stamped.as_posix())
        if item is None:
            return None
        stamp = {"sha256": item.sha256, "size": item.size, "mtime_ns": lm.FIXED_MTIME * 10**9}
        return json.dumps(stamp).encode("utf-8")
    if pure.name == lm.HF_MARKER_NAME:
        source = sources.get(pure.parent.as_posix())
        if source is None or not source.get("repo"):
            return None
        from nonoka_x.runtime import manifests

        spec = next((entry for entry in manifests.model_manifest().get("models", {}).values()
                     if entry.get("repo") == source.get("repo") and entry.get("revision") == source.get("revision")), None)
        if spec is None:
            return None
        marker = {"manifest": _engine_entry_digest(spec), "repo": spec["repo"], "revision": spec["revision"]}
        return json.dumps(marker, ensure_ascii=False).encode("utf-8")
    return None


def _engine_entry_digest(spec: Mapping[str, Any]) -> str:
    """`finesub_bootstrap.hf_verify.entry_digest` of a model-manifest entry (as `build_layers.engine_entry_digest`)."""

    import hashlib

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


def open_manager(paths: NonokaPaths, index_path: Path | None = None, **options: Any) -> LayerManager:
    return LayerManager(paths, load_index(index_path), **options)


__all__ = [
    "INDEX_FILE",
    "MAX_PATH_CHARS",
    "PATH_TOO_DEEP_MESSAGE",
    "CompletionReport",
    "Inspection",
    "LayerBusy",
    "LayerEntry",
    "LayerIndex",
    "LayerIndexError",
    "LayerManager",
    "LayerMismatch",
    "LayerProgress",
    "LayerState",
    "ObjectRef",
    "PathTooDeep",
    "UnusedVersion",
    "check_path_length",
    "inspect",
    "load_index",
    "longest_path",
    "offline_pack",
    "open_manager",
    "parse_index",
    "read_embedded_manifest",
    "unpack",
]
