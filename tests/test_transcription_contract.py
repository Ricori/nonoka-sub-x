"""WP0: the transcription and correction contracts, `transcript.json`, the facades.

`docs/transcription-engines-plan.md` sections 5.1-5.3 and WP0.
"""

from __future__ import annotations

import ast
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from nonoka_x import correction, transcription
from nonoka_x.config import ConfigStore, KeyEntry, KeySpec, key_specs, register_key_spec, registered_key_specs
from nonoka_x.config import _unregister_key_spec
from nonoka_x.runtime import ASR_RUNTIME, MEDIA, RuntimeManager, UnknownComponent
from nonoka_x.transcription import (
    ContractError,
    EngineEnvironment,
    EngineFeatures,
    EngineSpec,
    EventReporter,
    Issue,
    Readiness,
    ReadinessContext,
    Registry,
    Reporter,
    TranscriptionEngine,
    TranscriptionJob,
    TranscriptionResult,
    UnknownEngine,
    engine_capability,
)
from nonoka_x.transcription.model import (
    TranscriptDocument,
    TranscriptError,
    TranscriptLine,
    TranscriptWord,
    dumps_transcript,
    parse_transcript,
    read_transcript,
    write_transcript,
)

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "src" / "nonoka_x"

# The plan's own example (5.2), with the ellipses filled in.
EXAMPLE = {
    "schema": 1,
    "engine": "mai2",
    "model": "mai-transcribe-2",
    "language": "ja",
    "duration": 3600.0,
    "lines": [
        {
            "id": "1", "start": 1.0, "end": 2.4, "text": "こんにちは", "speaker": "S1",
            "words": [{"text": "こんにちは", "start": 1.0, "end": 1.3}],
        }
    ],
    "metadata": {"via": "openrouter", "log": []},
}


def _roundtrip(data: dict) -> dict:
    document = TranscriptDocument.from_dict(data)
    again = parse_transcript(dumps_transcript(document))
    assert again == document
    return again.to_dict()


# --- transcript.json -------------------------------------------------------------


def test_example_round_trips_unchanged() -> None:
    assert _roundtrip(EXAMPLE) == EXAMPLE


def test_round_trip_without_words() -> None:
    data = json.loads(json.dumps(EXAMPLE))
    del data["lines"][0]["words"]
    out = _roundtrip(data)
    assert out == data
    assert "words" not in out["lines"][0]
    assert TranscriptDocument.from_dict(data).lines[0].words is None


def test_round_trip_without_speaker() -> None:
    data = json.loads(json.dumps(EXAMPLE))
    del data["lines"][0]["speaker"]
    out = _roundtrip(data)
    assert out == data
    assert "speaker" not in out["lines"][0]
    assert TranscriptDocument.from_dict(data).lines[0].speaker is None


def test_round_trip_without_words_speaker_model_or_metadata() -> None:
    data = {
        "schema": 1, "engine": "moss-pro", "language": "zh", "duration": 12.5,
        "lines": [
            {"id": "a", "start": 0.0, "end": 1.0, "text": "一"},
            # Overlapping and out of order: allowed (two speakers talking at once).
            {"id": "b", "start": 0.5, "end": 0.9, "text": "二"},
        ],
    }
    out = _roundtrip(data)
    assert out == data
    assert list(out) == ["schema", "engine", "language", "duration", "lines"]


def test_empty_words_list_is_kept_distinct_from_absent() -> None:
    data = json.loads(json.dumps(EXAMPLE))
    data["lines"][0]["words"] = []
    assert _roundtrip(data)["lines"][0]["words"] == []


def test_unknown_fields_are_kept_and_written_back() -> None:
    data = json.loads(json.dumps(EXAMPLE))
    data["future_top"] = {"x": [1, 2]}
    data["lines"][0]["confidence"] = 0.9
    data["lines"][0]["words"][0]["probability"] = 0.5
    out = _roundtrip(data)
    assert out == data
    document = TranscriptDocument.from_dict(data)
    assert document.extra["future_top"] == {"x": [1, 2]}
    assert document.lines[0].extra == {"confidence": 0.9}
    assert document.lines[0].words[0].extra == {"probability": 0.5}


