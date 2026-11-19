"""Cloud baseline (WP-C): the unchanged Modal compute legs, run to `stable`.

What runs is the compute app's own code, from the sibling `nonoka-sub-cloud`
checkout, on its own images: `scheduler._orchestrate` drives the five legs
exactly as `orchestrate_task` does, `VocalSeparator.load`/`run` separate,
`phases.run_pipeline_phase(phase="gpu")` transcribes and diarizes, and every
word between them still goes through `JobChannel`. Nothing in that checkout
is edited and nothing is deployed: the app below is ephemeral (`app.run()`),
gone when the run ends.

Three things differ from a production task, and only these:

- the Worker. The deployed functions carry the production control Secret, so
  their `JobChannel` would call the real Worker about a job it has never heard
  of. Each container here instead starts a stand-in on 127.0.0.1 that answers
  the internal API (`/run` active with a call id, `/source` with the sample's
  bytes, `/events` and `/child` active, `/complete` and `/fail` recorded) and
  points `CF_CALLBACK_URL` at it. `JobChannel` itself, HTTP client included,
  is unchanged;
- the two GPU legs are this app's functions with the production envelope
  (image, CPU, memory, Volumes) but one card, `GPU` (owner 2026-11-11: L4
  only, no T4 fallback, so every run is comparable). The scheduler reaches
  them through `scheduler.VocalSeparator` and `scheduler.run_aligned_stage`,
  which are pointed here because the production functions belong to the
  deployed app;
- the working tree is captured. The CPU leg deletes `tasks/<job>/` right after
  `/complete`; the stand-in copies it out when `/complete` arrives, before that.

The request is the one the Worker stores for a desktop submission
(`desktop/internal/cloud/service.go` -> `validateJobRequest` in the Worker),
the source is the sample file's own bytes (owner 2026-11-11), and the job id is
fixed per sample so recorded paths do not differ between runs; its scratch
directory is removed before and after every run.

Two halves live in this file:

- the gate half (any Python, standard library only, plus the locked engine's
  own stdlib-only readers): `issues`, `record`, `check`, `compare_runs`,
  `run_statistics`, `compare_statistics`, `judge`. It starts the runner below
  with the cloud checkout's own interpreter (`modal_compute/.venv`), from
  `modal_compute/`, because the images resolve their `add_local_dir` paths
  from there;
- the Modal half, defined only in that runner and in the containers it starts.

How a cloud run is judged (plan section 0.1, rule 3; owner 2026-11-11): not by
bytes. A Vocal container picks AOTI or eager from the first input it sees
(plan 2.10), so the cloud cannot reproduce itself run to run, and the gate
compares structure and statistics instead: the same artifacts, each one
readable the way the desktop reads it, and the segment count, the text, the
speaker count and the covered time within `THRESHOLDS` of the baseline.
`compare_runs` (bytes) is still written into the report, as information.

Each run is compared with the baseline run made with the same separator
accelerator (owner 2026-11-12, WP-C option A): WP-C measured runs of one
accelerator as identical or nearly so, and all of the spread as AOTI against
eager, so the thresholds are tight and only hold within one accelerator.

Layout under `test_data/quality-baseline/cloud/` (never committed):

    <sample>/run-1/      request.json, run.json (images, card, accelerator,
                         events), artifacts/ (the captured working tree);
                         the AOTI run
    <sample>/run-2/      the eager run, made with `record` like run-1
    report.json          which artifacts the eager run reproduced
"""

from __future__ import annotations

import argparse
import bisect
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve()
#: The Nonoka X checkout. Inside a container this file sits alone in `/root`.
ROOT = _HERE.parents[2] if len(_HERE.parents) > 2 else _HERE.parent
CLOUD_CHECKOUT = Path(os.environ.get("NONOKA_QUALITY_CLOUD_CHECKOUT") or ROOT.parent / "nonoka-sub-cloud")
COMPUTE = CLOUD_CHECKOUT / "modal_compute"
BASELINE_ROOT = ROOT / "test_data" / "quality-baseline" / "cloud"
RUNNER_FLAG = "NONOKA_QUALITY_CLOUD_RUNNER"
#: The one card both GPU legs rent (owner 2026-11-11).
GPU = "L4"
#: Where the cloud stops; every artifact compared is one `stable` leaves behind.
TARGET = "stable"
SAMPLES: dict[str, dict[str, Any]] = {
    "sample_ja": {
        "path": ROOT / "test_data" / "audio" / "sample_ja.m4a",
        "fields": {"language": "ja"},
        "job_id": "job_9a7e0000000000000000c0a1",
    },
    "sample_multi_person": {
        "path": ROOT / "test_data" / "audio" / "sample_multi_person.aac",
        # What the Worker stores for a desktop `acoustics` request (`cloudDiarization`).
        "fields": {"language": "ja", "diarization": {"level": "acoustics", "max_speakers": None}},
        "job_id": "job_9a7e0000000000000000c0a2",
    },
}
RUN_ID = "run-quality-gate"
#: Measurement only (WP-C, owner 2026-11-11), never set by the gate. `on`/`off`
#: pins the separator accelerator through the compute code's own switch
#: (`vocal_accel.ENABLED_ENV`, "for pinning the behaviour while measuring"),
#: handed to the Vocal container as an environment variable; the image and
#: the code are unchanged. Unset is production's `auto`.
VOCAL_ACCEL_ENV = "NONOKA_QUALITY_CLOUD_VOCAL_ACCEL"
#: Measurement only: a Volume name mounted at the Vocal container's accel root
#: in place of the production models Volume, so AOTI builds from nothing and
#: publishes nowhere that production reads. Created if missing.
ACCEL_VOLUME_ENV = "NONOKA_QUALITY_CLOUD_ACCEL_VOLUME"
#: WP-CL0 only: a Volume name mounted at `/models` on the aligned and
#: scheduler legs in place of the production models Volume, to prove that a
#: Volume filled by the engine-free primer (`operations.prime::prime_models`)
#: leaves the engine nothing to download. It changes where the weights are
#: read from, not which weights, so unlike the switches above the gate itself
#: honours it (`check`). The Volume must exist; it is never created here.
MODELS_VOLUME_ENV = "NONOKA_QUALITY_CLOUD_MODELS_VOLUME"
#: The engine's own artifact names, by suffix, as `real.COMPARED_SUFFIXES` has
#: them, minus `-raw.srt`: the cloud stops at `stable`.
COMPARED_SUFFIXES = (
    "-vocal.ogg", "-vad.json", "-vad-energy.npz", "-aligned.json", "-stable.json",
    "-diar.json", "-diar-rows.json", "-diar-evidence.npz",
)
#: Captured but never compared: the source and its decode are inputs, the
#: metadata is wall-clock timings and the record of the run itself.
NOT_CAPTURED = ("source.m4a", "source-vocal-decoded.flac")
RUN_TIMEOUT_SEC = 2 * 60 * 60


def runner_python() -> Path:
    for candidate in (COMPUTE / ".venv" / "Scripts" / "python.exe", COMPUTE / ".venv" / "bin" / "python"):
        if candidate.is_file():
            return candidate
    return COMPUTE / ".venv" / "Scripts" / "python.exe"


# --- the Modal half ----------------------------------------------------------------


def _modal_side() -> bool:
    return os.environ.get("MODAL_IS_REMOTE") == "1" or os.environ.get(RUNNER_FLAG) == "1"


