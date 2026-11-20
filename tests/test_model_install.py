"""`nonoka_x.model_install`: the model ids, model presence in a model layer, migration's model completion (WP-E3)."""

from __future__ import annotations

import json
from pathlib import Path
import sys
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from nonoka_x import model_install  # noqa: E402
from nonoka_x.model_install import (  # noqa: E402
    DIARIZATION_MODEL_IDS,
    INSTALLABLE_MODEL_IDS,
    PIPELINE_MODEL_IDS,
    missing_managed_models,
    model_files,
)
from nonoka_x.runtime import layer_manifest as lm  # noqa: E402
from nonoka_x.runtime import manifests  # noqa: E402


def _lay_out(root: Path, model_id: str, *, skip: str | None = None, short: str | None = None) -> None:
    for name, size in model_files(model_id):
        if name == skip:
            continue
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "wb") as handle:
            handle.truncate((size or 1) - (1 if name == short else 0))


def test_the_model_layer_holds_exactly_the_installable_models():
    assert set(INSTALLABLE_MODEL_IDS) == set(lm.MODEL_LAYER_IDS)
    assert set(PIPELINE_MODEL_IDS).isdisjoint(DIARIZATION_MODEL_IDS)
    for model_id in INSTALLABLE_MODEL_IDS:
        assert model_id in manifests.model_manifest()["models"]


def test_model_files_follow_the_layout_the_engine_reads():
    separator = model_files("separator")
    assert all(name.startswith("audio-separator/") for name, _size in separator)
    whisper = model_files("whisper")
    entry = manifests.model_manifest()["models"]["whisper"]
    expected = f"huggingface/hub/models--{entry['repo'].replace('/', '--')}/snapshots/{entry['revision']}/"
    assert all(name.startswith(expected) for name, _size in whisper)


def test_presence_is_by_pinned_files_and_sizes(tmp_path):
    for model_id in INSTALLABLE_MODEL_IDS:
        _lay_out(tmp_path, model_id)
    assert missing_managed_models(tmp_path, INSTALLABLE_MODEL_IDS) == ()

    first = model_files("whisper")[0][0]
    (tmp_path / first).unlink()
    separator = model_files("separator")[0][0]
    with open(tmp_path / separator, "r+b") as handle:
        handle.truncate(1)
    assert missing_managed_models(tmp_path, INSTALLABLE_MODEL_IDS) == ("separator", "whisper")
    assert missing_managed_models(tmp_path, DIARIZATION_MODEL_IDS) == ()


def test_an_unknown_model_id_counts_as_missing(tmp_path):
    assert missing_managed_models(tmp_path, ("no-such-model",)) == ("no-such-model",)


def test_the_completion_hook_completes_the_migrated_directory():
    asked: list[Path] = []
    report = SimpleNamespace(version="v", kept=3, fetched=("a",), source="offline", rebuilt=(), removed=("b", "c"))
    manager = SimpleNamespace(complete_models=lambda directory, log: asked.append(directory) or report)
    lines: list[str] = []
    context = SimpleNamespace(migrated_models=Path("layers/models/migrated"), log=lines.append)
    hook = model_install.ModelCompletionHook(lambda _context: manager)

    assert hook.download_bytes(context) is None  # unknown until every moved file is hashed
    hook.run(context)

    assert asked == [Path("layers/models/migrated")]
    assert "kept 3, fetched 1 (offline)" in lines[-1] and "removed 2" in lines[-1]


def test_the_completion_hook_skips_on_unsupported_platforms(monkeypatch):
    from nonoka_x.runtime import layers as L

    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(L, "LayerManager", lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not open")))
    lines: list[str] = []
    context = SimpleNamespace(migrated_models=Path("layers/models/migrated"), log=lines.append)
    hook = model_install.ModelCompletionHook()

    assert hook.download_bytes(context) == 0
    hook.run(context)
    assert any("skipped" in line for line in lines)


def test_the_layer_markers_the_completion_rebuilds_are_the_builds(tmp_path):
    """`layers._marker_content` derives the stamps and markers exactly as `scripts/build_layers.py` writes them."""

    sys.path.insert(0, str(ROOT))
    from nonoka_x.runtime import layers as L
    from scripts import build_layers

    spec = manifests.model_manifest()["models"]["whisper"]
    directory = f"huggingface/hub/models--{spec['repo'].replace('/', '--')}"
    marker = {"manifest": build_layers.engine_entry_digest(spec), "repo": spec["repo"], "revision": spec["revision"]}
    built = json.dumps(marker, ensure_ascii=False).encode("utf-8")
    sources = {directory: {"directory": directory, "repo": spec["repo"], "revision": spec["revision"]}}
    assert L._marker_content(f"{directory}/{lm.HF_MARKER_NAME}", {}, sources) == built

    item = lm.LayerFile("audio-separator/model.ckpt", 10, "a" * 64)
    stamp = json.dumps({"sha256": item.sha256, "size": item.size, "mtime_ns": lm.FIXED_MTIME * 10**9}).encode()
    assert L._marker_content("audio-separator/model.ckpt.verified", {item.path: item}, {}) == stamp
    assert L._marker_content("audio-separator/model.ckpt", {item.path: item}, {}) is None


def test_migration_runs_the_two_layer_steps():
    from nonoka_x.migration import hooks
    from nonoka_x.runtime_install import LayerDownloadHook

    registered = hooks.available_hooks()
    assert isinstance(registered[hooks.MODEL_COMPLETION], model_install.ModelCompletionHook)
    assert isinstance(registered[hooks.LAYER_DOWNLOAD], LayerDownloadHook)
