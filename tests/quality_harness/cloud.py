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

- the gate half (any Python, standard library only): `issues`, `record`,
  `check`, `compare_runs`. It starts the runner below with the cloud
  checkout's own interpreter (`modal_compute/.venv`), from `modal_compute/`,
  because the images resolve their `add_local_dir` paths from there;
- the Modal half, defined only in that runner and in the containers it starts.

Layout under `test_data/quality-baseline/cloud/` (never committed):

    <sample>/run-1/      request.json, run.json (images, card, accelerator,
                         events), artifacts/ (the captured working tree)
    <sample>/run-2/      the repeat, made with `record`
    report.json          which artifacts the repeat reproduced
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
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

    @app.cls(
        image=_vocal_image,
        gpu=GPU,
        cpu=2.0,
        memory=3072,
        timeout=60 * 30,
        scaledown_window=2,
        volumes={"/data": _resources.scratch_volume, str(_ACCEL_ROOT): _resources.models_volume},
    )
    class QualityVocal:
        """`VocalSeparator`'s own `load` and `run`, on the production envelope but one card."""

        @modal.enter()
        def load(self) -> None:
            from nonoka_x_compute import vocal

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
        volumes={"/data": _resources.scratch_volume, "/models": _resources.models_volume},
        secrets=[_resources.huggingface_secret],
    )
    def quality_aligned(owner_id: str, job_id: str, request: dict, run_id: str) -> dict:
        """`run_aligned_stage`'s body: the unchanged `gpu` phase."""

        from nonoka_x_compute.phases import run_pipeline_phase

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
        volumes={"/data": _resources.scratch_volume, "/models": _resources.models_volume},
        secrets=[_resources.huggingface_secret],
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


def run_samples(out: Path, names: list[str] | None = None) -> dict[str, Any]:
    """One cloud run of each sample into `out/<sample>/`, all samples side by side."""

    names = names or list(SAMPLES)
    out.mkdir(parents=True, exist_ok=True)
    environment = dict(os.environ)
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
    summaries = {}
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
    return {"separations": separations, "preemptions": preemptions}


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


def record() -> dict[str, Any]:
    """Run every sample twice into the baseline directory and write the report."""

    found = issues()
    if found:
        raise SystemExit("cannot run the cloud samples:\n  " + "\n  ".join(found))
    report: dict[str, Any] = {"target": TARGET, "gpu": GPU, "samples": {}}
    runs = {index: run_samples(BASELINE_ROOT / f"run-{index}") for index in (1, 2)}
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
        }
    for index in (1, 2):
        log = BASELINE_ROOT / f"run-{index}" / "runner.log"
        if log.is_file():
            shutil.move(str(log), str(BASELINE_ROOT / f"runner-{index}.log"))
        shutil.rmtree(BASELINE_ROOT / f"run-{index}", ignore_errors=True)
    (BASELINE_ROOT / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def check(scratch: Path) -> dict[str, Any]:
    """Run every sample once into `scratch` and compare with the recorded `run-1`."""

    found = issues()
    if found:
        return {"skipped": found}
    result: dict[str, Any] = {}
    missing = [name for name in SAMPLES if not (BASELINE_ROOT / name / "run-1").is_dir()]
    for name in missing:
        result[name] = {"error": f"no baseline at {BASELINE_ROOT / name / 'run-1'}"}
    names = [name for name in SAMPLES if name not in missing]
    if names:
        summaries = run_samples(scratch, names)
        for name in names:
            result[name] = {"run": summaries[name],
                            "comparison": compare_runs(BASELINE_ROOT / name / "run-1", scratch / name)}
    return result


def environment_changes(recorded: dict[str, Any], current: dict[str, Any]) -> list[str]:
    """Card, image or accelerator facts that differ from the baseline's run-1 (reported, not judged)."""

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
    parser.add_argument("command", choices=("record", "issues", "check", "compare"))
    parser.add_argument("--scratch", type=Path)
    args = parser.parse_args()
    sys.path.insert(0, str(ROOT))
    from tests.quality_harness import cloud as _self  # the gate half, with its package for `real`

    if args.command == "issues":
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