if _modal_side():
    import modal

    if os.environ.get("MODAL_IS_REMOTE") != "1" and os.getcwd() not in sys.path:
        # The runner is started as a script from `modal_compute/`, whose
        # package is not on a script's path by default.
        sys.path.insert(0, os.getcwd())

    from nonoka_x_compute import images as _images
    from nonoka_x_compute import resources as _resources
    from nonoka_x_compute.vocal_accel import ACCEL_ROOT as _ACCEL_ROOT

    # The package itself has to reach the containers: the deployed app gets it
    # as its entry package, this one would not. Mounted at startup like every
    # other source mount in `images`, so no build step changes.
    _scheduler_image = _images.scheduler_image.add_local_python_source("nonoka_x_compute")
    _vocal_image = _images.vocal_accel_image.add_local_python_source("nonoka_x_compute")
    _gpu_image = _images.gpu_worker_image.add_local_python_source("nonoka_x_compute")

    app = modal.App("nonoka-x-quality-cloud")

    def _environment_facts() -> dict[str, Any]:
        facts: dict[str, Any] = {
            name: value for name, value in sorted(os.environ.items())
            if name.startswith("MODAL_") and not any(word in name for word in ("TOKEN", "SECRET", "KEY"))
        }
        try:
            import torch

            if torch.cuda.is_available():
                facts["cuda_device"] = torch.cuda.get_device_name(0)
                facts["cuda_capability"] = list(torch.cuda.get_device_capability(0))
            facts["torch"] = torch.__version__
        except Exception as error:  # noqa: BLE001 - a fact we could not read is recorded as such
            facts["torch_error"] = f"{type(error).__name__}: {error}"
        # Measurement switches, when set (`VOCAL_ACCEL_ENV`, `ACCEL_VOLUME_ENV`).
        for name in ("NONOKA_VOCAL_ACCEL", ACCEL_VOLUME_ENV, MODELS_VOLUME_ENV):
            if os.environ.get(name):
                facts[name] = os.environ[name]
        try:
            from nonoka_x_compute.vocal_accel import cpu_tag

            facts["cpu_tag"] = cpu_tag()
        except Exception as error:  # noqa: BLE001
            facts["cpu_tag_error"] = f"{type(error).__name__}: {error}"
        try:
            with open("/proc/cpuinfo", encoding="utf-8") as handle:
                facts["cpu_model"] = next(
                    (line.split(":", 1)[1].strip() for line in handle if line.startswith("model name")), ""
                )
        except OSError:
            pass
        return facts

    class _StandInWorker:
        """The Worker's internal API for one run, on 127.0.0.1, inside one container."""

        def __init__(self, job_id: str, request: dict, *, source: bytes | None = None, on_complete=None) -> None:
            import threading
            from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

            self.job_id = job_id
            self.events: list[dict] = []
            self.failures: list[dict] = []
            self.children: list[Any] = []
            self.completed: dict | None = None
            lock = threading.Lock()
            worker = self

            class Handler(BaseHTTPRequestHandler):
                def log_message(self, *args):  # noqa: D401 - silence
                    pass

                def _reply(self, status: int, body: Any, content_type: str = "application/json") -> None:
                    data = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
                    self.send_response(status)
                    self.send_header("Content-Type", content_type)
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)

                def _serve(self) -> None:
                    from urllib.parse import urlparse

                    path = urlparse(self.path).path.rstrip("/")
                    length = int(self.headers.get("Content-Length") or 0)
                    raw = self.rfile.read(length) if length else b""
                    body = json.loads(raw) if raw else {}
                    parts = path.split("/")
                    if len(parts) < 5 or parts[1:3] != ["internal", "jobs"] or parts[3] != worker.job_id:
                        self._reply(404, {"detail": "unknown job"})
                        return
                    endpoint = parts[4]
                    with lock:
                        if endpoint == "run":
                            self._reply(200, {"active": True, "call_id": "fc-quality-gate",
                                              "owner_id": "quality-gate", "request": request})
                        elif endpoint == "source":
                            if source is None:
                                self._reply(404, {"detail": "no source in this container"})
                            else:
                                self._reply(200, source, "audio/mp4")
                        elif endpoint == "events":
                            worker.events.append({"at": time.time(), **body})
                            self._reply(200, {"active": True})
                        elif endpoint == "child":
                            worker.children.append(body.get("child_call_id"))
                            self._reply(200, {"active": True})
                        elif endpoint == "complete":
                            worker.completed = {"artifacts": body.get("artifacts") or {}}
                            if on_complete is not None:
                                on_complete()
                            self._reply(200, {"active": True, "state": "completed"})
                        elif endpoint == "fail":
                            worker.failures.append(body)
                            self._reply(200, {"active": False})
                        else:
                            self._reply(404, {"detail": f"unknown endpoint {endpoint}"})

                do_GET = _serve
                do_POST = _serve

            self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
            self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)

        def __enter__(self) -> "_StandInWorker":
            self._thread.start()
            os.environ["CF_CALLBACK_URL"] = self.url
            os.environ["WORKER_CALLBACK_TOKEN"] = "quality-gate"
            return self

        def __exit__(self, *exc: object) -> None:
            self.server.shutdown()

    def _plain(value: Any) -> Any:
        """JSON-plain copy: what a leg returns is unpickled where torch is absent (the runner)."""

        return json.loads(json.dumps(value, ensure_ascii=False, default=str))

    def _capture(work: Path) -> dict[str, bytes]:
        found = {}
        if work.is_dir():
            for path in sorted(work.rglob("*")):
                if path.is_file() and path.name not in NOT_CAPTURED and not path.name.startswith("."):
                    found[path.relative_to(work).as_posix()] = path.read_bytes()
        return found

    # Measurement switches (see VOCAL_ACCEL_ENV, ACCEL_VOLUME_ENV): read where
    # the app is defined, reach the Vocal container as plain environment.
    _measure_env = {name: os.environ[name] for name in (VOCAL_ACCEL_ENV, ACCEL_VOLUME_ENV) if os.environ.get(name)}
    if VOCAL_ACCEL_ENV in _measure_env:
        _measure_env["NONOKA_VOCAL_ACCEL"] = _measure_env[VOCAL_ACCEL_ENV]
    _accel_volume = (modal.Volume.from_name(_measure_env[ACCEL_VOLUME_ENV], create_if_missing=True)
                     if ACCEL_VOLUME_ENV in _measure_env else _resources.models_volume)
    _models_env = {MODELS_VOLUME_ENV: os.environ[MODELS_VOLUME_ENV]} if os.environ.get(MODELS_VOLUME_ENV) else {}
    _models_volume = (modal.Volume.from_name(_models_env[MODELS_VOLUME_ENV])
                      if _models_env else _resources.models_volume)
    _models_secrets = [modal.Secret.from_dict(_models_env)] if _models_env else []

    def _point_phases_at_models_volume() -> None:
        """The legs reload the models Volume by its module name; point that at the one mounted here."""

        if os.environ.get(MODELS_VOLUME_ENV):
            from nonoka_x_compute import phases

            phases.models_volume = modal.Volume.from_name(os.environ[MODELS_VOLUME_ENV])

    @app.cls(
        image=_vocal_image,
        gpu=GPU,
        cpu=2.0,
        memory=3072,
        timeout=60 * 30,
        scaledown_window=2,
        volumes={"/data": _resources.scratch_volume, str(_ACCEL_ROOT): _accel_volume},
        secrets=[modal.Secret.from_dict(_measure_env)] if _measure_env else [],
    )
    class QualityVocal:
        """`VocalSeparator`'s own `load` and `run`, on the production envelope but one card."""

        @modal.enter()
        def load(self) -> None:
            from nonoka_x_compute import vocal

            if os.environ.get(ACCEL_VOLUME_ENV):
                # The separator reloads and commits the Volume it installs
                # from by its module name; point that at the one mounted here.
                vocal.models_volume = modal.Volume.from_name(os.environ[ACCEL_VOLUME_ENV])
            user_cls = vocal.VocalSeparator._get_user_cls()
            self.separator = user_cls()
            user_cls.__dict__["load"]._get_raw_f()(self.separator)
            self._run = user_cls.__dict__["run"]._get_raw_f()

        @modal.method()
        def run(self, owner_id: str, job_id: str, request: dict, run_id: str) -> dict:
            started = time.time()
            _resources.scratch_volume.reload()
            # Set when a restarted call finds the delivery an earlier start of
            # it committed: `accel` then describes a container that separated
            # nothing, and the separation's own record is in the runner log.
            reused = (_resources.task_dir(job_id) / "subtitle-vocal.ogg").is_file()
            with _StandInWorker(job_id, request) as worker:
                self._run(self.separator, owner_id, job_id, request, run_id)
            return _plain({"leg": "vocal", "seconds": round(time.time() - started, 1), "accel": self.separator.accel,
                           "reused_delivery": reused,
                           "checkpoint_load_sec": getattr(self.separator, "checkpoint_load_sec", None),
                           "events": worker.events, "failures": worker.failures, "facts": _environment_facts()})

    @app.function(
        image=_gpu_image,
        gpu=GPU,
        cpu=2.0,
        memory=3072,
        scaledown_window=2,
        timeout=60 * 60,
        volumes={"/data": _resources.scratch_volume, "/models": _models_volume},
        secrets=[_resources.huggingface_secret, *_models_secrets],
    )
    def quality_aligned(owner_id: str, job_id: str, request: dict, run_id: str) -> dict:
        """`run_aligned_stage`'s body: the unchanged `gpu` phase."""

        from nonoka_x_compute.phases import run_pipeline_phase

        _point_phases_at_models_volume()
        started = time.time()
        with _StandInWorker(job_id, request) as worker:
            run_pipeline_phase(owner_id, job_id, request, run_id, phase="gpu")
        return _plain({"leg": "aligned", "seconds": round(time.time() - started, 1),
                       "events": worker.events, "failures": worker.failures, "facts": _environment_facts()})

    class _RecordedCall:
        def __init__(self, call, results: list) -> None:
            self._call, self._results = call, results
            self.object_id = call.object_id

        def get(self):
            value = self._call.get()
            self._results.append(value)
            return value

        def cancel(self) -> None:
            self._call.cancel()

    class _Spawner:
        def __init__(self, function, results: list) -> None:
            self._function, self._results = function, results

        def spawn(self, *args):
            return _RecordedCall(self._function.spawn(*args), self._results)

    @app.function(
        image=_scheduler_image,
        cpu=2.0,
        memory=3584,
        scaledown_window=20,
        timeout=RUN_TIMEOUT_SEC,
        volumes={"/data": _resources.scratch_volume, "/models": _models_volume},
        secrets=[_resources.huggingface_secret, *_models_secrets],
    )
    def quality_orchestrate(job_id: str, request: dict, source: bytes, attempt_id: str) -> dict:
        """`orchestrate_task`'s body, `scheduler._orchestrate`, unchanged.

        `attempt_id` names this gate run. Modal restarts a preempted container
        with the same input, and that restart has to resume from the committed
        breakpoints the way a production run resumed by the user would; only a
        breakpoint from a *different* gate run is cleared first. Restarts are
        counted and reported, because a resumed run is a different code path
        through the legs.
        """

        from nonoka_x_compute import scheduler

        _point_phases_at_models_volume()
        work = _resources.task_dir(job_id)
        scratch = _resources.scratch_volume
        scratch.reload()
        marker = Path("/data/quality-gate") / f"{job_id}.json"
        state = json.loads(marker.read_text(encoding="utf-8")) if marker.is_file() else {}
        if state.get("attempt_id") == attempt_id:
            state["starts"] = int(state.get("starts", 1)) + 1
        else:
            # A breakpoint left by an earlier gate run would be resumed from.
            shutil.rmtree(work, ignore_errors=True)
            state = {"attempt_id": attempt_id, "starts": 1}
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(json.dumps(state), encoding="utf-8")
        scratch.commit()
        legs: list = []
        scheduler.VocalSeparator = lambda: type("Vocal", (), {"run": _Spawner(QualityVocal().run, legs)})()
        scheduler.run_aligned_stage = _Spawner(quality_aligned, legs)
        captured: dict[str, bytes] = {}

        def snapshot() -> None:
            captured.update(_capture(work))

        started = time.time()
        error = None
        try:
            with _StandInWorker(job_id, request, source=source, on_complete=snapshot) as worker:
                try:
                    scheduler._orchestrate("quality-gate", job_id, request, RUN_ID)
                except Exception as exc:  # noqa: BLE001 - reported, not swallowed
                    import traceback

                    error = f"{type(exc).__name__}: {exc}"
                    print("QUALITY-CLOUD-ERROR " + traceback.format_exc(), flush=True)
                if worker.completed is None:
                    snapshot()
        finally:
            shutil.rmtree(work, ignore_errors=True)
            marker.unlink(missing_ok=True)
            scratch.commit()
        return _plain({
            "seconds": round(time.time() - started, 1),
            "scheduler_starts": state["starts"],
            "error": error,
            "completed": worker.completed,
            "events": worker.events,
            "failures": worker.failures,
            "children": worker.children,
            "legs": legs,
            "facts": _environment_facts(),
        }) | {"files": captured}


