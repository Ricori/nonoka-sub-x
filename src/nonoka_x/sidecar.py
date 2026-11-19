"""Loopback-only, session-token protected HTTP adapter for LocalProvider.

Start-up migrates an installation in the old data layout before any data is
opened (`nonoka_x.migration.startup`, WP-M2). While the migration's local
steps run, or cannot, the server is already up -- the desktop's handshake
does not wait for a migration -- but answers every route except
`GET /v1/migration` and `POST /v1/migration/retry` with `migrating` (503).
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import signal
import sys
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from . import engine_host
from . import settings as settings_module
from .download_route import PREFERENCES_ENVIRONMENT, install_app_proxy_policy
from .local_provider import LocalProvider, ProviderError
from .migration.startup import READY, StartupMigration
from .provision import RuntimeProvisioner
from .settings import FineSubSettings


def session_authorized(authorization: str, token: str) -> bool:
    return secrets.compare_digest(authorization, f"Bearer {token}")


class SidecarServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        address,
        provider: LocalProvider | None,
        token: str,
        migration: StartupMigration | None = None,
        open_data=None,
    ) -> None:
        super().__init__(address, SidecarHandler)
        #: None until the migration's local steps are done (`open_data` sets it).
        self.provider = provider
        self.token = token
        self.migration = migration
        self.open_data = open_data


class SidecarHandler(BaseHTTPRequestHandler):
    server: SidecarServer

    def log_message(self, format: str, *args) -> None:
        return

    def _json(self, status: int, value: object) -> None:
        body = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        return session_authorized(self.headers.get("Authorization", ""), self.server.token)

    def _body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length", "0"))
            value = json.loads(self.rfile.read(length) if length else b"{}")
        except (ValueError, json.JSONDecodeError) as exc:
            raise ProviderError("invalid_json", "Request body must be JSON") from exc
        if not isinstance(value, dict):
            raise ProviderError("invalid_json", "Request body must be a JSON object")
        return value

    @staticmethod
    def _engine_query(query: str) -> str | None:
        """`?engine=<id>`: the desktop's current transcription engine; None when not given."""

        value = parse_qs(query).get("engine", [""])[0].strip()
        return value or None

    @staticmethod
    def _layers_query(query: str) -> list[str]:
        """`?layers=python,models`: the layers the user ticked for removal as a whole; none when not given."""

        value = parse_qs(query).get("layers", [""])[0]
        return [part.strip() for part in value.split(",") if part.strip()]

    def _dispatch(self) -> None:
        if not self._authorized():
            self._json(401, {"error": {"code": "invalid_session", "message": "Invalid sidecar session token"}})
            return
        parsed = urlsplit(self.path)
        parts = [part for part in parsed.path.split("/") if part]
        migration = self.server.migration
        if parts == ["v1", "migration"] and self.command == "GET":
            self._json(200, migration.status() if migration is not None else {"schema": 1, "state": "not-needed", "dataOpen": True})
            return
        if parts == ["v1", "migration", "retry"] and self.command == "POST":
            if migration is None:
                raise ProviderError("not_found", "Route not found", http_status=404)
            self._json(202, migration.retry(self.server.open_data or (lambda: None)))
            return
        provider = self.server.provider
        if provider is None:
            raise ProviderError("migrating", "正在迁移旧版数据，完成之前其他功能不可用", http_status=503)
        if self.command == "GET" and parts == ["v1", "telemetry"]:
            self._json(200, {"events": provider.telemetry.pending()})
        elif self.command == "POST" and parts == ["v1", "telemetry", "ack"]:
            provider.telemetry.ack(self._body().get("ids", []))
            self._json(200, {})
        elif self.command == "GET" and parts == ["v1", "capabilities"]:
            self._json(200, provider.get_capabilities())
        elif self.command == "GET" and parts == ["v1", "runtime", "provision"]:
            self._json(200, provider.runtime_provision_status())
        elif self.command == "POST" and parts == ["v1", "runtime", "provision"]:
            self._json(202, provider.install_runtime(str(self._body().get("target") or "all")))
        elif self.command == "POST" and parts == ["v1", "runtime", "provision", "cancel"]:
            self._json(200, provider.cancel_runtime_install())
        elif self.command == "DELETE" and parts == ["v1", "runtime", "provision"]:
            self._json(200, provider.remove_runtime())
        elif self.command == "GET" and parts == ["v1", "runtime", "cache"]:
            self._json(200, provider.runtime_cache_status())
        elif self.command == "DELETE" and parts == ["v1", "runtime", "cache"]:
            self._json(200, provider.clear_runtime_cache())
        elif self.command == "GET" and parts == ["v1", "runtime", "provision", "unused"]:
            self._json(200, provider.runtime_cleanup_plan(self._engine_query(parsed.query)))
        elif self.command == "DELETE" and parts == ["v1", "runtime", "provision", "unused"]:
            self._json(200, provider.remove_unused_runtime(
                self._engine_query(parsed.query), self._layers_query(parsed.query)))
        elif self.command == "GET" and parts == ["v1", "settings"]:
            self._json(200, provider.get_settings())
        elif self.command == "POST" and parts == ["v1", "knowledge"]:
            self._json(200, provider.knowledge(self._body()))
        elif self.command == "PUT" and parts == ["v1", "settings", "keys"]:
            self._json(200, provider.update_keys(self._body().get("keys", {})))
        elif self.command == "PUT" and parts == ["v1", "settings", "cloud"]:
            self._json(200, provider.set_cloud_account(self._body()))
        elif self.command == "GET" and parts == ["v1", "tasks"]:
            limit = int(parse_qs(parsed.query).get("limit", ["100"])[0])
            self._json(200, provider.list_tasks(limit=limit))
        elif self.command == "POST" and parts == ["v1", "tasks"]:
            self._json(202, provider.start(self._body()))
        elif self.command == "POST" and parts == ["v1", "llm", "complete"]:
            self._json(200, provider.llm_complete(self._body()))
        elif self.command == "POST" and parts == ["v1", "documents", "project"]:
            self._json(200, provider.project_contents(self._body()))
        elif self.command == "POST" and parts == ["v1", "documents", "import"]:
            self._json(200, provider.import_axis(self._body()))
        elif len(parts) >= 3 and parts[:2] == ["v1", "documents"]:
            video_id = parts[2]
            if self.command == "GET" and len(parts) == 3:
                self._json(200, provider.document(video_id))
            elif self.command == "PUT" and len(parts) == 3:
                self._json(200, provider.save_document(video_id, self._body()))
            elif self.command == "GET" and parts[3:] == ["peaks"]:
                self._json(200, provider.document_peaks(video_id))
            elif self.command == "GET" and parts[3:] == ["axis"]:
                self._json(200, provider.document_axis(video_id))
            elif self.command == "PUT" and parts[3:] == ["axis"]:
                self._json(200, provider.set_document_axis(video_id, self._body()))
            else:
                raise ProviderError("not_found", "Route not found", http_status=404)
        elif len(parts) >= 3 and parts[:2] == ["v1", "tasks"]:
            task_id = parts[2]
            if self.command == "GET" and len(parts) == 3:
                self._json(200, provider.status(task_id))
            elif self.command == "GET" and parts[3:] == ["events"]:
                after = int(parse_qs(parsed.query).get("after", ["0"])[0])
                self._json(200, provider.events(task_id, after))
            elif self.command == "GET" and parts[3:] == ["artifacts"]:
                self._json(200, provider.artifacts(task_id))
            elif self.command == "POST" and parts[3:] == ["cancel"]:
                self._json(200, provider.cancel(task_id))
            elif self.command == "POST" and parts[3:] == ["retry"]:
                self._json(200, provider.retry(task_id))
            elif self.command == "POST" and parts[3:] == ["resume"]:
                self._json(200, provider.resume(task_id))
            else:
                raise ProviderError("not_found", "Route not found", http_status=404)
        else:
            raise ProviderError("not_found", "Route not found", http_status=404)

    def do_GET(self) -> None:
        self._handle()

    def do_POST(self) -> None:
        self._handle()

    def do_PUT(self) -> None:
        self._handle()

    def do_DELETE(self) -> None:
        self._handle()

    def _handle(self) -> None:
        try:
            self._dispatch()
        except ProviderError as exc:
            self._json(exc.http_status, {"error": {"code": exc.code, "message": str(exc)}})
        except (ValueError, IndexError) as exc:
            self._json(400, {"error": {"code": "invalid_request", "message": str(exc)}})
        except Exception:
            traceback.print_exc(file=sys.stderr)
            self._json(500, {"error": {"code": "internal_error", "message": "Sidecar request failed"}})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    # Where the engine packages are (`engine_host.default_engines_root()` when not given).
    parser.add_argument("--engines", type=Path, default=None)
    # Accepted and ignored: a desktop from before the engine packages still names the vendored engine.
    parser.add_argument("--vendor", type=Path, default=None, help=argparse.SUPPRESS)
    # Where the multi-gigabyte install root lives. Optional so an older or
    # hand-written launch line keeps the historical layout under --data-dir.
    parser.add_argument("--install-dir", type=Path, default=None)
    args = parser.parse_args(argv)
    token = secrets.token_urlsafe(32)
    # The desktop's proxy setting lives in its preferences.json; naming it here
    # hands it to every worker and installer spawned from this environment.
    os.environ.setdefault(PREFERENCES_ENVIRONMENT, str(args.data_dir / "preferences.json"))
    install_app_proxy_policy()
    engines = args.engines if args.engines is not None else engine_host.default_engines_root()
    # Before anything is opened: an installation in the old layout is migrated first (WP-M2).
    migration = StartupMigration(args.data_dir, install_dir=args.install_dir, engines=engines)
    ready = migration.begin() == READY
    server = SidecarServer(("127.0.0.1", 0), None, token, migration)

    def open_data() -> None:
        server.provider = open_provider(args.data_dir, engines, migration.install_dir())

    server.open_data = open_data
    if ready:
        open_data()
        migration.start_downloads()
    else:
        migration.start_local(open_data)
    print(json.dumps({"schema": 1, "host": "127.0.0.1", "port": server.server_port, "token": token}, separators=(",", ":")), flush=True)

    def stop(_signum, _frame) -> None:
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    try:
        server.serve_forever()
    finally:
        if server.provider is not None:
            server.provider.shutdown()
        server.server_close()
    return 0


def open_provider(data_dir: Path, engines: Path, install_dir: Path | None) -> LocalProvider:
    """Open the data: the settings, the runtime, the tasks. Only once the migration allows it."""

    settings = FineSubSettings(data_dir)
    settings.bind_environment()
    try:
        # A build that retires a model moves the saved routes off it before
        # anything is planned against them (the engines' files are derived per
        # process from whatever is saved).
        settings.refresh_model_routes()
    except Exception as exc:  # noqa: BLE001 - a stale route is not fatal
        print(f"model route refresh skipped: {exc}", file=sys.stderr, flush=True)
    provisioner = RuntimeProvisioner(data_dir, engines, install_dir)
    try:
        provisioner.ensure_required_tools()
    except Exception as exc:  # noqa: BLE001 - the engine works without it
        print(f"required tool install skipped: {exc}", file=sys.stderr, flush=True)
    provider = LocalProvider(data_dir / "tasks", engines, settings=settings, provisioner=provisioner)
    # The settings page's packaged model catalog is the correction package's
    # to read: asked of its resident process.
    settings_module.set_catalog_source(lambda: provider._service_call("catalog"))
    return provider


if __name__ == "__main__":
    raise SystemExit(main())
