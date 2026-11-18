"""Old tasks: FineSub's outputs filed as the packages' private attachments, and converted (WP-M2).

Before the engine packages a task kept both halves' outputs in one
`tasks\\<id>\\workspace\\`, and its `artifacts.json` named them there. Now each
package keeps its files of a task in `tasks\\<id>\\native\\<package>\\` (plan
4.6 item 34) and the projection and the cloud library read Nonoka's own
formats, `transcript.json` and `correction.json` (WP-L). For every old task
the `task-products` step:

1. **files** each entry of `workspace\\` by `legacy.TRANSCRIPTION_SUFFIXES` /
   `BOTH_SUFFIXES` -- the transcription's outputs (the stable and the speaker
   files among them) to the transcription package's directory, the metadata
   to both, everything else (subtitles, `annotated.csv`, `llm-artifacts\\`,
   whatever else is there) to the correction's -- bytes unchanged, by rename;
2. **converts**, for a task with a manifest and a stable: the correction
   package's `correction_json` operation (strict alignment, as a local task's
   projection always was) and the transcription package's `transcript_json`,
   the same operations WP-L's library conversion calls; the two files land in
   the task directory beside `artifacts.json`. A task the operation refuses
   (products that never lined up) keeps its files and goes without; the
   report says why;
3. **rewrites the manifests** that name the task -- its `artifacts.json` and
   the `artifacts.json` of each document projected from it (the cloud
   library's sync reads those) -- so every entry points at the file's new
   place, and adds `transcript_json` / `correction_json`. Each is backed up
   under `runtime-state\\migration-backup\\` first; the projected documents
   themselves are left as they are.

A task that already has `native\\` is not merged into (it is listed and left).
Every operation is repeatable, so a step killed anywhere runs again from the
top; `undo` puts a task back as it was (the rollback of plan WP-M2).
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import shutil
from typing import Any
from urllib.parse import unquote, urlparse
from urllib.request import url2pathname

from nonoka_x.migration import legacy
from nonoka_x.migration.hooks import MigrationContext
from nonoka_x.runtime.fsops import remove_tree, replace_path, write_atomic
from nonoka_x.runtime.paths import NATIVE_DIRECTORY_NAME

MANIFEST_NAME = "artifacts.json"
DOCUMENTS_DIRECTORY = "documents"
#: Under `runtime-state\\`: the manifests as they were, until the migration is undone or forgotten.
BACKUP_DIRECTORY = "migration-backup"
#: Under `runtime-state\\`: the conversion processes' run directories, removed when they end.
RUN_DIRECTORY = "migration-run"
#: In the task directory while a converted file is being written.
STAGING_DIRECTORY = ".migration"

TRANSCRIPT_ARTIFACT = "transcript_json"
CORRECTION_ARTIFACT = "correction_json"
#: The two files' names in a task directory (`worker.TRANSCRIPT_FILENAME`, `worker.CORRECTION_FILENAME`).
CONVERTED_FILES = {TRANSCRIPT_ARTIFACT: "transcript.json", CORRECTION_ARTIFACT: "correction.json"}

TRANSCRIPTION = "transcription"
CORRECTION = "correction"
BOTH = "both"

Log = Callable[[str], None]


class ConversionUnavailable(RuntimeError):
    """A package needed for the conversion is installed but its process failed."""


# -- which file goes where ---------------------------------------------------------------------------------------


def owner_of(name: str) -> str:
    """Whose an entry of an old `workspace\\` is: `transcription`, `correction` or `both`."""

    if name.endswith(legacy.BOTH_SUFFIXES):
        return BOTH
    if name.endswith(legacy.TRANSCRIPTION_SUFFIXES):
        return TRANSCRIPTION
    return CORRECTION


def native_dirs(task: Path) -> dict[str, Path]:
    native = task / NATIVE_DIRECTORY_NAME
    return {TRANSCRIPTION: native / legacy.TRANSCRIPTION_PACKAGE, CORRECTION: native / legacy.CORRECTION_PACKAGE}


def new_location(task: Path, relative: tuple[str, ...]) -> Path:
    """Where `workspace\\<relative>` of `task` is once filed (a `both` file: the correction's copy)."""

    owner = owner_of(relative[0])
    directory = native_dirs(task)[TRANSCRIPTION if owner == TRANSCRIPTION else CORRECTION]
    return directory.joinpath(*relative)


# -- the plan ----------------------------------------------------------------------------------------------------


def candidates(tasks_root: Path) -> list[Path]:
    try:
        return sorted(entry for entry in tasks_root.iterdir() if (entry / legacy.WORKSPACE_DIRECTORY).is_dir())
    except OSError:
        return []


def plan_actions(context: MigrationContext) -> list[Any]:
    from nonoka_x.migration import plan as planning

    actions = []
    for task in candidates(context.paths.tasks):
        workspace = task / legacy.WORKSPACE_DIRECTORY
        if os.path.lexists(task / NATIVE_DIRECTORY_NAME):
            actions.append(planning.Action(
                kind=planning.KEEP, path=str(workspace),
                note="the task already has native\\: not merged, its workspace\\ stays as it is",
            ))
            continue
        action = planning._measured(
            planning.FILE_TASK, workspace, target=task / NATIVE_DIRECTORY_NAME,
            note="FineSub's outputs to native\\<package>\\; transcript.json and correction.json written; manifests repointed",
        )
        actions.append(action)
    return actions


# -- manifests ---------------------------------------------------------------------------------------------------


def backup_root(context: MigrationContext) -> Path:
    return context.paths.runtime_state / BACKUP_DIRECTORY


def _uri_path(uri: Any) -> Path | None:
    parsed = urlparse(str(uri or ""))
    if parsed.scheme != "file" or parsed.netloc:
        return None
    return Path(url2pathname(unquote(parsed.path)))


def _workspace_relative(path: Path, task_id: str) -> tuple[str, ...] | None:
    """The part of `path` under `<...>\\tasks\\<task_id>\\workspace\\`, whatever the data directory was called then."""

    parts = path.parts
    for index in range(len(parts) - 2):
        if parts[index] == task_id and parts[index + 1] == legacy.WORKSPACE_DIRECTORY and index + 2 < len(parts):
            return tuple(parts[index + 2:])
    return None


def _entry(path: Path) -> dict[str, Any]:
    data = path.read_bytes()
    return {"uri": path.resolve().as_uri(), "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}


def repointed(manifest: Mapping[str, Any], task: Path, converted: Mapping[str, Path]) -> dict[str, Any]:
    """`manifest` with every entry in the old workspace pointing at its new place, and the converted files added."""

    artifacts = manifest.get("artifacts")
    result = dict(manifest)
    if not isinstance(artifacts, Mapping):
        return result
    entries: dict[str, Any] = {}
    for name, item in artifacts.items():
        if isinstance(item, Mapping):
            path = _uri_path(item.get("uri"))
            relative = _workspace_relative(path, task.name) if path is not None else None
            if relative is not None:
                item = {**item, "uri": new_location(task, relative).resolve().as_uri()}
        entries[name] = item
    for name, path in converted.items():
        if name not in entries and path.is_file():
            entries[name] = _entry(path)
    result["artifacts"] = entries
    return result


def _read(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, value: Any) -> None:
    write_atomic(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def _write_bytes(path: Path, data: bytes) -> None:
    """`data` at `path`, all or nothing (a temporary file beside it, then a rename)."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp")
    temporary.write_bytes(data)
    replace_path(temporary, path)


def _backup(original: Path, backup: Path) -> None:
    """Keep `original`'s bytes at `backup`, once: a second call keeps the first copy."""

    if backup.is_file() or not original.is_file():
        return
    _write_bytes(backup, original.read_bytes())


def document_manifests(data: Path) -> dict[str, list[Path]]:
    """Task id -> the `documents\\<doc>\\artifacts.json` projected from it."""

    found: dict[str, list[Path]] = {}
    try:
        documents = sorted((data / DOCUMENTS_DIRECTORY).iterdir())
    except OSError:
        return found
    for document in documents:
        manifest = document / MANIFEST_NAME
        try:
            task_id = _read(manifest).get("task_id")
        except (OSError, ValueError, AttributeError):
            continue
        if isinstance(task_id, str) and task_id:
            found.setdefault(task_id, []).append(manifest)
    return found


# -- the conversion processes ------------------------------------------------------------------------------------


class Converter:
    """The two packages' processes, started when first needed (WP-L's operations)."""

    def __init__(self, context: MigrationContext) -> None:
        self.context = context
        self._packages: Mapping[str, Any] | None = None
        self._processes: dict[str, Any] = {}
        self._run = context.paths.runtime_state / RUN_DIRECTORY

    def _package(self, kind: str) -> Any | None:
        from nonoka_x import engine_host

        if self._packages is None:
            root = self.context.engines if self.context.engines is not None else engine_host.default_engines_root()
            try:
                self._packages = engine_host.discover(root)
            except Exception as error:  # noqa: BLE001 - no packages means nothing to convert with
                self.context.log(f"  no engine packages under {root}: {error}")
                self._packages = {}
        if kind == CORRECTION:
            return engine_host.correction_package(self._packages)
        return next(iter(engine_host.of_kind(self._packages, "transcription")), None)

    def process(self, kind: str) -> Any | None:
        if kind in self._processes:
            return self._processes[kind]
        package = self._package(kind)
        if package is None:
            self._processes[kind] = None
            return None
        from nonoka_x import engine_host

        run = self._run / package.id
        if run.exists():
            remove_tree(run)
        run.mkdir(parents=True)
        context = {"schema": 1, "package_id": package.id, "paths": {"package_data": str(run), "run": str(run)}}
        try:
            process = engine_host.PackageProcess(package, context)
            process.__enter__()
        except Exception as error:  # noqa: BLE001 - reported as the step's failure
            raise ConversionUnavailable(f"the {package.id} package's process did not start: {error}") from error
        self._processes[kind] = process
        return process

    def request(self, kind: str, op: str, payload: Mapping[str, Any]) -> Mapping[str, Any] | None:
        process = self.process(kind)
        if process is None:
            return None
        from nonoka_x import engine_host

        try:
            answer = process.request(op, payload)
        except engine_host.PackageError as error:
            return {"error": str(error)}
        except engine_host.PackageUnavailable as error:
            raise ConversionUnavailable(f"the {process.package.id} package's process failed: {error}") from error
        return answer if isinstance(answer, Mapping) else {}

    def close(self) -> None:
        for process in self._processes.values():
            if process is not None:
                try:
                    process.__exit__(None, None, None)
                except Exception:  # noqa: BLE001 - it is ending anyway
                    pass
        self._processes.clear()
        if self._run.exists():
            remove_tree(self._run)


@contextmanager
def converter(context: MigrationContext) -> Iterator[Converter]:
    made = Converter(context)
    try:
        yield made
    finally:
        made.close()


# -- one task ----------------------------------------------------------------------------------------------------


def _copy_once(source: Path, target: Path) -> None:
    if target.exists():
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(f".{target.name}.part")
    shutil.copyfile(source, partial)
    replace_path(partial, target)


def _file_outputs(task: Path, log: Log) -> None:
    workspace = task / legacy.WORKSPACE_DIRECTORY
    if not workspace.is_dir():
        return
    directories = native_dirs(task)
    for entry in sorted(workspace.iterdir()):
        owner = owner_of(entry.name)
        if owner == BOTH and entry.is_file():
            _copy_once(entry, directories[TRANSCRIPTION] / entry.name)
        target = directories[TRANSCRIPTION if owner == TRANSCRIPTION else CORRECTION] / entry.name
        if os.path.lexists(target):
            raise RuntimeError(f"{target} appeared while {entry} was being filed; not merging")
        target.parent.mkdir(parents=True, exist_ok=True)
        replace_path(entry, target)
    workspace.rmdir()
    log(f"  filed {workspace} under {task / NATIVE_DIRECTORY_NAME}")


def _named(manifest: Mapping[str, Any], name: str, task: Path) -> Path | None:
    item = (manifest.get("artifacts") or {}).get(name)
    path = _uri_path(item.get("uri")) if isinstance(item, Mapping) else None
    if path is None:
        return None
    relative = _workspace_relative(path, task.name)
    path = new_location(task, relative) if relative is not None else path
    return path if path.is_file() else None


def _language(task: Path) -> str:
    try:
        request = _read(task / "request.json")
    except (OSError, ValueError):
        return "ja"
    language = request.get("language") if isinstance(request, Mapping) else None
    return str(language or "ja")


def _convert(task: Path, original: Mapping[str, Any], tools: Converter, log: Log) -> dict[str, str]:
    """Write the task's `correction.json` and `transcript.json` when they are missing. Returns what was refused, by name."""

    refused: dict[str, str] = {}
    listed = set((original.get("artifacts") or {}))
    stable = _named(original, "stable_json", task)
    if stable is None:
        return refused
    staging = task / STAGING_DIRECTORY
    jobs = {
        CORRECTION_ARTIFACT: (CORRECTION, "correction_json", {
            "stable_json": str(stable),
            "annotated_csv": str(path) if (path := _named(original, "annotated_csv", task)) else None,
            "final_srt": str(path) if (path := _named(original, "final_srt", task)) else None,
            "correction_mode": str(path) if (path := _named(original, "correction_mode_json", task)) else None,
        }),
        TRANSCRIPT_ARTIFACT: (TRANSCRIPTION, "transcript_json", {
            "stable_json": str(stable),
            "language": _language(task),
            "vad_json": str(path) if (path := _named(original, "vad_json", task)) else None,
        }),
    }
    for name, (kind, op, payload) in jobs.items():
        target = task / CONVERTED_FILES[name]
        if name in listed or target.exists():
            continue
        if kind == CORRECTION and not (payload["annotated_csv"] and payload["final_srt"]):
            # The stable alone, as the projection of a task no correction took further read it.
            payload = {key: value for key, value in payload.items() if key == "stable_json"}
        staged = staging / CONVERTED_FILES[name]
        staging.mkdir(exist_ok=True)
        staged.unlink(missing_ok=True)
        answer = tools.request(kind, op, {**{k: v for k, v in payload.items() if v is not None}, "output": str(staged)})
        if answer is None:
            refused[name] = f"no {kind} package installed"
            continue
        if answer.get("error") or not staged.is_file():
            refused[name] = str(answer.get("error") or "nothing written")
            log(f"  {task.name}: no {CONVERTED_FILES[name]} ({refused[name]})")
            continue
        replace_path(staged, target)
    if staging.is_dir():
        remove_tree(staging)
    return refused


def file_task(task: Path, context: MigrationContext, tools: Converter, documents: Mapping[str, list[Path]]) -> dict[str, Any]:
    """File, convert and repoint one task. Repeatable. Returns what the report says of it."""

    _file_outputs(task, context.log)
    record: dict[str, Any] = {"filed": True}
    manifest = task / MANIFEST_NAME
    backups = backup_root(context)
    task_backup = backups / "tasks" / task.name / MANIFEST_NAME
    _backup(manifest, task_backup)
    if not task_backup.is_file():
        return record
    original = _read(task_backup)
    if not isinstance(original, Mapping):
        return record
    refused = _convert(task, original, tools, context.log)
    if refused:
        record["refused"] = refused
    converted = {name: task / filename for name, filename in CONVERTED_FILES.items()}
    _write_json(manifest, repointed(original, task, converted))
    for document in documents.get(task.name, []):
        backup = backups / DOCUMENTS_DIRECTORY / document.parent.name / MANIFEST_NAME
        _backup(document, backup)
        if backup.is_file():
            _write_json(document, repointed(_read(backup), task, converted))
    record["converted"] = sorted(name for name, path in converted.items() if path.is_file() and name not in (original.get("artifacts") or {}))
    return record


def file_tasks(actions: list[Any], context: MigrationContext) -> dict[str, Any]:
    """The `task-products` step. Returns the report, by task id."""

    from nonoka_x.migration import plan as planning

    tasks = [Path(action.path).parent for action in actions if action.kind == planning.FILE_TASK]
    report: dict[str, Any] = {}
    if not tasks:
        return report
    documents = document_manifests(context.paths.data)
    with converter(context) as tools:
        for task in tasks:
            report[task.name] = file_task(task, context, tools, documents)
    return report


# -- undoing it --------------------------------------------------------------------------------------------------


def undo_task(task: Path, context: MigrationContext, documents: Mapping[str, list[Path]]) -> None:
    """Put one task back as it was before `file_task`. Repeatable."""

    backups = backup_root(context)
    for document in documents.get(task.name, []):
        backup = backups / DOCUMENTS_DIRECTORY / document.parent.name / MANIFEST_NAME
        if backup.is_file():
            _write_bytes(document, backup.read_bytes())
            backup.unlink()
    task_backup = backups / "tasks" / task.name / MANIFEST_NAME
    listed: set[str] = set()
    if task_backup.is_file():
        original = _read(task_backup)
        listed = set((original.get("artifacts") or {}) if isinstance(original, Mapping) else ())
        _write_bytes(task / MANIFEST_NAME, task_backup.read_bytes())
        task_backup.unlink()
        # Only a task with a manifest was converted, and only what the manifest did not list.
        for name, filename in CONVERTED_FILES.items():
            if name not in listed:
                (task / filename).unlink(missing_ok=True)
    if (task / STAGING_DIRECTORY).is_dir():
        remove_tree(task / STAGING_DIRECTORY)
    directories = native_dirs(task)
    workspace = task / legacy.WORKSPACE_DIRECTORY
    for kind in (CORRECTION, TRANSCRIPTION):
        directory = directories[kind]
        if not directory.is_dir():
            continue
        for entry in sorted(directory.iterdir()):
            if entry.name.startswith(".") and entry.name.endswith(".part"):
                entry.unlink()
                continue
            target = workspace / entry.name
            if os.path.lexists(target):
                # The transcription's copy of a file both halves keep: the workspace has it back already.
                if kind == TRANSCRIPTION and owner_of(entry.name) == BOTH:
                    entry.unlink()
                    continue
                raise RuntimeError(f"{target} is back already; not overwriting it with {entry}")
            workspace.mkdir(exist_ok=True)
            replace_path(entry, target)
        directory.rmdir()
    native = task / NATIVE_DIRECTORY_NAME
    if native.is_dir() and not any(native.iterdir()):
        native.rmdir()


def undo_tasks(actions: list[Any], context: MigrationContext) -> None:
    from nonoka_x.migration import plan as planning

    tasks = [Path(action.path).parent for action in actions if action.kind == planning.FILE_TASK]
    if not tasks:
        return
    documents = document_manifests(context.paths.data)
    for task in reversed(tasks):
        undo_task(task, context, documents)
        context.log(f"  restored {task / legacy.WORKSPACE_DIRECTORY}")


def forget_backups(context: MigrationContext) -> None:
    """Once the migration is complete the manifests' backups are of no further use."""

    root = backup_root(context)
    if root.exists():
        remove_tree(root)


__all__ = [
    "ConversionUnavailable",
    "Converter",
    "file_task",
    "file_tasks",
    "forget_backups",
    "new_location",
    "owner_of",
    "plan_actions",
    "repointed",
    "undo_task",
    "undo_tasks",
]
