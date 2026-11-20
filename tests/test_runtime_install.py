"""`nonoka_x.runtime_install`: install targets in layers, the VC++ check, migration's layer-download step (WP-E3).

There is no venv, no uv run and no wheel download to test any more; the layer installs themselves are
`tests/test_layers.py`'s.
"""

from __future__ import annotations

import ast
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from nonoka_x import runtime_install  # noqa: E402
from nonoka_x.runtime import layer_manifest as lm  # noqa: E402

SOURCES = ("provision.py", "runtime_install.py", "model_install.py", "model_specs.py", "msvc_runtime.py",
           "runtime/__init__.py", "runtime/layers.py")


def test_every_target_maps_onto_known_layers_in_install_order():
    for target, layers in runtime_install.TARGET_LAYERS.items():
        assert layers and set(layers) <= set(lm.LAYERS), target
        assert runtime_install.ordered(layers) == layers, target
    assert runtime_install.layers_for("all") == ("tools", "python", "models")
    # Every tool is in the one tool layer.
    assert {runtime_install.layers_for(tool) for tool in ("yt-dlp", "tokcount", "node", "video-tools", "media")} == {("tools",)}


def test_the_vc_runtime_is_checked_only_for_the_python_layer():
    asked: list[str] = []

    def problem(*, platform):
        asked.append(platform)
        return "缺少 VC++ 运行库"

    assert runtime_install.vc_runtime_problem(("tools", "models"), problem=problem) is None
    assert asked == []
    assert runtime_install.vc_runtime_problem(("tools", "python"), problem=problem, platform="win32") == "缺少 VC++ 运行库"
    assert asked == ["win32"]


def test_the_vc_runtime_check_uses_the_msvc_module_words(monkeypatch):
    monkeypatch.setattr(runtime_install, "msvc_runtime_problem", lambda *, platform: f"checked on {platform}")
    assert runtime_install.vc_runtime_problem(["python"], platform="win32") == "checked on win32"


class _Context(SimpleNamespace):
    def __init__(self) -> None:
        super().__init__(paths=None, migrated_models=None, lines=[])

    def log(self, message: str) -> None:
        self.lines.append(message)


def test_layer_download_installs_the_python_and_tool_layers(monkeypatch):
    installed: list[str] = []
    manager = SimpleNamespace(install=lambda layer: installed.append(layer) or SimpleNamespace(state="ready", wanted="v"))
    monkeypatch.setattr(runtime_install, "msvc_runtime_problem", lambda *, platform: None)
    context = _Context()

    runtime_install.LayerDownloadHook(lambda _context: manager).run(context)

    assert installed == ["tools", "python"]
    assert any("python ready" in line for line in context.lines)


def test_layer_download_stops_before_downloading_without_a_vc_runtime(monkeypatch):
    manager = SimpleNamespace(install=lambda layer: pytest.fail("nothing may be downloaded"))
    monkeypatch.setattr(runtime_install, "msvc_runtime_problem", lambda *, platform: "缺少 VC++ 运行库")

    with pytest.raises(RuntimeError, match="VC"):
        runtime_install.LayerDownloadHook(lambda _context: manager).run(_Context())


def test_layer_download_says_how_much_it_fetches_without_touching_anything(monkeypatch):
    from nonoka_x.runtime import layers as L

    entry = lambda size: SimpleNamespace(archive=SimpleNamespace(size=size))  # noqa: E731
    index = SimpleNamespace(layers={"python": 1, "tools": 1, "models": 1},
                            entry=lambda layer: entry({"python": 100, "tools": 10, "models": 1000}[layer]))
    monkeypatch.setattr(L, "load_index", lambda: index)
    assert runtime_install.LayerDownloadHook().download_bytes(_Context()) == 110

    monkeypatch.setattr(L, "load_index", lambda: (_ for _ in ()).throw(L.LayerIndexError("none")))
    assert runtime_install.LayerDownloadHook().download_bytes(_Context()) is None


