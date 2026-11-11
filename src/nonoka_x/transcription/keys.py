"""The cloud ASR engines' keys, and the local account of each key's day.

Was FineSub's `speech/joint/quota.py` plus what the backends read through `finesub.api_keys` (WP1).

**Keys** come from Nonoka's own store through the `nonoka_x.config` facade (`use_store`), never from the process
environment or FineSub's `.env` (plan 4.5 item 30). Pool names:

- `GEMINI_FREE` (35t and its Live repair) -- the first `POOL_LIMITS` of them, as FineSub's `recommended_max`
  cut its pool; FineSub's `[pools]` / `[providers]` selection is not read;
- `MOSS_PRO` -- the first 3;
- `OPENROUTER_STT_KEY`, or `AZURE_SPEECH_KEY` with `AZURE_SPEECH_ENDPOINT` (MAI2) -- the first entry.

The last four are not settings keys yet; they are registered with `config.register_key_spec` on first use and
read as empty until Nonoka's secret store holds them (WP-R4, WP5). Their names in FineSub's `.env`, for the
migration of old installs (WP-R4's `config/migrate.py`): `FINESUB_KEY_OPENROUTER_STT` -> `OPENROUTER_STT_KEY`,
`FINESUB_KEY_AZURE_SPEECH` -> `AZURE_SPEECH_KEY`, `FINESUB_AZURE_SPEECH_ENDPOINT` -> `AZURE_SPEECH_ENDPOINT`;
`MOSS_PRO` and `GEMINI_FREE` keep their names.

**The ledger.** One key is used until its day is spent, then the next in pool order -- never alternating, so a
failure stays attributable to one key. "Spent" is recorded per (provider, model, key): 35t's daily requests are
not the LLM layer's, and a key spent on one model may still serve another. The account lives in Nonoka's own state
file (`<data dir>/state/cloud-asr-quota.json`, `ConfigStore.state_file`), under a lock, and resets when the
provider's quota day turns over. The rules are FineSub's, unchanged.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import threading
from typing import Any, Callable, Iterator, Sequence

from nonoka_x import config
from nonoka_x.runtime.fsops import holding_lock, write_atomic

GEMINI_FREE = "GEMINI_FREE"
MOSS_PRO = "MOSS_PRO"
OPENROUTER_STT_KEY = "OPENROUTER_STT_KEY"
AZURE_SPEECH_KEY = "AZURE_SPEECH_KEY"
AZURE_SPEECH_ENDPOINT = "AZURE_SPEECH_ENDPOINT"

#: How many keys of a pool are used, in order (FineSub `api_keys.POOL_SPECS[...].recommended_max`).
POOL_LIMITS = {GEMINI_FREE: 2, MOSS_PRO: 3}

#: The keys these engines need that the settings do not know yet (registered on first use).
KEY_SPECS = (
    config.KeySpec(OPENROUTER_STT_KEY, "OpenRouter（语音转写）", "MAI-Transcribe-2 经 OpenRouter"),
    config.KeySpec(AZURE_SPEECH_KEY, "Azure Speech", "MAI-Transcribe-2 经自己的 Azure Speech 资源"),
    config.KeySpec(AZURE_SPEECH_ENDPOINT, "Azure Speech 端点", "Azure Speech 资源的地址（不是密钥）"),
    config.KeySpec(MOSS_PRO, "MOSS Pro 池", "MOSS Pro 转写"),
)

#: The state file of the ledger (`ConfigStore.state_file`).
STATE_NAME = "cloud-asr-quota"

#: The zone whose midnight starts a provider's quota day.
PACIFIC = "America/Los_Angeles"  # Gemini
BEIJING = "Asia/Shanghai"  # MOSI
_FIXED = {PACIFIC: -8, BEIJING: 8}


class QuotaExhausted(RuntimeError):
    """Every key of the pool has spent its day (or none fits the call)."""


class NoKeyStore(RuntimeError):
    """No key store was handed to the cloud ASR engines (`use_store`)."""


# --- keys ---------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ApiKey:
    """One key of a pool (FineSub `api_keys.ApiKeyEntry`): `name` is its label, empty when the pool is a list."""

    name: str
    key: str
    named: bool = True

    @property
    def key_id(self) -> str:
        """What the ledger records it by: the label, or a digest of an unlabelled key (never the key)."""

        if self.named:
            return self.name
        digest = hashlib.sha256(self.key.encode("utf-8")).hexdigest()[:12]
        return f"sha256:{digest}"

    @property
    def label(self) -> str:
        if self.named:
            return self.name
        return self.key[-6:] if len(self.key) >= 6 else self.key

    def __repr__(self) -> str:  # never print a secret
        return f"ApiKey(name={self.name!r}, key=<redacted>, named={self.named})"


_store: Any | None = None
_store_lock = threading.Lock()
_registered = False


def use_store(store: Any | None) -> None:
    """Read keys from `store` (a `nonoka_x.config.ConfigStore`) and keep the ledger beside it; None forgets it."""

    global _store
    with _store_lock:
        _store = store


def current_store() -> Any:
    with _store_lock:
        store = _store
    if store is None:
        raise NoKeyStore("no key store was handed to the cloud ASR engines")
    return store


def register_key_specs() -> None:
    """Register `KEY_SPECS` with the config facade (once; a no-op for a name the settings already know)."""

    global _registered
    if _registered:
        return
    known = {spec.name for spec in config.key_specs()}
    for spec in KEY_SPECS:
        if spec.name not in known:
            config.register_key_spec(spec)
    _registered = True


def pool(name: str, *, store: Any | None = None) -> list[ApiKey]:
    """The keys saved under `name`, in the order they were entered, cut to `POOL_LIMITS` (module docstring).

    Labelled entries keep the first of each label, as FineSub's pool parser did."""

    register_key_specs()
    entries = (store or current_store()).key_pool(name)
    keys: list[ApiKey] = []
    seen: set[str] = set()
    for entry in entries:
        if not entry.value:
            continue
        if entry.label:
            if entry.label in seen:
                continue
            seen.add(entry.label)
            keys.append(ApiKey(entry.label, entry.value))
        else:
            keys.append(ApiKey("", entry.value, named=False))
    limit = POOL_LIMITS.get(name)
    return keys if limit is None else keys[:limit]


