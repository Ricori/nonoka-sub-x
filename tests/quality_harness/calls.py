"""Call equivalence: what a task request makes Nonoka hand each FineSub algorithm.

Each scenario is one TaskRequest as the desktop would send it. A child
interpreter (torch, numpy and the rest refused) binds the settings the sidecar
binds, normalizes the request with `local_provider.validate_request`, installs
the recording stand-ins (`stubs`) and runs `nonoka_x.worker.main` on it, the
request on stdin exactly as `LocalProvider._run_worker` writes it. The record
-- the normalized request, every algorithm entry point's bound arguments in
call order, the model routing at the correction task, and the artifact
manifest -- is normalized (temporary roots, writer-unique part names) and
compared with `tests/fixtures/quality/calls/<scenario>.json`.

Run as `python -m tests.quality_harness.calls --child <spec.json>` for one
child; everything else here is the parent side.
"""

from __future__ import annotations

import argparse
import contextlib
import copy
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Iterable, Mapping

from . import ENGINES, FIXTURES, ROOT

BASELINE_DIR = FIXTURES / "calls"
STABLE_FIXTURE = ROOT / "tests" / "fixtures" / "finesub" / "sample-stable.json"
TASK_ID = "gate-task"
#: The knowledge package's entry module (`engines/finesub-knowledge/PACKAGE.json`).
KNOWLEDGE_ENTRY = "finesub_knowledge_host"

# --- settings the sidecar would have bound -----------------------------------

#: Fixed inputs for `FineSubSettings`: the desktop's settings page saving these.
#: The keys are fakes and the endpoint is a closed local port; nothing calls it.
SETTINGS_PROFILES: dict[str, dict[str, Any]] = {
    "compat": {
        "keys": {
            "OPENAI_COMPAT_API_KEY": "gate:sk-quality-gate-compat",
            "OPENAI_COMPAT_BASE_URL": "http://127.0.0.1:9/v1",
            "LLM_DEFAULT_PROVIDER": "openai-compat",
            "LLM_DEFAULT_MODEL": "gate-default-model",
            "LLM_ROUTE_CORRECTION_PROVIDER": "openai-compat",
            "LLM_ROUTE_CORRECTION_MODEL": "gate-correction-model",
            "LLM_ROUTE_RESEARCH_PROVIDER": "openai-compat",
            "LLM_ROUTE_RESEARCH_MODEL": "gate-research-model",
        },
    },
    "cloud": {
        "cloud_account": ("https://cloud.quality-gate.invalid", "gate-cloud-key"),
        "keys": {
            "LLM_DEFAULT_PROVIDER": "nonoka-cloud",
            "LLM_DEFAULT_MODEL": "gpt-6.1-terra",
            "LLM_ROUTE_CORRECTION_PROVIDER": "nonoka-cloud",
            "LLM_ROUTE_CORRECTION_MODEL": "gpt-6.1-terra",
        },
    },
}


def bind_settings(data_dir: Path, profile: str) -> Any:
    """What `nonoka_x.sidecar.main` does before it serves: bind, then save the fixed settings."""

    from nonoka_x.settings import FineSubSettings

    settings = FineSubSettings(data_dir)
    settings.bind_environment()
    spec = SETTINGS_PROFILES[profile]
    if "cloud_account" in spec:
        settings.set_cloud_account(*spec["cloud_account"])
    settings.update_keys(spec["keys"])
    settings.refresh_model_routes()
    return settings


# --- the request matrix -------------------------------------------------------

BASE_REQUEST: dict[str, Any] = {
    "schema": 1,
    "provider": "local",
    "source": {"kind": "local_file", "path": "$MEDIA", "title": "节目"},
    "language": "ja",
}

SUBJECT_NEW = {"kind": "streamer", "subject": "猫又おかゆ", "aliases": "Okayu、おかゆ", "description": "Hololive 所属"}
SUBJECT_EXISTING = {"id": "$SUBJECT_ID", "kind": "streamer", "subject": "柊優花", "aliases": "", "description": ""}
AXIS = {
    "kind": "ja",
    "filename": "轴.ass",
    "rows": [
        {"t0": 1.0, "t1": 2.0, "ja": "おはよう", "zh": "", "spk": ""},
        {"t0": 2.2, "t1": 3.0, "ja": "ございます", "zh": "", "spk": "A"},
        {"t0": 4.0, "t1": 5.0, "ja": "", "zh": "", "spk": ""},
    ],
}


