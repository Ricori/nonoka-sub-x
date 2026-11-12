"""`nonoka_x.migration`: the old data layout moves to the new one (WP-M1).

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
        self.local_finesub = base / "Local" / "FineSub"
        self.system_python = base / "Envir" / "Python312"
        self.task = self.data / "tasks" / "17e7299434ac40deb1b9600deffb0d58"
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
    assert models.actions[0].target == str(install.migrated)
    deleted = {Path(action.path).relative_to(install.old_root / "models").as_posix() for action in models.actions[1:]}
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
        )

    after = install.snapshot()
    assert {key: value for key, value in after.items() if outside(key)} == {
        key: value for key, value in before.items() if outside(key)
    }

    journal = migration.read_journal(install.data)
    assert journal["status"] == planning.COMPLETED
    statuses = {step_id: record["status"] for step_id, record in journal["steps"].items()}
    for step_id in (planning.RUNTIME_ROOT, planning.MODELS, planning.OLD_ENVIRONMENT, planning.CACHES, planning.REBUILDABLE):
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
        install.data / "state",
        install.data / "knowledge" / "subjects" / "entry.md",
        install.task / "workspace" / "clip.mp4-stable.json",
        install.data / "documents" / "doc-1.json",
    ]
    digests = {path: install.digest(path) for path in keep}

    _migrate(install)

    assert {path: install.digest(path) for path in keep} == digests


def test_unrecognised_root_entries_move_along_and_stay(make_install):
    install = make_install()
    _write(install.old_root / "agent-capsules" / "mine.txt", "user's")
    plan = migration.prepare(install.data)
    kept = [action for action in plan.step(planning.RUNTIME_ROOT).actions if action.kind == planning.KEEP]
    assert [Path(action.path).name for action in kept] == ["agent-capsules"]

    migration.execute(plan)

    assert (install.new_root / "agent-capsules" / "mine.txt").read_text(encoding="utf-8") == "user's"


def test_a_checkout_clears_its_vendor_cache(tmp_path, capsys):
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


def test_a_finished_migration_is_not_repeated(make_install, capsys):
    install = make_install()
    _migrate(install)
    after = install.snapshot()

    plan = migration.prepare(install.data)
    assert plan.state == planning.COMPLETED
    assert migration.execute(plan).state == planning.COMPLETED
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
    seen: list[str] = []
    for name in ("replace_path", "remove_tree", "_write"):
        original = getattr(executing, name)

        def wrapped(*args, _original=original, _name=name, **kwargs):
            seen.append(_name)
            if kill_at is not None and len(seen) == kill_at:
                raise Killed(_name)
            return _original(*args, **kwargs)

        monkeypatch.setattr(executing, name, wrapped)
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

