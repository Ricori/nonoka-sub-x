"""ElevenLabs Scribe v3, ported from FineSub 0.7.1's `speech/joint/scribe.py` and `scribe_keys.py`.

One multipart call per source (Scribe takes up to 15 h a file and splits anything over 8 minutes into up to four
parallel parts itself), the source as 16 kHz mono FLAC. The answer times every word -- in Japanese and Chinese every
character -- and labels it with a speaker; `spacing` tokens are dropped (the line builder joins words by language)
and so are `audio_event` tokens (laughter, applause), counted in the log. Keys: the `ELEVENLABS` pool, front to back
(`KeyPool`; a key needs the speech_to_text permission, and user_read to have its balance checked).

What the adapter fixes in the answer: a stretch of talk returned as one word token is split into words of its
own characters (`unglued`); a mark's time is not the word's (`_closing_marks`), a bracketed note for what Scribe
could not make out is dropped (`without_notes`; both in `answer_units`).

The engine (`ScribeEngine`) runs without FineSub's repair, like `Mai2Engine`: re-asking a timestamp collapse or a
glued word needs Silero and voiceprints (`enhance/`), which the launcher's environment does not have (plan 4.1
item 6). So a collapse is only recorded in the log, and a glued word keeps its split and its one speaker label,
with a warning. Lines are FineSub's for Scribe without the sentence-boundary model (not wired either): one DP over
the words by start, every line START_PAD s earlier.
"""

from __future__ import annotations

import json
import tempfile
import threading
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Sequence

from .. import keys
from .. import lines as L
from .. import repair as R
from ..audio import _clip_duration, _format_seconds, _run_ffmpeg, probe_media_duration, tool
from ..cache import CallCache, audio_fingerprint
from ..http import CONNECT_TIMEOUT_SECONDS, RETRYABLE_TRANSPORT, api_http_client, retryable_status
from ..reporting import current_reporter
from ..units import Transcript, Unit, turns_from_units
from . import SCRIBE

from .. import DIARIZATION_LEVELS, EngineEnvironment, EngineFeatures, EngineSpec, Issue, Reporter, TranscriptionJob
from .cloud import MEDIA, CloudEngine, CloudEngineError, guarded

MODEL = "scribe_v3"
API = "https://api.elevenlabs.io/v1/speech-to-text"
KEY_ENV = keys.ELEVENLABS
#: The single call's cache name: the whole source.
CALL = "full"
#: Scribe's own segmentation (`additional_formats`), kept in the cached answer for comparison only.
SEGMENT_EXPORT = {"format": "segmented_json", "segment_on_silence_longer_than_s": 0.5, "max_segment_duration_s": 7}
RETRY_WAITS = (30.0, 60.0, 120.0)
MAX_RETRY_AFTER = 300.0
#: Lines start this much earlier (`lines.widened`): against the human word times, Scribe's line starts are 0.17 s
#: later than production's (median); its ends are not late (FineSub's 2026-11-11 report, arm E1st).
START_PAD = 0.17
#: Scribe v3's price per audio hour, pay-as-you-go (ElevenLabs' price list, 2026-11-11), for the estimate.
USD_PER_HOUR = 0.20


class ScribeError(RuntimeError):
    pass


class ScribeRefused(ScribeError):
    """The key was refused (401, 402, 403): bad, without the permission, or out of credits -- the answer does not
    say which reliably, so the pool moves on rather than reads it."""

    def __init__(self, status: int, text: str):
        super().__init__(f"HTTP {status}: {text[:2000]}")
        self.status = status


# --- reading an answer ------------------------------------------------------------------------------------

#: Scribe writes a stretch it could not make out as ordinary words in brackets (「（聞き取り不能）」, character by
#: character in Japanese): a bracketed run of at most this many words is dropped, brackets included.
NOTE_MAX_WORDS = 12
#: Square brackets too: miyako's glued word opens on an unpaired 「[聞き取り不能）」.
_OPEN, _CLOSE = "（([", "）)]"


