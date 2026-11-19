"""Carrying out a migration plan, one journaled step at a time.

The journal is `<main data directory>\\runtime-state\\migration.json`
(user 2026-11-12). It holds the plan as it was shown and each step's
progress. A step is marked `running` before it starts and `done` (or
`skipped`, for a deferred step nobody has implemented yet) when it ends, each
with an atomic write. Every step is idempotent: renames are atomic and are
not repeated once their source is gone, deletions carry on with whatever is
left. So a migration killed at any point continues from the journal: finished
steps are passed over, the interrupted one runs again, and the plan is the
journal's, never a fresh guess about a half-migrated disk.

**Rollback** (WP-M2). The steps up to `task-products` (`plan.REVERSIBLE`)
change nothing that cannot be put back. When one of them fails -- raises,
as opposed to the process dying -- every one of them that ran is undone in
reverse order, the journal says `rolled-back` with the failure, and
`MigrationRolledBack` is raised: the installation is in its old layout
again, and the next attempt plans afresh. A rollback killed half way is
finished first by the next `execute`. Once `old-environment` has begun there
is no rollback (the old environment is gone): a failure leaves the journal
`running` and the next attempt continues.

**Only once.** A journal that says `completed` is the mark that the
installation is migrated: `prepare` reads nothing else of the disk then, so
nothing reads the old paths again. A step an older journal skipped (its owner
had not implemented it yet) and that can run now is the one exception: the
journal is reopened for it (`catch-up`).

Downloads happen only in the two WP-E3 steps, which arrive as hooks;
`stop_before` lets the sidecar's start-up run the local steps first and the
downloads after the server is up (`migration.startup`).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from typing import Any

from nonoka_x.migration import hooks as deferred
from nonoka_x.migration import legacy
from nonoka_x.migration import plan as planning
from nonoka_x.migration.hooks import Hooks, MigrationContext
from nonoka_x.migration.legacy import LegacyLayout
from nonoka_x.migration.plan import MigrationPlan, Step
from nonoka_x.runtime.fsops import (
    LockUnavailable,
    holding_lock,
    is_directory_link,
    remove_tree,
    replace_path,
    write_atomic,
)
from nonoka_x.runtime.paths import RUNTIME_STATE_DIRECTORY_NAME, NonokaPaths

JOURNAL_SCHEMA = 1
JOURNAL_NAME = "migration.json"
LOCK_NAME = "migration.lock"

# Step statuses.
RUNNING = "running"
DONE = "done"
SKIPPED = "skipped"
PENDING = "pending"
FAILED = "failed"
UNDONE = "undone"

# Journal statuses besides RUNNING and planning.COMPLETED.
ROLLING_BACK = planning.ROLLING_BACK
ROLLED_BACK = "rolled-back"

Log = Callable[[str], None]
Progress = Callable[[Any], None]


class MigrationBlocked(RuntimeError):
    """The plan has blockers; nothing was changed."""


class MigrationError(RuntimeError):
    """A step found the disk in a state it cannot safely continue from, or failed past the point of rollback."""

    def __init__(self, message: str, *, step: str | None = None) -> None:
        super().__init__(message)
        self.step = step


class MigrationRolledBack(MigrationError):
    """A settings or data step failed; everything the migration had done was undone."""


def journal_path(data_dir: Path | str) -> Path:
    return Path(data_dir) / RUNTIME_STATE_DIRECTORY_NAME / JOURNAL_NAME


def read_journal(data_dir: Path | str) -> dict[str, Any] | None:
    try:
        record = json.loads(journal_path(data_dir).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as error:
        raise MigrationError(f"the migration journal {journal_path(data_dir)} cannot be read: {error}") from error
    if not isinstance(record, dict) or record.get("schema") != JOURNAL_SCHEMA:
        raise MigrationError(f"the migration journal {journal_path(data_dir)} has an unknown format")
    return record


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _runnable(step: Step, hooks: Hooks | None) -> bool:
    return not step.deferred or bool(hooks and step.id in hooks)


def _context(plan: MigrationPlan, log: Log | None = None, progress: Progress | None = None, engines: Path | None = None) -> MigrationContext:
    layout = plan.layout()
    return MigrationContext(
        layout=layout,
        paths=NonokaPaths.for_roots(layout.data, layout.new_root),
        log=log or (lambda message: None),
        progress=progress,
        engines=Path(engines) if engines is not None else None,
    )


def _unfinished(plan: MigrationPlan, step: Step) -> list[str]:
    """For `leftovers`: earlier steps with work that were skipped (the settings step without its hook).

    A skipped settings step makes its backups when it catches up later, so the
    cleanup waits for it; the downloads, after it, do not hold it back.
    """

    if step.id != planning.LEFTOVERS:
        return []
    earlier = set(planning.STEP_ORDER[: planning.STEP_ORDER.index(planning.LEFTOVERS)])
    return [other.id for other in plan.steps if other.id in earlier and other.progress == SKIPPED and other.actions]


def _add_missing_steps(plan: MigrationPlan, context: MigrationContext) -> bool:
    """Put the steps a journal's plan lacks into it, in `STEP_ORDER`. Returns whether one has work to do."""

    present = {step.id for step in plan.steps}
    if all(step_id in present for step_id in planning.STEP_ORDER):
        return False
    found = False
    by_id = {step.id: step for step in plan.steps}
    ordered: list[Step] = []
    for step_id in planning.STEP_ORDER:
        if step_id in by_id:
            ordered.append(by_id.pop(step_id))
            continue
        step = Step(id=step_id, title=planning.TITLES[step_id], actions=planning.step_actions(step_id, context, plan.blockers))
        found = found or bool(step.actions)
        ordered.append(step)
    plan.steps = ordered + list(by_id.values())
    return found


