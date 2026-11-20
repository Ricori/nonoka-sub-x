"""What FineSub is handed, derived in each engine package's process from its launch context (WP-R5, WP-P1).

The derivation moved from the core (`nonoka_x.finesub_bridge`) into the
packages: each package's `__main__.apply_context`, built on its own copy of
`bridge`. Every key here is a fake. The assertions that matter most: keys
reach the engine as process variables and never as a file (D3); each half
derives what it reads; the derived `finesub.toml` and `model_catalog.toml` are
the texts the settings page used to write (the quality gate's environment
baseline holds them byte for byte); a run directory lives exactly as long as
its process; and an old install, migrated, hands the engine what it read
before, for every setting Nonoka knows.

The contexts are applied in a child interpreter with nothing on its path but
the package's own roots, as the package's process applies them.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import textwrap
import tomllib

import pytest

ROOT = Path(__file__).resolve().parents[1]

from finesub_correct import bridge  # noqa: E402

from nonoka_x import engine_host  # noqa: E402
from nonoka_x.runtime import layer_manifest  # noqa: E402
from nonoka_x.settings import CORRECTION_SECRET_NAMES, KEY_SPECS, TRANSCRIPTION_SECRET_NAMES, FineSubSettings  # noqa: E402

PACKAGES = engine_host.discover(ROOT / "engines")
TRANSCRIBE, CORRECT = PACKAGES["finesub-transcribe"], PACKAGES["finesub-correct"]
FILE_VARIABLES = ("FINESUB_CONFIG_FILE", "FINESUB_MODEL_CATALOG", "FINESUB_ENV_FILE")

#: Applies a launch context the way the package's process does, then says what it has.
_APPLY = textwrap.dedent(
    """
    import importlib, json, os, sys
    from pathlib import Path

    entry, context, reads = sys.argv[1], json.loads(sys.argv[2]), json.loads(sys.argv[3])
    importlib.import_module(entry + ".__main__").apply_context(context, None)
    out = {"environment": dict(os.environ), "files": {}}
    run = Path((context.get("paths") or {}).get("run") or ".")
    if run.is_dir():
        out["files"] = {path.name: path.read_text(encoding="utf-8") for path in sorted(run.iterdir()) if path.name != "owner.lock"}
    if reads:
        try:
            from finesub import api_keys
        except ImportError:
            # The transcription package reads no key through FineSub's key layer (it
            # does not carry it): what it has is its environment, which the Hugging
            # Face libraries read `HF_TOKEN` from.
            out["keys"] = {name: [[name, os.environ[name]]] if os.environ.get(name) else [] for name in reads}
        else:
            from finesub.llm import llm_runtime

            dotenv = api_keys.read_dotenv()
            out["keys"] = {name: [[entry.name, entry.key] for entry in api_keys.key_entries(name, dotenv)] for name in reads}
            out["gemini"] = llm_runtime.gemini_api_base(dotenv)
    print(json.dumps(out, ensure_ascii=False))
    """
)


def apply(package, context: dict, *, inherited: dict | None = None, reads=()) -> dict:
    environment = {
        name: value for name, value in os.environ.items()
        if not name.upper().startswith("FINESUB_") and name.upper() not in {"PYTHONPATH"}
    }
    environment.update(inherited or {})
    environment["PYTHONPATH"] = os.pathsep.join(str(path) for path in package.paths)
    environment["PYTHONIOENCODING"] = "utf-8"
    completed = subprocess.run(
        [sys.executable, "-c", _APPLY, package.entry, json.dumps(context, ensure_ascii=False), json.dumps(list(reads))],
        capture_output=True, text=True, encoding="utf-8", env=environment, timeout=120,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout.strip().splitlines()[-1])


def _transcription(tmp_path: Path, **overrides) -> dict:
    context = {
        "schema": 1,
        "package_id": TRANSCRIBE.id,
        "paths": {"run": str(tmp_path / "t-run"), "state_file": str(tmp_path / "state"),
                  "knowledge_root": str(tmp_path / "knowledge")},
        "runtime": {"models": str(tmp_path / "models"), "cache": str(tmp_path / "cache" / TRANSCRIBE.id)},
        "secrets": {},
        "llm": {},
    }
    context.update(overrides)
    return context


def _correction(tmp_path: Path, *, llm: dict | None = None, secrets: dict | None = None) -> dict:
    return {
        "schema": 1,
        "package_id": CORRECT.id,
        "paths": {
            "run": str(tmp_path / "c-run"),
            "state_file": str(tmp_path / "state"),
            "knowledge_root": str(tmp_path / "knowledge"),
        },
        "secrets": secrets or {},
        "llm": llm or {},
    }


# -- the two halves -----------------------------------------------------------------------------------------------


def test_the_transcription_half_gets_the_model_layer(tmp_path):
    seen = apply(TRANSCRIBE, _transcription(tmp_path, secrets={"HF_TOKEN": "hf_fake-aaaa"}))
    environment = seen["environment"]
    models = tmp_path / "models"
    assert environment["FINESUB_MODEL_DIR"] == str(models)
    assert environment["HF_HOME"] == str(models / layer_manifest.HF_HOME_DIR)
    assert environment["TORCH_HOME"] == str(models / "torch")
    assert environment["FINESUB_SEPARATOR_ACCEL"] == environment["FINESUB_REFEREE_ACCEL"] == "0"
    assert environment["NUMBA_CACHE_DIR"] == str(tmp_path / "cache" / TRANSCRIBE.id / "numba")
    assert environment["HF_TOKEN"] == "hf_fake-aaaa"
    assert environment["FINESUB_CHECKOUT_DATA"] == "0" and environment["FINESUB_ENV_PROTECT"] == "0"
    for name in FILE_VARIABLES:
        assert Path(environment[name]).parent == tmp_path / "t-run"
    # Its preparation reads the route presets and the knowledge base's location,
    # as it did in the worker that ran both halves; nothing more of the correction's.
    assert sorted(seen["files"]) == ["model_catalog.toml"]
    assert environment["FINESUB_KNOWLEDGE_ROOT"] == str(tmp_path / "knowledge")
    assert "OPENAI_COMPAT_API_KEY" not in environment


def test_the_transcription_half_has_no_token_unless_one_was_set(tmp_path):
    environment = apply(TRANSCRIBE, _transcription(tmp_path, secrets={"HF_TOKEN": ""}))["environment"]
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
    seen = apply(CORRECT, _correction(tmp_path, llm=llm, secrets=keys))
    environment, files = seen["environment"], seen["files"]

    run = tmp_path / "c-run"
    target = bridge.custom_target("openai-compat", "fake-model")
    assert files["model_catalog.toml"] == (
        bridge.CATALOG_HEADER
        + f'\n[models."{target}"]\nprovider_tier = "NONOKA_OPENAI_COMPAT"\nbase_url = "http://127.0.0.1:9/v1"\n'
        'key_env = "OPENAI_COMPAT_API_KEY"\n'
    )
    assert files["finesub.toml"] == (
        '[nonoka_models]\ndefault_provider = "openai-compat"\ndefault_model = "fake-model"\n'
        f'\n[llm.preferred_targets]\ndefault = "{target}"\n'
    )
    assert files[".env"] == "GEMINI_BASE_URL=https://gemini.example.test/v1beta\n"
    assert {name: environment[name] for name in keys} == keys
    for text in files.values():
        assert "fake-compat" not in text and "fake-exa" not in text
    assert environment["FINESUB_ENV_PROTECT"] == "0"
    assert environment["FINESUB_KNOWLEDGE_ROOT"] == str(tmp_path / "knowledge")
    assert environment["FINESUB_STATE_FILE"] == str(tmp_path / "state")
    for name in FILE_VARIABLES:
        assert Path(environment[name]).parent == run
    # The transcription half's own variables are not its business.
    assert "FINESUB_MODEL_DIR" not in environment and "NUMBA_CACHE_DIR" not in environment


def test_nothing_configured_leaves_only_the_catalog_header(tmp_path):
    seen = apply(CORRECT, _correction(tmp_path))
    assert sorted(seen["files"]) == ["model_catalog.toml"]
    assert seen["files"]["model_catalog.toml"] == bridge.CATALOG_HEADER


def test_a_pin_on_an_endpoint_without_a_row_falls_back(tmp_path):
    llm = {"routes": {}, "pins": [{"group": "default", "provider": "openai-compat", "model": "gone", "kind": "http"}]}
    assert "finesub.toml" not in apply(CORRECT, _correction(tmp_path, llm=llm))["files"]


def test_a_local_agent_pin_resolves_only_the_offered_models():
    from nonoka_x.settings import LOCAL_AGENT_OVERLAY_MODELS, LOCAL_AGENT_PROVIDERS

    spec = LOCAL_AGENT_PROVIDERS["local-codex"]
    targets = dict((fact.api_model_id, target) for fact, target in bridge.local_agent_targets(spec["tier"]))
    packaged_model = next(model for model in spec["models"] if model in targets)
    pin = {"group": "default", "provider": "local-codex", "model": packaged_model, "kind": "local_agent",
           "tier": spec["tier"], "offered": list(spec["models"])}
    assert bridge._pin_target(pin, set()) == targets[packaged_model]
    assert bridge._pin_target({**pin, "offered": []}, set()) is None

    overlay_model, (overlay_target, _row) = next(iter(LOCAL_AGENT_OVERLAY_MODELS["local-codex"].items()))
    overlay_pin = {
        "group": "default", "provider": "local-codex", "model": overlay_model, "kind": "local_agent",
        "tier": spec["tier"], "offered": list(spec["models"]), "target": overlay_target,
    }
    assert bridge._pin_target(overlay_pin, {overlay_target}) == overlay_target
    assert bridge._pin_target(overlay_pin, set()) is None
    assert bridge._pin_target({**overlay_pin, "offered": []}, {overlay_target}) is None

    from nonoka_x.settings import GEMINI_OVERLAY_MODELS

    gemini_model, (gemini_target, _row) = next(iter(GEMINI_OVERLAY_MODELS["gemini-paid"].items()))
    gemini_pin = {
        "group": "default", "provider": "gemini-paid", "model": gemini_model, "kind": "gemini",
        "tier": "GEMINI_PAID", "target": gemini_target,
    }
    assert bridge._pin_target(gemini_pin, {gemini_target}) == gemini_target
    assert bridge._pin_target(gemini_pin, set()) is None


def test_inherited_finesub_variables_do_not_reach_an_engine(tmp_path):
    inherited = {"FINESUB_SEPARATOR_ACCEL": "1", "FINESUB_TASK_STRATEGY": "x", "FINESUBX": "kept"}
    environment = apply(CORRECT, _correction(tmp_path), inherited=inherited)["environment"]
    assert "FINESUB_TASK_STRATEGY" not in environment and "FINESUB_SEPARATOR_ACCEL" not in environment
    assert environment["FINESUBX"] == "kept"


def test_the_task_knowledge_process_is_reached_through_the_context(tmp_path):
    endpoint = {"address": r"\\.\pipe\x", "family": "AF_PIPE", "authkey": "00", "pid": 1}
    context = {**_correction(tmp_path), "knowledge_endpoint": endpoint}
    environment = apply(CORRECT, context)["environment"]
    assert json.loads(environment["FINESUB_KNOWLEDGE_ENDPOINT"]) == endpoint


def test_the_secret_names_split_between_the_halves():
    assert TRANSCRIPTION_SECRET_NAMES == ("HF_TOKEN",) == TRANSCRIBE.secrets
    assert set(CORRECTION_SECRET_NAMES) == {spec["name"] for spec in KEY_SPECS} - {"HF_TOKEN"} | {"NONOKA_CLOUD_KEY"}
    assert set(CORRECT.secrets) == set(CORRECTION_SECRET_NAMES)


# -- run directories ----------------------------------------------------------------------------------------------


def test_a_run_directory_lives_as_long_as_its_process(tmp_path):
    package = tmp_path / "engines" / CORRECT.id
    with engine_host.EngineRun.claim(package) as run:
        assert run.directory.parent == package / "run"
        assert run.directory.name.startswith(f"{os.getpid()}-")
        (run.directory / "finesub.toml").write_text("x", encoding="utf-8")
        # Live: a sweep leaves it alone.
        assert engine_host.sweep_runs([package]) == []
        assert run.directory.is_dir()
    assert not run.directory.exists()


def test_the_sweep_removes_what_a_dead_owner_left(tmp_path):
    package = tmp_path / "engines" / CORRECT.id
    unlocked = package / "run" / "111-aaaaaaaaaaaa"
    unlocked.mkdir(parents=True)
    (unlocked / engine_host.OWNER_LOCK_NAME).write_bytes(b"")
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
         str(ROOT / "src"), str(held / engine_host.OWNER_LOCK_NAME)],
        stdout=subprocess.PIPE, text=True,
    )
    try:
        assert holder.stdout.readline().strip() == "held"
        removed = engine_host.sweep_runs([package, tmp_path / "engines" / "missing"])
        assert sorted(removed) == sorted([unlocked, never_locked])
        assert held.is_dir()
    finally:
        holder.kill()
        holder.wait()
    assert engine_host.sweep_runs([package]) == [held]


def test_what_wp_r4_derived_is_removed_at_start_up(tmp_path):
    from nonoka_x.migration.legacy import TRANSITIONAL_ENGINE_DIRECTORY

    transitional = tmp_path / "engines" / TRANSITIONAL_ENGINE_DIRECTORY
    transitional.mkdir(parents=True)
    (transitional / "finesub.env").write_text("FINESUB_KEYRING=sealed\n", encoding="utf-8")
    FineSubSettings(tmp_path).bind_environment()
    assert not transitional.exists()


# -- an old install, before and after the migration ----------------------------------------------------------------


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
    before = _old_engine_reads(monkeypatch, before_environment, known)
    before_gemini = llm_runtime.gemini_api_base(finesub_secrets.read_env_file(data / old.OLD_ENV))
    before_models = tomllib.loads((data / old.OLD_TOML).read_text(encoding="utf-8"))["nonoka_models"]

    from nonoka_x.config.migrate import migrate_settings

    # Start-up runs the migration's settings step before it opens anything (WP-M2).
    migrate_settings(data)
    settings = FineSubSettings(data)
    settings.bind_environment()
    settings.refresh_model_routes()
    provider = LocalProvider(data / "tasks", ROOT / "engines", issues=[], settings=settings)
    try:
        with provider.launch_contexts({CORRECT.id: CORRECT, TRANSCRIBE.id: TRANSCRIBE}) as built:
            correction = apply(CORRECT, built.contexts[CORRECT.id], reads=CORRECTION_SECRET_NAMES)
            transcription = apply(TRANSCRIBE, built.contexts[TRANSCRIBE.id], reads=TRANSCRIPTION_SECRET_NAMES)
    finally:
        provider.shutdown()

    after = {**correction["keys"], **transcription["keys"]}
    assert {name: [tuple(entry) for entry in entries] for name, entries in after.items()} == before
    assert any(after.values())  # the fixture's keys were really read, not two empty answers
    assert correction["gemini"] == before_gemini == "https://gemini.example.test/v1beta"
    after_models = tomllib.loads(correction["files"]["finesub.toml"])["nonoka_models"]
    # The routes table: the known keys, after the same route migration the old sidecar ran at start-up.
    from nonoka_x.settings import migrate_model_config

    known_routes = {f"{route}_{field}" for route in ("default", "correction", "research", "knowledge") for field in ("provider", "model")}
    expected = {key: value for key, value in {**before_models, **migrate_model_config(before_models)}.items()
                if key in known_routes and value}
    assert after_models == expected


def _old_engine_reads(monkeypatch, environment: dict[str, str], names) -> dict[str, list[tuple[str, str]]]:
    """What FineSub's own key reader (`api_keys.key_entries`) gives for each name under `environment`, in this process."""

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


@pytest.fixture(autouse=True)
def _engine_caches():
    """The engine caches its config, catalog and routes per process; each in-process test starts and ends clean."""

    from finesub.config import clear_config_cache
    from finesub.llm.routing.model_catalog import default_model_catalog
    from finesub.llm.routing.model_routes import default_model_routes

    resets = (clear_config_cache, default_model_catalog.cache_clear, default_model_routes.cache_clear)
    for reset in resets:
        reset()
    yield
    for reset in resets:
        reset()