def without_notes(words: list[dict]) -> tuple[list[dict], int]:
    """(`words` minus the bracketed notes, how many notes were dropped)."""
    out, notes, i = [], 0, 0
    while i < len(words):
        text = (words[i].get("text") or "").strip()
        if words[i].get("type") == "word" and text[:1] in _OPEN:
            close = next((j for j in range(i, min(len(words), i + NOTE_MAX_WORDS + 2))
                          if any(ch in _CLOSE for ch in words[j].get("text") or "")), None)
            if close is not None:
                notes += 1
                last = words[close]["text"]
                rest = last[max(last.rfind(ch) for ch in _CLOSE) + 1:].strip()  # 「）。」 keeps its 「。」
                if rest:
                    out.append({**words[close], "text": rest})
                i = close + 1
                continue
        out.append(words[i])
        i += 1
    return out, notes


#: A timestamp collapse: at least COLLAPSE_WORDS words in a row (spacing aside) with zero duration, strung over at
#: least COLLAPSE_SPAN s. Scribe moved hina-73's 514-529 s this way to 559-593 s; a burst said at one point
#: (「え？」 seven times, 「哎呀」 over and over) is zero-length too but strung over a few seconds at most. The words
#: of the run's speaker starting within COLLAPSE_TAIL s of its last belong to it: hina-73's run ends on a 40 ms 「れ」.
COLLAPSE_WORDS = 15
COLLAPSE_SPAN = 15.0
COLLAPSE_TAIL = 0.3


def collapses(words: list[dict]) -> list[tuple[float, float]]:
    """(first start, last start) of every timestamp collapse in the answer's `words`."""
    words = [w for w in words if w.get("type") == "word" and w.get("start") is not None and w.get("end") is not None]
    out, i = [], 0
    while i < len(words):
        j = i
        while j < len(words) and float(words[j]["end"]) <= float(words[j]["start"]):
            j += 1
        first, last = float(words[i]["start"]), float(words[j - 1]["start"]) if j > i else 0.0
        if j - i >= COLLAPSE_WORDS and last - first >= COLLAPSE_SPAN:
            speaker = words[j - 1].get("speaker_id")
            while (j < len(words) and words[j].get("speaker_id") == speaker
                   and float(words[j]["start"]) <= last + COLLAPSE_TAIL):
                j += 1
            out.append((first, float(words[j - 1]["start"])))
        i = max(j, i + 1)
    return out


#: A glued word: Scribe once returned 983 s of talk (miyako, 4263 characters, from an unpaired 「[」 to the next 「]」)
#: as one word token -- its characters timed as usual, one speaker label on all of it, and to the line DP one unit
#: it cannot cut. A word token is glued when it holds GLUE_LETTERS letters and digits or more and is not one unit of
#: at most REPEAT_UNIT of them said over and over (REPEAT_SHARE of it in one run: `SPRITE` x31 in one 9 s token is a
#: repetition, left as it was), or when it holds GLUE_SENTENCES sentence ends with letters either side. Across 37
#: answers (11.8 h, 177 001 word tokens) an ordinary word has at most 16 and none a sentence end between letters;
#: duration tells nothing (an ordinary word spans up to 20.6 s).
GLUE_LETTERS = 40
GLUE_SENTENCES = 2
REPEAT_UNIT = 10
REPEAT_SHARE = 0.8
_SENTENCE_ENDS = set("。？！?!")


def is_glued(word: dict) -> bool:
    if word.get("type") != "word":
        return False
    text = word.get("text") or ""
    letters = R.key(text)
    if len(letters) >= GLUE_LETTERS and not any(
            R.longest_run(text, letters[:n]) * n >= REPEAT_SHARE * len(letters) for n in range(1, REPEAT_UNIT + 1)):
        return True
    at = [i for i, ch in enumerate(text) if R.key(ch)]
    return bool(at) and sum(ch in _SENTENCE_ENDS for ch in text[at[0]:at[-1]]) >= GLUE_SENTENCES


def _wide(ch: str) -> bool:
    return unicodedata.east_asian_width(ch) in "WF"