def prepare(
    data_dir: Path | str,
    *,
    checkout: Path | str | None = None,
    hooks: Hooks | None = None,
    startup: bool = False,
) -> MigrationPlan:
    """The plan for the installation at `data_dir`. Reads only.

    A journal decides first: an interrupted migration continues with its own
    plan (only the running-task check is repeated), an interrupted rollback is
    to be finished, a finished migration is reported as such -- unless a step
    it skipped can run now -- and a rolled-back one is planned afresh.
    Otherwise the disk is inspected afresh. `startup`: see `plan.build_plan`.
    """

    journal = read_journal(data_dir)
    if journal is None or journal.get("status") == ROLLED_BACK:
        return planning.build_plan(legacy.detect(data_dir, checkout=checkout), hooks, startup=startup)
    plan = MigrationPlan.from_dict(journal["plan"])
    progress = journal.get("steps") or {}
    for step in plan.steps:
        if step.id in deferred.FORMERLY_DEFERRED:
            step.owner = None
        step.progress = (progress.get(step.id) or {}).get("status", PENDING)
        if step.deferred and step.progress != DONE:
            step.hooked = bool(hooks and step.id in hooks)
    plan.blockers = []
    status = journal.get("status")
    if status == ROLLING_BACK:
        plan.state = planning.ROLLING_BACK
        return plan
    context = _context(plan)
    # Steps a journal of an older build does not list (`leftovers`, WP-M2 stage 2): listed from the disk now.
    added = _add_missing_steps(plan, context)
    catch_up = [step for step in plan.steps if step.progress == SKIPPED and step.actions and _runnable(step, hooks)]
    leftovers = next((step for step in plan.steps if step.id == planning.LEFTOVERS), None)
    if leftovers in catch_up and any(
        step.id in _unfinished(plan, leftovers) and step not in catch_up for step in plan.steps
    ):
        # The backups stay while a skipped step still cannot run: the migration has not succeeded yet.
        catch_up = [step for step in catch_up if step.id != planning.LEFTOVERS]
    if status == planning.COMPLETED and not catch_up and not added:
        plan.state = planning.COMPLETED
        return plan
    # Skipped by an older build that had nothing to run them with: listed from the disk as it is now.
    for step in catch_up:
        step.progress = PENDING
        step.actions = planning.step_actions(step.id, context, plan.blockers)
    plan.state = planning.RESUMING
    if not startup:
        plan.blockers.extend(planning.task_blockers(Path(plan.data)))
    return plan