def single(name: str, *, store: Any | None = None) -> str | None:
    """The first value saved under `name` (a one-key name, or an endpoint), or None."""

    entries = pool(name, store=store)
    return entries[0].key if entries else None


# --- the ledger ---------------------------------------------------------------------------------------------


def quota_day(zone: str, now: datetime | None = None) -> str:
    moment = now or datetime.now(timezone.utc)
    try:
        from zoneinfo import ZoneInfo

        local = moment.astimezone(ZoneInfo(zone))
    except Exception:
        # No tz database (Windows without tzdata): standard time. For Pacific
        # that turns the day over an hour late in summer -- a key waits an hour
        # longer, it is never used before it refreshed.
        local = moment.astimezone(timezone(timedelta(hours=_FIXED[zone])))
    return local.date().isoformat()


def ledger_path(store: Any | None = None) -> Path:
    return (store or current_store()).state_file(STATE_NAME)


def _read(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}  # unreadable state reads as empty, as FineSub's state file did
    rows = data.get("keys") if isinstance(data, dict) else None
    return dict(rows) if isinstance(rows, dict) else {}


@contextmanager
def _rows(path: Path) -> Iterator[dict[str, Any]]:
    """The ledger's rows, mutated under the file's lock and written back whole."""

    with holding_lock(path.with_name(path.name + ".lock")):
        rows = _read(path)
        yield rows
        write_atomic(path, json.dumps({"keys": rows}, ensure_ascii=False, indent=1, sort_keys=True))