def split_glued(word: dict) -> list[dict]:
    """A glued word as words of its own characters, shaped as Scribe writes them: a CJK or kana character is a word,
    a run of narrow letters and digits with the marks inside it (`Stratford-on-Avon`, `HP0`) is one, other marks go
    on the front of the next word (「。光」) and the last ones on the last word; a space ends a word. Empty when its
    characters are not all timed."""
    chars = word.get("characters") or []
    if not chars or any(c.get("start") is None or c.get("end") is None for c in chars):
        return []
    words: list[list[dict]] = []
    cur: list[dict] = []
    pending: list[dict] = []
    for c in chars:
        ch = (c.get("text") or "")[:1]
        narrow_tail = bool(cur) and not pending and not _wide(cur[-1]["text"][-1:] or " ")
        if not ch.strip():
            cur = []
        elif unicodedata.category(ch)[0] in "LN":
            if narrow_tail and not _wide(ch):
                cur.append(c)
            else:
                cur = [*pending, c]
                pending = []
                words.append(cur)
        elif narrow_tail and not _wide(ch) or unicodedata.category(ch)[0] == "M" and cur and not pending:
            cur.append(c)
        else:
            cur = []
            pending.append(c)
    if pending:
        if words:
            words[-1].extend(pending)
        else:
            words.append(pending)
    return [{"type": "word", "text": "".join(c["text"] for c in cs), "start": cs[0]["start"], "end": cs[-1]["end"],
             "speaker_id": word.get("speaker_id"), "characters": cs} for cs in words]


@dataclass(frozen=True)
class Glued:
    """A glued word: its first and last character's start, its length."""

    first: float
    last: float
    chars: int


def unglued(words: list[dict]) -> tuple[list[dict], list[Glued]]:
    """(`words` with each glued word split (`split_glued`; one whose characters are not timed stays whole), the
    glued words)."""
    out, found = [], []
    for w in words:
        if not is_glued(w):
            out.append(w)
            continue
        parts = split_glued(w)
        starts = [float(c["start"]) for c in w.get("characters") or [] if c.get("start") is not None]
        found.append(Glued(min(starts, default=float(w["start"])), max(starts, default=float(w["start"])),
                           len(w.get("text") or "")))
        out += parts or [w]
    return out, found


def answer_units(words: list[dict], offset: float = 0.0) -> tuple[list[dict], int]:
    """(word units in output order, how many audio_event tokens were dropped) of an answer's `words`; bracketed
    notes dropped too (`without_notes`)."""
    units, events = [], 0
    words, _ = without_notes(words)
    for w in words:
        kind = w.get("type")
        if kind == "audio_event":
            events += 1
            continue
        if kind != "word" or w.get("start") is None or w.get("end") is None or not (w.get("text") or "").strip():
            continue
        text, start, end = w["text"].strip(), float(w["start"]), float(w["end"])
        # A mark's character can sit on the far side of a pause ("What？" spans 11 s, its 「？」 at the end;
        # 「。26」 starts on the 「。」 before the pause): a word's span is its other characters'.
        core = [c for c in w.get("characters") or [] if c.get("start") is not None and c.get("end") is not None
                and (c.get("text") or "").strip() and _leading_marks(c["text"]) == 0]
        if core:
            start, end = float(core[0]["start"]), max(float(core[-1]["end"]), float(core[-1]["start"]))
        lead = _closing_marks(text)
        if 0 < lead < len(text):  # a closing mark goes to the word before (the DP folds it in); an opening one stays
            units.append({"start": round(float(w["start"]) + offset, 3), "end": round(float(w["start"]) + offset, 3),
                          "text": text[:lead], "speaker": w.get("speaker_id")})
            text = text[lead:]
        units.append({"start": round(start + offset, 3), "end": round(end + offset, 3), "text": text,
                      "speaker": w.get("speaker_id")})
    return units, events


#: Marks that close what came before: a word starting with them gives them to the word before.
_CLOSING = set("。，、？！；：.,?!;:…")


def _closing_marks(text: str) -> int:
    """How many closing marks (sentence and clause ends, closing brackets and quotes) `text` starts with."""
    n = 0
    while n < len(text) and (text[n] in _CLOSING or unicodedata.category(text[n]) in ("Pe", "Pf")):
        n += 1
    return n


def _leading_marks(text: str) -> int:
    """How many punctuation characters `text` starts with."""
    n = 0
    while n < len(text) and unicodedata.category(text[n])[0] == "P":
        n += 1
    return n


# --- calling ----------------------------------------------------------------------------------------------


def form(language: str | None, seed: int) -> dict:
    data = {"model_id": MODEL, "diarize": "true", "timestamps_granularity": "character", "tag_audio_events": "true",
            "seed": str(seed), "additional_formats": json.dumps([SEGMENT_EXPORT])}
    if language:
        data["language_code"] = language.split("-")[0]
    return data