def execute(
    plan: MigrationPlan,
    *,
    hooks: Hooks | None = None,
    log: Log | None = None,
    progress: Progress | None = None,
    engines: Path | str | None = None,
    stop_before: str | None = None,
) -> MigrationPlan:
    """Carry out `plan` (from `prepare`), journaling every step. Returns it with each step's progress.

    `stop_before`: a step id; the steps from it on are left for a later call
    (the journal stays `running`, and `plan.state` is `resuming`).
    Raises `MigrationRolledBack` when a reversible step failed and was undone,
    `MigrationError` when a later one failed (run again to continue).
    """

    log = log or (lambda message: None)
    hooks = hooks or {}
    if plan.state in (planning.NOTHING, planning.COMPLETED):
        return plan
    if plan.blockers:
        raise MigrationBlocked("; ".join(plan.blockers))
    context = _context(plan, log, progress, engines)
    journal_file = journal_path(context.layout.data)
    try:
        with holding_lock(journal_file.with_name(LOCK_NAME), timeout=0):
            journal = read_journal(context.layout.data)
            if journal is None or journal.get("status") == ROLLED_BACK:
                journal = {
                    "schema": JOURNAL_SCHEMA,
                    "status": RUNNING,
                    "started_at": _now(),
                    "plan": plan.to_dict(),
                    "steps": {},
                }
                _write(journal_file, journal)
            elif journal.get("status") == ROLLING_BACK:
                _rollback(plan, journal, journal_file, context, hooks)
                failure = journal.get("error") or {}
                raise MigrationRolledBack(str(failure.get("message") or "a step failed"), step=failure.get("step"))
            elif journal.get("status") == planning.COMPLETED and plan.state == planning.COMPLETED:
                return plan
            else:
                # Catch-up: the skipped steps `prepare` reopened run with the actions it listed now.
                recorded = journal.setdefault("steps", {})
                reopened = [
                    step.id for step in plan.steps
                    if step.progress == PENDING and (recorded.get(step.id) or {}).get("status") == SKIPPED
                ]
                listed = {item.get("id") for item in (journal.get("plan") or {}).get("steps", ())}
                grown = any(step.id not in listed for step in plan.steps)
                if reopened or grown or journal.get("status") == planning.COMPLETED:
                    for step_id in reopened:
                        recorded.pop(step_id, None)
                    journal["status"] = RUNNING
                    journal["plan"] = plan.to_dict()
                    _write(journal_file, journal)
            steps = journal.setdefault("steps", {})
            for step in plan.steps:
                if step.id == stop_before:
                    plan.state = planning.RESUMING
                    return plan
                previous = (steps.get(step.id) or {}).get("status")
                if previous in (DONE, SKIPPED):
                    step.progress = previous
                    continue
                waiting = _unfinished(plan, step)
                if (step.deferred and step.id not in hooks) or waiting:
                    if waiting:
                        note = "waits for " + ", ".join(waiting)
                    else:
                        note = f"implemented by {step.owner}" if step.actions else "nothing to do"
                    steps[step.id] = {"status": SKIPPED, "at": _now(), "note": note}
                    _write(journal_file, journal)
                    step.progress = SKIPPED
                    if step.actions:
                        log(f"{step.id}: skipped ({note})")
                    continue
                earlier = (steps.get(step.id) or {}).get("removing") or []
                steps[step.id] = {"status": RUNNING, "started_at": _now()}
                if step.id == planning.LEFTOVERS:
                    # Named before anything goes: a run killed half way still reports all of it.
                    steps[step.id]["removing"] = sorted({*earlier, *leftover_names(context.layout.data)})
                _write(journal_file, journal)
                step.progress = RUNNING
                report = None
                if step.actions:
                    log(f"{step.id}: {step.title}")
                    try:
                        if step.deferred:
                            hooks[step.id].run(context)
                        else:
                            report = _RUNNERS[step.id](step, context)
                    except Exception as error:  # noqa: BLE001 - recorded, then rolled back or reported
                        _failed(plan, step, error, journal, journal_file, context, hooks)
                steps[step.id] = {**steps[step.id], "status": DONE, "finished_at": _now()}
                if step.id == planning.LEFTOVERS and steps[step.id].get("removing"):
                    report = _leftovers_report(steps[step.id].pop("removing"), context.layout.data)
                if report:
                    steps[step.id]["report"] = report
                _write(journal_file, journal)
                step.progress = DONE
            # The backups only serve a rollback, which the deletions have ruled out by now.
            _forget(context)
            journal["status"] = planning.COMPLETED
            journal["finished_at"] = _now()
            journal.pop("error", None)
            _write(journal_file, journal)
            plan.state = planning.COMPLETED
    except LockUnavailable as error:
        raise MigrationError("another migration is running for this data directory") from error
    return plan