#: A cloud recognition as the desktop hands it to the core (WP-CL): the cloud's
#: `transcript.json` and the transcription package's stable (and timeline) as
#: its opaque attachments.
CLOUD_SOURCE = {
    "kind": "cloud_transcript",
    "transcript_json": "$CLOUD_TRANSCRIPT",
    "native": {"finesub.stable_json": "$CLOUD_STABLE"},
    "native_package": "finesub-transcribe",
}
CLOUD_SOURCE_WITH_TIMELINE = {
    **CLOUD_SOURCE,
    "native": {"finesub.stable_json": "$CLOUD_STABLE", "finesub.diar_json": "$CLOUD_DIAR"},
}


def write_cloud_transcript(stable: Path, path: Path) -> None:
    """The cloud's `transcript.json` of `stable`: what its CPU leg writes, the transcription package's projection."""

    import importlib.util

    module = ROOT / "engines" / "finesub-transcribe" / "adapter" / "finesub_transcribe" / "engine.py"
    spec = importlib.util.spec_from_file_location("quality_cloud_transcript", module)
    engine = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = engine  # its dataclasses look their module up
    spec.loader.exec_module(engine)
    engine.write_transcript(engine.project_transcript(stable, stage="stable", language="ja"), path)


def _scenario(target: str, *, settings: str = "compat", setup: Iterable[str] = (), **fields: Any) -> dict[str, Any]:
    request = copy.deepcopy(BASE_REQUEST)
    request["target"] = target
    for key, value in fields.items():
        if key == "source":
            request["source"] = {**request["source"], **value}
        else:
            request[key] = value
    return {"request": request, "settings": settings, "setup": list(setup)}


