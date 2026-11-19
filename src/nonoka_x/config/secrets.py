"""Nonoka's key store, `<data dir>\\secrets.json`, and the envelope that protects it.

The envelope is FineSub's (`docs/runtime-and-config.md` D2, 2.2), copied from
FineSub v0.7.0 `finesub_bootstrap/secrets.py` (7123055; the original now
ships in `engines/finesub-transcribe/src/finesub_bootstrap/`): a random 32-byte master key, kept as one keyring token, seals every
key value with a stream cipher plus MAC derived from it. How the master is kept
is the one thing that differs by platform, and the token's prefix says which:

- ``finesub$kek$v1$`` -- Windows. DPAPI, bound to the current Windows account
  (entropy ``b"finesub"``), wraps the master; the blob is then obfuscated so
  generic DPAPI harvesters do not find it. Wording elsewhere must say "绑定当前
  Windows 账户的保护", never unqualified "加密".
- ``finesub$obf$v1$`` -- every other platform. The master is only obfuscated
  and the file is written ``0600``; anyone with the file and this source can
  open it. Wording must say "混淆", never "加密".

The algorithms, prefixes, salts and lengths are FineSub's byte for byte, so a
value sealed here opens with FineSub's own code and the other way round (the
legacy `.env` migration, `config.migrate`, relies on it; so does the derived
`.env` FineSub reads until WP-R5, `nonoka_x.settings`). What changed is the
container: instead of a `.env` file, Nonoka keeps

    {"schema": 1, "keyring": "<finesub$kek$v1$... or finesub$obf$v1$...>",
     "values": {"<NAME>": "<the value as entered, key material replaced by fs$...>"}}

A pool's ``{label:key,label:key}`` / ``key,key`` spelling stays in the value and
only the key material is sealed (FineSub's `_value_spans` rules), so the settings
page can still show each key masked and say whether it is protected.

The `.env` line layer is here too, for the two callers that still need FineSub's
file format: reading an old install's `finesub.env` (`config.migrate`) and
writing the `.env` FineSub reads in the transition (`nonoka_x.settings`). The
name of the keyring line is a parameter: it is FineSub's, and only those two
modules name it.

What none of this protects against: code running as the current user -- the
program must decrypt without user input. No key value is ever logged: warnings
name the variable, never its value.
"""

from __future__ import annotations

import binascii
import ctypes
import hashlib
import hmac
import json
import os
import re
import sys
from base64 import urlsafe_b64decode, urlsafe_b64encode
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from nonoka_x.runtime.fsops import RECORD_REPLACE, holding_lock, replace_path

SCHEMA = 1
FILE_NAME = "secrets.json"

KEK_PREFIX = "finesub$kek$v1$"
OBF_PREFIX = "finesub$obf$v1$"
VALUE_PREFIX = "fs$"

# Two orthogonal uses of the same fixed string: DPAPI entropy (a generic
# harvester that finds the blob still cannot decrypt it) and the keystream
# for the keyring obfuscation layer (so the blob is not found at all).
_ENTROPY = b"finesub"

_MASTER_LEN = 32
_KEK_SALT_LEN = 16
_VALUE_SALT_LEN = 12
_TAG_LEN = 8

# The ciphertext alphabet is base64url without padding on purpose: it shares
# no character with the container syntax (: , " { }), so a value that failed
# to decrypt can never be silently re-split into garbage named keys.
_TOKEN_RE = re.compile(r"fs\$[A-Za-z0-9_-]+")

_LOCK_TIMEOUT_SECONDS = 10.0

#: Values longer than this are refused (the settings page's limit).
MAX_VALUE_LENGTH = 32_768

_REVEAL_HINT = "请在原机器上重新填写，或删除无法解密的值后重新填写；全部重填后保护会自动恢复。"


class ProtectionUnavailable(RuntimeError):
    """Wrapping is impossible here: crypt32 is unusable, or a backend refused."""


class SecretUnreadable(ValueError):
    """A ciphertext that cannot be opened: other machine/account, or corrupt."""


class SecretStoreError(RuntimeError):
    """`secrets.json` cannot be read as a key store (bad JSON, wrong schema)."""


def _b64encode(data: bytes) -> str:
    return urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64decode(text: str) -> bytes:
    return urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _xor(data: bytes, pad: bytes) -> bytes:
    return bytes(a ^ b for a, b in zip(data, pad))


