"""The migration plan: what moves where, what is deleted, what is still to download.

Built from the disk without changing it (`build_plan`), shown to the user
(`render`, or `to_dict` as JSON), and written into the journal before the
first step runs, so a resumed migration carries out the plan the user saw
rather than a fresh guess about a half-migrated disk.

Steps, in order (reversible work first, then the deletions, then the
downloads -- "delete first, download after", plan appendix B question 2):

| id | owner | does |
|---|---|---|
| `runtime-root` | WP-M1 | rename the old big-file root to its sibling `runtime`; rewrite `storage.json` when it records the root |
| `models` | WP-M1 | rename `models\\` to `layers\\models\\migrated\\` |
| `settings` | WP-R4 | read the old settings and keys into `settings.json` / `secrets.json`, back the old files up (`config.migrate`) |
| `knowledge` | WP-M2 | move the knowledge base into `engines\\finesub-knowledge\\`, content unchanged |
| `limit-state` | WP-M2 | move the rate-limit state into `engines\\finesub-correct\\`, content unchanged |
| `task-products` | WP-M2 | file each old task's `workspace\\` under `native\\<package>\\`, convert it to Nonoka's formats (`migration.tasks`) |
| `old-environment` | WP-M1 | delete the old `runtime\\` (the venv, `python-builds\\`, the tools, whatever else is in it) |
| `caches` | WP-M1 | delete the old `cache\\`, FineSub's compiled-kernel caches in staged engines and the checkout, and the moved models' `accel\\`, `xet\\`, `.locks\\` and `*.lock` |
| `rebuildable` | WP-M1 | delete the store marker, the registration script, the model catalog and `finesub-data\\`'s known files |
| `leftovers` | WP-M2 | delete the settings backups and what is left of `finesub-data\\` |
| `model-completion` | WP-E3 | check the models against the model layer, fetch only what is missing |
| `layer-download` | WP-E3 | download the Python environment and tool layers |

Everything up to `task-products` can be undone (`runner`; plan WP-M2: a failed
settings or data step rolls back to the layout before the migration); from
`old-environment` on nothing can, the old environment being gone.

`leftovers` (owner 2026-11-18/19, plan appendix B questions 89 and 90: delete
every file that is no longer of use, once past the point of rollback): the
settings backups serve only a rollback, so they go as soon as the deletions
have ruled one out, without waiting for the downloads -- even when they hold
keys this machine could not open (the report names those keys). With them goes
the rest of `finesub-data\\`, unrecognised files included, so nothing of
FineSub's is left in the main data directory outside `engines\\`. The step's
report names what it deleted.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import os
from pathlib import Path
from typing import Any

from nonoka_x.migration import hooks as deferred
from nonoka_x.migration import legacy
from nonoka_x.migration.hooks import Hooks, MigrationContext
from nonoka_x.migration.legacy import LegacyLayout
from nonoka_x.runtime.fsops import is_directory_link
from nonoka_x.runtime.paths import NonokaPaths

PLAN_SCHEMA = 1

RUNTIME_ROOT = "runtime-root"
MODELS = "models"
OLD_ENVIRONMENT = "old-environment"
CACHES = "caches"
REBUILDABLE = "rebuildable"
LEFTOVERS = "leftovers"

#: The steps that can be undone, in execution order: a failure in one of them rolls back all of them.
REVERSIBLE = (
    RUNTIME_ROOT,
    MODELS,
    deferred.SETTINGS,
    deferred.KNOWLEDGE,
    deferred.LIMIT_STATE,
    deferred.TASK_PRODUCTS,
)
#: The steps that download (run in the background at start-up, after the local ones).
DOWNLOADS = (deferred.MODEL_COMPLETION, deferred.LAYER_DOWNLOAD)

#: Every step, in execution order.
STEP_ORDER = (
    RUNTIME_ROOT,
    MODELS,
    deferred.SETTINGS,
    deferred.KNOWLEDGE,
    deferred.LIMIT_STATE,
    deferred.TASK_PRODUCTS,
    OLD_ENVIRONMENT,
    CACHES,
    REBUILDABLE,
    LEFTOVERS,
    deferred.MODEL_COMPLETION,
    deferred.LAYER_DOWNLOAD,
)

TITLES = {
    RUNTIME_ROOT: "Rename the big-file root to runtime",
    MODELS: "Move the models into the model layer's place",
    deferred.SETTINGS: "Read settings and keys into settings.json and secrets.json",
    deferred.KNOWLEDGE: "Move the knowledge base into its package directory",
    deferred.LIMIT_STATE: "Move the rate-limit state into the correction package directory",
    deferred.TASK_PRODUCTS: "File FineSub's task outputs as native attachments",
    OLD_ENVIRONMENT: "Delete the old Python environment and tools",
    CACHES: "Delete the old caches",
    REBUILDABLE: "Delete files that are rebuilt or no longer used",
    deferred.MODEL_COMPLETION: "Check the models against the model layer and fetch what is missing",
    deferred.LAYER_DOWNLOAD: "Download the Python environment and tool layers",
    LEFTOVERS: "Delete the settings backups and what is left of finesub-data",
}

# -- plan states --------------------------------------------------------------------------------------------------

#: No old layout: nothing to do.
NOTHING = "nothing"
#: An old layout, not migrated yet.
FRESH = "fresh"
#: A migration that was interrupted: the journal's plan continues.
RESUMING = "resuming"
#: Migrated before (or, at start-up, nothing to migrate: the mark that it ran once).
COMPLETED = "completed"
#: A rollback was interrupted: it is finished before anything else.
ROLLING_BACK = "rolling-back"

# -- action kinds ----------------------------------------------------------------------------------------------

RENAME = "rename"
DELETE = "delete"
#: Remove a directory only once nothing is left in it.
DELETE_IF_EMPTY = "delete-if-empty"
REWRITE = "rewrite"
#: Left where it is (unrecognised content of the old root; it moves along with it).
KEEP = "keep"
#: What a deferred step will work on.
PENDING = "pending"
#: One old task's `workspace\\` to file under `native\\` and convert (`migration.tasks`).
FILE_TASK = "file-task"


@dataclass
class Action:
    kind: str
    path: str
    target: str | None = None
    bytes: int = 0
    files: int = 0
    note: str = ""


@dataclass
class Step:
    id: str
    title: str
    actions: list[Action] = field(default_factory=list)
    #: The work package that implements a deferred step; None for WP-M1's own.
    owner: str | None = None
    #: A deferred step that will run now because its owner provided a hook.
    hooked: bool = False
    #: Bytes to download: 0 for local steps, None when not known yet.
    download_bytes: int | None = 0
    #: From the journal: pending, running, done or skipped.
    progress: str = "pending"

    @property
    def deferred(self) -> bool:
        return self.owner is not None

    @property
    def will_skip(self) -> bool:
        return self.deferred and not self.hooked


@dataclass
class MigrationPlan:
    data: str
    new_root: str
    old_root: str | None = None
    recorded: bool = False
    checkout: str | None = None
    state: str = FRESH
    steps: list[Step] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    schema: int = PLAN_SCHEMA

    @property
    def needed(self) -> bool:
        return any(step.actions for step in self.steps)

    def step(self, step_id: str) -> Step:
        for step in self.steps:
            if step.id == step_id:
                return step
        raise KeyError(step_id)

    def totals(self) -> dict[str, int | None]:
        # The root rename moves everything at once; what stays is the models.
        moved = sum(action.bytes for step in self.steps if step.id == MODELS for action in step.actions if action.kind == RENAME)
        deleted = sum(action.bytes for step in self.steps for action in step.actions if action.kind == DELETE)
        download: int | None = 0
        for step in self.steps:
            if not step.actions:
                continue
            if step.download_bytes is None:
                download = None
                break
            download += step.download_bytes
        return {"move_bytes": moved, "delete_bytes": deleted, "download_bytes": download}

    def layout(self) -> LegacyLayout:
        return LegacyLayout(
            data=Path(self.data),
            old_root=Path(self.old_root) if self.old_root is not None else None,
            new_root=Path(self.new_root),
            recorded=self.recorded,
            checkout=Path(self.checkout) if self.checkout is not None else None,
        )

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["totals"] = self.totals()
        return result

    @classmethod
    def from_dict(cls, record: dict[str, Any]) -> "MigrationPlan":
        if record.get("schema") != PLAN_SCHEMA:
            raise ValueError(f"unsupported migration plan schema: {record.get('schema')!r}")
        steps = [
            Step(
                **{key: value for key, value in item.items() if key != "actions"},
                actions=[Action(**action) for action in item.get("actions", ())],
            )
            for item in record.get("steps", ())
        ]
        fields = {key: record[key] for key in ("data", "new_root", "old_root", "recorded", "checkout", "state", "blockers") if key in record}
        return cls(**fields, steps=steps)


# -- measuring ---------------------------------------------------------------------------------------------------


def measure(path: Path) -> tuple[int, int]:
    """Files and bytes under `path`, never counting through a directory link."""

    if not os.path.lexists(path):
        return 0, 0
    if is_directory_link(path):
        return 0, 0
    if not path.is_dir():
        try:
            return 1, path.lstat().st_size
        except OSError:
            return 0, 0
    files = 0
    total = 0
    stack = [path]
    while stack:
        current = stack.pop()
        try:
            entries = list(os.scandir(current))
        except OSError:
            continue
        for entry in entries:
            try:
                if _is_junction(entry) or entry.is_symlink() and entry.is_dir():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    stack.append(Path(entry.path))
                    continue
                total += entry.stat(follow_symlinks=False).st_size
            except OSError:
                continue
            files += 1
    return files, total


def _is_junction(entry: os.DirEntry) -> bool:
    is_junction = getattr(entry, "is_junction", None)
    return bool(is_junction()) if is_junction is not None else False


def directory_links(root: Path) -> list[Path]:
    """Directory links (junctions, directory symlinks) under `root`, not descending through them."""

    found: list[Path] = []
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            entries = list(os.scandir(current))
        except OSError:
            continue
        for entry in entries:
            path = Path(entry.path)
            try:
                if _is_junction(entry) or (entry.is_symlink() and entry.is_dir()):
                    found.append(path)
                elif entry.is_dir(follow_symlinks=False):
                    stack.append(path)
            except OSError:
                continue
    return sorted(found)


def lock_files(root: Path) -> list[Path]:
    """`*.lock` files under `root`, not descending through directory links."""

    found: list[Path] = []
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            entries = list(os.scandir(current))
        except OSError:
            continue
        for entry in entries:
            try:
                if _is_junction(entry) or (entry.is_symlink() and entry.is_dir()):
                    continue
                if entry.is_dir(follow_symlinks=False):
                    stack.append(Path(entry.path))
                elif entry.name.endswith(legacy.MODEL_LOCK_SUFFIX):
                    found.append(Path(entry.path))
            except OSError:
                continue
    return sorted(found)


def _measured(kind: str, path: Path, *, target: Path | None = None, note: str = "") -> Action:
    files, size = measure(path)
    if is_directory_link(path):
        note = (note + "; " if note else "") + "a directory link: only the link itself"
    return Action(kind=kind, path=str(path), target=str(target) if target is not None else None, bytes=size, files=files, note=note)


# -- building ----------------------------------------------------------------------------------------------------


def build_plan(layout: LegacyLayout, hooks: Hooks | None = None, *, startup: bool = False) -> MigrationPlan:
    """What migrating `layout` would do. Reads the disk, changes nothing.

    `startup`: called by the sidecar before it opens anything. No task can be
    running then -- the process that runs them has not started -- so a task
    snapshot that says otherwise is stale (the sidecar marks it interrupted
    once it starts) and blocks nothing.
    """

    hooks = hooks or {}
    paths = NonokaPaths.for_roots(layout.data, layout.new_root)
    context = MigrationContext(layout=layout, paths=paths)
    blockers: list[str] = []
    steps: list[Step] = []
    for step_id in STEP_ORDER:
        actions = _BUILDERS[step_id](context, blockers)
        step = Step(id=step_id, title=TITLES[step_id], actions=actions)
        owner = deferred.DEFERRED_OWNERS.get(step_id)
        if owner is not None:
            step.owner = owner
            hook = hooks.get(step_id)
            step.hooked = hook is not None
            step.download_bytes = hook.download_bytes(context) if hook is not None else (
                None if step_id in (deferred.MODEL_COMPLETION, deferred.LAYER_DOWNLOAD) else 0
            )
        steps.append(step)
    if not startup:
        blockers.extend(task_blockers(layout.data))
    plan = MigrationPlan(
        data=str(layout.data),
        new_root=str(layout.new_root),
        old_root=str(layout.old_root) if layout.old_root is not None else None,
        recorded=layout.recorded,
        checkout=str(layout.checkout) if layout.checkout is not None else None,
        steps=steps,
        blockers=blockers,
    )
    if not plan.needed:
        plan.state = NOTHING
        plan.blockers = []
    return plan


def task_blockers(data: Path) -> list[str]:
    return [f"task {task_id} is {state}: finish or cancel it first" for task_id, state in legacy.running_tasks(data)]


def step_actions(step_id: str, context: MigrationContext, blockers: list[str]) -> list[Action]:
    """One step's actions, listed from the disk as it is now."""

    return _BUILDERS[step_id](context, blockers)


