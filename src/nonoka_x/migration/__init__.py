"""Moving an installation from the old data layout to the new one (WP-M1).

`docs/transcription-engines-plan.md` 4.4 items 24-25 and WP-M1;
`docs/runtime-and-config.md` 1.13 (the old layout) and 2.7 (the rules, with
D10 and D23 void). The command line is `python -m nonoka_x.migration`;
WP-M2 calls `prepare` and `execute` from the sidecar's start-up.

    plan = prepare(data_dir)            # reads only
    print(render(plan))
    if plan.needed and not plan.blockers:
        execute(plan, log=print)        # journaled, resumable

What WP-M1 does itself: rename the big-file root to `runtime` (same volume,
its own parent), move `models\\` to `layers\\models\\migrated\\` and remove
the AOTI builds, the xet cache and the download locks inside it, then delete
the old Python environment, tools, caches and the files that are rebuilt or
no longer used. What it leaves to others is in `hooks`.
"""

from nonoka_x.migration.runner import (
    MigrationBlocked,
    MigrationError,
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
