"""The transcription process's sandbox and how every package process is started (plan 4.5 items 27-29, WP-H).

- A package's process starts isolated (`-I`; the transcription's also `-B`),
  its import path its own roots and the interpreter's library, its standard
  streams UTF-8, its working directory the task's (transcription) or its own
  run directory (the others; owner H3).
- The transcription's `USERPROFILE`, `HOME`, `APPDATA`, `LOCALAPPDATA` are the
  sandbox's shared home under `engines\\finesub-transcribe\\sandbox\\home\\`;
  its `TEMP` / `TMP` a directory of its own under `sandbox\\temp\\`, made
  before it starts with an anchor file held open in it, and deleted once it
  has ended. A start-up sweep deletes the ones whose holder is gone, never a
  live one. Two processes at once share the home and never a TEMP.
- The parent's `CUDA_VISIBLE_DEVICES` is never handed on (the card of a
  `cuda:N` request is set by the transcription adapter, owner H6).

Fake packages stand in for the real ones: what is tested is the engine host.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from nonoka_x import engine_host
from nonoka_x.runtime import fsops

#: A package entry that answers each request with what its process looks like.
PROBE = '''
import json, os, sys, tempfile
from pathlib import Path
first = sys.stdin.readline()
for raw in sys.stdin:
    request = json.loads(raw)
    if request.get("op") == "write-temp":
        target = Path(tempfile.gettempdir()) / "written-by-the-engine.txt"
        target.write_text("x", encoding="utf-8")
    probe = {
        "environment": dict(os.environ),
        "isolated": sys.flags.isolated, "no_bytecode": sys.flags.dont_write_bytecode,
        "no_user_site": sys.flags.no_user_site,
        "path": sys.path, "cwd": os.getcwd(),
        "encodings": [sys.stdin.encoding, sys.stdout.encoding, sys.stderr.encoding],
        "temp": tempfile.gettempdir(), "home": str(Path.home()),
        "echo": request.get("text"),
    }
    sys.stdout.write(json.dumps({"type": "result", "payload": probe}, ensure_ascii=False) + "\\n")
    sys.stdout.flush()
'''


def make_package(engines: Path, package_id: str, kind: str) -> engine_host.Package:
    entry = package_id.replace("-", "_") + "_entry"
    directory = engines / package_id
    (directory / "adapter" / entry).mkdir(parents=True)
    (directory / "adapter" / entry / "__init__.py").write_text("", encoding="utf-8")
    (directory / "adapter" / entry / "__main__.py").write_text(PROBE, encoding="utf-8")
    manifest: dict = {
        "schema": 1, "id": package_id, "kind": kind, "entry": entry, "paths": ["adapter"],
        "interpreter": "python-layer" if kind == "transcription" else "launcher",
    }
    if kind in ("transcription", "correction"):
        manifest["engine"] = {
            "id": package_id, "label": package_id, "kind": "local-gpu",
            "features": {}, "keys": [],
            "environment": {"components": [], "models": [], "optional_components": [], "optional_models": [],
                            "gpu": "optional"},
        }
    (directory / "PACKAGE.json").write_text(json.dumps(manifest), encoding="utf-8")
    return engine_host.discover(engines)[package_id]


class SandboxTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="nonoka-sandbox-"))
        self.engines = self.root / "engines"
        self.data = self.root / "data"
        self.transcribe = make_package(self.engines, "fake-transcribe", "transcription")
        self.correct = make_package(self.engines, "fake-correct", "correction")
        self.task = self.data / "tasks" / "t1"
        self.task.mkdir(parents=True)

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def context(self, package: engine_host.Package, **extra) -> dict:
        package_data = self.data / "engines" / package.id
        run = package_data / "run" / "test"
        run.mkdir(parents=True, exist_ok=True)
        return {
            "schema": 1, "package_id": package.id,
            "paths": {"package_data": str(package_data), "run": str(run)},
            "proxy": {"policy": False, "url": None},
            "runtime": {"tools": [str(self.root / "tools" / "ffmpeg")]},
            **extra,
        }

    def start(self, package, context=None, **kwargs) -> engine_host.PackageProcess:
        return engine_host.PackageProcess(package, context or self.context(package), python=sys.executable, **kwargs)

    def sandbox(self) -> Path:
        return self.data / "engines" / self.transcribe.id / "sandbox"

    # -- how a process starts -------------------------------------------------------------------------------

    def test_a_transcription_process_is_isolated_without_bytecode_in_the_task_directory(self) -> None:
        with self.start(self.transcribe, task_dir=self.task) as process:
            probe = process.request("probe", {"text": "日本語のテスト"})
        self.assertEqual((probe["isolated"], probe["no_bytecode"], probe["no_user_site"]), (1, 1, 1))
        self.assertEqual(Path(probe["cwd"]), self.task)
        self.assertEqual(probe["path"][0], str(self.engines / self.transcribe.id / "adapter"))
        # Nothing but the package's own roots and the interpreter's own library.
        prefix = Path(sys.base_prefix).resolve()
        for entry in probe["path"][1:]:
            self.assertTrue(Path(entry).resolve().is_relative_to(prefix), entry)
        self.assertEqual(probe["encodings"], ["utf-8", "utf-8", "utf-8"])
        self.assertEqual(probe["echo"], "日本語のテスト")

    def test_a_correction_process_is_isolated_in_its_run_directory(self) -> None:
        context = self.context(self.correct)
        with self.start(self.correct, context) as process:
            probe = process.request("probe")
        self.assertEqual((probe["isolated"], probe["no_bytecode"]), (1, 0))
        self.assertEqual(Path(probe["cwd"]), Path(context["paths"]["run"]))

    # -- the sandbox ----------------------------------------------------------------------------------------

    def test_the_transcription_s_profile_and_temp_are_the_sandbox_s(self) -> None:
        with self.start(self.transcribe, task_dir=self.task) as process:
            temp = process.sandbox.temp
            probe = process.request("write-temp")
            self.assertTrue((temp / engine_host.TEMP_ANCHOR).is_file())
            self.assertTrue((temp / "written-by-the-engine.txt").is_file())
        environment = probe["environment"]
        home = self.sandbox() / "home"
        self.assertEqual(Path(environment["USERPROFILE"]), home)
        self.assertEqual(Path(environment["HOME"]), home)
        self.assertEqual(Path(environment["APPDATA"]), home / "AppData" / "Roaming")
        self.assertEqual(Path(environment["LOCALAPPDATA"]), home / "AppData" / "Local")
        if os.name == "nt":
            # Owner H8: the home's drive and path are the sandbox's too.
            self.assertEqual(Path(environment["HOMEDRIVE"] + environment["HOMEPATH"]), home)
        self.assertEqual(Path(environment["TEMP"]), temp)
        self.assertEqual(Path(environment["TMP"]), temp)
        self.assertEqual(Path(probe["temp"]), temp)
        self.assertEqual(Path(probe["home"]), home)
        self.assertEqual(temp.parent, self.sandbox() / "temp")
        # Ended: its TEMP is gone, the shared home stays.
        self.assertFalse(temp.exists())
        self.assertTrue((home / "AppData" / "Roaming").is_dir())

    def test_the_correction_keeps_the_real_profile(self) -> None:
        with self.start(self.correct) as process:
            probe = process.request("probe")
        for name in ("USERPROFILE", "APPDATA", "LOCALAPPDATA", "TEMP", "TMP", "HOMEDRIVE", "HOMEPATH"):
            self.assertEqual(probe["environment"].get(name), os.environ.get(name), name)
        self.assertFalse(self.sandbox().exists())

    def test_the_anchor_is_held_while_the_process_runs(self) -> None:
        with self.start(self.transcribe, task_dir=self.task) as process:
            temp = process.sandbox.temp
            self.assertFalse(fsops.try_lock(temp / engine_host.TEMP_ANCHOR))
            # What emptying TEMP would run into (torch's AOTI loader did that).
            with self.assertRaises(OSError):
                (temp / engine_host.TEMP_ANCHOR).unlink()
            self.assertEqual(engine_host.sweep_sandbox([self.sandbox().parent]), [])
            self.assertTrue(temp.is_dir())

    def test_the_sweep_takes_only_the_leftovers_of_processes_that_are_gone(self) -> None:
        temp_root = self.sandbox() / "temp"
        crashed = temp_root / "999-deadbeef0000"
        crashed.mkdir(parents=True)
        (crashed / engine_host.TEMP_ANCHOR).write_text("", encoding="utf-8")
        (crashed / "left.bin").write_bytes(b"x")
        never_anchored = temp_root / "998-deadbeef0001"
        never_anchored.mkdir()
        with self.start(self.transcribe, task_dir=self.task) as process:
            live = process.sandbox.temp
            # Starting a transcription process swept the leftovers first.
            self.assertFalse(crashed.exists())
            self.assertFalse(never_anchored.exists())
            self.assertTrue(live.is_dir())

    def test_two_transcription_processes_share_the_home_and_never_a_temp(self) -> None:
        results: dict[int, dict] = {}

        def run(index: int) -> None:
            with self.start(self.transcribe, task_dir=self.task) as process:
                results[index] = {"temp": str(process.sandbox.temp), **process.request("write-temp")}

        threads = [threading.Thread(target=run, args=(index,)) for index in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0]["environment"]["USERPROFILE"], results[1]["environment"]["USERPROFILE"])
        self.assertNotEqual(results[0]["temp"], results[1]["temp"])
        self.assertEqual(sorted((self.sandbox() / "temp").iterdir()), [])

    def test_a_process_that_cannot_start_leaves_no_temp_behind(self) -> None:
        with self.assertRaises(engine_host.PackageUnavailable):
            engine_host.PackageProcess(
                self.transcribe, self.context(self.transcribe), python=str(self.root / "missing" / "python.exe"),
                task_dir=self.task,
            )
        self.assertEqual(sorted((self.sandbox() / "temp").iterdir()), [])

    def test_no_sandbox_without_a_data_directory(self) -> None:
        context = self.context(self.transcribe)
        del context["paths"]["package_data"]
        with self.assertRaises(engine_host.PackageUnavailable):
            self.start(self.transcribe, context, task_dir=self.task)

    # -- the card ---------------------------------------------------------------------------------------------

    def test_the_parent_s_card_is_never_handed_on(self) -> None:
        # The card a `cuda:N` request names is set by the transcription
        # package's adapter from the job (owner H6, tests/test_engine_finesub.py).
        previous = os.environ.get("CUDA_VISIBLE_DEVICES")
        os.environ["CUDA_VISIBLE_DEVICES"] = "7"
        try:
            with self.start(self.transcribe, task_dir=self.task) as process:
                self.assertNotIn("CUDA_VISIBLE_DEVICES", process.request("probe")["environment"])
            with self.start(self.correct) as process:
                self.assertNotIn("CUDA_VISIBLE_DEVICES", process.request("probe")["environment"])
        finally:
            if previous is None:
                os.environ.pop("CUDA_VISIBLE_DEVICES", None)
            else:
                os.environ["CUDA_VISIBLE_DEVICES"] = previous


if __name__ == "__main__":
    unittest.main()