def test_integer_times_read_as_numbers() -> None:
    data = json.loads(json.dumps(EXAMPLE))
    data["duration"] = 3600
    data["lines"][0]["start"] = 1
    document = TranscriptDocument.from_dict(data)
    assert document.duration == 3600.0 and isinstance(document.duration, float)
    assert document.lines[0].start == 1.0


def test_file_round_trip(tmp_path: Path) -> None:
    path = write_transcript(TranscriptDocument.from_dict(EXAMPLE), tmp_path / "task" / "transcript.json")
    assert path.name == transcription.TRANSCRIPT_FILENAME
    text = path.read_text(encoding="utf-8")
    assert "こんにちは" in text  # written as UTF-8, not escaped
    assert read_transcript(path).to_dict() == EXAMPLE
    assert not list(path.parent.glob(".*.part"))


def _broken(mutate) -> dict:
    data = json.loads(json.dumps(EXAMPLE))
    mutate(data)
    return data


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda d: d.pop("schema"), "schema is required"),
        (lambda d: d.update(schema=2), "schema must be 1"),
        (lambda d: d.update(schema=True), "schema must be 1"),
        (lambda d: d.pop("engine"), "engine is required"),
        (lambda d: d.pop("language"), "language is required"),
        (lambda d: d.pop("duration"), "duration is required"),
        (lambda d: d.pop("lines"), "lines is required"),
        (lambda d: d.update(engine=""), "engine must not be empty"),
        (lambda d: d.update(duration=-1), "duration -1.0 is negative"),
        (lambda d: d.update(duration="1"), "duration must be a number"),
        (lambda d: d.update(lines={}), "lines must be a list"),
        (lambda d: d.update(metadata=[]), "metadata must be an object"),
        (lambda d: d.update(model=3), "model must be a string"),
        (lambda d: d["lines"][0].pop("id"), "lines[0].id is required"),
        (lambda d: d["lines"][0].update(id=""), "lines[0].id must not be empty"),
        (lambda d: d["lines"][0].update(id=1), "lines[0].id must be a string"),
        (lambda d: d["lines"][0].update(start=-0.1), "lines[0].start -0.1 is negative"),
        (lambda d: d["lines"][0].update(start=3.0), "lines[0].start 3.0 is after lines[0].end 2.4"),
        (lambda d: d["lines"][0].update(end=True), "lines[0].end must be a number"),
        (lambda d: d["lines"][0].update(text=None), "lines[0].text must be a string"),
        (lambda d: d["lines"][0].update(speaker=""), "lines[0].speaker must not be empty"),
        (lambda d: d["lines"][0].update(speaker=None), "lines[0].speaker must be a string"),
        (lambda d: d["lines"][0].update(words="a"), "lines[0].words must be a list"),
        (lambda d: d["lines"][0]["words"][0].pop("end"), "lines[0].words[0].end is required"),
        (
            lambda d: d["lines"][0]["words"][0].update(start=2.0),
            "lines[0].words[0].start 2.0 is after lines[0].words[0].end 1.3",
        ),
        (lambda d: d["lines"].append(dict(d["lines"][0])), "lines[1].id '1' is not unique"),
    ],
)
def test_invalid_documents_are_rejected(mutate, message: str) -> None:
    with pytest.raises(TranscriptError) as caught:
        TranscriptDocument.from_dict(_broken(mutate))
    assert message in str(caught.value)


def test_non_finite_numbers_are_rejected() -> None:
    with pytest.raises(TranscriptError, match="finite"):
        parse_transcript(json.dumps(EXAMPLE).replace("3600.0", "NaN"))
    with pytest.raises(TranscriptError, match="not valid JSON"):
        parse_transcript("{")