_OLD_ROOT_KNOWN = frozenset({
    legacy.OLD_INSTALL_DIRECTORY,
    legacy.OLD_MODELS_DIRECTORY,
    legacy.OLD_CACHE_DIRECTORY,
    *legacy.OLD_STORE_FILES,
})


def _runtime_root(context: MigrationContext, blockers: list[str]) -> list[Action]:
    layout = context.layout
    old, new = layout.old_root, layout.new_root
    if old is None:
        return []
    if not os.path.lexists(old):
        blockers.append(
            f"storage.json records the big-file root {old}, which does not exist "
            "(a drive not plugged in, or a storage move the desktop has not finished)"
        )
        return [Action(kind=RENAME, path=str(old), target=str(new), note="missing")]
    if is_directory_link(old):
        blockers.append(f"the big-file root {old} is a directory link; migration does not follow links")
    if os.path.lexists(new):
        blockers.append(f"{new} already exists; migration does not merge into it")
    actions = [_measured(RENAME, old, target=new)]
    try:
        entries = sorted(old.iterdir()) if not is_directory_link(old) else []
    except OSError:
        entries = []
    for entry in entries:
        if entry.name not in _OLD_ROOT_KNOWN:
            actions.append(_measured(KEEP, entry, target=new / entry.name, note="not recognised: moves with the root and is kept"))
    if layout.recorded:
        actions.append(Action(
            kind=REWRITE,
            path=str(legacy.storage_record_path(layout.data)),
            target=str(new),
            note="runtimeDir",
        ))
    return actions


