"""Nonoka's settings file, `<data dir>\\settings.json` (`docs/runtime-and-config.md` D1, 2.1).

UTF-8 JSON with a top-level ``schema``, written atomically under a ``.lock``
beside it. Sparse: only what has been set is stored, so a fresh install's file
is ``{"schema": 1}``. Only settings Nonoka knows are kept (D11, D11c); nothing
in FineSub's formats.

    {
      "schema": 1,
      "llm": {"routes": {"default":    {"provider": "...", "model": "..."},
                         "correction": {...}, "research": {...}, "knowledge": {...}}},
      "baseUrls": {"GEMINI_BASE_URL": "...", "OPENAI_COMPAT_BASE_URL": "..."},
      "cloud": {"llmBaseUrl": "https://..."}
    }

`llm.routes` is the model routing the settings page saves: the global route
and the three task routes. `baseUrls` are the provider endpoints the user
changed, under the names the settings page uses. `cloud.llmBaseUrl` is the
Nonoka Cloud LLM address, present while an account is signed in (its key is in
`secrets.json`). Keys and other secrets never come here (`config.secrets`).
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, Callable, Mapping

from nonoka_x.runtime.fsops import holding_lock, write_atomic

SCHEMA = 1
FILE_NAME = "settings.json"

#: The routes the settings page saves, in the order they are written out.
ROUTES = ("default", "correction", "research", "knowledge")
ROUTE_FIELDS = ("provider", "model")

_LOCK_TIMEOUT_SECONDS = 10.0


class SettingsStoreError(RuntimeError):
    """`settings.json` cannot be read as Nonoka's settings (bad JSON, wrong schema or shape)."""


def _check(document: Any, where: str) -> dict[str, Any]:
    if not isinstance(document, dict) or document.get("schema") != SCHEMA:
        raise SettingsStoreError(f"{where} has an unknown schema")
    routes = (document.get("llm") or {}).get("routes") if isinstance(document.get("llm") or {}, dict) else None
    if document.get("llm") is not None and not isinstance(document.get("llm"), dict):
        raise SettingsStoreError(f"{where}: llm is not an object")
    if routes is not None:
        if not isinstance(routes, dict):
            raise SettingsStoreError(f"{where}: llm.routes is not an object")
        for name, route in routes.items():
            if name not in ROUTES or not isinstance(route, dict) or not all(
                field in ROUTE_FIELDS and isinstance(value, str) for field, value in route.items()
            ):
                raise SettingsStoreError(f"{where}: llm.routes.{name} is not a known route")
    for section in ("baseUrls", "cloud"):
        value = document.get(section)
        if value is not None and (
            not isinstance(value, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in value.items())
        ):
            raise SettingsStoreError(f"{where}: {section} is not a name -> string map")
    return document


def _pruned(document: dict[str, Any]) -> dict[str, Any]:
    """Drop empty strings and the containers they leave empty; routes and sections in their fixed order."""

    result: dict[str, Any] = {"schema": SCHEMA}
    routes = ((document.get("llm") or {}).get("routes")) or {}
    kept_routes: dict[str, dict[str, str]] = {}
    for name in ROUTES:
        route = routes.get(name) or {}
        kept = {field: route[field] for field in ROUTE_FIELDS if route.get(field)}
        if kept:
            kept_routes[name] = kept
    if kept_routes:
        result["llm"] = {"routes": kept_routes}
    for section in ("baseUrls", "cloud"):
        values = {name: value for name, value in (document.get(section) or {}).items() if value}
        if values:
            result[section] = values
    return result


def render(document: Mapping[str, Any]) -> str:
    return json.dumps(_pruned(dict(document)), ensure_ascii=False, indent=2) + "\n"


class SettingsStore:
    """`settings.json` of one data directory."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    @classmethod
    def in_dir(cls, data_dir: str | Path) -> "SettingsStore":
        return cls(Path(data_dir) / FILE_NAME)

    def exists(self) -> bool:
        return self.path.is_file()

    def read(self) -> dict[str, Any]:
        """The document, pruned (`{"schema": 1}` when there is no file)."""

        if not self.path.is_file():
            return {"schema": SCHEMA}
        try:
            document = json.loads(self.path.read_bytes().decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as error:
            raise SettingsStoreError(f"{self.path.name} is not valid JSON: {error}") from None
        return _pruned(_check(document, self.path.name))

    def change(self, mutate: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
        """Read, let `mutate` edit the document in place, write it back -- all under the lock.

        Nothing is written when the result is what is already on disk.
        Returns the new document.
        """

        with holding_lock(self.path.with_name(self.path.name + ".lock"), timeout=_LOCK_TIMEOUT_SECONDS):
            current = self.read()
            document = copy.deepcopy(current)
            mutate(document)
            text = render(_check(document, self.path.name))
            if self.path.is_file() and self.path.read_bytes().decode("utf-8", "replace") == text:
                return _pruned(document)
            write_atomic(self.path, text)
            return _pruned(document)

    # -- views ------------------------------------------------------------------------------------------------

    def routes(self) -> dict[str, dict[str, str]]:
        return copy.deepcopy(((self.read().get("llm") or {}).get("routes")) or {})

    def base_urls(self) -> dict[str, str]:
        return dict(self.read().get("baseUrls") or {})

    def cloud(self) -> dict[str, str]:
        return dict(self.read().get("cloud") or {})


def set_route_field(document: dict[str, Any], route: str, field: str, value: str | None) -> None:
    if route not in ROUTES or field not in ROUTE_FIELDS:
        raise ValueError(f"unknown route setting {route}.{field}")
    routes = document.setdefault("llm", {}).setdefault("routes", {})
    entry = routes.setdefault(route, {})
    if value:
        entry[field] = value
    else:
        entry.pop(field, None)


def set_section_value(document: dict[str, Any], section: str, name: str, value: str | None) -> None:
    if section not in ("baseUrls", "cloud"):
        raise ValueError(f"unknown settings section {section}")
    values = document.setdefault(section, {})
    if value:
        values[name] = value
    else:
        values.pop(name, None)


__all__ = [
    "FILE_NAME",
    "ROUTES",
    "ROUTE_FIELDS",
    "SCHEMA",
    "SettingsStore",
    "SettingsStoreError",
    "render",
    "set_route_field",
    "set_section_value",
]