def mark_nothing_to_do(data_dir: Path | str, plan: MigrationPlan) -> None:
    """Record that the installation has no old layout: start-up never looks for one again ("only once")."""

    journal_file = journal_path(data_dir)
    try:
        with holding_lock(journal_file.with_name(LOCK_NAME), timeout=0):
            if read_journal(data_dir) is not None:
                return
            _write(journal_file, {
                "schema": JOURNAL_SCHEMA,
                "status": planning.COMPLETED,
                "started_at": _now(),
                "finished_at": _now(),
                "plan": plan.to_dict(),
                "steps": {},
            })
    except LockUnavailable as error:
        raise MigrationError("another migration is running for this data directory") from error


def _failed(
    plan: MigrationPlan,
    step: Step,
    error: Exception,
    journal: dict[str, Any],
    journal_file: Path,
    context: MigrationContext,
    hooks: Hooks,
) -> None:
    message = f"{error.__class__.__name__}: {error}" if not isinstance(error, MigrationError) else str(error)
    steps = journal["steps"]
    steps[step.id] = {**steps[step.id], "status": FAILED, "failed_at": _now(), "error": message}
    journal["error"] = {"step": step.id, "message": message, "at": _now()}
    irreversible_begun = any(
        (steps.get(other.id) or {}).get("status") in (RUNNING, DONE, FAILED)
        for other in plan.steps
        if other.id not in planning.REVERSIBLE and other.id != step.id
    )
    if step.id not in planning.REVERSIBLE or irreversible_begun:
        _write(journal_file, journal)
        context.log(f"{step.id}: failed ({message}); run again to continue")
        raise MigrationError(f"{step.id}: {message}", step=step.id) from error
    journal["status"] = ROLLING_BACK
    _write(journal_file, journal)
    context.log(f"{step.id}: failed ({message}); rolling back")
    _rollback(plan, journal, journal_file, context, hooks)
    raise MigrationRolledBack(f"{step.id}: {message}", step=step.id) from error


