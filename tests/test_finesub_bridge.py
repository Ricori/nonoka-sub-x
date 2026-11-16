"""WP-R5: what FineSub is handed, derived per process from Nonoka's settings (`nonoka_x.finesub_bridge`).

Every key here is a fake. The assertions that matter most: keys reach the
engine as process variables and never as a file (D3); each half derives only
what it needs; the derived `finesub.toml` and `model_catalog.toml` are the
texts the settings page used to write (the quality gate's environment
baseline holds them byte for byte); a run directory lives exactly as long as
its process; and an old install, migrated, hands the engine what it read
before, for every setting Nonoka knows.
"""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tomllib

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "third_party/finesub/src"))

from nonoka_x import finesub_bridge as bridge  # noqa: E402
from nonoka_x.runtime import layer_manifest  # noqa: E402
from nonoka_x.settings import CORRECTION_SECRET_NAMES, KEY_SPECS, TRANSCRIPTION_SECRET_NAMES, FineSubSettings  # noqa: E402

FILE_VARIABLES = ("FINESUB_CONFIG_FILE", "FINESUB_MODEL_CATALOG", "FINESUB_ENV_FILE")


@pytest.fixture(autouse=True)
def _engine_caches():
    """The engine caches its config, catalog and routes per process; each test starts and ends clean."""

    from finesub.config import clear_config_cache
    from finesub.llm.routing.model_catalog import default_model_catalog
    from finesub.llm.routing.model_routes import default_model_routes

    resets = (clear_config_cache, default_model_catalog.cache_clear, default_model_routes.cache_clear)
    for reset in resets:
        reset()
    yield
    for reset in resets:
        reset()


def _transcription(tmp_path: Path, **overrides) -> dict:
    context = {
        "schema": 1,
        "package_id": bridge.TRANSCRIPTION_PACKAGE,
        "paths": {"run": str(tmp_path / "t-run"), "state_file": str(tmp_path / "state")},
        "runtime": {"models": str(tmp_path / "models"), "cache": str(tmp_path / "cache" / bridge.TRANSCRIPTION_PACKAGE)},
        "secrets": {},
    }
    context.update(overrides)
    return context


def _correction(tmp_path: Path, *, llm: dict | None = None, secrets: dict | None = None) -> dict:
    return {
        "schema": 1,
        "package_id": bridge.CORRECTION_PACKAGE,
        "paths": {
            "run": str(tmp_path / "c-run"),
            "state_file": str(tmp_path / "state"),
            "knowledge_root": str(tmp_path / "knowledge"),
        },
        "secrets": secrets or {},
        "llm": llm or {},
    }


# -- the two halves -----------------------------------------------------------------------------------------------


def test_the_transcription_half_gets_the_model_layer_and_writes_nothing(tmp_path):
    environment = bridge.transcription_environment(_transcription(tmp_path, secrets={"HF_TOKEN": "hf_fake-aaaa"}))

    models = tmp_path / "models"
    assert environment["FINESUB_MODEL_DIR"] == str(models)
    assert environment["HF_HOME"] == str(models / layer_manifest.HF_HOME_DIR)
    assert environment["TORCH_HOME"] == str(models / "torch")
    assert environment["FINESUB_SEPARATOR_ACCEL"] == environment["FINESUB_REFEREE_ACCEL"] == "0"
    assert environment["NUMBA_CACHE_DIR"] == str(tmp_path / "cache" / bridge.TRANSCRIPTION_PACKAGE / "numba")
    assert environment["HF_TOKEN"] == "hf_fake-aaaa"
    assert environment["FINESUB_CHECKOUT_DATA"] == "0" and environment["FINESUB_ENV_PROTECT"] == "0"
    for name in FILE_VARIABLES:
        assert Path(environment[name]).parent == tmp_path / "t-run"
    assert not (tmp_path / "t-run").exists()
    # Correction's own variables are not its business.
    assert "FINESUB_KNOWLEDGE_ROOT" not in environment


def test_the_transcription_half_has_no_token_unless_one_was_set(tmp_path):
    environment = bridge.transcription_environment(_transcription(tmp_path, secrets={"HF_TOKEN": ""}))
    assert "HF_TOKEN" not in environment