# --- KEK backend (FineSub, unchanged) ---------------------------------------------------------------------------


class KekBackend(Protocol):
    def wrap(self, data: bytes) -> bytes: ...

    def unwrap(self, blob: bytes) -> bytes: ...


class ObfuscationBackend:
    """Every platform without DPAPI: the master is not wrapped at all.

    `wrap_master` still passes the result through `_obfuscate`, so the keyring
    carries no recognisable key material; that layer is the whole of the
    protection here.
    """

    prefix = OBF_PREFIX

    def wrap(self, data: bytes) -> bytes:
        return data

    def unwrap(self, blob: bytes) -> bytes:
        return blob


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.c_void_p)]


_CRYPTPROTECT_UI_FORBIDDEN = 0x01


class DpapiBackend:
    """CryptProtectData/CryptUnprotectData for the current Windows user.

    CryptProtectData is non-deterministic (internal random IV), which is why
    the design wraps a random master key instead of deriving one from a fixed
    string.
    """

    prefix = KEK_PREFIX

    def __init__(self) -> None:
        if os.name != "nt":
            raise ProtectionUnavailable("DPAPI is only available on Windows")
        try:
            self._crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
            self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        except OSError as error:
            raise ProtectionUnavailable(f"crypt32 unavailable: {error}") from error
        self._kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        self._kernel32.LocalFree.restype = ctypes.c_void_p
        blob_pointer = ctypes.POINTER(_DataBlob)
        for name in ("CryptProtectData", "CryptUnprotectData"):
            function = getattr(self._crypt32, name)
            function.argtypes = [
                blob_pointer,
                ctypes.c_wchar_p,
                blob_pointer,
                ctypes.c_void_p,
                ctypes.c_void_p,
                ctypes.c_uint32,
                blob_pointer,
            ]
            function.restype = ctypes.c_int

    def _call(self, function, data: bytes) -> bytes:
        buffer = ctypes.create_string_buffer(data, len(data))
        entropy_buffer = ctypes.create_string_buffer(_ENTROPY, len(_ENTROPY))
        blob_in = _DataBlob(len(data), ctypes.cast(buffer, ctypes.c_void_p))
        blob_entropy = _DataBlob(len(_ENTROPY), ctypes.cast(entropy_buffer, ctypes.c_void_p))
        blob_out = _DataBlob()
        succeeded = function(
            ctypes.byref(blob_in),
            None,
            ctypes.byref(blob_entropy),
            None,
            None,
            _CRYPTPROTECT_UI_FORBIDDEN,
            ctypes.byref(blob_out),
        )
        if not succeeded:
            raise OSError(ctypes.get_last_error() or "DPAPI call failed")
        try:
            return ctypes.string_at(blob_out.pbData, blob_out.cbData)
        finally:
            self._kernel32.LocalFree(blob_out.pbData)

    def wrap(self, data: bytes) -> bytes:
        try:
            return self._call(self._crypt32.CryptProtectData, data)
        except OSError as error:
            raise ProtectionUnavailable(f"DPAPI wrap failed: {error}") from error

    def unwrap(self, blob: bytes) -> bytes:
        try:
            return self._call(self._crypt32.CryptUnprotectData, blob)
        except OSError as error:
            # Wrong machine, wrong account, or a corrupted blob -- for the
            # caller these are all "this ciphertext cannot be opened here".
            raise SecretUnreadable(f"DPAPI unwrap failed: {error}") from error


_INJECTED_BACKEND: KekBackend | None = None
_DEFAULT_BACKEND: KekBackend | None = None
_OBFUSCATION = ObfuscationBackend()

# Per-process caches. The master is unwrapped once per keyring token and never
# logged. (Python cannot guarantee memory wiping; no promise is made.)
_MASTER_CACHE: dict[str, bytes] = {}
_MASTER_FAILURES: set[str] = set()
_WARNED: set[tuple[str, str]] = set()


def set_backend(backend: KekBackend | None) -> None:
    """Inject a fake backend for tests; ``None`` restores the default."""

    global _INJECTED_BACKEND, _DEFAULT_BACKEND
    _INJECTED_BACKEND = backend
    _DEFAULT_BACKEND = None
    _MASTER_CACHE.clear()
    _MASTER_FAILURES.clear()
    _WARNED.clear()


