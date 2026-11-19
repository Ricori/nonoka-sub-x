"""Reading an old install's `finesub.env` and `finesub.toml` into `settings.json` and `secrets.json`.

The rules are `docs/runtime-and-config.md` 2.7 item 3 with D11, D11c and D13,
and the owner's answers of 2026-11-12 (WP-R4):

* **Carried**: the settings and keys Nonoka knows. Keys -- the settings page's
  twelve (`settings.KEY_SPECS`), the Nonoka Cloud key, and the cloud ASR
  engines' `MOSS_PRO`, `OPENROUTER_STT_KEY`, `AZURE_SPEECH_KEY`,
  `AZURE_SPEECH_ENDPOINT` -- go to `secrets.json`; the provider endpoints
  (`settings.BASE_URL_SPECS`) and the Nonoka Cloud LLM address go to
  `settings.json`; so do the model routes of `[nonoka_models]`, after
  `settings.migrate_model_config` (an old `planning` route moves to
  `research`, renamed providers and models move on).
* **Renamed**: three of FineSub's names for the engines' keys
  (`LEGACY_RENAMES`). When the new name is in the file too, it wins and the
  old one is reported as superseded.
* **Derived, not carried**: `[llm.preferred_targets]` for the groups Nonoka
  pins, and `[llm]`'s `default_target` / `task_route_*` -- they are rewritten
  from the routes. The keyring line is the old file's protector, not a key.
* **Dropped**: everything else. The report names each dropped variable,
  table and key -- never a value. FineSub runs with its defaults for the
  dropped tables from now on (D11c), and the report says so.

Values are carried as they were written (a pool's ``label:key,label:key``
spelling included). A value that cannot be opened on this machine -- the old
file was protected under another Windows account -- cannot be carried; it is
reported by name; it stays in the backup until the migration is past the
point of rollback, and then goes with it (the migration's report names it).

The old files are renamed to `<name>.pre-migration` (`.1`, `.2` if taken) and
their `.lock` files deleted. The backups serve the migration's rollback only:
once the migration is past the point of rollback its `leftovers` step deletes them
(`remove_backups`; owner 2026-11-18/19, plan appendix B questions 89-90), and the
report records what went. The report is
`<data dir>\\runtime-state\\settings-migration.json`; its `status` also lets an
interrupted run continue: `started` (the stores may be half written: they are
written again from the old files), `stores-written` (only the renames are
left), `completed`.

Called by the migration's `settings` step (`SettingsHook`), from the command
line or the sidecar's start-up (`nonoka_x.migration.startup`); a failed
migration undoes it with `undo_settings`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tomllib
from typing import Any, Callable, Mapping

from nonoka_x.config.secrets import SecretStore, read_env_text
from nonoka_x.config.store import ROUTES, SettingsStore, set_route_field, set_section_value
from nonoka_x.migration import legacy
from nonoka_x.runtime.fsops import RECORD_REPLACE, replace_path, write_atomic
from nonoka_x.runtime.paths import NonokaPaths

REPORT_SCHEMA = 1
REPORT_NAME = "settings-migration.json"
BACKUP_SUFFIX = legacy.SETTINGS_BACKUP_SUFFIX

STARTED = "started"
STORES_WRITTEN = "stores-written"
COMPLETED = "completed"

ENV_FILE, TOML_FILE = legacy.SETTINGS_FILES

#: The old key file's protector line, and FineSub's names for the engines' keys
#: (named in `migration.legacy`, the one module that names the old layout).
KEYRING_NAME = legacy.KEYRING_NAME
LEGACY_RENAMES: Mapping[str, str] = legacy.LEGACY_KEY_RENAMES

#: The cloud ASR engines' keys (`transcription.keys.KEY_SPECS`; a test keeps the two lists equal).
ENGINE_KEY_NAMES = ("MOSS_PRO", "OPENROUTER_STT_KEY", "AZURE_SPEECH_KEY", "AZURE_SPEECH_ENDPOINT")

#: Task groups `[llm.preferred_targets]` pins, now or before (`settings.TASK_GROUPS_BY_ROUTE`, `LEGACY_ROUTES`).
DERIVED_PREFERRED_GROUPS = frozenset(
    {"default", "correction-mm", "correction-text", "research", "knowledge", "planning-mm", "planning-text", "search_judge"}
)
#: `[llm]` keys Nonoka used to write and now only clears.
DERIVED_LLM_KEYS = frozenset(
    {"default_target"} | {f"task_route_{route}" for route in ("correction", "research", "knowledge", "planning", "search_judge")}
)

_DEFAULT_NOTE = (
    f"Tables and keys of {TOML_FILE} that Nonoka does not manage were dropped; "
    "FineSub uses its defaults for them from now on."
)

Log = Callable[[str], None]


class SettingsMigrationError(RuntimeError):
    """The old files cannot be read (e.g. `finesub.toml` is not valid TOML)."""


class SettingsMigrationBlocked(SettingsMigrationError):
    """`settings.json` or `secrets.json` already exists and was not written by this migration: nothing is merged."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def report_path(data_dir: Path | str) -> Path:
    return NonokaPaths.for_roots(data_dir).runtime_state / REPORT_NAME