#: Every target, language, separation switch, speaker level (with and without
#: `max_speakers`), correction option, knowledge mode (with picked subjects),
#: cloud continuation (with and without a timeline) and the Japanese axis
#: appears at least once; so do the combinations that change each other.
SCENARIOS: dict[str, dict[str, Any]] = {
    # -- the ASR half
    "vocal-ja-defaults": _scenario("vocal"),
    "vocal-no-separate-cpu": _scenario("vocal", separate=False, device="cpu"),
    "aligned-ja-cuda1-standard": _scenario("aligned", device="cuda:1", gpu_tier="standard"),
    "aligned-zh-no-separate": _scenario("aligned", language="zh", separate=False),
    "stable-auto-acoustics-max3": _scenario(
        "stable", language="auto", diarization={"level": "acoustics", "max_speakers": 3}
    ),
    "stable-ja-cpu-tier": _scenario("stable", device="cpu", gpu_tier="cpu", knowledge="none"),
    "raw-srt-ja-defaults": _scenario("raw-srt"),
    "raw-srt-legacy-gpu-budget": _scenario("raw-srt", gpu_budget_gb=8, knowledge="none"),
    "raw-srt-judge-text-downgraded": _scenario(
        "raw-srt", diarization={"level": "judge-text", "max_speakers": None}, knowledge="none"
    ),
    "raw-srt-acoustics-read-subject-extra-info": _scenario(
        "raw-srt",
        setup=["subject"],
        diarization={"level": "acoustics"},
        knowledge="read",
        knowledge_subjects=[SUBJECT_EXISTING],
        correction={"extra_info": "这期是柊優花的游戏直播。"},
    ),
    "raw-srt-zh-update-subject": _scenario(
        "raw-srt",
        language="zh",
        knowledge="update",
        knowledge_subjects=[SUBJECT_NEW],
        correction={"extra_info": "背景", "extra_style": "简洁"},
    ),
    # -- the LLM half from a local recognition
    "translated-srt-text-efficiency": _scenario(
        "translated-srt",
        knowledge="none",
        correction={"media": "text", "retrieval": "none", "difficulty": "efficiency"},
    ),
    "final-srt-ja-defaults": _scenario("final-srt"),
    "final-srt-audio-local-quality-options": _scenario(
        "final-srt",
        knowledge="none",
        correction={
            "media": "audio", "retrieval": "local", "difficulty": "quality",
            "fast": "off", "extra_info": "嘉宾：戌神ころね", "extra_style": "口语化，保留语气词",
        },
    ),
    "final-srt-fast-on-retired": _scenario(
        "final-srt", knowledge="none", correction={"media": "text", "fast": "on"}
    ),
    "final-srt-judge-audio-max2-update-new-subject": _scenario(
        "final-srt",
        diarization={"level": "judge-audio", "max_speakers": 2},
        knowledge="update",
        knowledge_subjects=[SUBJECT_NEW],
        correction={"media": "audio", "retrieval": "none", "difficulty": "quality"},
    ),
    "final-srt-judge-text-text-media": _scenario(
        "final-srt",
        diarization={"level": "judge-text"},
        knowledge="none",
        correction={"media": "text", "retrieval": "none", "difficulty": "quality"},
    ),
    "final-srt-zh-update-subject-style": _scenario(
        "final-srt",
        language="zh",
        knowledge="update",
        knowledge_subjects=[SUBJECT_NEW],
        diarization={"level": "acoustics"},
        correction={"media": "audio", "extra_info": "背景信息", "extra_style": "简洁"},
    ),
    "final-srt-auto-read-existing-subject": _scenario(
        "final-srt",
        setup=["subject"],
        language="auto",
        knowledge="read",
        knowledge_subjects=[SUBJECT_EXISTING],
        correction={"media": "text", "retrieval": "native", "difficulty": "efficiency"},
    ),
    "final-srt-video-source": _scenario(
        "final-srt",
        source={"path": "$VIDEO", "title": "视频节目"},
        knowledge="none",
        correction={"media": "video"},
    ),
    "final-srt-video-on-audio-downgraded": _scenario(
        "final-srt", knowledge="none", correction={"media": "video"}
    ),
    "final-srt-llm-model-string": _scenario(
        "final-srt", knowledge="none", correction={"media": "text"},
        llm_model="correction-capable",
    ),
    "final-srt-llm-model-table": _scenario(
        "final-srt", knowledge="none", correction={"media": "text"},
        llm_model={"correction": "local-agy-gemini-3_8-flash", "research": "local-agy-gemini-3_8-flash"},
    ),
    "final-srt-legacy-collect-and-context": _scenario(
        "final-srt", setup=["subject"], knowledge="collect", correction={"media": "text"},
    ),
    "final-srt-legacy-update-context": _scenario(
        "final-srt", knowledge="update", knowledge_context=SUBJECT_NEW, correction={"media": "text"},
    ),
    "final-srt-cloud-route-proofread-off": _scenario(
        "final-srt", settings="cloud", knowledge="none", correction={"media": "text", "difficulty": "quality"},
    ),
    # -- continuing a cloud recognition
    "cloud-raw-srt-no-timeline": _scenario(
        "raw-srt",
        source=CLOUD_SOURCE,
        diarization={"level": "acoustics"},
        knowledge="none",
    ),
    "cloud-raw-srt-timeline-acoustics": _scenario(
        "raw-srt",
        setup=["cloud_diar"],
        source=CLOUD_SOURCE_WITH_TIMELINE,
        diarization={"level": "acoustics", "max_speakers": 4},
        knowledge="none",
    ),
    "cloud-final-srt-timeline-judge-audio": _scenario(
        "final-srt",
        setup=["cloud_diar"],
        source=CLOUD_SOURCE_WITH_TIMELINE,
        diarization={"level": "judge-audio"},
        knowledge="update",
        knowledge_subjects=[SUBJECT_NEW],
        correction={"media": "audio", "retrieval": "local", "difficulty": "quality"},
        vocal_profile="cost",
    ),
    "cloud-translated-srt-zh-update": _scenario(
        "translated-srt",
        source=CLOUD_SOURCE,
        language="zh",
        knowledge="update",
        knowledge_subjects=[SUBJECT_NEW],
        correction={"media": "text", "extra_style": "简洁"},
    ),
    # -- a Japanese source-text axis
    "axis-ja-text": _scenario("final-srt", axis=AXIS, knowledge="none", correction={"media": "text"}),
    "axis-ja-audio-zh-update-subject": _scenario(
        "final-srt",
        axis=AXIS,
        language="zh",
        knowledge="update",
        knowledge_subjects=[SUBJECT_NEW],
        correction={"media": "audio", "retrieval": "local", "extra_info": "轴的背景", "extra_style": "简洁"},
    ),
}