def _backend() -> KekBackend:
    """The backend new keyrings are written with on this machine."""

    global _DEFAULT_BACKEND
    if _INJECTED_BACKEND is not None:
        return _INJECTED_BACKEND
    if _DEFAULT_BACKEND is None:
        _DEFAULT_BACKEND = DpapiBackend() if os.name == "nt" else _OBFUSCATION
    return _DEFAULT_BACKEND


def available() -> bool:
    if _INJECTED_BACKEND is not None or os.name != "nt":
        return True
    try:
        _backend()
    except ProtectionUnavailable:
        return False
    return True


def account_bound(token: str) -> bool:
    """Whether THIS keyring's master key is bound to the current account (not just obfuscated)."""

    return not token.startswith(OBF_PREFIX)


# --- envelope (FineSub, unchanged) ------------------------------------------------------------------------------


def new_master() -> bytes:
    return os.urandom(_MASTER_LEN)


def _obfuscate(blob: bytes, salt: bytes) -> bytes:
    """XOR the DPAPI blob with a keystream. Not a security boundary: it only erases DPAPI's constant prefix."""

    pad = hashlib.shake_256(_ENTROPY + salt).digest(len(blob))
    return _xor(blob, pad)


_deobfuscate = _obfuscate


def wrap_master(master: bytes) -> str:
    salt = os.urandom(_KEK_SALT_LEN)
    backend = _backend()
    blob = backend.wrap(master)
    prefix = getattr(backend, "prefix", KEK_PREFIX)
    return prefix + _b64encode(salt + _obfuscate(blob, salt))


def _unwrapping_backend(token: str) -> tuple[KekBackend, str]:
    """The backend a keyring token was written with, chosen by its prefix (the file may come from elsewhere)."""

    if token.startswith(OBF_PREFIX):
        return _OBFUSCATION, token[len(OBF_PREFIX):]
    if token.startswith(KEK_PREFIX):
        if _INJECTED_BACKEND is None and os.name != "nt":
            raise SecretUnreadable("keyring is bound to a Windows account and cannot be opened here")
        return _backend(), token[len(KEK_PREFIX):]
    raise SecretUnreadable("keyring has an unknown format")


def unwrap_master(token: str) -> bytes:
    backend, body = _unwrapping_backend(token)
    try:
        raw = _b64decode(body)
    except (binascii.Error, ValueError) as error:
        raise SecretUnreadable(f"keyring is corrupt: {error}") from error
    if len(raw) <= _KEK_SALT_LEN:
        raise SecretUnreadable("keyring is truncated")
    salt, obfuscated = raw[:_KEK_SALT_LEN], raw[_KEK_SALT_LEN:]
    master = backend.unwrap(_deobfuscate(obfuscated, salt))
    if len(master) != _MASTER_LEN:
        raise SecretUnreadable("keyring does not hold a master key")
    return master


def _keystream(master: bytes, salt: bytes, length: int) -> bytes:
    return hashlib.shake_256(master + b"|v1|" + salt).digest(length)


def _tag(master: bytes, salt: bytes, ciphertext: bytes) -> bytes:
    # person= separates the MAC use of the master from the keystream use.
    return hashlib.blake2b(salt + ciphertext, key=master, person=b"fs-mac", digest_size=_TAG_LEN).digest()


def seal(plaintext: str, master: bytes) -> str:
    salt = os.urandom(_VALUE_SALT_LEN)
    data = plaintext.encode("utf-8")
    ciphertext = _xor(data, _keystream(master, salt, len(data)))
    tag = _tag(master, salt, ciphertext)
    return VALUE_PREFIX + _b64encode(salt + tag + ciphertext)


def open_(token: str, master: bytes) -> str:
    if not token.startswith(VALUE_PREFIX):
        raise SecretUnreadable("not a sealed value")
    try:
        raw = _b64decode(token[len(VALUE_PREFIX):])
    except (binascii.Error, ValueError) as error:
        raise SecretUnreadable(f"sealed value is corrupt: {error}") from error
    if len(raw) < _VALUE_SALT_LEN + _TAG_LEN:
        raise SecretUnreadable("sealed value is truncated")
    salt = raw[:_VALUE_SALT_LEN]
    tag = raw[_VALUE_SALT_LEN:_VALUE_SALT_LEN + _TAG_LEN]
    ciphertext = raw[_VALUE_SALT_LEN + _TAG_LEN:]
    if not hmac.compare_digest(tag, _tag(master, salt, ciphertext)):
        # Without this check a corrupt ciphertext would silently decrypt to a
        # garbage key and fail much later, at the API, pointing nowhere.
        raise SecretUnreadable("sealed value failed authentication")
    return _xor(ciphertext, _keystream(master, salt, len(ciphertext))).decode("utf-8")