def _models(context: MigrationContext, blockers: list[str]) -> list[Action]:
    old_models = context.layout.under_old_root(legacy.OLD_MODELS_DIRECTORY)
    if old_models is None or not os.path.lexists(old_models):
        return []
    target = context.migrated_models
    if is_directory_link(old_models):
        blockers.append(f"{old_models} is a directory link; migration does not follow links")
        return [_measured(RENAME, old_models, target=target)]
    links = directory_links(old_models)
    if links:
        listed = ", ".join(str(link) for link in links[:5]) + (" ..." if len(links) > 5 else "")
        blockers.append(f"{old_models} contains directory links ({listed}); migration does not carry links into a layer")
    clash = context.layout.old_root / context.migrated_models.relative_to(context.paths.runtime)
    if os.path.lexists(clash):
        blockers.append(f"{clash} already exists; migration does not merge into it")
    return [_measured(RENAME, old_models, target=target)]


def _model_extras(context: MigrationContext) -> list[Action]:
    """In the moved models: the AOTI builds, the xet cache and the download locks.

    Deleted with the caches, after the reversible steps, so that a rollback
    puts the models back exactly as they were.
    """

    old_models = context.layout.under_old_root(legacy.OLD_MODELS_DIRECTORY)
    if old_models is None or not os.path.lexists(old_models) or is_directory_link(old_models):
        return []
    actions = []
    for parts in legacy.MODEL_EXTRA_DIRECTORIES:
        extra = old_models.joinpath(*parts)
        if os.path.lexists(extra):
            actions.append(_measured(DELETE, extra, note="in the moved models: not part of the model layer"))
    for lock in lock_files(old_models):
        actions.append(_measured(DELETE, lock, note="a download lock in the moved models"))
    return actions


