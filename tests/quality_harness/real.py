"""Real recognition runs on the sample media, compared byte for byte.

Needs the managed FineSub runtime and its models on this machine; nothing here
installs or downloads anything. The worker is launched the way
`LocalProvider._run_worker` launches it -- the managed interpreter, the
provider's `_engine_environment()`, `-m nonoka_x.worker --task-id --task-dir
--vendor`, the validated request on stdin -- with two differences, both to
keep the run from reaching the network: every proxy variable points at a
closed local port and Hugging Face is offline. A stage that would download
fails instead.

Settings, knowledge base and engine state come from a fresh directory beside
the run, so the user's own configuration does not leak in.

Layout under `test_data/quality-baseline/real/` (never committed: `test_data/`
is ignored):

    <sample>/run-1/      the baseline run: request, events, task dir
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
import time
from pathlib import Path
from typing import Any

from . import ROOT, VENDOR, VENDOR_SOURCE

BASELINE_ROOT = ROOT / "test_data" / "quality-baseline" / "real"
SAMPLES: dict[str, dict[str, Any]] = {
    "sample_ja": {
        "path": ROOT / "test_data" / "audio" / "sample_ja.m4a",
        "fields": {"language": "ja", "knowledge": "none"},
    },
    "sample_multi_person": {
        "path": ROOT / "test_data" / "audio" / "sample_multi_person.aac",
        "fields": {"language": "ja", "knowledge": "none", "diarization": {"level": "acoustics"}},
    },
}
TARGET = "raw-srt"
#: The artifacts a rerun has to reproduce, by suffix of the engine's own names.
COMPARED_SUFFIXES = (
    "-vocal.ogg", "-vad.json", "-vad-energy.npz", "-aligned.json", "-stable.json",
    "-diar.json", "-diar-rows.json", "-diar-evidence.npz", "-raw.srt",
)
DEAD_PROXY = "http://127.0.0.1:9"

#: How an artifact that is not byte-reproducible even on unchanged code is
#: judged instead (owner decision 2026-11-11, after the two-run check): the
#: JSON documents by everything outside their top-level `metadata` (which holds
#: wall-clock timings), the vocal track by its decoded samples (an Ogg stream
#: serial number is random), and the VAD record by everything but the vocal
#: file's modification time and its elapsed-time fields (`without_run_timings`).
#: Any artifact not listed here and not byte-reproducible stays unjudged until
#: the owner rules on it.
ACCEPTED_STANDARDS = {
    "stable.json": "identical_outside_metadata",
    "aligned.json": "identical_outside_metadata",
    "vocal.ogg": "identical_decoded_samples",
    "vad.json": "identical_without_mtime_and_timings",
}


def without_run_timings(value: Any, *, top: bool = True) -> Any:
    """`-vad.json` without what describes the run rather than the speech (owner 2026-11-11).

    Drops exactly: `source.mtime_ns` (the vocal file is rewritten every run),
    the top-level `timing` object, and every key ending in `_sec` (elapsed
    seconds). Everything else -- the speech segments, energies, parameters --
    must be equal.
    """

    if isinstance(value, dict):
        kept = {}
        for key, item in value.items():
            if key.endswith("_sec") or (top and key == "timing"):
                continue
            if top and key == "source" and isinstance(item, dict):
                item = {k: v for k, v in item.items() if k != "mtime_ns"}
            kept[key] = without_run_timings(item, top=False)
        return kept
    if isinstance(value, list):
        return [without_run_timings(item, top=False) for item in value]
    return value


def locate_runtime() -> tuple[Path, Path | None]:
    """The data dir and install dir of a FineSub runtime on this machine.

    `NONOKA_QUALITY_DATA_DIR` / `NONOKA_QUALITY_INSTALL_DIR` win; otherwise the
    development data dir (`.local`, `scripts/run_local_sidecar.py`) when it has
    a runtime, then the installed app's (`%APPDATA%/Nonoka X`, whose
    `storage.json` names where the large install root went).
    """

    if os.environ.get("NONOKA_QUALITY_DATA_DIR"):
        install = os.environ.get("NONOKA_QUALITY_INSTALL_DIR")
        return Path(os.environ["NONOKA_QUALITY_DATA_DIR"]), Path(install) if install else None
    development = ROOT / ".local"
    if (development / "finesub" / "runtime").is_dir():
        return development, None
    appdata = Path(os.environ.get("APPDATA", "")) / "Nonoka X"
    storage = appdata / "storage.json"
    install = None
    if storage.is_file():
        runtime_dir = json.loads(storage.read_text(encoding="utf-8")).get("runtimeDir")
        install = Path(runtime_dir) if runtime_dir else None
    return appdata, install


def runtime_issues() -> list[str]:
    """Why the real runs cannot happen here; empty when they can."""

    sys.path[:0] = [str(ROOT / "src"), str(VENDOR_SOURCE)]
    from nonoka_x.model_install import INSTALLABLE_MODEL_IDS, missing_managed_models
    from nonoka_x.provision import RuntimeProvisioner

    data_dir, install_dir = locate_runtime()
    provisioner = RuntimeProvisioner(data_dir, VENDOR, install_dir)
    issues = []
    if provisioner._bootstrap_error:
        issues.append(f"bootstrap: {provisioner._bootstrap_error}")
    if provisioner.worker_python() is None:
        issues.append(f"no ready managed runtime under {provisioner.install_root}")
    elif provisioner._runtime_outdated():
        issues.append("the managed runtime does not match this vendor's lock")
    models = provisioner.worker_environment().get("FINESUB_MODEL_DIR")
    missing = missing_managed_models(Path(models), INSTALLABLE_MODEL_IDS) if models else ("all",)
    if missing:
        issues.append(f"models missing (would be downloaded): {', '.join(missing)}")
    for sample in SAMPLES.values():
        if not Path(sample["path"]).is_file():
            issues.append(f"sample missing: {sample['path']}")
    return issues


def _worker_launch(run_dir: Path) -> tuple[list[str], dict[str, str], Path, list[str]]:
    """The command and environment `LocalProvider` would spawn the worker with.

    Built from a scrubbed copy of this process's environment
    (`hygiene.scrubbed_environment`), the way a sidecar started on a clean
    machine would build it; the names removed are returned for the record.
    This process's own environment is restored afterwards.
    """

    sys.path[:0] = [str(ROOT / "src"), str(VENDOR_SOURCE)]
    from nonoka_x import local_provider
    from nonoka_x.provision import RuntimeProvisioner
    from nonoka_x.settings import FineSubSettings

    from .hygiene import scrubbed_environment

    saved = dict(os.environ)
    clean, removed = scrubbed_environment()
    os.environ.clear()
    os.environ.update(clean)
    try:
        settings = FineSubSettings(run_dir / "config")
        settings.bind_environment()
        data_dir, install_dir = locate_runtime()
        provisioner = RuntimeProvisioner(data_dir, VENDOR, install_dir)
        provider = local_provider.LocalProvider(
            run_dir / "tasks", VENDOR, settings=settings, provisioner=provisioner, issues=[]
        )
        # The legacy probe sweep edits the shared model directory; not ours to do.
        provider._separator_probe_checked = True
        environment = provider._engine_environment()
        task_dir = run_dir / "tasks" / "gate-real"
        command = provider._default_worker_command("gate-real", task_dir)
    finally:
        os.environ.clear()
        os.environ.update(saved)
    for name in ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "https_proxy", "http_proxy", "all_proxy"):
        environment[name] = DEAD_PROXY
    environment["NO_PROXY"] = environment["no_proxy"] = ""
    environment["HF_HUB_OFFLINE"] = "1"
    environment["TRANSFORMERS_OFFLINE"] = "1"
    return command, environment, task_dir, removed


def acceleration(run_dir: Path) -> dict[str, Any]:
    """What the run metadata says each worker ran with (device, accel), for comparing runs."""

    found: dict[str, Any] = {}
    for metadata in (run_dir / "tasks" / "gate-real" / "workspace").glob("*-metadata.json"):
        workers = json.loads(metadata.read_text(encoding="utf-8")).get("workers") or {}
        for name, record in workers.items():
            if isinstance(record, dict):
                found[name] = {key: record[key] for key in sorted(record) if "accel" in key or key == "device"}
    return found


def run_sample(name: str, run_dir: Path) -> dict[str, Any]:
    """One real worker run of `name` to `TARGET` in `run_dir`; returns what it left."""

    sys.path[:0] = [str(ROOT / "src"), str(VENDOR_SOURCE)]
    from nonoka_x.local_provider import validate_request

    if run_dir.exists():
        shutil.rmtree(run_dir)
    run_dir.mkdir(parents=True)
    sample = SAMPLES[name]
    request = validate_request({
        "schema": 1,
        "provider": "local",
        "source": {"kind": "local_file", "path": str(Path(sample["path"]).resolve()), "title": name},
        "target": TARGET,
        **sample["fields"],
    })
    command, environment, task_dir, removed = _worker_launch(run_dir)
    (run_dir / "request.json").write_text(json.dumps(request, ensure_ascii=False, indent=2), encoding="utf-8")
    (run_dir / "command.json").write_text(json.dumps(command, ensure_ascii=False, indent=2), encoding="utf-8")
    started = time.time()
    with (run_dir / "events.ndjson").open("w", encoding="utf-8") as events:
        process = subprocess.run(
            command,
            input=json.dumps(request, ensure_ascii=False) + "\n",
            stdout=events,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=environment,
            timeout=3600,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    summary = {
        "sample": name,
        "exit": process.returncode,
        "seconds": round(time.time() - started, 1),
        # Names only: these were in the launching environment and kept out of the worker's.
        "scrubbed_variables": removed,
        "acceleration": acceleration(run_dir),
    }
    (run_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return summary


def artifacts(run_dir: Path) -> dict[str, Path]:
    workspace = run_dir / "tasks" / "gate-real" / "workspace"
    found = {}
    for suffix in COMPARED_SUFFIXES:
        for path in workspace.glob(f"*{suffix}"):
            if path.name.startswith("."):
                continue
            found[suffix.lstrip("-")] = path
    return found


def _first_json_difference(left: Any, right: Any, where: str = "$") -> str:
    if type(left) is not type(right):
        return f"{where}: {type(left).__name__} vs {type(right).__name__}"
    if isinstance(left, dict):
        for key in sorted(set(left) | set(right)):
            if key not in left or key not in right:
                return f"{where}.{key}: only in {'second' if key not in left else 'first'}"
            found = _first_json_difference(left[key], right[key], f"{where}.{key}")
            if found:
                return found
        return ""
    if isinstance(left, list):
        if len(left) != len(right):
            prefix = next((index for index, (a, b) in enumerate(zip(left, right)) if a != b), min(len(left), len(right)))
            return f"{where}: length {len(left)} vs {len(right)} (first difference at [{prefix}])"
        for index, (a, b) in enumerate(zip(left, right)):
            found = _first_json_difference(a, b, f"{where}[{index}]")
            if found:
                return found
        return ""
    return "" if left == right else f"{where}: {json.dumps(left, ensure_ascii=False)[:120]} vs {json.dumps(right, ensure_ascii=False)[:120]}"


def _json_differences(left: Any, right: Any, where: str = "$", found: list[str] | None = None) -> list[str]:
    """Every path at which two JSON values differ (lists compared index by index)."""

    found = [] if found is None else found
    if type(left) is not type(right):
        found.append(where)
    elif isinstance(left, dict):
        for key in sorted(set(left) | set(right)):
            if key not in left or key not in right:
                found.append(f"{where}.{key}")
            else:
                _json_differences(left[key], right[key], f"{where}.{key}", found)
    elif isinstance(left, list):
        if len(left) != len(right):
            found.append(f"{where}[len]")
        for index, (a, b) in enumerate(zip(left, right)):
            _json_differences(a, b, f"{where}[{index}]", found)
    elif left != right:
        found.append(where)
    return found


def _decoded_pcm_sha256(path: Path) -> str | None:
    """The sha256 of an audio file's decoded samples, or None without ffmpeg.

    An Ogg container carries a stream serial number that encoders pick at
    random, so two encodings of identical samples differ in bytes; this says
    whether the samples themselves do.
    """

    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        return None
    decoded = subprocess.run(
        [ffmpeg, "-v", "error", "-i", str(path), "-map", "0:a:0", "-f", "f32le", "-"],
        capture_output=True, timeout=600, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return hashlib.sha256(decoded.stdout).hexdigest() if decoded.returncode == 0 else None


def _without_run_paths(text: str, run_dir: Path) -> str:
    for spelling in {str(run_dir), run_dir.as_posix(), str(run_dir).replace("\\", "\\\\")}:
        text = text.replace(spelling, "$RUN")
    return text


def compare_runs(first: Path, second: Path) -> dict[str, Any]:
    """Per compared artifact: identical bytes, or where the two runs part."""

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
        }
        if a == b:
            entry["status"] = "identical"
        else:
            entry["status"] = "differs"
            if key.endswith(".json") or key.endswith(".srt"):
                ta = _without_run_paths(a.decode("utf-8"), first)
                tb = _without_run_paths(b.decode("utf-8"), second)
                entry["identical_without_run_paths"] = ta == tb
                if key.endswith(".json") and ta != tb:
                    ja, jb = json.loads(ta), json.loads(tb)
                    entry["first_difference"] = _first_json_difference(ja, jb)
                    paths = _json_differences(ja, jb)
                    entry["differing_paths"] = paths[:200]
                    entry["differing_path_count"] = len(paths)
                    if isinstance(ja, dict) and isinstance(jb, dict):
                        outside_a = {k: v for k, v in ja.items() if k != "metadata"}
                        outside_b = {k: v for k, v in jb.items() if k != "metadata"}
                        entry["identical_outside_metadata"] = outside_a == outside_b
                        if key == "vad.json":
                            clean_a, clean_b = without_run_timings(ja), without_run_timings(jb)
                            entry["identical_without_mtime_and_timings"] = clean_a == clean_b
                            if clean_a != clean_b:
                                entry["first_difference"] = _first_json_difference(clean_a, clean_b)
                        elif outside_a != outside_b:
                            entry["first_difference"] = _first_json_difference(outside_a, outside_b)
                elif ta != tb:
                    lines_a, lines_b = ta.splitlines(), tb.splitlines()
                    index = next((i for i, (x, y) in enumerate(zip(lines_a, lines_b)) if x != y), min(len(lines_a), len(lines_b)))
                    entry["first_difference"] = {
                        "line": index + 1,
                        "first": lines_a[index] if index < len(lines_a) else None,
                        "second": lines_b[index] if index < len(lines_b) else None,
                        "lines": [len(lines_a), len(lines_b)],
                    }
            elif key.endswith(".ogg"):
                pcm = (_decoded_pcm_sha256(left[key]), _decoded_pcm_sha256(right[key]))
                entry["decoded_pcm_sha256"] = list(pcm)
                entry["identical_decoded_samples"] = None if None in pcm else pcm[0] == pcm[1]
        result[key] = entry
    return result


def record() -> dict[str, Any]:
    """Run every sample twice into the baseline directory and write the report."""

    issues = runtime_issues()
    if issues:
        raise SystemExit("cannot run the real samples:\n  " + "\n  ".join(issues))
    from .hygiene import inventory

    report: dict[str, Any] = {"target": TARGET, "machine": inventory(), "samples": {}}
    for name in SAMPLES:
        runs = [run_sample(name, BASELINE_ROOT / name / f"run-{index}") for index in (1, 2)]
        report["samples"][name] = {
            "runs": runs,
            "comparison": compare_runs(BASELINE_ROOT / name / "run-1", BASELINE_ROOT / name / "run-2"),
        }
    (BASELINE_ROOT / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def check(scratch: Path) -> dict[str, Any]:
    """Run every sample once into `scratch` and compare with the recorded `run-1`."""

    issues = runtime_issues()
    if issues:
        return {"skipped": issues}
    result: dict[str, Any] = {}
    for name in SAMPLES:
        baseline = BASELINE_ROOT / name / "run-1"
        if not baseline.is_dir():
            result[name] = {"error": f"no baseline at {baseline}"}
            continue
        summary = run_sample(name, scratch / name)
        result[name] = {"run": summary, "comparison": compare_runs(baseline, scratch / name)}
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("record", "issues", "check", "compare"))
    parser.add_argument("--scratch", type=Path)
    args = parser.parse_args()
    if args.command == "issues":
        print(json.dumps(runtime_issues(), ensure_ascii=False, indent=2))
    elif args.command == "compare":
        # Recompute the report from the runs already on disk, without running anything.
        report_path = BASELINE_ROOT / "report.json"
        report = json.loads(report_path.read_text(encoding="utf-8"))
        for name in SAMPLES:
            report["samples"][name]["comparison"] = compare_runs(
                BASELINE_ROOT / name / "run-1", BASELINE_ROOT / name / "run-2"
            )
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False, indent=2))
    elif args.command == "record":
        print(json.dumps(record(), ensure_ascii=False, indent=2))
    else:
        print(json.dumps(check(args.scratch), ensure_ascii=False, indent=2))
