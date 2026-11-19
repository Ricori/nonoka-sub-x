"""MAI-Transcribe-2 through OpenRouter or the user's own Azure Speech resource.

Two request formats, one answer shape (owner 2026-10-29: these are the two
routes there are, and a new endpoint will most likely speak one of them):

* OpenRouter (`microsoft/mai-transcribe-2`, routed to Azure): JSON with the
  audio base64 in `input_audio`, `verbose_json`, diarization as the Azure
  option under `provider.options`. Key: `OPENROUTER_STT_KEY` (`keys.py`).
* Azure fast transcription: multipart, the resource's endpoint and key.

Word times and speakers; words never overlap in time (MAI2 does not mark
overlapping speech), so no turn reordering is needed. A source longer than one
call's window is cut into windows overlapping by OVERLAP and stitched like
35t's. The windows are the lengths the experiment sent (Azure up to 100 min,
OpenRouter 30 min). Both fit what the service takes: Azure fast transcription
documents under 120 min and under 300 MB a file, and a 100-min window of the
upload MP3 (16 kHz mono 64 kbps) is about 48 MB. So a window is never halved: a
504 is a busy gateway, retried like any 5xx, and a 413 is an error.

The engine (`Mai2Engine`, WP3a) runs without repair: re-asking needs Silero and voiceprints (`enhance/`), which
the first version does not wire (plan 4.1 item 6). The repair below is kept, unreached, for a later phase.

Repair (`--asr-repair on`, `docs/joint-asr.md`「退化的修补」): MAI2 bills by audio time, so a gap Silero hears
speech in is asked of MAI2 again (`reask`): gaps closer than `repair.MERGE_WITHIN` as one span, REASK_PAD s of
audio either side. Only the words inside a gap are taken, less an edge that repeats the neighbours. The answer
numbers its speakers anew: a label is linked to the transcript's by the seconds both talk in the padding
(`stitch.link`), else by voice (`relink.Voices`), else it is `unknown` -- a few recovered words are not a new person.
"""

from __future__ import annotations

import base64
import json
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from .. import keys
from .. import lines as L
from .. import repair as R
from ..audio import extract_upload_mp3, probe_media_duration
from ..cache import CacheMiss, CallCache, audio_fingerprint
from ..http import CONNECT_TIMEOUT_SECONDS, RETRYABLE_TRANSPORT, api_http_client, retryable_status
from ..keys import OPENROUTER_STT_KEY
from ..reporting import current_reporter
from ..stitch import stitch, windows
from ..units import Transcript, Unit, turns_from_units
from . import MAI2
from .moss_pro import SpeechSpans

from .. import DIARIZATION_LEVELS, EngineEnvironment, EngineFeatures, EngineSpec, Issue, Reporter, TranscriptionJob
from .cloud import MEDIA, CloudEngine, CloudEngineError, guarded

if TYPE_CHECKING:
    from ..enhance.relink import Voiceprints

MODEL = "mai-transcribe-2"
OPENROUTER_API = "https://openrouter.ai/api/v1/audio/transcriptions"
AZURE_PATH = "/speechtotext/transcriptions:transcribe?api-version=2025-10-15"
OPENROUTER_KEY_ENV = OPENROUTER_STT_KEY
OVERLAP = 60.0
WINDOW = {"azure": 6000.0, "openrouter": 1800.0}
#: The task's language forced where the experiment forced it; the rest is auto-detected.
LOCALES = {"ja": "ja"}
#: The cache directory of the repair's re-asks, beside the windows'.
REPAIR_CACHE = f"{MAI2}-repair"
REASK_PAD = 10.0


class Mai2Error(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None):
        super().__init__(message)
        self.status = status


# --- reading an answer ------------------------------------------------------------------------------------


def _speaker(value) -> str | None:
    return None if value is None else f"S{value}"


def azure_words(answer: dict, offset: float) -> list[dict]:
    """Words of a fast-transcription answer; the phrase's speaker is the word's. (Phrase times are
    sometimes all zero; word times are not.)"""
    words = []
    for phrase in answer.get("phrases", []) or []:
        speaker = _speaker(phrase.get("speaker"))
        for w in phrase.get("words", []) or []:
            start = w["offsetMilliseconds"] / 1000 + offset
            words.append({"start": start, "end": start + w["durationMilliseconds"] / 1000,
                          "speaker": speaker, "text": w["text"]})
    return words


def openrouter_words(answer: dict, offset: float) -> list[dict]:
    return [{"start": w["start"] + offset, "end": w["end"] + offset, "speaker": _speaker(w.get("speaker")),
             "text": w.get("word", "")} for w in answer.get("words") or []]


