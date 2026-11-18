"""Moving an installation from the old data layout to the new one (WP-M1, WP-M2).

`docs/transcription-engines-plan.md` 4.4 items 24-25 and WP-M1;
`docs/runtime-and-config.md` 1.13 (the old layout) and 2.7 (the rules, with
D10 and D23 void). The command line is `python -m nonoka_x.migration`; the
sidecar's start-up runs the same plan through `startup.StartupMigration`
(WP-M2): the local steps before any data is opened, the downloads after.

    plan = prepare(data_dir)            # reads only
    print(render(plan))
    if plan.needed and not plan.blockers:
        execute(plan, log=print)        # journaled, resumable

What the migration does itself: rename the big-file root to `runtime` (same
volume, its own parent), move `models\\` to `layers\\models\\migrated\\`, move
the knowledge base and the rate-limit state into the packages' directories,
file the old tasks' outputs under `native\\` and convert them (`tasks`) -- all
of which a failure rolls back -- then delete the old Python environment,
tools, caches (the AOTI builds, xet cache and download locks in the moved
models among them) and the files that are rebuilt or no longer used. The
settings and the downloads are their owners' (`hooks`).
"""

from nonoka_x.migration.runner import (
    MigrationBlocked,
    MigrationError,
    MigrationRolledBack,
    execute,
    journal_path,
    prepare,
    read_journal,
)
from nonoka_x.migration.hooks import DEFERRED_OWNERS, Hooks, MigrationContext, StepHook
from nonoka_x.migration.legacy import LegacyLayout, default_data_dir, detect
from nonoka_x.migration.plan import MigrationPlan, build_plan, render

__all__ = [
    "DEFERRED_OWNERS",
    "Hooks",
    "LegacyLayout",
    "MigrationBlocked",
    "MigrationContext",
    "MigrationError",
    "MigrationPlan",
    "MigrationRolledBack",
    "StepHook",
    "build_plan",
    "default_data_dir",
    "detect",
    "execute",
    "journal_path",
    "prepare",
    "read_journal",
    "render",
]