def _main_entries(
    context: MigrationContext,
    names: tuple[str, ...],
    *,
    target: Path | None = None,
    note: str = "",
) -> list[Action]:
    return [
        _measured(PENDING, path, target=target, note=note)
        for name in names
        if os.path.lexists(path := context.layout.data / name)
    ]


def _settings(context: MigrationContext, blockers: list[str]) -> list[Action]:
    from nonoka_x.config.migrate import blocker

    reason = blocker(context.layout.data)
    if reason is not None:
        blockers.append(reason)
    return _main_entries(
        context,
        legacy.SETTINGS_FILES,
        note="read into settings.json and secrets.json; the old file is kept as a backup",
    )


def _moves(
    context: MigrationContext,
    blockers: list[str],
    moves: list[tuple[Path, Path, bool]],
) -> list[Action]:
    """`(source, target, is a lock)` -> rename actions; an existing target blocks, except for a lock (dropped)."""

    actions = []
    for source, target, lock in moves:
        if not os.path.lexists(source):
            continue
        if is_directory_link(source):
            blockers.append(f"{source} is a directory link; migration does not follow links")
        if os.path.lexists(target):
            if lock:
                actions.append(_measured(DELETE, source, note=f"a lock; {target} is already there"))
                continue
            blockers.append(f"{target} already exists; migration does not merge {source} into it")
        actions.append(_measured(RENAME, source, target=target, note="content unchanged"))
    return actions


