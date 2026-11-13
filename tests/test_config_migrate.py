"""WP-R4: Nonoka's own settings and key stores, and the migration of an old install's files into them.

Every key here is a fake made up for the test. The assertions that matter most
are the negative ones: no value ever shows up in the migration report, the log,
the settings snapshot or `settings.json`, and `secrets.json` never holds one in
the clear.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "third_party/finesub/src"))

from finesub_bootstrap import secrets as finesub_secrets  # noqa: E402  (the engine's own reader, for compatibility)

from nonoka_x import config  # noqa: E402
from nonoka_x.config import migrate  # noqa: E402
from nonoka_x.config import secrets as nonoka_secrets  # noqa: E402
from nonoka_x.config.secrets import SecretStore  # noqa: E402
from nonoka_x.config.store import SettingsStore, SettingsStoreError  # noqa: E402
from nonoka_x.settings import FineSubSettings  # noqa: E402

OLD_ENV, OLD_TOML = "finesub" + ".env", "finesub" + ".toml"

#: name -> fake value, as a real install's key file holds them (pools, endpoints, the cloud account, old names).
FAKE_ENV = {
    "GEMINI_FREE": "{main:fake-gemini-free-aaaa1111,backup:fake-gemini-free-bbbb2222}",
    "GEMINI_PAID": "fake-gemini-paid-cccc3333",
    "EXA_KEYS": "fake-exa-dddd4444,fake-exa-eeee5555",
    "OPENAI_COMPAT_API_KEY": "fake-compat-ffff6666",
    "OPENAI_COMPAT_BASE_URL": "https://compat.example.test/v1",
    "GEMINI_BASE_URL": "https://gemini.example.test/v1beta",
    "NONOKA_CLOUD_KEY": "fake-cloud-gggg7777",
    "NONOKA_CLOUD_BASE_URL": "https://cloud-jp.nonoka.online/v1/llm",
    "MOSS_PRO": "{one:fake-moss-hhhh8888,two:fake-moss-iiii9999}",
    "FINESUB_KEY_OPENROUTER_STT": "fake-openrouter-jjjj0000",
    "FINESUB_KEY_AZURE_SPEECH": "fake-azure-kkkk1111",
    "FINESUB_AZURE_SPEECH_ENDPOINT": "https://azure-speech.example.test",
    "GEMINI_TOKEN_COUNTER_EXE": "C:\\fake\\tokcount.exe",
    "MY_PRIVATE_TOKEN": "fake-unknown-llll2222",
}
UNKNOWN_ENV = ["GEMINI_TOKEN_COUNTER_EXE", "MY_PRIVATE_TOKEN"]

FAKE_TOML = """[nonoka_models]
default_provider = "local-codex"
default_model = "gpt-6.1-sol"
correction_provider = "openai-compat"
correction_model = "vendor-correction-model"
planning_provider = "local-agy"
planning_model = "gemini-3.8-flash"
mystery = "fake-toml-value-mmmm3333"

[finoka_models]
default_provider = "local-codex"
default_model = "fake-toml-value-nnnn4444"

[llm]
default_target = "local-codex-gpt-6_1-sol"
task_route_correction = "x"
temperature = 0.3

[llm.preferred_targets]
default = "local-codex-gpt-6_1-sol"
custom-group = "fake-toml-value-oooo5555"

[llm.model_groups]
fast = ["fake-toml-value-pppp6666"]

[segmentation]
max_chars = 30

