"""Where Nonoka hands FineSub what it needs, and still reaches into its modules, in one file.

Nonoka's code outside the boundary files does not import `finesub` or
`finesub_bootstrap`, name a `FINESUB_*` variable or one of FineSub's files
(`docs/transcription-engines-plan.md` 4.2 item 12, 4.6 item 33; the boundary
test `tests/test_finesub_boundary.py`). Two jobs live here.

**Deriving what FineSub reads** (WP-R5; plan 4.3 items 22, 23; R0 document
2.5). Nonoka's `settings.json` and `secrets.json` are the only source of
truth. For every engine process the core describes what it is starting in a
neutral launch context (R0 document 1.15: paths, the model layer, the keys by
name, the LLM routes and endpoints) and this module turns it into FineSub's
files and variables:

- `transcription_environment`: the transcription half -- the model layer
  (`FINESUB_MODEL_DIR`, `HF_HOME`, `TORCH_HOME`), compiled acceleration off,
  numba's cache outside the read-only layer, `HF_TOKEN` when there is one.
  It has no file to write; its file variables name files that do not exist,
  which FineSub reads as "nothing configured".
- `correction_environment`: the correction half -- `finesub.toml` (the
  `[nonoka_models]` and `[llm.preferred_targets]` tables, byte for byte what
  the settings page used to write, D20), `model_catalog.toml`, and a `.env`
  only when the Gemini base URL was changed (D3); every key as a process
  variable and never on disk, with `FINESUB_ENV_PROTECT=0` so the engine
  only reads the derived files (D22).
- `combined_environment`: both halves in one process, which is what the task
  worker still is until the engine packages split (WP-P1).

The files go in `engines\\<package>\\run\\<process>\\`, one directory per
process (`EngineRun`), removed when the process ends and swept at start-up
when their owner is gone (D6). The two halves derive only what each needs, so
WP-P1 can move each into its package as it is; until then the core calls them
in the sidecar just before it starts a worker.

**Patching FineSub in-process** -- route FineSub's downloads and API calls the
way Nonoka routes its own -- behind the same functions it had before:

- `install_app_proxy_policy`: FineSub's `http_client` discovers routes through
  Nonoka's proxy setting (`nonoka_x.runtime.fetch`), and `finesub.api_http`
  sends API calls the same way. Moved from
  `nonoka_x.download_route.install_app_proxy_policy`.
- `install_download_policy`: FineSub never picks a regional mirror by itself
  (`download_routes.resolve_region` always answers `global`), and its file and
  model downloads take Nonoka's route ladder (`nonoka_x.download_route.climb`).
  Moved from `nonoka_x.download_route.install_proxy_route_policy` and
  `_install_download_ladder`.
- `install_cloud_digest_source`: FineSub's release-asset lookup asks the Nonoka
  cloud before GitHub. Moved from `nonoka_x.asset_digest`.

The code moved verbatim (WP-R2, user decision 2026-11-11), with two changes
that follow from the same package: the ladder has no mirror or backup rung any
more, and the app policy reads the system proxy, the liveness probe and the
custom proxy from `nonoka_x.runtime.fetch` instead of FineSub's copies (the
same code). `nonoka_x.download_route` and `nonoka_x.asset_digest` keep their
public functions and call in here, so no caller changed. Everything here is
idempotent and does nothing when FineSub cannot be imported.

WP-P1 moves this file into the engine packages.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import secrets
import sys
from typing import Any, TypeVar
from urllib.parse import urlparse
import time

from nonoka_x import download_route
from nonoka_x.download_route import DownloadStage, DownloadUnreachable
from nonoka_x.runtime import fetch, fsops

_T = TypeVar("_T")

_INSTALLED_ATTRIBUTE = "_nonoka_proxy_route_policy"
_APP_POLICY_ATTRIBUTE = "_nonoka_app_proxy_policy"
_LADDER_ATTRIBUTE = "_nonoka_download_ladder"
_CLOUD_DIGEST_ATTRIBUTE = "_nonoka_cloud_digest_source"


# -- what FineSub reads, derived per process (WP-R5) --------------------------------------------------------------

TRANSCRIPTION_PACKAGE = "finesub-transcribe"
CORRECTION_PACKAGE = "finesub-correct"
#: The compile caches under the big-file root's cache directory (`NUMBA_CACHE_DIR` below): the
#: settings page's "clear the cache" empties them with the downloads; the next run recompiles.
COMPILE_CACHES = (f"{TRANSCRIPTION_PACKAGE}/numba",)

#: The derived files in a run directory, by the variable that points FineSub at each.
CONFIG_NAME = "finesub.toml"
CATALOG_NAME = "model_catalog.toml"
ENV_NAME = ".env"
#: Held by the process that owns a run directory for as long as the directory is live.
OWNER_LOCK_NAME = "owner.lock"
#: The run directory the sidecar's own FineSub imports are pointed at: never created.
HOST_RUN_NAME = "host"
#: Where WP-R4 derived the files until WP-R5 (owner 2026-11-12); swept at start-up.
TRANSITIONAL_DIRECTORY = "finesub-shared"
#: Inside the model layer: the Hugging Face cache (`nonoka_x.runtime.layer_manifest.HF_HOME_DIR`).
HF_HOME_DIRECTORY = "huggingface"

CATALOG_HEADER = "# Generated by Nonoka X from the saved model routes; edits are overwritten.\n"

#: Compiled acceleration is off (owner decision 2026-11-11, `85ae2b9`): the vocal
#: separator's AOTI and JIT layers and the referee's compiled step give results
#: that differ from the eager path and depend on the machine, the input length
#: and whether a compiler is installed. Eager is the one result every machine
#: reproduces. Both run in the transcription half.
ACCELERATION_OFF: Mapping[str, str] = {"FINESUB_SEPARATOR_ACCEL": "0", "FINESUB_REFEREE_ACCEL": "0"}

#: The prefix of every variable FineSub reads of its own. None reaches an engine
#: process from outside: the bridge writes each one it needs.
FINESUB_VARIABLE_PREFIX = "FINESUB_"


def custom_target(provider: str, model: str) -> str:
    """The catalog target id of a route over an HTTP transport: `nonoka-<provider>-<sha256[:12]>`."""

    digest = hashlib.sha256(f"{provider}\0{model}".encode("utf-8")).hexdigest()[:12]
    return f"nonoka-{provider}-{digest}"


def _file_variables(run: Path) -> dict[str, str]:
    """The three files FineSub looks for, all in this process's run directory.

    Always named, whether or not the file exists: an unset variable sends
    FineSub looking in its default locations (a source checkout, the user's
    profile), and a named file that is not there reads as "nothing configured".
    """

    return {
        "FINESUB_CHECKOUT_DATA": "0",
        "FINESUB_CONFIG_FILE": str(run / CONFIG_NAME),
        "FINESUB_MODEL_CATALOG": str(run / CATALOG_NAME),
        "FINESUB_ENV_FILE": str(run / ENV_NAME),
        "FINESUB_ENV_PROTECT": "0",
    }


def _secret_variables(context: Mapping[str, Any]) -> dict[str, str]:
    """The keys the core handed this package, as process variables (D3): the engine reads them before any file."""

    return {str(name): str(value) for name, value in (context.get("secrets") or {}).items() if value}


def transcription_environment(context: Mapping[str, Any]) -> dict[str, str]:
    """The variables of FineSub's transcription half, from its launch context.

    `context`: `paths.run` (this process's run directory), `paths.state_file`;
    `runtime.models` (the model layer's directory), `runtime.cache` (this
    package's directory under the big-file root's `cache\\`, or absent);
    `secrets` (only `HF_TOKEN`, and only when the user set one).

    Writes nothing: the transcription half has no file of its own to read.
    """

    paths = context["paths"]
    models = Path(context["runtime"]["models"])
    environment = _file_variables(Path(paths["run"]))
    environment.update({
        "FINESUB_STATE_FILE": str(paths["state_file"]),
        "FINESUB_MODEL_DIR": str(models),
        "HF_HOME": str(models / HF_HOME_DIRECTORY),
        "TORCH_HOME": str(models / "torch"),
        **ACCELERATION_OFF,
    })
    cache = context["runtime"].get("cache")
    if cache:
        # numba caches compiled functions beside their source unless told
        # otherwise, which is inside the read-only Python layer. Same machine
        # code either way; kept across runs so it is compiled once.
        environment["NUMBA_CACHE_DIR"] = str(Path(cache) / "numba")
    environment.update(_secret_variables(context))
    return environment


def correction_environment(context: Mapping[str, Any]) -> dict[str, str]:
    """Derive the correction half's files into `paths.run` and return its variables.

    `context`: `paths.run`, `paths.state_file`, `paths.knowledge_root`;
    `secrets` (the LLM and search keys by name); `llm` (see `derive_llm_files`).
    """

    paths = context["paths"]
    run = Path(paths["run"])
    derive_llm_files(run, context.get("llm") or {})
    environment = _file_variables(run)
    environment.update({
        "FINESUB_STATE_FILE": str(paths["state_file"]),
        "FINESUB_KNOWLEDGE_ROOT": str(paths["knowledge_root"]),
    })
    environment.update(_secret_variables(context))
    return environment


def combined_environment(transcription: Mapping[str, Any], correction: Mapping[str, Any]) -> dict[str, str]:
    """One process running both halves (the task worker until WP-P1): the correction half's files win."""

    environment = transcription_environment(transcription)
    environment.update(correction_environment(correction))
    return environment


def without_inherited(environment: Mapping[str, str]) -> dict[str, str]:
    """`environment` with every `FINESUB_*` variable taken out, so only the bridge's reach the engine."""

    return {name: value for name, value in environment.items() if not name.upper().startswith(FINESUB_VARIABLE_PREFIX)}


# The files ---------------------------------------------------------------------------------------------------------


def _toml_value(value: Any) -> str:
    """One TOML value. JSON's string and array forms are valid TOML; a bool is `true` / `false` in both."""

    return json.dumps(value, ensure_ascii=False)


def derive_llm_files(run: Path, llm: Mapping[str, Any]) -> None:
    """Write `model_catalog.toml`, `finesub.toml` and, when needed, `.env` into `run`.

    `llm`:
    - `endpoints`: `[{provider, model, row}]`, one per route over an HTTP
      transport that has an address, in catalog order; `row` is the catalog
      table's keys in order (the core works them out from `model_specs`).
    - `routes`: `{route: {provider, model}}` for `default`, `correction`,
      `research`, `knowledge`, in that order -- the `[nonoka_models]` table.
    - `pins`: `[{group, provider, model, kind, tier, offered}]` in order, one
      per engine task group a route binds; `kind` is `gemini`, `local_agent`
      or `http`, `offered` the models the desktop offers a local agent.
      Resolved to catalog targets here -- `[llm.preferred_targets]`.
    - `gemini_base_url`: present only when the user changed it.

    The texts are the ones `FineSubSettings` wrote before WP-R5 (the quality
    gate's environment baseline compares them byte for byte).
    """

    run.mkdir(parents=True, exist_ok=True)
    emitted: set[str] = set()
    tables = [CATALOG_HEADER]
    for endpoint in llm.get("endpoints") or ():
        target = custom_target(str(endpoint["provider"]), str(endpoint["model"]))
        emitted.add(target)
        tables.append(
            f"[models.{_toml_value(target)}]\n"
            + "".join(f"{key} = {_toml_value(value)}\n" for key, value in endpoint["row"].items())
        )
    fsops.write_atomic(run / CATALOG_NAME, "\n".join(tables), newline="")

    routes = [
        (f"{route}_{field}", entry.get(field))
        for route, entry in (llm.get("routes") or {}).items()
        for field in ("provider", "model")
    ]
    preferred = [(pin["group"], _pin_target(pin, emitted)) for pin in llm.get("pins") or ()]
    blocks = [
        f"[{name}]\n" + "".join(f"{key} = {json.dumps(value)}\n" for key, value in entries)
        for name, entries in (
            ("nonoka_models", [(key, value) for key, value in routes if value]),
            ("llm.preferred_targets", [(key, value) for key, value in preferred if value]),
        )
        if entries
    ]
    config = run / CONFIG_NAME
    if blocks:
        fsops.write_atomic(config, "\n".join(blocks), newline="")
    else:
        config.unlink(missing_ok=True)

    env_file = run / ENV_NAME
    gemini_base_url = str(llm.get("gemini_base_url") or "")
    if gemini_base_url:
        # The one setting the engine reads from its key file only
        # (`llm_runtime.gemini_base_url`); no key ever goes in it.
        fsops.write_atomic(env_file, f"GEMINI_BASE_URL={gemini_base_url}\n", newline="")
    else:
        env_file.unlink(missing_ok=True)


def _pin_target(pin: Mapping[str, Any], emitted: set[str]) -> str | None:
    """The catalog target a route's pin names, or None to leave the group to the engine's own routing."""

    provider, model = str(pin.get("provider") or ""), str(pin.get("model") or "")
    if not provider or not model:
        return None
    kind = pin.get("kind")
    if kind == "gemini":
        return gemini_targets(str(pin["tier"])).get(model)
    if kind == "local_agent":
        offered = pin.get("offered")
        if offered is not None and model not in offered:
            return None
        return {fact.api_model_id: target for fact, target in local_agent_targets(str(pin["tier"]))}.get(model)
    if kind == "http":
        # Only if its catalog row was written: a pin on a target the catalog
        # does not carry is worse than falling back.
        target = custom_target(provider, model)
        return target if target in emitted else None
    return None


def gemini_targets(tier: str) -> dict[str, str]:
    """`api_model_id -> fact id` of the packaged Gemini models of one key pool (`GEMINI_FREE`, `GEMINI_PAID`)."""

    from finesub.llm.routing.model_catalog import default_model_catalog

    return {
        entry.api_model_id: entry.fact_id
        for entry in default_model_catalog()
        if entry.provider_tier == tier and not entry.self_reported
    }


def local_agent_targets(tier: str) -> list[tuple[Any, str]]:
    """(fact, target id) of every packaged local-agent model of `tier`, one per model, in roster order.

    Read without the user's overlay: the packaged declaration is the only
    source of these targets.
    """

    from finesub.llm.routing.model_routes import load_model_routes

    routes = load_model_routes(user_config={})
    by_model: dict[str, tuple[Any, str]] = {}
    for target in routes.targets.values():
        if target.backend != "local_agent":
            continue
        fact = routes.facts.get(target.fact_id)
        if fact is None or fact.provider_tier != tier:
            continue
        by_model[fact.api_model_id] = (fact, target.id)
    return list(by_model.values())


# Run directories ---------------------------------------------------------------------------------------------------


class EngineRun:
    """One engine process's run directory, `engines\\<package>\\run\\<process>\\` (D6).

    Claimed before the process starts and closed when it has ended: closing
    releases the owner lock and deletes the directory. The lock is what makes
    a directory left behind by a crash recognisable (`sweep_runs`).
    """

    def __init__(self, directory: Path, stack: ExitStack) -> None:
        self.directory = directory
        self._stack = stack

    @classmethod
    def claim(cls, package_dir: Path) -> "EngineRun":
        directory = package_dir / "run" / f"{os.getpid()}-{secrets.token_hex(6)}"
        directory.mkdir(parents=True)
        stack = ExitStack()
        try:
            stack.enter_context(fsops.holding_lock(directory / OWNER_LOCK_NAME, timeout=0))
        except BaseException:
            stack.close()
            fsops.remove_tree(directory)
            raise
        return cls(directory, stack)

    def close(self) -> None:
        self._stack.close()
        try:
            fsops.remove_tree(self.directory)
        except OSError:
            # A file still open in a process that is going away: the next
            # start-up sweep takes it.
            pass

    def __enter__(self) -> "EngineRun":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


def sweep_runs(package_dirs: Iterable[Path]) -> list[Path]:
    """Delete run directories whose owner is gone; returns what was removed.

    A directory is live while its owner holds `owner.lock`; one without the
    lock file never got that far.
    """

    removed: list[Path] = []
    for package_dir in package_dirs:
        root = package_dir / "run"
        try:
            entries = sorted(root.iterdir())
        except OSError:
            continue
        for entry in entries:
            if entry.name == HOST_RUN_NAME or not entry.is_dir() or fsops.is_directory_link(entry):
                continue
            if not fsops.try_lock(entry / OWNER_LOCK_NAME):
                continue
            try:
                fsops.remove_tree(entry)
            except OSError:
                continue
            removed.append(entry)
    return removed


def bind_host_process(engines_dir: Path) -> None:
    """Point the sidecar's own FineSub imports at nothing but FineSub's packaged defaults.

    Until WP-P1 the sidecar still imports FineSub in-process (model options,
    local-agent targets, the GPU tier choices, speaker products); none of
    that reads the user's routes or keys. Named files that never exist keep
    FineSub from looking in a source checkout or the user's profile. Engine
    processes do not inherit any of this (`without_inherited`).

    Also removes what WP-R4's transitional derivation left behind
    (`engines\\finesub-shared\\`, a sealed copy of the keys; owner 2026-11-13).
    """

    os.environ.update(_file_variables(engines_dir / CORRECTION_PACKAGE / "run" / HOST_RUN_NAME))
    transitional = engines_dir / TRANSITIONAL_DIRECTORY
    if os.path.lexists(transitional):
        try:
            fsops.remove_tree(transitional)
        except OSError as error:
            print(f"could not remove {transitional}: {error}", file=sys.stderr, flush=True)


# -- the app's proxy setting -------------------------------------------------------------------------------------


def install_app_proxy_policy() -> None:
    """Make FineSub's route discovery and its LLM layer follow the app's proxy setting.

    * `finesub_bootstrap.http_client`: a custom proxy is the only candidate;
      without one, the system proxy and the environment as before. Its
      liveness probe still skips a local proxy whose client is not running.
      Every route lookup also keeps `os.environ` on the live route
      (`fetch.sync_environment`).
    * `finesub.api_http`: API calls take the same route instead of httpx's own
      `trust_env` lookup. An explicit `[api] proxy` in config.toml still wins.
      A SOCKS route goes direct with a warning: the engine refuses SOCKS.

    Expects `fetch.install_app_policy()` to have run (it holds the environment's
    own proxies from before the policy started rewriting them).
    """

    try:
        from finesub_bootstrap import http_client
    except ImportError:
        return
    if getattr(http_client._environment_proxies, _APP_POLICY_ATTRIBUTE, False):
        return

    def environment_proxies() -> list[str]:
        return fetch.policy_environment_proxies()

    setattr(environment_proxies, _APP_POLICY_ATTRIBUTE, True)
    http_client._windows_proxy = fetch.policy_system_proxy
    http_client._environment_proxies = environment_proxies

    try:
        from finesub import api_http
    except Exception:  # noqa: BLE001 - the LLM layer is optional in installer processes
        api_http = None
    if api_http is not None:
        original_configured = api_http.configured_proxy
        warned_socks = False

        def configured_proxy() -> str | None:
            nonlocal warned_socks
            explicit = original_configured()
            if explicit is not None:
                return explicit
            proxy = download_route.live_proxy()
            if proxy and proxy.lower().startswith("socks"):
                if not warned_socks:
                    warned_socks = True
                    print(
                        "Warning: the transcription engine does not support SOCKS proxies; "
                        "its API calls go direct. Use the proxy's HTTP port to route them.",
                        file=sys.stderr,
                    )
                return api_http.DIRECT
            return proxy or api_http.DIRECT

        api_http.configured_proxy = configured_proxy


# -- FineSub's downloads -----------------------------------------------------------------------------------------


def install_download_policy() -> None:
    """Put FineSub's downloads in this process on Nonoka's ladder.

    Every engine call site imports `resolve_region` from the module at call
    time, so replacing the module attribute reaches all of them in this
    process. Each process that downloads installs it for itself; it is
    idempotent and the ladder is worked out per download, so turning the proxy
    on or off takes effect at the next one without a restart.
    """

    try:
        from finesub_bootstrap import download_routes
    except ImportError:
        return

    original = download_routes.resolve_region
    if not getattr(original, _INSTALLED_ATTRIBUTE, False):

        def resolve_region(data_root, *args: Any, **kwargs: Any):
            return download_routes.RouteDecision(region="global", source="ladder")

        setattr(resolve_region, _INSTALLED_ATTRIBUTE, True)
        resolve_region.__doc__ = original.__doc__
        download_routes.resolve_region = resolve_region
    _install_download_ladder()


def _seed_from_offline_pack(asset, destination: Path, progress, should_pause) -> None:
    """Put `asset` where the downloader looks first, if the offline package has it.

    The downloader then hashes it like any finished download and goes no
    further, so a package holding the wrong bytes cannot get them installed.
    """

    from finesub_bootstrap import downloader

    source = download_route.offline_file(asset.sha256)
    if source is None or (destination.is_file() and downloader._sha256(destination) == asset.sha256):
        return
    started = time.monotonic()

    def report(copied: int) -> None:
        if should_pause is not None and should_pause():
            raise downloader.DownloadPaused("Download paused")
        elapsed = max(time.monotonic() - started, 1e-6)
        progress(downloader.DownloadProgress(downloaded=copied, total=asset.size, bytes_per_second=copied / elapsed))

    download_route._copy_into_place(source, destination, report)


def _file_advances(stage: DownloadStage, error: Exception) -> bool:
    """Which failures of a verified file download move to the next rung.

    Wrong bytes never do: every rung fetches from the official source, and
    another route would fetch the same wrong file.
    """

    from finesub_bootstrap.downloader import DownloadError
    from finesub_bootstrap.model_fetch import is_mirror_failure

    if isinstance(error, DownloadError):
        return False
    return is_mirror_failure(error) or download_route.is_transport_failure(error)


def _laddered_download(download: Callable[..., _T]) -> Callable[..., _T]:
    """`_download_locked`, climbing the ladder unless a caller already is."""

    def download_locked(asset, destination, progress, should_pause):
        _seed_from_offline_pack(asset, destination, progress, should_pause)
        if download_route.current_stage() is not None:
            return download(asset, destination, progress, should_pause)

        def attempt(stage: DownloadStage):
            with download_route.pinned_stage(stage):
                return download(asset, destination, progress, should_pause)

        try:
            return download_route.climb(download_route.download_stages(), attempt, advances=_file_advances)
        except DownloadUnreachable as error:
            if download_route.offline_pack() is None:
                raise
            raise DownloadUnreachable(f"离线资源包里没有 {Path(urlparse(asset.url).path).name}，{error}") from error

    return download_locked


def _fingerprint(path: Path) -> tuple[int, int] | None:
    try:
        status = path.stat()
    except OSError:
        return None
    return status.st_mtime_ns, status.st_size


def _laddered_unverified(fetch_file: Callable[..., None]) -> Callable[..., None]:
    """`_fetch_unverified`, which reports nothing: a written file means it worked."""

    def fetch_unverified(url, destination, proxy, *, data_root=None) -> None:
        root = download_route.offline_pack()
        source = root / "unverified" / Path(urlparse(url).path).name if root is not None else None
        if source is not None and source.is_file():
            download_route._copy_into_place(source, destination)
            return
        for stage in download_route.download_stages():
            before = _fingerprint(destination)
            with download_route.pinned_stage(stage):
                fetch_file(url, destination, "", data_root=None)
            if _fingerprint(destination) != before:
                return

    return fetch_unverified


def _install_download_ladder() -> None:
    """Route FineSub's file and model downloads through `download_route.climb`.

    `_download_locked` rather than `download_asset`, which callers import by
    name: the wrapper is then the one every caller already reaches.
    """

    from finesub_bootstrap import downloader, http_client, model_fetch

    if getattr(downloader._download_locked, _LADDER_ATTRIBUTE, False):
        return
    routes = http_client.network_routes
    create_client = downloader.create_client

    def network_routes():
        stage = download_route.current_stage()
        return routes() if stage is None else [http_client.NetworkRoute(stage.label, stage.proxy)]

    def watched_client(route, *, timeout, headers=None):
        client = create_client(route, timeout=timeout, headers=headers)
        if download_route.speed_watched():
            client.event_hooks["response"].append(download_route.meter_response)
        return client

    def fetch_with_fallback(fetch_model, *, base_environment, data_root, region, is_retryable=None) -> None:
        download_route.climb_huggingface(fetch_model, base_environment, is_retryable=is_retryable)

    download_locked = _laddered_download(downloader._download_locked)
    setattr(download_locked, _LADDER_ATTRIBUTE, True)
    downloader._download_locked = download_locked
    downloader.network_routes = network_routes
    downloader.create_client = watched_client
    http_client.network_routes = network_routes
    model_fetch._fetch_unverified = _laddered_unverified(model_fetch._fetch_unverified)
    model_fetch.fetch_with_fallback = fetch_with_fallback


# -- release digests ---------------------------------------------------------------------------------------------


def install_cloud_digest_source(cloud_asset: Callable[[str, str, str, str], dict | None]) -> None:
    """Wrap `asset_resolve._find_asset` so `cloud_asset` is asked before GitHub.

    `resolve_asset` looks `_find_asset` up in its module at call time, so
    replacing the module attribute reaches it. Idempotent.
    """

    try:
        from finesub_bootstrap import asset_resolve
    except ImportError:
        return

    original = asset_resolve._find_asset
    if getattr(original, _CLOUD_DIGEST_ATTRIBUTE, False):
        return

    def _find_asset(owner: str, repo: str, tag: str, name: str) -> dict:
        entry = cloud_asset(owner, repo, tag, name)
        return entry if entry is not None else original(owner, repo, tag, name)

    setattr(_find_asset, _CLOUD_DIGEST_ATTRIBUTE, True)
    _find_asset.__doc__ = original.__doc__
    asset_resolve._find_asset = _find_asset


__all__ = [
    "CORRECTION_PACKAGE",
    "TRANSCRIPTION_PACKAGE",
    "EngineRun",
    "bind_host_process",
    "combined_environment",
    "correction_environment",
    "custom_target",
    "derive_llm_files",
    "gemini_targets",
    "install_app_proxy_policy",
    "install_cloud_digest_source",
    "install_download_policy",
    "local_agent_targets",
    "sweep_runs",
    "transcription_environment",
    "without_inherited",
]
