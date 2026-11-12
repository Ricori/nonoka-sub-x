"""`python -m nonoka_x.migration`: show the migration plan, and carry it out when asked.

    python -m nonoka_x.migration --dry-run                 # the installed app's data directory
    python -m nonoka_x.migration --data-dir "D:\\Other\\Nonoka X" --dry-run
    python -m nonoka_x.migration --checkout D:\\Development\\nonoka-sub-x --dry-run
    python -m nonoka_x.migration --yes                     # migrate without asking
    python -m nonoka_x.migration --json --dry-run          # the plan as JSON

Without `--dry-run` the plan is printed and then carried out only after a
"y" on a terminal, or with `--yes`; without a terminal and without `--yes`
nothing is changed.

Exit status: 0 done, nothing to do, or a dry run; 1 a step failed (run again
to continue from the journal); 2 blocked; 3 not confirmed.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from nonoka_x.migration import legacy
from nonoka_x.migration import plan as planning
from nonoka_x.migration.runner import MigrationError, execute, prepare

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_BLOCKED = 2
EXIT_NOT_CONFIRMED = 3


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m nonoka_x.migration",
        description="Move a Nonoka X installation from the old data layout to the new one.",
    )
    parser.add_argument("--data-dir", type=Path, help="the main data directory (default: the installed app's)")
    parser.add_argument(
        "--checkout",
        type=Path,
        help="a development checkout: its .local is the data directory (unless --data-dir is given) "
        "and its vendor's compiled-kernel cache is cleared too",
    )
    parser.add_argument("--dry-run", action="store_true", help="show the plan and change nothing")
    parser.add_argument("--yes", action="store_true", help="carry out the plan without asking")
    parser.add_argument("--json", action="store_true", help="print the plan as JSON")
    return parser


def _print(text: str) -> None:
    sys.stdout.write(text if text.endswith("\n") else text + "\n")
    sys.stdout.flush()


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    data_dir = args.data_dir
    if data_dir is None and args.checkout is not None:
        data_dir = args.checkout / legacy.CHECKOUT_DATA_DIRECTORY
    if data_dir is None:
        data_dir = legacy.default_data_dir()
    if data_dir is None:
        _print("No default data directory on this platform: pass --data-dir.")
        return EXIT_FAILED

    def show(plan: planning.MigrationPlan) -> None:
        _print(json.dumps(plan.to_dict(), ensure_ascii=False, indent=2) if args.json else planning.render(plan))

    try:
        plan = prepare(data_dir, checkout=args.checkout)
    except MigrationError as error:
        _print(f"Cannot plan the migration: {error}")
        return EXIT_FAILED
    if plan.state in (planning.NOTHING, planning.COMPLETED) or args.dry_run:
        show(plan)
        return EXIT_BLOCKED if plan.blockers and plan.state not in (planning.NOTHING, planning.COMPLETED) else EXIT_OK
    if plan.blockers:
        show(plan)
        return EXIT_BLOCKED
    if not args.json:
        show(plan)
    if not args.yes:
        if not sys.stdin.isatty():
            _print("Not carried out: pass --yes to migrate.")
            return EXIT_NOT_CONFIRMED
        answer = input("Carry out this plan? Deleted environments cannot be restored. [y/N] ").strip().lower()
        if answer not in ("y", "yes"):
            _print("Not carried out.")
            return EXIT_NOT_CONFIRMED
    log = (lambda message: None) if args.json else _print
    try:
        plan = execute(plan, log=log)
    except MigrationError as error:
        _print(f"Migration stopped: {error}\nRun the same command again to continue from where it stopped.")
        return EXIT_FAILED
    except OSError as error:
        _print(f"Migration stopped: {error}\nRun the same command again to continue from where it stopped.")
        return EXIT_FAILED
    if args.json:
        show(plan)
    else:
        skipped = [step for step in plan.steps if step.actions and step.progress == "skipped"]
        _print("Migration finished.")
        for step in skipped:
            _print(f"  skipped {step.id}: implemented by {step.owner}")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
