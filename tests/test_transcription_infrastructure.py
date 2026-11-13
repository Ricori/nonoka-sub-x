"""Offline unit tests of what WP1 moved or wrote for the cloud engines: stitching, the call cache, keys and the
quota ledger (switching keys), the HTTP route, the media commands, the optional-repair gate, the facade additions.
Nothing here touches the network, ffmpeg or a model."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from nonoka_x import config  # noqa: E402
from nonoka_x.runtime import RuntimeManager, UnknownComponent, manifests  # noqa: E402
from nonoka_x.transcription import audio, cache, enhance, http, keys, reporting, stitch  # noqa: E402
from nonoka_x.transcription.engines import JointAsrError, gemini, mai2  # noqa: E402


class Recorder:
    def __init__(self) -> None:
        self.warnings: list[tuple[str, str]] = []
        self.logs: list[tuple[str, dict | None]] = []

    def warning(self, code, message):
        self.warnings.append((code, message))

    def log(self, message, fields=None):
        self.logs.append((message, fields))


# --- stitching ----------------------------------------------------------------------------------------------


def test_windows_overlap_and_cover_the_source() -> None:
    assert stitch.windows(0.0, 100.0, None, 10.0) == [(0.0, 100.0)]
    assert stitch.windows(0.0, 100.0, 200.0, 10.0) == [(0.0, 100.0)]
    assert stitch.windows(0.0, 250.0, 100.0, 20.0) == [(0.0, 100.0), (80.0, 180.0), (160.0, 250.0)]


def _w(start, end, text, speaker):
    return {"start": start, "end": end, "text": text, "speaker": speaker}


def test_stitch_links_speakers_across_windows_and_keeps_each_word_once() -> None:
    # Window 0 hears A as "1" and B as "2"; window 1 numbers them the other way round, and adds C.
    left = [_w(0.0, 1.0, "a1", "1"), _w(50.0, 51.0, "b1", "2"), _w(85.0, 86.0, "a2", "1"), _w(92.0, 93.0, "b2", "2")]
    right = [_w(85.0, 86.0, "a2", "2"), _w(92.0, 93.0, "b2", "1"), _w(110.0, 111.0, "c1", "3"),
             _w(120.0, 121.0, "a3", "2")]
    units = stitch.stitch([(0.0, 100.0, left), (80.0, 180.0, right)], segments=False)
    assert [u["text"] for u in units] == ["a1", "b1", "a2", "b2", "c1", "a3"]
    speakers = {u["text"]: u["speaker"] for u in units}
    assert speakers["a3"] == speakers["a1"] == "w0:1"
    assert speakers["b1"] == "w0:2"
    assert speakers["c1"] == "w1:3"  # unlinked: a new person, its window-local name


def test_handover_falls_in_a_gap_nobody_speaks_in() -> None:
    units = [_w(80.0, 89.0, "x", "1"), _w(91.0, 99.0, "y", "1")]
    assert 89.0 <= stitch.handover(units, 80.0, 100.0, 90.0) <= 91.0
    assert stitch.handover([_w(70.0, 110.0, "z", "1")], 80.0, 100.0, 90.0) == 90.0  # no gap: the target


def test_link_is_one_to_one_by_co_speaking_seconds() -> None:
    prev = {"a": [(0.0, 10.0)], "b": [(10.0, 12.0)]}
    cur = {"x": [(0.0, 9.0)], "y": [(9.0, 12.0)]}
    assert stitch.link(prev, cur) == [("a", "x"), ("b", "y")]


# --- the call cache -----------------------------------------------------------------------------------------


def _source(tmp_path: Path, content: bytes = b"audio") -> Path:
    path = tmp_path / "source.wav"
    path.write_bytes(content)
    return path


def test_cache_hit_spends_no_second_call(tmp_path: Path) -> None:
    src = _source(tmp_path)
    identity = {"provider": "p", "audio": cache.audio_fingerprint(src)}
    calls = []
    first = cache.CallCache(tmp_path / "c", "p", identity=identity)
    assert first.fetch("w-0.0-10.0", lambda: calls.append(1) or {"answer": 1}) == {"answer": 1}
    again = cache.CallCache(tmp_path / "c", "p", identity=identity)
    assert again.fetch("w-0.0-10.0", lambda: calls.append(2) or {"answer": 2}) == {"answer": 1}
    assert calls == [1]
    assert json.loads((tmp_path / "c" / "p" / cache.IDENTITY_FILE).read_text(encoding="utf-8")) == identity


def test_cache_miss_with_calls_off_raises(tmp_path: Path) -> None:
    src = _source(tmp_path)
    offline = cache.CallCache(tmp_path / "c", "p", identity={"audio": cache.audio_fingerprint(src)}, offline=True)
    with pytest.raises(cache.CacheMiss):
        offline.fetch("w-0.0-10.0", lambda: {"never": True})


def test_another_recording_discards_the_answers_and_says_so(tmp_path: Path) -> None:
    root = tmp_path / "c"
    cache.CallCache(root, "p", identity={"audio": "1:aaaa"}).fetch("w-0.0-10.0", lambda: {"old": True})
    recorder = Recorder()
    with reporting.reporting_to(recorder):
        fresh = cache.CallCache(root, "p", identity={"audio": "2:bbbb"})
        assert fresh.fetch("w-0.0-10.0", lambda: {"new": True}) == {"new": True}
    assert [code for code, _ in recorder.warnings] == ["cloud-cache-audio"]
    # Offline over another recording's answers: none is served, nothing is deleted.
    replay = cache.CallCache(root, "p", identity={"audio": "3:cccc"}, offline=True)
    with pytest.raises(cache.CacheMiss, match="another recording"):
        replay.fetch("w-0.0-10.0", lambda: {})
    assert (root / "p" / "w-0.0-10.0.raw.json").exists()


def test_other_identity_changes_only_warn(tmp_path: Path) -> None:
    root = tmp_path / "c"
    cache.CallCache(root, "p", identity={"audio": "1:a", "window": 540}).fetch("w", lambda: {"kept": True})
    recorder = Recorder()
    with reporting.reporting_to(recorder):
        assert cache.CallCache(root, "p", identity={"audio": "1:a", "window": 600}).load("w") == {"kept": True}
    assert [code for code, _ in recorder.warnings] == ["cloud-cache-identity"]


# --- keys and the quota ledger ------------------------------------------------------------------------------


class FakeStore:
    def __init__(self, root: Path, pools: dict[str, list[tuple[str, str]]]):
        self.root = root
        self.pools = pools

    def key_pool(self, name):
        return tuple(config.KeyEntry(label, value) for label, value in self.pools.get(name, ()))

    def state_file(self, name):
        return self.root / "state" / f"{name}.json"


@pytest.fixture
def store(tmp_path: Path):
    s = FakeStore(tmp_path, {
        keys.GEMINI_FREE: [("one", "k1"), ("two", "k2"), ("one", "dup"), ("three", "k3")],
        keys.MOSS_PRO: [("", "m1"), ("", "m2"), ("", "m3"), ("", "m4")],
        keys.OPENROUTER_STT_KEY: [("", "or-key")],
    })
    keys.use_store(s)
    yield s
    keys.use_store(None)


def test_pools_keep_order_drop_repeated_labels_and_cut_to_the_limit(store) -> None:
    assert [k.name for k in keys.pool(keys.GEMINI_FREE)] == ["one", "two"]
    moss = keys.pool(keys.MOSS_PRO)
    assert [k.key for k in moss] == ["m1", "m2", "m3"]
    assert moss[0].key_id.startswith("sha256:") and "m1" not in moss[0].key_id and "m1" not in repr(moss[0])
    assert keys.single(keys.OPENROUTER_STT_KEY) == "or-key"
    assert keys.single(keys.AZURE_SPEECH_KEY) is None


def test_without_a_store_the_keys_are_not_guessed() -> None:
    keys.use_store(None)
    with pytest.raises(keys.NoKeyStore):
        keys.pool(keys.GEMINI_FREE)


def test_the_new_key_names_are_registered() -> None:
    keys.register_key_specs()
    names = {spec.name for spec in config.registered_key_specs()}
    assert {keys.OPENROUTER_STT_KEY, keys.AZURE_SPEECH_KEY, keys.AZURE_SPEECH_ENDPOINT, keys.MOSS_PRO} <= names
    assert not any(name.startswith("FINESUB") for name in names)


def _clock(day: int):
    return lambda: datetime(2026, 11, day, 20, 0, tzinfo=timezone.utc)


def test_ledger_moves_to_the_next_key_once_one_is_spent(store, tmp_path: Path) -> None:
    pool = keys.pool(keys.GEMINI_FREE)
    ledger = keys.KeyLedger("gemini-35t", "m", keys.PACIFIC, clock=_clock(11))
    assert ledger.path == tmp_path / "state" / "cloud-asr-quota.json"
    assert keys.pick_key(pool, ledger, what="t").name == "one"
    ledger.record(pool[0], 60.0)
    assert ledger.usage(pool[0]) == {"calls": 1, "seconds": 60.0, "exhausted": False}
    ledger.mark_exhausted(pool[0])
    assert keys.pick_key(pool, ledger, what="t").name == "two"
    ledger.mark_exhausted(pool[1])
    with pytest.raises(keys.QuotaExhausted):
        keys.pick_key(pool, ledger, what="t")
    # The quota day turns over (Pacific midnight): every key is fresh again.
    tomorrow = keys.KeyLedger("gemini-35t", "m", keys.PACIFIC, clock=_clock(12))
    assert keys.pick_key(pool, tomorrow, what="t").name == "one"
    # Spent on one model, still good on another.
    assert keys.pick_key(pool, keys.KeyLedger("gemini-35t", "other", keys.PACIFIC, clock=_clock(11)), what="t").name == "one"


def test_reserve_and_refund_keep_the_account_honest(store) -> None:
    pool = keys.pool(keys.MOSS_PRO)
    ledger = keys.KeyLedger("moss-pro", "m", keys.BEIJING, clock=_clock(11))
    room = lambda entry, usage: usage["seconds"] + 1800 <= 3600  # noqa: E731
    first, day = ledger.reserve(pool, fits=room, seconds=1800, what="t")
    second, _ = ledger.reserve(pool, fits=room, seconds=1800, what="t")
    third, _ = ledger.reserve(pool, fits=room, seconds=1800, what="t")
    assert (first, second) == (pool[0], pool[0]) and third == pool[1]
    ledger.refund(third, 1800, day=day, exhausted=True)
    assert ledger.usage(pool[1]) == {"calls": 0, "seconds": 0.0, "exhausted": True}
    ledger.refund(first, 1800, day="2000-01-01")  # yesterday's call: nothing to take back
    assert ledger.usage(pool[0])["calls"] == 2


def test_an_unreadable_ledger_reads_as_empty(store, tmp_path: Path) -> None:
    path = tmp_path / "state" / "cloud-asr-quota.json"
    path.parent.mkdir(parents=True)
    path.write_text("{not json", encoding="utf-8")
    ledger = keys.KeyLedger("p", "m", keys.PACIFIC, clock=_clock(11))
    assert ledger.usage(keys.pool(keys.GEMINI_FREE)[0])["calls"] == 0
    ledger.record(keys.pool(keys.GEMINI_FREE)[0], 1.0)
    assert json.loads(path.read_text(encoding="utf-8"))["keys"]["p/m/one"]["calls"] == 1


def test_gemini_switches_key_when_one_reports_its_day_spent(store, monkeypatch) -> None:
    used = []

    def handler(request: httpx.Request) -> httpx.Response:
        key = request.headers.get("x-goog-api-key")
        if request.url.path.startswith("/upload"):
            if "X-Goog-Upload-URL" not in request.headers and request.headers.get("X-Goog-Upload-Command") == "start":
                return httpx.Response(200, headers={"X-Goog-Upload-URL": "https://upload.test/session"})
        if request.url.host == "upload.test":
            return httpx.Response(200, json={"file": {"uri": "files/1"}})
        used.append(key)
        if key == "k1":
            return httpx.Response(429, json={"error": {"message": "quota", "details": [{"quotaId": "RequestsPerDayPerProject"}]}})
        return httpx.Response(200, json={"status": "completed", "steps": []})

    monkeypatch.setattr(gemini, "_client", lambda: httpx.Client(transport=httpx.MockTransport(handler)))
    ledger = keys.KeyLedger("gemini-35t", gemini.MODEL, keys.PACIFIC, clock=_clock(11))
    caller = gemini.Caller(ledger=ledger, sleep=lambda s: None)
    clip = store.root / "clip.mp3"
    clip.write_bytes(b"mp3")
    assert caller(clip, language="ja-JP", seconds=540.0) == {"status": "completed", "steps": []}
    assert used == ["k1", "k2"]
    pool = keys.pool(keys.GEMINI_FREE)
    assert ledger.usage(pool[0])["exhausted"] and ledger.usage(pool[1])["calls"] == 1


def test_the_engine_picks_mai2s_route_from_nonokas_keys(store) -> None:
    """Was FineSub's joint stage's choice (`stage._mai2_caller`); the MAI2 engine chooses the same way (WP3a)."""
    caller = mai2.route(store)
    assert (caller.via, caller.key) == ("openrouter", "or-key")
    store.pools = {keys.AZURE_SPEECH_KEY: [("", "az")], keys.AZURE_SPEECH_ENDPOINT: [("", "https://r.test")]}
    caller = mai2.route(store)
    assert (caller.via, caller.key, caller.endpoint) == ("azure", "az", "https://r.test")
    store.pools = {}
    assert mai2.route(store) is None
    (issue,) = mai2.Mai2Engine(store, None).key_issues(store)
    assert issue.code == "missing_key" and "OPENROUTER_STT_KEY" in issue.message


def test_local_moss_reports_through_the_engines_error() -> None:
    """`moss_local` raises `engines.JointAsrError`, which lived in the joint stage before WP3 removed it."""
    import inspect

    from nonoka_x.transcription.engines import moss_local

    assert issubclass(JointAsrError, RuntimeError)
    assert "from .. import JointAsrError" in inspect.getsource(moss_local.transcribe)


# --- the HTTP route -----------------------------------------------------------------------------------------


def test_api_calls_follow_nonokas_route(monkeypatch) -> None:
    monkeypatch.setattr(http.fetch, "live_proxy", lambda: "http://127.0.0.1:7890")
    assert http.api_proxy() == "http://127.0.0.1:7890"
    assert http.websocket_kwargs("h") == {"http_proxy_host": "127.0.0.1", "http_proxy_port": 7890, "proxy_type": "http"}
    with http.api_http_client(httpx.Timeout(5.0)) as client:
        assert client._trust_env is False
    monkeypatch.setattr(http.fetch, "live_proxy", lambda: None)
    assert http.api_proxy() is None
    assert http.websocket_kwargs("h") == {"http_no_proxy": ["h"]}


def test_a_socks_route_goes_direct_with_one_warning(monkeypatch) -> None:
    monkeypatch.setattr(http.fetch, "live_proxy", lambda: "socks5://127.0.0.1:1080")
    monkeypatch.setattr(http, "_warned_socks", False)
    recorder = Recorder()
    with reporting.reporting_to(recorder):
        assert http.api_proxy() is None
        assert http.api_proxy() is None
    assert [code for code, _ in recorder.warnings] == ["api-proxy-socks"]


def test_retry_classification_is_by_type_and_status() -> None:
    assert http.retryable_status(429) and http.retryable_status(503) and http.retryable_status(425)
    assert not http.retryable_status(400) and not http.retryable_status(413)
    assert http.is_retryable_http_error(httpx.ReadTimeout("t"))
    assert not http.is_retryable_http_error(ValueError("x"))


# --- media ----------------------------------------------------------------------------------------------------


def test_the_upload_clip_is_cut_as_finesub_cut_it() -> None:
    # FineSub `media/ffmpeg.py:328`: 16 kHz mono 64 kbps MP3, -ss/-t before -i.
    assert audio.upload_mp3_command("ffmpeg", "in.wav", 540.0, 1080.5, "out.mp3") == [
        "ffmpeg", "-y", "-nostdin", "-loglevel", "error", "-ss", "540.000", "-t", "540.500",
        "-i", "in.wav", "-vn", "-ac", "1", "-ar", "16000", "-b:a", "64k", "out.mp3"]
    with pytest.raises(ValueError):
        audio.upload_mp3_command("ffmpeg", "in.wav", 5.0, 5.0, "out.mp3")


def test_media_tools_come_from_the_runtime_facade_never_path() -> None:
    audio.use_runtime(None)
    with pytest.raises(audio.MediaToolMissing):
        audio.tool("ffmpeg")
    assert audio.tool("ffmpeg", "C:/x/ffmpeg.exe") == "C:/x/ffmpeg.exe"
    audio.use_runtime(SimpleNamespace(tool_path=lambda name: Path(f"C:/tools/{name}.exe") if name == "ffmpeg" else None))
    try:
        assert audio.tool("ffmpeg") == str(Path("C:/tools/ffmpeg.exe"))
        with pytest.raises(audio.MediaToolMissing, match="ffprobe"):
            audio.tool("ffprobe")
    finally:
        audio.use_runtime(None)


# --- the optional repairs -------------------------------------------------------------------------------------


def test_the_layer_judgement_names_what_is_missing(monkeypatch) -> None:
    monkeypatch.setattr(enhance, "missing_modules", lambda *names: [n for n in names if n == "torch"])
    assert "torch" in enhance.unavailable("silero")
    assert enhance.unavailable("live") is None
    assert not enhance.layer_available()
    with pytest.raises(KeyError):
        enhance.unavailable("nothing")


def test_a_missing_model_makes_its_repair_unavailable(monkeypatch) -> None:
    monkeypatch.setattr(enhance, "missing_modules", lambda *names: [])
    audio.use_runtime(None)
    assert enhance.model_dir("speaker-embedding") is None
    assert enhance.unavailable("voiceprint") == "模型 speaker-embedding 没有安装"


def test_importing_the_package_imports_none_of_its_modules() -> None:
    import subprocess

    code = (f"import sys; sys.path.insert(0, {str(ROOT / 'src')!r}); "
            "import nonoka_x.transcription.enhance, nonoka_x.transcription.engines; "
            "loaded = [m for m in sys.modules if m.startswith(('nonoka_x.transcription.enhance.', "
            "'nonoka_x.transcription.engines.')) or m.split('.')[0] in {'numpy', 'torch', 'finesub'}]; "
            "assert not loaded, loaded")
    subprocess.run([sys.executable, "-I", "-c", code], check=True)


# --- the facades ---------------------------------------------------------------------------------------------


def test_config_state_file(tmp_path: Path) -> None:
    store = config.ConfigStore(SimpleNamespace(root=tmp_path))
    assert store.state_file("cloud-asr-quota") == tmp_path / "state" / "cloud-asr-quota.json"
    for bad in ("../x", "Cloud", "", "a/b"):
        with pytest.raises(ValueError):
            store.state_file(bad)


def test_runtime_model_dir_answers_from_the_model_layer_only_when_it_is_ready(tmp_path: Path) -> None:
    """WP-E3: a model's directory is inside the model layer's current version, and only while it checks out."""

    import os

    if os.name != "nt":
        pytest.skip("layers are Windows-only (tar.exe)")
    from nonoka_x.runtime import layers as L
    from nonoka_x.runtime.fetch import NetworkRoute
    from nonoka_x.runtime.paths import NonokaPaths
    from tests import layer_fakes as fakes

    built = {"models": fakes.build_layer(tmp_path / "built", "models")}
    pack = fakes.publish(built, tmp_path / "pack")
    (pack / L.OFFLINE_PACK_MARKER).write_text("{}", encoding="utf-8")
    layers = L.LayerManager(NonokaPaths.for_roots(tmp_path / "data"),
                            L.parse_index(fakes.index_bytes(built, "http://127.0.0.1:9/")),
                            offline=lambda: pack, routes=lambda: [NetworkRoute("direct", None)], in_use=lambda: False)
    manager = RuntimeManager(SimpleNamespace(layers=layers))
    assert manager.model_dir("whisper") is None

    root = layers.install("models").path
    snapshot = root / fakes.HF_SNAPSHOT
    assert manager.model_dir("whisper") == snapshot
    assert manager.model_dir("separator") == root / "audio-separator"
    assert manager.model_dir("moss-transcribe-diarize") is None  # in the manifest, not in the layer
    (snapshot / "config.json").unlink()
    layers.forget()
    assert manager.model_dir("whisper") is None  # the layer no longer checks out
    with pytest.raises(UnknownComponent):
        manager.model_dir("no-such-model")
    assert RuntimeManager(None).model_dir("speaker-embedding") is None


def test_reporting_folds_impact_into_the_message() -> None:
    recorder = Recorder()
    with reporting.reporting_to(recorder):
        reporting.current_reporter().warning("c", "m", impact="i", action="a")
        reporting.current_reporter().debug("d", {"k": 1})
    assert recorder.warnings == [("c", "m；影响：i；处理：a")]
    assert recorder.logs == [("d", {"k": 1})]
    reporting.current_reporter().warning("dropped", "nobody listens")  # unbound: silent
