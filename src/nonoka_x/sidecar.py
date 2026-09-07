"""Loopback-only, session-token protected HTTP adapter for LocalProvider."""

from __future__ import annotations

import argparse
import json
import secrets
import signal
import sys
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .local_provider import LocalProvider, ProviderError
from .provision import RuntimeProvisioner
from .settings import FineSubSettings


def session_authorized(authorization: str, token: str) -> bool:
    return secrets.compare_digest(authorization, f"Bearer {token}")


class SidecarServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, provider: LocalProvider, token: str) -> None:
        super().__init__(address, SidecarHandler)
        self.provider = provider
        self.token = token


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

    def _dispatch(self) -> None:
        if not self._authorized():
            self._json(401, {"error": {"code": "invalid_session", "message": "Invalid sidecar session token"}})
            return
        parsed = urlsplit(self.path)
        parts = [part for part in parsed.path.split("/") if part]
        provider = self.server.provider
        if self.command == "GET" and parts == ["v1", "capabilities"]:
            self._json(200, provider.get_capabilities())
        elif self.command == "GET" and parts == ["v1", "runtime", "provision"]:
            self._json(200, provider.runtime_provision_status())
        elif self.command == "POST" and parts == ["v1", "runtime", "provision"]:
            self._json(202, provider.install_runtime(str(self._body().get("target") or "all")))
        elif self.command == "POST" and parts == ["v1", "runtime", "provision", "cancel"]:
            self._json(200, provider.cancel_runtime_install())
        elif self.command == "DELETE" and parts == ["v1", "runtime", "provision"]:
            self._json(200, provider.remove_runtime())
        elif self.command == "DELETE" and parts == ["v1", "runtime", "provision", "group"]:
            self._json(200, provider.remove_runtime_group(str(self._body().get("target") or "")))
        elif self.command == "GET" and parts == ["v1", "settings"]:
            self._json(200, provider.get_settings())
        elif self.command == "PUT" and parts == ["v1", "settings", "keys"]:
            self._json(200, provider.update_keys(self._body().get("keys", {})))
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
    parser.add_argument("--vendor", type=Path, required=True)
    # Where the multi-gigabyte install root lives. Optional so an older or
    # hand-written launch line keeps the historical layout under --data-dir.
    parser.add_argument("--install-dir", type=Path, default=None)
    args = parser.parse_args(argv)
    token = secrets.token_urlsafe(32)
    settings = FineSubSettings(args.data_dir)
    settings.bind_environment()
    try:
        # The generated model catalog is only as current as the last settings
        # save; a build that ships new model facts has to restate them itself,
        # or an existing install keeps planning against the old ones.
        settings.refresh_model_catalog()
    except Exception as exc:  # noqa: BLE001 - a stale catalog is not fatal
        print(f"model catalog refresh skipped: {exc}", file=sys.stderr, flush=True)
    provisioner = RuntimeProvisioner(args.data_dir, args.vendor, args.install_dir)
    provider = LocalProvider(args.data_dir / "tasks", args.vendor, settings=settings, provisioner=provisioner)
    server = SidecarServer(("127.0.0.1", 0), provider, token)
    print(json.dumps({"schema": 1, "host": "127.0.0.1", "port": server.server_port, "token": token}, separators=(",", ":")), flush=True)

    def stop(_signum, _frame) -> None:
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    try:
        server.serve_forever()
    finally:
        provider.shutdown()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