def test_constructed_documents_are_validated_too() -> None:
    with pytest.raises(TranscriptError):
        TranscriptLine(id="1", start=2.0, end=1.0, text="")
    with pytest.raises(TranscriptError, match="shadow"):
        TranscriptWord(text="a", start=0, end=1, extra={"start": 0})
    line = TranscriptLine(id="1", start=0, end=1, text="")
    with pytest.raises(TranscriptError, match="not unique"):
        TranscriptDocument(engine="e", language="ja", duration=1, lines=(line, line))


# --- specs, jobs and results ---------------------------------------------------


def _spec(**overrides) -> EngineSpec:
    values = dict(
        id="mai2",
        label="MAI-Transcribe-2",
        kind="cloud-api",
        features=EngineFeatures(
            speakers=True, word_timestamps=True,
            diarization_levels=("off", "acoustics", "judge-text", "judge-audio"),
        ),
        keys=("OPENROUTER_STT",),
        environment=EngineEnvironment(components=(MEDIA,), optional_components=(ASR_RUNTIME,)),
        billing="按音频时长计费",
    )
    values.update(overrides)
    return EngineSpec(**values)


def _through_json(value):
    return type(value).from_dict(json.loads(json.dumps(value.to_dict())))


def test_contract_types_round_trip_through_json(tmp_path: Path) -> None:
    result = TranscriptionResult(
        transcript_json=tmp_path / "transcript.json",
        native={"finesub.stable_json": tmp_path / "a-stable.json", "finesub.diar_json": tmp_path / "a-diar.json"},
        metadata={"windows": 3},
    )
    values = [
        _spec(),
        _spec(id="finesub", label="FineSub", kind="local-gpu", environment=EngineEnvironment(gpu="optional")),
        Readiness((Issue("missing_key", "缺少密钥"),)),
        Readiness(),
        ReadinessContext(target="raw-srt", language="ja", device="cpu", options={"x": 1}),
        TranscriptionJob(
            source=tmp_path / "v.mp4", language="ja", task_dir=tmp_path, cache_dir=tmp_path / "cache",
            target="stable", options={"repair": True},
        ),
        result,
        correction.CorrectionJob(
            transcription=result, source=tmp_path / "v.mp4", target="final-srt", task_dir=tmp_path,
            options={"media": "audio"}, knowledge={"knowledge": "read"}, diarization_level="acoustics",
        ),
        correction.CorrectionResult(artifacts={"raw_srt": tmp_path / "a-raw.srt"}),
        correction.CorrectionSpec(id="finesub", label="finesub correction"),
    ]
    for value in values:
        assert _through_json(value) == value, type(value).__name__


def test_engine_kinds_and_provider() -> None:
    assert transcription.ENGINE_KINDS == ("local-gpu", "cloud-api", "cloud-service")
    assert _spec(kind="local-gpu").provider == "local"
    assert _spec(kind="cloud-api").provider == "local"
    assert _spec(id=transcription.CLOUD_ENGINE, kind="cloud-service").provider == "cloud"
    with pytest.raises(ContractError):
        _spec(kind="gpu")
    with pytest.raises(ContractError):
        EngineSpec.from_dict({**_spec().to_dict(), "provider": "cloud"})
    with pytest.raises(ContractError):
        _spec(id="Not An Id")
    with pytest.raises(ContractError):
        EngineFeatures(diarization_levels=("loud",))
    with pytest.raises(ContractError):
        EngineEnvironment(gpu="maybe")


def test_readiness_is_ready_exactly_without_issues() -> None:
    assert Readiness().ready
    assert not Readiness((Issue("x", "y"),)).ready
    with pytest.raises(ContractError):
        Readiness.from_dict({"ready": False, "issues": []})