def test_the_correction_half_writes_its_files_and_keeps_keys_off_disk(tmp_path):
    llm = {
        "endpoints": [{
            "provider": "openai-compat",
            "model": "fake-model",
            "row": {"provider_tier": "NONOKA_OPENAI_COMPAT", "base_url": "http://127.0.0.1:9/v1", "key_env": "OPENAI_COMPAT_API_KEY"},
        }],
        "routes": {"default": {"provider": "openai-compat", "model": "fake-model"}, "correction": {"provider": "", "model": ""}},
        "pins": [{"group": "default", "provider": "openai-compat", "model": "fake-model", "kind": "http"}],
        "gemini_base_url": "https://gemini.example.test/v1beta",
    }
    keys = {"OPENAI_COMPAT_API_KEY": "label:fake-compat-bbbb", "EXA_KEYS": "fake-exa-cccc"}
    environment = bridge.correction_environment(_correction(tmp_path, llm=llm, secrets=keys))

    run = tmp_path / "c-run"
    target = bridge.custom_target("openai-compat", "fake-model")
    assert (run / "model_catalog.toml").read_text(encoding="utf-8") == (
        bridge.CATALOG_HEADER
        + f'\n[models."{target}"]\nprovider_tier = "NONOKA_OPENAI_COMPAT"\nbase_url = "http://127.0.0.1:9/v1"\n'
        'key_env = "OPENAI_COMPAT_API_KEY"\n'
    )
    assert (run / "finesub.toml").read_text(encoding="utf-8") == (
        '[nonoka_models]\ndefault_provider = "openai-compat"\ndefault_model = "fake-model"\n'
        f'\n[llm.preferred_targets]\ndefault = "{target}"\n'
    )
    assert (run / ".env").read_text(encoding="utf-8") == "GEMINI_BASE_URL=https://gemini.example.test/v1beta\n"
    assert {name: environment[name] for name in keys} == keys
    for path in run.iterdir():
        text = path.read_text(encoding="utf-8")
        assert "fake-compat" not in text and "fake-exa" not in text
    assert environment["FINESUB_ENV_PROTECT"] == "0"
    assert environment["FINESUB_KNOWLEDGE_ROOT"] == str(tmp_path / "knowledge")
    assert environment["FINESUB_STATE_FILE"] == str(tmp_path / "state")
    for name in FILE_VARIABLES:
        assert Path(environment[name]).parent == run
    # The transcription half's own variables are not its business.
    assert "FINESUB_MODEL_DIR" not in environment and "NUMBA_CACHE_DIR" not in environment


def test_nothing_configured_leaves_only_the_catalog_header(tmp_path):
    environment = bridge.correction_environment(_correction(tmp_path))
    run = tmp_path / "c-run"
    assert sorted(path.name for path in run.iterdir()) == ["model_catalog.toml"]
    assert (run / "model_catalog.toml").read_text(encoding="utf-8") == bridge.CATALOG_HEADER
    assert not Path(environment["FINESUB_ENV_FILE"]).exists()


def test_a_pin_on_an_endpoint_without_a_row_falls_back(tmp_path):
    llm = {"routes": {}, "pins": [{"group": "default", "provider": "openai-compat", "model": "gone", "kind": "http"}]}
    bridge.correction_environment(_correction(tmp_path, llm=llm))
    assert not (tmp_path / "c-run" / "finesub.toml").exists()


def test_a_local_agent_pin_resolves_only_the_offered_models(tmp_path):
    from nonoka_x.settings import LOCAL_AGENT_PROVIDERS

    spec = LOCAL_AGENT_PROVIDERS["local-codex"]
    model = spec["models"][0]
    targets = dict((fact.api_model_id, target) for fact, target in bridge.local_agent_targets(spec["tier"]))
    pin = {"group": "default", "provider": "local-codex", "model": model, "kind": "local_agent", "tier": spec["tier"],
           "offered": list(spec["models"])}
    assert bridge._pin_target(pin, set()) == targets[model]
    assert bridge._pin_target({**pin, "offered": []}, set()) is None