# --- normalization -------------------------------------------------------------

_PART_NAME = re.compile(r"\.part-\d+-[0-9a-f]{8}")
#: Since WP-P1 each FineSub package keeps its files of a task in its own directory there
#: (`native\\<package>\\`, plan 4.6 item 34) where both halves shared `workspace\\` before. The
#: baselines name `workspace`; both directories are recorded as it (only where they are, not what).
_NATIVE_DIRECTORY = re.compile(r"/native/finesub-(?:transcribe|correct)(?=/|$|[\s\"',;])")


class Normalizer:
    """Replaces a run's own roots with placeholders so two runs record the same."""

    def __init__(self, roots: Mapping[str, Path], literals: Mapping[str, str] | None = None) -> None:
        patterns: list[tuple[re.Pattern[str], str]] = [
            (re.compile(re.escape(literal)), placeholder) for literal, placeholder in (literals or {}).items()
        ]
        for placeholder, root in roots.items():
            spellings = {str(root), str(root.resolve()), root.as_posix(), root.resolve().as_posix()}
            spellings |= {spelling.replace("\\", "\\\\") for spelling in spellings}
            spellings |= {f"file:///{root.resolve().as_posix()}"}
            for spelling in sorted(spellings, key=len, reverse=True):
                patterns.append((re.compile(re.escape(spelling), re.IGNORECASE), placeholder))
        self.patterns = patterns

    def text(self, value: str) -> str:
        for pattern, placeholder in self.patterns:
            value = pattern.sub(placeholder.replace("\\", "\\\\"), value)
        value = _PART_NAME.sub(".part-$WRITER", value)
        # Inside a placeholder path the separator is the platform's; record one.
        value = re.sub(
            r"(\$[A-Z_]+)((?:[\\/][^\s\"',;]*)*)",
            lambda match: match.group(1) + match.group(2).replace("\\\\", "/").replace("\\", "/"),
            value,
        )
        return _NATIVE_DIRECTORY.sub("/workspace", value)

    def value(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, list):
            return [self.value(item) for item in value]
        if isinstance(value, dict):
            return {self.text(str(key)): self.value(item) for key, item in value.items()}
        return value


#: Manifest entries whose bytes describe the run rather than its result
#: (timings, which ffmpeg ran): recorded as present, never hashed.
UNHASHED_ARTIFACTS = frozenset({"metadata_json"})
#: Nonoka's own formats in the manifest (WP-L): derived from FineSub's
#: products, not FineSub's, so not part of what the baseline pins (owner
#: 2026-11-18, WP-L question 4). `tests/test_projection_equivalence.py` and
#: `tests/test_correction_document.py` check them.
NONOKA_ARTIFACTS = frozenset({"transcript_json", "correction_json"})


def manifest_record(manifest: Mapping[str, Any], normalizer: Normalizer) -> dict[str, Any]:
    from urllib.parse import urlparse
    from urllib.request import url2pathname

    artifacts = {}
    for name, entry in sorted(manifest.get("artifacts", {}).items()):
        if name in NONOKA_ARTIFACTS:
            continue
        path = Path(url2pathname(urlparse(entry["uri"]).path))
        record: dict[str, Any] = {"file": normalizer.text(str(path))}
        if name not in UNHASHED_ARTIFACTS:
            data = path.read_bytes()
            try:
                data = normalizer.text(data.decode("utf-8")).encode("utf-8")
            except UnicodeDecodeError:
                pass
            record["sha256"] = hashlib.sha256(data).hexdigest()
        artifacts[name] = record
    return {"engine_commit": manifest.get("engine_commit"), "artifacts": artifacts}


# --- the child -----------------------------------------------------------------


def _substitute(value: Any, replacements: Mapping[str, str]) -> Any:
    if isinstance(value, str):
        return replacements.get(value, value)
    if isinstance(value, list):
        return [_substitute(item, replacements) for item in value]
    if isinstance(value, dict):
        return {key: _substitute(item, replacements) for key, item in value.items()}
    return value