def test_capability_entries_match_provider_spec() -> None:
    entry = engine_capability(_spec(), Readiness((Issue("missing_key", "…"),)))
    assert entry == {
        "id": "mai2",
        "label": "MAI-Transcribe-2",
        "kind": "cloud-api",
        "provider": "local",
        "ready": False,
        "issues": [{"code": "missing_key", "message": "…"}],
        "features": {
            "speakers": True, "word_timestamps": True, "separate": False,
            "diarization_levels": ["off", "acoustics", "judge-text", "judge-audio"],
            "languages": [],
        },
        "billing": "按音频时长计费",
        "keys": ["OPENROUTER_STT"],
    }
    spec = correction.CorrectionSpec(id="finesub", label="finesub correction")
    assert correction.correction_capability(spec, Readiness()) == {"id": "finesub", "ready": True, "issues": []}


def test_native_artifacts_are_namespaced_and_opaque(tmp_path: Path) -> None:
    with pytest.raises(ContractError):
        TranscriptionResult(transcript_json=tmp_path / "t.json", native={"stable_json": tmp_path})
    result = TranscriptionResult(transcript_json=tmp_path / "t.json", native={"mai2.raw_responses": str(tmp_path)})
    assert result.native["mai2.raw_responses"] == tmp_path


def test_correction_targets() -> None:
    result = TranscriptionResult(transcript_json=Path("t.json"))
    for target in correction.CORRECTION_TARGETS:
        correction.CorrectionJob(transcription=result, source=Path("v"), target=target, task_dir=Path("."))
    with pytest.raises(ContractError):
        correction.CorrectionJob(transcription=result, source=Path("v"), target="stable", task_dir=Path("."))


# --- request fields (5.3) ---------------------------------------------------------


def test_requested_engines_default_to_finesub() -> None:
    assert transcription.requested_engine({}) == "finesub"
    assert transcription.requested_engine({"transcription": {}}) == "finesub"
    assert transcription.requested_engine({"transcription": {"engine": "mai2", "options": {}}}) == "mai2"
    assert dict(transcription.requested_options({"transcription": {"engine": "mai2", "options": {"a": 1}}})) == {"a": 1}
    assert dict(transcription.requested_options({})) == {}
    with pytest.raises(ContractError):
        transcription.requested_engine({"transcription": "mai2"})
    with pytest.raises(ContractError):
        transcription.requested_engine({"transcription": {"engine": "../x"}})
    assert correction.requested_engine({}) == "finesub"
    assert correction.requested_engine({"correction": {"media": "audio"}}) == "finesub"
    assert correction.requested_engine({"correction": {"engine": "other"}}) == "other"


# --- registries -------------------------------------------------------------------


class _FakeEngine:
    def __init__(self, spec: EngineSpec) -> None:
        self.spec = spec

    def readiness(self, context: ReadinessContext) -> Readiness:
        return Readiness()

    def transcribe(self, job: TranscriptionJob, report: Reporter) -> TranscriptionResult:
        report.stage("asr", "transcribing")
        document = TranscriptDocument(
            engine=self.spec.id, language=job.language, duration=1.0,
            lines=(TranscriptLine(id="1", start=0.0, end=1.0, text="x"),),
        )
        return TranscriptionResult(transcript_json=write_transcript(document, job.task_dir / "transcript.json"))


class _FakeCorrection:
    spec = correction.CorrectionSpec(id="fake", label="fake")

    def readiness(self, context, request) -> Readiness:
        return Readiness()

    def run(self, job, report) -> correction.CorrectionResult:
        return correction.CorrectionResult(artifacts={"raw_srt": job.task_dir / "raw.srt"})


def test_registry_behaviour() -> None:
    registry: Registry[TranscriptionEngine] = Registry("transcription")
    first = registry.register(_FakeEngine(_spec()))
    registry.register(_FakeEngine(_spec(id="gemini-transcribe", label="Gemini")))
    assert registry.ids() == ("mai2", "gemini-transcribe")
    assert registry.get("mai2") is first
    assert "mai2" in registry and len(registry) == 2
    with pytest.raises(ContractError, match="already registered"):
        registry.register(_FakeEngine(_spec()))
    with pytest.raises(UnknownEngine, match="gemini-transcribe"):
        registry.get("finesub")
    registry.unregister("mai2")
    assert registry.ids() == ("gemini-transcribe",)