def _rollback(
    plan: MigrationPlan,
    journal: dict[str, Any],
    journal_file: Path,
    context: MigrationContext,
    hooks: Hooks,
) -> None:
    """Undo every reversible step that ran, last first; repeatable (a killed rollback runs again)."""

    steps = journal.setdefault("steps", {})
    for step in reversed(plan.steps):
        if step.id not in planning.REVERSIBLE:
            continue
        status = (steps.get(step.id) or {}).get("status")
        if status not in (RUNNING, DONE, FAILED):
            continue
        if step.actions:
            context.log(f"{step.id}: undo")
            if step.deferred:
                undo = getattr(hooks.get(step.id), "undo", None)
                if undo is None and step.id == deferred.SETTINGS:
                    from nonoka_x.config.migrate import undo_settings

                    undo_settings(context.layout.data, log=context.log)
                elif undo is not None:
                    undo(context)
            else:
                _UNDO[step.id](step, context)
        steps[step.id] = {**steps[step.id], "status": UNDONE, "undone_at": _now()}
        _write(journal_file, journal)
    _forget(context)
    journal["status"] = ROLLED_BACK
    journal["rolled_back_at"] = _now()
    _write(journal_file, journal)
    plan.state = planning.FRESH


def _forget(context: MigrationContext) -> None:
    from nonoka_x.migration import tasks

    tasks.forget_backups(context)


def _write(path: Path, journal: dict[str, Any]) -> None:
    write_atomic(path, json.dumps(journal, ensure_ascii=False, indent=2) + "\n")


# -- the steps ---------------------------------------------------------------------------------------------------


def _locate(context: MigrationContext, original: str | Path) -> Path:
    """Where something the plan named by its pre-migration location is now."""

    path = context.layout.current(Path(original))
    old_root = context.layout.old_root
    if old_root is None:
        return path
    models = context.layout.new_root / legacy.OLD_MODELS_DIRECTORY
    try:
        relative = path.relative_to(models)
    except ValueError:
        return path
    if not os.path.lexists(models) and os.path.lexists(context.migrated_models):
        return context.migrated_models / relative
    return path


def _runtime_root(step: Step, context: MigrationContext) -> None:
    layout = context.layout
    old, new = layout.old_root, layout.new_root
    assert old is not None
    if os.path.lexists(old):
        if os.path.lexists(new):
            raise MigrationError(f"{new} appeared after the plan was made; not merging {old} into it")
        if is_directory_link(old):
            raise MigrationError(f"{old} is a directory link")
        context.log(f"  rename {old} -> {new}")
        replace_path(old, new)
    elif not os.path.isdir(new):
        raise MigrationError(f"neither {old} nor {new} exists")
    if layout.recorded:
        record = legacy.storage_record_path(layout.data)
        backup = _storage_backup(context)
        if not backup.is_file() and record.is_file():
            # The record as it was, byte for byte: what a rollback writes back.
            _write_bytes(backup, record.read_bytes())
        _rewrite_storage_record(layout, layout.new_root, context.log)


def _write_bytes(path: Path, data: bytes) -> None:
    from nonoka_x.migration import tasks

    tasks._write_bytes(path, data)


def _storage_backup(context: MigrationContext) -> Path:
    from nonoka_x.migration import tasks

    return tasks.backup_root(context) / legacy.storage_record_path(context.layout.data).name


def _rewrite_storage_record(layout: LegacyLayout, root: Path, log: Log) -> None:
    """Point `storage.json`'s `runtimeDir` at `root`, keeping every other field as it is."""

    path = legacy.storage_record_path(layout.data)
    record = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(record, dict):
        raise MigrationError(f"{path} is not a JSON object")
    if record.get("runtimeDir") == str(root):
        return
    record["runtimeDir"] = str(root)
    log(f"  rewrite {path}: runtimeDir -> {root}")
    # The desktop writes it with two-space indentation and a final newline
    # (`desktop/internal/storage/locations.go`, `save`).
    write_atomic(path, json.dumps(record, ensure_ascii=False, indent=2) + "\n")


def _models(step: Step, context: MigrationContext) -> None:
    models = context.layout.new_root / legacy.OLD_MODELS_DIRECTORY
    target = context.migrated_models
    if os.path.lexists(models):
        if os.path.lexists(target):
            raise MigrationError(f"{target} appeared after the plan was made; not merging {models} into it")
        if is_directory_link(models):
            raise MigrationError(f"{models} is a directory link")
        target.parent.mkdir(parents=True, exist_ok=True)
        context.log(f"  rename {models} -> {target}")
        replace_path(models, target)
    if not target.is_dir():
        raise MigrationError(f"{target} is missing")


