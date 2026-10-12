from __future__ import annotations

import sys
from types import ModuleType, SimpleNamespace

from nonoka_x.model_install import ByteProgressReporter, _install_huggingface, emit_event, missing_managed_models


def test_model_byte_reporter_emits_bounded_percentage_inputs() -> None:
    events: list[dict] = []
    reporter = ByteProgressReporter("whisper", 100, events.append)

    reporter.update(0, force=True)
    reporter.update(125, force=True)

    assert events[0]["completed"] == 0
    assert events[-1]["completed"] == 100
    assert events[-1]["total"] == 100
    assert events[-1]["unit"] == "bytes"
    assert events[-1]["bytes_per_second"] >= 0


def test_model_event_wire_format_is_ascii_safe(capsys) -> None:
    emit_event({"type": "stage", "resource": "whisper", "message": "正在下载模型：whisper"})

    output = capsys.readouterr().out.strip()
    output.encode("ascii")
    assert "\\u6b63\\u5728" in output


def test_huggingface_download_reports_dry_run_total(monkeypatch, tmp_path) -> None:
    module = ModuleType("huggingface_hub")
    calls: list[dict] = []

    def snapshot_download(**kwargs):
        calls.append(kwargs)
        if kwargs.get("dry_run"):
            return [SimpleNamespace(file_size=100, will_download=True)]
        bar = kwargs["tqdm_class"](total=100, desc="Downloading bytes")
        bar.update(40)
        bar.update(60)
        bar.close()
        return str(tmp_path / "snapshot")

    module.snapshot_download = snapshot_download
    monkeypatch.setitem(sys.modules, "huggingface_hub", module)
    events: list[dict] = []

    snapshot = _install_huggingface(
        SimpleNamespace(repo="owner/model", revision="abc123"),
        "whisper",
        tmp_path,
        events.append,
    )

    assert snapshot == tmp_path / "snapshot"
    assert calls[0]["dry_run"] is True
    progress = [event for event in events if event["type"] == "progress"]
    assert progress[-1]["completed"] == 100
    assert progress[-1]["total"] == 100


def test_listed_only_entry_fetches_only_its_manifest_files(monkeypatch, tmp_path) -> None:
    module = ModuleType("huggingface_hub")
    calls: list[dict] = []

    def snapshot_download(**kwargs):
        calls.append(kwargs)
        return [] if kwargs.get("dry_run") else str(tmp_path / "snapshot")

    module.snapshot_download = snapshot_download
    monkeypatch.setitem(sys.modules, "huggingface_hub", module)
    entry = SimpleNamespace(
        repo="nvidia/model",
        revision="abc123",
        listed_only=True,
        files=(SimpleNamespace(name="model.safetensors"), SimpleNamespace(name="config.json")),
    )

    _install_huggingface(entry, "nemotron3", tmp_path, lambda _event: None)
    _install_huggingface(SimpleNamespace(repo="o/m", revision="r", listed_only=False, files=()), "whisper", tmp_path, lambda _event: None)

    assert calls[0]["allow_patterns"] == ["model.safetensors", "config.json"]
    assert calls[1]["allow_patterns"] == ["model.safetensors", "config.json"]
    assert calls[2]["allow_patterns"] is None


def test_diarization_models_are_in_the_engine_manifest() -> None:
    from finesub_bootstrap.model_manifest import entry_for
    from nonoka_x.model_install import DIARIZATION_MODEL_IDS

    for model_id in DIARIZATION_MODEL_IDS:
        assert entry_for(model_id) is not None, model_id


def test_managed_model_status_ignores_orphan_incomplete_files(monkeypatch, tmp_path) -> None:
    def wanted(name: str, size: int):
        return SimpleNamespace(name=name, size=size, sha256="a" * 64, is_verifiable=True)

    entries = {
        "separator": SimpleNamespace(
            repo="",
            revision="",
            files=(wanted("separator.ckpt", 4),),
        ),
        "whisper": SimpleNamespace(
            repo="owner/whisper",
            revision="whisper-revision",
            files=(wanted("model.bin", 5),),
        ),
        "qwen-referee": SimpleNamespace(
            repo="owner/qwen",
            revision="qwen-revision",
            files=(wanted("model.safetensors", 6),),
        ),
    }
    import finesub_bootstrap.model_manifest as manifest

    monkeypatch.setattr(manifest, "entry_for", entries.get)
    separator = tmp_path / "audio-separator" / "separator.ckpt"
    separator.parent.mkdir(parents=True)
    separator.write_bytes(b"1234")
    for model_id in ("whisper", "qwen-referee"):
        entry = entries[model_id]
        repository = f"models--{entry.repo.replace('/', '--')}"
        snapshot = tmp_path / "huggingface" / "hub" / repository / "snapshots" / entry.revision
        snapshot.mkdir(parents=True)
        (snapshot / entry.files[0].name).write_bytes(b"x" * entry.files[0].size)
    orphan = tmp_path / "huggingface" / "hub" / "models--owner--whisper" / "blobs" / "old-attempt.incomplete"
    orphan.parent.mkdir(parents=True)
    orphan.write_bytes(b"partial")

    assert missing_managed_models(tmp_path) == ()

    (tmp_path / "huggingface" / "hub" / "models--owner--qwen" / "snapshots" / "qwen-revision" / "model.safetensors").unlink()
    assert missing_managed_models(tmp_path) == ("qwen-referee",)