def _cloud_request(name: str) -> dict[str, Any]:
    """What the Worker stores for this sample uploaded from the desktop (runner side)."""

    from nonoka_x_compute.vocal_policy import REQUEST_FIELD, tier_for_duration

    sample = SAMPLES[name]
    path = Path(sample["path"])
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1",
         str(path)], capture_output=True, text=True, check=True,
    )
    duration = float(probe.stdout.strip())
    tier, _reason = tier_for_duration(duration)
    request: dict[str, Any] = {
        "schema": 1,
        "provider": "cloud",
        "source": {"kind": "uploaded_audio", "object_id": "upl_quality_gate", "title": name,
                   "fingerprint": hashlib.sha256(path.read_bytes()).hexdigest(), "duration": duration},
        "target": TARGET,
        "device": "cuda",
        "gpu_tier": "auto",
        "vocal_profile": "cost",
        "separate": True,
        "cleanup_intermediate": False,
        **sample["fields"],
        REQUEST_FIELD: tier.name,
    }
    return request


def _write_run(out: Path, name: str, request: dict, result: dict, images: dict[str, str]) -> dict[str, Any]:
    if out.exists():
        shutil.rmtree(out)
    (out / "artifacts").mkdir(parents=True)
    for relative, data in result.pop("files").items():
        target = out / "artifacts" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    (out / "request.json").write_text(json.dumps(request, ensure_ascii=False, indent=2), encoding="utf-8")
    summary = {
        "sample": name,
        "ok": result["error"] is None and result["completed"] is not None and not result["failures"],
        "error": result["error"],
        "failures": result["failures"],
        "seconds": result["seconds"],
        "images": images,
        "gpu": GPU,
        "legs": {
            leg["leg"]: {
                "seconds": leg["seconds"],
                "card": leg["facts"].get("cuda_device"),
                "cpu_tag": leg["facts"].get("cpu_tag"),
                "cpu_model": leg["facts"].get("cpu_model"),
                "image_id": leg["facts"].get("MODAL_IMAGE_ID"),
                **{key: leg["facts"][key] for key in ("NONOKA_VOCAL_ACCEL", ACCEL_VOLUME_ENV, MODELS_VOLUME_ENV)
                   if key in leg["facts"]},
                **({"accel": leg["accel"]} if "accel" in leg else {}),
                **({"reused_delivery": leg["reused_delivery"]} if "reused_delivery" in leg else {}),
            }
            for leg in result["legs"] if isinstance(leg, dict)
        },
        # More than 1: Modal restarted a preempted scheduler, which resumed
        # from its committed breakpoints.
        "scheduler_starts": result.get("scheduler_starts"),
        "scheduler": {key: result["facts"].get(key) for key in ("cpu_tag", "cpu_model", "MODAL_IMAGE_ID", "torch")},
        "handed_back": sorted((result["completed"] or {}).get("artifacts", {})),
    }
    (out / "run.json").write_text(json.dumps({**summary, "raw": result}, ensure_ascii=False, indent=2, default=str),
                                  encoding="utf-8")
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary


