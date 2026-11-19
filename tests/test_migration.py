"""`nonoka_x.migration`: the old data layout moves to the new one (WP-M1, WP-M2).

Every test works on a copy of the old layout built here from small fake
files; nothing touches a real installation. The network is stubbed for the
whole module and every test asserts it was never used.

"Another drive" is simulated by a big-file root recorded in `storage.json`
outside the main data directory: the rule under test is that the root is
renamed within its own parent and never copied, which holds the same on any
volume.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import socket
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from nonoka_x import migration  # noqa: E402
from nonoka_x.migration import __main__ as cli  # noqa: E402
from nonoka_x.migration import runner as executing  # noqa: E402
from nonoka_x.migration import hooks as deferred  # noqa: E402
from nonoka_x.migration import plan as planning  # noqa: E402
from nonoka_x.migration import tasks as task_filing  # noqa: E402
from nonoka_x.runtime import fsops  # noqa: E402

WINDOWS = os.name == "nt"
OLD_SETTINGS = ("finesub" + ".env", "finesub" + ".toml")


# -- no network, anywhere --------------------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    attempts: list[str] = []

    def refuse(name):
        def stub(*args, **kwargs):
            attempts.append(f"{name}{args[:2]!r}")
            raise OSError(f"network use in a migration test: {name}")

        return stub

    monkeypatch.setattr(socket.socket, "connect", refuse("connect"))
    monkeypatch.setattr(socket.socket, "connect_ex", refuse("connect_ex"))
    monkeypatch.setattr(socket, "create_connection", refuse("create_connection"))
    monkeypatch.setattr(socket, "getaddrinfo", refuse("getaddrinfo"))
    yield attempts
    assert attempts == [], f"the migration used the network: {attempts}"


# -- an old installation ---------------------------------------------------------------------------------------


def _write(path: Path, content: str | bytes = "x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8")
    return path


class OldInstall:
    def __init__(self, base: Path, *, relocated: bool, python_builds: bool, complete_models: bool) -> None:
        self.base = base
        self.data = base / "Roaming" / "Nonoka X"
        if relocated:
            self.old_root = base / "OtherDrive" / "Nonoka X" / "finesub"
        else:
            self.old_root = self.data / "finesub"
        self.new_root = self.old_root.parent / "runtime"
        self.migrated = self.new_root / "layers" / "models" / "migrated"
        self.knowledge = self.data / "engines" / "finesub-knowledge" / "knowledge"
        self.limit_state = self.data / "engines" / "finesub-correct" / "state"
        self.local_finesub = base / "Local" / "FineSub"
        self.system_python = base / "Envir" / "Python312"
        self.task = self.data / "tasks" / "17e7299434ac40deb1b9600deffb0d58"
        self.native = self.task / "native"
        self.staged = self.data / "app-resources" / "064cb39512581cdc" / "nonoka_x"

        data = self.data
        _write(data / "preferences.json", '{"theme": "dark"}\n')
        _write(data / "library.json", "[]\n")
        _write(data / "documents" / "doc-1.json", '{"lines": ["こんにちは"]}\n')
        _write(data / OLD_SETTINGS[0], "OPENROUTER_API_KEY=finesub$dpapi$v1$not-a-real-secret\n")
        _write(data / (OLD_SETTINGS[0] + ".lock"), "")
        _write(data / OLD_SETTINGS[1], "[nonoka_models]\ncorrection = \"x\"\n[segmentation]\nmax = 3\n")
        _write(data / (OLD_SETTINGS[1] + ".lock"), "")
        _write(data / ("model_catalog" + ".toml"), "[models]\n")
        _write(data / "state", '{"limits": {}}\n')
        _write(data / "state.lock", "")
        _write(data / "knowledge" / "subjects" / "entry.md", "# 用語\n")
        _write(data / "knowledge.lock", "")
        _write(data / "finesub-data" / "download-routes.json", '{"region": "global"}\n')
        _write(data / "finesub-data" / "locations.json", '{"schemaVersion": 1}\n')
        _write(data / "finesub-data" / "locations.lock", "")
        _write(data / "bootstrap" / "python-installations" / "cpython" / "python.exe", b"MZ launcher")
        _write(self.staged / "src" / "nonoka_x" / "sidecar.py", "print('staged')\n")
        _write(self.staged / "third_party" / "finesub" / "pyproject.toml", "[project]\n")
        _write(self.staged / "third_party" / "finesub" / "cache" / "qwen-referee-accel" / "k" / "a.bin", b"\0" * 64)
        _write(self.task / "snapshot.json", json.dumps({"state": "completed"}))
        _write(self.task / "request.json", "{}")
        _write(self.task / "workspace" / "clip.mp4-stable.json", '{"segments": []}')
        if relocated:
            _write(
                data / "storage.json",
                json.dumps({"schema": 1, "runtimeDir": str(self.old_root), "videoDir": ""}, indent=2) + "\n",
            )

        root = self.old_root
        _write(root / ".finesub-store.json", '{"schemaVersion": 1, "kind": "finesub-store"}')
        _write(root / "register-location.cmd", "@echo off\r\n")
        install = root / "runtime"
        _write(install / ".install.lock", "")
        _write(install / "python" / "pyvenv.cfg", f"home = {self.system_python}\nversion_info = 3.12.1\n")
        _write(install / "python" / "Lib" / "site-packages" / "torch" / "__init__.py", "# torch\n")
        _write(install / "python" / "Scripts" / "python.exe", b"MZ venv")
        _write(install / "python" / "finesub-runtime.json", '{"schemaVersion": 2}')
        if python_builds:
            _write(install / "python-builds" / "cpython-3.12.1-windows" / "python.exe", b"MZ pbs")
        for tool in ("ffmpeg", "uv", "tokcount", "yt-dlp", "aria2c", "node", "pot-provider"):
            _write(install / tool / "1.0" / f"{tool}.exe", b"MZ " + tool.encode())
            _write(install / tool / "current.json", '{"current": "1.0"}')
        _write(install / "cuda-extras" / "readme.txt", "an entry nobody registered")
        _write(root / "cache" / "uv" / "wheels" / "torch.whl", b"PK" + b"\0" * 32)

        models = root / "models"
        separator = models / "audio-separator"
        _write(separator / "model_bs_roformer_ep_317_sdr_12.9755.ckpt", b"ckpt" * 100)
        _write(separator / "model_bs_roformer_ep_317_sdr_12.9755.ckpt.lock", "")
        _write(separator / "model_bs_roformer_ep_317_sdr_12.9755.ckpt.verified", '{"sha256": "aa"}')
        _write(separator / "download_checks.json", "{}")
        if complete_models:
            _write(separator / "model_bs_roformer_ep_317_sdr_12.9755.yaml", "audio: {}\n")
        _write(separator / "accel" / "v2-2.11.0+cu128-sm86" / "model.pt2", b"aoti" * 50)
        hf = models / "huggingface"
        _write(hf / ".agent_harnesses.json", "{}")
        _write(hf / "hub" / "CACHEDIR.TAG", "Signature: 8a477f597d28d172789f06886806bc55\n")
        (hf / "hub" / ".locks" / "models--Qwen--Qwen3-ASR-0.6B-hf").mkdir(parents=True)
        qwen = hf / "hub" / "models--Qwen--Qwen3-ASR-0.6B-hf"
        _write(qwen / "snapshots" / "7f1569a48a89f3e3f4dc3a5c9d28bddd903bc76c" / "model.safetensors", b"w" * 300)
        _write(qwen / ".finesub-verified.json", "{}")
        if complete_models:
            whisper = hf / "hub" / "models--mobiuslabsgmbh--faster-whisper-large-v3-turbo"
            _write(whisper / "snapshots" / "0a363e9161cbc7ed1431c9597a8ceaf0c4f78fcf" / "model.bin", b"m" * 200)
        _write(hf / "xet" / "logs" / "xet.log", "log\n")

        # Outside the installation: never touched.
        _write(self.local_finesub / "agent-capsules" / "c1" / "capsule.json", "{}")
        _write(self.local_finesub / "download-routes.json", "{}")
        _write(self.system_python / "python.exe", b"MZ system")

    def snapshot(self, *, skip_journal: bool = False) -> dict[str, str]:
        """Every file and directory under the test's base, with a content digest."""

        result: dict[str, str] = {}
        for path in sorted(self.base.rglob("*")):
            relative = path.relative_to(self.base).as_posix()
            if skip_journal and relative.endswith("runtime-state/migration.json"):
                continue
            if path.is_dir():
                result[relative + "/"] = "dir"
            else:
                result[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
        return result

    def digest(self, path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def make_install(tmp_path):
    counter = iter(range(1000))

    def make(*, relocated: bool = False, python_builds: bool = True, complete_models: bool = True) -> OldInstall:
        return OldInstall(
            tmp_path / f"case{next(counter)}",
            relocated=relocated,
            python_builds=python_builds,
            complete_models=complete_models,
        )

    return make


def _migrate(install: OldInstall, **kwargs) -> planning.MigrationPlan:
    plan = migration.prepare(install.data, **kwargs)
    assert plan.state == planning.FRESH
    assert not plan.blockers, plan.blockers
    return migration.execute(plan)


# -- detection ------------------------------------------------------------------------------------------------


def test_no_old_layout_means_nothing_happens(tmp_path, capsys):
    data = tmp_path / "Nonoka X"
    _write(data / "preferences.json", "{}")
    _write(data / "settings.json", "{}")
    (data / "runtime" / "layers").mkdir(parents=True)
    before = sorted(str(path) for path in tmp_path.rglob("*"))

    plan = migration.prepare(data)
    assert plan.state == planning.NOTHING
    assert not plan.needed
    assert migration.execute(plan) is plan
    assert cli.main(["--data-dir", str(data), "--yes"]) == cli.EXIT_OK
    assert "no old layout found" in capsys.readouterr().out
    assert sorted(str(path) for path in tmp_path.rglob("*")) == before
    assert not migration.journal_path(data).exists()


def test_detect_finds_the_default_and_the_recorded_root(make_install):
    default = make_install()
    layout = migration.detect(default.data)
    assert (layout.old_root, layout.new_root, layout.recorded) == (default.old_root, default.data / "runtime", False)

    relocated = make_install(relocated=True)
    layout = migration.detect(relocated.data)
    assert (layout.old_root, layout.new_root, layout.recorded) == (relocated.old_root, relocated.new_root, True)
    # The new root is the old one's sibling, so the rename never leaves its volume.
    assert layout.new_root.parent == layout.old_root.parent


def test_a_recorded_root_already_named_runtime_is_the_new_layout(tmp_path):
    data = tmp_path / "Nonoka X"
    root = tmp_path / "Other" / "Nonoka X" / "runtime"
    root.mkdir(parents=True)
    _write(data / "storage.json", json.dumps({"schema": 1, "runtimeDir": str(root), "videoDir": ""}))

    layout = migration.detect(data)
    assert layout.old_root is None and layout.new_root == root
    assert migration.prepare(data).state == planning.NOTHING


# -- the plan, read only ---------------------------------------------------------------------------------------


@pytest.mark.parametrize("relocated", [False, True])
def test_dry_run_changes_nothing(make_install, capsys, relocated):
    install = make_install(relocated=relocated, complete_models=False)
    before = install.snapshot()

    assert cli.main(["--data-dir", str(install.data), "--dry-run"]) == cli.EXIT_OK
    text = capsys.readouterr().out
    assert cli.main(["--data-dir", str(install.data), "--dry-run", "--json"]) == cli.EXIT_OK
    record = json.loads(capsys.readouterr().out)

    assert install.snapshot() == before
    assert str(install.old_root) in text and str(install.new_root) in text
    assert str(install.migrated) in text
    assert record["state"] == planning.FRESH
    assert record["new_root"] == str(install.new_root)
    assert [step["id"] for step in record["steps"]] == list(planning.STEP_ORDER)
    # Paths and sizes only: no setting or key content ends up in the plan.
    assert "not-a-real-secret" not in text and "not-a-real-secret" not in json.dumps(record)


def test_the_plan_lists_moves_deletions_and_deferred_steps(make_install):
    install = make_install(relocated=True)
    plan = migration.prepare(install.data)

    root = plan.step(planning.RUNTIME_ROOT)
    assert [(action.kind, action.path, action.target) for action in root.actions if action.kind == planning.RENAME] == [
        (planning.RENAME, str(install.old_root), str(install.new_root))
    ]
    assert any(action.kind == planning.REWRITE and action.path.endswith("storage.json") for action in root.actions)

    models = plan.step(planning.MODELS)
    assert [(action.kind, action.target) for action in models.actions] == [(planning.RENAME, str(install.migrated))]
    # Deleted with the caches, after the reversible steps: a rollback gives the models back whole.
    deleted = {
        Path(action.path).relative_to(install.old_root / "models").as_posix()
        for action in plan.step(planning.CACHES).actions
        if Path(action.path).is_relative_to(install.old_root / "models")
    }
    assert deleted == {
        "audio-separator/accel",
        "huggingface/xet",
        "huggingface/hub/.locks",
        "audio-separator/model_bs_roformer_ep_317_sdr_12.9755.ckpt.lock",
    }

    old_environment = {Path(action.path).name for action in plan.step(planning.OLD_ENVIRONMENT).actions}
    assert {"python", "python-builds", "ffmpeg", "uv", "pot-provider", "cuda-extras", "runtime"} <= old_environment

    for step_id, owner in deferred.DEFERRED_OWNERS.items():
        step = plan.step(step_id)
        assert step.owner == owner and step.will_skip and step.actions, step_id
    assert set(deferred.DEFERRED_OWNERS) == {deferred.SETTINGS, deferred.MODEL_COMPLETION, deferred.LAYER_DOWNLOAD}

    knowledge = plan.step(deferred.KNOWLEDGE)
    assert knowledge.owner is None
    assert [(action.kind, Path(action.path).name, action.target) for action in knowledge.actions] == [
        (planning.RENAME, "knowledge", str(install.knowledge)),
        (planning.RENAME, "knowledge.lock", str(install.knowledge) + ".lock"),
    ]
    assert [(action.kind, action.target) for action in plan.step(deferred.LIMIT_STATE).actions] == [
        (planning.RENAME, str(install.limit_state)),
        (planning.RENAME, str(install.limit_state) + ".lock"),
    ]
    assert [(action.kind, action.path) for action in plan.step(deferred.TASK_PRODUCTS).actions] == [
        (planning.FILE_TASK, str(install.task / "workspace"))
    ]
    assert plan.step(deferred.MODEL_COMPLETION).download_bytes is None
    assert plan.totals()["download_bytes"] is None
    assert plan.totals()["delete_bytes"] > 0 and plan.totals()["move_bytes"] > 0


# -- carrying it out ------------------------------------------------------------------------------------------


def _assert_migrated(install: OldInstall, before: dict[str, str]) -> None:
    assert not install.old_root.exists()
    assert install.new_root.is_dir()

    # The models moved as they were, less the AOTI builds, the xet cache and the locks.
    migrated = install.migrated
    old_models = install.old_root.relative_to(install.base).as_posix() + "/models/"
    kept = {
        key[len(old_models):]: value
        for key, value in before.items()
        if key.startswith(old_models) and value != "dir"
        and "/accel/" not in key and "/xet/" not in key and not key.endswith(".lock")
    }
    now = {
        path.relative_to(migrated).as_posix(): install.digest(path)
        for path in migrated.rglob("*")
        if path.is_file()
    }
    assert now == kept
    assert not (migrated / "audio-separator" / "accel").exists()
    assert not (migrated / "huggingface" / "xet").exists()
    assert not (migrated / "huggingface" / "hub" / ".locks").exists()
    assert not (install.new_root / "layers" / "models" / "current.json").exists()

    # The old environment, tools, caches and markers are gone.
    assert sorted(path.name for path in install.new_root.iterdir()) == ["layers"]
    assert not (install.data / ("model_catalog" + ".toml")).exists()
    assert not (install.data / "finesub-data").exists()
    assert not (install.staged / "third_party" / "finesub" / "cache").exists()

    # The knowledge base, the rate-limit state and the task's outputs moved, content unchanged.
    data = install.data.relative_to(install.base).as_posix()
    task = install.task.relative_to(install.base).as_posix()
    moved = {
        f"{data}/knowledge/subjects/entry.md": install.knowledge / "subjects" / "entry.md",
        f"{data}/knowledge.lock": install.knowledge.with_name("knowledge.lock"),
        f"{data}/state": install.limit_state,
        f"{data}/state.lock": install.limit_state.with_name("state.lock"),
        f"{task}/workspace/clip.mp4-stable.json": install.native / "finesub-transcribe" / "clip.mp4-stable.json",
    }
    for key, path in moved.items():
        assert install.digest(path) == before[key], key
    assert not (install.data / "knowledge").exists() and not (install.data / "state").exists()
    assert not (install.task / "workspace").exists()

    # Everything else is byte for byte what it was.
    def outside(key: str) -> bool:
        return not (
            key.startswith(install.old_root.relative_to(install.base).as_posix())
            or key.startswith(install.new_root.relative_to(install.base).as_posix())
            or "finesub-data" in key
            or key.endswith("model_catalog.toml")
            or "/third_party/finesub/cache" in key
            or "runtime-state" in key
            or key.endswith("storage.json")
            or key.startswith(f"{data}/knowledge")
            or key.startswith(f"{data}/state")
            or key.startswith(f"{data}/engines")
            or key.startswith(f"{task}/workspace")
            or key.startswith(f"{task}/native")
        )

    after = install.snapshot()
    assert {key: value for key, value in after.items() if outside(key)} == {
        key: value for key, value in before.items() if outside(key)
    }

    journal = migration.read_journal(install.data)
    assert journal["status"] == planning.COMPLETED
    statuses = {step_id: record["status"] for step_id, record in journal["steps"].items()}
    for step_id in (
        planning.RUNTIME_ROOT, planning.MODELS, deferred.KNOWLEDGE, deferred.LIMIT_STATE, deferred.TASK_PRODUCTS,
        planning.OLD_ENVIRONMENT, planning.CACHES, planning.REBUILDABLE,
    ):
        assert statuses[step_id] == executing.DONE, step_id
    for step_id in deferred.DEFERRED_OWNERS:
        assert statuses[step_id] == executing.SKIPPED, step_id


@pytest.mark.parametrize(
    ("relocated", "python_builds", "complete_models"),
    [
        (False, True, True),
        (True, True, True),
        (True, False, True),
        (False, False, False),
        (True, True, False),
    ],
    ids=["default", "other-drive", "other-drive-no-python-builds", "default-incomplete-models", "other-drive-incomplete-models"],
)
def test_migration_puts_everything_in_place(make_install, relocated, python_builds, complete_models):
    install = make_install(relocated=relocated, python_builds=python_builds, complete_models=complete_models)
    before = install.snapshot()
    storage_before = (install.data / "storage.json").read_text(encoding="utf-8") if relocated else None

    plan = _migrate(install)

    assert plan.state == planning.COMPLETED
    _assert_migrated(install, before)
    if relocated:
        record = json.loads((install.data / "storage.json").read_text(encoding="utf-8"))
        assert record == {**json.loads(storage_before), "runtimeDir": str(install.new_root)}
    else:
        assert not (install.data / "storage.json").exists()


def test_incomplete_models_are_left_for_the_completion_step(make_install):
    install = make_install(complete_models=False)
    _migrate(install)

    separator = install.migrated / "audio-separator"
    assert (separator / "model_bs_roformer_ep_317_sdr_12.9755.ckpt").is_file()
    assert not (separator / "model_bs_roformer_ep_317_sdr_12.9755.yaml").exists()
    journal = migration.read_journal(install.data)
    assert journal["steps"][deferred.MODEL_COMPLETION] == {**journal["steps"][deferred.MODEL_COMPLETION], "status": "skipped"}
    assert "WP-E3" in journal["steps"][deferred.MODEL_COMPLETION]["note"]


def test_what_is_not_ours_is_never_touched(make_install):
    install = make_install(relocated=True)
    keep = [
        install.local_finesub / "agent-capsules" / "c1" / "capsule.json",
        install.local_finesub / "download-routes.json",
        install.system_python / "python.exe",
        install.data / "bootstrap" / "python-installations" / "cpython" / "python.exe",
        install.staged / "src" / "nonoka_x" / "sidecar.py",
        install.staged / "third_party" / "finesub" / "pyproject.toml",
        install.data / OLD_SETTINGS[0],
        install.data / OLD_SETTINGS[1],
        install.data / "documents" / "doc-1.json",
    ]
    digests = {path: install.digest(path) for path in keep}
    # Moved, and the bytes with them.
    moved = {
        install.data / "state": install.limit_state,
        install.data / "knowledge" / "subjects" / "entry.md": install.knowledge / "subjects" / "entry.md",
        install.task / "workspace" / "clip.mp4-stable.json": install.native / "finesub-transcribe" / "clip.mp4-stable.json",
    }
    moved_digests = {target: install.digest(source) for source, target in moved.items()}

    _migrate(install)

    assert {path: install.digest(path) for path in keep} == digests
    assert {path: install.digest(path) for path in moved_digests} == moved_digests


def test_unrecognised_root_entries_move_along_and_stay(make_install):
    install = make_install()
    _write(install.old_root / "agent-capsules" / "mine.txt", "user's")
    plan = migration.prepare(install.data)
    kept = [action for action in plan.step(planning.RUNTIME_ROOT).actions if action.kind == planning.KEEP]
    assert [Path(action.path).name for action in kept] == ["agent-capsules"]

    migration.execute(plan)

    assert (install.new_root / "agent-capsules" / "mine.txt").read_text(encoding="utf-8") == "user's"


def test_a_checkout_clears_its_vendor_cache(tmp_path, capsys, monkeypatch):
    # The layer steps (WP-E3) would reach R2; this test is about the vendor cache.
    only_settings = {deferred.SETTINGS: deferred.available_hooks()[deferred.SETTINGS]}
    monkeypatch.setattr(cli, "available_hooks", lambda: only_settings)
    repo = tmp_path / "nonoka-sub-x"
    install = OldInstall(tmp_path / "base", relocated=False, python_builds=False, complete_models=True)
    # A checkout's data directory is its .local: lay the old install there.
    repo.mkdir(parents=True)
    os.replace(install.data, repo / ".local")
    _write(repo / "third_party" / "finesub" / "src" / "finesub" / "stages.py", "# locked\n")
    _write(repo / "third_party" / "finesub" / "cache" / "qwen-referee-accel" / "k" / "a.bin", b"\0")
    stages = hashlib.sha256((repo / "third_party" / "finesub" / "src" / "finesub" / "stages.py").read_bytes()).hexdigest()

    assert cli.main(["--checkout", str(repo), "--yes"]) == cli.EXIT_OK
    capsys.readouterr()

    assert not (repo / "third_party" / "finesub" / "cache").exists()
    assert hashlib.sha256((repo / "third_party" / "finesub" / "src" / "finesub" / "stages.py").read_bytes()).hexdigest() == stages
    assert (repo / ".local" / "runtime" / "layers" / "models" / "migrated").is_dir()
    assert not (repo / ".local" / "finesub").exists()


def test_a_finished_migration_is_not_repeated(make_install, capsys, monkeypatch):
    install = make_install()
    _migrate(install)
    after = install.snapshot()

    plan = migration.prepare(install.data)
    assert plan.state == planning.COMPLETED
    assert migration.execute(plan).state == planning.COMPLETED
    # The same hooks as the first run: nothing skipped can run now (catching up has its own test).
    monkeypatch.setattr(cli, "available_hooks", lambda: {})
    assert cli.main(["--data-dir", str(install.data), "--yes"]) == cli.EXIT_OK
    capsys.readouterr()
    assert install.snapshot() == after


def test_hooks_run_in_place_of_the_skipped_steps(make_install):
    install = make_install()
    calls: list[tuple[str, bool]] = []

    class Hook:
        def __init__(self, name: str, size: int) -> None:
            self.name, self.size = name, size

        def download_bytes(self, context):
            return self.size

        def run(self, context):
            calls.append((self.name, context.migrated_models.is_dir()))

    hooks = {
        deferred.MODEL_COMPLETION: Hook("complete", 123),
        deferred.LAYER_DOWNLOAD: Hook("download", 4000),
    }
    plan = migration.prepare(install.data, hooks=hooks)
    assert plan.totals()["download_bytes"] == 4123
    migration.execute(plan, hooks=hooks)

    assert calls == [("complete", True), ("download", True)]
    journal = migration.read_journal(install.data)
    assert journal["steps"][deferred.MODEL_COMPLETION]["status"] == executing.DONE
    assert journal["steps"][deferred.SETTINGS]["status"] == executing.SKIPPED


# -- interrupted at any point ----------------------------------------------------------------------------------


class Killed(BaseException):
    """Stands in for the process dying: not an Exception, so nothing catches it on the way out."""


def _operations(monkeypatch, kill_at: int | None) -> list[str]:
    """Count (and kill at) every disk operation of the runner and of the task filing."""

    seen: list[str] = []
    for prefix, module, names in (
        ("runner", executing, ("replace_path", "remove_tree", "_write")),
        ("tasks", task_filing, ("replace_path", "remove_tree", "write_atomic", "_write_bytes")),
    ):
        for name in names:
            original = getattr(module, name)

            def wrapped(*args, _original=original, _name=f"{prefix}.{name}", **kwargs):
                seen.append(_name)
                if kill_at is not None and len(seen) == kill_at:
                    raise Killed(_name)
                return _original(*args, **kwargs)

            monkeypatch.setattr(module, name, wrapped)
    return seen


def test_a_migration_killed_at_any_operation_continues_to_the_same_result(make_install, monkeypatch):
    reference = make_install(relocated=True, complete_models=False)
    with monkeypatch.context() as patch:
        operations = _operations(patch, None)
        _migrate(reference)
    # Snapshots are relative to each case's base, so they compare across cases;
    # storage.json holds the absolute new root and is checked on its own.
    expected = reference.snapshot(skip_journal=True)
    expected.pop("Roaming/Nonoka X/storage.json")
    total = len(operations)
    assert total > 10

    for kill_at in range(1, total + 1):
        install = make_install(relocated=True, complete_models=False)
        plan = migration.prepare(install.data)
        with monkeypatch.context() as patch:
            _operations(patch, kill_at)
            with pytest.raises(Killed):
                migration.execute(plan)
        resumed = migration.prepare(install.data)
        if migration.read_journal(install.data) is not None:
            assert resumed.state == planning.RESUMING, kill_at
        migration.execute(resumed)
        got = install.snapshot(skip_journal=True)
        got.pop("Roaming/Nonoka X/storage.json")
        assert got == expected, f"killed at operation {kill_at} ({operations[kill_at - 1]})"
        assert json.loads((install.data / "storage.json").read_text(encoding="utf-8"))["runtimeDir"] == str(install.new_root)
        assert migration.read_journal(install.data)["status"] == planning.COMPLETED


def test_dry_run_of_an_interrupted_migration_shows_progress_and_writes_nothing(make_install, monkeypatch, capsys):
    install = make_install()
    plan = migration.prepare(install.data)
    with monkeypatch.context() as patch:
        _operations(patch, 4)
        with pytest.raises(Killed):
            migration.execute(plan)
    before = install.snapshot()

    assert cli.main(["--data-dir", str(install.data), "--dry-run"]) == cli.EXIT_OK
    text = capsys.readouterr().out

    assert "interrupted" in text
    assert install.snapshot() == before


# -- refusing ---------------------------------------------------------------------------------------------------


def _assert_blocked(install: OldInstall, capsys, needle: str) -> None:
    before = install.snapshot()
    plan = migration.prepare(install.data)
    assert any(needle in blocker for blocker in plan.blockers), plan.blockers
    with pytest.raises(migration.MigrationBlocked):
        migration.execute(plan)
    assert cli.main(["--data-dir", str(install.data), "--yes"]) == cli.EXIT_BLOCKED
    assert "Blocked" in capsys.readouterr().out
    assert install.snapshot() == before


def test_an_existing_target_blocks_rather_than_merges(make_install, capsys):
    install = make_install(relocated=True)
    _write(install.new_root / "something.txt", "already here")
    _assert_blocked(install, capsys, "already exists")


def test_a_running_task_blocks(make_install, capsys):
    install = make_install()
    _write(install.data / "tasks" / "running-task" / "snapshot.json", json.dumps({"state": "running"}))
    _assert_blocked(install, capsys, "running-task is running")


def test_a_recorded_root_that_is_missing_blocks(make_install, capsys):
    install = make_install(relocated=True)
    fsops.remove_tree(install.old_root)
    _assert_blocked(install, capsys, "does not exist")


@pytest.mark.skipif(not WINDOWS, reason="junctions are a Windows thing")
def test_a_linked_models_directory_blocks(make_install, capsys, tmp_path):
    install = make_install()
    elsewhere = tmp_path / "elsewhere-models"
    os.replace(install.old_root / "models", elsewhere)
    fsops.create_directory_link(install.old_root / "models", str(elsewhere))
    _assert_blocked(install, capsys, "directory link")
    assert (elsewhere / "audio-separator").is_dir()


@pytest.mark.skipif(not WINDOWS, reason="junctions are a Windows thing")
def test_a_link_inside_models_blocks(make_install, capsys, tmp_path):
    install = make_install()
    elsewhere = _write(tmp_path / "elsewhere" / "w.bin", b"w").parent
    fsops.create_directory_link(install.old_root / "models" / "torch", str(elsewhere))
    _assert_blocked(install, capsys, "contains directory links")


@pytest.mark.skipif(not WINDOWS, reason="junctions are a Windows thing")
def test_a_linked_tool_is_unlinked_not_followed(make_install, tmp_path):
    install = make_install()
    elsewhere = _write(tmp_path / "user-ffmpeg" / "ffmpeg.exe", b"MZ user").parent
    fsops.remove_tree(install.old_root / "runtime" / "ffmpeg")
    fsops.create_directory_link(install.old_root / "runtime" / "ffmpeg", str(elsewhere))

    _migrate(install)

    assert (elsewhere / "ffmpeg.exe").read_bytes() == b"MZ user"
    assert not (install.new_root / "runtime").exists()


def test_without_yes_and_without_a_terminal_nothing_happens(make_install, monkeypatch, capsys):
    install = make_install()
    before = install.snapshot()
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False, raising=False)

    assert cli.main(["--data-dir", str(install.data)]) == cli.EXIT_NOT_CONFIRMED
    assert "--yes" in capsys.readouterr().out
    assert install.snapshot() == before


def test_a_no_on_the_terminal_changes_nothing(make_install, monkeypatch, capsys):
    install = make_install()
    before = install.snapshot()
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr("builtins.input", lambda prompt: "n")

    assert cli.main(["--data-dir", str(install.data)]) == cli.EXIT_NOT_CONFIRMED
    capsys.readouterr()
    assert install.snapshot() == before


def test_a_second_migration_at_the_same_time_is_refused(make_install):
    install = make_install()
    plan = migration.prepare(install.data)
    lock = migration.journal_path(install.data).with_name(executing.LOCK_NAME)
    with fsops.holding_lock(lock):
        with pytest.raises(migration.MigrationError, match="another migration"):
            migration.execute(plan)
    assert install.old_root.is_dir()



# == WP-M2 ======================================================================================================

ROOT = Path(__file__).resolve().parents[1]
#: The real loopback calls, captured before `no_network` replaces them (the sidecar test talks to its own server).
_REAL_SOCKET = (socket.socket.connect, socket.create_connection, socket.getaddrinfo)
FIXTURES = ROOT / "tests" / "fixtures" / "finesub"
TITLE = "节目.m4a"


def _settings_hooks() -> dict:
    from nonoka_x.config.migrate import SettingsHook

    return {deferred.SETTINGS: SettingsHook()}


def _without(snapshot: dict[str, str], *needles: str) -> dict[str, str]:
    return {key: value for key, value in snapshot.items() if not any(needle in key for needle in needles)}


def _finished_task(install: OldInstall, task_id: str = "a1b2c3d4e5f60718293a4b5c6d7e8f90", *, with_document: bool = True) -> Path:
    """A completed task as the old worker left it: both halves' outputs in `workspace\\`, a manifest naming them."""

    task = install.data / "tasks" / task_id
    workspace = task / "workspace"
    files = {
        f"{TITLE[:-4]}-vocal.ogg": b"OggS" + b"\0" * 40,
        f"{TITLE[:-4]}-vad.json": b'{"segments": []}',
        f"{TITLE[:-4]}-aligned.json": b'{"segments": []}',
        f"{TITLE[:-4]}-stable.json": (FIXTURES / "sample-stable.json").read_bytes(),
        f"{TITLE[:-4]}-metadata.json": b'{"stages": {}}',
        f"{TITLE[:-4]}-raw.srt": b"1\n00:00:00,000 --> 00:00:01,000\nx\n",
        f"{TITLE[:-4]}.srt": (FIXTURES / "sample-final.srt").read_bytes(),
        f"{TITLE[:-4]}-annotated.csv": (FIXTURES / "sample-annotated.csv").read_bytes(),
        "llm-artifacts/task-artifacts.jsonl": b"{}\n",
    }
    for name, data in files.items():
        _write(workspace / name, data)
    names = {
        "vocal_audio": f"{TITLE[:-4]}-vocal.ogg",
        "vad_json": f"{TITLE[:-4]}-vad.json",
        "aligned_json": f"{TITLE[:-4]}-aligned.json",
        "stable_json": f"{TITLE[:-4]}-stable.json",
        "raw_srt": f"{TITLE[:-4]}-raw.srt",
        "annotated_csv": f"{TITLE[:-4]}-annotated.csv",
        "final_srt": f"{TITLE[:-4]}.srt",
        "metadata_json": f"{TITLE[:-4]}-metadata.json",
    }
    manifest = {
        "schema": 1,
        "task_id": task_id,
        "engine_commit": "0" * 40,
        "artifacts": {
            name: {
                "uri": (workspace / file).resolve().as_uri(),
                "sha256": hashlib.sha256((workspace / file).read_bytes()).hexdigest(),
                "bytes": (workspace / file).stat().st_size,
            }
            for name, file in names.items()
        },
    }
    _write(task / "artifacts.json", json.dumps(manifest, ensure_ascii=False, indent=2))
    _write(task / "snapshot.json", json.dumps({"state": "completed"}))
    _write(task / "request.json", json.dumps({"language": "ja", "target": "final-srt"}))
    if with_document:
        _write(install.data / "documents" / "video-1" / "artifacts.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        _write(install.data / "documents" / "video-1" / "document.json", '{"lines": []}')
    return task


class FakeConversions:
    """Stands in for the two packages' operations: writes a small file, remembers what it was asked."""

    def __init__(self, monkeypatch, *, refuse: str | None = None) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.refuse = refuse

        def request(converter, kind, op, payload):
            self.calls.append((op, dict(payload)))
            if op == self.refuse:
                return {"error": "the products do not line up"}
            Path(payload["output"]).write_text(json.dumps({"op": op}), encoding="utf-8")
            return {op: payload["output"]}

        monkeypatch.setattr(task_filing.Converter, "request", request)


def test_a_state_directory_is_nonoka_own_and_stays(make_install):
    install = make_install()
    (install.data / "state").unlink()
    _write(install.data / "state" / "cloud-asr-quota.json", '{"used": 1}')
    plan = migration.prepare(install.data)
    assert [Path(action.path).name for action in plan.step(deferred.LIMIT_STATE).actions] == ["state.lock"]

    migration.execute(plan)

    assert (install.data / "state" / "cloud-asr-quota.json").read_text(encoding="utf-8") == '{"used": 1}'
    assert not install.limit_state.exists()


def test_an_existing_knowledge_base_in_the_package_directory_blocks(make_install, capsys):
    install = make_install()
    _write(install.knowledge / "subjects" / "other.md", "# 別の\n")
    _assert_blocked(install, capsys, "migration does not merge")


def test_the_knowledge_registrations_are_dropped(make_install):
    install = make_install()
    _write(install.data / "knowledge.run" / "1234.json", "{}")
    _migrate(install)
    assert not (install.data / "knowledge.run").exists()
    assert (install.knowledge / "subjects" / "entry.md").is_file()


def test_an_old_task_is_filed_converted_and_repointed(make_install, monkeypatch):
    install = make_install()
    task = _finished_task(install)
    before = {path.relative_to(task / "workspace").as_posix(): path.read_bytes()
              for path in (task / "workspace").rglob("*") if path.is_file()}
    fake = FakeConversions(monkeypatch)

    _migrate(install)

    transcribe, correct = task / "native" / "finesub-transcribe", task / "native" / "finesub-correct"
    base = TITLE[:-4]
    placed = {
        **{name: transcribe / name for name in (f"{base}-vocal.ogg", f"{base}-vad.json", f"{base}-aligned.json", f"{base}-stable.json")},
        **{name: correct / name for name in (f"{base}-raw.srt", f"{base}.srt", f"{base}-annotated.csv", f"{base}-metadata.json")},
        "llm-artifacts/task-artifacts.jsonl": correct / "llm-artifacts" / "task-artifacts.jsonl",
    }
    for name, path in placed.items():
        assert path.read_bytes() == before[name], name
    # Each half keeps its own run metadata from now on.
    assert (transcribe / f"{base}-metadata.json").read_bytes() == before[f"{base}-metadata.json"]
    assert not (task / "workspace").exists()

    # The two operations WP-L's conversion calls, on the filed files; a local task aligns strictly.
    ops = dict(fake.calls)
    assert ops["correction_json"] == {
        "stable_json": str(transcribe / f"{base}-stable.json"),
        "annotated_csv": str(correct / f"{base}-annotated.csv"),
        "final_srt": str(correct / f"{base}.srt"),
        "output": str(task / ".migration" / "correction.json"),
    }
    assert ops["transcript_json"] == {
        "stable_json": str(transcribe / f"{base}-stable.json"),
        "language": "ja",
        "vad_json": str(transcribe / f"{base}-vad.json"),
        "output": str(task / ".migration" / "transcript.json"),
    }
    assert not (task / ".migration").exists()

    # Both manifests name the new places (same bytes, so the same digests) and the two new files.
    for manifest_path in (task / "artifacts.json", install.data / "documents" / "video-1" / "artifacts.json"):
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        for name, item in manifest["artifacts"].items():
            path = Path(task_filing._uri_path(item["uri"]))
            assert path.is_file(), (manifest_path, name)
            assert hashlib.sha256(path.read_bytes()).hexdigest() == item["sha256"], name
            assert "workspace" not in path.parts
        assert Path(task_filing._uri_path(manifest["artifacts"]["correction_json"]["uri"])) == (task / "correction.json").resolve()
        assert Path(task_filing._uri_path(manifest["artifacts"]["transcript_json"]["uri"])) == (task / "transcript.json").resolve()
    assert json.loads((install.data / "documents" / "video-1" / "document.json").read_text(encoding="utf-8")) == {"lines": []}
    assert not (install.data / "runtime-state" / "migration-backup").exists()
    report = migration.read_journal(install.data)["steps"][deferred.TASK_PRODUCTS]["report"]
    assert report == {"tasks": 2}


def test_a_task_the_conversion_refuses_keeps_its_files_and_is_reported(make_install, monkeypatch):
    install = make_install()
    task = _finished_task(install, with_document=False)
    FakeConversions(monkeypatch, refuse="correction_json")

    _migrate(install)

    assert not (task / "correction.json").exists() and (task / "transcript.json").is_file()
    manifest = json.loads((task / "artifacts.json").read_text(encoding="utf-8"))
    assert "correction_json" not in manifest["artifacts"] and "transcript_json" in manifest["artifacts"]
    report = migration.read_journal(install.data)["steps"][deferred.TASK_PRODUCTS]["report"]
    assert report["not_converted"] == {task.name: {"correction_json": "the products do not line up"}}


def test_a_task_that_already_has_native_is_not_merged(make_install):
    install = make_install()
    _write(install.native / "finesub-transcribe" / "x-stable.json", "{}")
    plan = migration.prepare(install.data)
    [action] = plan.step(deferred.TASK_PRODUCTS).actions
    assert action.kind == planning.KEEP
    migration.execute(plan)
    assert (install.task / "workspace" / "clip.mp4-stable.json").is_file()


def test_the_conversion_runs_the_packages_operations(make_install):
    """The real operations, on the real packages (the launcher is this interpreter)."""

    install = make_install()
    task = _finished_task(install, with_document=False)
    plan = migration.prepare(install.data)
    migration.execute(plan, engines=ROOT / "engines")

    correction = json.loads((task / "correction.json").read_text(encoding="utf-8"))
    transcript = json.loads((task / "transcript.json").read_text(encoding="utf-8"))
    assert correction["mode"] == "final" and correction["lines"]
    assert transcript["lines"]
    assert not (install.data / "runtime-state" / "migration-run").exists()


# -- rolling back -------------------------------------------------------------------------------------------------


def _bad_toml(install: OldInstall) -> None:
    _write(install.data / OLD_SETTINGS[1], "[nonoka_models\ncorrection = \n")


def test_a_failed_settings_step_rolls_everything_back(make_install):
    install = make_install(relocated=True)
    _bad_toml(install)
    before = install.snapshot()

    plan = migration.prepare(install.data, hooks=_settings_hooks())
    with pytest.raises(migration.MigrationRolledBack) as raised:
        migration.execute(plan, hooks=_settings_hooks())

    assert raised.value.step == deferred.SETTINGS
    assert _without(install.snapshot(), "runtime-state") == _without(before, "runtime-state")
    journal = migration.read_journal(install.data)
    assert journal["status"] == executing.ROLLED_BACK
    assert journal["error"]["step"] == deferred.SETTINGS and "not valid TOML" in journal["error"]["message"]
    # Planned afresh next time, and once the file is fixed it goes through.
    _write(install.data / OLD_SETTINGS[1], "[nonoka_models]\ncorrection = \"x\"\n")
    again = migration.prepare(install.data, hooks=_settings_hooks())
    assert again.state == planning.FRESH
    migration.execute(again, hooks=_settings_hooks())
    assert (install.data / "settings.json").is_file()


def _failing_conversion(monkeypatch) -> None:
    def fail(task, original, tools, log):
        raise RuntimeError("the correction package's process crashed")

    monkeypatch.setattr(task_filing, "_convert", fail)


def test_a_failed_task_step_rolls_back_settings_knowledge_and_tasks(make_install, monkeypatch):
    install = make_install(relocated=True)
    _finished_task(install)
    second = _finished_task(install, "f" * 32, with_document=False)
    before = install.snapshot()
    _failing_conversion(monkeypatch)

    plan = migration.prepare(install.data, hooks=_settings_hooks())
    with pytest.raises(migration.MigrationRolledBack, match="crashed"):
        migration.execute(plan, hooks=_settings_hooks())

    assert _without(install.snapshot(), "runtime-state") == _without(before, "runtime-state")
    assert (second / "workspace").is_dir() and not (second / "native").exists()
    steps = migration.read_journal(install.data)["steps"]
    assert {step: steps[step]["status"] for step in planning.REVERSIBLE} == dict.fromkeys(planning.REVERSIBLE, executing.UNDONE)
    assert planning.OLD_ENVIRONMENT not in steps


def test_a_migration_killed_anywhere_in_a_failing_run_ends_rolled_back(make_install, monkeypatch):
    reference = make_install(relocated=True)
    _finished_task(reference)
    with monkeypatch.context() as patch:
        _failing_conversion(patch)
        operations = _operations(patch, None)
        with pytest.raises(migration.MigrationRolledBack):
            migration.execute(migration.prepare(reference.data, hooks=_settings_hooks()), hooks=_settings_hooks())
    total = len(operations)
    assert total > 20

    for kill_at in range(1, total + 1):
        install = make_install(relocated=True)
        _finished_task(install)
        before = _without(install.snapshot(), "runtime-state")
        with monkeypatch.context() as patch:
            _failing_conversion(patch)
            _operations(patch, kill_at)
            with pytest.raises(Killed):
                migration.execute(migration.prepare(install.data, hooks=_settings_hooks()), hooks=_settings_hooks())
        with monkeypatch.context() as patch:
            _failing_conversion(patch)
            resumed = migration.prepare(install.data, hooks=_settings_hooks())
            if resumed.state != planning.FRESH or migration.read_journal(install.data) is None:
                with pytest.raises(migration.MigrationRolledBack):
                    migration.execute(resumed, hooks=_settings_hooks())
        assert _without(install.snapshot(), "runtime-state") == before, f"killed at {kill_at} ({operations[kill_at - 1]})"
        assert migration.read_journal(install.data)["status"] == executing.ROLLED_BACK


def test_past_the_old_environment_there_is_no_rollback(make_install, monkeypatch):
    install = make_install()
    original = executing._RUNNERS[planning.CACHES]
    failures = iter([True])

    def flaky(step, context):
        if next(failures, False):
            raise OSError("the scanner holds a file")
        return original(step, context)

    monkeypatch.setitem(executing._RUNNERS, planning.CACHES, flaky)
    with pytest.raises(migration.MigrationError) as raised:
        migration.execute(migration.prepare(install.data))
    assert not isinstance(raised.value, migration.MigrationRolledBack)
    assert not (install.new_root / "runtime").exists()  # the old environment is gone
    assert migration.read_journal(install.data)["status"] == executing.RUNNING

    resumed = migration.prepare(install.data)
    assert resumed.state == planning.RESUMING
    assert migration.execute(resumed).state == planning.COMPLETED


# -- in two halves, catching up ----------------------------------------------------------------------------------


class RecordingHook:
    def __init__(self, calls: list, name: str) -> None:
        self.calls, self.name = calls, name

    def download_bytes(self, context):
        return 10

    def run(self, context):
        if context.progress is not None:
            context.progress({"layer": self.name, "stage": "downloading", "message": "", "completed": 5, "total": 10})
        self.calls.append(self.name)


def _download_hooks(calls: list) -> dict:
    return {
        deferred.MODEL_COMPLETION: RecordingHook(calls, "models"),
        deferred.LAYER_DOWNLOAD: RecordingHook(calls, "layers"),
    }


def test_the_local_steps_and_the_downloads_run_apart(make_install):
    install = make_install()
    calls: list[str] = []
    hooks = _download_hooks(calls)
    plan = migration.execute(migration.prepare(install.data, hooks=hooks), hooks=hooks, stop_before=deferred.MODEL_COMPLETION)

    assert plan.state == planning.RESUMING and calls == []
    assert not (install.new_root / "runtime").exists()
    assert migration.read_journal(install.data)["status"] == executing.RUNNING

    resumed = migration.prepare(install.data, hooks=hooks)
    assert migration.execute(resumed, hooks=hooks).state == planning.COMPLETED
    assert calls == ["models", "layers"]


def test_steps_an_older_build_skipped_run_once_they_can(make_install):
    install = make_install()
    _migrate(install)  # no hooks: the settings step is skipped
    journal = migration.read_journal(install.data)
    assert journal["steps"][deferred.SETTINGS]["status"] == executing.SKIPPED
    # As WP-M1 left it: the knowledge base not moved, its step deferred to WP-M2 and skipped.
    os.replace(install.knowledge, install.data / "knowledge")
    step = next(item for item in journal["plan"]["steps"] if item["id"] == deferred.KNOWLEDGE)
    step.update(owner="WP-M2", actions=[{"kind": "pending", "path": str(install.data / "knowledge"), "target": None,
                                         "bytes": 0, "files": 0, "note": ""}])
    journal["steps"][deferred.KNOWLEDGE] = {"status": executing.SKIPPED, "note": "implemented by WP-M2"}
    executing._write(migration.journal_path(install.data), journal)

    assert migration.prepare(install.data).state == planning.RESUMING  # the knowledge step is the migration's own now
    plan = migration.prepare(install.data, hooks=_settings_hooks())
    assert plan.state == planning.RESUMING
    migration.execute(plan, hooks=_settings_hooks())

    assert (install.data / "settings.json").is_file()
    assert (install.knowledge / "subjects" / "entry.md").is_file() and not (install.data / "knowledge").exists()
    final = migration.read_journal(install.data)
    assert final["status"] == planning.COMPLETED
    assert final["steps"][deferred.SETTINGS]["status"] == executing.DONE
    assert migration.prepare(install.data, hooks=_settings_hooks()).state == planning.COMPLETED


# -- at start-up ------------------------------------------------------------------------------------------------


def _startup(install_or_data, **kwargs):
    from nonoka_x.migration.startup import StartupMigration

    data = install_or_data.data if isinstance(install_or_data, OldInstall) else install_or_data
    return StartupMigration(data, log=lambda message: None, **kwargs)


def test_a_new_install_is_marked_and_never_looked_at_again(tmp_path, monkeypatch):
    from nonoka_x.migration import startup

    data = tmp_path / "Nonoka X"
    first = _startup(data, hooks={})
    assert first.begin() == startup.READY
    assert first.status()["state"] == startup.NOT_NEEDED and first.data_open
    assert migration.read_journal(data)["status"] == planning.COMPLETED

    # Old files appearing later are not read: the mark says it ran once.
    _write(data / "knowledge" / "subjects" / "entry.md", "# 用語\n")
    monkeypatch.setattr(migration.legacy, "detect", lambda *a, **k: pytest.fail("looked for an old layout"))
    second = _startup(data, hooks={})
    assert second.begin() == startup.READY
    assert (data / "knowledge" / "subjects" / "entry.md").is_file()


def test_start_up_migrates_before_opening_and_downloads_after(make_install):
    from nonoka_x.migration import startup

    install = make_install(relocated=True)
    # The previous app was closed mid-task: nothing runs it now, so it blocks nothing.
    _write(install.data / "tasks" / "stale-task" / "snapshot.json", json.dumps({"state": "running"}))
    calls: list[str] = []
    run = _startup(install, install_dir=install.old_root, hooks={**_settings_hooks(), **_download_hooks(calls)})

    assert run.begin() == startup.GATED
    assert not run.data_open
    assert run.run_local() is True
    status = run.status()
    assert status["state"] == startup.DOWNLOADING and status["dataOpen"]
    assert {"settings", "tasks", "cleanup"} >= set(status.get("report", {})) and "settings" in status["report"]
    assert "segmentation" in status["report"]["settings"]["droppedTables"]
    assert calls == []
    # The desktop of this version still names the old root on its first start.
    assert run.install_dir() == install.new_root

    run.run_downloads()
    assert calls == ["models", "layers"]
    assert run.status()["state"] == startup.COMPLETED
    assert (install.data / "tasks" / "stale-task" / "snapshot.json").is_file()

    # Started again: migrated, nothing to do.
    again = _startup(install, hooks={})
    assert again.begin() == startup.READY and again.status()["state"] == startup.NOT_NEEDED


def test_start_up_after_a_restart_mid_download_opens_at_once(make_install):
    from nonoka_x.migration import startup

    install = make_install()
    calls: list[str] = []
    hooks = {**_settings_hooks(), **_download_hooks(calls)}
    assert _startup(install, hooks=hooks).run_local()

    restarted = _startup(install, hooks=hooks)
    assert restarted.begin() == startup.READY
    assert restarted.status()["state"] == startup.DOWNLOADING
    restarted.run_downloads()
    assert calls == ["models", "layers"]


def test_start_up_keeps_the_data_closed_when_blocked_or_rolled_back(make_install):
    from nonoka_x.migration import startup

    blocked = make_install(relocated=True)
    _write(blocked.new_root / "something.txt", "already here")
    run = _startup(blocked, hooks={})
    assert run.begin() == startup.GATED and run.run_local() is False
    assert run.status()["state"] == startup.BLOCKED and not run.data_open
    assert any("already exists" in reason for reason in run.status()["blockers"])

    failing = make_install()
    _bad_toml(failing)
    run = _startup(failing, hooks=_settings_hooks())
    assert run.run_local() is False
    status = run.status()
    assert status["state"] == startup.ROLLED_BACK and not status["dataOpen"]
    assert status["step"] == deferred.SETTINGS and "not valid TOML" in status["error"]
    assert failing.old_root.is_dir() and not failing.new_root.exists()

    # Fixed, and tried again.
    _write(failing.data / OLD_SETTINGS[1], "[nonoka_models]\n")
    opened: list[bool] = []
    run.retry(lambda: opened.append(True))
    run._local.join(timeout=60)
    assert opened == [True] and run.data_open


def test_the_sidecar_answers_only_the_migration_while_the_data_is_closed(make_install, monkeypatch):
    import threading
    import urllib.error
    import urllib.request

    from nonoka_x.migration import startup
    from nonoka_x.sidecar import SidecarServer

    install = make_install(relocated=True)
    _write(install.new_root / "something.txt", "already here")
    run = _startup(install, hooks={})
    run.run_local()
    server = SidecarServer(("127.0.0.1", 0), None, "token", run)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        def call(path: str, method: str = "GET"):
            request = urllib.request.Request(f"http://127.0.0.1:{server.server_port}{path}", method=method,
                                             headers={"Authorization": "Bearer token"}, data=b"{}" if method == "POST" else None)
            try:
                with urllib.request.urlopen(request, timeout=10) as response:
                    return response.status, json.loads(response.read())
            except urllib.error.HTTPError as error:
                return error.code, json.loads(error.read())

        # The test module refuses the network; the loopback server is the one exception here.
        connect, create_connection, getaddrinfo = _REAL_SOCKET
        monkeypatch.setattr(socket.socket, "connect", connect)
        monkeypatch.setattr(socket, "create_connection", create_connection)
        monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
        status, body = call("/v1/settings")
        assert status == 503 and body["error"]["code"] == "migrating"
        status, body = call("/v1/migration")
        assert status == 200 and body["state"] == startup.BLOCKED and body["dataOpen"] is False
    finally:
        server.shutdown()
        server.server_close()


# -- the leftovers, once everything succeeded (plan appendix B question 89) ---------------------------------------

#: What may stay in the main data directory after migration besides `engines\` (plan WP-M2, last criterion).
_NONOKA_OWN = {"preferences.json", "library.json", "documents", "bootstrap", "app-resources", "tasks", "runtime-state",
               "settings.json", "settings.json.lock", "secrets.json", "secrets.json.lock", "storage.json", "engines",
               "runtime"}


def _finesub_leftovers(data: Path) -> list[str]:
    """Anything in the main data directory that is not Nonoka's own (FineSub's lives under `engines\\` only)."""

    return [entry.name for entry in sorted(data.iterdir()) if entry.name not in _NONOKA_OWN]


def _with_unknown_finesub_data(install: OldInstall) -> None:
    _write(install.data / "finesub-data" / "mirror-probe.json", '{"unknown": true}\n')
    _write(install.data / "finesub-data" / "notes" / "a.txt", "an entry nobody registered\n")


def test_the_backups_and_finesub_data_go_once_past_the_point_of_rollback(make_install):
    from nonoka_x.config.migrate import read_report
    from nonoka_x.migration import startup

    install = make_install(relocated=True)
    _with_unknown_finesub_data(install)
    # A key Nonoka knows, protected under another Windows account: it cannot be opened here.
    _write(install.data / OLD_SETTINGS[0], "FINESUB_KEYRING=finesub$kek$v1$AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA\n"
                                           "GEMINI_PAID=fs$AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA\n")
    calls: list[str] = []
    run = _startup(install, install_dir=install.old_root, hooks={**_settings_hooks(), **_download_hooks(calls)})
    assert run.begin() == startup.GATED

    # The local steps are done: the old environment is gone, so are the backups -- the downloads are not awaited.
    assert run.run_local() is True
    status = run.status()
    assert status["state"] == startup.DOWNLOADING and calls == []
    assert _finesub_leftovers(install.data) == []
    removed = status["report"]["cleanup"]["removed"]
    assert sorted(removed) == sorted([
        OLD_SETTINGS[0] + ".pre-migration",
        OLD_SETTINGS[1] + ".pre-migration",
        "finesub-data\\",
        "finesub-data\\mirror-probe.json",
        "finesub-data\\notes",
    ])
    assert read_report(install.data)["backupsRemoved"] == sorted(name + ".pre-migration" for name in OLD_SETTINGS)
    # The fixture's key file was protected elsewhere: its key could not be carried and went with the backup,
    # which the report says by name (owner 2026-11-19, plan appendix B question 90).
    unreadable = read_report(install.data)["env"]["unreadable"]
    assert unreadable and status["report"]["cleanup"]["unreadableKeysRemoved"] == unreadable
    assert (install.data / "settings.json").is_file()

    run.run_downloads()
    assert calls == ["models", "layers"] and run.status()["state"] == startup.COMPLETED
    assert run.status()["report"]["cleanup"]["removed"] == removed


def test_without_downloads_the_cleanup_runs_with_the_local_steps(tmp_path):
    from nonoka_x.migration import startup

    data = tmp_path / "Nonoka X"
    # Settings migrated by an older build (WP-R4's automatic migration), nothing else of the old layout.
    _write(data / (OLD_SETTINGS[0] + ".pre-migration"), "OPENROUTER_API_KEY=x\n")
    _write(data / "finesub-data" / "download-routes.json", "{}")
    run = _startup(data, hooks=_settings_hooks())
    assert run.begin() == startup.GATED
    assert run.run_local() is True
    assert run.status()["state"] == startup.COMPLETED
    assert _finesub_leftovers(data) == []
    # `finesub-data` held only known files: the `rebuildable` step removed it, the cleanup only the backup.
    assert run.status()["report"]["cleanup"]["removed"] == [OLD_SETTINGS[0] + ".pre-migration"]


def test_a_skipped_settings_step_holds_the_cleanup_back_and_the_downloads_do_not(make_install):
    # No settings hook (a library caller): the settings step is skipped and would make its backups when it
    # catches up, so the cleanup waits for it.
    install = make_install()
    _with_unknown_finesub_data(install)
    migration.execute(migration.prepare(install.data))
    journal = migration.read_journal(install.data)
    assert journal["steps"][planning.LEFTOVERS]["status"] == executing.SKIPPED
    assert "waits for settings" in journal["steps"][planning.LEFTOVERS]["note"]
    assert (install.data / "finesub-data" / "mirror-probe.json").is_file()
    assert migration.prepare(install.data).state == planning.COMPLETED

    # With the settings hook it catches up, and the cleanup after it -- the downloads still skipped.
    plan = migration.prepare(install.data, hooks=_settings_hooks())
    assert plan.state == planning.RESUMING
    migration.execute(plan, hooks=_settings_hooks())
    journal = migration.read_journal(install.data)
    assert journal["steps"][planning.LEFTOVERS]["status"] == executing.DONE
    assert journal["steps"][deferred.LAYER_DOWNLOAD]["status"] == executing.SKIPPED
    assert _finesub_leftovers(install.data) == []

    # A failed download does not bring anything back.
    failed = make_install()

    class Failing:
        def download_bytes(self, context):
            return 10

        def run(self, context):
            raise RuntimeError("R2 unreachable")

    hooks = {**_settings_hooks(), deferred.MODEL_COMPLETION: Failing(), deferred.LAYER_DOWNLOAD: Failing()}
    with pytest.raises(executing.MigrationError):
        migration.execute(migration.prepare(failed.data, hooks=hooks), hooks=hooks)
    assert _finesub_leftovers(failed.data) == []


def test_a_rollback_still_has_the_backups(make_install, monkeypatch):
    install = make_install()
    _with_unknown_finesub_data(install)
    _finished_task(install)
    before = install.snapshot()
    _failing_conversion(monkeypatch)
    hooks = {**_settings_hooks(), **_download_hooks([])}
    with pytest.raises(executing.MigrationRolledBack):
        migration.execute(migration.prepare(install.data, hooks=hooks), hooks=hooks)
    assert _without(install.snapshot(), "runtime-state") == _without(before, "runtime-state")


def test_a_journal_without_the_cleanup_step_gets_it(make_install):
    install = make_install()
    hooks = {**_settings_hooks(), **_download_hooks([])}
    migration.execute(migration.prepare(install.data, hooks=hooks), hooks=hooks)
    assert _finesub_leftovers(install.data) == []

    # As stage 1 of WP-M2 left it: completed, no `leftovers` step, the backups and finesub-data still there.
    journal = migration.read_journal(install.data)
    journal["plan"]["steps"] = [item for item in journal["plan"]["steps"] if item["id"] != planning.LEFTOVERS]
    journal["steps"].pop(planning.LEFTOVERS)
    executing._write(migration.journal_path(install.data), journal)
    _write(install.data / (OLD_SETTINGS[0] + ".pre-migration"), "OPENROUTER_API_KEY=x\n")
    _write(install.data / "finesub-data" / "mirror-probe.json", "{}")

    plan = migration.prepare(install.data, hooks=hooks)
    assert plan.state == planning.RESUMING
    migration.execute(plan, hooks=hooks)
    assert _finesub_leftovers(install.data) == []
    final = migration.read_journal(install.data)
    assert final["status"] == planning.COMPLETED
    assert any(item["id"] == planning.LEFTOVERS for item in final["plan"]["steps"])
    assert migration.prepare(install.data, hooks=hooks).state == planning.COMPLETED


def test_the_cleanup_killed_at_any_operation_continues_and_reports_everything(make_install, monkeypatch):
    from nonoka_x.config import migrate as settings_migration

    def operations(patch, kill_at):
        seen = _operations(patch, kill_at)
        for name in ("replace_path", "write_atomic"):
            original = getattr(settings_migration, name)

            def wrapped(*args, _original=original, _name=f"settings.{name}", **kwargs):
                seen.append(_name)
                if kill_at is not None and len(seen) == kill_at:
                    raise Killed(_name)
                return _original(*args, **kwargs)

            patch.setattr(settings_migration, name, wrapped)
        unlink = Path.unlink

        def counted_unlink(self, *args, **kwargs):
            if self.name.endswith(".pre-migration"):
                seen.append("settings.unlink")
                if kill_at is not None and len(seen) == kill_at:
                    raise Killed("settings.unlink")
            return unlink(self, *args, **kwargs)

        patch.setattr(Path, "unlink", counted_unlink)
        return seen

    hooks = {**_settings_hooks(), **_download_hooks([])}
    reference = make_install()
    _with_unknown_finesub_data(reference)
    with monkeypatch.context() as patch:
        seen = operations(patch, None)
        migration.execute(migration.prepare(reference.data, hooks=hooks), hooks=hooks)
    expected = _without(reference.snapshot(), "runtime-state")
    expected_removed = sorted(migration.read_journal(reference.data)["steps"][planning.LEFTOVERS]["report"]["removed"])
    # From the journal write that starts the cleanup on: the operations before it are covered above.
    first_unlink = seen.index("settings.unlink")
    start = max(index for index in range(first_unlink) if seen[index] == "runner._write")
    total = len(seen)
    assert total - start >= 4

    for kill_at in range(start + 1, total + 1):
        install = make_install()
        _with_unknown_finesub_data(install)
        with monkeypatch.context() as patch:
            operations(patch, kill_at)
            with pytest.raises(Killed):
                migration.execute(migration.prepare(install.data, hooks=hooks), hooks=hooks)
        migration.execute(migration.prepare(install.data, hooks=hooks), hooks=hooks)
        assert _without(install.snapshot(), "runtime-state") == expected, f"killed at operation {kill_at} ({seen[kill_at - 1]})"
        journal = migration.read_journal(install.data)
        assert journal["status"] == planning.COMPLETED
        assert sorted(journal["steps"][planning.LEFTOVERS]["report"]["removed"]) == expected_removed, kill_at


# -- old cloud continuations (WP-CL retired `cloud_stable`) ------------------------------------------------------

_ATTACHMENT_KEY = __import__("re").compile(r"^[a-z0-9_-]{1,40}\.[a-z0-9_]{1,40}$")


def _cloud_continuation(install: OldInstall, task_id: str = "c" * 32, *, timeline: bool = True) -> tuple[Path, dict]:
    """A task an old desktop started to continue the cloud's recognition (`source.kind` = `cloud_stable`)."""

    task = _finished_task(install, task_id, with_document=False)
    relay = install.data / "cloud-relays" / "rly_0123456789abcdef01234567"
    _write(relay / "stable.json", (FIXTURES / "sample-stable.json").read_bytes())
    source = {
        "kind": "cloud_stable", "path": str(install.base / "media" / TITLE), "title": TITLE,
        "fingerprint": "f" * 64, "video_id": "loc_0123456789ab", "duration": 12.5,
        "stable_json": str(relay / "stable.json"),
    }
    if timeline:
        _write(task / "workspace" / f"{TITLE[:-4]}-diar.json", b'{"turns": []}')
        _write(relay / "diar.json", b'{"turns": []}')
        source["diar_json"] = str(relay / "diar.json")
    request = {"schema": 1, "provider": "local", "language": "ja", "target": "final-srt", "source": source}
    _write(task / "request.json", json.dumps(request, ensure_ascii=False))
    return task, request


def test_an_old_cloud_continuation_request_is_rewritten_to_the_transcript_shape(make_install, monkeypatch):
    install = make_install()
    task, before = _cloud_continuation(install)
    FakeConversions(monkeypatch)
    _migrate(install)

    request = json.loads((task / "request.json").read_text(encoding="utf-8"))
    source = request["source"]
    assert source["kind"] == "cloud_transcript"
    assert "stable_json" not in source and "diar_json" not in source
    # Everything else as it was.
    assert {key: value for key, value in source.items() if key not in ("kind", "transcript_json", "native", "native_package")} == {
        key: value for key, value in before["source"].items() if key not in ("kind", "stable_json", "diar_json")
    }
    assert {key: value for key, value in request.items() if key != "source"} == {key: value for key, value in before.items() if key != "source"}

    # The source lives in its own directory of the task: a retry copies it into the task, never onto itself.
    cloud_source = task / "migration" / "cloud-source"
    assert Path(source["transcript_json"]) == cloud_source / "transcript.json"
    assert (cloud_source / "transcript.json").read_bytes() == (task / "transcript.json").read_bytes()
    transcribe = task / "native" / "finesub-transcribe"
    assert source["native_package"] == "finesub-transcribe"
    assert source["native"] == {
        "finesub.stable_json": str(cloud_source / "native" / "finesub.stable_json"),
        "finesub.diar_json": str(cloud_source / "native" / "finesub.diar_json"),
    }
    assert all(_ATTACHMENT_KEY.match(key) for key in source["native"])
    assert Path(source["native"]["finesub.stable_json"]).read_bytes() == (transcribe / f"{TITLE[:-4]}-stable.json").read_bytes()
    assert Path(source["native"]["finesub.diar_json"]).read_bytes() == (transcribe / f"{TITLE[:-4]}-diar.json").read_bytes()
    for path in (source["transcript_json"], *source["native"].values()):
        assert Path(path).parent != task and transcribe not in Path(path).parents

    report = migration.read_journal(install.data)["steps"][deferred.TASK_PRODUCTS]["report"]
    assert report["requests"] == 1 and "not_converted" not in report


def test_a_continuation_without_its_speaker_timeline_carries_only_the_stable(make_install, monkeypatch):
    install = make_install()
    task, _ = _cloud_continuation(install, timeline=False)
    FakeConversions(monkeypatch)
    _migrate(install)
    source = json.loads((task / "request.json").read_text(encoding="utf-8"))["source"]
    assert list(source["native"]) == ["finesub.stable_json"]


def test_a_continuation_without_a_transcript_keeps_its_request_and_is_reported(make_install, monkeypatch):
    install = make_install()
    task, before = _cloud_continuation(install)
    FakeConversions(monkeypatch, refuse="transcript_json")
    _migrate(install)
    assert json.loads((task / "request.json").read_text(encoding="utf-8")) == before
    assert not (task / "migration").exists()
    report = migration.read_journal(install.data)["steps"][deferred.TASK_PRODUCTS]["report"]
    assert "request_json" in report["not_converted"][task.name]
    assert "requests" not in report


def test_a_rollback_puts_the_old_request_back(make_install, monkeypatch):
    install = make_install()
    _cloud_continuation(install)
    FakeConversions(monkeypatch)
    before = install.snapshot()
    original = task_filing.file_tasks

    def then_fail(actions, context):
        original(actions, context)
        raise RuntimeError("the step failed after the tasks were filed")

    monkeypatch.setattr(task_filing, "file_tasks", then_fail)
    with pytest.raises(migration.MigrationRolledBack):
        migration.execute(migration.prepare(install.data, hooks=_settings_hooks()), hooks=_settings_hooks())
    assert _without(install.snapshot(), "runtime-state") == _without(before, "runtime-state")