def test_layer_download_skips_on_unsupported_platforms(monkeypatch):
    from nonoka_x.runtime import layers as L

    monkeypatch.setattr(sys, "platform", "linux")
    entry = lambda size: SimpleNamespace(archive=SimpleNamespace(size=size))  # noqa: E731
    index = SimpleNamespace(
        platform=lm.PLATFORM,
        layers={"python": 1, "tools": 1},
        entry=lambda layer: entry({"python": 100, "tools": 10}[layer]),
    )
    monkeypatch.setattr(L, "load_index", lambda: index)
    monkeypatch.setattr(L, "LayerManager", lambda *a, **k: pytest.fail("LayerManager must not open on Linux"))

    hook = runtime_install.LayerDownloadHook()
    context = _Context()
    assert hook.download_bytes(context) == 0
    hook.run(context)
    assert any("skipped" in line for line in context.lines)


def test_layer_download_installs_only_tools_on_macos(monkeypatch):
    from nonoka_x.runtime import layers as L
    from nonoka_x.runtime import manifest_tools

    monkeypatch.setattr(sys, "platform", "darwin")
    entry = lambda size: SimpleNamespace(archive=SimpleNamespace(size=size))  # noqa: E731
    index = SimpleNamespace(
        platform=lm.PLATFORM,
        layers={"python": 1, "tools": 1},
        entry=lambda layer: entry({"python": 100, "tools": 10}[layer]),
    )
    monkeypatch.setattr(L, "load_index", lambda: index)
    monkeypatch.setattr(L, "LayerManager", lambda *a, **k: pytest.fail("Windows LayerManager must not open on macOS"))

    installed: list[str] = []
    fake_tools = SimpleNamespace(install=lambda layer: installed.append(layer) or SimpleNamespace(state="ready", wanted="macv"))
    monkeypatch.setattr(manifest_tools, "open_tools_manager", lambda paths: fake_tools)

    hook = runtime_install.LayerDownloadHook()
    context = _Context()
    assert hook.download_bytes(context) > 0
    hook.run(context)
    assert installed == ["tools"]
    assert any("python skipped" in line for line in context.lines)


