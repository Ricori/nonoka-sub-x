"""The migration as the sidecar runs it at start-up (WP-M2).

`docs/transcription-engines-plan.md` WP-M2: before the sidecar opens any data
it detects and carries out the migration; the old environment is deleted
first and the new layers downloaded after ("delete first, download after",
appendix B question 2), with the progress on screen and a retry; closing the
app and starting it again carries on from the journal.

So start-up has two halves:

* **The local steps** (everything before `model-completion`: renames, the
  settings, the knowledge base, the old tasks, the deletions). They run before
  any data is opened. While they run -- or when they cannot (`blocked`), or
  were rolled back (`rolled-back`), or failed past the point of rollback
  (`failed`) -- the sidecar answers every request but the migration's own with
  `migrating` (`sidecar.py`), so nothing reads or writes the half-migrated
  layout, and nothing writes a new store an unfinished migration would then
  refuse to merge into. `retry` runs them again.
* **The downloads** (`model-completion`, `layer-download`) run in the
  background once the sidecar is serving: FineSub's transcription is
  unavailable until they finish, the cloud engines as soon as the tool layer
  is in (the layer manager installs it first). A failure can be retried; a
  restart continues them.

`status()` is what `GET /v1/migration` returns: the state, the steps and their
progress, the current download's progress, the failure, and the report the
user is owed -- the settings and keys that were dropped (names only) and the
old tasks that could not be converted.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import asdict, is_dataclass
import os
from pathlib import Path
import sys
import threading
from typing import Any

from nonoka_x.migration import plan as planning
from nonoka_x.migration import runner
from nonoka_x.migration.hooks import Hooks, available_hooks

#: The data may be opened now (nothing to migrate, migrated, or only downloads left).
READY = "ready"
#: The local steps are still to run: the data stays closed until they have.
GATED = "gated"

CHECKING = "checking"
NOT_NEEDED = "not-needed"
MIGRATING = "migrating"
BLOCKED = "blocked"
ROLLED_BACK = "rolled-back"
FAILED = "failed"
DOWNLOADING = "downloading"
DOWNLOAD_FAILED = "download-failed"
COMPLETED = "completed"

#: States in which the data is closed.
CLOSED_STATES = frozenset({CHECKING, MIGRATING, BLOCKED, ROLLED_BACK, FAILED})

MESSAGES = {
    CHECKING: "正在检查旧版数据",
    NOT_NEEDED: "",
    MIGRATING: "正在把旧版数据迁移到新的目录结构，请不要关闭应用",
    BLOCKED: "旧版数据暂时无法迁移，处理下面的问题后点「重试」",
    ROLLED_BACK: "迁移失败，已恢复到迁移前的样子；处理下面的问题后点「重试」",
    FAILED: "迁移中断：旧的运行环境已删除，无法回滚，点「重试」接着完成",
    DOWNLOADING: "正在下载新的运行环境；下载完成之前 FineSub 转写不可用，云 API 引擎在工具层装好后即可使用",
    DOWNLOAD_FAILED: "新的运行环境下载失败，点「重试」继续下载（旧环境已删除，不会回退）",
    COMPLETED: "旧版数据已迁移完成",
}

FIRST_DOWNLOAD = planning.DOWNLOADS[0]


def _print(message: str) -> None:
    print(f"migration: {message}", file=sys.stderr, flush=True)


def _same(left: Path | str, right: Path | str) -> bool:
    return os.path.normcase(os.path.abspath(left)) == os.path.normcase(os.path.abspath(right))


def _progress_dict(value: Any) -> dict[str, Any]:
    if is_dataclass(value):
        raw = asdict(value)
    elif isinstance(value, Mapping):
        raw = dict(value)
    else:
        return {"message": str(value)}
    return {
        "layer": raw.get("layer"),
        "stage": raw.get("stage"),
        "message": raw.get("message"),
        "completed": raw.get("completed", 0),
        "total": raw.get("total", 0),
        "bytesPerSecond": raw.get("bytes_per_second", 0.0),
    }


class StartupMigration:
    """One sidecar's migration: decide, run the local steps, then the downloads; say how it went."""

    def __init__(
        self,
        data_dir: Path | str,
        *,
        install_dir: Path | str | None = None,
        engines: Path | str | None = None,
        hooks: Hooks | None = None,
        log: Callable[[str], None] | None = None,
    ) -> None:
        self.data_dir = Path(data_dir)
        self._given_install_dir = Path(install_dir) if install_dir is not None else None
        self._engines = Path(engines) if engines is not None else None
        self._hooks = hooks
        self._log = log or _print
        self._lock = threading.RLock()
        self._plan: planning.MigrationPlan | None = None
        self._downloads: threading.Thread | None = None
        self._local: threading.Thread | None = None
        self._status: dict[str, Any] = {"schema": 1, "state": CHECKING, "message": MESSAGES[CHECKING]}

    # -- what the sidecar asks ---------------------------------------------------------------------------------

    def hooks(self) -> Hooks:
        if self._hooks is None:
            self._hooks = available_hooks()
        return self._hooks

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {**self._status, "dataOpen": self._status["state"] not in CLOSED_STATES}

    @property
    def data_open(self) -> bool:
        return self.status()["dataOpen"]

    def install_dir(self) -> Path | None:
        """The big-file root to hand the provisioner: a desktop of this version still names the old one on its first start."""

        given = self._given_install_dir
        plan = self._plan
        if given is None or plan is None or plan.old_root is None:
            return given
        if _same(given, plan.old_root) and not os.path.lexists(plan.old_root) and os.path.isdir(plan.new_root):
            return Path(plan.new_root)
        return given

    # -- deciding --------------------------------------------------------------------------------------------

    def begin(self) -> str:
        """READY when the data can be opened right away, GATED when the local steps have to run first. Cheap."""

        try:
            journal = runner.read_journal(self.data_dir)
            if journal is None or journal.get("status") == runner.ROLLED_BACK:
                from nonoka_x.migration import legacy

                if journal is None and legacy.detect(self.data_dir).old_root is None:
                    # No old big-file root: listing the rest is a handful of stats.
                    plan = runner.prepare(self.data_dir, hooks=self.hooks(), startup=True)
                    if plan.state == planning.NOTHING:
                        runner.mark_nothing_to_do(self.data_dir, plan)
                        self._set(NOT_NEEDED, plan)
                        return READY
                return GATED
            plan = runner.prepare(self.data_dir, hooks=self.hooks(), startup=True)
        except runner.MigrationError as error:
            self._fail(FAILED, str(error))
            return GATED
        if plan.state == planning.COMPLETED:
            self._set(NOT_NEEDED, plan)
            return READY
        if plan.state == planning.RESUMING and self._local_done(plan):
            self._set(DOWNLOADING, plan)
            return READY
        return GATED

    @staticmethod
    def _local_done(plan: planning.MigrationPlan) -> bool:
        local = planning.STEP_ORDER[: planning.STEP_ORDER.index(FIRST_DOWNLOAD)]
        return all(plan.step(step_id).progress in (runner.DONE, runner.SKIPPED) for step_id in local)

    # -- the local steps -----------------------------------------------------------------------------------------

    def run_local(self) -> bool:
        """Carry out the steps before the downloads. Returns whether the data can be opened now."""

        self._set(MIGRATING, None)
        try:
            plan = runner.prepare(self.data_dir, hooks=self.hooks(), startup=True)
            self._plan = plan
            if plan.state == planning.NOTHING:
                runner.mark_nothing_to_do(self.data_dir, plan)
                self._set(NOT_NEEDED, plan)
                return True
            if plan.blockers:
                self._set(BLOCKED, plan, blockers=list(plan.blockers))
                self._log("blocked: " + "; ".join(plan.blockers))
                return False
            self._log(f"migrating {plan.data} ({plan.state})")
            self._set(MIGRATING, plan)
            # Nothing to download: run to the end here rather than show a "download" phase.
            downloads = any(plan.step(step_id).actions for step_id in planning.DOWNLOADS)
            plan = runner.execute(
                plan, hooks=self.hooks(), log=self._step_log, engines=self._engines,
                stop_before=FIRST_DOWNLOAD if downloads else None,
            )
        except runner.MigrationRolledBack as error:
            self._fail(ROLLED_BACK, str(error), step=error.step)
            return False
        except runner.MigrationBlocked as error:
            self._set(BLOCKED, self._plan, blockers=str(error).split("; "))
            return False
        except (runner.MigrationError, OSError) as error:
            self._fail(FAILED, str(error), step=getattr(error, "step", None))
            return False
        self._plan = plan
        self._set(COMPLETED if plan.state == planning.COMPLETED else DOWNLOADING, plan)
        return True

    def start_local(self, on_open: Callable[[], None]) -> None:
        """`run_local` in a thread of its own; `on_open` runs once the data can be opened."""

        def work() -> None:
            if self.run_local():
                on_open()
                self.start_downloads()

        with self._lock:
            if self._local is not None and self._local.is_alive():
                return
            self._local = threading.Thread(target=work, name="nonoka-migration", daemon=True)
            self._local.start()

    # -- the downloads ---------------------------------------------------------------------------------------

    def downloads_pending(self) -> bool:
        return self.status()["state"] in (DOWNLOADING, DOWNLOAD_FAILED)

    def run_downloads(self) -> None:
        try:
            plan = runner.prepare(self.data_dir, hooks=self.hooks(), startup=True)
            self._plan = plan
            if plan.state == planning.COMPLETED:
                self._set(COMPLETED, plan)
                return
            self._set(DOWNLOADING, plan)
            plan = runner.execute(plan, hooks=self.hooks(), log=self._step_log, progress=self._progress, engines=self._engines)
        except (runner.MigrationError, OSError) as error:
            self._fail(DOWNLOAD_FAILED, str(error), step=getattr(error, "step", None))
            return
        except Exception as error:  # noqa: BLE001 - a download thread must say why it stopped
            self._fail(DOWNLOAD_FAILED, f"{error.__class__.__name__}: {error}")
            return
        self._plan = plan
        self._set(COMPLETED, plan)
        self._log("completed")

    def start_downloads(self) -> None:
        with self._lock:
            if not self.downloads_pending():
                return
            if self._downloads is not None and self._downloads.is_alive():
                return
            self._downloads = threading.Thread(target=self.run_downloads, name="nonoka-migration-download", daemon=True)
            self._downloads.start()

    # -- retrying --------------------------------------------------------------------------------------------

    def retry(self, on_open: Callable[[], None]) -> dict[str, Any]:
        state = self.status()["state"]
        if state in (BLOCKED, ROLLED_BACK, FAILED):
            self.start_local(on_open)
        elif state == DOWNLOAD_FAILED:
            with self._lock:
                self._status["state"] = DOWNLOADING
            self.start_downloads()
        return self.status()

    # -- the status ------------------------------------------------------------------------------------------

    def _step_log(self, message: str) -> None:
        self._log(message)
        if not message.startswith(" ") and ":" in message:
            step = message.split(":", 1)[0]
            if step in planning.STEP_ORDER:
                with self._lock:
                    self._status["step"] = step
                    self._status["title"] = planning.TITLES.get(step, step)
                    self._refresh_steps()

    def _progress(self, value: Any) -> None:
        with self._lock:
            self._status["progress"] = _progress_dict(value)

    def _refresh_steps(self) -> None:
        try:
            journal = runner.read_journal(self.data_dir) or {}
        except runner.MigrationError:
            return
        recorded = journal.get("steps") or {}
        plan = self._plan
        if plan is None:
            return
        self._status["steps"] = [
            {
                "id": step.id,
                "title": step.title,
                "status": (recorded.get(step.id) or {}).get("status", runner.PENDING),
                "download": step.id in planning.DOWNLOADS,
            }
            for step in plan.steps
            if step.actions
        ]

    def _set(self, state: str, plan: planning.MigrationPlan | None, *, blockers: list[str] | None = None) -> None:
        with self._lock:
            if plan is not None:
                self._plan = plan
            self._status = {
                "schema": 1,
                "state": state,
                "message": MESSAGES[state],
                **({"blockers": blockers} if blockers else {}),
                **({"progress": self._status["progress"]} if state == DOWNLOADING and "progress" in self._status else {}),
            }
            self._refresh_steps()
            report = self._report()
            if report:
                self._status["report"] = report

    def _fail(self, state: str, error: str, *, step: str | None = None) -> None:
        self._log(f"{state}: {error}")
        self._set(state, None)
        with self._lock:
            self._status["error"] = error
            if step:
                self._status["step"] = step
                self._status["title"] = planning.TITLES.get(step, step)

    def _report(self) -> dict[str, Any]:
        """What the user is owed: names of what was dropped or could not be carried, never a value."""

        report: dict[str, Any] = {}
        try:
            from nonoka_x.config.migrate import read_report

            settings = read_report(self.data_dir)
        except Exception:  # noqa: BLE001 - the report is a courtesy
            settings = None
        if settings:
            env = settings.get("env") or {}
            toml = settings.get("toml") or {}
            report["settings"] = {
                "carried": list(env.get("carried") or []),
                "routes": list(toml.get("routes") or []),
                "renamed": dict(env.get("renamed") or {}),
                "droppedVariables": list(env.get("dropped") or []),
                "droppedTables": list(toml.get("droppedTables") or []),
                "droppedKeys": list(toml.get("droppedKeys") or []),
                "unreadable": list(env.get("unreadable") or []),
                "notes": list(settings.get("notes") or []),
            }
        try:
            journal = runner.read_journal(self.data_dir) or {}
        except runner.MigrationError:
            journal = {}
        steps = journal.get("steps") or {}
        tasks = (steps.get(planning.deferred.TASK_PRODUCTS) or {}).get("report") or {}
        if tasks:
            report["tasks"] = tasks
        # What the `leftovers` step deleted (plan appendix B questions 89-90): names relative to the main data directory.
        cleanup = (steps.get(planning.LEFTOVERS) or {}).get("report") or {}
        if cleanup:
            report["cleanup"] = cleanup
        return report


__all__ = [
    "CLOSED_STATES",
    "COMPLETED",
    "DOWNLOADING",
    "DOWNLOAD_FAILED",
    "GATED",
    "READY",
    "StartupMigration",
]
