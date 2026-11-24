"""Recording stand-ins for FineSub's algorithm entry points.

Installed into a child interpreter before `nonoka_x.worker` is imported (see
`calls.child_main`). Each stand-in records the arguments it was called with --
bound to the real function's signature, so positional versus keyword and an
omitted default versus the same default passed explicitly record the same --
and writes whatever placeholder artifact the next stage reads, so the whole
pipeline runs to the requested target without torch, numpy, a GPU or a model.

The record points are below the orchestration on purpose: these are functions
in locked engine files (separation, VAD + ASR, stabilization and recall
repair, diarization, device and tier probing, SRT rendering, the correction
task), not `run_pipeline` itself, so the record stays comparable after
`run_pipeline` is split up.

The torch-dependent modules (separation, VAD + ASR, recall decoding, device
probing) cannot be imported here, so they are replaced outright in
`sys.modules`; their signatures are read from the locked source with `ast`.
The others are imported for real and only the entry point is wrapped.
"""

from __future__ import annotations

import ast
import dataclasses
import enum
import importlib
import importlib.abc
import inspect
import json
import os
import sys
import types
from pathlib import Path, PurePath
from typing import Any, Callable, Mapping

from . import source_file

#: What a child must never import: the managed runtime's heavy stack.
BLOCKED_TOP_LEVEL = frozenset({
    "torch", "torchaudio", "numpy", "soundfile", "faster_whisper", "ctranslate2",
    "transformers", "audio_separator", "onnxruntime", "librosa", "scipy", "nemo",
})

RECORD: list[dict[str, Any]] = []


class RefuseHeavyImports(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):  # noqa: ANN001 - finder protocol
        if name.split(".")[0] in BLOCKED_TOP_LEVEL:
            raise ImportError(f"blocked by the quality gate: {name}")
        return None


class _SourceDefault:
    """A default the signature reader could not evaluate; recorded by its source text."""

    def __init__(self, text: str) -> None:
        self.text = text

    def __repr__(self) -> str:
        return f"<default {self.text}>"


