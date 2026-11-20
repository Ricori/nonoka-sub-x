"""The file manifest of a prebuilt environment layer (docs/transcription-engines-plan.md 4.2 items 14, 17, 19).

One format, three readers: the build pipeline writes it (`scripts/build_layers.py`, WP-E1), the quality
gate checks it against `RUNTIME_PINS.json` (`tests/quality_harness/layers.py`), and the layer manager
checks an installed layer against it on the user's machine (WP-E3).

A layer is a directory tree. Its manifest lists every regular file in it -- relative POSIX path, size,
sha256 -- plus the directories that hold nothing, the files that carry a build-machine path
(`exceptions`), and where the content came from (`sources`: the pinned inputs, never a build-machine
path or a time). The manifest is stored twice: inside the layer as `MANIFEST_NAME` at the root (not
listed in its own `files`), and beside the archive. Its bytes are canonical (`render`), so the same
inputs give the same bytes, and the layer's version is the first 16 hex digits of their sha256
(`version_of`, owner's choice 2026-11-12; `digest_of` is the full digest).

Not recorded: modification times. The build sets every file's to `FIXED_MTIME` and the separator's
`.verified` stamps state it; whoever unpacks a layer keeps the times the archive carries (tar does).

Symbolic links, junctions and other reparse points are refused everywhere: a layer holds only regular
files (4.2 item 17).
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import stat
import sys
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

SCHEMA = 1

#: The manifest's name inside the layer's own root directory.
MANIFEST_NAME = ".nonoka-layer.json"

#: The four layers (4.2 item 14, plus the optional local-MOSS model layer). Windows only for now.
LAYERS = ("python", "models", "models-moss", "tools")
#: The three layers every `layers.json` since v0.7 carries; `models-moss` may be added once built.
REQUIRED_INDEX_LAYERS = ("python", "models", "tools")
PLATFORM = "windows-x64"


def current_platform(platform_name: str | None = None, machine: str | None = None) -> str:
    """The layer platform id for the running host (`windows-x64`, `macos-arm64`, `macos-amd64`, ...)."""

    target = sys.platform if platform_name is None else platform_name
    if target == "win32":
        return "windows-x64"
    if target == "darwin":
        arch = (platform.machine() if machine is None else machine).lower()
        if arch in {"arm64", "aarch64"}:
            return "macos-arm64"
        if arch in {"x86_64", "amd64"}:
            return "macos-amd64"
    return target


def platform_supported(platform_name: str | None = None, machine: str | None = None) -> bool:
    """Whether prebuilt environment layers (`PLATFORM`) are built for this host."""

    return current_platform(platform_name, machine) == PLATFORM

#: Every file and directory of a built layer carries this modification time: 2026-01-01T00:00:00Z.
FIXED_MTIME = 1767225600

#: Model-manifest ids the FineSub model layer holds: the ones FineSub's default path loads (owner 2026-11-12,
#: plan appendix C item 6). separator, whisper and qwen-referee on every run; nemotron3 and
#: speaker-embedding whenever speaker diarization is not `off`.
MODEL_LAYER_IDS = ("separator", "whisper", "qwen-referee", "nemotron3", "speaker-embedding")

#: Model-manifest ids the local MOSS model layer (`models-moss`) holds: `moss-transcribe-diarize` plus
#: `speaker-embedding` trimmed to the ~26 MB CPU WeSpeaker + PLDA files `relink` opens.
MOSS_MODEL_LAYER_IDS = ("moss-transcribe-diarize", "speaker-embedding")
MOSS_SPEAKER_EMBEDDING_FILES = ("wespeaker-cpu.ts", "plda/plda.npz", "plda/xvec_transform.npz")
MODEL_LAYERS: dict[str, tuple[str, ...]] = {
    "models": MODEL_LAYER_IDS,
    "models-moss": MOSS_MODEL_LAYER_IDS,
}


def layer_model_spec(layer: str, model_id: str, spec: dict[str, Any]) -> dict[str, Any]:
    """The model-manifest entry as `layer` builds and verifies it (`models-moss` trims `speaker-embedding`)."""

    copied = dict(spec)
    files = [dict(item) for item in spec.get("files", [])]
    if layer == "models-moss" and model_id == "speaker-embedding":
        copied["listedOnly"] = True
        files = [item for item in files if item.get("name") in MOSS_SPEAKER_EMBEDDING_FILES]
    copied["files"] = files
    return copied

#: Tool-manifest ids the tool layer leaves out: uv only builds the Python layer (4.2 item 14).
TOOLS_LEFT_OUT = ("uv",)

#: Inside the model layer: the directories `finesub_bootstrap.model_caches.managed_model_dirs` names
#: under a models root, the Hugging Face hub under the first, and the two marker kinds the engine reads.
HF_HOME_DIR = "huggingface"
SEPARATOR_DIR = "audio-separator"
HF_MARKER_NAME = ".finesub-verified.json"
VERIFIED_SUFFIX = ".verified"

#: Separator files the engine's own manifest (`finesub_bootstrap/model-manifest.json`) leaves unpinned:
#: it never verifies them and reads no stamp for them, so the layer carries none. Nonoka's manifest pins
#: them (owner 2026-11-12); `tests/quality_harness/pins.BOOTSTRAP_MOVING` is the same list.
ENGINE_UNVERIFIED = ("download_checks.json",)


class LayerError(RuntimeError):
    """A layer or manifest that cannot be what it claims."""


@dataclass(frozen=True)
class LayerFile:
    path: str
    size: int
    sha256: str

    def to_dict(self) -> dict[str, Any]:
        return {"path": self.path, "size": self.size, "sha256": self.sha256}


@dataclass
class LayerManifest:
    layer: str
    files: list[LayerFile]
    platform: str = PLATFORM
    empty_dirs: list[str] = field(default_factory=list)
    #: `{"path", "reason"}`: files that hold a build-machine path (4.2 item 17), listed, not hidden.
    exceptions: list[dict[str, str]] = field(default_factory=list)
    sources: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA,
            "layer": self.layer,
            "platform": self.platform,
            "sources": self.sources,
            "exceptions": sorted(self.exceptions, key=lambda item: (item["path"], item["reason"])),
            "empty_dirs": sorted(self.empty_dirs),
            "files": [item.to_dict() for item in sorted(self.files, key=lambda item: item.path)],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "LayerManifest":
        if data.get("schema") != SCHEMA:
            raise LayerError(f"unknown layer manifest schema: {data.get('schema')!r}")
        if data.get("layer") not in LAYERS:
            raise LayerError(f"unknown layer: {data.get('layer')!r}")
        files = [LayerFile(check_path(item["path"]), int(item["size"]), str(item["sha256"]))
                 for item in data.get("files", [])]
        return cls(
            layer=data["layer"],
            platform=data.get("platform", ""),
            files=files,
            empty_dirs=[check_path(item) for item in data.get("empty_dirs", [])],
            exceptions=[{"path": check_path(item["path"]), "reason": str(item["reason"])}
                        for item in data.get("exceptions", [])],
            sources=data.get("sources", {}),
        )

    def by_path(self) -> dict[str, LayerFile]:
        return {item.path: item for item in self.files}

    def total_size(self) -> int:
        return sum(item.size for item in self.files)


# --- bytes ----------------------------------------------------------------------------------------


def render(manifest: LayerManifest) -> bytes:
    """The canonical bytes: sorted keys, one-space indent, LF, a final newline."""

    return (json.dumps(manifest.to_dict(), ensure_ascii=False, sort_keys=True, indent=1) + "\n").encode("utf-8")


def parse(data: bytes) -> LayerManifest:
    return LayerManifest.from_dict(json.loads(data.decode("utf-8")))


def read(path: Path) -> tuple[LayerManifest, bytes]:
    data = Path(path).read_bytes()
    return parse(data), data


def digest_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def version_of(data: bytes) -> str:
    """A layer's version: the first 16 hex digits of its manifest's sha256 (4.2 item 19)."""

    return digest_of(data)[:16]


# --- paths ----------------------------------------------------------------------------------------


def check_path(value: str) -> str:
    """A manifest path: relative, POSIX, no `.`/`..`/empty parts, no drive."""

    text = str(value)
    pure = PurePosixPath(text)
    if (not text or "\\" in text or pure.is_absolute() or ":" in pure.parts[0]
            or any(part in ("", ".", "..") for part in text.split("/"))):
        raise LayerError(f"not a layer-relative path: {value!r}")
    return text


def relative(root: Path, path: Path) -> str:
    return check_path(path.relative_to(root).as_posix())


def is_link(path: Path) -> bool:
    """A symbolic link, a junction or any other reparse point (Windows), never followed."""

    try:
        status = os.lstat(path)
    except OSError:
        return False
    if stat.S_ISLNK(status.st_mode):
        return True
    attributes = getattr(status, "st_file_attributes", 0)
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        for chunk in iter(lambda: source.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def walk(root: Path) -> tuple[list[Path], list[Path], list[Path]]:
    """Every regular file, every empty directory and every link under `root` (links not followed).

    `MANIFEST_NAME` at the root is not a file of the layer. Anything that is neither a regular file
    nor a directory is reported as a link.
    """

    root = Path(root)
    files: list[Path] = []
    empty: list[Path] = []
    links: list[Path] = []
    pending = [root]
    while pending:
        directory = pending.pop()
        with os.scandir(directory) as entries:
            children = list(entries)
        if not children and directory != root:
            empty.append(Path(directory))
        for entry in children:
            path = Path(entry.path)
            if is_link(path):
                links.append(path)
            elif entry.is_dir(follow_symlinks=False):
                pending.append(path)
            elif entry.is_file(follow_symlinks=False):
                if directory == root and entry.name == MANIFEST_NAME:
                    continue
                files.append(path)
            else:
                links.append(path)
    return sorted(files), sorted(empty), sorted(links)


def scan(root: Path) -> tuple[list[LayerFile], list[str]]:
    """The files (hashed) and empty directories of a layer tree; links are an error."""

    root = Path(root)
    files, empty, links = walk(root)
    if links:
        raise LayerError("links in a layer: " + ", ".join(relative(root, path) for path in links[:10]))
    listed = [LayerFile(relative(root, path), path.stat().st_size, sha256_file(path)) for path in files]
    return listed, [relative(root, path) for path in empty]


# --- checking a tree against its manifest -------------------------------------------------------------


def compare(manifest: LayerManifest, root: Path, *, full: bool) -> list[str]:
    """How the tree at `root` departs from `manifest`; empty when it matches.

    `full=False` is the quick check (4.2 item 19): the file list and sizes. `full=True` also hashes
    every file. Extra files, missing files, links and empty directories that come or go all count.
    """

    root = Path(root)
    problems: list[str] = []
    files, empty, links = walk(root)
    problems += [f"link (layers hold only regular files): {relative(root, path)}" for path in links]
    expected = manifest.by_path()
    present = {relative(root, path): path for path in files}
    problems += [f"missing: {path}" for path in sorted(expected.keys() - present.keys())]
    problems += [f"not in the manifest: {path}" for path in sorted(present.keys() - expected.keys())]
    for name in sorted(expected.keys() & present.keys()):
        item, path = expected[name], present[name]
        size = path.stat().st_size
        if size != item.size:
            problems.append(f"size differs: {name} ({size}, manifest {item.size})")
        elif full and sha256_file(path) != item.sha256:
            problems.append(f"sha256 differs: {name}")
    empty_now = {relative(root, path) for path in empty}
    problems += [f"empty directory not in the manifest: {path}" for path in sorted(empty_now - set(manifest.empty_dirs))]
    problems += [f"empty directory missing: {path}" for path in sorted(set(manifest.empty_dirs) - empty_now)
                 if not (root / path).is_dir()]
    return problems


def manifest_in(root: Path) -> tuple[LayerManifest, bytes]:
    """The manifest a layer directory carries at its root."""

    path = Path(root) / MANIFEST_NAME
    if not path.is_file():
        raise LayerError(f"no {MANIFEST_NAME} in {root}")
    return read(path)


def differences(first: LayerManifest, second: LayerManifest) -> list[str]:
    """Where two manifests of the same layer differ (two builds, plan WP-E1)."""

    found: list[str] = []
    left, right = first.to_dict(), second.to_dict()
    for key in sorted(left.keys() | right.keys()):
        if key != "files" and left.get(key) != right.get(key):
            found.append(f"{key} differs")
    a, b = first.by_path(), second.by_path()
    found += [f"only in the first: {path}" for path in sorted(a.keys() - b.keys())]
    found += [f"only in the second: {path}" for path in sorted(b.keys() - a.keys())]
    found += [f"content differs: {path}" for path in sorted(a.keys() & b.keys()) if a[path] != b[path]]
    return found


def iter_paths(manifest: LayerManifest) -> Iterable[str]:
    """Everything an archive of the layer holds, in archive order: the manifest, files, empty dirs."""

    yield MANIFEST_NAME
    yield from sorted([item.path for item in manifest.files] + list(manifest.empty_dirs))


__all__ = [
    "ENGINE_UNVERIFIED",
    "FIXED_MTIME",
    "HF_HOME_DIR",
    "HF_MARKER_NAME",
    "LAYERS",
    "MANIFEST_NAME",
    "MODEL_LAYERS",
    "MODEL_LAYER_IDS",
    "MOSS_MODEL_LAYER_IDS",
    "MOSS_SPEAKER_EMBEDDING_FILES",
    "PLATFORM",
    "REQUIRED_INDEX_LAYERS",
    "SCHEMA",
    "SEPARATOR_DIR",
    "TOOLS_LEFT_OUT",
    "VERIFIED_SUFFIX",
    "LayerError",
    "LayerFile",
    "LayerManifest",
    "check_path",
    "compare",
    "current_platform",
    "differences",
    "digest_of",
    "is_link",
    "iter_paths",
    "layer_model_spec",
    "manifest_in",
    "parse",
    "platform_supported",
    "read",
    "relative",
    "render",
    "scan",
    "sha256_file",
    "version_of",
    "walk",
]