[pools]
GEMINI_FREE = ["main"]
"""

FAKE_SECRETS = [value for name, value in FAKE_ENV.items() if "fake" in value]
FAKE_TOML_VALUES = ["fake-toml-value-mmmm3333", "fake-toml-value-nnnn4444", "fake-toml-value-oooo5555", "fake-toml-value-pppp6666"]


def _fake_key_parts() -> list[str]:
    """Every individual fake key (pool members split out), for the leak checks."""

    parts: list[str] = []
    for value in FAKE_SECRETS:
        for _label, key in nonoka_secrets.iter_entries(value):
            parts.append(key)
    return parts


@pytest.fixture(autouse=True)
def _protection_on(monkeypatch):
    # The legacy file is protected the way a real install's is.
    monkeypatch.delenv("FINESUB_ENV_PROTECT", raising=False)
    for module in (finesub_secrets, nonoka_secrets):
        module.set_backend(None)
    yield
    for module in (finesub_secrets, nonoka_secrets):
        module.set_backend(None)


def _old_install(data: Path, *, protect: bool = True) -> Path:
    """An old main data directory: `finesub.env` written and protected by FineSub's own code, a hand-grown `finesub.toml`."""

    data.mkdir(parents=True, exist_ok=True)
    env = data / OLD_ENV
    env.write_text("# keys saved by an older Nonoka X\n\n" + "".join(f"{name}={value}\n" for name, value in FAKE_ENV.items()), encoding="utf-8")
    if protect:
        assert finesub_secrets.protect_env_file(env)
        assert "FINESUB_KEYRING=" in env.read_text(encoding="utf-8")
        assert "fake-gemini-paid" not in env.read_text(encoding="utf-8")
    (data / (OLD_ENV + ".lock")).write_text("", encoding="utf-8")
    (data / OLD_TOML).write_text(FAKE_TOML, encoding="utf-8")
    (data / (OLD_TOML + ".lock")).write_text("", encoding="utf-8")
    return data


def _assert_no_values(text: str) -> None:
    for key in [*_fake_key_parts(), *FAKE_TOML_VALUES]:
        assert key not in text