def _knowledge(context: MigrationContext, blockers: list[str]) -> list[Action]:
    data, root = context.layout.data, legacy.knowledge_root(context.paths)
    old = data / legacy.KNOWLEDGE_DIRECTORY
    moves = [(old, root, False)]
    moves += [
        (old.with_name(old.name + suffix), root.with_name(root.name + suffix), True)
        for suffix in legacy.KNOWLEDGE_SIBLINGS
    ]
    actions = _moves(context, blockers, moves)
    registrations = old.with_name(old.name + legacy.KNOWLEDGE_REGISTRATIONS)
    if os.path.lexists(registrations):
        actions.append(_measured(DELETE, registrations, note="endpoint registrations of knowledge processes that have ended"))
    return actions


def _limit_state(context: MigrationContext, blockers: list[str]) -> list[Action]:
    data, state = context.layout.data, legacy.limit_state_file(context.paths)
    old = data / legacy.LIMIT_STATE_FILE
    moves = []
    # A `state` directory is Nonoka's own state (`ConfigStore.state_file`), not FineSub's file.
    if os.path.lexists(old) and not old.is_dir():
        moves.append((old, state, False))
    moves.append((data / legacy.LIMIT_STATE_LOCK, state.with_name(state.name + ".lock"), True))
    return _moves(context, blockers, moves)


def _task_products(context: MigrationContext, blockers: list[str]) -> list[Action]:
    from nonoka_x.migration import tasks

    return tasks.plan_actions(context)


_OLD_INSTALL_NOTES = {
    "python": "the old Python environment (venv)",
    "python-builds": "the old interpreter builds",
    **{tool: "an old tool" for tool in legacy.OLD_TOOLS},
}


