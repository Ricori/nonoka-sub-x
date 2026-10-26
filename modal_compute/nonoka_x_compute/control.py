"""The control endpoint: how the Cloudflare Worker reaches compute.

Modal has no public HTTP API for spawning or cancelling a function call, so
this small ASGI app does it on the Worker's behalf. It is internal: every route
but `/` wants `Authorization: Bearer <CONTROL_TOKEN>`, and the Worker is the only
holder of that token (as `MODAL_CONTROL_TOKEN`).

- `POST /spawn  {owner_id, job_id, run_id, request}` -> `{call_id}`
- `POST /cancel {call_ids}` -> `{cancelled, errors}`
- `POST /probe  {call_id}` -> `{state: running|finished|failed|unknown, error?}`
- `POST /purge  {job_id}` -> `{removed}`
- `GET  /` -> `{ok: true}`

Client: `nonoka-cloud-cf/src/modal.ts`.
"""

from __future__ import annotations

import hmac
import os
import re
import shutil
from pathlib import Path
from typing import Any, Callable

import modal

from .images import control_image
from .resources import DATA_ROOT, app, control_secret, scratch_volume


JOB_ID_PATTERN = re.compile(r"job_[0-9a-f]{24}")
RUN_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,64}")
CALL_ID_PATTERN = re.compile(r"fc-[A-Za-z0-9]{1,64}")
OWNER_ID_PATTERN = re.compile(r"[A-Za-z0-9._~-]{1,256}")


def _spawn(owner_id: str, job_id: str, request: dict, run_id: str) -> str:
    from .scheduler import orchestrate_task

    return orchestrate_task.spawn(owner_id, job_id, request, run_id).object_id


def _function_call(call_id: str):
    return modal.FunctionCall.from_id(call_id)


def _purge(job_id: str, data_root: Path = DATA_ROOT) -> bool:
    try:
        scratch_volume.reload()
    except Exception:
        pass
    target = data_root / "tasks" / job_id
    existed = target.exists()
    shutil.rmtree(target, ignore_errors=True)
    if existed:
        scratch_volume.commit()
    return existed


def probe_state(call) -> dict[str, str]:
    """What became of a call, without waiting for it.

    Still running is a timeout; returned is finished; anything it raised is
    failed -- the Worker labels both of the latter `interrupted`, because a run
    that ended properly has already published its outcome. A control plane
    this container cannot reach says nothing about the call.
    """

    try:
        call.get(timeout=0)
    except (TimeoutError, modal.exception.TimeoutError):
        return {"state": "running"}
    except (ConnectionError, modal.exception.ConnectionError) as exc:
        return {"state": "unknown", "error": type(exc).__name__}
    except Exception as exc:
        return {"state": "failed", "error": type(exc).__name__}
    return {"state": "finished"}


def build_control_api(
    *,
    token: str | None = None,
    spawn: Callable[[str, str, dict, str], str] = _spawn,
    function_call: Callable[[str], Any] = _function_call,
    purge: Callable[[str], bool] = _purge,
):
    """The ASGI app, with Modal behind replaceable callables for tests."""

    from fastapi import Body, Depends, FastAPI, Header, HTTPException
    from fastapi.concurrency import run_in_threadpool

    expected = token if token is not None else os.environ.get("CONTROL_TOKEN", "")

    def authorized(authorization: str = Header(default="")) -> None:
        supplied = authorization[7:].strip() if authorization.startswith("Bearer ") else ""
        # Fails closed: with no token configured, nothing is authorized.
        if not expected or not supplied or not hmac.compare_digest(supplied, expected):
            raise HTTPException(status_code=401, detail="unauthorized")

    def field(body: Any, name: str, pattern: re.Pattern[str]) -> str:
        value = body.get(name) if isinstance(body, dict) else None
        if not isinstance(value, str) or not pattern.fullmatch(value):
            raise HTTPException(status_code=422, detail=f"{name} 无效")
        return value

    web = FastAPI(title="Nonoka X Compute control", version="1", docs_url=None, redoc_url=None, openapi_url=None)

    @web.get("/")
    def root():
        return {"ok": True}

    @web.post("/spawn", dependencies=[Depends(authorized)])
    async def spawn_run(body: Any = Body(...)):
        owner_id = field(body, "owner_id", OWNER_ID_PATTERN)
        job_id = field(body, "job_id", JOB_ID_PATTERN)
        run_id = field(body, "run_id", RUN_ID_PATTERN)
        request = body.get("request")
        if not isinstance(request, dict):
            raise HTTPException(status_code=422, detail="request 必须是对象")
        call_id = await run_in_threadpool(spawn, owner_id, job_id, request, run_id)
        return {"call_id": call_id}

    @web.post("/cancel", dependencies=[Depends(authorized)])
    async def cancel_calls(body: Any = Body(...)):
        call_ids = body.get("call_ids") if isinstance(body, dict) else None
        if not isinstance(call_ids, list) or len(call_ids) > 16:
            raise HTTPException(status_code=422, detail="call_ids 必须是数组")
        cancelled: list[str] = []
        errors: dict[str, str] = {}
        for call_id in call_ids:
            if not isinstance(call_id, str) or not CALL_ID_PATTERN.fullmatch(call_id):
                errors[str(call_id)[:80]] = "invalid call id"
                continue
            try:
                await run_in_threadpool(lambda: function_call(call_id).cancel())
            except Exception as exc:
                errors[call_id] = f"{type(exc).__name__}: {exc}"[:200]
                continue
            cancelled.append(call_id)
        return {"cancelled": cancelled, "errors": errors}

    @web.post("/probe", dependencies=[Depends(authorized)])
    async def probe_call(body: Any = Body(...)):
        call_id = field(body, "call_id", CALL_ID_PATTERN)
        try:
            call = await run_in_threadpool(function_call, call_id)
        except (ConnectionError, modal.exception.ConnectionError) as exc:
            return {"state": "unknown", "error": type(exc).__name__}
        except Exception as exc:
            # An id Modal does not know is a call that is not running.
            return {"state": "failed", "error": type(exc).__name__}
        return await run_in_threadpool(probe_state, call)

    @web.post("/purge", dependencies=[Depends(authorized)])
    async def purge_breakpoints(body: Any = Body(...)):
        job_id = field(body, "job_id", JOB_ID_PATTERN)
        return {"removed": await run_in_threadpool(purge, job_id)}

    return web


@app.function(
    image=control_image,
    secrets=[control_secret],
    volumes={"/data": scratch_volume},
    cpu=0.25,
    memory=256,
    max_containers=2,
    # Warm across a burst of probes; the Worker polls a running job every 10 s.
    scaledown_window=300,
)
@modal.concurrent(max_inputs=20)
@modal.asgi_app()
def control():
    return build_control_api()