def test_module_registries_start_empty_and_are_separate() -> None:
    assert transcription.registry is not correction.registry
    assert transcription.registry.layer == "transcription"
    assert correction.registry.layer == "correction"
    assert transcription.registry.ids() == ()
    assert correction.registry.ids() == ()


def test_fake_engines_satisfy_the_protocols(tmp_path: Path) -> None:
    engine = _FakeEngine(_spec())
    assert isinstance(engine, TranscriptionEngine)
    assert isinstance(_FakeCorrection(), correction.CorrectionEngine)
    events: list[tuple[str, dict]] = []
    report = EventReporter(lambda kind, payload: events.append((kind, dict(payload))))
    assert isinstance(report, Reporter)
    job = TranscriptionJob(source=tmp_path / "v", language="ja", task_dir=tmp_path, cache_dir=tmp_path, target="raw-srt")
    result = engine.transcribe(job, report)
    assert result.read_transcript().engine == "mai2"
    assert events == [("stage", {"stage": "asr", "message": "transcribing"})]
    corrected = _FakeCorrection().run(
        correction.CorrectionJob(transcription=result, source=job.source, target="raw-srt", task_dir=tmp_path),
        report,
    )
    assert "raw_srt" in corrected.artifacts


def test_event_reporter_events() -> None:
    events: list[tuple[str, dict]] = []
    cancelled = [False]
    report = EventReporter(lambda kind, payload: events.append((kind, dict(payload))), lambda: cancelled[0])
    report.progress("asr", completed=1, total=4, unit="windows")
    report.progress("asr", completed=2)
    report.log("hello", {"path": Path("x")})
    report.warning("no-runtime", "跳过空档重问")
    assert events == [
        ("progress", {"stage": "asr", "completed": 1, "total": 4, "unit": "windows", "message": ""}),
        ("progress", {"stage": "asr", "completed": 2, "unit": "", "message": ""}),
        ("log", {"message": "hello", "fields": {"path": "x"}}),
        ("log", {"level": "warning", "code": "no-runtime", "message": "跳过空档重问"}),
    ]
    assert not report.cancelled()
    cancelled[0] = True
    assert report.cancelled()


# --- import boundaries ------------------------------------------------------------


def _imports(path: Path) -> set[str]:
    """Absolute module names imported by `path`, relative imports resolved."""

    package = ".".join(("nonoka_x", *path.relative_to(PACKAGE).with_suffix("").parts))
    if path.name == "__init__.py":
        package = package.rsplit(".", 1)[0]
        base = package.split(".")
    else:
        base = package.split(".")[:-1]
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                anchor = base[: len(base) - node.level + 1]
                module = ".".join([*anchor, *([node.module] if node.module else [])])
            else:
                module = node.module or ""
            names.add(module)
            names.update(f"{module}.{alias.name}" for alias in node.names)
    return names


def _layer_files(layer: str) -> list[Path]:
    return sorted((PACKAGE / layer).rglob("*.py"))


def test_only_the_finesub_adapters_import_finesub() -> None:
    for layer, allowed in (("transcription", "engines/finesub.py"), ("correction", "finesub.py")):
        for path in _layer_files(layer):
            relative = path.relative_to(PACKAGE / layer).as_posix()
            fine = {name for name in _imports(path) if name.split(".")[0] in {"finesub", "finesub_bootstrap"}}
            if relative != allowed:
                assert not fine, f"{layer}/{relative} imports {sorted(fine)}"