def _runner_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, required=True, help="writes <out>/<sample>/")
    parser.add_argument("--sample", action="append", choices=sorted(SAMPLES), required=True)
    args = parser.parse_args(argv)
    requests = {name: _cloud_request(name) for name in args.sample}
    summaries = {}
    with modal.enable_output(), app.run():
        images = {}
        for leg, image in (("scheduler", _scheduler_image), ("vocal", _vocal_image), ("gpu", _gpu_image)):
            try:
                images[leg] = image.object_id
            except Exception as error:  # noqa: BLE001 - recorded as unknown, the run still counts
                images[leg] = f"unknown ({type(error).__name__})"
        import uuid

        calls = {
            name: quality_orchestrate.spawn(SAMPLES[name]["job_id"], requests[name],
                                            Path(SAMPLES[name]["path"]).read_bytes(), uuid.uuid4().hex)
            for name in args.sample
        }
        for name, call in calls.items():
            summaries[name] = _write_run(args.out / name, name, requests[name], call.get(), images)
    print("QUALITY-CLOUD-SUMMARY " + json.dumps(summaries, ensure_ascii=False), flush=True)
    return 0 if all(summary["ok"] for summary in summaries.values()) else 1


# --- the gate half -----------------------------------------------------------------


def issues() -> list[str]:
    """Why the cloud runs cannot happen here; empty when they can."""

    found = []
    if not COMPUTE.is_dir():
        found.append(f"no compute checkout at {COMPUTE} (set NONOKA_QUALITY_CLOUD_CHECKOUT)")
    if not runner_python().is_file():
        found.append(f"no interpreter for the compute app at {runner_python()} (uv sync in modal_compute)")
    if shutil.which("ffprobe") is None:
        found.append("ffprobe is not on PATH (needed for the request's duration)")
    for sample in SAMPLES.values():
        if not Path(sample["path"]).is_file():
            found.append(f"sample missing: {sample['path']}")
    return found


def run_samples(out: Path, names: list[str] | None = None, *, measure: dict[str, str] | None = None) -> dict[str, Any]:
    """One cloud run of each sample into `out/<sample>/`, all samples side by side.

    `measure` sets the measurement switches (`VOCAL_ACCEL_ENV`,
    `ACCEL_VOLUME_ENV`); without it they are removed, so the gate always runs
    production's own choice whatever this shell has set (`record` pins the accelerator).
    """

    names = names or list(SAMPLES)
    # The runner works in the compute checkout; a relative `out` would land there.
    out = out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    environment = dict(os.environ)
    for name in (VOCAL_ACCEL_ENV, ACCEL_VOLUME_ENV, MODELS_VOLUME_ENV):
        environment.pop(name, None)
    environment.update(measure or {})
    environment[RUNNER_FLAG] = "1"
    environment["NONOKA_X_CHECKOUT"] = str(ROOT)
    # Pins the separator tier for every task when set; the request must carry
    # the ladder's answer, as the Worker's does.
    environment.pop("NONOKA_VOCAL_TIER", None)
    environment["PYTHONIOENCODING"] = "utf-8"
    command = [str(runner_python()), str(Path(__file__).resolve()), "--out", str(out)]
    for name in names:
        command += ["--sample", name]
    with (out / "runner.log").open("w", encoding="utf-8") as log:
        process = subprocess.run(
            command, cwd=COMPUTE, env=environment, stdout=log, stderr=subprocess.STDOUT,
            timeout=RUN_TIMEOUT_SEC + 600, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    log_facts = _runner_log_facts(out / "runner.log")
    summaries = {"_app": log_facts["app"]} if measure is not None else {}
    for name in names:
        summary_path = out / name / "summary.json"
        summary = (json.loads(summary_path.read_text(encoding="utf-8")) if summary_path.is_file()
                   else {"sample": name, "ok": False, "error": f"runner exited {process.returncode}; see {out / 'runner.log'}"})
        summary["separations"] = log_facts["separations"].get(SAMPLES[name]["job_id"], [])
        summary["preemptions_in_invocation"] = log_facts["preemptions"]
        if summary_path.is_file():
            summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        summaries[name] = summary
    return summaries


def _runner_log_facts(log: Path) -> dict[str, Any]:
    """What only the container log says: each separation's accelerator, and preemptions.

    Every separation prints one `separator timing` line naming its job; a call
    Modal restarted after the separation committed reports `reused_delivery`
    instead, so this is the record of what actually separated. Preemption
    notices name no job, so they are counted for the whole invocation.
    """

    separations: dict[str, list[dict[str, Any]]] = {}
    preemptions = 0
    text = log.read_text(encoding="utf-8", errors="replace") if log.is_file() else ""
    import re

    apps = re.findall(r"/apps/[^/\s]+/[^/\s]+/(ap-[A-Za-z0-9]+)", text)
    for line in text.splitlines():
        if "preemption" in line.lower():
            preemptions += 1
        if '"separator timing"' not in line:
            continue
        try:
            record = json.loads(line[line.index("{"):])
        except ValueError:
            continue
        fields = record.get("fields") or {}
        separations.setdefault(str(record.get("task_id")), []).append(
            {key: fields.get(key) for key in ("accel", "tier", "blocks", "separation_sec", "total_sec")}
        )
    return {"separations": separations, "preemptions": preemptions, "app": apps[0] if apps else None}


def artifacts(run_dir: Path) -> dict[str, Path]:
    found = {}
    for suffix in COMPARED_SUFFIXES:
        for path in (run_dir / "artifacts").glob(f"*{suffix}"):
            if not path.name.startswith("."):
                found[suffix.lstrip("-")] = path
    return found


def compare_runs(first: Path, second: Path) -> dict[str, Any]:
    """Per compared artifact: identical bytes, or where the two runs part.

    `real.compare_runs` judged against the cloud layout: the same entries, the
    same accepted standards. Paths need no normalizing here, because both runs
    work under the same `/data/tasks/<job>/`.
    """

    from . import real

    left, right = artifacts(first), artifacts(second)
    result: dict[str, Any] = {}
    for key in sorted(set(left) | set(right)):
        if key not in left or key not in right:
            result[key] = {"status": "missing", "in": "first" if key in left else "second"}
            continue
        a, b = left[key].read_bytes(), right[key].read_bytes()
        entry: dict[str, Any] = {
            "first_sha256": hashlib.sha256(a).hexdigest(),
            "second_sha256": hashlib.sha256(b).hexdigest(),
            "bytes": [len(a), len(b)],
            "status": "identical" if a == b else "differs",
        }
        if a != b and key.endswith(".json"):
            ja, jb = json.loads(a.decode("utf-8")), json.loads(b.decode("utf-8"))
            paths = real._json_differences(ja, jb)
            entry["first_difference"] = real._first_json_difference(ja, jb)
            entry["differing_paths"] = paths[:200]
            entry["differing_path_count"] = len(paths)
            if isinstance(ja, dict) and isinstance(jb, dict):
                outside_a = {k: v for k, v in ja.items() if k != "metadata"}
                outside_b = {k: v for k, v in jb.items() if k != "metadata"}
                entry["identical_outside_metadata"] = outside_a == outside_b
                if key == "vad.json":
                    clean_a, clean_b = real.without_run_timings(ja), real.without_run_timings(jb)
                    entry["identical_without_mtime_and_timings"] = clean_a == clean_b
                    if clean_a != clean_b:
                        entry["first_difference"] = real._first_json_difference(clean_a, clean_b)
                elif outside_a != outside_b:
                    entry["first_difference"] = real._first_json_difference(outside_a, outside_b)
        elif a != b and key.endswith(".ogg"):
            pcm = (real._decoded_pcm_sha256(left[key]), real._decoded_pcm_sha256(right[key]))
            entry["decoded_pcm_sha256"] = list(pcm)
            entry["identical_decoded_samples"] = None if None in pcm else pcm[0] == pcm[1]
        result[key] = entry
    return result


# --- structure and statistics (plan section 0.1, rule 3) ---------------------------


#: What a desktop continuation receives from a cloud task (WP-CL, plan 4.7 item
#: 36): the attachments, by the key the scheduler hands each back under
#: (`summary.json` `handed_back`), and the file of the captured working tree
#: each one is, byte for byte; besides them `transcript_json`, projected from
#: the stable outside that tree.
HANDED_BACK = {"finesub.stable_json": "subtitle-stable.json", "finesub.diar_json": "subtitle-diar.json"}
TRANSCRIPT_HANDED_BACK = "transcript_json"
#: A run recorded before WP-CL handed back the same files under these names,
#: and no transcript (the baselines under `test_data/` are from then and are not
#: re-recorded). Read through the rename, such a run is held to the contract as
#: it is now: every file it handed back must come back under its new name, and
#: the transcript every result has besides.
RECORDED_NAMES = {"stable_json": "finesub.stable_json", "diar_json": "finesub.diar_json"}


def contract_names(handed_back: list[str]) -> list[str]:
    """`handed_back` in the result contract's names: a run recorded before WP-CL read through the rename."""

    if not set(handed_back) & set(RECORDED_NAMES):
        return sorted(handed_back)
    return sorted({RECORDED_NAMES.get(name, name) for name in handed_back} | {TRANSCRIPT_HANDED_BACK})


@dataclass(frozen=True)
class Thresholds:
    """How far a cloud run's statistics may sit from the baseline's.

    `None` means "not approved yet": the statistic is measured and reported,
    and the gate leaves it undecided (exit 2) instead of passing it. The values
    are the owner's to approve after WP-C has measured the natural spread on
    unchanged code (plan section 0.1 rule 3, appendix B questions 5 and 11),
    and are never widened to let a run through (section 0, rule 6).

    They hold between runs of one separator accelerator (`baseline_for`).
    """

    #: |lines - baseline lines| / baseline lines, in `-stable.json`.
    segment_count_ratio: float | None = None
    #: The edit distance between each baseline line and the run's words timed
    #: into it, summed over the lines, over the baseline's characters.
    text_distance_ratio: float | None = None
    #: |speakers - baseline speakers|, in `-diar.json` (samples with `acoustics`).
    speaker_count_delta: int | None = None
    #: |covered - baseline covered|, each the share of the audio the stable's
    #: lines span.
    coverage_delta: float | None = None


#: Approved by the owner on 2026-11-12 (WP-C option A). Measured on L4, vocal
#: and aligned on cpu_tag 9c8ea9902a27, four runs per sample (two AOTI with the
#: package reused, two eager): within one accelerator every statistic was 0
#: except coverage, at most 0.000017 (sample_ja, eager). The margins: one
#: segment on sample_multi_person (54 lines), about 11 characters of 2285 on
#: sample_ja, no speaker change, and about 100 times the coverage seen (about
#: 0.6 s of 302 s). For scale, AOTI against eager measured up to 0.13, 0.051,
#: 0 and 0.0084.
THRESHOLDS = Thresholds(segment_count_ratio=0.02, text_distance_ratio=0.005, speaker_count_delta=0,
                        coverage_delta=0.002)


def edit_distance(a: str, b: str) -> int:
    """Levenshtein distance between two strings, by code point."""

    if a == b:
        return 0
    start = 0
    while start < len(a) and start < len(b) and a[start] == b[start]:
        start += 1
    end_a, end_b = len(a), len(b)
    while end_a > start and end_b > start and a[end_a - 1] == b[end_b - 1]:
        end_a, end_b = end_a - 1, end_b - 1
    a, b = a[start:end_a], b[start:end_b]
    if len(a) < len(b):
        a, b = b, a
    if not b:
        return len(a)
    previous = list(range(len(b) + 1))
    for i, char_a in enumerate(a, 1):
        current = [i]
        for j, char_b in enumerate(b, 1):
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (char_a != char_b)))
        previous = current
    return previous[-1]