def upload_flac(source: Path, out: Path, span: tuple[float, float] | None = None) -> Path:
    """The source (or its [start, end) `span`) as Scribe is sent it: 16 kHz mono FLAC."""
    cut = ["-ss", _format_seconds(span[0]), "-t", _format_seconds(_clip_duration(*span))] if span else []
    _run_ffmpeg([tool("ffmpeg"), "-y", "-nostdin", "-loglevel", "error", *cut, "-i", str(source),
                 "-vn", "-ac", "1", "-ar", "16000", "-c:a", "flac", str(out)])
    return out


@dataclass(frozen=True)
class Caller:
    key: str
    seed: int = 20261111
    sleep: Callable[[float], None] = time.sleep

    def __call__(self, audio: Path, *, language: str | None) -> dict:
        import httpx

        for attempt in range(len(RETRY_WAITS) + 1):
            try:
                with api_http_client(httpx.Timeout(1800.0, connect=CONNECT_TIMEOUT_SECONDS)) as client, \
                        audio.open("rb") as f:
                    r = client.post(API, headers={"xi-api-key": self.key}, data=form(language, self.seed),
                                    files={"file": (audio.name, f, "audio/flac")})
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
                    raise ScribeError(f"HTTP {r.status_code}: the service asks to wait {wait:.0f} s")
                self.sleep(wait)
                continue
            if r.status_code in (401, 402, 403):
                raise ScribeRefused(r.status_code, r.text)
            raise ScribeError(f"HTTP {r.status_code}: {r.text[:2000]}")
        raise ScribeError("no answer after retries")


# --- which key a call goes out on -------------------------------------------------------------------------
#
# FineSub's `scribe_keys.py`, its rules unchanged (owner there, 2026-11-11). Up to three keys, in the order they
# were saved; the last is taken as the paid one (pay-as-you-go), as MOSS Pro's is. One call goes whole to one key.
# A key that may read its subscription (the `user_read` permission) is asked its balance before each call:
# credits left / the key's credits per audio hour = the hours it has left. A key short of the call is passed over
# for this call; one with less than SPENT_BELOW left is frozen until its credits refresh, and not asked again
# before then. The last key is never passed over for its balance. A key whose balance cannot be read is used
# until refused. Any refused key is left out for the rest of this process, not frozen. Credits per hour differ by
# plan: the free tier's 10k credits buy about 4.5 h (2222/h), other plans start from 1100, and every call of
# LEARN_MIN_SECONDS or more corrects the key's rate from its balance before and after (only a rise, within
# RATE_CLAMP of the start). Freezes and learned rates live in Nonoka's state file `STATE_NAME`, beside the other
# cloud engines' ledger, never FineSub's.

FAMILY = "elevenlabs"
SUBSCRIPTION_API = "https://api.elevenlabs.io/v1/user/subscription"
FREE_TIER = "free"
FREE_RATE = 2222.0
OTHER_RATE = 1100.0
RATE_CLAMP = (0.5, 2.0)
LEARN_MIN_SECONDS = 300.0
SPENT_BELOW = 300.0
FREEZE_WITHOUT_RESET = timedelta(hours=24)
#: The state file of the freezes and learned rates (`ConfigStore.state_file`).
STATE_NAME = "cloud-asr-scribe"

_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()
#: Keys refused in this process: left out for the rest of it.
_REFUSED: set[str] = set()
#: Keys whose refusal this process has warned about.
_WARNED: set[str] = set()


@dataclass(frozen=True)
class Balance:
    tier: str
    used: int
    limit: int
    reset: datetime | None

    def start_rate(self) -> float:
        return FREE_RATE if self.tier == FREE_TIER else OTHER_RATE


def subscription(key: str) -> tuple[str, Balance | None]:
    """("ok", the balance) | ("no_permission", None) for a key without `user_read` | ("unknown", None)."""
    import httpx

    try:
        with api_http_client(httpx.Timeout(30.0, connect=CONNECT_TIMEOUT_SECONDS)) as client:
            r = client.get(SUBSCRIPTION_API, headers={"xi-api-key": key})
    except RETRYABLE_TRANSPORT:
        return "unknown", None
    if r.status_code in (401, 403) and "missing_permissions" in r.text:
        return "no_permission", None
    if r.status_code != 200:
        return "unknown", None
    try:
        body = r.json()
        reset = body.get("next_character_count_reset_unix")
        return "ok", Balance(str(body.get("tier") or ""), int(body["character_count"]), int(body["character_limit"]),
                             datetime.fromtimestamp(int(reset), timezone.utc) if reset else None)
    except (ValueError, KeyError, TypeError):
        return "unknown", None


