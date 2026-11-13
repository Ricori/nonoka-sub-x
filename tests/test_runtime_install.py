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
