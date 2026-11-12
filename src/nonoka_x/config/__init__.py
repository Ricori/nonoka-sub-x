"""Nonoka's settings and secrets, behind one facade.

New code (the transcription engines, WP1/WP3/WP5) reads keys and settings only
through here, never through `nonoka_x.settings` directly, so the runtime line
can replace what is underneath (WP-R4) without touching the engine line
(`docs/transcription-engines-plan.md`, section 6).

Underneath is Nonoka's own storage (WP-R4): `<data dir>/settings.json`
(`config.store`) and `<data dir>/secrets.json` (`config.secrets`, FineSub's
envelope); `config.migrate` reads an old install's files into them.
`settings.FineSubSettings` is the settings page's model on top of the two.

- `key_pool(name)` reads a key pool from `secrets.json`;
- `snapshot()` / `update(...)` are the settings snapshot and update the
  desktop already uses; `update` also takes the registered names;
- `register_key_spec(...)` records a key the settings page does not list (the
  cloud ASR engines' keys, WP1/WP3). It is kept in memory only and does not add
  to the page's key list; its value lives in `secrets.json` like any other key;
- `ConfigStore.state_file(name)` is where Nonoka's own small state files live
  (`<data dir>/state/<name>.json`, WP1).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import threading
from typing import Any, Mapping

_KEY_NAME = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
#: Names whose value is one address, not a pool: never split on `:` or `,`.
_SCALAR_SUFFIXES = ("_ENDPOINT",)
_STATE_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")


@dataclass(frozen=True)
class KeySpec:
    name: str
    label: str
    purpose: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not _KEY_NAME.match(self.name):
            raise ValueError(f"key name must be upper-case letters, digits and '_', got {self.name!r}")
        if not isinstance(self.label, str) or not self.label:
            raise ValueError("key label must be a non-empty string")

    def to_dict(self) -> dict[str, str]:
        return {"name": self.name, "label": self.label, "purpose": self.purpose}


@dataclass(frozen=True)
class KeyEntry:
    """One key of a pool; `label` is empty when the pool is a plain list."""

    label: str
    value: str

    def __repr__(self) -> str:  # never print a secret
        return f"KeyEntry(label={self.label!r}, value=<redacted>)"


_registered: dict[str, KeySpec] = {}
_registered_lock = threading.Lock()


def _existing_specs() -> tuple[KeySpec, ...]:
    from nonoka_x.settings import KEY_SPECS

    return tuple(KeySpec(spec["name"], spec["label"], spec.get("purpose", "")) for spec in KEY_SPECS)


def register_key_spec(spec: KeySpec) -> KeySpec:
    """Record a key the settings do not know yet (in memory only, see above).

    Registering the same spec twice is a no-op; a different spec under a name
    that is taken -- registered or already a settings key -- is an error.
    """

    if not isinstance(spec, KeySpec):
        raise TypeError("register_key_spec takes a KeySpec")
    if any(existing.name == spec.name for existing in _existing_specs()):
        raise ValueError(f"key {spec.name} is already a settings key")
    with _registered_lock:
        current = _registered.get(spec.name)
        if current is not None and current != spec:
            raise ValueError(f"key {spec.name} is already registered with a different spec")
        _registered[spec.name] = spec
    return spec


def registered_key_specs() -> tuple[KeySpec, ...]:
    """The specs added through `register_key_spec`, in registration order."""

    with _registered_lock:
        return tuple(_registered.values())


def key_specs() -> tuple[KeySpec, ...]:
    """Every key Nonoka knows: the settings' own, then the registered ones."""

    return _existing_specs() + registered_key_specs()


def _unregister_key_spec(name: str) -> None:
    """Tests only."""

    with _registered_lock:
        _registered.pop(name, None)


class ConfigStore:
    """Settings and secrets of one data directory."""

    def __init__(self, settings: Any) -> None:
        # A `settings.FineSubSettings`; typed loosely so importing this module
        # does not import the settings module.
        self._settings = settings

    @classmethod
    def open(cls, root: str | Path) -> "ConfigStore":
        from nonoka_x.settings import FineSubSettings

        return cls(FineSubSettings(root))

    @property
    def root(self) -> Path:
        return Path(self._settings.root)

    def key_pool(self, name: str) -> tuple[KeyEntry, ...]:
        """The keys saved under `name`, in the order they were entered.

        Unknown names raise; a known or registered name with nothing saved is
        an empty pool. An endpoint (a name ending in `_ENDPOINT`) is one
        unlabelled entry holding the whole address.
        """

        known = {spec.name for spec in key_specs()}
        if name not in known:
            raise KeyError(f"unknown key {name!r}")
        from nonoka_x import settings as settings_module

        raw = self._settings.secret_values().get(name, "")
        if name.endswith(_SCALAR_SUFFIXES):
            value = raw.strip()
            return (KeyEntry("", value),) if value else ()
        return tuple(KeyEntry(label, value) for label, value in settings_module._entries(raw))

    def has_key(self, name: str) -> bool:
        return bool(self.key_pool(name))

    def state_file(self, name: str) -> Path:
        """Where Nonoka's own small state `name` lives: `<data dir>/state/<name>.json`.

        Only the path; the caller reads, locks and writes it (e.g. the cloud ASR
        quota ledger, `transcription.keys`). `name` is lower-case letters,
        digits and `-`.
        """

        if not isinstance(name, str) or not _STATE_NAME.match(name):
            raise ValueError(f"state name must be lower-case letters, digits and '-', got {name!r}")
        return self.root / "state" / f"{name}.json"

    def snapshot(self) -> dict[str, Any]:
        """The settings as the desktop sees them; secret values never appear."""

        return self._settings.snapshot()

    def update(self, updates: Mapping[str, Any]) -> dict[str, Any]:
        """Save settings and keys (`None` clears one); returns the new snapshot."""

        registered = frozenset(spec.name for spec in registered_key_specs())
        return self._settings.update_keys(updates, extra_key_names=registered)


__all__ = [
    "ConfigStore",
    "KeyEntry",
    "KeySpec",
    "key_specs",
    "register_key_spec",
    "registered_key_specs",
]