class KeyLedger:
    def __init__(self, provider: str, model: str, zone: str, *, state_path: str | Path | None = None,
                 clock: Callable[[], datetime] | None = None):
        self.provider, self.model, self.zone = provider, model, zone
        self._state_path = Path(state_path) if state_path is not None else None
        self._clock = clock

    @property
    def path(self) -> Path:
        return self._state_path if self._state_path is not None else ledger_path()

    def _key(self, entry: ApiKey) -> str:
        return f"{self.provider}/{self.model}/{entry.key_id}"

    def _today(self) -> str:
        return quota_day(self.zone, self._clock() if self._clock else None)

    def usage(self, entry: ApiKey) -> dict:
        """{calls, seconds, exhausted} for today; zeros when the day turned over."""

        return _usage(_read(self.path).get(self._key(entry)), self._today())

    def _update(self, entry: ApiKey, change: Callable[[dict], None]) -> None:
        today = self._today()
        with _rows(self.path) as section:
            row = section.get(self._key(entry))
            if not isinstance(row, dict) or row.get("day") != today:
                row = {"day": today, "calls": 0, "seconds": 0.0, "exhausted": False}
            change(row)
            section[self._key(entry)] = row

    def record(self, entry: ApiKey, seconds: float) -> None:
        """One billed call of `seconds` of audio."""

        self._update(entry, lambda row: _count(row, +1, seconds))

    def reserve(self, entries: Sequence[ApiKey], *, fits: Callable[[ApiKey, dict], bool], seconds: float,
                what: str) -> tuple[ApiKey, str]:
        """`pick_key` and `record` in one locked read-modify-write: the call is on the account before it is
        made, so batch items calling at once cannot each see the same room left on a key. Returns the key
        and the quota day the call went on, which `refund` needs to take back a call the server did not
        take. `fits(entry, usage)` says whether that key has room for the call."""

        if not entries:
            raise QuotaExhausted(f"{what}: no key is configured")
        today = self._today()
        with _rows(self.path) as section:
            for entry in entries:
                row = section.get(self._key(entry))
                usage = _usage(row, today)
                if not usage["exhausted"] and fits(entry, usage):
                    row = row if isinstance(row, dict) and row.get("day") == today else {
                        "day": today, "calls": 0, "seconds": 0.0, "exhausted": False}
                    _count(row, +1, seconds)
                    section[self._key(entry)] = row
                    return entry, today
        raise QuotaExhausted(f"{what}: every key has spent today's quota")

    def refund(self, entry: ApiKey, seconds: float, *, day: str, exhausted: bool = False) -> None:
        """Take back a call reserved on quota `day`; `exhausted` also marks the key spent, in the same write.
        Once that day has turned over there is nothing to take back: the new day's account holds other calls,
        and a refusal of yesterday's call says nothing about today's quota."""

        with _rows(self.path) as section:
            row = section.get(self._key(entry))
            if not isinstance(row, dict) or row.get("day") != day:
                return
            _count(row, -1, seconds)
            row["calls"], row["seconds"] = max(0, row["calls"]), max(0.0, row["seconds"])
            if exhausted:
                row["exhausted"] = True
            section[self._key(entry)] = row

    def mark_exhausted(self, entry: ApiKey) -> None:
        self._update(entry, lambda row: row.__setitem__("exhausted", True))


def _usage(row, today: str) -> dict:
    if not isinstance(row, dict) or row.get("day") != today:
        return {"calls": 0, "seconds": 0.0, "exhausted": False}
    return {"calls": int(row.get("calls", 0)), "seconds": float(row.get("seconds", 0.0)),
            "exhausted": bool(row.get("exhausted", False))}


def _count(row: dict, calls: int, seconds: float) -> None:
    row["calls"] = int(row.get("calls", 0)) + calls
    row["seconds"] = round(float(row.get("seconds", 0.0)) + calls * seconds, 3)


def pick_key(
    entries: Sequence[ApiKey],
    ledger: KeyLedger,
    *,
    what: str,
) -> ApiKey:
    """The first key (pool order) not spent today."""

    if not entries:
        raise QuotaExhausted(f"{what}: no key is configured")
    for entry in entries:
        usage = ledger.usage(entry)
        if not usage["exhausted"]:
            return entry
    raise QuotaExhausted(f"{what}: every key has spent today's quota")