def test_layers_do_not_import_each_others_implementations() -> None:
    for path in _layer_files("transcription"):
        bad = {name for name in _imports(path) if name == "nonoka_x.correction" or name.startswith("nonoka_x.correction.")}
        assert not bad, f"{path.relative_to(PACKAGE)} imports {sorted(bad)}"
    # The correction layer may use the transcription contract, never an engine.
    # `from nonoka_x.transcription import X` records `nonoka_x.transcription.X`.
    from nonoka_x.transcription import model as transcript_model

    contract = {"nonoka_x.transcription", "nonoka_x.transcription.model"}
    contract |= {f"nonoka_x.transcription.{name}" for name in transcription.__all__}
    contract |= {f"nonoka_x.transcription.model.{name}" for name in transcript_model.__all__}
    for path in _layer_files("correction"):
        bad = {
            name for name in _imports(path)
            if name.startswith("nonoka_x.transcription.") and name not in contract
        }
        assert not bad, f"{path.relative_to(PACKAGE)} imports {sorted(bad)}"


def test_contracts_import_in_a_bare_interpreter() -> None:
    """Launcher environment: no torch, no numpy, no FineSub on the path."""

    code = (
        "import sys\n"
        "import nonoka_x.transcription, nonoka_x.transcription.model, nonoka_x.correction\n"
        "import nonoka_x.config, nonoka_x.runtime\n"
        "heavy = sorted(m for m in sys.modules if m.split('.')[0] in {'torch', 'numpy', 'finesub', 'finesub_bootstrap'})\n"
        "print(','.join(heavy))\n"
    )
    environment = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
    completed = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, env=environment, cwd=ROOT, check=True
    )
    assert completed.stdout.strip() == ""


# --- facades ----------------------------------------------------------------------


@pytest.fixture
def config_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from finesub.config import clear_config_cache
    from finesub.llm.routing.model_catalog import default_model_catalog
    from finesub.llm.routing.model_routes import default_model_routes

    monkeypatch.setenv("FINESUB_ENV_PROTECT", "0")
    for name in ("FINESUB_ENV_FILE", "FINESUB_CONFIG_FILE", "FINESUB_MODEL_CATALOG", "FINESUB_CHECKOUT_DATA",
                 "FINESUB_KNOWLEDGE_ROOT", "FINESUB_STATE_FILE"):
        monkeypatch.setenv(name, "")
        monkeypatch.delenv(name)
    resets = (clear_config_cache, default_model_catalog.cache_clear, default_model_routes.cache_clear)
    for reset in resets:
        reset()
    store = ConfigStore.open(tmp_path)
    store._settings.bind_environment()
    yield store
    for reset in resets:
        reset()


def test_config_reads_key_pools(config_store: ConfigStore) -> None:
    assert config_store.key_pool("GEMINI_FREE") == ()
    assert not config_store.has_key("GEMINI_FREE")
    snapshot = config_store.update({"GEMINI_FREE": "primary:g-secret-1, backup:g-secret-2", "EXA_KEYS": "exa-1,exa-2"})
    assert "g-secret-1" not in repr(snapshot)
    assert config_store.key_pool("GEMINI_FREE") == (KeyEntry("primary", "g-secret-1"), KeyEntry("backup", "g-secret-2"))
    assert config_store.key_pool("EXA_KEYS") == (KeyEntry("", "exa-1"), KeyEntry("", "exa-2"))
    assert "g-secret" not in repr(config_store.key_pool("GEMINI_FREE"))
    assert config_store.snapshot()["llmKeyConfigured"]
    with pytest.raises(KeyError):
        config_store.key_pool("NO_SUCH_KEY")