def _move_entries(step: Step, context: MigrationContext) -> None:
    """The knowledge base and the rate-limit state: renamed into the packages' directories."""

    for action in step.actions:
        source = Path(action.path)
        if action.kind == planning.RENAME:
            target = Path(action.target or "")
            if not os.path.lexists(source):
                if os.path.lexists(target):
                    continue
                raise MigrationError(f"neither {source} nor {target} exists")
            if os.path.lexists(target):
                raise MigrationError(f"{target} appeared after the plan was made; not merging {source} into it")
            target.parent.mkdir(parents=True, exist_ok=True)
            context.log(f"  rename {source} -> {target}")
            replace_path(source, target)
        elif action.kind == planning.DELETE and os.path.lexists(source):
            context.log(f"  delete {source}")
            if source.is_dir() and not is_directory_link(source):
                remove_tree(source)
            else:
                source.unlink(missing_ok=True)


def _task_products(step: Step, context: MigrationContext) -> dict[str, Any] | None:
    from nonoka_x.migration import tasks

    try:
        report = tasks.file_tasks(step.actions, context)
    except tasks.ConversionUnavailable as error:
        raise MigrationError(str(error)) from error
    refused = {task: record["refused"] for task, record in report.items() if record.get("refused")}
    return {"tasks": len(report), **({"not_converted": refused} if refused else {})}


def _delete(step: Step, context: MigrationContext) -> None:
    for action in step.actions:
        if action.kind not in (planning.DELETE, planning.DELETE_IF_EMPTY):
            continue
        path = _locate(context, action.path)
        if not os.path.lexists(path):
            continue
        if action.kind == planning.DELETE_IF_EMPTY:
            try:
                path.rmdir()
            except OSError:
                context.log(f"  kept {path}: not empty")
                continue
            context.log(f"  delete {path}")
            continue
        if is_directory_link(path) or path.is_dir():
            context.log(f"  delete {path}")
            remove_tree(path)
        else:
            context.log(f"  delete {path}")
            path.unlink(missing_ok=True)


def leftover_names(data: Path) -> list[str]:
    """What the `leftovers` step deletes, relative to the main data directory: the settings backups, `finesub-data\\` and what is in it."""

    names = [backup.name for backup in legacy.settings_backups(data)]
    finesub_data = data / legacy.FINESUB_DATA_DIRECTORY
    if os.path.lexists(finesub_data):
        try:
            inside = sorted(entry.name for entry in finesub_data.iterdir()) if finesub_data.is_dir() else []
        except OSError:
            inside = []
        names.extend(f"{legacy.FINESUB_DATA_DIRECTORY}\\{name}" for name in inside)
        names.append(f"{legacy.FINESUB_DATA_DIRECTORY}\\")
    return names


def _leftovers_report(removed: list[str], data: Path) -> dict[str, Any]:
    """What the `leftovers` step deleted, and the keys that went with the key file's backup.

    A key this machine could not open was not carried (`config.migrate`); its
    only copy was the backup, deleted all the same (owner 2026-11-19, plan
    appendix B question 90). The report names those keys -- names only.
    """

    from nonoka_x.config.migrate import ENV_FILE, SettingsMigrationError, read_report

    report: dict[str, Any] = {"removed": removed}
    if any(name.startswith(ENV_FILE + legacy.SETTINGS_BACKUP_SUFFIX) for name in removed):
        try:
            settings = read_report(data) or {}
        except SettingsMigrationError:
            settings = {}
        lost = list((settings.get("env") or {}).get("unreadable") or [])
        if lost:
            report["unreadableKeysRemoved"] = lost
    return report


