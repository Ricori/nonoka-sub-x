"""MAI-Transcribe-2 as a transcription engine (`docs/transcription-engines-plan.md` WP3a).

Offline throughout: the provider's answers are WP1's recorded ones (`tests/fixtures/transcription/cases/`,
raw answers in the backend's own cache layout, recorded through FineSub's unmodified `speech/joint/`), copied
into the job's cache directory so the engine replays them; ffprobe is stood in for. The engine's lines are held
to WP1's line baselines (`expected/<case>.json`). The first version has no gap re-asks (they need Silero, plan
4.1 item 6): on the repair case the engine's transcript is the baseline's less exactly the words the re-ask
inserted (owner 2026-11-12, WP3 question 1).
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from nonoka_x import config  # noqa: E402
from nonoka_x.transcription import (  # noqa: E402
    ReadinessContext,
    TranscriptionEngine,
    TranscriptionJob,
    audio,
    keys,
    read_transcript,
)
from nonoka_x.transcription.engines import cloud, mai2  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures" / "transcription"
SOURCE = FIXTURES / "source.bin"


class Store:
    def __init__(self, root: Path, pools: dict[str, list[tuple[str, str]]]):
        self.root = root
        self.pools = pools

    def key_pool(self, name):
        return tuple(config.KeyEntry(label, value) for label, value in self.pools.get(name, ()))

    def state_file(self, name):
        return self.root / "state" / f"{name}.json"


class Runtime:
    def __init__(self, media: bool = True):
        self.media = media

    def component_ready(self, component):
        return self.media and component == "media"

    def tool_path(self, name):
        return Path(f"C:/tools/{name}.exe") if self.media else None


class Recorder:
    def __init__(self, cancelled: bool = False):
        self.warnings: list[tuple[str, str]] = []
        self.logs: list[tuple[str, dict | None]] = []
        self._cancelled = cancelled

    def stage(self, *a, **k):
        pass

    def progress(self, *a, **k):
        pass

    def log(self, message, fields=None):
        self.logs.append((message, fields))

    def warning(self, code, message):
        self.warnings.append((code, message))

    def cancelled(self):
        return self._cancelled


def load(name: str) -> tuple[dict, dict]:
    case = json.loads((FIXTURES / "cases" / name / "case.json").read_text(encoding="utf-8"))
    expected = json.loads((FIXTURES / "expected" / f"{name}.json").read_text(encoding="utf-8"))
    return case, expected


def job_for(tmp_path: Path, name: str, *, cached: bool = True, options=None) -> TranscriptionJob:
    case, _ = load(name)
    task = tmp_path / "task"
    task.mkdir(parents=True)
    if cached:
        shutil.copytree(FIXTURES / "cases" / name / "cache", task / "cache")
    return TranscriptionJob(source=SOURCE, language=case["language"], task_dir=task, cache_dir=task / "cache",
                            target="final-srt", options=options or {})


@pytest.fixture
def engine(tmp_path: Path, monkeypatch):
    store = Store(tmp_path, {keys.OPENROUTER_STT_KEY: [("", "or-key")]})
    monkeypatch.setattr(audio, "probe_media_duration", lambda path, **k: pytest.fail("no duration stubbed"))
    yield mai2.Mai2Engine(store, Runtime())
    keys.use_store(None)
    audio.use_runtime(None)


def with_duration(monkeypatch, name: str) -> None:
    case, _ = load(name)
    monkeypatch.setattr(audio, "probe_media_duration", lambda path, **k: case["duration"])


def baseline_lines(expected: dict, variant: str) -> list[dict]:
    """The baseline's lines as `transcript.json` carries them (`cloud.document`'s rules, written out here)."""
    return [{"id": str(i + 1), "start": s["start"], "end": s["end"], "text": s["text"], "speaker": s["line_speaker"],
             **({"words": [{"text": w["word"], "start": w["start"], "end": w["end"]} for w in s["words"]]}
                if s.get("words") else {})}
            for i, s in enumerate(expected["lines"][variant]["segments"])]


def no_network(monkeypatch) -> list:
    asked = []

    def refuse(*args, **kwargs):
        asked.append(args)
        raise AssertionError("an offline test must not call MAI2")

    monkeypatch.setattr(mai2.Caller, "__call__", refuse)
    return asked


# --- the spec and readiness ---------------------------------------------------------------------------------


def test_the_spec_says_what_the_engine_needs() -> None:
    spec = mai2.SPEC
    assert isinstance(mai2.Mai2Engine(), TranscriptionEngine)
    assert (spec.id, spec.kind, spec.provider) == ("mai2", "cloud-api", "local")
    assert spec.keys == ("OPENROUTER_STT_KEY", "AZURE_SPEECH_KEY", "AZURE_SPEECH_ENDPOINT")
    assert spec.environment.components == ("media",) and spec.environment.gpu == "none"
    assert not spec.environment.models and not spec.environment.optional_components
    assert spec.features.speakers and spec.features.word_timestamps and not spec.features.separate
    assert "$0.10/小时" in spec.billing


def test_readiness_names_the_missing_keys(tmp_path: Path) -> None:
    store = Store(tmp_path, {})
    issues = mai2.Mai2Engine(store, Runtime()).readiness(ReadinessContext(target="final-srt")).issues
    assert [i.code for i in issues] == ["missing_key"]
    store.pools = {keys.AZURE_SPEECH_KEY: [("", "az")]}  # a key without its endpoint opens no route
    assert not mai2.Mai2Engine(store, Runtime()).readiness(ReadinessContext()).ready
    store.pools[keys.AZURE_SPEECH_ENDPOINT] = [("", "https://r.test")]
    assert mai2.Mai2Engine(store, Runtime()).readiness(ReadinessContext()).ready
    store.pools = {keys.OPENROUTER_STT_KEY: [("", "or")]}
    assert mai2.Mai2Engine(store, Runtime()).readiness(ReadinessContext()).ready


def test_readiness_needs_the_media_tools_and_a_store(tmp_path: Path) -> None:
    store = Store(tmp_path, {keys.OPENROUTER_STT_KEY: [("", "or")]})
    assert [i.code for i in mai2.Mai2Engine(store, Runtime(media=False)).readiness(ReadinessContext()).issues] == [
        "missing_component"]
    assert [i.code for i in mai2.Mai2Engine(store, None).readiness(ReadinessContext()).issues] == ["runtime_unavailable"]
    assert [i.code for i in mai2.Mai2Engine(None, Runtime()).readiness(ReadinessContext()).issues] == [
        "config_unavailable"]


def test_the_route_prefers_openrouter_then_the_azure_resource(tmp_path: Path) -> None:
    store = Store(tmp_path, {keys.OPENROUTER_STT_KEY: [("", "or-key")], keys.AZURE_SPEECH_KEY: [("", "az")],
                             keys.AZURE_SPEECH_ENDPOINT: [("", "https://r.test")]})
    caller = mai2.route(store)
    assert (caller.via, caller.key) == ("openrouter", "or-key")
    del store.pools[keys.OPENROUTER_STT_KEY]
    caller = mai2.route(store)
    assert (caller.via, caller.key, caller.endpoint) == ("azure", "az", "https://r.test")
    store.pools = {}
    assert mai2.route(store) is None


# --- reading the two answer formats -----------------------------------------------------------------------


def test_both_recorded_answer_formats_are_read() -> None:
    openrouter = json.loads(next((FIXTURES / "cases" / "mai2-openrouter-windows" / "cache" / "mai2")
                                 .glob("w-*.raw.json")).read_text(encoding="utf-8"))
    azure = json.loads(next((FIXTURES / "cases" / "mai2-azure-repair" / "cache" / "mai2")
                            .glob("w-*.raw.json")).read_text(encoding="utf-8"))
    assert "words" in openrouter and "phrases" in azure
    for answer, count in ((openrouter, len(openrouter["words"])),
                          (azure, sum(len(p["words"]) for p in azure["phrases"]))):
        words = mai2.answer_words(answer, 100.0)
        assert len(words) == count and words
        assert all(w["start"] >= 100.0 and w["end"] >= w["start"] and w["text"] for w in words)
        assert {w["speaker"] for w in words} <= {f"S{n}" for n in range(10)}
    first = azure["phrases"][0]["words"][0]
    assert mai2.azure_words(azure, 0.0)[0]["start"] == first["offsetMilliseconds"] / 1000
    w0 = openrouter["words"][0]
    assert mai2.openrouter_words(openrouter, 0.0)[0] == {"start": w0["start"], "end": w0["end"],
                                                         "speaker": f"S{w0['speaker']}", "text": w0["word"]}


# --- a run ---------------------------------------------------------------------------------------------------


def test_windows_stitch_into_the_baselines_lines(engine, tmp_path: Path, monkeypatch) -> None:
    """Two OpenRouter windows (30 min, 60 s overlap) of a 35-min source: the engine's `transcript.json` holds
    the lines FineSub's joint stage built from them, line for line, with each line's speaker."""
    name = "mai2-openrouter-windows"
    _, expected = load(name)
    with_duration(monkeypatch, name)
    asked = no_network(monkeypatch)
    report = Recorder()
    result = engine.transcribe(job_for(tmp_path, name), report)
    assert not asked
    document = read_transcript(result.transcript_json)
    assert result.transcript_json == tmp_path / "task" / "transcript.json"
    assert (document.engine, document.model, document.language, document.duration) == (
        "mai2", "mai-transcribe-2", "ja", 2100.0)
    assert [line.to_dict() for line in document.lines] == baseline_lines(expected, "scale-1.0")
    assert document.metadata["log"] == expected["transcript"]["log"]  # the two windows
    assert document.metadata["via"] == "openrouter" and document.metadata["repair"] == "off"
    assert len({line.speaker for line in document.lines}) > 1
    # every window cached: nothing to upload, nothing to pay, no upload notice
    assert [code for code, _ in report.warnings] == ["cloud-asr-cost"]
    assert "0.00 小时" in report.warnings[0][1]


def test_without_repair_the_transcript_is_the_baseline_less_what_the_reask_inserted(
        engine, tmp_path: Path, monkeypatch) -> None:
    """The Azure case was recorded with repair on (a gap Silero heard speech in, re-asked). The engine does
    not re-ask: its units are the baseline's less exactly the `inserted` words, all inside the logged gaps."""
    name = "mai2-azure-repair"
    case, expected = load(name)
    want = [u for turn in expected["transcript"]["turns"] for u in turn["units"]]
    (entry,) = [e for e in expected["transcript"]["log"] if e.get("repair") == "miss"]
    got = mai2.transcribe(SOURCE, cache_root=FIXTURES / "cases" / name / "cache", language=case["language"],
                          via="azure", repair="off", caller=None, offline=True, duration=case["duration"])
    units = json.loads(json.dumps([u.as_dict() for u in got.units()]))
    dropped = [u for u in want if u not in units]
    assert [u for u in want if u in units] == units  # nothing else moved
    assert len(dropped) == entry["inserted"] > 0
    assert all(any(lo - 0.3 <= u["start"] and u["end"] <= hi + 0.3 for lo, hi in entry["gaps"]) for u in dropped)
    assert got.log == []

    store = Store(tmp_path, {keys.AZURE_SPEECH_KEY: [("", "az")], keys.AZURE_SPEECH_ENDPOINT: [("", "https://r")]})
    with_duration(monkeypatch, name)
    no_network(monkeypatch)
    result = mai2.Mai2Engine(store, Runtime()).transcribe(job_for(tmp_path, name), Recorder())
    document = read_transcript(result.transcript_json)
    assert document.metadata["via"] == "azure" and "log" not in document.metadata
    assert sum(len(line.words or ()) for line in document.lines) == len(units)