def answer_words(answer: dict, offset: float) -> list[dict]:
    """Either route's answer, read by its own shape: a source under one window has the same cache name on
    both routes, so an answer cached on one is replayed on the other."""
    return azure_words(answer, offset) if "phrases" in answer else openrouter_words(answer, offset)


# --- calling ----------------------------------------------------------------------------------------------


def _client():
    import httpx

    return api_http_client(httpx.Timeout(1800.0, connect=CONNECT_TIMEOUT_SECONDS))


def azure_definition(locale: str | None) -> dict:
    d: dict = {"enhancedMode": {"enabled": True, "model": "MAI-Transcribe-2", "modelOptions": {"timestamps": "word"}},
               "diarization": {"enabled": True}}
    if locale:
        d["locales"] = [locale]
    return d


def openrouter_body(audio: Path, locale: str | None) -> dict:
    body: dict = {"model": "microsoft/mai-transcribe-2",
                  "input_audio": {"data": base64.b64encode(audio.read_bytes()).decode("ascii"),
                                  "format": audio.suffix.lstrip(".").lower()},
                  "response_format": "verbose_json", "timestamp_granularities": ["segment", "word"],
                  "provider": {"options": {"azure": {"diarization": {"enabled": True}}}}}
    if locale:
        body["language"] = locale.split("-")[0]
    return body


#: Seconds between attempts after a retryable status (`api_http.retryable_status`; Retry-After wins when the
#: service sends one) or no answer at all (`api_http.RETRYABLE_TRANSPORT`: a reset, a dropped proxy, a timeout).
RETRY_WAITS = (30.0, 60.0, 120.0)
MAX_RETRY_AFTER = 300.0


@dataclass(frozen=True)
class Caller:
    via: str  # "openrouter" | "azure"
    key: str
    endpoint: str | None = None  # the Azure resource, e.g. https://<name>.cognitiveservices.azure.com
    sleep: Callable[[float], None] = time.sleep

    def __call__(self, audio: Path, *, locale: str | None) -> dict:
        for attempt in range(len(RETRY_WAITS) + 1):
            try:
                r = self._once(audio, locale)
            except RETRYABLE_TRANSPORT:
                if attempt == len(RETRY_WAITS):
                    raise
                self.sleep(RETRY_WAITS[attempt])
                continue
            if r.status_code < 400:
                return r.json()
            if retryable_status(r.status_code) and attempt < len(RETRY_WAITS):
                after = r.headers.get("Retry-After", "")
                wait = float(after) if after.replace(".", "", 1).isdigit() else RETRY_WAITS[attempt]
                if wait > MAX_RETRY_AFTER:
                    raise Mai2Error(f"HTTP {r.status_code}: the service asks to wait {wait:.0f} s; try again later",
                                    status=r.status_code)
                self.sleep(wait)
                continue
            raise Mai2Error(f"HTTP {r.status_code}: {r.text[:2000]}", status=r.status_code)
        raise Mai2Error("no answer after retries")

    def _once(self, audio: Path, locale: str | None):
        with _client() as client:
            if self.via == "openrouter":
                return client.post(OPENROUTER_API, headers={"Authorization": f"Bearer {self.key}"},
                                   json=openrouter_body(audio, locale))
            if not self.endpoint:
                raise Mai2Error("MAI2 through Azure needs the resource's endpoint")
            with audio.open("rb") as f:
                return client.post(self.endpoint.rstrip("/") + AZURE_PATH,
                                   headers={"Ocp-Apim-Subscription-Key": self.key},
                                   files={"audio": (audio.name, f), "definition": (None, json.dumps(azure_definition(locale)))})


# --- one source -------------------------------------------------------------------------------------------


def identity(source: Path, via: str, language: str | None) -> dict:
    return {"provider": MAI2, "model": MODEL, "via": via, "window": WINDOW[via], "overlap": OVERLAP,
            "language": language, "audio": audio_fingerprint(source)}


def window_cache(source: Path, cache_root: str | Path, via: str, language: str | None, *,
                 offline: bool = False) -> CallCache:
    """The cache of the windows' answers (`cache_root/mai2/`); `language` is the task's, as `transcribe` takes it."""
    locale = LOCALES.get((language or "").lower())
    return CallCache(cache_root, MAI2, identity=identity(source, via, locale), offline=offline)


def window_name(start: float, end: float) -> str:
    return f"w-{start:.1f}-{end:.1f}"