def encode(value: Any) -> Any:
    """A JSON-able, order-stable form of an argument."""

    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, _SourceDefault):
        return repr(value)
    if isinstance(value, enum.Enum):
        return encode(value.value)
    if isinstance(value, PurePath):
        return {"path": str(value)}
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {
            "__type__": type(value).__name__,
            **{field.name: encode(getattr(value, field.name)) for field in dataclasses.fields(value)},
        }
    if isinstance(value, Mapping):
        return {str(key): encode(item) for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))}
    if isinstance(value, (list, tuple)):
        return [encode(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted(encode(item) for item in value)
    if getattr(value, "_quality_gate_marker", None) is not None:
        return value._quality_gate_marker
    if callable(value):
        # By kind only: a helper that moves inside the orchestration keeps its behaviour.
        return "<callable>"
    return {"__type__": type(value).__name__, "repr": repr(value)}


def signature_from_source(relative: str, function: str) -> inspect.Signature:
    """`function`'s signature as written in the locked file `relative`, without importing it."""

    source = source_file(relative).read_text(encoding="utf-8")
    tree = ast.parse(source)
    node = next(
        item for item in tree.body
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name == function
    )

    def default_of(expression: ast.expr | None) -> Any:
        if expression is None:
            return inspect.Parameter.empty
        try:
            return ast.literal_eval(expression)
        except ValueError:
            return _SourceDefault(ast.unparse(expression))

    arguments = node.args
    parameters: list[inspect.Parameter] = []
    positional = [*arguments.posonlyargs, *arguments.args]
    defaults = [None] * (len(positional) - len(arguments.defaults)) + list(arguments.defaults)
    for index, (argument, default) in enumerate(zip(positional, defaults)):
        kind = (
            inspect.Parameter.POSITIONAL_ONLY
            if index < len(arguments.posonlyargs)
            else inspect.Parameter.POSITIONAL_OR_KEYWORD
        )
        parameters.append(inspect.Parameter(argument.arg, kind, default=default_of(default)))
    if arguments.vararg is not None:
        parameters.append(inspect.Parameter(arguments.vararg.arg, inspect.Parameter.VAR_POSITIONAL))
    for argument, default in zip(arguments.kwonlyargs, arguments.kw_defaults):
        parameters.append(
            inspect.Parameter(argument.arg, inspect.Parameter.KEYWORD_ONLY, default=default_of(default))
        )
    if arguments.kwarg is not None:
        parameters.append(inspect.Parameter(arguments.kwarg.arg, inspect.Parameter.VAR_KEYWORD))
    return inspect.Signature(parameters)


def record(entry: str, signature: inspect.Signature, args: tuple, kwargs: dict) -> dict[str, Any]:
    """Append one call, its arguments bound and defaulted, and return the bound values."""

    bound = signature.bind(*args, **kwargs)
    bound.apply_defaults()
    values = dict(bound.arguments)
    RECORD.append({
        "entry": entry,
        "arguments": {name: encode(value) for name, value in values.items()},
        # The one environment variable the worker sets for the engine itself.
        "CUDA_VISIBLE_DEVICES": os.environ.get("CUDA_VISIBLE_DEVICES"),
    })
    return values


def note(entry: str, **values: Any) -> None:
    """Append an observation that is not a call (a routing snapshot, say)."""

    RECORD.append({"entry": entry, **{name: encode(value) for name, value in values.items()}})


class Marker:
    """A placeholder object handed back by a stand-in, recorded by name where it travels."""

    def __init__(self, name: str) -> None:
        self._quality_gate_marker = f"<stub {name}>"


# --- placeholder artifacts ---------------------------------------------------

STABLE_FIXTURE: Path | None = None


def _write_json(path: Path, value: Any) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def _stable_payload() -> Any:
    assert STABLE_FIXTURE is not None
    return json.loads(STABLE_FIXTURE.read_text(encoding="utf-8"))


SRT_STUB = "1\n00:00:01,000 --> 00:00:02,000\n[quality-gate stub]\n"


# --- module replacements ----------------------------------------------------


def _module(name: str, functions: Mapping[str, Callable[..., Any]], **constants: Any) -> types.ModuleType:
    module = types.ModuleType(name)
    module.__dict__.update(functions)
    module.__dict__.update(constants)

    def missing(attribute: str) -> Any:
        raise AttributeError(
            f"quality gate stub {name} has no {attribute!r}; add a recording stand-in for it"
        )

    module.__getattr__ = missing  # type: ignore[attr-defined]
    return module


def carried(name: str) -> bool:
    """Whether this process's engine package has the FineSub module `name` (since WP-P1 each has only its half).

    A stand-in or wrapper goes only where the module is: put into a package
    that lacks it, a stand-in would hide that the package lacks it, and a run
    that needs a module its package does not carry fails on that import, as
    it would on a user's machine.
    """

    import importlib.util

    try:
        return importlib.util.find_spec(name) is not None
    except ModuleNotFoundError:
        return False


def _install_module(name: str, module: types.ModuleType) -> None:
    if not carried(name):
        return
    sys.modules[name] = module
    parent_name, _, child = name.rpartition(".")
    parent = importlib.import_module(parent_name)
    setattr(parent, child, module)


def _separation() -> types.ModuleType:
    relative = "finesub/speech/preprocessing/separator/separation.py"
    separate_signature = signature_from_source(relative, "run_vocal_separation")
    encode_signature = signature_from_source(relative, "encode_asr_delivery")

    def run_vocal_separation(*args: Any, **kwargs: Any) -> Path:
        values = record("separation.run_vocal_separation", separate_signature, args, kwargs)
        target = Path(values["output_path"])
        target.write_bytes(b"OggS quality-gate separated vocal")
        return target

    def encode_asr_delivery(*args: Any, **kwargs: Any) -> Path:
        values = record("separation.encode_asr_delivery", encode_signature, args, kwargs)
        target = Path(values[list(encode_signature.parameters)[1]])
        target.write_bytes(b"OggS quality-gate transcoded source")
        return target

    return _module(
        "finesub.speech.preprocessing.separator.separation",
        {"run_vocal_separation": run_vocal_separation, "encode_asr_delivery": encode_asr_delivery},
    )


def _vad_asr() -> types.ModuleType:
    relative = "finesub/speech/recognition/vad_asr_stage.py"
    asr_signature = signature_from_source(relative, "run_vad_asr")
    tail_signature = signature_from_source(relative, "tail_verify_device")

    def run_vad_asr(*args: Any, **kwargs: Any) -> Path:
        values = record("vad_asr_stage.run_vad_asr", asr_signature, args, kwargs)
        if values.get("vad_prefix_path"):
            _write_json(Path(values["vad_prefix_path"]), {"quality_gate": "vad stub"})
        return _write_json(Path(values["output_path"]), _stable_payload())

    def tail_verify_device(*args: Any, **kwargs: Any) -> str:
        values = record("vad_asr_stage.tail_verify_device", tail_signature, args, kwargs)
        return str(next(iter(values.values())))

    return _module(
        "finesub.speech.recognition.vad_asr_stage",
        {"run_vad_asr": run_vad_asr, "tail_verify_device": tail_verify_device},
    )


def _recall_decode() -> types.ModuleType:
    signature = signature_from_source("finesub/speech/verification/recall_decode.py", "recall_repair")

    def recall_repair(*args: Any, **kwargs: Any) -> Marker:
        values = record("recall_decode.recall_repair", signature, args, kwargs)
        # The hook asks for its devices only once it loads a model; asked here
        # so the record says where the repair would have run.
        devices = values.get("devices")
        if callable(devices):
            note("recall_decode.recall_repair.devices()", devices=devices())
        return Marker("recall_repair hook")

    return _module("finesub.speech.verification.recall_decode", {"recall_repair": recall_repair})


#: What the stand-in device module reports: one 8 GiB CUDA card, usable.
FAKE_CARD = {"present": True, "total_vram_gib": 8.0, "free_vram_gib": 6.0}


def _device() -> types.ModuleType:
    relative = "finesub/speech/runtime/device.py"

    def recorder(name: str, answer: Callable[[dict[str, Any]], Any]) -> Callable[..., Any]:
        signature = signature_from_source(relative, name)

        def stand_in(*args: Any, **kwargs: Any) -> Any:
            values = record(f"device.{name}", signature, args, kwargs)
            return answer(values)

        stand_in.__name__ = name
        return stand_in

    functions = {
        "cuda_device_present": recorder("cuda_device_present", lambda _v: FAKE_CARD["present"]),
        # FineSub 0.7.1 asks for the accelerator family first (Intel support):
        # the fake card is a CUDA one.
        "gpu_device": recorder("gpu_device", lambda _v: "cuda" if FAKE_CARD["present"] else ""),
        "gpu_device_present": recorder("gpu_device_present", lambda _v: FAKE_CARD["present"]),
        "total_vram_gib": recorder("total_vram_gib", lambda _v: FAKE_CARD["total_vram_gib"]),
        "free_vram_gib": recorder("free_vram_gib", lambda _v: FAKE_CARD["free_vram_gib"]),
        "warn_if_an_installed_card_is_unused": recorder("warn_if_an_installed_card_is_unused", lambda _v: None),
        "cuda_usable": recorder("cuda_usable", lambda _v: True),
        "cuda_unusable_reason": recorder("cuda_unusable_reason", lambda _v: None),
        "resolve_device": recorder("resolve_device", lambda v: str(v["requested_device"])),
        "resolve_asr_device": recorder(
            "resolve_asr_device",
            lambda v: str(v["requested_device"]) if v["gpu_allowed"] else "cpu",
        ),
        # FineSub 0.7.1's string rules: pure, so the same rule and not recorded.
        "validate_device_token": _validate_device_token,
        "device_type": _device_type,
        "is_gpu_device": lambda device: _device_type(device) != "cpu",
    }
    return _module("finesub.speech.runtime.device", functions)


def _validate_device_token(device: str | None) -> None:
    """`device.validate_device_token`: `cpu`, `cuda` or `xpu`, or not chosen."""

    if device is not None and str(device).strip().lower() not in {"cpu", "cuda", "xpu"}:
        raise ValueError(f"--device takes cpu, cuda or xpu, not {device!r}")


def _device_type(device: str | None) -> str:
    """`device.device_type`: the family of a device string."""

    text = str(device or "cpu").strip().lower()
    if text == "cuda" or text.startswith("cuda:"):
        return "cuda"
    if text == "xpu" or text.startswith("xpu:"):
        return "xpu"
    return "cpu"


# --- wrappers around real entry points ---------------------------------------


def _wrap(module_name: str, function: str, entry: str, replacement: Callable[[dict[str, Any], Callable[..., Any], tuple, dict], Any] | None = None) -> None:
    if not carried(module_name):
        return
    module = importlib.import_module(module_name)
    original = getattr(module, function)
    signature = inspect.signature(original)

    def stand_in(*args: Any, **kwargs: Any) -> Any:
        values = record(entry, signature, args, kwargs)
        if replacement is None:
            return original(*args, **kwargs)
        return replacement(values, original, args, kwargs)

    stand_in.__wrapped_original__ = original  # type: ignore[attr-defined]
    setattr(module, function, stand_in)


def _stabilize(values: dict[str, Any], _original, _args, _kwargs) -> Path:
    return _write_json(Path(values["output_path"]), _stable_payload())


def _diarize(values: dict[str, Any], _original, _args, _kwargs) -> Any:
    from finesub.speech.diarization.segments import TrackSegment
    from finesub.speech.diarization.stage import DiarizationResult

    return DiarizationResult(
        tracks=(TrackSegment(0, 0.0, 2.1), TrackSegment(1, 2.1, 6.0)),
        frame_seconds=0.08,
        overlap=0.0,
    )


def _routing_snapshot(request: Any) -> dict[str, Any]:
    from finesub.llm.routing import model_routes

    routes = model_routes.default_model_routes()
    difficulty = str(getattr(getattr(request, "profile", None), "difficulty", "") or "quality")
    bindings = {}
    for group_id in ("correction-mm", "correction-text", "research", "knowledge"):
        try:
            group, _cell = routes.resolve_binding(routes.active_preset_id, group_id, difficulty)
            bindings[group_id] = list(group.target_ids)
        except Exception as error:  # noqa: BLE001 - recorded, not hidden
            bindings[group_id] = f"<{type(error).__name__}: {error}>"
    return {
        "active_preset": routes.active_preset_id,
        "difficulty": difficulty,
        "runtime_overlay": dict(model_routes._RUNTIME_PREFERRED),
        "bindings": bindings,
    }


def _correct(values: dict[str, Any], _original, _args, _kwargs) -> Any:
    request = next(iter(values.values()))
    note("routing at run_correction_task", routing=_routing_snapshot(request))
    output = Path(request.output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    if getattr(request, "postprocess_profile", 0) is None:
        result = output.with_name(f"{output.with_suffix('').name}-translated.srt")
        result.write_text(SRT_STUB, encoding="utf-8")
    else:
        result = output
        output.write_text(SRT_STUB, encoding="utf-8")
        output.with_name(f"{output.stem}-annotated.csv").write_text("# quality-gate stub\n", encoding="utf-8")
    return types.SimpleNamespace(result=result)


def install(stable_fixture: Path) -> None:
    """Block the heavy stack and put every recording stand-in in place."""

    global STABLE_FIXTURE
    STABLE_FIXTURE = stable_fixture
    sys.meta_path.insert(0, RefuseHeavyImports())
    _install_module("finesub.speech.runtime.device", _device())
    _install_module("finesub.speech.preprocessing.separator.separation", _separation())
    _install_module("finesub.speech.recognition.vad_asr_stage", _vad_asr())
    _install_module("finesub.speech.verification.recall_decode", _recall_decode())
    _wrap("finesub.speech.postprocessing.stabilization", "run_asr_stabilize",
          "stabilization.run_asr_stabilize", _stabilize)
    _wrap("finesub.speech.diarization.stage", "run_diarization", "diarization.run_diarization", _diarize)
    _wrap("finesub.speech.diarization.stage", "write_rows", "diarization.write_rows")
    _wrap("finesub.speech.diarization.stage", "seed_legacy", "diarization.seed_legacy")
    _wrap("finesub.subtitles.rendering", "convert_json_to_srt", "rendering.convert_json_to_srt")
    _wrap("finesub.subtitles.postprocess", "postprocess_srt_file", "postprocess.postprocess_srt_file")
    for name in ("group_by_speaker", "render_speaker_srts", "render_ass"):
        _wrap("finesub.speech.diarization.export", name, f"speaker_export.{name}")
    _wrap("finesub.llm.routing.model_routes", "install_runtime_preferred",
          "model_routes.install_runtime_preferred")
    _wrap("finesub.llm.correction_translation", "run_correction_task",
          "correction_translation.run_correction_task", _correct)