def is_protected(value: str) -> bool:
    return value.startswith(VALUE_PREFIX)


def _resolve_master(keyring_token: str) -> bytes:
    if keyring_token in _MASTER_CACHE:
        return _MASTER_CACHE[keyring_token]
    if keyring_token in _MASTER_FAILURES:
        raise SecretUnreadable("keyring already failed to open in this process")
    try:
        master = unwrap_master(keyring_token)
    except SecretUnreadable:
        _MASTER_FAILURES.add(keyring_token)
        raise
    _MASTER_CACHE[keyring_token] = master
    return master


def _warn_once(where: str, topic: str, message: str) -> None:
    if (where, topic) in _WARNED:
        return
    _WARNED.add((where, topic))
    print(f"Warning: {message}", file=sys.stderr)


# --- the key material inside a value (FineSub, unchanged) ------------------------------------------------------


def _trimmed(rhs: str, start: int, end: int) -> tuple[int, int]:
    while start < end and rhs[start].isspace():
        start += 1
    while end > start and rhs[end - 1].isspace():
        end -= 1
    return start, end


def _strip_one_quote_layer(rhs: str, start: int, end: int) -> tuple[int, int]:
    if end - start >= 2 and rhs[start] == rhs[end - 1] and rhs[start] in "\"'":
        return start + 1, end - 1
    return start, end


def _value_spans(rhs: str) -> list[tuple[int, int]]:
    """Offsets of the secret material inside a raw value.

    Split items on every comma, the value is what follows the first colon, one
    layer of paired quotes comes off -- inside ``{}`` only; anything else is one
    span. The ``{}`` test is the whole dispatch.
    """

    start, end = _trimmed(rhs, 0, len(rhs))
    if start >= end:
        return []  # empty stays empty: encrypting it would fake "configured"
    if not (rhs[start] == "{" and rhs[end - 1] == "}"):
        span_start, span_end = _strip_one_quote_layer(rhs, start, end)
        return [(span_start, span_end)] if span_start < span_end else []
    inner_start, inner_end = start + 1, end - 1
    if inner_start == inner_end:
        # "{}" is deliberately encrypted (it also hides which pool is empty).
        return [(inner_start, inner_end)]
    spans: list[tuple[int, int]] = []
    item_start = inner_start
    while item_start <= inner_end:
        comma = rhs.find(",", item_start, inner_end)
        item_end = comma if comma != -1 else inner_end
        value_start = item_start
        colon = rhs.find(":", item_start, item_end)
        if colon != -1:
            value_start = colon + 1
        span_start, span_end = _trimmed(rhs, value_start, item_end)
        span_start, span_end = _strip_one_quote_layer(rhs, span_start, span_end)
        if span_start < span_end:
            spans.append((span_start, span_end))
        if comma == -1:
            break
        item_start = comma + 1
    return spans


def iter_entries(plaintext_rhs: str) -> list[tuple[str, str]]:
    """(label, key) pairs of a decrypted value, for masked display."""

    entries: list[tuple[str, str]] = []
    start, end = _trimmed(plaintext_rhs, 0, len(plaintext_rhs))
    is_container = start < end and plaintext_rhs[start] == "{" and plaintext_rhs[end - 1] == "}"
    for span_start, span_end in _value_spans(plaintext_rhs):
        value = plaintext_rhs[span_start:span_end]
        if not value:
            continue
        label = ""
        if is_container:
            item_head = plaintext_rhs.rfind(",", start + 1, span_start)
            head = item_head + 1 if item_head != -1 else start + 1
            colon = plaintext_rhs.find(":", head, span_start)
            if colon != -1:
                label = plaintext_rhs[head:colon].strip().strip('"').strip("'")
        entries.append((label, value))
    return entries


def masked(key: str) -> str:
    if len(key) <= 8:
        return "****"
    return f"{key[:4]}…{key[-4:]}"