def _format_problems(path: Path) -> list[str]:
    """Why one captured artifact is not a well-formed file of its kind; empty when it is."""

    try:
        if path.suffix == ".json":
            json.loads(path.read_text(encoding="utf-8"))
        elif path.suffix == ".npz":
            if not zipfile.is_zipfile(path):
                return [f"{path.name}: not a zip archive"]
            with zipfile.ZipFile(path) as archive:
                members = archive.namelist()
                if not members:
                    return [f"{path.name}: no arrays"]
                for member in members:
                    with archive.open(member) as handle:
                        if not member.endswith(".npy") or handle.read(6) != b"\x93NUMPY":
                            return [f"{path.name}: {member} is not a .npy array"]
        elif path.suffix == ".ogg":
            with path.open("rb") as handle:
                if handle.read(4) != b"OggS":
                    return [f"{path.name}: no Ogg page at the start"]
    except (OSError, ValueError, zipfile.BadZipFile) as error:
        return [f"{path.name}: {type(error).__name__}: {error}"]
    return []


def _handed_back_payload(run_dir: Path) -> dict[str, str] | None:
    """What the scheduler handed back, as sent (`run.json`), or None for a run recorded before WP-CL."""

    path = run_dir / "run.json"
    if not path.is_file():
        return None
    completed = (json.loads(path.read_text(encoding="utf-8")).get("raw") or {}).get("completed") or {}
    artifacts = completed.get("artifacts")
    return artifacts if isinstance(artifacts, dict) and TRANSCRIPT_HANDED_BACK in artifacts else None


def _transcript_reading(run_dir: Path, payload: dict[str, str]) -> tuple[dict[str, Any], list[str]]:
    """The handed-back transcript as the desktop's core reads it, and what proves where it came from.

    It must parse with the core's own reader (`nonoka_x.transcription.model`,
    standard library only); record the sha256 of the handed-back stable, so it
    was projected from that attachment; and have one line per stable segment.
    Each attachment must be its working-tree file byte for byte.
    """

    facts: dict[str, Any] = {}
    problems: list[str] = []
    for key, name in HANDED_BACK.items():
        if key in payload and (run_dir / "artifacts" / name).is_file():
            if payload[key].encode("utf-8") != (run_dir / "artifacts" / name).read_bytes():
                problems.append(f"handed back {key!r}, which is not {name} byte for byte")
    if str(ROOT / "src") not in sys.path:
        sys.path.insert(0, str(ROOT / "src"))
    from nonoka_x.transcription.model import parse_transcript

    try:
        document = parse_transcript(payload[TRANSCRIPT_HANDED_BACK])
    except Exception as error:  # noqa: BLE001 - an unreadable transcript is the finding
        return facts, [*problems, f"the core cannot read the transcript: {type(error).__name__}: {error}"]
    facts["transcript_lines"] = len(document.lines)
    stable = payload.get("finesub.stable_json")
    if stable is not None:
        digest = hashlib.sha256(stable.encode("utf-8")).hexdigest()
        if (document.metadata or {}).get("sha256") != digest:
            problems.append("the transcript does not record the handed-back stable's sha256")
        segments = json.loads(stable).get("segments") or []
        if len(document.lines) != len(segments):
            problems.append(f"the transcript has {len(document.lines)} lines for {len(segments)} stable segments")
    return facts, problems


