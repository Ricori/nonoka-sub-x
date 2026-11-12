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

Nothing here downloads anything: the two steps that will (WP-E3) arrive as
hooks.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from typing import Any

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

RUNNING = "running"
DONE = "done"
SKIPPED = "skipped"
PENDING = "pending"

Log = Callable[[str], None]


class MigrationBlocked(RuntimeError):
    """The plan has blockers; nothing was changed."""


class MigrationError(RuntimeError):
    """A step found the disk in a state it cannot safely continue from."""


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


def prepare(data_dir: Path | str, *, checkout: Path | str | None = None, hooks: Hooks | None = None) -> MigrationPlan:
    """The plan for the installation at `data_dir`. Reads only.

    A journal decides first: an interrupted migration continues with its own
    plan (only the running-task check is repeated), a finished one is
    reported as such. Otherwise the disk is inspected afresh.
    """

    journal = read_journal(data_dir)
    if journal is not None:
        plan = MigrationPlan.from_dict(journal["plan"])
        progress = journal.get("steps") or {}
        for step in plan.steps:
            step.progress = (progress.get(step.id) or {}).get("status", PENDING)
            if step.deferred and step.progress != DONE:
                step.hooked = bool(hooks and step.id in hooks)
        if journal.get("status") == planning.COMPLETED:
            plan.state = planning.COMPLETED
            plan.blockers = []
        else:
            plan.state = planning.RESUMING
            plan.blockers = [
                f"task {task_id} is {state}: finish or cancel it first"
                for task_id, state in legacy.running_tasks(Path(plan.data))
            ]
        return plan
    return planning.build_plan(legacy.detect(data_dir, checkout=checkout), hooks)


def execute(plan: MigrationPlan, *, hooks: Hooks | None = None, log: Log | None = None) -> MigrationPlan:
    """Carry out `plan` (from `prepare`), journaling every step. Returns it with each step's progress."""

    log = log or (lambda message: None)
    hooks = hooks or {}
    if plan.state in (planning.NOTHING, planning.COMPLETED):
        return plan
    if plan.blockers:
        raise MigrationBlocked("; ".join(plan.blockers))
    layout = plan.layout()
    context = MigrationContext(layout=layout, paths=NonokaPaths.for_roots(layout.data, layout.new_root), log=log)
    journal_file = journal_path(layout.data)
    try:
        with holding_lock(journal_file.with_name(LOCK_NAME), timeout=0):
            journal = read_journal(layout.data)
            if journal is None:
                journal = {
                    "schema": JOURNAL_SCHEMA,
                    "status": RUNNING,
                    "started_at": _now(),
                    "plan": plan.to_dict(),
                    "steps": {},
                }
                _write(journal_file, journal)
            elif journal.get("status") == planning.COMPLETED:
                plan.state = planning.COMPLETED
                return plan
            steps = journal.setdefault("steps", {})
            for step in plan.steps:
                previous = (steps.get(step.id) or {}).get("status")
                if previous in (DONE, SKIPPED):
                    step.progress = previous
                    continue
                if step.deferred and step.id not in hooks:
                    note = f"implemented by {step.owner}" if step.actions else "nothing to do"
                    steps[step.id] = {"status": SKIPPED, "at": _now(), "note": note}
                    _write(journal_file, journal)
                    step.progress = SKIPPED
                    if step.actions:
                        log(f"{step.id}: skipped ({note})")
                    continue
                steps[step.id] = {"status": RUNNING, "started_at": _now()}
                _write(journal_file, journal)
                step.progress = RUNNING
                if step.actions:
                    log(f"{step.id}: {step.title}")
                    if step.deferred:
                        hooks[step.id].run(context)
                    else:
                        _RUNNERS[step.id](step, context)
                steps[step.id] = {**steps[step.id], "status": DONE, "finished_at": _now()}
                _write(journal_file, journal)
                step.progress = DONE
            journal["status"] = planning.COMPLETED
            journal["finished_at"] = _now()
            _write(journal_file, journal)
            plan.state = planning.COMPLETED
    except LockUnavailable as error:
        raise MigrationError("another migration is running for this data directory") from error
    return plan


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
        _rewrite_storage_record(layout, context.log)


def _rewrite_storage_record(layout: LegacyLayout, log: Log) -> None:
    """Point `storage.json`'s `runtimeDir` at the new root, keeping every other field as it is."""

    path = legacy.storage_record_path(layout.data)
    record = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(record, dict):
        raise MigrationError(f"{path} is not a JSON object")
    if record.get("runtimeDir") == str(layout.new_root):
        return
    record["runtimeDir"] = str(layout.new_root)
    log(f"  rewrite {path}: runtimeDir -> {layout.new_root}")
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
    for parts in legacy.MODEL_EXTRA_DIRECTORIES:
        extra = target.joinpath(*parts)
        if os.path.lexists(extra):
            context.log(f"  delete {extra}")
            remove_tree(extra)
    for lock in planning.lock_files(target):
        context.log(f"  delete {lock}")
        lock.unlink(missing_ok=True)


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


_RUNNERS: dict[str, Callable[[Step, MigrationContext], None]] = {
    planning.RUNTIME_ROOT: _runtime_root,
    planning.MODELS: _models,
    planning.OLD_ENVIRONMENT: _delete,
    planning.CACHES: _delete,
    planning.REBUILDABLE: _delete,
}

__all__ = [
    "JOURNAL_NAME",
    "MigrationBlocked",
    "MigrationError",
    "execute",
    "journal_path",
    "prepare",
    "read_journal",
]