def test_manifest_tools_manager_installs_verifies_and_repairs(tmp_path):
    import hashlib
    from http.server import BaseHTTPRequestHandler, HTTPServer
    import os
    import threading
    import zipfile
    from nonoka_x.provision import RuntimeProvisioner
    from nonoka_x.runtime import RuntimeManager
    from nonoka_x.runtime.fetch import NetworkRoute
    from nonoka_x.runtime.manifest_tools import ManifestToolsManager
    from nonoka_x.runtime.paths import NonokaPaths

    def make_zip(path: Path, files: dict[str, bytes]) -> tuple[int, str]:
        with zipfile.ZipFile(path, "w") as zf:
            for name, content in files.items():
                zf.writestr(name, content)
        raw = path.read_bytes()
        return len(raw), hashlib.sha256(raw).hexdigest()

    served_dir = tmp_path / "served"
    served_dir.mkdir()
    ff_size, ff_sha = make_zip(served_dir / "ffmpeg.zip", {"ffmpeg": b"#!/bin/sh\necho ffmpeg\n"})
    fp_size, fp_sha = make_zip(served_dir / "ffprobe.zip", {"ffprobe": b"#!/bin/sh\necho ffprobe\n"})
    yt_size, yt_sha = make_zip(served_dir / "yt_dlp.whl", {"yt_dlp/__init__.py": b"__version__ = '1'\n"})

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            target = served_dir / self.path.lstrip("/")
            if not target.is_file():
                self.send_response(404)
                self.end_headers()
                return
            data = target.read_bytes()
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, format, *args):
            return

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_port}/"
        manifest = {
            "schema_version": 1,
            "platform": "macos-arm64",
            "resources": [
                {
                    "id": "ffmpeg",
                    "version": "9.0.1",
                    "directory": "ffmpeg",
                    "archive_type": "zip",
                    "required_files": ["ffmpeg"],
                    "asset": {"url": base + "ffmpeg.zip", "size": ff_size, "sha256": ff_sha},
                },
                {
                    "id": "ffprobe",
                    "version": "9.0.1",
                    "directory": "ffprobe",
                    "archive_type": "zip",
                    "required_files": ["ffprobe"],
                    "asset": {"url": base + "ffprobe.zip", "size": fp_size, "sha256": fp_sha},
                },
                {
                    "id": "yt-dlp",
                    "version": "2026.8.19",
                    "directory": "yt-dlp",
                    "archive_type": "zip",
                    "required_files": ["yt_dlp/__init__.py"],
                    "asset": {"url": base + "yt_dlp.whl", "size": yt_size, "sha256": yt_sha},
                },
            ],
        }
        paths = NonokaPaths.for_roots(tmp_path / "data")
        mgr = ManifestToolsManager(
            paths,
            manifest,
            in_use=lambda: False,
            routes=lambda: [NetworkRoute("直连", None)],
            offline=lambda: None,
        )
        assert mgr.state("tools").state == "missing"

        stages: list[str] = []
        state = mgr.install("tools", progress=lambda p: stages.append(p.stage))
        assert state.ready
        assert "downloading" in stages and "unpacking" in stages and "completed" in stages

        ffmpeg = mgr.tool_file("ffmpeg")
        ffprobe = mgr.tool_file("ffprobe")
        yt_dir = mgr.tool_dir("yt-dlp")
        assert ffmpeg is not None and ffmpeg.is_file()
        assert ffprobe is not None and ffprobe.is_file()
        assert yt_dir is not None and (yt_dir / "yt_dlp" / "__init__.py").is_file()
        if os.name != "nt":
            assert os.access(ffmpeg, os.X_OK)
            assert os.access(ffprobe, os.X_OK)

        # Pointer and manifest layout match Go managedtools expectations.
        assert mgr.pointer("tools") == mgr.index.entry("tools").version
        assert (paths.layer("tools") / "current.json").is_file()

        # Full check detects corruption and repair restores it.
        ffmpeg.write_bytes(b"corrupted")
        assert mgr.state("tools", full=True, fresh=True).state == "damaged"
        repaired = mgr.repair("tools")
        assert repaired.ready
        assert ffmpeg.read_bytes() == b"#!/bin/sh\necho ffmpeg\n"

        # RuntimeManager facade recognizes media readiness.
        prov = RuntimeProvisioner(tmp_path / "data", ROOT / "engines")
        prov.layers = mgr
        rm = RuntimeManager(prov)
        assert rm.component_ready("media")
        assert rm.tool_path("ffmpeg") == ffmpeg
        assert rm.tool_path("ffprobe") == ffprobe
    finally:
        server.shutdown()
        server.server_close()


def test_system_tar_uses_posix_tar_on_macos(tmp_path):
    import io
    import tarfile
    from nonoka_x.runtime import layers as L

    if sys.platform == "darwin":
        assert L.SYSTEM_TAR == Path("/usr/bin/tar")
        archive = tmp_path / "sample.tar"
        manifest = b'{"schema": 1}\n'
        with tarfile.open(archive, "w") as handle:
            info = tarfile.TarInfo(lm.MANIFEST_NAME)
            info.size = len(manifest)
            handle.addfile(info, io.BytesIO(manifest))
        assert L.read_embedded_manifest(archive) == manifest
        out = tmp_path / "out"
        L.unpack(archive, out)
        assert (out / lm.MANIFEST_NAME).read_bytes() == manifest


def test_none_of_the_rewritten_modules_imports_finesub_bootstrap():
    for name in SOURCES:
        tree = ast.parse((ROOT / "src" / "nonoka_x" / name).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            modules = []
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules = [node.module]
            assert not any(module.split(".")[0] in {"finesub_bootstrap", "finesub"} for module in modules), name


def test_nothing_runs_uv_or_builds_a_venv():
    for name in SOURCES:
        text = (ROOT / "src" / "nonoka_x" / name).read_text(encoding="utf-8")
        tree = ast.parse(text)
        literals = [node.value for node in ast.walk(tree) if isinstance(node, ast.Constant) and isinstance(node.value, str)]
        assert not any(value in {"uv", "uv.exe", "venv", "pyvenv.cfg"} for value in literals), name
