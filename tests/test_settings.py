from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "third_party/finesub/src"))

from nonoka_x.settings import FineSubSettings


class FineSubSettingsTests(unittest.TestCase):
    def setUp(self) -> None:
        # The engine caches config, catalog and routes per process, so a test
        # that pins a route would otherwise answer the next one's questions.
        from finesub.config import clear_config_cache
        from finesub.llm.routing.model_catalog import default_model_catalog
        from finesub.llm.routing.model_routes import default_model_routes

        for reset in (clear_config_cache, default_model_catalog.cache_clear, default_model_routes.cache_clear):
            reset()
            self.addCleanup(reset)

    def test_keys_are_saved_in_finesub_format_but_never_returned(self) -> None:
        previous = os.environ.get("FINESUB_ENV_PROTECT")
        os.environ["FINESUB_ENV_PROTECT"] = "0"
        try:
            with tempfile.TemporaryDirectory() as temporary:
                settings = FineSubSettings(temporary)
                settings.bind_environment()
                snapshot = settings.update_keys(
                    {
                        "GEMINI_FREE": "primary:gemini-secret",
                        "EXA_KEYS": "exa-secret",
                        "GEMINI_BASE_URL": "https://gemini.example.test/v1beta/",
                    }
                )
                self.assertTrue(snapshot["llmKeyConfigured"])
                self.assertTrue(snapshot["retrievalKeyConfigured"])
                encoded = repr(snapshot)
                self.assertNotIn("gemini-secret", encoded)
                self.assertNotIn("exa-secret", encoded)
                gemini = next(item for item in snapshot["keys"] if item["name"] == "GEMINI_FREE")
                self.assertEqual(gemini["count"], 1)
                self.assertEqual(gemini["masked"][0]["name"], "primary")
                gemini_base_url = next(
                    item for item in snapshot["baseUrls"] if item["name"] == "GEMINI_BASE_URL"
                )
                self.assertEqual(gemini_base_url["value"], "https://gemini.example.test/v1beta")
                self.assertTrue(gemini_base_url["customized"])
                if os.name != "nt":
                    self.assertEqual((Path(temporary) / "finesub.env").stat().st_mode & 0o077, 0)
        finally:
            if previous is None:
                os.environ.pop("FINESUB_ENV_PROTECT", None)
            else:
                os.environ["FINESUB_ENV_PROTECT"] = previous

    def test_unknown_key_name_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(ValueError, "Unknown FineSub key"):
                FineSubSettings(temporary).update_keys({"NOT_A_KEY": "secret"})

    def test_invalid_base_url_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(ValueError, "must be an http"):
                FineSubSettings(temporary).update_keys({"OPENAI_BASE_URL": "not-a-url"})

    def test_model_routing_writes_custom_catalog_and_task_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, patch.dict(
            os.environ, {"FINESUB_ENV_PROTECT": "0"}, clear=False
        ):
            settings = FineSubSettings(temporary)
            settings.bind_environment()
            snapshot = settings.update_keys(
                {
                    "OPENAI_API_KEY": "openai-secret",
                    "OPENAI_BASE_URL": "https://proxy.example/v1",
                    # The research route below points at Gemini, and a
                    # provider without a key cannot be routed to.
                    "GEMINI_FREE": "gemini-secret",
                    "LLM_DEFAULT_PROVIDER": "openai",
                    "LLM_DEFAULT_MODEL": "gpt-custom",
                    "LLM_ROUTE_RESEARCH_PROVIDER": "gemini-free",
                    "LLM_ROUTE_RESEARCH_MODEL": "gemini/gemini-3.6-flash",
                }
            )
            self.assertEqual(
                snapshot["modelRouting"]["defaultRoute"],
                {"provider": "openai", "model": "gpt-custom"},
            )
            catalog = (Path(temporary) / "model_catalog.psv").read_text(encoding="utf-8")
            self.assertIn("openai_compat|https://proxy.example/v1|OPENAI_API_KEY", catalog)
            self.assertIn("|gpt-custom|gpt-custom|", catalog)
            config = (Path(temporary) / "finesub.toml").read_text(encoding="utf-8")
            self.assertIn("[llm.preferred_targets]", config)
            self.assertIn('default = "nonoka-openai-', config)
            self.assertIn('research = "gemini-free-3_6-flash"', config)

            from finesub.config import clear_config_cache
            from finesub.llm.routing.model_catalog import default_model_catalog
            from finesub.llm.routing.model_routes import default_model_routes

            clear_config_cache()
            default_model_catalog.cache_clear()
            routes = default_model_routes()
            correction, _ = routes.resolve_binding(
                routes.active_preset_id, "correction-text", "quality"
            )
            correction_mid, _ = routes.resolve_binding(
                routes.active_preset_id, "correction-text", "intermediate"
            )
            correction_eff, _ = routes.resolve_binding(
                routes.active_preset_id, "correction-text", "efficiency"
            )
            research, _ = routes.resolve_binding(
                routes.active_preset_id, "research", "quality"
            )
            self.assertTrue(correction.target_ids[0].startswith("nonoka-openai-"))
            self.assertTrue(correction_mid.target_ids[0].startswith("nonoka-openai-"))
            self.assertTrue(correction_eff.target_ids[0].startswith("nonoka-openai-"))
            self.assertEqual(research.target_ids[0], "gemini-free-3_6-flash")

    def test_the_two_gemini_pools_share_one_provider_row(self) -> None:
        # 对配置的人来说 Gemini 是一个服务：同一个控制台、同一个端点，两档配额
        # 各一个 Key。对路由来说仍然是两个 tier——打包目录给了它们不同的 fact
        # id 和不同的模型清单（免费池七个、付费池两个），路由存的就是 tier。所
        # 以两条记录保留，靠 groupId 在设置里折成一行。
        with tempfile.TemporaryDirectory() as temporary, patch.dict(
            os.environ, {"FINESUB_ENV_PROTECT": "0"}, clear=False
        ):
            settings = FineSubSettings(temporary)
            settings.bind_environment()
            providers = {
                item["id"]: item
                for item in settings.snapshot()["modelRouting"]["providers"]
            }
            free, paid = providers["gemini-free"], providers["gemini-paid"]
            self.assertEqual(free["groupId"], paid["groupId"])
            self.assertEqual(free["groupLabel"], "Gemini")
            self.assertEqual(paid["groupLabel"], "Gemini")
            self.assertEqual({free["tierLabel"], paid["tierLabel"]}, {"免费池", "付费池"})
            # 档位各自的 Key，也各自的模型清单——切换档位时模型必须跟着换。
            self.assertNotEqual(free["keyName"], paid["keyName"])
            self.assertNotEqual(
                [item["id"] for item in free["models"]],
                [item["id"] for item in paid["models"]],
            )
            # 其余提供商不分组，用空串说出来而不是缺字段。
            self.assertEqual(providers["openai"]["groupId"], "")
            self.assertEqual(providers["local-agy"]["groupId"], "")

    def test_llm_is_not_ready_until_a_global_model_is_saved(self) -> None:
        # A key on its own configures nothing: without a saved global model the
        # desktop must not offer 「最终字幕」.
        with tempfile.TemporaryDirectory() as temporary, patch.dict(
            os.environ, {"FINESUB_ENV_PROTECT": "0"}, clear=False
        ):
            settings = FineSubSettings(temporary)
            settings.bind_environment()
            snapshot = settings.update_keys({"GEMINI_FREE": "gemini-secret"})
            self.assertTrue(snapshot["llmKeyConfigured"])
            self.assertFalse(snapshot["llmReady"])

            snapshot = settings.update_keys(
                {
                    "LLM_DEFAULT_PROVIDER": "gemini-free",
                    "LLM_DEFAULT_MODEL": "gemini/gemini-3.6-flash",
                }
            )
            self.assertTrue(snapshot["llmReady"])

    def test_llm_is_not_ready_when_the_saved_provider_lost_its_key(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, patch.dict(
            os.environ, {"FINESUB_ENV_PROTECT": "0"}, clear=False
        ):
            settings = FineSubSettings(temporary)
            settings.bind_environment()
            settings.update_keys(
                {
                    "GEMINI_FREE": "gemini-secret",
                    "LLM_DEFAULT_PROVIDER": "gemini-free",
                    "LLM_DEFAULT_MODEL": "gemini/gemini-3.6-flash",
                }
            )
            snapshot = settings.update_keys({"GEMINI_FREE": None})
            self.assertFalse(snapshot["llmReady"])

    def test_llm_is_not_ready_when_a_compat_endpoint_has_no_base_url(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, patch.dict(
            os.environ, {"FINESUB_ENV_PROTECT": "0"}, clear=False
        ):
            settings = FineSubSettings(temporary)
            settings.bind_environment()
            snapshot = settings.update_keys(
                {
                    "OPENAI_COMPAT_API_KEY": "compat-secret",
                    "OPENAI_COMPAT_BASE_URL": "https://vendor.example/v1",
                    "LLM_DEFAULT_PROVIDER": "openai-compat",
                    "LLM_DEFAULT_MODEL": "vendor-model",
                }
            )
            self.assertTrue(snapshot["llmReady"])
            snapshot = settings.update_keys({"OPENAI_COMPAT_BASE_URL": None})
            self.assertFalse(snapshot["llmReady"])

    def test_local_agent_is_only_ready_once_its_route_is_saved_and_the_cli_exists(self) -> None:
        # Having codex on the machine is not a configuration — the route has to
        # be saved — and a saved route to a CLI that is gone is not usable.
        with tempfile.TemporaryDirectory() as temporary, patch.dict(
            os.environ, {"FINESUB_ENV_PROTECT": "0"}, clear=False
        ):
            settings = FineSubSettings(temporary)
            settings.bind_environment()
            with patch("nonoka_x.settings.shutil.which", return_value="/usr/local/bin/codex"):
                self.assertFalse(settings.snapshot()["llmReady"])
                snapshot = settings.update_keys(
                    {
                        "LLM_DEFAULT_PROVIDER": "local-codex",
                        "LLM_DEFAULT_MODEL": "gpt-5.6-sol",
                    }
                )
                self.assertTrue(snapshot["llmReady"])
            with patch("nonoka_x.settings.shutil.which", return_value=None), patch(
                "nonoka_x.settings.os.name", "posix"
            ):
                self.assertFalse(settings.snapshot()["llmReady"])

    def test_local_codex_route_binds_the_packaged_agent_target(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, patch.dict(
            os.environ, {"FINESUB_ENV_PROTECT": "0"}, clear=False
        ):
            settings = FineSubSettings(temporary)
            settings.bind_environment()
            snapshot = settings.update_keys(
                {
                    "LLM_DEFAULT_PROVIDER": "local-codex",
                    "LLM_DEFAULT_MODEL": "gpt-5.6-sol",
                }
            )
            codex = next(
                item for item in snapshot["modelRouting"]["providers"] if item["id"] == "local-codex"
            )
            self.assertFalse(codex["requiresKey"])
            self.assertEqual(codex["mode"], "select")
            # Sol leads the roster, so selecting the provider fills it in.
            self.assertEqual([model["id"] for model in codex["models"]], ["gpt-5.6-sol", "gpt-5.6-terra"])
            self.assertEqual(codex["defaultModel"], "gpt-5.6-sol")
            config = (Path(temporary) / "finesub.toml").read_text(encoding="utf-8")
            self.assertIn('default = "local-codex-completion-gpt-5_6-sol"', config)

            from finesub.config import clear_config_cache
            from finesub.llm.routing.model_catalog import default_model_catalog
            from finesub.llm.routing.model_routes import default_model_routes

            clear_config_cache()
            default_model_catalog.cache_clear()
            default_model_routes.cache_clear()
            routes = default_model_routes()
            correction, _ = routes.resolve_binding(
                routes.active_preset_id, "correction-text", "quality"
            )
            # Alone: a selected local CLI runs on the user's own
            # subscription, so the packaged API tail is not a fallback
            # they asked for. A window this CLI cannot serve now fails
            # its capability check naming the CLI, instead of quietly
            # continuing on a Gemini tier that may have no key at all.
            self.assertEqual(
                correction.target_ids, ("local-codex-completion-gpt-5_6-sol",)
            )

    def test_local_agy_route_binds_the_packaged_agent_target(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, patch.dict(
            os.environ, {"FINESUB_ENV_PROTECT": "0"}, clear=False
        ):
            settings = FineSubSettings(temporary)
            settings.bind_environment()
            snapshot = settings.update_keys(
                {
                    "LLM_DEFAULT_PROVIDER": "local-agy",
                    "LLM_DEFAULT_MODEL": "gemini-3.7-flash",
                }
            )
            agy = next(
                item for item in snapshot["modelRouting"]["providers"] if item["id"] == "local-agy"
            )
            self.assertFalse(agy["requiresKey"])
            self.assertEqual(agy["mode"], "select")
            self.assertEqual(
                [model["id"] for model in agy["models"]],
                ["gemini-3.7-flash", "claude-opus-4-6-thinking"],
            )
            self.assertEqual(agy["defaultModel"], "gemini-3.7-flash")
            # The multimodal model is why agy leads with it; the snapshot has
            # to carry that through to the picker.
            self.assertTrue(agy["models"][0]["supportsAudio"])
            self.assertTrue(agy["models"][0]["supportsVideo"])
            config = (Path(temporary) / "finesub.toml").read_text(encoding="utf-8")
            # The media target, not the search-entitled twin: a pinned target
            # is prepended to the bound group, so `retrieval=native` has to
            # stay free to fall through.
            self.assertIn('default = "local-agy-media-gemini-3_7-flash"', config)

            from finesub.config import clear_config_cache
            from finesub.llm.routing.model_catalog import default_model_catalog
            from finesub.llm.routing.model_routes import default_model_routes

            clear_config_cache()
            default_model_catalog.cache_clear()
            default_model_routes.cache_clear()
            routes = default_model_routes()
            correction, _ = routes.resolve_binding(
                routes.active_preset_id, "correction-text", "quality"
            )
            self.assertEqual(
                correction.target_ids, ("local-agy-media-gemini-3_7-flash",)
            )

    def test_local_agy_opus_route_binds_its_own_target(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, patch.dict(
            os.environ, {"FINESUB_ENV_PROTECT": "0"}, clear=False
        ):
            settings = FineSubSettings(temporary)
            settings.bind_environment()
            settings.update_keys(
                {
                    "LLM_ROUTE_CORRECTION_PROVIDER": "local-agy",
                    "LLM_ROUTE_CORRECTION_MODEL": "claude-opus-4-6-thinking",
                }
            )
            config = (Path(temporary) / "finesub.toml").read_text(encoding="utf-8")
            self.assertIn('correction-mm = "local-agy-opus-4_6"', config)
            self.assertIn('correction-text = "local-agy-opus-4_6"', config)

    def test_local_workbuddy_route_binds_the_packaged_agent_target(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, patch.dict(
            os.environ, {"FINESUB_ENV_PROTECT": "0"}, clear=False
        ):
            settings = FineSubSettings(temporary)
            settings.bind_environment()
            snapshot = settings.update_keys(
                {
                    "LLM_DEFAULT_PROVIDER": "local-workbuddy",
                    "LLM_DEFAULT_MODEL": "glm-5.3-flash",
                }
            )
            workbuddy = next(
                item
                for item in snapshot["modelRouting"]["providers"]
                if item["id"] == "local-workbuddy"
            )
            self.assertFalse(workbuddy["requiresKey"])
            self.assertEqual(workbuddy["mode"], "select")
            self.assertEqual(
                [model["id"] for model in workbuddy["models"]],
                [
                    "glm-5.3-flash",
                    "hy3",
                    "hy4-preview",
                    "deepseek-v4-flash",
                    "deepseek-v4-pro",
                ],
            )
            self.assertEqual(workbuddy["defaultModel"], "glm-5.3-flash")
            # None of the five takes a clip: the multimodal correction entry
            # has to stay disabled on this provider rather than fail mid-run.
            self.assertFalse(any(model["supportsAudio"] for model in workbuddy["models"]))
            self.assertFalse(any(model["supportsVideo"] for model in workbuddy["models"]))
            config = (Path(temporary) / "finesub.toml").read_text(encoding="utf-8")
            # The plain target, not the search-entitled twin: a pinned target is
            # prepended to the bound group, so `retrieval=native` has to stay
            # free to fall through to one that declares a search tool.
            self.assertIn('default = "local-workbuddy-glm-5_3-flash"', config)

            from finesub.config import clear_config_cache
            from finesub.llm.routing.model_catalog import default_model_catalog
            from finesub.llm.routing.model_routes import default_model_routes

            clear_config_cache()
            default_model_catalog.cache_clear()
            default_model_routes.cache_clear()
            routes = default_model_routes()
            correction, _ = routes.resolve_binding(
                routes.active_preset_id, "correction-text", "quality"
            )
            self.assertEqual(correction.target_ids, ("local-workbuddy-glm-5_3-flash",))

    def test_workbuddy_says_which_of_its_models_bill_automatically(self) -> None:
        """The one provider whose default behaviour spends money unprompted.

        `hy3` and `hy4-preview` carry a `fallback_model` pointing at their paid
        twin, so a spent daily allowance switches lines and carries on instead
        of stopping. That is the engine's decision and a good one -- a task
        half-done is worse -- but it has to be legible *before* someone picks
        the provider, which is what the snapshot's `note` is for.
        """

        with tempfile.TemporaryDirectory() as temporary, patch.dict(
            os.environ, {"FINESUB_ENV_PROTECT": "0"}, clear=False
        ):
            settings = FineSubSettings(temporary)
            settings.bind_environment()
            providers = {
                item["id"]: item
                for item in settings.snapshot()["modelRouting"]["providers"]
            }
            self.assertIn("Hunyuan", providers["local-workbuddy"]["note"])
            # Every provider carries the field; only this one has anything to
            # say, so a frontend can render it unconditionally.
            self.assertEqual(providers["local-agy"]["note"], "")
            self.assertEqual(providers["openai"]["note"], "")

    def test_unknown_local_workbuddy_model_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, patch.dict(
            os.environ, {"FINESUB_ENV_PROTECT": "0"}, clear=False
        ):
            settings = FineSubSettings(temporary)
            settings.bind_environment()
            with self.assertRaisesRegex(ValueError, "not available in local-workbuddy"):
                settings.update_keys(
                    {
                        "LLM_DEFAULT_PROVIDER": "local-workbuddy",
                        "LLM_DEFAULT_MODEL": "hy5",
                    }
                )

    def test_workbuddy_is_detected_inside_its_desktop_app(self) -> None:
        """The same file the engine's own resolver falls back to.

        WorkBuddy ships the CLI inside the Electron app instead of as an npm
        package, and its PATH shim is a bash script `shutil.which` cannot find
        on Windows -- so the install root is the whole question here.
        """

        with tempfile.TemporaryDirectory() as temporary:
            cli = (
                Path(temporary)
                / "Programs"
                / "WorkBuddy"
                / "resources"
                / "app.asar.unpacked"
                / "cli"
                / "bin"
            )
            cli.mkdir(parents=True)
            (cli / "codebuddy").write_bytes(b"")
            with patch.dict(os.environ, {"LOCALAPPDATA": temporary}, clear=False), patch(
                "nonoka_x.settings.shutil.which", return_value=None
            ), patch("nonoka_x.settings.os.name", "nt"):
                from nonoka_x.settings import (
                    local_agent_executable,
                    local_agent_path_entries,
                )

                self.assertEqual(
                    local_agent_executable("local-workbuddy"), cli / "codebuddy"
                )
                # ...and deliberately not put on PATH: that file is a Node entry
                # script, not an executable, and the engine reaches it through
                # %LOCALAPPDATA% itself. Advertising it would put a `codebuddy`
                # on PATH that nothing can launch.
                self.assertEqual(local_agent_path_entries(), [])

    def test_unknown_local_agy_model_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, patch.dict(
            os.environ, {"FINESUB_ENV_PROTECT": "0"}, clear=False
        ):
            settings = FineSubSettings(temporary)
            settings.bind_environment()
            with self.assertRaisesRegex(ValueError, "not available in local-agy"):
                settings.update_keys(
                    {
                        "LLM_DEFAULT_PROVIDER": "local-agy",
                        "LLM_DEFAULT_MODEL": "gemini-nope",
                    }
                )

    def test_codex_installed_off_path_is_still_found_and_put_back_on_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            app = Path(temporary) / "OpenAI" / "Codex" / "bin"
            live = app / "6ca77c4a9caa4eed"
            live.mkdir(parents=True)
            (live / "codex.exe").write_bytes(b"")
            staging = app / ".staging-abcdef"
            staging.mkdir()
            (staging / "codex.exe").write_bytes(b"")
            with patch.dict(os.environ, {"LOCALAPPDATA": temporary}, clear=False), patch(
                "nonoka_x.settings.shutil.which", return_value=None
            ), patch("nonoka_x.settings.os.name", "nt"):
                from nonoka_x.settings import local_agent_executable, local_agent_path_entries

                self.assertEqual(local_agent_executable("local-codex"), live / "codex.exe")
                self.assertEqual(local_agent_path_entries(), [str(live)])

    def test_agy_installed_off_path_is_still_found_and_put_back_on_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            bin_dir = Path(temporary) / "agy" / "bin"
            bin_dir.mkdir(parents=True)
            (bin_dir / "agy.exe").write_bytes(b"")
            with patch.dict(os.environ, {"LOCALAPPDATA": temporary}, clear=False), patch(
                "nonoka_x.settings.shutil.which", return_value=None
            ), patch("nonoka_x.settings.os.name", "nt"):
                from nonoka_x.settings import local_agent_executable, local_agent_path_entries

                self.assertEqual(local_agent_executable("local-agy"), bin_dir / "agy.exe")
                self.assertEqual(local_agent_path_entries(), [str(bin_dir)])

    def test_antigravity_ide_alone_does_not_count_as_the_agy_cli(self) -> None:
        # The Electron IDE installs under %LOCALAPPDATA%/Programs/antigravity
        # and ships no `agy` at all; reporting it as available would put a
        # provider in the picker that every call then fails on.
        with tempfile.TemporaryDirectory() as temporary:
            ide = Path(temporary) / "Programs" / "antigravity"
            ide.mkdir(parents=True)
            (ide / "Antigravity.exe").write_bytes(b"")
            with patch.dict(os.environ, {"LOCALAPPDATA": temporary}, clear=False), patch(
                "nonoka_x.settings.shutil.which", return_value=None
            ), patch("nonoka_x.settings.os.name", "nt"):
                from nonoka_x.settings import local_agent_executable

                self.assertIsNone(local_agent_executable("local-agy"))

    def test_local_agent_on_path_is_not_added_to_it_twice(self) -> None:
        with patch("nonoka_x.settings.shutil.which", return_value="/usr/local/bin/codex"):
            from nonoka_x.settings import local_agent_path_entries

            self.assertEqual(local_agent_path_entries(), [])

    def test_terra_route_binds_its_own_target(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, patch.dict(
            os.environ, {"FINESUB_ENV_PROTECT": "0"}, clear=False
        ):
            settings = FineSubSettings(temporary)
            settings.bind_environment()
            settings.update_keys(
                {
                    "LLM_ROUTE_CORRECTION_PROVIDER": "local-codex",
                    "LLM_ROUTE_CORRECTION_MODEL": "gpt-5.6-terra",
                }
            )
            config = (Path(temporary) / "finesub.toml").read_text(encoding="utf-8")
            self.assertIn('correction-mm = "local-codex-completion-gpt-5_6-terra"', config)
            self.assertIn('correction-text = "local-codex-completion-gpt-5_6-terra"', config)

    def test_unknown_local_codex_model_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, patch.dict(
            os.environ, {"FINESUB_ENV_PROTECT": "0"}, clear=False
        ):
            settings = FineSubSettings(temporary)
            settings.bind_environment()
            with self.assertRaisesRegex(ValueError, "not available in local-codex"):
                settings.update_keys(
                    {
                        "LLM_DEFAULT_PROVIDER": "local-codex",
                        "LLM_DEFAULT_MODEL": "gpt-nope",
                    }
                )

    def test_compat_provider_keeps_its_own_endpoint_and_key(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, patch.dict(
            os.environ, {"FINESUB_ENV_PROTECT": "0"}, clear=False
        ):
            settings = FineSubSettings(temporary)
            settings.bind_environment()
            snapshot = settings.update_keys(
                {
                    "OPENAI_COMPAT_API_KEY": "compat-secret",
                    "OPENAI_COMPAT_BASE_URL": "https://vendor.example/v1/",
                    "LLM_DEFAULT_PROVIDER": "openai-compat",
                    "LLM_DEFAULT_MODEL": "vendor-large",
                }
            )
            provider = next(
                item
                for item in snapshot["modelRouting"]["providers"]
                if item["id"] == "openai-compat"
            )
            self.assertTrue(provider["customEndpoint"])
            self.assertTrue(provider["keyConfigured"])
            self.assertEqual(provider["keyName"], "OPENAI_COMPAT_API_KEY")
            catalog = (Path(temporary) / "model_catalog.psv").read_text(encoding="utf-8")
            self.assertIn(
                "openai_compat|https://vendor.example/v1|OPENAI_COMPAT_API_KEY", catalog
            )
            # The official OpenAI address is untouched by the compat endpoint.
            official = next(
                item for item in snapshot["baseUrls"] if item["name"] == "OPENAI_BASE_URL"
            )
            self.assertFalse(official["customized"])

    def test_known_model_row_carries_its_declared_limits(self) -> None:
        """A recognized model is described by its own facts, not a placeholder.

        Windows are planned against `max_output_tokens`, so the row deciding
        that DeepSeek can only answer 16k tokens is what turns every window
        into a split-and-retry ladder. The thinking column matters for the same
        reason: DeepSeek bills its chain of thought inside `completion_tokens`,
        and identity would send it an effort word it does not know.
        """

        with tempfile.TemporaryDirectory() as temporary, patch.dict(
            os.environ, {"FINESUB_ENV_PROTECT": "0"}, clear=False
        ):
            settings = FineSubSettings(temporary)
            settings.bind_environment()
            settings.update_keys(
                {
                    "OPENAI_COMPAT_API_KEY": "compat-secret",
                    "OPENAI_COMPAT_BASE_URL": "https://vendor.example/v1",
                    "LLM_DEFAULT_PROVIDER": "openai-compat",
                    "LLM_DEFAULT_MODEL": "deepseek-v4-flash",
                }
            )
            catalog = (Path(temporary) / "model_catalog.psv").read_text(encoding="utf-8")
            self.assertIn(
                "|DeepSeek-V4-Flash|deepseek-v4-flash|1000000|384000|"
                "false|false|false|max,high,low|1.15|70",
                catalog,
            )

            from finesub.config import clear_config_cache
            from finesub.llm.routing.model_catalog import default_model_catalog

            clear_config_cache()
            default_model_catalog.cache_clear()
            # By `provider_tier`, not by `api_model_id`: engine 0.5.1 ships a
            # packaged WorkBuddy row serving the same model id, and the row
            # under test is the generated one for this endpoint.
            entry = next(
                item
                for item in default_model_catalog()
                if item.api_model_id == "deepseek-v4-flash"
                and item.provider_tier == "NONOKA_OPENAI_COMPAT"
            )
            self.assertEqual(entry.max_output_tokens, 384_000)
            self.assertEqual(entry.max_input_tokens, 1_000_000)
            self.assertEqual(entry.thinking_levels, ("max", "high", "low"))
            self.assertAlmostEqual(entry.token_scale, 1.15)
            # Media stays off whatever the model can do: the compat transport
            # is text-only, and the catalog rejects a row that claims otherwise.
            self.assertFalse(entry.supports_audio)
            self.assertFalse(entry.supports_video)

    def test_anthropic_compat_row_reads_the_same_model_table(self) -> None:
        """The facts are the model's, not the route's.

        Both transports carry an effort word -- `reasoning_effort` on the
        OpenAI dialect, `output_config.effort` on the Anthropic one -- so the
        same table serves every HTTP provider the desktop offers.
        """

        with tempfile.TemporaryDirectory() as temporary, patch.dict(
            os.environ, {"FINESUB_ENV_PROTECT": "0"}, clear=False
        ):
            settings = FineSubSettings(temporary)
            settings.bind_environment()
            settings.update_keys(
                {
                    "ANTHROPIC_COMPAT_API_KEY": "compat-secret",
                    "ANTHROPIC_COMPAT_BASE_URL": "https://relay.example",
                    "LLM_DEFAULT_PROVIDER": "anthropic-compat",
                    "LLM_DEFAULT_MODEL": "claude-sonnet-5",
                }
            )
            catalog = (Path(temporary) / "model_catalog.psv").read_text(encoding="utf-8")
            self.assertIn("|anthropic|https://relay.example|", catalog)
            self.assertIn(
                "|Claude Sonnet 5|claude-sonnet-5|1000000|65536|"
                "false|false|false|true|1|77",
                catalog,
            )

            from finesub.config import clear_config_cache
            from finesub.llm.routing.model_catalog import default_model_catalog

            clear_config_cache()
            default_model_catalog.cache_clear()
            entry = next(
                item
                for item in default_model_catalog()
                if item.api_model_id == "claude-sonnet-5" and item.base_url
            )
            self.assertEqual(entry.provider_kind, "anthropic")
            self.assertEqual(entry.max_output_tokens, 65_536)
            self.assertEqual(entry.thinking_levels, ("high", "medium", "low"))

    def test_refresh_rewrites_a_catalog_written_by_an_older_build(self) -> None:
        """New model facts have to reach installs nobody re-saves settings on."""

        with tempfile.TemporaryDirectory() as temporary, patch.dict(
            os.environ, {"FINESUB_ENV_PROTECT": "0"}, clear=False
        ):
            settings = FineSubSettings(temporary)
            settings.bind_environment()
            settings.update_keys(
                {
                    "OPENAI_COMPAT_API_KEY": "compat-secret",
                    "OPENAI_COMPAT_BASE_URL": "https://vendor.example/v1",
                    "LLM_DEFAULT_PROVIDER": "openai-compat",
                    "LLM_DEFAULT_MODEL": "deepseek-v4-pro",
                }
            )
            catalog = Path(temporary) / "model_catalog.psv"
            catalog.write_text(
                catalog.read_text(encoding="utf-8").replace("1000000|384000", "128000|16384"),
                encoding="utf-8",
            )

            restarted = FineSubSettings(temporary)
            restarted.bind_environment()
            restarted.refresh_model_catalog()
            self.assertIn("|1000000|384000|", catalog.read_text(encoding="utf-8"))

    def test_unknown_model_row_clears_the_engines_refusal_line(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, patch.dict(
            os.environ, {"FINESUB_ENV_PROTECT": "0"}, clear=False
        ):
            settings = FineSubSettings(temporary)
            settings.bind_environment()
            settings.update_keys(
                {
                    "OPENAI_COMPAT_API_KEY": "compat-secret",
                    "OPENAI_COMPAT_BASE_URL": "https://vendor.example/v1",
                    "LLM_DEFAULT_PROVIDER": "openai-compat",
                    "LLM_DEFAULT_MODEL": "vendor-large",
                }
            )
            catalog = (Path(temporary) / "model_catalog.psv").read_text(encoding="utf-8")
            # No display name to borrow, and no reasoning parameter: an endpoint
            # nobody has declared may 400 on a field it does not know. The two
            # window figures sit at FineSub 0.5.0's warning line rather than
            # under it: the engine refuses to start a run whose bound model
            # declares `max_output < 32,000`, and the old 16,384 placeholder
            # would have stopped every task pinned to an endpoint this table
            # does not recognise.
            self.assertIn(
                "|vendor-large|vendor-large|194000|64000|false|false|false|false|1|70",
                catalog,
            )
            from finesub.llm.routing.capabilities import (
                WINDOW_REFUSE_INPUT,
                WINDOW_REFUSE_OUTPUT,
            )
            from nonoka_x.model_specs import UNKNOWN_MODEL_SPEC

            self.assertGreater(UNKNOWN_MODEL_SPEC.max_input_tokens, WINDOW_REFUSE_INPUT)
            self.assertGreater(
                UNKNOWN_MODEL_SPEC.max_output_tokens, WINDOW_REFUSE_OUTPUT
            )

    def test_a_pinned_provider_is_the_whole_chain(self) -> None:
        """Upstream 0.5.0: a pin replaces the chain, it does not lead it.

        `0003-desktop-model-routing.patch` used to keep the packaged
        candidates behind an API provider's selection, so an audio or video
        window stayed routable when the user had pinned a text-only endpoint.
        Upstream took the overlay and ruled the other way
        (`_preferred_bindings`, owner decision 2026-08-28): a call the pinned
        model cannot serve fails loudly instead of being served by a provider
        nobody chose. Nonoka X follows it, and moves the moment of truth
        forward instead -- `route_media_warnings` refuses the combination in
        the settings panel rather than mid-run.
        """

        with tempfile.TemporaryDirectory() as temporary, patch.dict(
            os.environ, {"FINESUB_ENV_PROTECT": "0"}, clear=False
        ):
            settings = FineSubSettings(temporary)
            settings.bind_environment()
            settings.update_keys(
                {
                    "OPENAI_COMPAT_API_KEY": "compat-secret",
                    "OPENAI_COMPAT_BASE_URL": "https://vendor.example/v1",
                    "LLM_DEFAULT_PROVIDER": "openai-compat",
                    "LLM_DEFAULT_MODEL": "vendor-large",
                }
            )

            from finesub.config import clear_config_cache
            from finesub.llm.routing.model_catalog import default_model_catalog
            from finesub.llm.routing.model_routes import default_model_routes

            clear_config_cache()
            default_model_catalog.cache_clear()
            default_model_routes.cache_clear()
            routes = default_model_routes()
            correction, _ = routes.resolve_binding(
                routes.active_preset_id, "correction-text", "quality"
            )
            self.assertEqual(len(correction.target_ids), 1)

    def test_compat_provider_without_a_base_url_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, patch.dict(
            os.environ, {"FINESUB_ENV_PROTECT": "0"}, clear=False
        ):
            settings = FineSubSettings(temporary)
            settings.bind_environment()
            with self.assertRaisesRegex(ValueError, "base URL is required"):
                settings.update_keys(
                    {
                        "ANTHROPIC_COMPAT_API_KEY": "compat-secret",
                        "LLM_DEFAULT_PROVIDER": "anthropic-compat",
                        "LLM_DEFAULT_MODEL": "vendor-sonnet",
                    }
                )

    def test_provider_without_a_key_cannot_be_routed_to(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, patch.dict(
            os.environ, {"FINESUB_ENV_PROTECT": "0"}, clear=False
        ):
            settings = FineSubSettings(temporary)
            settings.bind_environment()
            with self.assertRaisesRegex(ValueError, "API key is required"):
                settings.update_keys(
                    {"LLM_DEFAULT_PROVIDER": "openai", "LLM_DEFAULT_MODEL": "gpt-custom"}
                )


if __name__ == "__main__":
    unittest.main()