def test_the_estimate_counts_the_windows_still_to_upload(tmp_path: Path) -> None:
    case, _ = load("mai2-openrouter-windows")
    empty = tmp_path / "empty"
    seconds = mai2.billable_seconds(SOURCE, cache_root=empty, via="openrouter", language="ja", duration=2100.0)
    assert seconds == 1800.0 + (2100.0 - 1740.0)  # both windows, the shared minute twice
    cached = FIXTURES / "cases" / "mai2-openrouter-windows" / "cache"
    assert mai2.billable_seconds(SOURCE, cache_root=cached, via="openrouter", language="ja", duration=2100.0) == 0


def test_the_estimate_comes_before_the_first_upload(engine, tmp_path: Path, monkeypatch) -> None:
    name = "mai2-openrouter-windows"
    _, expected = load(name)
    with_duration(monkeypatch, name)
    recorded = {p.name.removesuffix(".raw.json"): json.loads(p.read_text(encoding="utf-8"))
                for p in (FIXTURES / "cases" / name / "cache" / "mai2").glob("w-*.raw.json")}
    report = Recorder()
    uploads = []

    def answer(caller, clip, *, locale):
        uploads.append((caller.via, caller.key, locale, [code for code, _ in report.warnings]))
        return recorded[clip.parent.name]

    def clip(src, start, end, out, **k):  # the window's cache name, so `answer` can find it
        path = Path(out).parent / mai2.window_name(start, end) / "window.mp3"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"mp3")
        return path

    monkeypatch.setattr(mai2.Caller, "__call__", answer)
    monkeypatch.setattr(mai2, "extract_upload_mp3", clip)
    result = engine.transcribe(job_for(tmp_path, name, cached=False), report)
    assert [u[:3] for u in uploads] == [("openrouter", "or-key", "ja")] * 2
    assert uploads[0][3] == ["cloud-asr-cost", "cloud-asr-upload"]
    assert [line.to_dict() for line in read_transcript(result.transcript_json).lines] == baseline_lines(
        expected, "scale-1.0")
    assert sorted(p.name for p in (tmp_path / "task" / "cache" / "mai2").glob("*.raw.json")) == sorted(
        f"{n}.raw.json" for n in recorded)