def _leftovers(step: Step, context: MigrationContext) -> None:
    """The `leftovers` step: the settings backups and the rest of `finesub-data\\` (plan appendix B question 89).

    Found on the disk as it is now: the backups did not exist yet when the
    plan was made. What goes is written into the journal before anything is
    deleted (`execute`), so the report stays whole across an interruption.
    """

    from nonoka_x.config.migrate import remove_backups

    data = context.layout.data
    remove_backups(data, log=context.log)
    finesub_data = data / legacy.FINESUB_DATA_DIRECTORY
    if os.path.lexists(finesub_data):
        context.log(f"  delete {finesub_data}")
        if is_directory_link(finesub_data) or finesub_data.is_dir():
            remove_tree(finesub_data)
        else:
            finesub_data.unlink(missing_ok=True)


_RUNNERS: dict[str, Callable[[Step, MigrationContext], Any]] = {
    planning.RUNTIME_ROOT: _runtime_root,
    planning.MODELS: _models,
    deferred.KNOWLEDGE: _move_entries,
    deferred.LIMIT_STATE: _move_entries,
    deferred.TASK_PRODUCTS: _task_products,
    planning.OLD_ENVIRONMENT: _delete,
    planning.CACHES: _delete,
    planning.REBUILDABLE: _delete,
    planning.LEFTOVERS: _leftovers,
}


# -- undoing them --------------------------------------------------------------------------------------------------


def _remove_if_empty(*directories: Path) -> None:
    for directory in directories:
        try:
            directory.rmdir()
        except OSError:
            pass


def _undo_runtime_root(step: Step, context: MigrationContext) -> None:
    layout = context.layout
    old, new = layout.old_root, layout.new_root
    assert old is not None
    if os.path.lexists(new) and not os.path.lexists(old):
        context.log(f"  rename {new} -> {old}")
        replace_path(new, old)
    record, backup = legacy.storage_record_path(layout.data), _storage_backup(context)
    if layout.recorded and backup.is_file():
        context.log(f"  restore {record}")
        _write_bytes(record, backup.read_bytes())
        backup.unlink()
    elif layout.recorded and record.is_file():
        _rewrite_storage_record(layout, old, context.log)


def _undo_models(step: Step, context: MigrationContext) -> None:
    root = context.layout.new_root if not os.path.lexists(context.layout.old_root or Path()) else context.layout.old_root
    models = root / legacy.OLD_MODELS_DIRECTORY
    target = NonokaPaths.for_roots(context.layout.data, root).layer_version(deferred.MODELS_LAYER, deferred.MIGRATED_MODELS_NAME)
    if os.path.lexists(target) and not os.path.lexists(models):
        context.log(f"  rename {target} -> {models}")
        replace_path(target, models)
    _remove_if_empty(target.parent, target.parent.parent)


def _undo_moves(step: Step, context: MigrationContext) -> None:
    for action in reversed(step.actions):
        if action.kind != planning.RENAME:
            continue
        source, target = Path(action.path), Path(action.target or "")
        if os.path.lexists(target) and not os.path.lexists(source):
            context.log(f"  rename {target} -> {source}")
            replace_path(target, source)
        _remove_if_empty(target.parent)
    _remove_if_empty(context.paths.engines)


def _undo_task_products(step: Step, context: MigrationContext) -> None:
    from nonoka_x.migration import tasks

    tasks.undo_tasks(step.actions, context)


_UNDO: dict[str, Callable[[Step, MigrationContext], None]] = {
    planning.RUNTIME_ROOT: _undo_runtime_root,
    planning.MODELS: _undo_models,
    deferred.KNOWLEDGE: _undo_moves,
    deferred.LIMIT_STATE: _undo_moves,
    deferred.TASK_PRODUCTS: _undo_task_products,
}

__all__ = [
    "JOURNAL_NAME",
    "MigrationBlocked",
    "MigrationError",
    "MigrationRolledBack",
    "ROLLED_BACK",
    "execute",
    "journal_path",
    "mark_nothing_to_do",
    "prepare",
    "read_journal",
]
