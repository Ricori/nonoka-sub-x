#!/usr/bin/env python3
"""Destructive-to-test-data-only end-to-end check for the deployed Modal app."""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import re
import secrets
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import modal


DEFAULT_ENDPOINT = "https://ricori--nonoka-x-cloud-api.modal.run"

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
MODAL_BACKEND_ROOT = REPOSITORY_ROOT / "modal_backend"
if str(MODAL_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(MODAL_BACKEND_ROOT))


class ApiError(RuntimeError):
    def __init__(self, status: int, body: str) -> None:
        super().__init__(f"HTTP {status}: {body}")
        self.status = status
        self.body = body


def temporary_key_record(login_key: str) -> dict[str, Any]:
    """A one-run key in the same plaintext shape the deployed API verifies."""

    return {
        "id": login_key,
        "key": login_key,
        "name": "Modal E2E",
        "video_ids": [],
        "remaining": 1,
        "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }


def request(
    endpoint: str,
    method: str,
    path: str,
    *,
    key: str = "",
    payload: dict[str, Any] | None = None,
    timeout: float = 60,
) -> tuple[int, bytes]:
    data = None
    headers = {"Accept": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    attempts = 4 if method == "GET" else 1
    for attempt in range(attempts):
        req = urllib.request.Request(
            endpoint + path, data=data, headers=headers, method=method
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise ApiError(exc.code, body) from exc
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            if attempt + 1 >= attempts:
                raise
            time.sleep(1 + attempt)
    raise AssertionError("unreachable")


def request_json(
    endpoint: str,
    method: str,
    path: str,
    *,
    key: str = "",
    payload: dict[str, Any] | None = None,
    timeout: float = 60,
) -> dict[str, Any]:
    _status, body = request(
        endpoint, method, path, key=key, payload=payload, timeout=timeout
    )
    value = json.loads(body)
    if not isinstance(value, dict):
        raise RuntimeError(f"Expected JSON object from {path}")
    return value


def synthesize_japanese_audio(directory: Path, repetitions: int) -> tuple[Path, float]:
    output = directory / "nonoka-x-e2e.aiff"
    phrase = (
        "こんにちは。これはフィノカ字幕処理の端末間テスト音声です。"
        "音声認識、字幕の修正、そして中国語への翻訳を確認します。"
    )
    subprocess.run(
        ["/usr/bin/say", "-v", "Kyoko", "-r", "190", "-o", str(output), phrase * repetitions],
        check=True,
    )
    info = subprocess.run(
        ["/usr/bin/afinfo", str(output)],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    match = re.search(r"estimated duration:\s*([0-9.]+) sec", info)
    duration = float(match.group(1)) if match else 0.0
    if duration <= 0 or output.stat().st_size <= 4096:
        raise RuntimeError("macOS speech synthesis produced no audio")
    return output, duration


def put_upload(url: str, path: Path) -> None:
    # curl is more tolerant than urllib of the local HTTPS proxy closing an
    # upload socket while a request body is still being written.
    try:
        subprocess.run(
            [
                "/usr/bin/curl",
                "--fail",
                "--show-error",
                "--silent",
                "--retry",
                "3",
                "--retry-all-errors",
                "--connect-timeout",
                "20",
                "--max-time",
                "180",
                "--request",
                "PUT",
                "--header",
                "Content-Type: audio/mp4",
                "--upload-file",
                str(path),
                url,
            ],
            check=True,
        )
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f"R2 upload failed (curl exit {exc.returncode})") from None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    parser.add_argument("--timeout-minutes", type=float, default=40)
    parser.add_argument("--repetitions", type=int, default=8)
    args = parser.parse_args()

    endpoint = args.endpoint.rstrip("/")
    login_key = "e2e_" + secrets.token_urlsafe(32)
    owner_id = login_key
    keys = modal.Dict.from_name("nonoka-x-keys")
    videos = modal.Dict.from_name("nonoka-x-videos")
    uploads = modal.Dict.from_name("nonoka-x-uploads")
    controls = modal.Dict.from_name("nonoka-x-task-controls")
    artifacts = modal.Volume.from_name("nonoka-x-artifacts")
    object_id = ""
    task_id = ""
    latest_state = ""

    keys.put(owner_id, temporary_key_record(login_key))
    print("[e2e] temporary login key registered", flush=True)

    try:
        try:
            request_json(endpoint, "GET", "/v1/session", key="invalid-e2e-key")
            raise RuntimeError("invalid key unexpectedly authenticated")
        except ApiError as exc:
            if exc.status != 401:
                raise

        capabilities = request_json(endpoint, "GET", "/v1/capabilities", key=login_key)
        session = request_json(endpoint, "GET", "/v1/session", key=login_key)
        if capabilities.get("features", {}).get("resume") is not True:
            raise RuntimeError("cloud resume capability is missing")
        if session.get("remaining") != 1:
            raise RuntimeError(f"unexpected initial quota: {session}")
        print("[e2e] auth, session and capabilities passed", flush=True)

        def session_probe(_index: int) -> bool:
            return request_json(endpoint, "GET", "/v1/session", key=login_key).get("authenticated") is True

        with concurrent.futures.ThreadPoolExecutor(max_workers=32) as pool:
            concurrent_results = list(pool.map(session_probe, range(64)))
        if not all(concurrent_results):
            raise RuntimeError("one or more concurrent API requests failed")
        print("[e2e] 64 concurrent authenticated API requests passed", flush=True)

        # Image verification is a test concern, so run it in an anonymous,
        # temporary Modal app instead of keeping a test function deployed in
        # the production app.
        from nonoka_x_modal.testing.verify_container import (
            app as verify_app,
            verify_patched_ctranslate2,
        )

        with verify_app.run():
            verify = verify_patched_ctranslate2.remote()
        if verify.get("fwRefineTrace") is not True:
            raise RuntimeError(f"patched CTranslate2 verification failed: {verify}")
        print(f"[e2e] patched CTranslate2 runtime passed ({verify.get('version')})", flush=True)

        with tempfile.TemporaryDirectory(prefix="nonoka-x-modal-e2e-") as temporary:
            audio, duration = synthesize_japanese_audio(Path(temporary), args.repetitions)
            fingerprint = hashlib.sha256(audio.read_bytes()).hexdigest()
            upload = request_json(
                endpoint,
                "POST",
                "/v1/uploads/init",
                key=login_key,
                payload={"filename": "nonoka-x-e2e.m4a", "bytes": audio.stat().st_size},
            )
            object_id = str(upload["objectId"])
            put_upload(str(upload["uploadUrl"]), audio)
            print(f"[e2e] synthesized {duration:.1f}s audio uploaded to R2", flush=True)

            task = request_json(
                endpoint,
                "POST",
                "/v1/tasks",
                key=login_key,
                payload={
                    "schema": 1,
                    "provider": "cloud",
                    "source": {
                        "kind": "uploaded_audio",
                        "object_id": object_id,
                        "title": "Nonoka X Modal E2E",
                        "fingerprint": fingerprint,
                        "duration": duration,
                    },
                    "target": "final-srt",
                    "language": "ja",
                    "device": "cuda",
                    "gpu_tier": "standard",
                    "correction": {
                        "enabled": True,
                        "media": "text",
                        "retrieval": "none",
                        "difficulty": "efficiency",
                        "fast": "on",
                        "extra_info": "端到端测试音频。",
                        "extra_style": "简体中文，忠实简洁。",
                    },
                    "knowledge": "none",
                    "cleanup_intermediate": False,
                },
                timeout=120,
            )
            task_id = str(task["task_id"])
            print(f"[e2e] cloud task started: {task_id}", flush=True)

            task_listing = request_json(endpoint, "GET", "/v1/tasks?limit=100", key=login_key)
            listed = [item for item in task_listing.get("tasks", []) if item.get("snapshot", {}).get("task_id") == task_id]
            if not listed or listed[0].get("title") != "Nonoka X Modal E2E":
                raise RuntimeError(f"new cloud task missing from task listing: {task_listing}")
            print("[e2e] cloud task discovery endpoint passed", flush=True)

            cursor = 0
            events: list[dict[str, Any]] = []
            cancelled_once = False
            deadline = time.monotonic() + args.timeout_minutes * 60
            while time.monotonic() < deadline:
                snapshot = request_json(endpoint, "GET", f"/v1/tasks/{task_id}", key=login_key)
                latest_state = str(snapshot.get("state") or "")
                page = request_json(
                    endpoint,
                    "GET",
                    f"/v1/tasks/{task_id}/events?after={cursor}",
                    key=login_key,
                )
                new_events = list(page.get("events") or [])
                events.extend(new_events)
                if new_events:
                    cursor = max(cursor, max(int(event.get("cursor", 0)) for event in new_events))

                has_runtime_progress = any(
                    event.get("type") == "progress"
                    and str((event.get("payload") or {}).get("stage") or "") not in {"", "starting"}
                    for event in events
                )
                if not cancelled_once and latest_state in {"queued", "running"} and has_runtime_progress:
                    cancelled = request_json(
                        endpoint, "POST", f"/v1/tasks/{task_id}/cancel", key=login_key, payload={}
                    )
                    if cancelled.get("state") != "cancelled":
                        raise RuntimeError(f"cancel did not stop the task: {cancelled}")
                    cancelled_once = True
                    print("[e2e] running GPU task cancelled after progress", flush=True)
                    resumed = request_json(
                        endpoint, "POST", f"/v1/tasks/{task_id}/resume", key=login_key, payload={}
                    )
                    if resumed.get("state") != "queued":
                        raise RuntimeError(f"resume did not queue the task: {resumed}")
                    print("[e2e] cancelled task resumed without spending quota", flush=True)

                if latest_state == "completed":
                    break
                if latest_state in {"failed", "interrupted"}:
                    raise RuntimeError(f"cloud task ended in {latest_state}: {snapshot.get('error')}")
                time.sleep(5)
            else:
                raise TimeoutError(f"task did not complete within {args.timeout_minutes} minutes")

            if not cancelled_once:
                raise RuntimeError("task completed before cancel/resume could be exercised")

            manifest = request_json(
                endpoint, "GET", f"/v1/tasks/{task_id}/artifacts", key=login_key
            )
            artifact_names = set((manifest.get("artifacts") or {}).keys())
            if not {"raw_srt", "stable_json", "final_srt"}.issubset(artifact_names):
                raise RuntimeError(f"artifact manifest incomplete: {artifact_names}")
            _status, final_srt = request(
                endpoint,
                "GET",
                f"/v1/tasks/{task_id}/artifacts/final_srt",
                key=login_key,
            )
            if b"-->" not in final_srt or len(final_srt) < 40:
                raise RuntimeError("downloaded final SRT is empty or invalid")

            page = request_json(
                endpoint,
                "GET",
                f"/v1/tasks/{task_id}/events?after={cursor}",
                key=login_key,
            )
            events.extend(page.get("events") or [])
            event_types = {str(event.get("type")) for event in events}
            required = {
                "started",
                "stage",
                "progress",
                "log",
                "cancelled",
                "handoff",
                "completed",
            }
            if not required.issubset(event_types):
                raise RuntimeError(f"missing event types: {sorted(required - event_types)}")

            final_session = request_json(endpoint, "GET", "/v1/session", key=login_key)
            library = request_json(endpoint, "GET", "/v1/library", key=login_key)
            task_listing = request_json(endpoint, "GET", "/v1/tasks?limit=100", key=login_key)
            if final_session.get("remaining") != 0:
                raise RuntimeError(f"resume spent quota or initial task did not: {final_session}")
            if not any(item.get("id") == task_id and item.get("status") == "completed" for item in library.get("videos", [])):
                raise RuntimeError("completed task missing from cloud library")
            if not any(item.get("snapshot", {}).get("task_id") == task_id and item.get("snapshot", {}).get("state") == "completed" for item in task_listing.get("tasks", [])):
                raise RuntimeError("completed task missing from cloud task listing")
            if uploads.get(object_id) is not None:
                raise RuntimeError("completed task did not clear its upload marker")
            print(
                f"[e2e] completed with artifacts={sorted(artifact_names)} events={len(events)}",
                flush=True,
            )
            print("[e2e] quota, library sync and R2 marker cleanup passed", flush=True)
        return 0
    finally:
        if task_id:
            try:
                cleanup_snapshot = request_json(
                    endpoint, "GET", f"/v1/tasks/{task_id}", key=login_key
                )
                latest_state = str(cleanup_snapshot.get("state") or latest_state)
            except Exception:
                pass
        if task_id and latest_state in {"queued", "running"}:
            try:
                request_json(
                    endpoint,
                    "POST",
                    f"/v1/tasks/{task_id}/cancel",
                    key=login_key,
                    payload={},
                )
            except Exception:
                pass
        if object_id:
            try:
                request_json(endpoint, "DELETE", f"/v1/uploads/{object_id}", key=login_key)
            except ApiError as exc:
                if exc.status != 404:
                    print(f"[e2e] upload cleanup warning: {exc}", flush=True)
        if task_id:
            videos.pop(task_id, None)
            controls.pop(task_id, None)
            for relative in (
                f"tasks/{task_id}",
                f"library/{owner_id}/{task_id}",
                f"knowledge/{owner_id}",
            ):
                try:
                    artifacts.remove_file(relative, recursive=True)
                except Exception:
                    pass
        if object_id:
            uploads.pop(object_id, None)
        keys.pop(owner_id, None)
        print("[e2e] temporary Modal test data cleaned", flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