def _lock(key_id: str) -> threading.Lock:
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(key_id, threading.Lock())


def _when(value: Any) -> datetime | None:
    try:
        moment = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


class KeyPool:
    """A Scribe caller over the pool: `pool(audio, language=..., seconds=..., whole=...)` is the answer from the
    first key that takes it. `announce()` runs right before an upload (the cancel check and the upload notice)."""

    def __init__(self, entries: Sequence[keys.ApiKey], *, state_path: str | Path,
                 fetch: Callable[[str], tuple[str, Balance | None]] = subscription,
                 make_caller: Callable[[str], Callable[..., dict]] = Caller,
                 clock: Callable[[], datetime] | None = None, announce: Callable[[], Any] | None = None):
        self._entries = list(entries)
        self._state_path = Path(state_path)
        self._fetch, self._make_caller = fetch, make_caller
        self._clock = clock
        self._announce = announce

    def _now(self) -> datetime:
        return self._clock() if self._clock else datetime.now(timezone.utc)

    @staticmethod
    def _row(entry: keys.ApiKey) -> str:
        return f"{FAMILY}/{MODEL}/{entry.key_id}"

    def _state(self, entry: keys.ApiKey) -> dict:
        row = keys._read(self._state_path).get(self._row(entry))
        return row if isinstance(row, dict) else {}

    def frozen_until(self, entry: keys.ApiKey) -> datetime | None:
        until = _when(self._state(entry).get("frozen_until"))
        return until if until is not None and until > self._now() else None

    def rate(self, entry: keys.ApiKey, balance: Balance) -> float:
        learned = self._state(entry).get("rate")
        if isinstance(learned, dict) and learned.get("tier") == balance.tier \
                and isinstance(learned.get("rate"), (int, float)):
            return float(learned["rate"])
        return balance.start_rate()

    def left_seconds(self, entry: keys.ApiKey, balance: Balance) -> float:
        return max(0, balance.limit - balance.used) / self.rate(entry, balance) * 3600

    def __call__(self, audio: Path, *, language: str | None, seconds: float, whole: bool = False) -> dict:
        if not self._entries:
            raise ScribeError(f"Scribe needs a key in the {KEY_ENV} pool")
        passed: list[str] = []
        for i, entry in enumerate(self._entries):
            last = i == len(self._entries) - 1
            with _lock(entry.key_id):  # the checks too: a call that waited here sees what the one before it found
                if entry.key_id in _REFUSED:
                    passed.append(f"{entry.label}: refused earlier in this run")
                    continue
                held = None if last else self.frozen_until(entry)
                if held is not None:
                    passed.append(f"{entry.label}: frozen until {held:%Y-%m-%d %H:%M} UTC")
                    continue
                state, balance = self._fetch(entry.key)
                if balance is not None and not last:
                    left = self.left_seconds(entry, balance)
                    if left < SPENT_BELOW:
                        self._freeze(entry, balance)
                        passed.append(f"{entry.label}: spent, frozen until its credits refresh")
                        continue
                    if left < seconds:
                        passed.append(f"{entry.label}: about {left / 3600:.2f} h left, short of {seconds / 3600:.2f} h")
                        continue
                if whole:
                    self._say_cost(entry, balance, seconds)
                if self._announce is not None:
                    self._announce()
                try:
                    answer = self._make_caller(entry.key)(audio, language=language)
                except ScribeRefused as error:
                    _REFUSED.add(entry.key_id)
                    said = self._refused(entry, error, state)
                    passed.append(said)
                    if entry.key_id not in _WARNED:
                        _WARNED.add(entry.key_id)
                        current_reporter().warning("scribe-key-refused", f"Scribe：{said}",
                                                   impact="这次上传换下一把 key（或停下）")
                    if last:
                        raise ScribeError(f"Scribe: every {KEY_ENV} key was passed over or refused: "
                                          + "; ".join(passed) + self._refresh(balance)) from None
                    continue
                if balance is not None and seconds >= LEARN_MIN_SECONDS:
                    self._learn(entry, balance, seconds)
                return answer
        raise ScribeError(f"Scribe: every {KEY_ENV} key was passed over or refused: " + "; ".join(passed))

    def _freeze(self, entry: keys.ApiKey, balance: Balance) -> None:
        reset = balance.reset if balance.reset and balance.reset > self._now() else None
        until = reset or self._now() + FREEZE_WITHOUT_RESET
        with keys._rows(self._state_path) as rows:
            row = rows.get(self._row(entry))
            row = dict(row) if isinstance(row, dict) else {}
            row["frozen_until"] = until.isoformat(timespec="seconds")
            rows[self._row(entry)] = row

    @staticmethod
    def _refused(entry: keys.ApiKey, error: ScribeRefused, state: str) -> str:
        said = f"key {entry.label} 被拒（HTTP {error.status}），这次运行不再用它"
        if state == "no_permission":
            said += "；给这把 key 开 User: Read 权限，就能在上传前查余额"
        elif state != "ok":
            said += "（余额也查不到，请检查 key）"
        else:
            said += "，但余额显示还有：请检查它有没有 Speech to Text 权限（或者别的任务刚用光了它）"
        return said

    @staticmethod
    def _refresh(balance: Balance | None) -> str:
        if balance is None or balance.reset is None:
            return ""
        return f"; the last key's credits refresh {balance.reset:%Y-%m-%d %H:%M} UTC"

    def _say_cost(self, entry: keys.ApiKey, balance: Balance | None, seconds: float) -> None:
        hours = seconds / 3600
        if balance is None:
            message = f"Scribe 按音频时长计费：这次用 key {entry.label} 上传 {hours:.2f} 小时音频"
        else:
            left = self.left_seconds(entry, balance) / 3600
            if balance.tier == FREE_TIER and left >= hours:
                message = (f"Scribe：这次 {hours:.2f} 小时用 key {entry.label} 的免费额度"
                           f"（估计本月还剩约 {left:.1f} 小时）")
            elif balance.tier == FREE_TIER:
                message = (f"Scribe：key {entry.label} 是最后一把，免费额度约 {left:.1f} 小时，"
                           f"不够这次的 {hours:.2f} 小时，照样上传（账户开了按量付费时，超出部分约 "
                           f"${USD_PER_HOUR:.2f}/小时）")
            else:
                message = (f"Scribe：这次 {hours:.2f} 小时用 key {entry.label}（{balance.tier} 套餐），"
                           f"约 {hours * self.rate(entry, balance):.0f} 积分（估计还剩约 {left:.1f} 小时）")
        current_reporter().warning("cloud-asr-cost", message)

    def _learn(self, entry: keys.ApiKey, before: Balance, seconds: float) -> None:
        state, after = self._fetch(entry.key)
        if state != "ok" or after is None or after.tier != before.tier or after.used <= before.used:
            return
        low, high = (before.start_rate() * k for k in RATE_CLAMP)
        rate = min(high, max(low, (after.used - before.used) / (seconds / 3600)))
        with keys._rows(self._state_path) as rows:
            row = rows.get(self._row(entry))
            row = dict(row) if isinstance(row, dict) else {}
            row["rate"] = {"tier": after.tier, "rate": round(rate, 1)}
            rows[self._row(entry)] = row