def test_one_process_with_both_halves_reads_the_correction_files(tmp_path):
    environment = bridge.combined_environment(_transcription(tmp_path), _correction(tmp_path))
    for name in FILE_VARIABLES:
        assert Path(environment[name]).parent == tmp_path / "c-run"
    assert environment["FINESUB_MODEL_DIR"] == str(tmp_path / "models")
    assert environment["FINESUB_KNOWLEDGE_ROOT"] == str(tmp_path / "knowledge")
    assert not (tmp_path / "t-run").exists()


def test_inherited_finesub_variables_do_not_reach_an_engine():
    inherited = {"FINESUB_SEPARATOR_ACCEL": "1", "finesub_task_strategy": "x", "PATH": "p", "FINESUBX": "kept"}
    assert bridge.without_inherited(inherited) == {"PATH": "p", "FINESUBX": "kept"}


def test_the_secret_names_split_between_the_halves():
    assert TRANSCRIPTION_SECRET_NAMES == ("HF_TOKEN",)
    assert set(CORRECTION_SECRET_NAMES) == {spec["name"] for spec in KEY_SPECS} - {"HF_TOKEN"} | {"NONOKA_CLOUD_KEY"}


# -- run directories ----------------------------------------------------------------------------------------------


def test_a_run_directory_lives_as_long_as_its_process(tmp_path):
    package = tmp_path / "engines" / bridge.CORRECTION_PACKAGE
    with bridge.EngineRun.claim(package) as run:
        assert run.directory.parent == package / "run"
        assert run.directory.name.startswith(f"{os.getpid()}-")
        (run.directory / "finesub.toml").write_text("x", encoding="utf-8")
        # Live: a sweep leaves it alone.
        assert bridge.sweep_runs([package]) == []
        assert run.directory.is_dir()
    assert not run.directory.exists()


def test_the_sweep_removes_what_a_dead_owner_left(tmp_path):
    package = tmp_path / "engines" / bridge.CORRECTION_PACKAGE
    unlocked = package / "run" / "111-aaaaaaaaaaaa"
    unlocked.mkdir(parents=True)
    (unlocked / bridge.OWNER_LOCK_NAME).write_bytes(b"")
    (unlocked / "finesub.toml").write_text("x", encoding="utf-8")
    never_locked = package / "run" / "222-bbbbbbbbbbbb"
    never_locked.mkdir()
    # Held by another process that is still running.
    held = package / "run" / "333-cccccccccccc"
    held.mkdir()
    holder = subprocess.Popen(
        [sys.executable, "-c",
         "import sys, time; sys.path.insert(0, sys.argv[1]);"
         "from pathlib import Path; from nonoka_x.runtime import fsops;"
         "lock = fsops.holding_lock(Path(sys.argv[2]), timeout=5); lock.__enter__(); print('held', flush=True); time.sleep(60)",
         str(ROOT / "src"), str(held / bridge.OWNER_LOCK_NAME)],
        stdout=subprocess.PIPE, text=True,
    )
    try:
        assert holder.stdout.readline().strip() == "held"
        removed = bridge.sweep_runs([package, tmp_path / "engines" / "missing"])
        assert sorted(removed) == sorted([unlocked, never_locked])
        assert held.is_dir()
    finally:
        holder.kill()
        holder.wait()
    assert bridge.sweep_runs([package]) == [held]


def test_the_sidecar_reads_only_the_packaged_defaults(tmp_path, monkeypatch):
    # Recorded as set, so the variables `bind_host_process` writes are undone afterwards.
    for name in (*FILE_VARIABLES, "FINESUB_CHECKOUT_DATA", "FINESUB_ENV_PROTECT"):
        monkeypatch.setenv(name, "")
        monkeypatch.delenv(name)
    engines = tmp_path / "engines"
    transitional = engines / bridge.TRANSITIONAL_DIRECTORY
    transitional.mkdir(parents=True)
    (transitional / "finesub.env").write_text("FINESUB_KEYRING=sealed\n", encoding="utf-8")

    bridge.bind_host_process(engines)

    host = engines / bridge.CORRECTION_PACKAGE / "run" / bridge.HOST_RUN_NAME
    assert [os.environ[name] for name in FILE_VARIABLES] == [str(host / "finesub.toml"), str(host / "model_catalog.toml"), str(host / ".env")]
    assert os.environ["FINESUB_CHECKOUT_DATA"] == "0"
    assert not host.exists()
    assert not transitional.exists()
    from finesub.paths import resolve_config_file, resolve_env_file, resolve_model_catalog_override

    assert resolve_config_file() is None and resolve_env_file() is None and resolve_model_catalog_override() is None