def _open_value(value: str, master: bytes) -> str:
    return _TOKEN_RE.sub(lambda match: open_(match.group(0), master), value)


def _value_state(value: str, keyring: str | None) -> str | None:
    """``protected`` / ``plaintext`` / ``unreadable``, or None for a value with no key material."""

    spans = _value_spans(value)
    if not spans:
        return None
    parts = [value[start:end] for start, end in spans]
    tokens = [part for part in parts if is_protected(part)]
    state = "protected" if len(tokens) == len(parts) else "plaintext"
    for token in tokens:
        try:
            if keyring is None:
                raise SecretUnreadable("no keyring")
            open_(token, _resolve_master(keyring))
        except (SecretUnreadable, ProtectionUnavailable):
            return "unreadable"
    return state


@dataclass(slots=True)
class _Sealed:
    """What sealing a set of values gave: the values and keyring to store, or why it gave up."""

    values: dict[str, str]
    keyring: str | None
    ok: bool
    sealed: list[str] = field(default_factory=list)
    reason: str = ""


def _seal_values(values: Mapping[str, str], keyring: str | None) -> _Sealed:
    """Seal every plaintext span of `values` (FineSub `_protect_text`, on a mapping instead of `.env` lines).

    The keyring is kept when it opens; a keyring that does not open here is
    only replaced when no ciphertext is left that it might still open
    elsewhere (the file may just be visiting). May raise ProtectionUnavailable.
    """

    current = dict(values)
    has_ciphertext = any(_TOKEN_RE.search(value) for value in current.values())
    todo: list[tuple[str, list[tuple[int, int]]]] = []
    for name, value in current.items():
        spans = [(start, end) for start, end in _value_spans(value) if not is_protected(value[start:end])]
        if spans:
            todo.append((name, spans))

    master: bytes | None = None
    rebuild = False
    if keyring is not None:
        try:
            master = _resolve_master(keyring)
        except SecretUnreadable as error:
            if has_ciphertext:
                return _Sealed(current, keyring, False, reason=f"主密钥无法在本机解开（{error}），已有密文保持原样。{_REVEAL_HINT}")
            rebuild = True

    if not todo:
        return _Sealed(current, None if rebuild else keyring, True)

    if master is None or rebuild:
        master = new_master()
        keyring = wrap_master(master)  # ProtectionUnavailable propagates

    sealed: list[str] = []
    for name, spans in todo:
        value = current[name]
        for start, end in reversed(spans):
            plaintext = value[start:end]
            token = seal(plaintext, master)
            if open_(token, master) != plaintext:
                # The in-memory round trip is the validation before writing.
                return _Sealed(dict(values), keyring, False, reason="密钥保护自校验失败，保持原样")
            value = value[:start] + token + value[end:]
        current[name] = value
        sealed.append(name)
    return _Sealed(current, keyring, True, sealed=sealed)


# --- secrets.json ------------------------------------------------------------------------------------------------