def transcribe(
    source: str | Path,
    *,
    cache_root: str | Path,
    language: str | None,
    via: str,
    repair: str,
    caller: Caller | None = None,
    offline: bool = False,
    duration: float | None = None,
    speech: SpeechSpans | None = None,
    voiceprints: Voiceprints | None = None,
) -> Transcript:
    """The source's MAI2 transcript, words by start; every call cached under `cache_root/mai2/`, the repair's
    under `cache_root/mai2-repair/`. `repair` is `--asr-repair` (`repair.MODES`)."""

    src = Path(source)
    locale = LOCALES.get((language or "").lower())
    cache = window_cache(src, cache_root, via, language, offline=offline)
    total = duration if duration is not None else probe_media_duration(src)

    def window(start: float, end: float) -> tuple[float, float, list[dict]]:
        """(start, end, words) for the window."""

        def ask() -> dict:
            if caller is None:
                raise Mai2Error("no MAI2 key configured")
            current_reporter().debug("mai2 call", {"start": round(start, 1), "end": round(end, 1), "via": via})
            with tempfile.TemporaryDirectory(prefix="nonoka-mai2-") as tmp:
                clip = extract_upload_mp3(src, start, end, Path(tmp) / "window.mp3")
                return caller(clip, locale=locale)

        return start, end, answer_words(cache.fetch(window_name(start, end), ask), start)

    pieces = [window(lo, hi) for lo, hi in windows(0.0, total, WINDOW[via], OVERLAP)]
    units = stitch(pieces, segments=False)
    units.sort(key=lambda u: u["start"])  # words do not overlap; output order is time order
    log = [{"windows": [[round(lo, 1), round(hi, 1)] for lo, hi, _ in pieces]}] if len(pieces) > 1 else []
    if R.mode(repair) != "off":
        # The repair hears gaps through Silero and names voices (`enhance/`): never reached from the engine,
        # which runs without the Python environment layer (plan 4.1 item 6); kept for a later phase.
        from ..enhance.relink import VOICEPRINTS

        repair_cache = CallCache(cache_root, REPAIR_CACHE, offline=offline,
                                 identity=dict(identity(src, via, locale), provider=REPAIR_CACHE, pad=REASK_PAD))

        def ask(lo: float, hi: float) -> list[dict]:
            def call() -> dict:
                if caller is None:
                    raise Mai2Error("no MAI2 key configured")
                current_reporter().debug("mai2 repair call", {"start": round(lo, 1), "end": round(hi, 1), "via": via})
                with tempfile.TemporaryDirectory(prefix="nonoka-mai2-") as tmp:
                    return caller(extract_upload_mp3(src, lo, hi, Path(tmp) / "span.mp3"), locale=locale)

            return answer_words(repair_cache.fetch(f"r-{lo:.1f}-{hi:.1f}", call), lo)

        units, fixes = reask(units, total=total, ask=ask, speech=speech or SpeechSpans(src),
                             voiceprints=voiceprints or VOICEPRINTS)
        log += fixes
    return Transcript(MAI2, MODEL, total, "word", turns_from_units(Unit.from_dict(u) for u in units), log)


# --- repair -----------------------------------------------------------------------------------------------


def reask(units: list[dict], *, total: float, ask: Callable[[float, float], list[dict]], speech: SpeechSpans,
          voiceprints: Voiceprints) -> tuple[list[dict], list[dict]]:
    """The units (by start) with what re-asking each span of gaps recovered, and one log entry per span (module
    docstring). `ask(lo, hi)` is MAI2's words for [lo, hi], absolute times."""
    from ..enhance.relink import Voices

    voices = Voices(speech.pcm, voiceprints, R.blocks(units))
    log, new_units, failed = [], [], []
    # one window of the shorter route at most: a re-ask is not split like the first pass's windows
    for a, b, found in R.merged(R.gaps(units, total, speech.voiced), longest=min(WINDOW.values()) - 2 * REASK_PAD):
        lo, hi = max(0.0, a - REASK_PAD), min(total, b + REASK_PAD)
        entry = {"repair": "miss", "via": "mai2", "span": [round(lo, 2), round(hi, 2)],
                 "gaps": [g.as_list() for g in found]}
        log.append(entry)
        try:
            words = ask(lo, hi)
        except CacheMiss:
            entry["failed"] = "not cached (calls off)"
            continue
        except Exception as error:  # noqa: BLE001 -- a failed spot is given up, the task goes on (owner 2026-11-02)
            entry["failed"] = f"{type(error).__name__}: {error}"[:300]
            failed.append(entry)
            continue
        got = []
        for g in found:
            inside = [w for w in words if w["start"] >= g.start - R.PAD and w["end"] <= g.end + R.PAD]
            left, right = R.neighbours(units, g.start, g.end)
            first, end = R.trim_echo([w["text"] for w in inside], left, right)
            got += inside[first:end]
        mapping = R.relabel(units, words, lo, hi, voices)
        for w in got:
            w["speaker"] = mapping.get(w["speaker"])
        entry.update(inserted=len(got), speakers=mapping)
        new_units += got
    if failed:
        current_reporter().warning("cloud-repair-failed", f"MAI2 重问 {len(failed)} 处失败（{failed[0]['failed']}）",
                                   impact="这几处的空档照原样留着；metadata 里记了是哪几处")
    return sorted([*units, *new_units], key=lambda u: u["start"]), log


