"""A run's line to the Cloudflare Worker: `/internal/jobs/{id}/*`.

Every leg of a run -- the scheduler, the Vocal worker, the Whisper worker --
used to read and write the job record in a Modal Dict and poll a second Dict for
cancellation. Both now live in D1 behind the Worker, and this is the only way a
container reaches them. Each call names the run it belongs to, and the Worker
fences every write on it in one statement: the run must still be the job's live
attempt, still queued or running, and not cancelled. A run told
`active: false` stops, by raising `TaskCancelled`.

Standard library only, on purpose: the three images this is imported into do
not share an HTTP client, and the Vocal image may not grow a dependency (see
`images`).

Contract: `nonoka-cloud-cf/src/internal/routes.ts`.
"""

from __future__ import annotations

import http.client
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable


#: Attempts per call against 5xx, 429 and network errors, with exponential
#: backoff between them: 0.5 s doubling to 8 s, about half a minute in all. A
#: Worker deploy or a blip at the edge is absorbed; an outage longer than that
#: fails the leg, which the user resumes from its breakpoints.
DEFAULT_ATTEMPTS = 8
DEFAULT_BACKOFF_SEC = 0.5
MAX_BACKOFF_SEC = 8.0
#: Per request. The source download sets its own, per read.
DEFAULT_TIMEOUT_SEC = 30.0

#: The callId handshake. The Worker learns the scheduler's call id only when
#: `/spawn` returns, so a leg waits for it before publishing anything: until
#: then a cancel could not reach the call that is running.
DISPATCH_TIMEOUT_SEC = 15.0
DISPATCH_POLL_SEC = 0.1

#: Progress is published at most this often. A repeated value is never
#: published at all.
PROGRESS_MIN_INTERVAL_SEC = 1.0

#: How stale the last word from the Worker may be before a call that publishes
#: nothing -- a log line, a throttled progress tick -- asks again whether the
#: run is still live. Every published event already answers that question, so
#: this only bounds how long a quiet stretch can miss a cancel.
ACTIVE_RECHECK_SEC = 5.0

_DOWNLOAD_CHUNK = 1 << 20


class TaskCancelled(RuntimeError):
    """The run is no longer live: cancelled, superseded, finished or gone."""