# -- the round trip ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("protect", [True, False], ids=["protected", "plaintext"])
def test_migration_round_trip(tmp_path, protect, capsys):
    data = _old_install(tmp_path / "Nonoka X", protect=protect)
    env_before = (data / OLD_ENV).read_bytes()
    toml_before = (data / OLD_TOML).read_bytes()
    logged: list[str] = []

    report = migrate.migrate_settings(data, log=logged.append)

    # -- what Nonoka knows reads back through the settings page's model and the facade
    settings = FineSubSettings(data)
    snapshot = settings.snapshot()
    keys = {item["name"]: item for item in snapshot["keys"]}
    assert keys["GEMINI_FREE"]["count"] == 2
    assert [entry["name"] for entry in keys["GEMINI_FREE"]["masked"]] == ["main", "backup"]
    assert keys["GEMINI_PAID"]["configured"] and keys["EXA_KEYS"]["count"] == 2
    assert keys["OPENAI_COMPAT_API_KEY"]["configured"]
    assert all(keys[name]["storage"] == "protected" for name in ("GEMINI_FREE", "GEMINI_PAID", "EXA_KEYS"))
    assert snapshot["protection"] == "protected"
    urls = {item["name"]: item for item in snapshot["baseUrls"]}
    assert urls["OPENAI_COMPAT_BASE_URL"]["value"] == "https://compat.example.test/v1"
    assert urls["GEMINI_BASE_URL"]["value"] == "https://gemini.example.test/v1beta"
    routing = snapshot["modelRouting"]
    assert routing["defaultRoute"] == {"provider": "local-codex", "model": "gpt-6.1-sol"}
    routes = {item["id"]: item["route"] for item in routing["taskRoutes"]}
    assert routes["correction"] == {"provider": "openai-compat", "model": "vendor-correction-model"}
    # The old planning route moved to research (`migrate_model_config`).
    assert routes["research"] == {"provider": "local-agy", "model": "gemini-3.8-flash"}
    assert routes["knowledge"] == {"provider": "", "model": ""}
    cloud = next(item for item in routing["providers"] if item["id"] == "nonoka-cloud")
    assert cloud["available"]  # signed in: key and address both carried
    _assert_no_values(repr(snapshot))

    values = settings.values()
    for name in ("GEMINI_FREE", "GEMINI_PAID", "EXA_KEYS", "OPENAI_COMPAT_API_KEY", "NONOKA_CLOUD_KEY", "MOSS_PRO",
                 "OPENAI_COMPAT_BASE_URL", "GEMINI_BASE_URL", "NONOKA_CLOUD_BASE_URL"):
        assert values[name] == FAKE_ENV[name], name

    store = config.ConfigStore.open(data)
    from nonoka_x.transcription import keys as engine_keys

    engine_keys.register_key_specs()
    # The three old names read back under their new names.
    assert engine_keys.single(engine_keys.OPENROUTER_STT_KEY, store=store) == FAKE_ENV["FINESUB_KEY_OPENROUTER_STT"]
    assert engine_keys.single(engine_keys.AZURE_SPEECH_KEY, store=store) == FAKE_ENV["FINESUB_KEY_AZURE_SPEECH"]
    assert engine_keys.single(engine_keys.AZURE_SPEECH_ENDPOINT, store=store) == FAKE_ENV["FINESUB_AZURE_SPEECH_ENDPOINT"]
    assert [key.name for key in engine_keys.pool(engine_keys.MOSS_PRO, store=store)] == ["one", "two"]
    assert [entry.label for entry in store.key_pool("GEMINI_FREE")] == ["main", "backup"]
    for old in migrate.LEGACY_RENAMES:
        assert old not in values

    # -- what Nonoka does not know is gone, and named (only named) in the report
    assert report["status"] == migrate.COMPLETED
    assert report["env"]["dropped"] == UNKNOWN_ENV
    assert report["env"]["renamed"] == dict(migrate.LEGACY_RENAMES)
    assert report["env"]["superseded"] == [] and report["env"]["unreadable"] == []
    assert set(report["toml"]["droppedTables"]) == {"finoka_models", "llm.model_groups", "segmentation", "pools"}
    assert set(report["toml"]["droppedKeys"]) == {
        "nonoka_models.mystery", "finoka_models.default_provider", "finoka_models.default_model", "llm.temperature",
        "llm.preferred_targets.custom-group", "llm.model_groups.fast", "segmentation.max_chars", "pools.GEMINI_FREE",
    }
    assert report["notes"], "the report says FineSub falls back to its defaults"
    for name in UNKNOWN_ENV:
        assert name not in values
    report_text = migrate.report_path(data).read_text(encoding="utf-8")
    assert json.loads(report_text) == report
    _assert_no_values(report_text)
    _assert_no_values("\n".join(logged))
    _assert_no_values(capsys.readouterr().err)

    # -- the stores: settings.json holds no key, secrets.json no key in the clear
    settings_text = (data / "settings.json").read_text(encoding="utf-8")
    secrets_text = (data / "secrets.json").read_text(encoding="utf-8")
    _assert_no_values(settings_text)
    _assert_no_values(secrets_text)
    document = json.loads(secrets_text)
    assert document["schema"] == 1 and document["keyring"].startswith(("finesub$kek$v1$", "finesub$obf$v1$"))
    assert set(document["values"]) == {
        "GEMINI_FREE", "GEMINI_PAID", "EXA_KEYS", "OPENAI_COMPAT_API_KEY", "NONOKA_CLOUD_KEY", "MOSS_PRO",
        "OPENROUTER_STT_KEY", "AZURE_SPEECH_KEY", "AZURE_SPEECH_ENDPOINT",
    }
    # The pool spelling stays; only the key material is sealed.
    assert document["values"]["GEMINI_FREE"].startswith("{main:fs$") and ",backup:fs$" in document["values"]["GEMINI_FREE"]

    # -- the old files are backed up byte for byte, their locks gone
    assert not (data / OLD_ENV).exists() and not (data / OLD_TOML).exists()
    assert (data / (OLD_ENV + ".pre-migration")).read_bytes() == env_before
    assert (data / (OLD_TOML + ".pre-migration")).read_bytes() == toml_before
    assert not (data / (OLD_ENV + ".lock")).exists() and not (data / (OLD_TOML + ".lock")).exists()

    # -- running it again changes nothing
    before = {path: path.read_bytes() for path in data.rglob("*") if path.is_file()}
    assert migrate.migrate_settings(data)["status"] == migrate.COMPLETED
    assert not migrate.needed(data)
    assert {path: path.read_bytes() for path in data.rglob("*") if path.is_file()} == before