def test_a_cancelled_task_makes_no_call(engine, tmp_path: Path, monkeypatch) -> None:
    with_duration(monkeypatch, "mai2-openrouter-windows")
    asked = no_network(monkeypatch)
    with pytest.raises(cloud.EngineCancelled):
        engine.transcribe(job_for(tmp_path, "mai2-openrouter-windows", cached=False), Recorder(cancelled=True))
    assert not asked

    def clip(src, start, end, out, **k):
        Path(out).write_bytes(b"mp3")
        return Path(out)

    monkeypatch.setattr(mai2, "extract_upload_mp3", clip)

    class LateCancel(Recorder):
        def cancelled(self):  # not yet when the run starts; by the first upload it is
            self.asks = getattr(self, "asks", 0) + 1
            return self.asks > 1

    with pytest.raises(cloud.EngineCancelled):
        engine.transcribe(job_for(tmp_path / "again", "mai2-openrouter-windows", cached=False), LateCancel())
    assert not asked


def test_without_keys_the_run_stops_before_any_call(tmp_path: Path, monkeypatch) -> None:
    with_duration(monkeypatch, "mai2-openrouter-windows")
    with pytest.raises(cloud.CloudEngineError):
        mai2.Mai2Engine(Store(tmp_path, {}), Runtime()).transcribe(
            job_for(tmp_path, "mai2-openrouter-windows"), Recorder())
    keys.use_store(None)