def _setup_subject(data: Path) -> str:
    """An existing subject the request picks by id, the way the desktop's picker does.

    Made by the knowledge package, in a process of its own, as a task's
    preparation makes one; the setup is not what is recorded.
    """

    from nonoka_x import engine_contexts, engine_host
    from nonoka_x.runtime.paths import NonokaPaths

    package = engine_host.knowledge_package(engine_host.discover(ENGINES))
    item = {"kind": "streamer", "subject": "柊優花", "aliases": "柊优花、yuka", "description": "游戏主播"}
    paths = NonokaPaths.for_roots(data)
    knowledge_root = engine_contexts.knowledge_root(paths)
    with engine_contexts.build({package.id: package}, paths=paths) as built:
        with engine_host.PackageProcess(package, built.contexts[package.id]) as process:
            process.request("prepare_subjects", {"subjects": [item], "knowledge": "update", "task_id": "gate-setup",
                                                 "knowledge_root": str(knowledge_root)})
            listed = process.request("manage", {"request": {"action": "list"}})["result"]
    return next(entry["id"] for entry in listed["entries"] if entry["name"] == "柊優花")


def _setup_cloud_diar(path: Path) -> None:
    from finesub.speech.diarization.segments import TrackSegment
    from finesub.speech.diarization.stage import DiarizationResult, write_timeline

    write_timeline(
        DiarizationResult(
            tracks=(TrackSegment(0, 0.0, 2.1), TrackSegment(1, 2.1, 6.0)), frame_seconds=0.08, overlap=0.0
        ),
        path,
    )


def package_hook(spec: Mapping[str, Any], entry: str, context: Mapping[str, Any]) -> None:
    """In each engine package's process, once its bridge ran: the recording stand-ins (`stubs`).

    Not in the knowledge package's: the knowledge process a worker started for
    itself ran no stand-in either.
    """

    if entry == KNOWLEDGE_ENTRY:
        return
    from . import stubs
    from .package_child import SharedRecord

    stubs.RECORD = SharedRecord(Path(spec["record"]))
    stubs.install(STABLE_FIXTURE)


def child_main(spec_path: Path) -> int:
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    root = Path(spec["root"])
    from . import stubs, union

    union.install()
    from .package_child import read_shared, use_in_worker

    sys.meta_path.insert(0, stubs.RefuseHeavyImports())
    settings = bind_settings(root / "data", spec["settings"])
    adopt_worker_environment(root / "data", settings)
    record_file = root / "calls.jsonl"
    use_in_worker({"hook": "tests.quality_harness.calls:package_hook", "record": str(record_file),
                   # WP-H's read audit, when a test asks for one.
                   "audit": spec.get("audit")},
                  root / "package-child.json")

    replacements = {
        "$MEDIA": str(root / "media" / "节目.m4a"),
        "$VIDEO": str(root / "media" / "视频节目.mp4"),
        "$CLOUD_STABLE": str(root / "cloud" / "cloud.stable.json"),
        "$CLOUD_TRANSCRIPT": str(root / "cloud" / "cloud.transcript.json"),
        "$CLOUD_DIAR": str(root / "cloud" / "cloud.diar.json"),
    }
    if "subject" in spec["setup"]:
        # The knowledge base every launch context names (`nonoka_x.engine_contexts`).
        replacements["$SUBJECT_ID"] = _setup_subject(root / "data")
    if "cloud_diar" in spec["setup"]:
        _setup_cloud_diar(Path(replacements["$CLOUD_DIAR"]))

    from nonoka_x.local_provider import ProviderError, validate_request

    # Generated values the parent turns back into their placeholders.
    result: dict[str, Any] = {
        "literals": {value: key for key, value in replacements.items() if key == "$SUBJECT_ID"}
    }
    try:
        request = validate_request(_substitute(spec["request"], replacements))
    except ProviderError as error:
        result["rejected"] = {"code": error.code, "message": str(error)}
        Path(spec["out"]).write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
        return 0
    result["request"] = request

    from nonoka_x import worker

    task_dir = root / "data" / "tasks" / TASK_ID
    stdout = io.StringIO()
    previous_stdin = sys.stdin
    sys.stdin = io.StringIO(json.dumps(request, ensure_ascii=False) + "\n")
    try:
        with contextlib.redirect_stdout(stdout):
            code = worker.main(worker_arguments(task_dir, root / "data"))
    finally:
        sys.stdin = previous_stdin
    events = [json.loads(line) for line in stdout.getvalue().splitlines() if line.startswith("{")]
    result["exit"] = code
    result["calls"] = read_shared(record_file)
    completed = next((event for event in events if event["type"] == "completed"), None)
    result["manifest"] = completed["payload"]["artifacts"] if completed else None
    failed = [event["payload"].get("message") for event in events if event["type"] == "failed"]
    if failed:
        result["failed"] = failed
    Path(spec["out"]).write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    return 0


