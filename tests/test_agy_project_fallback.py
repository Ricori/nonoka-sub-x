"""A kept agy project id that agy no longer has, the engine half.

Promises kept by `patches/finesub/0007-agy-project-fallback-reregister.patch`:
agy 1.3 answers an unknown `--project` id by opening `default-cli-project`
with exit status 0, so the project check now reads which project agy actually
opened. A stale id is registered afresh; a project agy cannot open even right
after registering it fails closed instead of registering one per call. agy 1.4
logs no opened project at all, so a kept id whose record is gone counts as
stale too.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
import uuid
from pathlib import Path

from finesub.llm.agent.local_agent import (
    AGY_PROJECT_PROTOCOL_VERSION,
    AgyLocalAgentDriver,
    LocalAgentUnavailableError,
)


class _FakeAgy:
    """`/hooks` as agy 1.3.3 logs it: an unknown id opens the default project.

    `version="1.4.5"` drops the opened-project line, as 1.4 does; either way
    a known project keeps its record under `records_dir`."""

    def __init__(
        self, records_dir: Path, *, forget_new: bool = False, version: str = "1.3.3"
    ) -> None:
        self.records_dir = records_dir
        self.registrations = 0
        self.forget_new = forget_new
        self.version = version

    @property
    def known(self) -> set[str]:
        return {path.stem for path in self.records_dir.glob("*.json")}

    def keep(self, project_id: str) -> None:
        (self.records_dir / f"{project_id}.json").write_text(
            json.dumps({"id": project_id}), encoding="utf-8"
        )

    def __call__(
        self,
        project_root: Path,
        *,
        project_id: str = "",
        create: bool = False,
        log_path: Path | None = None,
        agent_name: str = "",
    ) -> subprocess.CompletedProcess[str]:
        lines = [f"Language server version: {self.version}"]
        if create:
            self.registrations += 1
            project_id = str(uuid.uuid4())
            if not self.forget_new:
                self.keep(project_id)
            lines.append(f'project: created project "x" (id={project_id}) at C:\\x.json')
            opened = project_id
        else:
            opened = project_id if project_id in self.known else "default-cli-project"
        if self.version.startswith("1.3"):
            lines.append(f"Conversation using project ID: {opened}")
        assert log_path is not None
        log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return subprocess.CompletedProcess([], 0, "", "")


def _driver(agy: _FakeAgy) -> AgyLocalAgentDriver:
    driver = object.__new__(AgyLocalAgentDriver)
    driver._hooks_command = agy  # type: ignore[method-assign]
    driver._verify_hook_output = lambda stdout, expected: None  # type: ignore[method-assign]
    driver.agy_project_records_dir = lambda: agy.records_dir  # type: ignore[method-assign]
    return driver


class AgyProjectFallbackTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.project_root = Path(self._temp.name) / ".finesub-tool-0"
        self.records_dir = Path(self._temp.name) / "projects"
        self.records_dir.mkdir()

    def tearDown(self) -> None:
        self._temp.cleanup()

    def _keep_state(self, driver: AgyLocalAgentDriver, project_id: str) -> Path:
        self.project_root.mkdir(parents=True, exist_ok=True)
        paths = driver._write_project_resources(self.project_root, tool=True)
        paths["state"].write_text(
            json.dumps(
                {
                    "protocol_version": AGY_PROJECT_PROTOCOL_VERSION,
                    "project_id": project_id,
                    "project_root": str(self.project_root.resolve()),
                    "hook_digest": driver._hook_digest(paths),
                }
            ),
            encoding="utf-8",
        )
        return paths["state"]

    def test_kept_id_agy_still_has_is_reused(self) -> None:
        agy = _FakeAgy(self.records_dir)
        kept = str(uuid.uuid4())
        agy.keep(kept)
        driver = _driver(agy)
        self._keep_state(driver, kept)

        project_id, _digest = driver._ensure_project(self.project_root, tool=True)

        self.assertEqual(project_id, kept)
        self.assertEqual(agy.registrations, 0)

    def test_kept_id_agy_lost_is_registered_again(self) -> None:
        agy = _FakeAgy(self.records_dir)
        driver = _driver(agy)
        state = self._keep_state(driver, str(uuid.uuid4()))

        project_id, _digest = driver._ensure_project(self.project_root, tool=True)

        self.assertEqual(agy.registrations, 1)
        self.assertIn(project_id, agy.known)
        saved = json.loads(state.read_text(encoding="utf-8"))
        self.assertEqual(saved["project_id"], project_id)

    def test_fresh_project_agy_cannot_open_fails_closed(self) -> None:
        agy = _FakeAgy(self.records_dir, forget_new=True)
        driver = _driver(agy)
        self._keep_state(driver, str(uuid.uuid4()))

        with self.assertRaisesRegex(LocalAgentUnavailableError, "default-cli-project"):
            driver._ensure_project(self.project_root, tool=True)
        self.assertEqual(agy.registrations, 1)

    def test_kept_id_with_record_is_reused_on_14(self) -> None:
        agy = _FakeAgy(self.records_dir, version="1.4.5")
        kept = str(uuid.uuid4())
        agy.keep(kept)
        driver = _driver(agy)
        self._keep_state(driver, kept)

        project_id, _digest = driver._ensure_project(self.project_root, tool=True)

        self.assertEqual(project_id, kept)
        self.assertEqual(agy.registrations, 0)

    def test_kept_id_without_record_is_registered_again_on_14(self) -> None:
        agy = _FakeAgy(self.records_dir, version="1.4.5")
        driver = _driver(agy)
        state = self._keep_state(driver, str(uuid.uuid4()))

        project_id, _digest = driver._ensure_project(self.project_root, tool=True)

        self.assertEqual(agy.registrations, 1)
        self.assertIn(project_id, agy.known)
        saved = json.loads(state.read_text(encoding="utf-8"))
        self.assertEqual(saved["project_id"], project_id)

    def test_fresh_project_without_record_fails_closed_on_14(self) -> None:
        agy = _FakeAgy(self.records_dir, forget_new=True, version="1.4.5")
        driver = _driver(agy)
        self._keep_state(driver, str(uuid.uuid4()))

        with self.assertRaisesRegex(LocalAgentUnavailableError, "no record"):
            driver._ensure_project(self.project_root, tool=True)
        self.assertEqual(agy.registrations, 1)

    def test_missing_records_dir_is_not_a_verdict(self) -> None:
        agy = _FakeAgy(self.records_dir / "moved", version="1.4.5")
        _driver(agy)._verify_project_record(str(uuid.uuid4()))

    def test_log_without_the_line_is_not_a_verdict(self) -> None:
        AgyLocalAgentDriver._verify_project_resolved(
            "Language server version: 1.2.2\n", str(uuid.uuid4())
        )


if __name__ == "__main__":
    unittest.main()
