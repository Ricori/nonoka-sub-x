"""The correction layer's contract: everything after `transcript.json`.

`docs/transcription-engines-plan.md` section 5.1. A correction engine takes a
transcription result -- `transcript.json`, plus the opaque native artifacts
of whichever engine produced it -- and renders subtitles, labels speakers,
corrects and translates. It runs for `raw-srt` too, then without the LLM.

This package depends on the transcription layer's *contract* only
(`nonoka_x.transcription` and its `model`), never on any transcription
engine's implementation, and the transcription layer never imports this one.
`tests/test_transcription_contract.py` holds both directions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping, Protocol, runtime_checkable

from nonoka_x.transcription import (
    ContractError,
    EngineEnvironment,
    Readiness,
    ReadinessContext,
    Registry,
    Reporter,
    TranscriptionResult,
    check_id,
)

#: The only correction engine for now; the UI offers no choice (plan 5.3).
DEFAULT_ENGINE = "finesub"
CORRECTION_TARGETS = ("raw-srt", "translated-srt", "final-srt")


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


@dataclass(frozen=True)
class CorrectionSpec:
    id: str
    label: str
    environment: EngineEnvironment = field(default_factory=EngineEnvironment)

    def __post_init__(self) -> None:
        check_id(self.id, "spec.id")
        if not isinstance(self.label, str) or not self.label:
            raise ContractError("spec.label must be a non-empty string")

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "label": self.label, "environment": self.environment.to_dict()}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "CorrectionSpec":
        return cls(
            id=data["id"],
            label=data["label"],
            environment=EngineEnvironment.from_dict(data.get("environment") or {}),
        )


def correction_capability(spec: CorrectionSpec, readiness: Readiness) -> dict[str, Any]:
    """One `Capabilities.correction_engines` item (`docs/provider-spec.md` 4.1)."""

    from ..runtime_cleanup import environment_layers

    return {"id": spec.id, **readiness.to_dict(), "layers": environment_layers(spec.environment)}


@dataclass(frozen=True)
class CorrectionJob:
    transcription: TranscriptionResult
    source: Path
    target: str
    task_dir: Path
    #: `TaskRequest.correction` as the caller prepared it for the engine: the
    #: provider's normalized block, with `extra_info` already composed with the
    #: picked knowledge subjects, and `fast` and `proofread` already resolved
    #: to the values the engine takes (the caller owns those two decisions).
    options: Mapping[str, Any] = field(default_factory=dict)
    #: `knowledge` (the mode) and `knowledge_subjects` -- the subjects to pin,
    #: as the names the engine takes, resolved by the caller (empty: none).
    knowledge: Mapping[str, Any] = field(default_factory=dict)
    diarization_level: str = "off"
    #: Names the task's subtitle files (`<title>.srt`); empty: the source's stem.
    title: str = ""
    task_id: str = ""
    #: What the post-correction knowledge update is told the task was.
    task_summary: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.transcription, TranscriptionResult):
            raise ContractError("job.transcription must be a TranscriptionResult")
        if self.target not in CORRECTION_TARGETS:
            raise ContractError(f"job.target must be one of {', '.join(CORRECTION_TARGETS)}, got {self.target!r}")
        for name in ("title", "task_id", "task_summary"):
            if not isinstance(getattr(self, name), str):
                raise ContractError(f"job.{name} must be a string")
        object.__setattr__(self, "source", Path(self.source))
        object.__setattr__(self, "task_dir", Path(self.task_dir))
        object.__setattr__(self, "options", _frozen(self.options))
        object.__setattr__(self, "knowledge", _frozen(self.knowledge))

    def to_dict(self) -> dict[str, Any]:
        return {
            "transcription": self.transcription.to_dict(),
            "source": str(self.source),
            "target": self.target,
            "task_dir": str(self.task_dir),
            "options": _plain(self.options),
            "knowledge": _plain(self.knowledge),
            "diarization_level": self.diarization_level,
            "title": self.title,
            "task_id": self.task_id,
            "task_summary": self.task_summary,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "CorrectionJob":
        return cls(
            transcription=TranscriptionResult.from_dict(data["transcription"]),
            source=Path(data["source"]),
            target=str(data["target"]),
            task_dir=Path(data["task_dir"]),
            options=data.get("options") or {},
            knowledge=data.get("knowledge") or {},
            diarization_level=str(data.get("diarization_level") or "off"),
            title=str(data.get("title") or ""),
            task_id=str(data.get("task_id") or ""),
            task_summary=str(data.get("task_summary") or ""),
        )


@dataclass(frozen=True)
class CorrectionResult:
    """The artifacts a run produced, by name.

    `correction_json` is the result in Nonoka's format (`docs/provider-spec.md`,
    "correction.json"): what the projection reads and the cloud library syncs
    (WP-L). The subtitles keep their `ArtifactManifest` names (`raw_srt`,
    `final_srt`, ...); anything else an engine lists is its own attachment,
    which the core does not parse.
    """

    artifacts: Mapping[str, Path] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        artifacts: dict[str, Path] = {}
        for key, value in dict(self.artifacts).items():
            if not isinstance(key, str) or not key:
                raise ContractError("artifact names must be non-empty strings")
            artifacts[key] = Path(value)
        object.__setattr__(self, "artifacts", MappingProxyType(artifacts))
        object.__setattr__(self, "metadata", _frozen(self.metadata))

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifacts": {key: str(path) for key, path in self.artifacts.items()},
            "metadata": _plain(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "CorrectionResult":
        return cls(
            artifacts={key: Path(value) for key, value in (data.get("artifacts") or {}).items()},
            metadata=data.get("metadata") or {},
        )


@runtime_checkable
class CorrectionEngine(Protocol):
    spec: CorrectionSpec

    def readiness(self, context: ReadinessContext, request: Mapping[str, Any]) -> Readiness: ...

    def run(self, job: CorrectionJob, report: Reporter) -> CorrectionResult: ...


#: The process-wide correction registry; nothing is registered by this module.
registry: Registry[CorrectionEngine] = Registry("correction")


def requested_engine(request: Mapping[str, Any]) -> str:
    """`TaskRequest.correction.engine`, `finesub` when absent (plan 5.3)."""

    block = request.get("correction")
    engine = block.get("engine") if isinstance(block, Mapping) else None
    if engine is None:
        return DEFAULT_ENGINE
    return check_id(engine, "correction.engine")


__all__ = [
    "CORRECTION_TARGETS",
    "DEFAULT_ENGINE",
    "CorrectionEngine",
    "CorrectionJob",
    "CorrectionResult",
    "CorrectionSpec",
    "correction_capability",
    "registry",
    "requested_engine",
]