# --- the parent ------------------------------------------------------------------

#: Variables that would let the developer's own machine leak into a run.
def knowledge_base(data: Path) -> Path:
    """Where the launch contexts put the knowledge base of the data directory `data` (WP-M2: its package's directory)."""

    if str(ROOT / "src") not in sys.path:
        sys.path.insert(0, str(ROOT / "src"))
    from nonoka_x.engine_contexts import knowledge_root
    from nonoka_x.runtime.paths import NonokaPaths

    return knowledge_root(NonokaPaths.for_roots(data))


def hermetic_environment(root: Path) -> dict[str, str]:
    """A child environment with nothing of this machine's that could steer FineSub.

    `hygiene.isolated_environment`: the influencing variables removed and the
    profile directories (so every default location FineSub falls back to)
    replaced by empty ones. Then only what the child needs to run at all.
    """

    from .hygiene import isolated_environment

    environment, _removed = isolated_environment(root)
    environment.update({
        # Only so the child can import the core; it sets up what a scenario
        # needs with FineSub's own code through `union.install()` (on its own
        # `sys.path`, which no package process inherits), and `child_main` then
        # runs the worker under the environment the provider builds.
        "PYTHONPATH": str(ROOT / "src"),
        "PYTHONIOENCODING": "utf-8",
        "PYTHONDONTWRITEBYTECODE": "1",
        # Any key file in plaintext: DPAPI output differs on every write.
        # (Since WP-R5 no key is written and the bridge sets this itself.)
        "FINESUB_ENV_PROTECT": "0",
        "TEMP": str(root / "tmp"),
        "TMP": str(root / "tmp"),
    })
    return environment


def adopt_worker_environment(data_dir: Path, settings: Any) -> dict[str, str]:
    """Make this process's environment the one `LocalProvider` would spawn the worker into.

    Through the provider's own `_worker_environment()`, so whatever later
    builds that environment (a hermetic launcher) is what the record runs
    under. What FineSub reads of its own is not in it: each engine package's
    bridge derives that in the package's process, from the launch context
    the worker builds from the data directory (`worker_arguments`). Returns it.
    """

    from nonoka_x import local_provider
    from nonoka_x.provision import RuntimeProvisioner

    machine_tools_as_tool_layer()
    provisioner = RuntimeProvisioner(data_dir, ENGINES, None)
    provider = local_provider.LocalProvider(
        data_dir / "tasks", ENGINES, settings=settings, provisioner=provisioner, issues=[]
    )
    environment = provider._worker_environment()
    os.environ.clear()
    os.environ.update(environment)
    return environment


def machine_tools_as_tool_layer() -> None:
    """Without a tool layer, the tools this machine's PATH has stand in for it (WP-H).

    The gate runs no managed runtime here. Since WP-H a package's process gets
    no inherited PATH: it finds ffmpeg, ffprobe and tokcount only in the tool
    layer's directories its launch context names. So the provisioner of this
    run answers, for a tool its layer does not have, the one this machine's
    PATH finds -- what the package processes found on the inherited PATH when
    the baselines were recorded. Only where the tools are found changes.
    """

    from nonoka_x import provision

    original = provision.RuntimeProvisioner.tool_path
    if getattr(original, "_quality_machine_tools", False):
        return

    def tool_path(self: Any, name: str) -> Path | None:
        found = original(self, name)
        if found is None and name in ("ffmpeg", "ffprobe", "tokcount"):
            located = shutil.which(name)
            return Path(located) if located else None
        return found

    tool_path._quality_machine_tools = True  # type: ignore[attr-defined]
    provision.RuntimeProvisioner.tool_path = tool_path  # type: ignore[method-assign]


def worker_arguments(task_dir: Path, data_dir: Path) -> list[str]:
    """The worker's command line as `LocalProvider._default_worker_command` gives it (no runtime installed)."""

    return ["--task-id", TASK_ID, "--task-dir", str(task_dir), "--engines", str(ENGINES), "--data-dir", str(data_dir)]