def _old_environment(context: MigrationContext, blockers: list[str]) -> list[Action]:
    install = context.layout.under_old_root(legacy.OLD_INSTALL_DIRECTORY)
    if install is None or not os.path.lexists(install):
        return []
    if is_directory_link(install):
        return [_measured(DELETE, install)]
    try:
        entries = sorted(install.iterdir())
    except OSError:
        entries = []
    actions = [
        _measured(DELETE, entry, note=_OLD_INSTALL_NOTES.get(entry.name, "not recognised: deleted with the rest"))
        for entry in entries
    ]
    actions.append(Action(kind=DELETE, path=str(install), note="the directory itself"))
    return actions


def _caches(context: MigrationContext, blockers: list[str]) -> list[Action]:
    layout = context.layout
    actions = _model_extras(context)
    old_cache = layout.under_old_root(legacy.OLD_CACHE_DIRECTORY)
    if old_cache is not None and os.path.lexists(old_cache):
        actions.append(_measured(DELETE, old_cache, note="the old installer's caches (uv and others)"))
    for cache in legacy.staged_vendor_caches(layout.data):
        actions.append(_measured(DELETE, cache, note="FineSub's compiled-kernel cache in a staged engine"))
    if layout.checkout is not None:
        cache = layout.checkout.joinpath(*legacy.CHECKOUT_VENDOR_CACHE)
        if os.path.lexists(cache):
            actions.append(_measured(DELETE, cache, note="FineSub's compiled-kernel cache in the checkout"))
    return actions


def _rebuildable(context: MigrationContext, blockers: list[str]) -> list[Action]:
    layout = context.layout
    actions = []
    for name in legacy.OLD_STORE_FILES:
        path = layout.under_old_root(name)
        if path is not None and os.path.lexists(path):
            actions.append(_measured(DELETE, path, note="no longer used (D18)"))
    for name in legacy.REBUILDABLE_FILES:
        path = layout.data / name
        if os.path.lexists(path):
            actions.append(_measured(DELETE, path, note="derived per process from now on"))
    finesub_data = layout.data / legacy.FINESUB_DATA_DIRECTORY
    if os.path.lexists(finesub_data):
        for name in legacy.FINESUB_DATA_FILES:
            path = finesub_data / name
            if os.path.lexists(path):
                actions.append(_measured(DELETE, path, note="no longer used (D18, no download routes)"))
        try:
            rest = sorted(entry.name for entry in finesub_data.iterdir() if entry.name not in legacy.FINESUB_DATA_FILES)
        except OSError:
            rest = []
        if rest:
            actions.append(Action(kind=KEEP, path=str(finesub_data), note="also holds " + ", ".join(rest) + ": deleted with it by the leftovers step"))
        else:
            actions.append(Action(kind=DELETE_IF_EMPTY, path=str(finesub_data), note="the directory itself, once empty"))
    return actions


def _leftovers(context: MigrationContext, blockers: list[str]) -> list[Action]:
    data = context.layout.data
    actions = [
        Action(kind=DELETE, path=str(backup), note="the settings backup, once past the point of rollback")
        for backup in legacy.settings_backups(data)
    ]
    for name in legacy.SETTINGS_FILES:
        if (data / name).is_file():
            actions.append(Action(
                kind=DELETE,
                path=str(data / (name + legacy.SETTINGS_BACKUP_SUFFIX)),
                note=f"the backup the settings step makes of {name}, once past the point of rollback",
            ))
    finesub_data = data / legacy.FINESUB_DATA_DIRECTORY
    if os.path.lexists(finesub_data):
        actions.append(_measured(DELETE, finesub_data, note="whatever is left in it, unrecognised files included"))
    return actions


def _model_completion(context: MigrationContext, blockers: list[str]) -> list[Action]:
    old_models = context.layout.under_old_root(legacy.OLD_MODELS_DIRECTORY)
    if old_models is None or not os.path.lexists(old_models):
        return []
    return [Action(
        kind=PENDING,
        path=str(context.migrated_models),
        target=str(context.paths.layer(deferred.MODELS_LAYER) / "<version>"),
        note="checked file by file against the model layer manifest; only missing or wrong files fetched from R2; then current.json",
    )]