# --- the transcript ---------------------------------------------------------------------------------------


def identity(source: Path, language: str | None) -> dict:
    return {"provider": SCRIBE, "model": MODEL, "language": language, "audio": audio_fingerprint(source)}


def transcribe(source: str | Path, *, cache_root: str | Path, language: str | None,
               caller: Callable[..., dict] | None = None, offline: bool = False,
               duration: float | None = None) -> Transcript:
    """The source's Scribe transcript, words in output order; the call cached under `cache_root/scribe/` (the
    same name as FineSub's, so an answer cached there is read back)."""
    src = Path(source)
    cache = CallCache(cache_root, SCRIBE, identity=identity(src, language), offline=offline)
    total = duration if duration is not None else probe_media_duration(src)

    def call() -> dict:
        if caller is None:
            raise ScribeError(f"Scribe needs a key in the {KEY_ENV} pool")
        current_reporter().debug("scribe call", {"span": [0.0, round(total, 1)]})
        with tempfile.TemporaryDirectory(prefix="nonoka-scribe-") as tmp:
            return caller(upload_flac(src, Path(tmp) / "audio.flac"), language=language, seconds=total, whole=True)

    answer = cache.fetch(CALL, call)
    words, glued = unglued(answer.get("words") or [])
    units, events = answer_units(words)
    log: list[dict] = [{"audio_events_dropped": events}] if events else []
    found = collapses(words)
    if found:
        log.append({"collapses": [[round(a, 2), round(b, 2)] for a, b in found]})
    log += [{"glued": [round(g.first, 2), round(g.last, 2)], "chars": g.chars, "via": "split"} for g in glued]
    _warn(log)
    return Transcript(SCRIBE, MODEL, total, "word", turns_from_units(Unit.from_dict(u) for u in units), log)