# --- the engine -------------------------------------------------------------------------------------------

ENGINE_ID = "mai2"
LABEL = "MAI-Transcribe-2"
#: MAI2's price per audio hour on both routes (OpenRouter and Azure, 2026-10), for the estimate before a run.
USD_PER_HOUR = 0.10

SPEC = EngineSpec(
    id=ENGINE_ID,
    label=LABEL,
    kind="cloud-api",
    features=EngineFeatures(speakers=True, word_timestamps=True, separate=False,
                            diarization_levels=DIARIZATION_LEVELS),
    # Either route: OpenRouter's key, or the user's Azure Speech resource (key and endpoint).
    keys=(keys.OPENROUTER_STT_KEY, keys.AZURE_SPEECH_KEY, keys.AZURE_SPEECH_ENDPOINT),
    environment=EngineEnvironment(components=(MEDIA,)),
    billing=f"按音频时长计费，约 ${USD_PER_HOUR:.2f}/小时",
)


def route(store: Any) -> Caller | None:
    """The route Nonoka's keys open (FineSub's joint stage chose the same way): OpenRouter when its key is saved,
    else the user's Azure Speech resource when both its key and endpoint are; None when neither."""
    key = keys.single(OPENROUTER_KEY_ENV, store=store)
    if key:
        return Caller("openrouter", key)
    endpoint = keys.single(keys.AZURE_SPEECH_ENDPOINT, store=store)
    key = keys.single(keys.AZURE_SPEECH_KEY, store=store)
    if endpoint and key:
        return Caller("azure", key, endpoint)
    return None


def billable_seconds(source: Path, *, cache_root: str | Path, via: str, language: str | None,
                     duration: float) -> float:
    """The audio a run will upload (owner 2026-11-12, WP3 question 4): every window not cached yet, at its full
    length -- the OVERLAP seconds two windows share are billed twice."""
    cache = window_cache(source, cache_root, via, language)
    return sum(hi - lo for lo, hi in windows(0.0, duration, WINDOW[via], OVERLAP)
               if cache.load(window_name(lo, hi)) is None)


def estimate_notice(seconds: float, *, via: str) -> str:
    hours = seconds / 3600
    return (f"MAI-Transcribe-2（{via}）按音频时长计费：这次要上传 {hours:.2f} 小时音频")


class Mai2Engine(CloudEngine):
    """MAI-Transcribe-2 as a `TranscriptionEngine` (WP3a). No gap re-asks: they need Silero (plan 4.1 item 6)."""

    spec = SPEC
    label = LABEL

    def key_issues(self, store: Any) -> list[Issue]:
        if route(store) is not None:
            return []
        return [Issue("missing_key", f"缺少服务商 key（OpenRouter 或者 Azure Speech）")]

    def run(self, job: TranscriptionJob, report: Reporter, *, duration: float,
            language: str | None) -> tuple[Transcript, dict[str, Any]]:
        caller = route(self._store)
        if caller is None:
            raise CloudEngineError(self.key_issues(self._store)[0].message)
        seconds = billable_seconds(job.source, cache_root=job.cache_dir, via=caller.via, language=language,
                                   duration=duration)
        current_reporter().warning("cloud-asr-cost", estimate_notice(seconds, via=caller.via))
        transcript = transcribe(job.source, cache_root=job.cache_dir, language=language, via=caller.via,
                                repair="off", caller=guarded(caller, label=LABEL, report=report), duration=duration)
        return transcript, {"via": caller.via, "repair": "off", "edge_pad": L.MAI2_EDGE_PAD}

    def build_lines(self, transcript: Transcript, *, duration: float, params, language: str | None) -> list[dict]:
        """FineSub's joint stage for MAI2 (arm M1): one DP over the words by start, then the edges widened."""
        units = [u.as_dict() for u in transcript.units()]
        segments, _ = L.word_lines(units, duration=duration, params=params, language=language)
        return L.widened(segments, pad=L.MAI2_EDGE_PAD, duration=duration)