# -- an old install, before and after the migration ----------------------------------------------------------------


def _engine_reads(monkeypatch, environment: dict[str, str], names) -> dict[str, list[tuple[str, str]]]:
    """What FineSub's own key reader (`api_keys.key_entries`) gives for each name under `environment`."""

    from finesub import api_keys

    for name in [*os.environ, *environment]:
        if name.upper().startswith("FINESUB_") or name in names or name in environment:
            monkeypatch.setenv(name, "")
            monkeypatch.delenv(name)
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    api_keys._DOTENV_CACHE.clear()
    dotenv = api_keys.read_dotenv()
    return {name: [(entry.name, entry.key) for entry in api_keys.key_entries(name, dotenv)] for name in names}


def test_the_engine_reads_the_same_known_settings_after_the_migration(tmp_path, monkeypatch):
    """Plan WP-R5: the same old configuration means the same to the engine before and after, for every key Nonoka knows."""

    from finesub.llm import llm_runtime
    from finesub_bootstrap import secrets as finesub_secrets

    from nonoka_x.local_provider import LocalProvider
    from tests import test_config_migrate as old

    monkeypatch.delenv("FINESUB_ENV_PROTECT", raising=False)  # the old key file sealed, as a real one is
    data = old._old_install(tmp_path / "Nonoka X")
    known = [*CORRECTION_SECRET_NAMES, *TRANSCRIPTION_SECRET_NAMES]
    # Before: the engine pointed at the old key file, as the old sidecar did. The config
    # file is left out: its `[pools]` table is one Nonoka does not know (dropped, and
    # reported, by the migration).
    before_environment = {
        "FINESUB_ENV_FILE": str(data / old.OLD_ENV), "FINESUB_CONFIG_FILE": str(tmp_path / "none.toml"),
        "FINESUB_CHECKOUT_DATA": "0", "FINESUB_ENV_PROTECT": "0",
    }
    before = _engine_reads(monkeypatch, before_environment, known)
    before_gemini = llm_runtime.gemini_api_base(finesub_secrets.read_env_file(data / old.OLD_ENV))
    before_models = tomllib.loads((data / old.OLD_TOML).read_text(encoding="utf-8"))["nonoka_models"]

    settings = FineSubSettings(data)
    settings.bind_environment()  # migrates the old files into the new stores
    settings.refresh_model_routes()
    provider = LocalProvider(data / "tasks", ROOT / "third_party/finesub", issues=[], settings=settings)
    try:
        environment, run = provider._engine_launch("task")
        try:
            after = _engine_reads(monkeypatch, {name: value for name, value in environment.items()
                                               if name.startswith("FINESUB_") or name in known}, known)
            from finesub import api_keys

            after_gemini = llm_runtime.gemini_api_base(api_keys.read_dotenv())
            after_models = tomllib.loads(Path(environment["FINESUB_CONFIG_FILE"]).read_text(encoding="utf-8"))["nonoka_models"]
        finally:
            run.close()
    finally:
        provider.shutdown()

    assert after == before
    assert any(after.values())  # the fixture's keys were really read, not two empty answers
    assert after_gemini == before_gemini == "https://gemini.example.test/v1beta"
    # The routes table: the known keys, after the same route migration the old sidecar ran at start-up.
    from nonoka_x.settings import migrate_model_config

    known_routes = {f"{route}_{field}" for route in ("default", "correction", "research", "knowledge") for field in ("provider", "model")}
    expected = {key: value for key, value in {**before_models, **migrate_model_config(before_models)}.items()
                if key in known_routes and value}
    assert after_models == expected