def test_a_bad_length_scale_is_refused(engine, tmp_path: Path) -> None:
    with pytest.raises(cloud.CloudEngineError, match="length_scale"):
        engine.transcribe(job_for(tmp_path, "mai2-openrouter-windows", options={"split_length_scale": 3}),
                          Recorder())


def test_the_retry_after_a_busy_gateway(monkeypatch, tmp_path: Path) -> None:
    statuses = iter([503, 200])

    def handler(request):
        status = next(statuses)
        return httpx.Response(status, json={"words": []} if status == 200 else {"error": "busy"})

    monkeypatch.setattr(mai2, "_client", lambda: httpx.Client(transport=httpx.MockTransport(handler)))
    clip = tmp_path / "clip.mp3"
    clip.write_bytes(b"mp3")
    waits = []
    assert mai2.Caller("openrouter", "k", sleep=waits.append)(clip, locale="ja") == {"words": []}
    assert waits == [mai2.RETRY_WAITS[0]]


# --- the launcher's environment -----------------------------------------------------------------------------


def run_in_launcher(tmp_path: Path, *, module: str, engine: str, case: str, pools: dict[str, str]) -> None:
    """In a fresh interpreter that cannot import FineSub, numpy, torch or onnx, `engines.<module>.<engine>`
    imports and runs a whole transcription of `case` (from its cached answers), and never loads `enhance/`.
    Shared by the three cloud engines' tests."""
    data, _ = load(case)
    task = tmp_path / "task"
    shutil.copytree(FIXTURES / "cases" / case / "cache", task / "cache")
    script = f"""
import builtins, sys
sys.path.insert(0, {str(ROOT / 'src')!r})
real = builtins.__import__
def guard(name, *a, **k):
    if name.split('.')[0] in {{'finesub', 'finesub_bootstrap', 'numpy', 'torch', 'onnx', 'onnxruntime'}}:
        raise ImportError('not in the launcher: ' + name)
    return real(name, *a, **k)
builtins.__import__ = guard
from pathlib import Path
from nonoka_x import config
from nonoka_x.transcription import ReadinessContext, TranscriptionJob, EventReporter, audio, read_transcript
from nonoka_x.transcription.engines import {module}
POOLS = {pools!r}
class Store:
    root = Path({str(tmp_path)!r})
    def key_pool(self, name):
        return (config.KeyEntry('', POOLS[name]),) if name in POOLS else ()
    def state_file(self, name):
        return self.root / 'state' / (name + '.json')
class Runtime:
    def component_ready(self, c): return c == 'media'
    def tool_path(self, n): return None
audio.probe_media_duration = lambda path, **k: {data['duration']!r}
engine = {module}.{engine}(Store(), Runtime())
assert engine.readiness(ReadinessContext()).ready
job = TranscriptionJob(source=Path({str(SOURCE)!r}), language={data['language']!r}, task_dir=Path({str(task)!r}),
                       cache_dir=Path({str(task / 'cache')!r}), target='final-srt')
result = engine.transcribe(job, EventReporter(lambda kind, payload: None))
assert read_transcript(result.transcript_json).lines
loaded = sorted(m for m in sys.modules if m.startswith('nonoka_x.transcription.enhance')
                or m.split('.')[0] in {{'numpy', 'torch', 'finesub', 'finesub_bootstrap', 'onnx'}})
assert not loaded, loaded
print('ok')
"""
    done = subprocess.run([sys.executable, "-I", "-c", script], cwd=ROOT, capture_output=True, text=True,
                          encoding="utf-8")
    assert done.returncode == 0 and done.stdout.strip() == "ok", done.stdout + done.stderr


def test_the_engine_runs_where_only_the_launcher_is_installed(tmp_path: Path) -> None:
    run_in_launcher(tmp_path, module="mai2", engine="Mai2Engine", case="mai2-openrouter-windows",
                    pools={"OPENROUTER_STT_KEY": "or-key"})