def test_registered_key_specs_live_in_memory_and_their_keys_in_secrets_json(config_store: ConfigStore) -> None:
    spec = KeySpec("WP0_TEST_STT_KEYS", "测试 STT", "只在内存里")
    try:
        assert register_key_spec(spec) is spec
        assert register_key_spec(spec) is spec  # idempotent
        assert spec in registered_key_specs()
        assert key_specs()[-1] == spec
        with pytest.raises(ValueError, match="different spec"):
            register_key_spec(KeySpec("WP0_TEST_STT_KEYS", "另一个"))
        with pytest.raises(ValueError, match="already a settings key"):
            register_key_spec(KeySpec("GEMINI_FREE", "Gemini"))
        # An empty pool until saved; saved through the facade it lives in secrets.json (WP-R4,
        # owner 2026-11-12), while the settings page's key list and FineSub's key file never learn the name.
        assert config_store.key_pool("WP0_TEST_STT_KEYS") == ()
        snapshot = config_store.update({"WP0_TEST_STT_KEYS": "label:fake-registered-key"})
        assert config_store.key_pool("WP0_TEST_STT_KEYS") == (KeyEntry("label", "fake-registered-key"),)
        # The settings' own list never learns it; the engine panel's `engineKeys` lists it, masked
        # (WP6, owner 2026-11-16).
        assert "WP0_TEST" not in repr(snapshot["keys"])
        listed = {item["name"]: item for item in snapshot["engineKeys"]}["WP0_TEST_STT_KEYS"]
        assert listed["configured"] and listed["count"] == 1
        assert "fake-registered-key" not in repr(snapshot)
        from nonoka_x.settings import CORRECTION_SECRET_NAMES, TRANSCRIPTION_SECRET_NAMES

        assert "WP0_TEST_STT_KEYS" not in (*CORRECTION_SECRET_NAMES, *TRANSCRIPTION_SECRET_NAMES)
        assert "fake-registered-key" not in (config_store.root / "secrets.json").read_text(encoding="utf-8")
        with pytest.raises(ValueError):
            config_store.update({"WP0_NOT_REGISTERED": "x"})
        from nonoka_x.settings import KEY_NAMES

        assert "WP0_TEST_STT_KEYS" not in KEY_NAMES
    finally:
        _unregister_key_spec("WP0_TEST_STT_KEYS")
    with pytest.raises(ValueError):
        KeySpec("lower", "x")


class _FakeProvisioner:
    def __init__(self, *, python: Path | None, tools: dict[str, Path]) -> None:
        self._python = python
        self._tools = tools

    def worker_python(self) -> Path | None:
        return self._python

    def tool_path(self, name: str) -> Path | None:
        return self._tools.get(name)

    def worker_environment(self) -> dict[str, str]:
        return {"MARK": "provisioner"}

    def status(self) -> dict:
        return {
            "resources": [{"id": "yt-dlp", "state": "ready"}, {"id": "node", "state": "missing"}],
            "models": [{"id": "separator", "state": "ready"}, {"id": "whisper", "state": "missing"}],
            "optional_models": [{"id": "speaker-embedding", "state": "ready"}],
        }


def test_runtime_facade_delegates_to_the_provisioner(tmp_path: Path) -> None:
    ffmpeg = tmp_path / "ffmpeg.exe"
    runtime = RuntimeManager(_FakeProvisioner(python=tmp_path / "python.exe", tools={"ffmpeg": ffmpeg}))
    assert runtime.asr_runtime_available()
    assert runtime.worker_python() == tmp_path / "python.exe"
    assert runtime.tool_path("ffmpeg") == ffmpeg
    assert runtime.tool_path("ffprobe") is None
    assert runtime.component_ready(ASR_RUNTIME)
    assert runtime.component_ready("ffmpeg")
    assert not runtime.component_ready(MEDIA)  # ffprobe missing
    assert runtime.component_ready("yt-dlp")
    assert not runtime.component_ready("node")
    assert runtime.model_ready("separator")
    assert not runtime.model_ready("whisper")
    assert runtime.model_ready("speaker-embedding")
    assert runtime.worker_environment() == {"MARK": "provisioner"}
    with pytest.raises(UnknownComponent):
        runtime.tool_path("git")
    with pytest.raises(UnknownComponent):
        runtime.component_ready("no-such-tool")
    with pytest.raises(UnknownComponent):
        runtime.model_ready("no-such-model")


def test_runtime_facade_without_a_provisioner() -> None:
    runtime = RuntimeManager(None)
    assert not runtime.asr_runtime_available()
    assert runtime.tool_path("ffmpeg") is None
    assert not runtime.component_ready(MEDIA)
    assert not runtime.component_ready("yt-dlp")
    assert not runtime.model_ready("separator")
    assert runtime.worker_environment() == dict(os.environ)