def read_report(data_dir: Path | str) -> dict[str, Any] | None:
    path = report_path(data_dir)
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as error:
        raise SettingsMigrationError(f"{path} cannot be read: {error.__class__.__name__}") from None
    return report if isinstance(report, dict) else None


def _write_report(data_dir: Path, report: Mapping[str, Any]) -> None:
    write_atomic(report_path(data_dir), json.dumps(report, ensure_ascii=False, indent=2) + "\n")


def _sources(data: Path) -> list[Path]:
    return [data / name for name in legacy.SETTINGS_FILES if (data / name).is_file()]


def _stores_exist(data: Path) -> bool:
    return SettingsStore.in_dir(data).exists() or SecretStore.in_dir(data).exists()


def needed(data_dir: Path | str) -> bool:
    """Whether there is anything to do: old files to read, or an interrupted run to finish."""

    data = Path(data_dir)
    report = read_report(data)
    if report is not None:
        return report.get("status") != COMPLETED
    return bool(_sources(data))


def blocker(data_dir: Path | str) -> str | None:
    """Why the old files cannot be migrated, or None (R0 2.7 item 1: an existing new store is not merged into)."""

    data = Path(data_dir)
    try:
        report = read_report(data)
    except SettingsMigrationError as error:
        return str(error)
    if report is not None or not _sources(data) or not _stores_exist(data):
        return None
    return f"settings.json or secrets.json already exists in {data}; the old {ENV_FILE} / {TOML_FILE} are not merged into it"


# -- reading the old files ---------------------------------------------------------------------------------------


@dataclass
class _Carried:
    secrets: dict[str, str]
    base_urls: dict[str, str]
    cloud: dict[str, str]
    routes: dict[str, dict[str, str]]
    report: dict[str, Any]


def _known_names() -> tuple[frozenset[str], frozenset[str], str, str]:
    from nonoka_x import settings

    secrets = frozenset(settings.KEY_NAMES | {settings.CLOUD_KEY_NAME} | set(ENGINE_KEY_NAMES))
    return secrets, frozenset(settings.BASE_URL_NAMES), settings.CLOUD_BASE_URL_NAME, settings.CLOUD_KEY_NAME


def _unique(names: list[str]) -> list[str]:
    return list(dict.fromkeys(names))


def _read_env(path: Path, carried: _Carried) -> None:
    secret_names, base_url_names, cloud_url_name, _cloud_key = _known_names()
    read = read_env_text(path.read_bytes().decode("utf-8"), KEYRING_NAME)
    unreadable = set(read.unreadable)
    dropped: list[str] = []
    unopened: list[str] = []
    renamed: dict[str, str] = {}
    superseded: list[str] = []
    old_values: dict[str, str] = {}
    for name in _unique(list(read.names)):
        known = name in secret_names or name in base_url_names or name == cloud_url_name or name in LEGACY_RENAMES
        if not known:
            dropped.append(name)
            continue
        if name in unreadable:
            unopened.append(name)
            continue
        value = read.values.get(name, "")
        if not value:
            continue
        if name in LEGACY_RENAMES:
            old_values[name] = value
        elif name in secret_names:
            carried.secrets[name] = value
        elif name in base_url_names:
            carried.base_urls[name] = value
        else:
            carried.cloud["llmBaseUrl"] = value
    for old, new in LEGACY_RENAMES.items():
        if old not in old_values:
            continue
        if new in carried.secrets or new in unopened:
            superseded.append(old)
            continue
        carried.secrets[new] = old_values[old]
        renamed[old] = new
    section = carried.report.setdefault("env", {})
    section["carried"] = sorted(set(carried.secrets) | set(carried.base_urls) | ({cloud_url_name} if carried.cloud else set()))
    section["renamed"] = renamed
    section["superseded"] = superseded
    section["unreadable"] = unopened
    section["dropped"] = dropped


def _key_paths(table: Mapping[str, Any], prefix: str) -> list[str]:
    paths: list[str] = []
    for key, value in table.items():
        dotted = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict):
            paths.extend(_key_paths(value, dotted))
        else:
            paths.append(dotted)
    return paths