def _layer_download(context: MigrationContext, blockers: list[str]) -> list[Action]:
    install = context.layout.under_old_root(legacy.OLD_INSTALL_DIRECTORY)
    if install is None or not os.path.lexists(install):
        return []
    return [Action(
        kind=PENDING,
        path=str(context.paths.layers),
        note="the Python environment layer and the tool layer this version needs (layers.json)",
    )]


_BUILDERS = {
    RUNTIME_ROOT: _runtime_root,
    MODELS: _models,
    deferred.SETTINGS: _settings,
    deferred.KNOWLEDGE: _knowledge,
    deferred.LIMIT_STATE: _limit_state,
    deferred.TASK_PRODUCTS: _task_products,
    OLD_ENVIRONMENT: _old_environment,
    CACHES: _caches,
    REBUILDABLE: _rebuildable,
    deferred.MODEL_COMPLETION: _model_completion,
    deferred.LAYER_DOWNLOAD: _layer_download,
    LEFTOVERS: _leftovers,
}


# -- showing -----------------------------------------------------------------------------------------------------


def human_size(size: int | None) -> str:
    if size is None:
        return "unknown"
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024 or unit == "GiB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.2f} {unit}"
        value /= 1024
    return f"{size} B"


def render(plan: MigrationPlan) -> str:
    """The plan as text: paths and sizes, never file contents."""

    lines = ["Nonoka X data layout migration", f"  main data directory: {plan.data}"]
    if plan.state == NOTHING:
        lines.append("  no old layout found: nothing to do")
        return "\n".join(lines) + "\n"
    lines.append(f"  old big-file root:   {plan.old_root or '(none)'}" + ("  [recorded in storage.json]" if plan.recorded and plan.old_root else ""))
    lines.append(f"  new big-file root:   {plan.new_root}")
    if plan.checkout:
        lines.append(f"  development checkout: {plan.checkout}")
    state = {
        FRESH: "not migrated yet",
        RESUMING: "interrupted: continuing the journal's plan",
        ROLLING_BACK: "a failed migration is being rolled back",
        COMPLETED: "migrated",
    }[plan.state]
    lines.append(f"  state: {state}")
    totals = plan.totals()
    lines.append(
        f"  to move: {human_size(totals['move_bytes'])}   to delete: {human_size(totals['delete_bytes'])}   "
        f"to download: {human_size(totals['download_bytes'])}"
    )
    lines.append("")
    for number, step in enumerate(plan.steps, start=1):
        if not step.actions:
            continue
        status = ""
        if step.will_skip:
            status = f"  [skipped: implemented by {step.owner}]"
        elif plan.state != FRESH:
            status = f"  [{step.progress}]"
        lines.append(f"[{number}] {step.id}: {step.title}{status}")
        if step.deferred and step.download_bytes != 0:
            lines.append(f"      to download: {human_size(step.download_bytes)}")
        for action in step.actions:
            size = f"  ({human_size(action.bytes)}, {action.files} files)" if action.files or action.bytes else ""
            target = f"  ->  {action.target}" if action.target else ""
            note = f"  -- {action.note}" if action.note else ""
            lines.append(f"      {action.kind:<8} {action.path}{target}{size}{note}")
        lines.append("")
    if plan.blockers:
        lines.append("Blocked, nothing will be changed:")
        lines.extend(f"  - {blocker}" for blocker in plan.blockers)
        lines.append("")
    return "\n".join(lines)


__all__ = [
    "COMPLETED",
    "DOWNLOADS",
    "LEFTOVERS",
    "REVERSIBLE",
    "ROLLING_BACK",
    "FRESH",
    "NOTHING",
    "RESUMING",
    "STEP_ORDER",
    "Action",
    "MigrationPlan",
    "Step",
    "build_plan",
    "human_size",
    "measure",
    "render",
]