def test_what_fineSub_is_handed_after_migration(tmp_path, monkeypatch):
    """The derived files hold the known settings only, and FineSub's own reader opens the key file."""

    data = _old_install(tmp_path / "Nonoka X")
    monkeypatch.delenv("FINESUB_ENV_FILE", raising=False)
    settings = FineSubSettings(data)
    settings.bind_environment()  # migrates on its own: the old files, no new store
    settings.refresh_model_catalog()
    assert (data / "settings.json").is_file() and not (data / OLD_ENV).exists()

    env_text = settings.env_file.read_text(encoding="utf-8")
    _assert_no_values(env_text)  # sealed
    read = finesub_secrets.read_env_file(settings.env_file)
    assert read == {name: FAKE_ENV[name] for name in (
        "GEMINI_FREE", "GEMINI_PAID", "EXA_KEYS", "OPENAI_COMPAT_API_KEY", "OPENAI_COMPAT_BASE_URL", "GEMINI_BASE_URL",
        "NONOKA_CLOUD_KEY", "NONOKA_CLOUD_BASE_URL",
    )}
    # The engines' own keys are Nonoka's: not handed to FineSub (plan WP5).
    assert set(read).isdisjoint({"MOSS_PRO", "OPENROUTER_STT_KEY", "AZURE_SPEECH_KEY", "AZURE_SPEECH_ENDPOINT", *migrate.LEGACY_RENAMES})
    assert finesub_secrets.env_status(settings.env_file) == {name: "protected" for name in read}

    config_text = settings.config_file.read_text(encoding="utf-8")
    assert config_text.startswith(
        "[nonoka_models]\n"
        'default_provider = "local-codex"\n'
        'default_model = "gpt-6.1-sol"\n'
        'correction_provider = "openai-compat"\n'
        'correction_model = "vendor-correction-model"\n'
        'research_provider = "local-agy"\n'
        'research_model = "gemini-3.8-flash"\n'
        "\n[llm.preferred_targets]\n"
    )
    for gone in ("segmentation", "pools", "finoka", "planning", "mystery", "custom-group", "model_groups", "temperature"):
        assert gone not in config_text
    assert os.environ["FINESUB_CONFIG_FILE"] == str(settings.config_file)
    assert settings.config_file.parent == data.resolve() / "engines" / "finesub-shared"


def test_the_engines_key_names_match_the_engines(tmp_path):
    from nonoka_x.transcription import keys as engine_keys

    assert set(migrate.ENGINE_KEY_NAMES) == {spec.name for spec in engine_keys.KEY_SPECS}
    assert set(migrate.LEGACY_RENAMES.values()) <= set(migrate.ENGINE_KEY_NAMES)


# -- edge cases -------------------------------------------------------------------------------------------------