def _desktop_reading(run_dir: Path, handed_back: list[str]) -> tuple[dict[str, Any], list[str]]:
    """Read what was handed back the way the desktop reads it.

    The transcript (a run from WP-CL on) the way the core reads it
    (`_transcript_reading`). The attachments the way the correction package
    reads them: placed under the engine's names, the locked engine's own
    readers then load the stable's rows (`subtitles.stable_rows.read_rows`),
    the lines the correction reads (`llm.chunking.load_product_segments`) and
    the speaker timeline (`speech.diarization.stage.read_timeline`). All are
    standard library only, so they run in the gate's interpreter.
    """

    from . import union

    union.install()
    facts: dict[str, Any] = {}
    problems: list[str] = []
    payload = _handed_back_payload(run_dir)
    if payload is not None:
        transcript_facts, transcript_problems = _transcript_reading(run_dir, payload)
        facts.update(transcript_facts)
        problems += transcript_problems
    with tempfile.TemporaryDirectory(prefix="nonoka-quality-cloud-read-") as seeded:
        for key in handed_back:
            if key == TRANSCRIPT_HANDED_BACK:
                if payload is None and not set(RECORDED_NAMES) & set(_recorded_handed_back(run_dir)):
                    problems.append("handed back a transcript, but run.json does not carry it")
                continue
            name = HANDED_BACK.get(key)
            if name is None:
                problems.append(f"handed back {key!r}, which the desktop does not seed")
            elif not (run_dir / "artifacts" / name).is_file():
                problems.append(f"handed back {key!r}, but {name} was not captured")
            else:
                shutil.copyfile(run_dir / "artifacts" / name, Path(seeded) / name)
        stable = Path(seeded) / HANDED_BACK["finesub.stable_json"]
        if stable.is_file():
            try:
                from finesub.llm.chunking import load_product_segments
                from finesub.subtitles.stable_rows import read_rows

                rows = read_rows(stable)
                lines, _layer = load_product_segments(stable)
                facts["rows"], facts["correction_lines"] = len(rows), len(lines)
                if not lines:
                    problems.append("the correction half reads no lines from the stable")
            except Exception as error:  # noqa: BLE001 - an unreadable stable is the finding
                problems.append(f"the correction half cannot read the stable: {type(error).__name__}: {error}")
        diar = Path(seeded) / HANDED_BACK["finesub.diar_json"]
        if diar.is_file():
            try:
                from finesub.speech.diarization.stage import read_timeline

                timeline = read_timeline(diar)
                facts["timeline_tracks"] = len(timeline.tracks)
                if not timeline.tracks:
                    problems.append("the speaker timeline has no tracks")
            except Exception as error:  # noqa: BLE001
                problems.append(f"the desktop cannot read the speaker timeline: {type(error).__name__}: {error}")
    return facts, problems


def _recorded_handed_back(run_dir: Path) -> list[str]:
    path = run_dir / "summary.json"
    return list((json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}).get("handed_back") or [])


def _stable_lines(stable: Any) -> tuple[list[dict[str, Any]], list[str]]:
    """The stable's lines as (start, end, text, words); the shape problems found on the way."""

    segments = stable.get("segments") if isinstance(stable, dict) else None
    if not isinstance(segments, list) or not segments:
        return [], ["-stable.json has no segments"]
    lines, problems = [], []
    for index, segment in enumerate(segments):
        start = segment.get("start") if isinstance(segment, dict) else None
        end = segment.get("end") if isinstance(segment, dict) else None
        text = segment.get("text") if isinstance(segment, dict) else None
        if not isinstance(start, (int, float)) or not isinstance(end, (int, float)) or not isinstance(text, str):
            problems.append(f"-stable.json segment {index}: no numeric start/end or no text")
            continue
        if end < start:
            problems.append(f"-stable.json segment {index}: ends at {end} before it starts at {start}")
        words = []
        for word in segment.get("words") or []:
            if isinstance(word, dict) and isinstance(word.get("start"), (int, float)) \
                    and isinstance(word.get("end"), (int, float)) and isinstance(word.get("word"), str):
                words.append(((word["start"] + word["end"]) / 2, word["word"], bool(word.get("space_before"))))
        if not words and text.strip():
            # A line without timed words still has its text, at its middle.
            words = [((start + end) / 2, text.strip(), False)]
        lines.append({"start": float(start), "end": float(end), "text": text.strip(), "words": words})
    return lines, problems


def _covered_seconds(lines: list[dict[str, Any]]) -> float:
    spans = sorted((line["start"], line["end"]) for line in lines if line["end"] > line["start"])
    covered, reach = 0.0, None
    for start, end in spans:
        if reach is None or start > reach:
            covered += end - start
            reach = end
        elif end > reach:
            covered += end - reach
            reach = end
    return covered


def run_statistics(run_dir: Path) -> dict[str, Any]:
    """The structure and statistics of one captured cloud run (`<sample>/run-N/` or a check's scratch)."""

    folder = run_dir / "artifacts"
    names = sorted(path.name for path in folder.iterdir() if path.is_file() and not path.name.startswith(".")) \
        if folder.is_dir() else []
    summary_path = run_dir / "summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8")) if summary_path.is_file() else {}
    request_path = run_dir / "request.json"
    request = json.loads(request_path.read_text(encoding="utf-8")) if request_path.is_file() else {}
    problems = [problem for name in names for problem in _format_problems(folder / name)]
    handed_back = contract_names(list(summary.get("handed_back") or []))
    reading, reading_problems = _desktop_reading(run_dir, handed_back)
    problems += reading_problems
    stats: dict[str, Any] = {
        "artifacts": names,
        "handed_back": handed_back,
        "reading": reading,
        "audio_sec": (request.get("source") or {}).get("duration"),
    }
    stable_path = folder / HANDED_BACK["finesub.stable_json"]
    lines: list[dict[str, Any]] = []
    if stable_path.is_file() and not _format_problems(stable_path):
        lines, shape = _stable_lines(json.loads(stable_path.read_text(encoding="utf-8")))
        problems += shape
    if stats["audio_sec"] is None:
        vad = folder / "subtitle-vad.json"
        if vad.is_file() and not _format_problems(vad):
            stats["audio_sec"] = json.loads(vad.read_text(encoding="utf-8")).get("audio_duration")
    stats["segments"] = len(lines)
    stats["characters"] = sum(len(line["text"]) for line in lines)
    stats["covered_sec"] = round(_covered_seconds(lines), 3)
    stats["coverage"] = (round(stats["covered_sec"] / stats["audio_sec"], 6)
                         if lines and stats["audio_sec"] else None)
    diar_path = folder / HANDED_BACK["finesub.diar_json"]
    if diar_path.is_file() and not _format_problems(diar_path):
        tracks = json.loads(diar_path.read_text(encoding="utf-8")).get("tracks") or []
        stats["speakers"] = len({track.get("track") for track in tracks if isinstance(track, dict)})
    else:
        stats["speakers"] = None
    stats["problems"] = problems
    stats["_lines"] = lines
    return stats


def _nearest_line(lines: list[dict[str, Any]], starts: list[float], moment: float) -> int:
    """The line `moment` falls in; in a gap, the nearer neighbour (the earlier on a tie).

    Lines are in time order and may overlap; the latest-starting line at or
    before `moment` and the one after it are the only candidates that matter
    for a stable's lines, which overlap their neighbours at most.
    """

    index = bisect.bisect_right(starts, moment) - 1
    candidates = [i for i in (index - 1, index, index + 1) if 0 <= i < len(lines)]
    return min(candidates, key=lambda i: (max(lines[i]["start"] - moment, moment - lines[i]["end"], 0.0), i))