def _warn(log: list[dict]) -> None:
    """One warning for the glued words left split (their speakers are one label), one for the collapses."""
    split = [e for e in log if "glued" in e]
    if split:
        where = "、".join(f"{e['glued'][0]:.0f}–{e['glued'][1]:.0f} 秒（{e['chars']} 字）" for e in split)
        current_reporter().warning(
            "scribe-glued", f"Scribe 把 {len(split)} 段话粘成了一个词（{where}），已按逐字时间拆开",
            impact="这几段的说话人都记成了同一个人，不可信")
    spans = [span for e in log for span in e.get("collapses", ())]
    if spans:
        where = "、".join(f"{a:.0f}–{b:.0f} 秒" for a, b in spans)
        current_reporter().warning(
            "scribe-collapse", f"Scribe 有 {len(spans)} 处时间戳挤在了一起（{where}）",
            impact="这几处的台词时间不准，可能挪到了别处")


# --- the engine -------------------------------------------------------------------------------------------

ENGINE_ID = "scribe"
LABEL = "ElevenLabs Scribe"

SPEC = EngineSpec(
    id=ENGINE_ID,
    label=LABEL,
    kind="cloud-api",
    features=EngineFeatures(speakers=True, word_timestamps=True, separate=False,
                            diarization_levels=DIARIZATION_LEVELS),
    keys=(keys.ELEVENLABS,),
    environment=EngineEnvironment(components=(MEDIA,)),
    billing=f"免费档每月约 4.5 小时，之后约 ${USD_PER_HOUR:.2f}/小时",
)


class ScribeEngine(CloudEngine):
    """ElevenLabs Scribe v3 as a `TranscriptionEngine`. No re-asks: they need Silero and voiceprints (module
    docstring)."""

    spec = SPEC
    label = LABEL

    def key_issues(self, store: Any) -> list[Issue]:
        if keys.pool(keys.ELEVENLABS, store=store):
            return []
        return [Issue("missing_key", "缺少 ElevenLabs 的 key")]

    def caller(self, report: Reporter) -> KeyPool:
        """The pool's keys and the state file beside the store, read once per run."""
        return KeyPool(keys.pool(keys.ELEVENLABS, store=self._store),
                       state_path=self._store.state_file(STATE_NAME),
                       announce=guarded(lambda: None, label=LABEL, report=report))

    def run(self, job: TranscriptionJob, report: Reporter, *, duration: float,
            language: str | None) -> tuple[Transcript, dict[str, Any]]:
        if not keys.pool(keys.ELEVENLABS, store=self._store):
            raise CloudEngineError(self.key_issues(self._store)[0].message)
        transcript = transcribe(job.source, cache_root=job.cache_dir, language=language,
                                caller=self.caller(report), duration=duration)
        return transcript, {"repair": "off", "edge_pad": [START_PAD, 0.0]}

    def build_lines(self, transcript: Transcript, *, duration: float, params, language: str | None) -> list[dict]:
        """FineSub's joint stage for Scribe, less the sentence-boundary model: one DP over the words by start,
        then every start START_PAD s earlier."""
        units = [u.as_dict() for u in transcript.units()]
        segments, _ = L.word_lines(units, duration=duration, params=params, language=language)
        return L.widened(segments, pad=START_PAD, end_pad=0.0, duration=duration)


__all__ = [
    "API",
    "MODEL",
    "SPEC",
    "Balance",
    "Caller",
    "KeyPool",
    "ScribeEngine",
    "ScribeError",
    "ScribeRefused",
    "answer_units",
    "collapses",
    "is_glued",
    "split_glued",
    "subscription",
    "transcribe",
    "unglued",
    "without_notes",
]