def test_the_new_name_wins_over_the_old_one(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    (data / OLD_ENV).write_text(
        "FINESUB_KEY_OPENROUTER_STT=fake-old-qqqq1111\nOPENROUTER_STT_KEY=fake-new-rrrr2222\n", encoding="utf-8"
    )
    report = migrate.migrate_settings(data)
    assert report["env"]["superseded"] == ["FINESUB_KEY_OPENROUTER_STT"]
    assert report["env"]["renamed"] == {}
    assert SecretStore.in_dir(data).read() == {"OPENROUTER_STT_KEY": "fake-new-rrrr2222"}


def test_a_value_that_does_not_open_here_is_reported_not_carried(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    (data / OLD_ENV).write_text(
        "FINESUB_KEYRING=finesub$kek$v1$AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA\n"
        "GEMINI_PAID=fs$AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA\n"
        "EXA_KEYS=fake-plain-ssss3333\n",
        encoding="utf-8",
    )
    report = migrate.migrate_settings(data)
    assert report["env"]["unreadable"] == ["GEMINI_PAID"]
    assert SecretStore.in_dir(data).read() == {"EXA_KEYS": "fake-plain-ssss3333"}
    # It stays in the backup.
    assert "GEMINI_PAID=fs$" in (data / (OLD_ENV + ".pre-migration")).read_text(encoding="utf-8")


def test_an_existing_new_store_is_not_merged_into(tmp_path):
    data = _old_install(tmp_path / "data")
    SettingsStore.in_dir(data).change(lambda document: None)
    before = {path: path.read_bytes() for path in data.rglob("*") if path.is_file()}
    assert migrate.blocker(data)
    with pytest.raises(migrate.SettingsMigrationBlocked):
        migrate.migrate_settings(data)
    assert {path: path.read_bytes() for path in data.rglob("*") if path.is_file()} == before
    # Start-up does not touch them either: it reports and goes on.
    FineSubSettings(data).bind_environment()
    assert (data / OLD_ENV).read_bytes() == before[data / OLD_ENV]


def test_an_unreadable_toml_changes_nothing(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    (data / OLD_TOML).write_text("[nonoka_models\n", encoding="utf-8")
    with pytest.raises(migrate.SettingsMigrationError):
        migrate.migrate_settings(data)
    assert not (data / "settings.json").exists() and (data / OLD_TOML).exists()


def test_a_taken_backup_name_gets_a_number(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    (data / OLD_TOML).write_text('[nonoka_models]\ndefault_provider = "local-codex"\n', encoding="utf-8")
    (data / (OLD_TOML + ".pre-migration")).write_text("an older backup\n", encoding="utf-8")
    report = migrate.migrate_settings(data)
    assert report["backups"] == {OLD_TOML: OLD_TOML + ".pre-migration.1"}
    assert (data / (OLD_TOML + ".pre-migration")).read_text(encoding="utf-8") == "an older backup\n"


@pytest.mark.parametrize("crash_at", ["stores", "renames"])
def test_an_interrupted_migration_continues(tmp_path, monkeypatch, crash_at):
    data = _old_install(tmp_path / "data")

    class Crash(RuntimeError):
        pass

    if crash_at == "stores":
        real = SecretStore.update

        def crashing(self, updates):
            raise Crash("killed while writing secrets.json")

        monkeypatch.setattr(SecretStore, "update", crashing)
    else:
        real = migrate.replace_path
        calls = []

        def crashing(source, target, **kwargs):
            calls.append(source)
            if len(calls) == 2:
                raise Crash("killed between the two renames")
            return real(source, target, **kwargs)

        monkeypatch.setattr(migrate, "replace_path", crashing)
    with pytest.raises(Crash):
        migrate.migrate_settings(data)
    assert migrate.needed(data)
    monkeypatch.setattr(SecretStore, "update", real) if crash_at == "stores" else monkeypatch.setattr(migrate, "replace_path", real)

    report = migrate.migrate_settings(data)
    assert report["status"] == migrate.COMPLETED
    assert not (data / OLD_ENV).exists() and not (data / OLD_TOML).exists()
    assert SecretStore.in_dir(data).read()["GEMINI_PAID"] == FAKE_ENV["GEMINI_PAID"]
    assert FineSubSettings(data).model_config()["default_model"] == "gpt-6.1-sol"


def test_the_migration_command_runs_the_settings_step(tmp_path):
    from nonoka_x import migration
    from nonoka_x.migration import hooks as deferred
    from nonoka_x.migration import runner as executing

    data = _old_install(tmp_path / "Nonoka X")
    (data / "finesub" / "runtime" / "python").mkdir(parents=True)
    # Only the settings step: the layer steps (WP-E3) would reach R2.
    hooks = {deferred.SETTINGS: deferred.available_hooks()[deferred.SETTINGS]}
    plan = migration.prepare(data, hooks=hooks)
    step = next(step for step in plan.steps if step.id == deferred.SETTINGS)
    assert step.hooked and step.actions
    migration.execute(plan, hooks=hooks)
    journal = migration.read_journal(data)
    assert journal["steps"][deferred.SETTINGS]["status"] == executing.DONE
    assert (data / "secrets.json").is_file() and (data / (OLD_ENV + ".pre-migration")).is_file()


def test_the_migration_plan_is_blocked_by_a_foreign_store(tmp_path):
    from nonoka_x import migration

    data = _old_install(tmp_path / "Nonoka X")
    (data / "finesub" / "runtime").mkdir(parents=True)
    SecretStore.in_dir(data).update({"GEMINI_PAID": "fake-existing-tttt4444"})
    plan = migration.prepare(data)
    assert any("secrets.json" in blocker for blocker in plan.blockers)


# -- the stores ---------------------------------------------------------------------------------------------------


def test_the_envelope_is_finesubs(tmp_path):
    """A value sealed by Nonoka opens with FineSub's code and the other way round."""

    master = nonoka_secrets.new_master()
    keyring = nonoka_secrets.wrap_master(master)
    token = nonoka_secrets.seal("fake-envelope-uuuu5555", master)
    assert finesub_secrets.open_(token, finesub_secrets.unwrap_master(keyring)) == "fake-envelope-uuuu5555"
    other = finesub_secrets.seal("fake-envelope-vvvv6666", finesub_secrets.unwrap_master(keyring))
    assert nonoka_secrets.open_(other, nonoka_secrets.unwrap_master(keyring)) == "fake-envelope-vvvv6666"
    for prefix in ("KEK_PREFIX", "OBF_PREFIX", "VALUE_PREFIX"):
        assert getattr(nonoka_secrets, prefix) == getattr(finesub_secrets, prefix)
    sample = "{a: fake-1, 'b':\"fake-2\" ,c:}"
    assert nonoka_secrets._value_spans(sample) == finesub_secrets._value_spans(sample)
    assert nonoka_secrets.iter_entries(sample) == finesub_secrets.iter_entries(sample)


def test_secret_store_seals_keeps_order_and_reports_states(tmp_path):
    store = SecretStore.in_dir(tmp_path)
    store.update({"EXA_KEYS": "fake-w1,fake-w2", "GEMINI_PAID": "fake-wwww7777"})
    store.update({"GEMINI_PAID": None, "MOSS_PRO": "{x:fake-xxxx8888}"})
    assert list(store.read()) == ["EXA_KEYS", "MOSS_PRO"]
    assert store.read()["MOSS_PRO"] == "{x:fake-xxxx8888}"
    assert store.status() == {"EXA_KEYS": "protected", "MOSS_PRO": "protected"}
    text = store.path.read_text(encoding="utf-8")
    for fake in ("fake-w1", "fake-w2", "fake-xxxx8888", "fake-wwww7777"):
        assert fake not in text
    # Another account's keyring: the value is unreadable, never garbage.
    document = json.loads(text)
    document["keyring"] = "finesub$obf$v1$" + "A" * 80
    store.path.write_text(json.dumps(document), encoding="utf-8")
    nonoka_secrets.set_backend(None)
    assert store.read() == {}
    assert set(store.status().values()) == {"unreadable"}
    if os.name != "nt":
        assert store.path.stat().st_mode & 0o077 == 0


def test_settings_store_is_sparse_and_checked(tmp_path):
    store = SettingsStore.in_dir(tmp_path)
    assert store.read() == {"schema": 1}
    from nonoka_x.config.store import set_route_field, set_section_value

    store.change(lambda d: (set_route_field(d, "research", "model", "m"), set_route_field(d, "default", "provider", "p"),
                            set_section_value(d, "baseUrls", "GEMINI_BASE_URL", "https://g.test")))
    assert json.loads(store.path.read_text(encoding="utf-8")) == {
        "schema": 1,
        "llm": {"routes": {"default": {"provider": "p"}, "research": {"model": "m"}}},
        "baseUrls": {"GEMINI_BASE_URL": "https://g.test"},
    }
    store.change(lambda d: set_route_field(d, "research", "model", None))
    assert store.routes() == {"default": {"provider": "p"}}
    store.path.write_text('{"schema": 2}', encoding="utf-8")
    with pytest.raises(SettingsStoreError):
        store.read()


def test_the_facade_saves_and_reads_a_registered_key(tmp_path, monkeypatch):
    monkeypatch.setenv("FINESUB_ENV_PROTECT", "0")
    spec = config.KeySpec("WP_R4_TEST_ENDPOINT", "test endpoint")
    config.register_key_spec(spec)
    try:
        store = config.ConfigStore.open(tmp_path)
        assert store.key_pool("WP_R4_TEST_ENDPOINT") == ()
        snapshot = store.update({"WP_R4_TEST_ENDPOINT": "https://endpoint.example.test:8443/x"})
        assert "WP_R4_TEST_ENDPOINT" not in {item["name"] for item in snapshot["keys"]}
        assert store.key_pool("WP_R4_TEST_ENDPOINT") == (config.KeyEntry("", "https://endpoint.example.test:8443/x"),)
        # Nonoka's own, not FineSub's.
        settings = FineSubSettings(tmp_path)
        assert not settings.env_file.exists() or "WP_R4_TEST" not in settings.env_file.read_text(encoding="utf-8")
        with pytest.raises(KeyError):
            store.key_pool("NOT_A_KEY")
    finally:
        config._unregister_key_spec("WP_R4_TEST_ENDPOINT")