def _read_toml(path: Path, carried: _Carried) -> None:
    from nonoka_x.settings import LEGACY_ROUTES, _migrated

    try:
        document = tomllib.loads(path.read_bytes().decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise SettingsMigrationError(f"{path.name} is not valid TOML ({error.__class__.__name__}); fix or remove it, then run again") from None
    route_keys = {f"{route}_{field}" for route in (*ROUTES, *LEGACY_ROUTES) for field in ("provider", "model")}
    dropped_tables: list[str] = []
    dropped_keys: list[str] = []
    flat: dict[str, str] = {}
    for name, value in document.items():
        if name == "nonoka_models" and isinstance(value, dict):
            for key, item in value.items():
                if key in route_keys and isinstance(item, str):
                    if item.strip():
                        flat[key] = item.strip()
                elif isinstance(item, dict):
                    dropped_tables.append(f"nonoka_models.{key}")
                    dropped_keys.extend(_key_paths(item, f"nonoka_models.{key}"))
                else:
                    dropped_keys.append(f"nonoka_models.{key}")
        elif name == "llm" and isinstance(value, dict):
            for key, item in value.items():
                if key == "preferred_targets" and isinstance(item, dict):
                    for group, target in item.items():
                        if group not in DERIVED_PREFERRED_GROUPS or isinstance(target, dict):
                            dropped_keys.append(f"llm.preferred_targets.{group}")
                elif key in DERIVED_LLM_KEYS and not isinstance(item, dict):
                    continue
                elif isinstance(item, dict):
                    dropped_tables.append(f"llm.{key}")
                    dropped_keys.extend(_key_paths(item, f"llm.{key}"))
                else:
                    dropped_keys.append(f"llm.{key}")
        elif isinstance(value, dict):
            dropped_tables.append(name)
            dropped_keys.extend(_key_paths(value, name))
        else:
            dropped_keys.append(name)
    migrated = _migrated(flat)
    for route in ROUTES:
        entry = {field: migrated[f"{route}_{field}"] for field in ("provider", "model") if migrated.get(f"{route}_{field}")}
        if entry:
            carried.routes[route] = entry
    carried.report["toml"] = {
        "carried": sorted(f"nonoka_models.{key}" for key in flat),
        "routes": sorted(carried.routes),
        "droppedTables": dropped_tables,
        "droppedKeys": dropped_keys,
    }
    if dropped_tables or dropped_keys:
        carried.report.setdefault("notes", []).append(_DEFAULT_NOTE)


def _backup_name(source: Path) -> Path:
    candidate = source.with_name(source.name + BACKUP_SUFFIX)
    index = 1
    while os.path.lexists(candidate):
        candidate = source.with_name(f"{source.name}{BACKUP_SUFFIX}.{index}")
        index += 1
    return candidate


def read_legacy(data_dir: Path | str) -> _Carried:
    """What migrating `data_dir` would carry and drop. Reads only."""

    data = Path(data_dir)
    carried = _Carried(secrets={}, base_urls={}, cloud={}, routes={}, report={})
    env, toml = data / ENV_FILE, data / TOML_FILE
    if env.is_file():
        _read_env(env, carried)
    if toml.is_file():
        _read_toml(toml, carried)
    return carried


# -- writing ---------------------------------------------------------------------------------------------------


def _write_stores(data: Path, carried: _Carried) -> None:
    # A store left by an interrupted run of this migration is ours: write it again whole.
    for store in (SettingsStore.in_dir(data), SecretStore.in_dir(data)):
        store.path.unlink(missing_ok=True)

    def fill(document: dict[str, Any]) -> None:
        for route, entry in carried.routes.items():
            for field, value in entry.items():
                set_route_field(document, route, field, value)
        for name, value in carried.base_urls.items():
            set_section_value(document, "baseUrls", name, value)
        for name, value in carried.cloud.items():
            set_section_value(document, "cloud", name, value)

    SettingsStore.in_dir(data).change(fill)
    if carried.secrets:
        SecretStore.in_dir(data).update(carried.secrets)


def migrate_settings(data_dir: Path | str, *, log: Log | None = None) -> dict[str, Any] | None:
    """Carry the old settings and keys over and back the old files up. Resumable.

    Returns the report (names only), or None when there was nothing to do.
    Raises `SettingsMigrationBlocked` when a new store exists that this
    migration did not write, `SettingsMigrationError` when an old file cannot
    be read; in both cases nothing has been changed.
    """

    log = log or (lambda message: None)
    data = Path(os.path.abspath(Path(data_dir).expanduser()))
    report = read_report(data)
    if report is not None and report.get("status") == COMPLETED:
        return report
    sources = _sources(data)
    if report is None:
        if not sources:
            return None
        reason = blocker(data)
        if reason is not None:
            raise SettingsMigrationBlocked(reason)
    if report is None or report.get("status") == STARTED:
        if report is not None and len(sources) < len(report.get("backups") or {}):
            raise SettingsMigrationError("an old settings file disappeared while its migration was being written; restore it from its backup")
        carried = read_legacy(data)
        report = {
            "schema": REPORT_SCHEMA,
            "status": STARTED,
            "startedAt": (report or {}).get("startedAt") or _now(),
            "backups": {source.name: _backup_name(source).name for source in sources},
            # The old files' lock sidecars, removed with the renames: a rollback puts them back.
            "locks": sorted({*(report or {}).get("locks", ()), *(
                f"{source.name}.lock" for source in sources if (data / f"{source.name}.lock").exists()
            )}),
            **carried.report,
        }
        _write_report(data, report)
        _write_stores(data, carried)
        report["status"] = STORES_WRITTEN
        _write_report(data, report)
        env = report.get("env") or {}
        toml = report.get("toml") or {}
        log(f"settings: carried {', '.join(env.get('carried') or []) or 'no keys'}; routes {', '.join(toml.get('routes') or []) or 'none'}")
        for old, new in (env.get("renamed") or {}).items():
            log(f"settings: {old} is now {new}")
        dropped = [*(env.get("dropped") or []), *(toml.get("droppedTables") or []), *(toml.get("droppedKeys") or [])]
        if dropped:
            log(f"settings: dropped {', '.join(dropped)}")
        if env.get("unreadable"):
            log(f"settings: could not open on this machine, not carried: {', '.join(env['unreadable'])}")
    for original, backup in (report.get("backups") or {}).items():
        source, target = data / original, data / backup
        if source.is_file() and not os.path.lexists(target):
            replace_path(source, target, budget=RECORD_REPLACE)
            log(f"settings: kept {original} as {backup}")
        (data / f"{original}.lock").unlink(missing_ok=True)
    report["status"] = COMPLETED
    report["finishedAt"] = _now()
    _write_report(data, report)
    return report


def undo_settings(data_dir: Path | str, *, log: Log | None = None) -> bool:
    """Put the old files back and remove what `migrate_settings` wrote (the migration's rollback, WP-M2).

    Only a migration this module recorded is undone: its report says which
    backups it made, and the stores it wrote did not exist before it (an
    existing store blocks it, `blocker`). Repeatable: a rollback killed half
    way runs again to the same end. Returns whether there was anything to undo.
    """

    log = log or (lambda message: None)
    data = Path(os.path.abspath(Path(data_dir).expanduser()))
    report = read_report(data)
    if report is None:
        return False
    for original, backup in (report.get("backups") or {}).items():
        source, target = data / backup, data / original
        if source.is_file() and not os.path.lexists(target):
            replace_path(source, target, budget=RECORD_REPLACE)
            log(f"settings: restored {original}")
    for name in report.get("locks") or ():
        lock = data / name
        if not lock.exists():
            lock.write_bytes(b"")
    for store in (SettingsStore.in_dir(data), SecretStore.in_dir(data)):
        if store.path.exists():
            store.path.unlink()
            log(f"settings: removed {store.path.name}")
        store.path.with_name(store.path.name + ".lock").unlink(missing_ok=True)
    report_path(data).unlink(missing_ok=True)
    return True


def remove_backups(data_dir: Path | str, *, log: Log | None = None) -> list[str]:
    """Delete the old files' backups: the migration has succeeded and will not roll back (WP-M2).

    Called by the migration's `leftovers` step once the deletions have ruled a
    rollback out, never before: until then `undo_settings` needs them. Repeatable. Returns the names deleted, and
    records them in the report (`backupsRemoved`).
    """

    log = log or (lambda message: None)
    data = Path(os.path.abspath(Path(data_dir).expanduser()))
    removed: list[str] = []
    for backup in legacy.settings_backups(data):
        backup.unlink(missing_ok=True)
        removed.append(backup.name)
        log(f"settings: deleted the backup {backup.name}")
    try:
        report = read_report(data)
    except SettingsMigrationError:
        report = None  # the deletions stand; the migration's own report names them too
    if report is not None and removed:
        report["backupsRemoved"] = sorted({*(report.get("backupsRemoved") or ()), *removed})
        _write_report(data, report)
    return removed


class SettingsHook:
    """The migration command's `settings` step (`nonoka_x.migration.hooks.StepHook`)."""

    def download_bytes(self, context: Any) -> int:
        return 0

    def run(self, context: Any) -> None:
        migrate_settings(context.layout.data, log=context.log)

    def undo(self, context: Any) -> None:
        undo_settings(context.layout.data, log=context.log)


__all__ = [
    "BACKUP_SUFFIX",
    "COMPLETED",
    "ENGINE_KEY_NAMES",
    "LEGACY_RENAMES",
    "REPORT_NAME",
    "SettingsHook",
    "SettingsMigrationBlocked",
    "SettingsMigrationError",
    "blocker",
    "migrate_settings",
    "needed",
    "read_legacy",
    "read_report",
    "remove_backups",
    "report_path",
    "undo_settings",
]