def run_scenario(
    name: str,
    scratch: Path,
    *,
    audit: Path | None = None,
    prepare: Any = None,
) -> dict[str, Any]:
    """Run one scenario in a child and return its normalized record.

    For WP-H's tests: `audit` records what each engine package process reads
    of its environment into that directory (`package_child.EnvironmentAudit`);
    `prepare(root, environment)` may poison the child's environment and plant
    files before it starts, and returns the child's working directory (None:
    the repository root, as always).
    """

    scenario = SCENARIOS[name]
    root = (scratch / name).resolve()
    if root.exists():
        shutil.rmtree(root)
    for directory in ("media", "cloud", "data", "tmp"):
        (root / directory).mkdir(parents=True)
    (root / "media" / "节目.m4a").write_bytes(b"quality-gate audio placeholder")
    (root / "media" / "视频节目.mp4").write_bytes(b"quality-gate video placeholder")
    shutil.copyfile(STABLE_FIXTURE, root / "cloud" / "cloud.stable.json")
    write_cloud_transcript(root / "cloud" / "cloud.stable.json", root / "cloud" / "cloud.transcript.json")
    spec = {**scenario, "root": str(root), "out": str(root / "record.json")}
    if audit is not None:
        spec["audit"] = str(audit)
    spec_path = root / "spec.json"
    spec_path.write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
    environment = hermetic_environment(root)
    cwd = prepare(root, environment) if prepare is not None else None
    completed = subprocess.run(
        [sys.executable, "-m", "tests.quality_harness.calls", "--child", str(spec_path)],
        cwd=cwd or ROOT,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
    )
    out = root / "record.json"
    if completed.returncode != 0 or not out.is_file():
        raise RuntimeError(
            f"scenario {name} child failed ({completed.returncode}):\n{completed.stderr[-4000:]}"
        )
    raw = json.loads(out.read_text(encoding="utf-8"))
    normalizer = Normalizer({"$ROOT": root, "$REPO": ROOT}, raw.get("literals"))
    record: dict[str, Any] = {"scenario": name, "settings": scenario["settings"], "input": scenario["request"]}
    if "rejected" in raw:
        record["rejected"] = raw["rejected"]
        return record
    record["request"] = normalizer.value(raw["request"])
    record["exit"] = raw["exit"]
    if raw.get("failed"):
        record["failed"] = normalizer.value(raw["failed"])
    record["calls"] = normalizer.value(raw["calls"])
    record["manifest"] = manifest_record(raw["manifest"], normalizer) if raw.get("manifest") else None
    return record


def run_all(
    names: Iterable[str] | None = None, *, workers: int = 6, audit: Path | None = None, prepare: Any = None,
) -> dict[str, dict[str, Any]]:
    selected = list(names or SCENARIOS)
    scratch = Path(tempfile.mkdtemp(prefix="nonoka-quality-calls-"))
    try:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            results = list(pool.map(
                lambda name: run_scenario(name, scratch, audit=audit, prepare=prepare), selected
            ))
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    return dict(zip(selected, results))


def render(record: Any) -> str:
    return json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def baseline_path(name: str) -> Path:
    return BASELINE_DIR / f"{name}.json"


def write_baselines(records: Mapping[str, Any]) -> None:
    BASELINE_DIR.mkdir(parents=True, exist_ok=True)
    for name, record in records.items():
        baseline_path(name).write_bytes(render(record).encode("utf-8"))


def differences(records: Mapping[str, Any]) -> dict[str, str]:
    """Scenario -> unified diff against its baseline; empty when every one matches."""

    import difflib

    found: dict[str, str] = {}
    for name, record in records.items():
        path = baseline_path(name)
        actual = render(record)
        expected = path.read_text(encoding="utf-8") if path.is_file() else ""
        if actual != expected:
            found[name] = "".join(difflib.unified_diff(
                expected.splitlines(keepends=True),
                actual.splitlines(keepends=True),
                fromfile=f"baseline {path.relative_to(ROOT).as_posix()}",
                tofile=f"current {name}",
                n=2,
            )) or f"{path.relative_to(ROOT).as_posix()} is missing"
    for orphan in sorted({path.stem for path in BASELINE_DIR.glob("*.json")} - set(SCENARIOS)):
        found[orphan] = f"baseline {orphan}.json has no scenario"
    return found


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--child", type=Path, required=True)
    raise SystemExit(child_main(parser.parse_args().child))
