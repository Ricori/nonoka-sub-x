"""The transcription layer's contract: engine specs, the engine protocol, a registry.

`docs/transcription-engines-plan.md` sections 5.1-5.3. A transcription engine
turns one source into `transcript.json` (`model.py`) plus whatever native
artifacts it keeps for itself; the worker only ever talks to it through
`TranscriptionEngine`, so no engine -- FineSub included -- gets a branch of its
own anywhere else.

Every type that crosses the engine boundary is plain data that round-trips
through JSON (`to_dict` / `from_dict`). In-process engines get the objects;
an engine package running in its own process (plan section 4, items 30-33)
gets the same objects as JSON, through a proxy that implements this protocol.
Callbacks therefore never live inside a job: cancellation is asked of the
`Reporter`, which is the channel back to the caller either way.

`native` artifacts are opaque attachments: a map from `<engine>.<name>` to a
path. The core registers, stores and hands them on, and never parses them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import re
from types import MappingProxyType
from typing import Any, Callable, Generic, Iterator, Mapping, Protocol, TypeVar, runtime_checkable

from .model import (
    FILENAME as TRANSCRIPT_FILENAME,
    TranscriptDocument,
    TranscriptError,
    TranscriptLine,
    TranscriptWord,
    read_transcript,
    write_transcript,
)

#: The engine a request without a `transcription` block runs on (plan 5.3).
DEFAULT_ENGINE = "finesub"
#: The registry id the desktop gives Nonoka Cloud's FineSub (plan section 4, item 37).
CLOUD_ENGINE = "finesub-cloud"

#: `local-gpu` runs on this machine with the managed runtime; `cloud-api` runs
#: on this machine against a third-party API; `cloud-service` is not run here at
#: all -- the task goes to the cloud provider (Nonoka Cloud's FineSub).
ENGINE_KINDS = ("local-gpu", "cloud-api", "cloud-service")
GPU_NEEDS = ("none", "optional", "required")
DIARIZATION_LEVELS = ("off", "acoustics", "judge-text", "judge-audio")

_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_NATIVE_KEY = re.compile(r"^[a-z0-9][a-z0-9-]*\.[a-z0-9][a-z0-9_.-]*$")


class ContractError(ValueError):
    """A spec, job or result that breaks the contract."""


def _frozen(value: Mapping[str, Any] | None) -> Mapping[str, Any]:
    return MappingProxyType(dict(value or {}))


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    return value


def _strings(value: Any, where: str) -> tuple[str, ...]:
    if isinstance(value, str) or not isinstance(value, (list, tuple)):
        raise ContractError(f"{where} must be a list of strings")
    items = tuple(value)
    for item in items:
        if not isinstance(item, str) or not item:
            raise ContractError(f"{where} must contain non-empty strings")
    return items


def check_id(value: Any, where: str = "id") -> str:
    if not isinstance(value, str) or not _ID.match(value):
        raise ContractError(f"{where} must be a lowercase id of letters, digits and '-', got {value!r}")
    return value


# --- readiness -----------------------------------------------------------------


@dataclass(frozen=True)
class Issue:
    """One reason something is not ready, in `Capabilities` terms (`{code, message}`)."""

    code: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Issue":
        return cls(code=str(data["code"]), message=str(data["message"]))


@dataclass(frozen=True)
class Readiness:
    """Ready exactly when there is no issue (`docs/provider-spec.md` 4.1)."""

    issues: tuple[Issue, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "issues", tuple(self.issues))

    @property
    def ready(self) -> bool:
        return not self.issues

    def to_dict(self) -> dict[str, Any]:
        return {"ready": self.ready, "issues": [issue.to_dict() for issue in self.issues]}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Readiness":
        readiness = cls(tuple(Issue.from_dict(item) for item in data.get("issues") or ()))
        if "ready" in data and bool(data["ready"]) != readiness.ready:
            raise ContractError("readiness.ready contradicts its issues")
        return readiness


@dataclass(frozen=True)
class ReadinessContext:
    """What a readiness check is asked about: one target, on one device.

    `target` is a TaskRequest target (`raw-srt`, `final-srt`, ...); None asks
    whether the engine can run at all. Engines read keys and runtime state
    through the `nonoka_x.config` and `nonoka_x.runtime` facades, not from here.
    """

    target: str | None = None
    language: str | None = None
    device: str | None = None
    options: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "options", _frozen(self.options))

    def to_dict(self) -> dict[str, Any]:
        return {
            "target": self.target,
            "language": self.language,
            "device": self.device,
            "options": _plain(self.options),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ReadinessContext":
        return cls(
            target=data.get("target"),
            language=data.get("language"),
            device=data.get("device"),
            options=data.get("options") or {},
        )


# --- reporting -------------------------------------------------------------------


@runtime_checkable
class Reporter(Protocol):
    """The channel from a running engine back to the task.

    Each call is one worker event (`docs/provider-spec.md` 9.1): `stage`,
    `progress`, `log`. `cancelled()` is how an engine learns it should stop.
    """

    def stage(self, stage: str, message: str = "") -> None: ...

    def progress(
        self, stage: str, *, completed: int, total: int | None = None, unit: str = "", message: str = ""
    ) -> None: ...

    def log(self, message: str, fields: Mapping[str, Any] | None = None) -> None: ...

    def warning(self, code: str, message: str) -> None: ...

    def cancelled(self) -> bool: ...


class EventReporter:
    """A `Reporter` that turns every call into a worker event `{type, payload}`."""

    def __init__(
        self,
        emit: Callable[[str, Mapping[str, Any]], None],
        cancelled: Callable[[], bool] | None = None,
    ) -> None:
        self._emit = emit
        self._cancelled = cancelled or (lambda: False)

    def stage(self, stage: str, message: str = "") -> None:
        self._emit("stage", {"stage": stage, "message": message})

    def progress(
        self, stage: str, *, completed: int, total: int | None = None, unit: str = "", message: str = ""
    ) -> None:
        payload: dict[str, Any] = {"stage": stage, "completed": completed, "unit": unit, "message": message}
        if total is not None:
            payload["total"] = total
        self._emit("progress", payload)

    def log(self, message: str, fields: Mapping[str, Any] | None = None) -> None:
        payload: dict[str, Any] = {"message": message}
        if fields:
            payload["fields"] = _plain(fields)
        self._emit("log", payload)

    def warning(self, code: str, message: str) -> None:
        self._emit("log", {"level": "warning", "code": code, "message": message})

    def cancelled(self) -> bool:
        return bool(self._cancelled())


# --- specs -----------------------------------------------------------------------


@dataclass(frozen=True)
class EngineFeatures:
    speakers: bool = False
    word_timestamps: bool = False
    separate: bool = False
    diarization_levels: tuple[str, ...] = ("off",)
    #: Language codes the engine accepts; empty means any.
    languages: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        levels = _strings(self.diarization_levels, "features.diarization_levels")
        unknown = sorted(set(levels) - set(DIARIZATION_LEVELS))
        if unknown:
            raise ContractError(f"features.diarization_levels has unknown levels: {', '.join(unknown)}")
        object.__setattr__(self, "diarization_levels", levels)
        object.__setattr__(self, "languages", _strings(self.languages, "features.languages"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "speakers": self.speakers,
            "word_timestamps": self.word_timestamps,
            "separate": self.separate,
            "diarization_levels": list(self.diarization_levels),
            "languages": list(self.languages),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "EngineFeatures":
        return cls(
            speakers=bool(data.get("speakers", False)),
            word_timestamps=bool(data.get("word_timestamps", False)),
            separate=bool(data.get("separate", False)),
            diarization_levels=tuple(data.get("diarization_levels") or ("off",)),
            languages=tuple(data.get("languages") or ()),
        )


@dataclass(frozen=True)
class EngineEnvironment:
    """What an engine needs installed (plan section 4, item 19).

    Component and model ids are the runtime manager's (`nonoka_x.runtime`).
    Required ones decide readiness; optional ones only enable enhancements.
    """

    components: tuple[str, ...] = ()
    models: tuple[str, ...] = ()
    optional_components: tuple[str, ...] = ()
    optional_models: tuple[str, ...] = ()
    gpu: str = "none"

    def __post_init__(self) -> None:
        for name in ("components", "models", "optional_components", "optional_models"):
            object.__setattr__(self, name, _strings(getattr(self, name), f"environment.{name}"))
        if self.gpu not in GPU_NEEDS:
            raise ContractError(f"environment.gpu must be one of {', '.join(GPU_NEEDS)}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "components": list(self.components),
            "models": list(self.models),
            "optional_components": list(self.optional_components),
            "optional_models": list(self.optional_models),
            "gpu": self.gpu,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "EngineEnvironment":
        return cls(
            components=tuple(data.get("components") or ()),
            models=tuple(data.get("models") or ()),
            optional_components=tuple(data.get("optional_components") or ()),
            optional_models=tuple(data.get("optional_models") or ()),
            gpu=str(data.get("gpu") or "none"),
        )


@dataclass(frozen=True)
class EngineSpec:
    id: str
    label: str
    kind: str
    features: EngineFeatures = field(default_factory=EngineFeatures)
    #: Names of the secrets the engine needs (`nonoka_x.config` key specs).
    keys: tuple[str, ...] = ()
    environment: EngineEnvironment = field(default_factory=EngineEnvironment)
    #: One line on what using the engine costs, shown before the user picks it.
    billing: str = ""

    def __post_init__(self) -> None:
        check_id(self.id, "spec.id")
        if not isinstance(self.label, str) or not self.label:
            raise ContractError("spec.label must be a non-empty string")
        if self.kind not in ENGINE_KINDS:
            raise ContractError(f"spec.kind must be one of {', '.join(ENGINE_KINDS)}, got {self.kind!r}")
        object.__setattr__(self, "keys", _strings(self.keys, "spec.keys"))

    @property
    def provider(self) -> str:
        """The TaskRequest provider a task on this engine goes to."""

        return "cloud" if self.kind == "cloud-service" else "local"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "kind": self.kind,
            "provider": self.provider,
            "features": self.features.to_dict(),
            "keys": list(self.keys),
            "environment": self.environment.to_dict(),
            "billing": self.billing,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "EngineSpec":
        spec = cls(
            id=data["id"],
            label=data["label"],
            kind=data["kind"],
            features=EngineFeatures.from_dict(data.get("features") or {}),
            keys=tuple(data.get("keys") or ()),
            environment=EngineEnvironment.from_dict(data.get("environment") or {}),
            billing=str(data.get("billing") or ""),
        )
        if "provider" in data and data["provider"] != spec.provider:
            raise ContractError("spec.provider contradicts spec.kind")
        return spec


def engine_capability(spec: EngineSpec, readiness: Readiness) -> dict[str, Any]:
    """One `Capabilities.transcription_engines` item (`docs/provider-spec.md` 4.1)."""

    return {
        "id": spec.id,
        "label": spec.label,
        "kind": spec.kind,
        "provider": spec.provider,
        **readiness.to_dict(),
        "features": spec.features.to_dict(),
        "billing": spec.billing,
        # The key names the engine reads, for the desktop's engine panel (WP6).
        "keys": list(spec.keys),
    }


# --- jobs ------------------------------------------------------------------------


@dataclass(frozen=True)
class TranscriptionJob:
    source: Path
    language: str
    task_dir: Path
    cache_dir: Path
    #: The TaskRequest target the task is heading for; the engine stops at its
    #: own last stage when the target lies beyond it.
    target: str
    options: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("source", "task_dir", "cache_dir"):
            object.__setattr__(self, name, Path(getattr(self, name)))
        object.__setattr__(self, "options", _frozen(self.options))

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": str(self.source),
            "language": self.language,
            "task_dir": str(self.task_dir),
            "cache_dir": str(self.cache_dir),
            "target": self.target,
            "options": _plain(self.options),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "TranscriptionJob":
        return cls(
            source=Path(data["source"]),
            language=str(data["language"]),
            task_dir=Path(data["task_dir"]),
            cache_dir=Path(data["cache_dir"]),
            target=str(data["target"]),
            options=data.get("options") or {},
        )


@dataclass(frozen=True)
class TranscriptionResult:
    """`transcript.json` plus the engine's own artifacts, which the core never parses."""

    transcript_json: Path
    native: Mapping[str, Path] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "transcript_json", Path(self.transcript_json))
        native: dict[str, Path] = {}
        for key, value in dict(self.native).items():
            if not isinstance(key, str) or not _NATIVE_KEY.match(key):
                raise ContractError(f"native key must be '<engine>.<name>', got {key!r}")
            native[key] = Path(value)
        object.__setattr__(self, "native", MappingProxyType(native))
        object.__setattr__(self, "metadata", _frozen(self.metadata))

    def read_transcript(self) -> TranscriptDocument:
        return read_transcript(self.transcript_json)

    def to_dict(self) -> dict[str, Any]:
        return {
            "transcript_json": str(self.transcript_json),
            "native": {key: str(path) for key, path in self.native.items()},
            "metadata": _plain(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "TranscriptionResult":
        return cls(
            transcript_json=Path(data["transcript_json"]),
            native={key: Path(value) for key, value in (data.get("native") or {}).items()},
            metadata=data.get("metadata") or {},
        )


@runtime_checkable
class TranscriptionEngine(Protocol):
    spec: EngineSpec

    def readiness(self, context: ReadinessContext) -> Readiness: ...

    def transcribe(self, job: TranscriptionJob, report: Reporter) -> TranscriptionResult: ...


# --- registry --------------------------------------------------------------------


class UnknownEngine(LookupError):
    def __init__(self, layer: str, engine_id: str, known: tuple[str, ...]) -> None:
        listed = ", ".join(known) or "none"
        super().__init__(f"no {layer} engine {engine_id!r} is registered (registered: {listed})")
        self.engine_id = engine_id


E = TypeVar("E")


class Registry(Generic[E]):
    """Engines by `spec.id`, in registration order. Shared by both layers."""

    def __init__(self, layer: str) -> None:
        self.layer = layer
        self._engines: dict[str, E] = {}

    def register(self, engine: E) -> E:
        engine_id = check_id(getattr(getattr(engine, "spec", None), "id", None), f"{self.layer} engine id")
        if engine_id in self._engines:
            raise ContractError(f"{self.layer} engine {engine_id!r} is already registered")
        self._engines[engine_id] = engine
        return engine

    def unregister(self, engine_id: str) -> None:
        self._engines.pop(engine_id, None)

    def get(self, engine_id: str) -> E:
        try:
            return self._engines[engine_id]
        except KeyError:
            raise UnknownEngine(self.layer, engine_id, self.ids()) from None

    def __contains__(self, engine_id: object) -> bool:
        return engine_id in self._engines

    def __iter__(self) -> Iterator[E]:
        return iter(list(self._engines.values()))

    def __len__(self) -> int:
        return len(self._engines)

    def ids(self) -> tuple[str, ...]:
        return tuple(self._engines)

    def clear(self) -> None:
        self._engines.clear()


#: The process-wide transcription registry. Engines register themselves here;
#: nothing is registered by this module.
registry: Registry[TranscriptionEngine] = Registry("transcription")


def requested_engine(request: Mapping[str, Any]) -> str:
    """`TaskRequest.transcription.engine`, `finesub` when absent (plan 5.3)."""

    block = request.get("transcription")
    if block is None:
        return DEFAULT_ENGINE
    if not isinstance(block, Mapping):
        raise ContractError("transcription must be an object")
    engine = block.get("engine")
    if engine is None:
        return DEFAULT_ENGINE
    return check_id(engine, "transcription.engine")


def requested_options(request: Mapping[str, Any]) -> Mapping[str, Any]:
    """`TaskRequest.transcription.options`, `{}` when absent."""

    block = request.get("transcription")
    options = block.get("options") if isinstance(block, Mapping) else None
    if options is None:
        return MappingProxyType({})
    if not isinstance(options, Mapping):
        raise ContractError("transcription.options must be an object")
    return _frozen(options)


__all__ = [
    "CLOUD_ENGINE",
    "DEFAULT_ENGINE",
    "DIARIZATION_LEVELS",
    "ENGINE_KINDS",
    "GPU_NEEDS",
    "TRANSCRIPT_FILENAME",
    "ContractError",
    "EngineEnvironment",
    "EngineFeatures",
    "EngineSpec",
    "EventReporter",
    "Issue",
    "Readiness",
    "ReadinessContext",
    "Registry",
    "Reporter",
    "TranscriptDocument",
    "TranscriptError",
    "TranscriptLine",
    "TranscriptWord",
    "TranscriptionEngine",
    "TranscriptionJob",
    "TranscriptionResult",
    "UnknownEngine",
    "check_id",
    "engine_capability",
    "read_transcript",
    "registry",
    "requested_engine",
    "requested_options",
    "write_transcript",
]