def _write_private(path: Path, text: str) -> None:
    """Atomic write; owner-only from the first byte where file modes mean anything."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    try:
        if os.name == "nt":
            temporary.write_text(text, encoding="utf-8", newline="")
        else:
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
                os.fchmod(handle.fileno(), 0o600)
                handle.write(text)
        replace_path(temporary, path, budget=RECORD_REPLACE)
    except BaseException:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _lock_path(path: Path) -> Path:
    return path.with_name(path.name + ".lock")


class SecretStore:
    """`secrets.json` of one data directory. Values go in and come out as plaintext; on disk they are sealed."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    @classmethod
    def in_dir(cls, data_dir: str | Path) -> "SecretStore":
        return cls(Path(data_dir) / FILE_NAME)

    def exists(self) -> bool:
        return self.path.is_file()

    # -- the document ------------------------------------------------------------------------------------------

    def _load(self) -> tuple[str | None, dict[str, str]]:
        if not self.path.is_file():
            return None, {}
        try:
            document = json.loads(self.path.read_bytes().decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as error:
            raise SecretStoreError(f"{self.path.name} is not valid JSON: {error.__class__.__name__}") from None
        if not isinstance(document, dict) or document.get("schema") != SCHEMA:
            raise SecretStoreError(f"{self.path.name} has an unknown schema")
        keyring = document.get("keyring")
        values = document.get("values") or {}
        if keyring is not None and not isinstance(keyring, str):
            raise SecretStoreError(f"{self.path.name}: keyring is not a string")
        if not isinstance(values, dict) or not all(
            isinstance(name, str) and isinstance(value, str) for name, value in values.items()
        ):
            raise SecretStoreError(f"{self.path.name}: values is not a name -> string map")
        return keyring or None, dict(values)

    @staticmethod
    def _render(keyring: str | None, values: Mapping[str, str]) -> str:
        document: dict[str, Any] = {"schema": SCHEMA}
        if keyring:
            document["keyring"] = keyring
        document["values"] = dict(values)
        return json.dumps(document, ensure_ascii=False, indent=2) + "\n"

    # -- reading ---------------------------------------------------------------------------------------------

    def read(self) -> dict[str, str]:
        """Every saved value, opened. A value that cannot be opened here is left out (with a warning naming it)."""

        keyring, values = self._load()
        result: dict[str, str] = {}
        for name, value in values.items():
            if not _TOKEN_RE.search(value):
                result[name] = value
                continue
            try:
                if keyring is None:
                    raise SecretUnreadable("no keyring")
                result[name] = _open_value(value, _resolve_master(keyring))
            except (SecretUnreadable, ProtectionUnavailable) as error:
                _warn_once(str(self.path), f"unreadable:{name}", f"{self.path.name} 中 {name} 的密钥无法在本机解密（{error}）。{_REVEAL_HINT}")
        return result

    def status(self) -> dict[str, str]:
        """Per name: ``protected`` / ``plaintext`` / ``unreadable``; names with no key material are left out."""

        keyring, values = self._load()
        states: dict[str, str] = {}
        for name, value in values.items():
            state = _value_state(value, keyring)
            if state is not None:
                states[name] = state
        return states

    # -- writing ---------------------------------------------------------------------------------------------

    def update(self, updates: Mapping[str, str | None]) -> None:
        """Set (or with ``None``: remove) values, sealed, in one locked atomic write.

        Values not named keep their place and their ciphertext. If sealing is
        impossible here, the new values are written in plaintext with a
        warning, never dropped (FineSub's rule).
        """

        for name, value in updates.items():
            if not isinstance(name, str) or not name:
                raise ValueError("a key name must be a non-empty string")
            if value is not None and (not isinstance(value, str) or len(value) > MAX_VALUE_LENGTH):
                raise ValueError(f"{name} must be a string of at most {MAX_VALUE_LENGTH} characters")
        with holding_lock(_lock_path(self.path), timeout=_LOCK_TIMEOUT_SECONDS):
            keyring, values = self._load()
            merged = dict(values)
            for name, value in updates.items():
                if value is None or not value.strip():
                    merged.pop(name, None)
                else:
                    merged[name] = value
            try:
                result = _seal_values(merged, keyring)
            except ProtectionUnavailable as error:
                result = _Sealed(merged, keyring, False, reason=f"本机无法保护密钥（{error}）")
            if not result.ok:
                _warn_once(str(self.path), "write", f"{result.reason}；新值以明文写入 {self.path.name}")
                result = _Sealed(merged, keyring, True)
            text = self._render(result.keyring, result.values)
            if self.path.is_file() and self.path.read_bytes().decode("utf-8", "replace") == text:
                return
            _write_private(self.path, text)


# --- FineSub's `.env` line layer (FineSub, unchanged; the keyring line's name is the caller's) ------------------


@dataclass(slots=True)
class EnvLine:
    body: str  # without the line terminator
    ending: str  # "\n", "\r\n" or "" for the last line
    name: str | None = None  # None for comments/blank/parse-skipped lines
    rhs_start: int = 0  # offset of the value within body (after '=')

    @property
    def rhs(self) -> str:
        return self.body[self.rhs_start:]


def parse_env_lines(text: str) -> list[EnvLine]:
    lines: list[EnvLine] = []
    for raw in text.splitlines(keepends=True):
        body = raw.rstrip("\r\n")
        ending = raw[len(body):]
        stripped = body.strip()
        if not stripped or stripped.startswith("#") or "=" not in body:
            lines.append(EnvLine(body, ending))
            continue
        eq = body.index("=")
        name = body[:eq].strip()
        lines.append(EnvLine(body, ending, name or None, eq + 1))
    return lines


@dataclass(frozen=True)
class EnvRead:
    """An old `.env` read without writing: the opened values, in file order, and what could not be opened."""

    values: dict[str, str]
    unreadable: tuple[str, ...]
    names: tuple[str, ...]


def read_env_text(text: str, keyring_name: str) -> EnvRead:
    """Parse `.env` text and open its values (FineSub `read_env_file`). Pure.

    `names` is every variable named, in order, the keyring line excepted;
    `unreadable` the ones whose key material does not open here.
    """

    keyring_token: str | None = None
    pending: list[tuple[str, str]] = []
    for line in parse_env_lines(text):
        if line.name is None:
            continue
        value = line.rhs.strip()
        if line.name == keyring_name:
            keyring_token = value
            continue
        pending.append((line.name, value))
    values: dict[str, str] = {}
    unreadable: list[str] = []
    for name, value in pending:
        if not _TOKEN_RE.search(value):
            values[name] = value
            continue
        try:
            if keyring_token is None:
                raise SecretUnreadable("no keyring line")
            values[name] = _open_value(value, _resolve_master(keyring_token))
        except (SecretUnreadable, ProtectionUnavailable):
            unreadable.append(name)
    return EnvRead(values=values, unreadable=tuple(unreadable), names=tuple(name for name, _ in pending))


#: The note above the keyring line (FineSub's wording, so the derived file reads like the old one).
_HEADER_NOTE_PREFIX = "# FineSub 主密钥："


def _header_note(bound: bool) -> str:
    tail = "绑定当前 Windows 账户，勿手动修改或删除。" if bound else "仅作混淆（不是加密），勿手动修改或删除。"
    return _HEADER_NOTE_PREFIX + tail


def protect_env_text(text: str, keyring_name: str) -> tuple[str, bool]:
    """Seal every plaintext span of `.env` text (FineSub `_protect_text` for a file with no keyring yet or one that opens).

    Returns the text and whether it is protected; on failure the text comes back
    unchanged. Used only for a file written whole from Nonoka's store, so the
    unreadable-keyring branches of FineSub's version cannot arise.
    """

    lines = parse_env_lines(text)
    ending = "\r\n" if "\r\n" in text else "\n"
    todo: list[tuple[int, list[tuple[int, int]]]] = []
    for index, line in enumerate(lines):
        if line.name is None or line.name == keyring_name:
            continue
        spans = [(start, end) for start, end in _value_spans(line.rhs) if not is_protected(line.rhs[start:end])]
        if spans:
            todo.append((index, spans))
    if not todo:
        return text, True
    try:
        master = new_master()
        keyring_token = wrap_master(master)
    except ProtectionUnavailable as error:
        _warn_once("env", "unavailable", f"本机无法保护密钥（{error}），继续使用明文")
        return text, False
    header = [
        EnvLine(_header_note(account_bound(keyring_token)), ending),
        EnvLine("# 导出明文密钥：finesub keys --reveal", ending),
        EnvLine(f"{keyring_name}={keyring_token}", ending, keyring_name, len(keyring_name) + 1),
    ]
    lines = header + lines
    todo = [(index + len(header), spans) for index, spans in todo]
    for index, spans in todo:
        line = lines[index]
        body = line.body
        for start, end in reversed(spans):
            plaintext = line.rhs[start:end]
            token = seal(plaintext, master)
            if open_(token, master) != plaintext:
                return text, False
            offset = line.rhs_start
            body = body[:offset + start] + token + body[offset + end:]
        lines[index] = EnvLine(body, line.ending, line.name, line.rhs_start)
    return "".join(line.body + line.ending for line in lines), True


def write_private_text(path: Path, text: str) -> None:
    """`_write_private` for the derived `.env` (FineSub `_write_env_text`)."""

    _write_private(path, text)


__all__ = [
    "FILE_NAME",
    "KEK_PREFIX",
    "OBF_PREFIX",
    "SCHEMA",
    "VALUE_PREFIX",
    "EnvLine",
    "EnvRead",
    "ProtectionUnavailable",
    "SecretStore",
    "SecretStoreError",
    "SecretUnreadable",
    "account_bound",
    "available",
    "is_protected",
    "iter_entries",
    "masked",
    "open_",
    "parse_env_lines",
    "protect_env_text",
    "read_env_text",
    "seal",
    "set_backend",
    "unwrap_master",
    "wrap_master",
    "write_private_text",
]
