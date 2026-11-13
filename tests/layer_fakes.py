"""Small made-up layers, a `layers.json` for them and a local origin, for the layer manager's tests (WP-E3).

Each fake layer is built the way `scripts/build_layers.py` builds a real one: files with `FIXED_MTIME`,
the canonical manifest at the root and beside the archive, a `tar.zst` made by Windows' `tar.exe`
from the manifest's own path list. The origin serves them under `v1/<layer>/<sha256>.<ext>` and honours
`Range`; it can cut a response short to stand in for a dropped connection.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import random
import subprocess
import sys
import threading
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from nonoka_x.runtime import layer_manifest as lm  # noqa: E402

SYSTEM_TAR = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "tar.exe"

SEPARATOR_FILE = "audio-separator/model.ckpt"
HF_DIRECTORY = "huggingface/hub/models--fake--whisper"
HF_REVISION = "0123456789abcdef0123456789abcdef01234567"
HF_SNAPSHOT = f"{HF_DIRECTORY}/snapshots/{HF_REVISION}"


def noise(size: int, seed: int) -> bytes:
    return random.Random(seed).randbytes(size)


def stamp(data: bytes) -> bytes:
    """A separator `.verified` stamp as the layer carries it (`build_layers.Build.fixed_model`)."""

    return json.dumps({"sha256": hashlib.sha256(data).hexdigest(), "size": len(data),
                       "mtime_ns": lm.FIXED_MTIME * 10**9}).encode("utf-8")


def default_files(layer: str, *, big: int = 0) -> dict[str, bytes]:
    """What each fake layer holds; `big` adds an incompressible file of that size (for interrupted downloads)."""

    if layer == "python":
        files = {"python.exe": b"MZ fake interpreter", "Lib/site-packages/librosa/core/spectrum.py": b"# librosa\n",
                 "Lib/site-packages/torch/include/a/deep/header.h": b"#pragma once\n"}
    elif layer == "models":
        checkpoint = noise(4096, 1)
        files = {SEPARATOR_FILE: checkpoint, f"{SEPARATOR_FILE}.verified": stamp(checkpoint),
                 f"{HF_DIRECTORY}/.finesub-verified.json": b'{"manifest": "x", "repo": "fake/whisper"}',
                 f"{HF_SNAPSHOT}/model.bin": noise(8192, 2), f"{HF_SNAPSHOT}/config.json": b"{}"}
    else:
        files = {"ffmpeg/ffmpeg-n9/bin/ffmpeg.exe": b"MZ ffmpeg", "ffmpeg/ffmpeg-n9/bin/ffprobe.exe": b"MZ ffprobe",
                 "tokcount/tokcount.exe": b"MZ tokcount", "yt-dlp/yt_dlp/__init__.py": b"# yt-dlp\n"}
    if big:
        files["payload/big.bin"] = noise(big, 7)
    return files


def sources(layer: str) -> dict[str, Any]:
    if layer == "models":
        return {"models": [
            {"id": "separator", "directory": "audio-separator", "files": []},
            {"id": "whisper", "directory": HF_DIRECTORY, "repo": "fake/whisper", "revision": HF_REVISION, "files": []},
        ]}
    if layer == "tools":
        return {"tools": [
            {"id": "ffmpeg", "directory": "ffmpeg", "version": "n9",
             "required_files": ["ffmpeg-n9/bin/ffmpeg.exe", "ffmpeg-n9/bin/ffprobe.exe"]},
            {"id": "tokcount", "directory": "tokcount", "version": "1", "required_files": ["tokcount.exe"]},
            {"id": "yt-dlp", "directory": "yt-dlp", "version": "1", "required_files": ["yt_dlp/__init__.py"]},
        ]}
    return {"interpreter": {"build": "fake"}}


@dataclass
class FakeLayer:
    layer: str
    root: Path
    manifest: lm.LayerManifest
    data: bytes
    archive: Path

    @property
    def version(self) -> str:
        return lm.version_of(self.data)

    @property
    def manifest_sha256(self) -> str:
        return lm.digest_of(self.data)

    @property
    def archive_sha256(self) -> str:
        return lm.sha256_file(self.archive)

    def object_paths(self) -> tuple[str, str]:
        return (f"v1/{self.layer}/{self.manifest_sha256}.json", f"v1/{self.layer}/{self.archive_sha256}.tar.zst")

    def entry(self) -> dict[str, Any]:
        manifest_path, archive_path = self.object_paths()
        return {
            "version": self.version,
            "manifest": {"path": manifest_path, "size": len(self.data), "sha256": self.manifest_sha256},
            "archive": {"path": archive_path, "size": self.archive.stat().st_size, "sha256": self.archive_sha256},
        }


def build_layer(work: Path, layer: str, files: dict[str, bytes] | None = None, *,
                archive_files: dict[str, bytes] | None = None, big: int = 0, tag: str = "") -> FakeLayer:
    """One fake layer under `work`. `archive_files`, when given, is what the archive really holds."""

    files = default_files(layer, big=big) if files is None else files
    if tag:
        files = {**files, "tag.txt": tag.encode("utf-8")}
    root = work / f"{layer}-{tag or 'tree'}"
    _write_tree(root, files)
    listed, empty = lm.scan(root)
    manifest = lm.LayerManifest(layer=layer, files=listed, empty_dirs=empty, sources=sources(layer))
    data = lm.render(manifest)
    (root / lm.MANIFEST_NAME).write_bytes(data)
    os.utime(root / lm.MANIFEST_NAME, (lm.FIXED_MTIME, lm.FIXED_MTIME))
    packed = root
    if archive_files is not None:
        packed = work / f"{layer}-{tag or 'tree'}-packed"
        _write_tree(packed, {**archive_files})
        (packed / lm.MANIFEST_NAME).write_bytes(data)
    archive = work / f"{layer}-{tag or 'tree'}.tar.zst"
    names = [lm.MANIFEST_NAME, *sorted({*archive_files} if archive_files is not None else (item.path for item in listed))]
    listing = work / f"{layer}-{tag or 'tree'}.files.txt"
    listing.write_text("\n".join(names) + "\n", encoding="utf-8")
    subprocess.run([str(SYSTEM_TAR), "--zstd", "--no-recursion", "-c", "-f", str(archive), "-C", str(packed),
                    "-T", str(listing)], check=True, capture_output=True)
    return FakeLayer(layer, root, manifest, data, archive)


def _write_tree(root: Path, files: dict[str, bytes]) -> None:
    for name, data in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        os.utime(path, (lm.FIXED_MTIME, lm.FIXED_MTIME))


def index_bytes(layers: dict[str, FakeLayer], base_url: str, *, overrides: dict[str, dict[str, Any]] | None = None) -> bytes:
    body = {
        "schema": 1,
        "platform": "windows-x64",
        "baseUrl": base_url,
        "layers": {name: {**item.entry(), **((overrides or {}).get(name) or {})} for name, item in layers.items()},
        "files": {"layers": [], "path": "v1/files/{sha256}"},
    }
    return json.dumps(body, indent=1).encode("utf-8")


def publish(layers: dict[str, FakeLayer], into: Path) -> Path:
    """The objects of `layers` under their index paths in `into` (an origin's root or an offline package)."""

    for item in layers.values():
        manifest_path, archive_path = item.object_paths()
        (into / manifest_path).parent.mkdir(parents=True, exist_ok=True)
        (into / manifest_path).write_bytes(item.data)
        (into / archive_path).write_bytes(item.archive.read_bytes())
    return into


@dataclass
class Origin:
    """A local HTTP origin serving a directory, with `Range`; `cut_after` drops the next archive response early."""

    root: Path
    requests: list[tuple[str, str | None]] = field(default_factory=list)
    cut_after: int | None = None
    fail_with: int | None = None

    def __post_init__(self) -> None:
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                wanted = self.headers.get("Range")
                outer.requests.append((self.path, wanted))
                if outer.fail_with is not None:
                    self.send_error(outer.fail_with)
                    return
                path = outer.root / self.path.lstrip("/")
                if not path.is_file():
                    self.send_error(404)
                    return
                payload = path.read_bytes()
                body = payload
                if wanted:
                    start = int(wanted.removeprefix("bytes=").split("-")[0])
                    body = payload[start:]
                    self.send_response(206)
                    self.send_header("Content-Range", f"bytes {start}-{len(payload) - 1}/{len(payload)}")
                else:
                    self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                cut = None
                if self.path.endswith(".tar.zst"):
                    cut, outer.cut_after = outer.cut_after, None
                if cut is not None and cut < len(body):
                    self.wfile.write(body[:cut])
                    self.wfile.flush()
                    self.close_connection = True
                    return
                self.wfile.write(body)

            def log_message(self, *args: Any) -> None:
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self._server.server_port}/"

    def archive_requests(self) -> list[tuple[str, str | None]]:
        return [item for item in self.requests if item[0].endswith(".tar.zst")]

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