class ChannelError(RuntimeError):
    """The Worker refused or could not answer a call."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class ChannelUnauthorized(ChannelError):
    """`WORKER_CALLBACK_TOKEN` is not the one the Worker holds. Never retried."""


class ChannelUnavailable(ChannelError):
    """Every attempt failed on a 5xx, a 429 or the network."""


def _retryable(status: int) -> bool:
    return status >= 500 or status == 429


class JobChannel:
    """One run's calls to the Worker. Thread-safe."""

    def __init__(
        self,
        job_id: str,
        run_id: str,
        *,
        base_url: str | None = None,
        token: str | None = None,
        attempts: int = DEFAULT_ATTEMPTS,
        backoff_sec: float = DEFAULT_BACKOFF_SEC,
        timeout_sec: float = DEFAULT_TIMEOUT_SEC,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        base = base_url if base_url is not None else os.environ.get("CF_CALLBACK_URL", "")
        secret = token if token is not None else os.environ.get("WORKER_CALLBACK_TOKEN", "")
        if not base:
            raise ChannelError("CF_CALLBACK_URL is not configured")
        if not secret:
            raise ChannelError("WORKER_CALLBACK_TOKEN is not configured")
        self.job_id = job_id
        self.run_id = run_id
        self._base = base.rstrip("/")
        self._token = secret
        self._attempts = max(1, int(attempts))
        self._backoff = float(backoff_sec)
        self._timeout = float(timeout_sec)
        self._sleep = sleep
        self._clock = clock
        self._lock = threading.RLock()
        self._confirmed_at = float("-inf")
        self._last_progress: tuple[str, dict] | None = None
        self._last_progress_at = float("-inf")

    # -- transport -----------------------------------------------------------

    def _url(self, endpoint: str, query: dict[str, str] | None = None) -> str:
        path = f"{self._base}/internal/jobs/{urllib.parse.quote(self.job_id, safe='')}/{endpoint}"
        return f"{path}?{urllib.parse.urlencode(query)}" if query else path

    def _open(self, method: str, url: str, body: dict | None, timeout: float):
        data = None
        headers = {"Authorization": f"Bearer {self._token}", "Accept": "application/json"}
        if body is not None:
            data = json.dumps(body, ensure_ascii=False, default=str).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, method=method, headers=headers)
        return urllib.request.urlopen(request, timeout=timeout)

    def _backoff_for(self, attempt: int) -> float:
        return min(MAX_BACKOFF_SEC, self._backoff * (2**attempt))

    def _call(self, method: str, endpoint: str, *, body: dict | None = None, query: dict[str, str] | None = None) -> tuple[int, Any]:
        """One call with retries. Returns the status and the decoded JSON body."""

        url = self._url(endpoint, query)
        last = ""
        for attempt in range(self._attempts):
            if attempt:
                self._sleep(self._backoff_for(attempt - 1))
            try:
                with self._open(method, url, body, self._timeout) as response:
                    status, raw = response.status, response.read()
            except urllib.error.HTTPError as error:
                status, raw = error.code, error.read()
            except (urllib.error.URLError, http.client.HTTPException, OSError) as error:
                last = f"{type(error).__name__}: {error}"
                continue
            if status == 401:
                raise ChannelUnauthorized(f"{method} {endpoint}: 401 unauthorized", status=401)
            if _retryable(status):
                last = f"HTTP {status}"
                continue
            try:
                payload = json.loads(raw.decode("utf-8")) if raw else None
            except ValueError:
                payload = None
            return status, payload
        raise ChannelUnavailable(f"{method} {endpoint}: gave up after {self._attempts} attempts ({last})")

    @staticmethod
    def _detail(payload: Any) -> str:
        return str(payload.get("detail")) if isinstance(payload, dict) and payload.get("detail") else ""

    def _fenced(self, endpoint: str, body: dict) -> dict:
        """A write the Worker fences on the run. Raises `TaskCancelled` when it is not live."""

        status, payload = self._call("POST", endpoint, body={"run_id": self.run_id, **body})
        if status == 404:
            raise TaskCancelled("job no longer exists")
        if status >= 400:
            raise ChannelError(f"POST {endpoint}: {status} {self._detail(payload)}", status=status)
        result = payload if isinstance(payload, dict) else {}
        if result.get("active") is not True:
            raise TaskCancelled("task cancelled or superseded")
        self._confirmed_at = self._clock()
        return result

    # -- run state -----------------------------------------------------------

    def run(self) -> dict:
        """`{active, call_id, owner_id, request}`. A job that is gone is inactive."""

        status, payload = self._call("GET", "run", query={"run_id": self.run_id})
        if status == 404:
            return {"active": False, "call_id": None}
        if status >= 400 or not isinstance(payload, dict):
            raise ChannelError(f"GET run: {status} {self._detail(payload)}", status=status)
        if payload.get("active") is True:
            self._confirmed_at = self._clock()
        return payload

    def active(self) -> bool:
        return self.run().get("active") is True

    def ensure_active(self, max_age_sec: float = ACTIVE_RECHECK_SEC) -> None:
        """Raise `TaskCancelled` if the run has stopped. Asks at most every `max_age_sec`."""

        if self._clock() - self._confirmed_at < max_age_sec:
            return
        if not self.active():
            raise TaskCancelled("task cancelled or superseded")

    def wait_dispatched(
        self,
        *,
        timeout_sec: float = DISPATCH_TIMEOUT_SEC,
        poll_sec: float = DISPATCH_POLL_SEC,
    ) -> dict | None:
        """Wait until the Worker has recorded this run's call id.

        Returns the run, or None when there is nothing to do: the run is no
        longer live, or the call id never arrived -- which is published as
        `dispatch_incomplete` before returning.
        """

        deadline = self._clock() + timeout_sec
        while True:
            state = self.run()
            if state.get("active") is not True:
                return None
            if state.get("call_id"):
                return state
            if self._clock() >= deadline:
                self.fail("dispatch_incomplete", "Modal 调用已启动，但 callId 未能写入任务记录")
                return None
            self._sleep(poll_sec)

    # -- events --------------------------------------------------------------

    _UNSET: Any = object()

    def publish(self, events: list[dict], *, stage: Any = _UNSET, progress: Any = _UNSET) -> None:
        body: dict[str, Any] = {"events": events}
        if stage is not self._UNSET:
            body["stage"] = stage
        if progress is not self._UNSET:
            body["progress"] = progress
        with self._lock:
            self._fenced("events", body)

    def stage(self, stage: str, payload: dict) -> None:
        """A stage began. The Worker clears the job's progress with it."""

        with self._lock:
            self.publish([{"type": "stage", "payload": payload}], stage=stage, progress=None)
            self._last_progress = None

    def handoff(self, stage: str, payload: dict) -> None:
        with self._lock:
            self.publish([{"type": "handoff", "payload": payload}], stage=stage, progress=None)
            self._last_progress = None

    def warning(self, payload: dict) -> None:
        self.publish([{"type": "warning", "payload": payload}])

    def progress(self, stage: str, payload: dict) -> bool:
        """Publish a progress value unless it repeats or arrives too soon.

        The first value after a stage and the final one (`completed == total`)
        are never held back. Returns whether it was published; a value that
        was not still confirms the run is live, at most every few seconds.
        """

        value = {
            "completed": payload.get("completed", 0),
            "total": payload.get("total"),
            "unit": payload.get("unit", ""),
            "message": payload.get("message", ""),
        }
        with self._lock:
            now = self._clock()
            if self._last_progress == (stage, value):
                self.ensure_active()
                return False
            final = value["total"] is not None and value["completed"] == value["total"]
            if (
                self._last_progress is not None
                and not final
                and now - self._last_progress_at < PROGRESS_MIN_INTERVAL_SEC
            ):
                self.ensure_active()
                return False
            self.publish([{"type": "progress", "payload": payload}], stage=stage, progress=value)
            self._last_progress = (stage, value)
            self._last_progress_at = now
            return True

    def child(self, child_call_id: str | None) -> None:
        """Record the GPU stage's call so a cancel reaches it too (None clears it)."""

        self._fenced("child", {"child_call_id": child_call_id})

    # -- outcome -------------------------------------------------------------

    def complete(self, artifacts: dict[str, str]) -> dict:
        """Hand the result to the Worker. A run cancelled first raises `TaskCancelled`."""

        return self._fenced("complete", {"artifacts": artifacts})

    def fail(self, code: str, message: str, *, stage: str | None = None) -> bool:
        """Publish this run's failure. Best effort, and never raises.

        Always called on the way out of a failing leg, where the original error
        is the one worth propagating. The Worker ignores a failure for a run
        that is no longer live, and never overwrites one already published.
        """

        body: dict[str, Any] = {"run_id": self.run_id, "code": code, "message": message}
        if stage:
            body["stage"] = stage
        try:
            status, payload = self._call("POST", "fail", body=body)
        except ChannelError as error:
            _print(self.job_id, self.run_id, "warning", {"message": f"failure not published: {error}", "code": code})
            return False
        if status >= 400 and status != 404:
            _print(self.job_id, self.run_id, "warning", {"message": f"failure not published: HTTP {status} {self._detail(payload)}", "code": code})
            return False
        return status < 400

    # -- source --------------------------------------------------------------

    def download_source(self, destination: Path, *, read_timeout_sec: float = 120.0) -> int:
        """Stream the run's source audio to `destination` via a `.part` file.

        Retried from the start on a network error or a short body. A 409 means
        the run is no longer live; a 404 that the audio is gone.
        """

        destination = Path(destination)
        partial = destination.with_suffix(destination.suffix + ".part")
        url = self._url("source", {"run_id": self.run_id})
        last = ""
        for attempt in range(self._attempts):
            if attempt:
                self._sleep(self._backoff_for(attempt - 1))
            partial.unlink(missing_ok=True)
            try:
                with self._open("GET", url, None, read_timeout_sec) as response:
                    expected = response.headers.get("Content-Length")
                    written = 0
                    with partial.open("wb") as handle:
                        while True:
                            chunk = response.read(_DOWNLOAD_CHUNK)
                            if not chunk:
                                break
                            handle.write(chunk)
                            written += len(chunk)
            except urllib.error.HTTPError as error:
                status = error.code
                detail = error.read()[:200].decode("utf-8", "replace")
                if status == 401:
                    raise ChannelUnauthorized("GET source: 401 unauthorized", status=401)
                if status == 409:
                    raise TaskCancelled("task no longer running")
                if status == 404:
                    raise FileNotFoundError(f"source audio is missing: {detail}")
                if _retryable(status):
                    last = f"HTTP {status}"
                    continue
                raise ChannelError(f"GET source: {status} {detail}", status=status)
            except (urllib.error.URLError, http.client.HTTPException, OSError) as error:
                last = f"{type(error).__name__}: {error}"
                continue
            if expected is not None and int(expected) != written:
                last = f"short body: {written} of {expected} bytes"
                continue
            os.replace(partial, destination)
            self._confirmed_at = self._clock()
            return written
        partial.unlink(missing_ok=True)
        raise ChannelUnavailable(f"GET source: gave up after {self._attempts} attempts ({last})")


def _print(job_id: str, run_id: str, event_type: str, payload: dict) -> None:
    print(
        json.dumps(
            {"scope": "finesub", "task_id": job_id, "run_id": run_id, "type": event_type, **payload},
            ensure_ascii=False,
            default=str,
        ),
        flush=True,
    )