def _text_distance(baseline: list[dict[str, Any]], run: list[dict[str, Any]]) -> dict[str, Any]:
    """Each baseline line against the run's words timed into it.

    The two runs need not cut their lines in the same places, so lines are not
    paired by index: every word of the run goes to the baseline line its middle
    falls in (or the nearest one, for a word in a gap), and each baseline line
    is compared with the words it received, joined the way the stable joins
    them. Every word lands somewhere, so text the run added counts as well as
    text it lost.
    """

    starts = [line["start"] for line in baseline]
    received: list[list[tuple[str, bool]]] = [[] for _ in baseline]
    for line in run:
        for middle, word, space_before in line["words"]:
            received[_nearest_line(baseline, starts, middle)].append((word, space_before))
    distance = changed = 0
    for line, words in zip(baseline, received):
        text = "".join((" " if space_before else "") + word for word, space_before in words).strip()
        apart = edit_distance(line["text"], text)
        distance += apart
        changed += apart > 0
    characters = sum(len(line["text"]) for line in baseline)
    whole = edit_distance("".join(line["text"] for line in baseline), "".join(line["text"] for line in run))
    return {
        "distance": distance,
        "baseline_characters": characters,
        "ratio": round(distance / characters, 6) if characters else None,
        "changed_lines": changed,
        "changed_line_ratio": round(changed / len(baseline), 6) if baseline else None,
        # Reported, not judged: the same texts joined end to end, line cuts ignored.
        "whole_text_distance_ratio": round(whole / characters, 6) if characters else None,
    }


def compare_statistics(baseline: dict[str, Any], run: dict[str, Any]) -> dict[str, Any]:
    """A run's structure and statistics against the baseline's (both from `run_statistics`)."""

    comparison: dict[str, Any] = {
        "missing_artifacts": sorted(set(baseline["artifacts"]) - set(run["artifacts"])),
        "extra_artifacts": sorted(set(run["artifacts"]) - set(baseline["artifacts"])),
        "missing_handed_back": sorted(set(baseline["handed_back"]) - set(run["handed_back"])),
        "extra_handed_back": sorted(set(run["handed_back"]) - set(baseline["handed_back"])),
        "problems": list(run["problems"]),
        "baseline_problems": list(baseline["problems"]),
        "reading": run["reading"],
    }
    lines_a, lines_b = baseline["_lines"], run["_lines"]
    statistics: dict[str, Any] = {}
    if lines_a and lines_b:
        statistics["segment_count_ratio"] = {
            "baseline": baseline["segments"], "run": run["segments"],
            "value": round(abs(run["segments"] - baseline["segments"]) / baseline["segments"], 6),
        }
        text = _text_distance(lines_a, lines_b)
        statistics["text_distance_ratio"] = {"value": text.pop("ratio"), **text}
    if baseline["speakers"] is not None:
        statistics["speaker_count_delta"] = {
            "baseline": baseline["speakers"], "run": run["speakers"],
            "value": None if run["speakers"] is None else abs(run["speakers"] - baseline["speakers"]),
        }
    if baseline["coverage"] is not None and run["coverage"] is not None:
        statistics["coverage_delta"] = {
            "baseline": baseline["coverage"], "run": run["coverage"],
            "value": round(abs(run["coverage"] - baseline["coverage"]), 6),
        }
    comparison["statistics"] = statistics
    return comparison


def judge(comparison: dict[str, Any], thresholds: Thresholds | None = None) -> dict[str, list[str]]:
    """Failures, undecided statistics and report lines for one sample's comparison.

    `thresholds` defaults to `THRESHOLDS`, the approved values.

    Structure is never a matter of thresholds: an artifact missing or extra, a
    handed-back result missing, or a file the desktop cannot read fails the
    run. A statistic over its threshold fails it; one whose threshold is not
    approved yet is left undecided.
    """

    thresholds = THRESHOLDS if thresholds is None else thresholds
    failures: list[str] = []
    undecided: list[str] = []
    lines: list[str] = []
    structure = [
        *(f"artifact missing: {name}" for name in comparison["missing_artifacts"]),
        *(f"artifact not in the baseline: {name}" for name in comparison["extra_artifacts"]),
        *(f"not handed back: {key}" for key in comparison["missing_handed_back"]),
        *(f"handed back, not in the baseline: {key}" for key in comparison["extra_handed_back"]),
        *(f"malformed: {problem}" for problem in comparison["problems"]),
    ]
    for problem in structure:
        failures.append(problem)
        lines.append(f"STRUCTURE {problem}")
    if not structure:
        lines.append(f"ok        structure: same artifacts, all readable ({json.dumps(comparison['reading'])})")
    statistics = comparison["statistics"]
    for field in fields(Thresholds):
        name, limit = field.name, getattr(thresholds, field.name)
        entry = statistics.get(name)
        if entry is None:
            if name != "speaker_count_delta":
                failures.append(f"{name}: not measurable")
                lines.append(f"FAIL      {name}: not measurable (no lines on one side)")
            continue
        shown = {key: value for key, value in entry.items() if key != "value"}
        value = entry["value"]
        if value is None:
            failures.append(f"{name}: not measurable")
            lines.append(f"FAIL      {name}: not measurable {json.dumps(shown)}")
        elif limit is None:
            undecided.append(name)
            lines.append(f"UNJUDGED  {name} = {value} (threshold not approved) {json.dumps(shown)}")
        elif value > limit:
            failures.append(f"{name} = {value} > {limit}")
            lines.append(f"OVER      {name} = {value} > {limit} {json.dumps(shown)}")
        else:
            lines.append(f"ok        {name} = {value} <= {limit} {json.dumps(shown)}")
    return {"failures": failures, "undecided": undecided, "lines": lines}


def public_statistics(stats: dict[str, Any]) -> dict[str, Any]:
    """`run_statistics` without the per-line working copy, for reports."""

    return {key: value for key, value in stats.items() if not key.startswith("_")}


#: The baseline's runs and the separator accelerator `record` makes each with.
BASELINE_RUNS = {"run-1": "aoti", "run-2": "eager"}
_PIN = {"aoti": "on", "eager": "off"}


def run_accel(summary: dict[str, Any]) -> str | None:
    """The separator accelerator that produced a run (`aoti`, `eager`), or None.

    The runner log's `separator timing` lines are the record of what actually
    separated; without one, the vocal leg's own facts. None when a restarted
    call separated with both accelerators, or nothing says.
    """

    found = {entry.get("accel") for entry in summary.get("separations") or []} - {None}
    if len(found) == 1:
        return found.pop()
    if found:
        return None
    return ((((summary.get("legs") or {}).get("vocal") or {}).get("accel")) or {}).get("backend")


def baseline_for(name: str, summary: dict[str, Any]) -> tuple[Path | None, str]:
    """The baseline run of `name` made with the same accelerator as `summary`, and why."""

    accel = run_accel(summary)
    if accel is None:
        return None, "the run's separator accelerator cannot be told (see separations)"
    for run in BASELINE_RUNS:
        recorded = BASELINE_ROOT / name / run / "summary.json"
        if recorded.is_file() and run_accel(json.loads(recorded.read_text(encoding="utf-8"))) == accel:
            return recorded.parent, f"{run} ({accel})"
    return None, f"no baseline run of {name} was made with the {accel} separator"


