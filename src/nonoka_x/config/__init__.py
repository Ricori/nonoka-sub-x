"""Nonoka's settings and secrets, behind one facade.

New code (the transcription engines, WP1/WP3/WP5) reads keys and settings only
through here, never through `nonoka_x.settings` directly, so the runtime line
can replace what is underneath (WP-R4) without touching the engine line
(`docs/transcription-engines-plan.md`, section 6).

For now this is a thin layer over `settings.FineSubSettings`:

- `key_pool(name)` reads a key pool from the protected key file;
- `snapshot()` / `update(...)` are the settings snapshot and update the
  desktop already uses;
- `register_key_spec(...)` only records a new key's spec in memory. It does
  not add to the settings' key list and writes nothing: new keys go straight
  into Nonoka's own secret store once WP-R4 has built it, and until then a
  registered name reads as an empty pool.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import threading
from typing import Any, Mapping

_KEY_NAME = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")


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

        Unknown names raise; a registered name that has no storage yet (see
        the module docstring) and a known name with nothing saved are both
        an empty pool.
        """

        known = {spec.name for spec in _existing_specs()}
        if name not in known:
            if name in {spec.name for spec in registered_key_specs()}:
                return ()
            raise KeyError(f"unknown key {name!r}")
        from nonoka_x import settings as settings_module

        values = settings_module._secrets_module().read_env_file(self._settings.env_file)
        raw = values.get(name, "")
        return tuple(KeyEntry(label, value) for label, value in settings_module._entries(raw))

    def has_key(self, name: str) -> bool:
        return bool(self.key_pool(name))

    def snapshot(self) -> dict[str, Any]:
        """The settings as the desktop sees them; secret values never appear."""

        return self._settings.snapshot()

    def update(self, updates: Mapping[str, Any]) -> dict[str, Any]:
        """Save settings and keys (`None` clears one); returns the new snapshot."""

        return self._settings.update_keys(updates)


__all__ = [
    "ConfigStore",
    "KeyEntry",
    "KeySpec",
    "key_specs",
    "register_key_spec",
    "registered_key_specs",
]