def record() -> dict[str, Any]:
    """Run every sample twice into the baseline directory and write the report.

    run-1 pins the separator to AOTI and run-2 to eager, so the gate has a
    baseline for either one; WP-C found a pinned run identical to one the
    automatic choice made the same way.
    """

    found = issues()
    if found:
        raise SystemExit("cannot run the cloud samples:\n  " + "\n  ".join(found))
    report: dict[str, Any] = {"target": TARGET, "gpu": GPU, "samples": {}}
    runs = {index: run_samples(BASELINE_ROOT / f"run-{index}",
                               measure={VOCAL_ACCEL_ENV: _PIN[BASELINE_RUNS[f"run-{index}"]]})
            for index in (1, 2)}
    for name in SAMPLES:
        for index in (1, 2):
            # <sample>/run-N, the layout `real` keeps.
            source, target = BASELINE_ROOT / f"run-{index}" / name, BASELINE_ROOT / name / f"run-{index}"
            if target.exists():
                shutil.rmtree(target)
            if source.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(source), str(target))
        report["samples"][name] = {
            "runs": [runs[1][name], runs[2][name]],
            "comparison": compare_runs(BASELINE_ROOT / name / "run-1", BASELINE_ROOT / name / "run-2"),
            "statistics": _statistics_between(BASELINE_ROOT / name / "run-1", BASELINE_ROOT / name / "run-2"),
        }
    for index in (1, 2):
        log = BASELINE_ROOT / f"run-{index}" / "runner.log"
        if log.is_file():
            shutil.move(str(log), str(BASELINE_ROOT / f"runner-{index}.log"))
        shutil.rmtree(BASELINE_ROOT / f"run-{index}", ignore_errors=True)
    (BASELINE_ROOT / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def _statistics_between(baseline: Path, run: Path) -> dict[str, Any]:
    """`compare_statistics` of two run directories, with each side's own statistics."""

    left, right = run_statistics(baseline), run_statistics(run)
    return {"baseline": public_statistics(left), "run": public_statistics(right),
            "comparison": compare_statistics(left, right)}


def check(scratch: Path) -> dict[str, Any]:
    """Run every sample once into `scratch` and compare with the baseline run of its accelerator.

    `baseline` names that run (`baseline_for`); a successful run with none to
    compare with is an `error`. `comparison` is the byte comparison
    (information); `statistics` is what the gate judges (`judge`).
    """

    found = issues()
    if found:
        return {"skipped": found}
    result: dict[str, Any] = {}
    missing = [name for name in SAMPLES if not (BASELINE_ROOT / name / "run-1").is_dir()]
    for name in missing:
        result[name] = {"error": f"no baseline at {BASELINE_ROOT / name / 'run-1'}"}
    names = [name for name in SAMPLES if name not in missing]
    if names:
        # The one switch the gate honours (`MODELS_VOLUME_ENV`); it is what
        # this shell set, passed on explicitly like every measurement switch.
        if os.environ.get(MODELS_VOLUME_ENV):
            summaries = run_samples(scratch, names, measure={MODELS_VOLUME_ENV: os.environ[MODELS_VOLUME_ENV]})
        else:
            summaries = run_samples(scratch, names)
        for name in names:
            baseline, why = baseline_for(name, summaries[name])
            if baseline is None:
                if summaries[name].get("ok"):
                    result[name] = {"run": summaries[name], "error": why}
                    continue
                # The run failed anyway; run-1 only fills the report.
                baseline = BASELINE_ROOT / name / "run-1"
            result[name] = {"run": summaries[name], "baseline": baseline.name, "baseline_why": why,
                            "comparison": compare_runs(baseline, scratch / name),
                            "statistics": _statistics_between(baseline, scratch / name)}
    return result


def environment_changes(recorded: dict[str, Any], current: dict[str, Any]) -> list[str]:
    """Card, image or accelerator facts that differ from a baseline run (reported, not judged)."""

    notes = []
    if recorded.get("images") != current.get("images"):
        notes.append(f"images: {recorded.get('images')} -> {current.get('images')}")
    for leg in sorted(set(recorded.get("legs") or {}) | set(current.get("legs") or {})):
        old, new = (recorded.get("legs") or {}).get(leg, {}), (current.get("legs") or {}).get(leg, {})
        for key in ("card", "cpu_tag"):
            if old.get(key) != new.get(key):
                notes.append(f"{leg} {key}: {old.get(key)} -> {new.get(key)}")
    old_accel = [entry.get("accel") for entry in recorded.get("separations") or []]
    new_accel = [entry.get("accel") for entry in current.get("separations") or []]
    if old_accel != new_accel:
        notes.append(f"separator accelerator: {old_accel} -> {new_accel}")
    if (current.get("scheduler_starts") or 1) > 1 or current.get("preemptions_in_invocation"):
        notes.append(f"preempted: scheduler started {current.get('scheduler_starts')} time(s), "
                     f"{current.get('preemptions_in_invocation')} preemption notice(s) in the runner log")
    return notes


if __name__ == "__main__":
    if _modal_side():
        raise SystemExit(_runner_main(sys.argv[1:]))
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("record", "issues", "check", "compare", "stats", "measure"))
    parser.add_argument("--scratch", type=Path)
    parser.add_argument("--out", type=Path, help="measure: where the runs go (never test_data/)")
    parser.add_argument("--accel", choices=("on", "off"), help="measure: pin the separator accelerator")
    parser.add_argument("--accel-volume", help="measure: a scratch Volume for the accel root (forces a build)")
    parser.add_argument("runs", nargs="*", type=Path,
                        help="stats: a baseline run directory, then the run directories to judge against it")
    args = parser.parse_args()
    sys.path.insert(0, str(ROOT))
    from tests.quality_harness import cloud as _self  # the gate half, with its package for `real`

    if args.command == "measure":
        # WP-C's spread measurement (owner-approved plan, 2026-11-11). Writes
        # only under --out; the baseline directory is refused.
        if args.out is None or BASELINE_ROOT in (args.out.resolve(), *args.out.resolve().parents):
            raise SystemExit("measure needs --out outside the baseline directory")
        found = _self.issues()
        if found:
            raise SystemExit("cannot run the cloud samples:\n  " + "\n  ".join(found))
        switches = {}
        if args.accel:
            switches[VOCAL_ACCEL_ENV] = args.accel
        if args.accel_volume:
            switches[ACCEL_VOLUME_ENV] = args.accel_volume
        started = time.time()
        summaries = _self.run_samples(args.out, measure=switches)
        summaries["_measure"] = {"switches": switches, "wall_sec": round(time.time() - started, 1)}
        (args.out / "measure.json").write_text(json.dumps(summaries, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(summaries, ensure_ascii=False, indent=2))
    elif args.command == "stats":
        # Offline: no Modal, nothing written. For measuring the spread between captured runs.
        if len(args.runs) < 2:
            raise SystemExit("stats takes a baseline run directory and at least one run directory")
        reference = _self.run_statistics(args.runs[0])
        for run_dir in args.runs[1:]:
            comparison = _self.compare_statistics(reference, _self.run_statistics(run_dir))
            print(json.dumps({"baseline": str(args.runs[0]), "run": str(run_dir),
                              "statistics": comparison["statistics"],
                              "judged": _self.judge(comparison)}, ensure_ascii=False, indent=2))
    elif args.command == "issues":
        print(json.dumps(_self.issues(), ensure_ascii=False, indent=2))
    elif args.command == "compare":
        report_path = BASELINE_ROOT / "report.json"
        report = json.loads(report_path.read_text(encoding="utf-8"))
        for name in SAMPLES:
            report["samples"][name]["comparison"] = _self.compare_runs(
                BASELINE_ROOT / name / "run-1", BASELINE_ROOT / name / "run-2"
            )
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False, indent=2))
    elif args.command == "record":
        print(json.dumps(_self.record(), ensure_ascii=False, indent=2, default=str))
    else:
        scratch = args.scratch or Path(tempfile.mkdtemp(prefix="nonoka-quality-cloud-"))
        print(json.dumps(_self.check(scratch), ensure_ascii=False, indent=2, default=str))
