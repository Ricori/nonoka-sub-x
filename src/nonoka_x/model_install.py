"""The models in the model layer: which ids are there, whether a models root has them, and migration's completion.

Models are no longer downloaded one by one from Hugging Face or GitHub (plan 4.2 items 14, 19): the
model layer brings all of them, installed by `nonoka_x.runtime.layers.LayerManager`. What is left
here:

- the model ids the pipeline needs and the two speaker diarization needs (optional: a run asks for
  them only when it diarizes);
- `missing_managed_models`: whether a models root (the model layer, `FINESUB_MODEL_DIR`) holds the
  files Nonoka's model manifest pins for some ids, laid out as the engine reads them -- by presence
  and size, since the layer manager checks the content;
- `ModelCompletionHook`: migration's `model-completion` step (plan 4.4 item 25, owner 2026-11-13):
  the moved models are checked file by file against the model layer's manifest, and only when some
  are missing or wrong is the model layer archive fetched and just those files unpacked.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from nonoka_x.runtime import layer_manifest as lm

PIPELINE_MODEL_IDS = ("separator", "whisper", "qwen-referee")
#: Speaker diarization's default backend (FineSub 0.6.0 `nemotron3_backend`'s `HOSTED_MODEL` /
#: `HOSTED_EMBEDDING`). Optional: only a run that asks for diarization needs them.
DIARIZATION_MODEL_IDS = ("nemotron3", "speaker-embedding")
INSTALLABLE_MODEL_IDS = (*PIPELINE_MODEL_IDS, *DIARIZATION_MODEL_IDS)


def model_files(model_id: str) -> list[tuple[str, int | None]]:
    """The files Nonoka's model manifest pins for `model_id`, as paths under a models root, with sizes.

    The separator's files sit in `audio-separator\\`; a Hugging Face model's in its pinned snapshot
    (`huggingface\\hub\\models--<repo>\\snapshots\\<revision>\\`), the layout the engine reads.
    """

    from nonoka_x.runtime import manifests

    entry = manifests.model_manifest().get("models", {}).get(model_id)
    if entry is None:
        raise KeyError(model_id)
    if entry.get("repo"):
        base = "/".join((lm.HF_HOME_DIR, "hub", "models--" + entry["repo"].replace("/", "--"), "snapshots", entry["revision"]))
    else:
        base = lm.SEPARATOR_DIR
    return [(f"{base}/{item['name']}", item.get("size")) for item in entry.get("files") or ()]


def missing_managed_models(models_root: Path, model_ids: tuple[str, ...] = PIPELINE_MODEL_IDS) -> tuple[str, ...]:
    """The ids among `model_ids` whose pinned files are not all under `models_root` at their pinned sizes."""

    missing: list[str] = []
    for model_id in model_ids:
        try:
            files = model_files(model_id)
        except KeyError:
            missing.append(model_id)
            continue
        present = bool(files)
        for name, size in files:
            path = Path(models_root) / name
            try:
                if not path.is_file() or (size is not None and path.stat().st_size != int(size)):
                    present = False
                    break
            except OSError:
                present = False
                break
        if not present:
            missing.append(model_id)
    return tuple(missing)


class ModelCompletionHook:
    """Migration's `model-completion` step (`migration.hooks.MODEL_COMPLETION`).

    `context.migrated_models` is where WP-M1 left the old models root (`layers\\models\\migrated\\`);
    afterwards it is the model layer's current version, or the step failed and can be run again.
    """

    def __init__(self, open_manager: Callable[[Any], Any] | None = None) -> None:
        self._open = open_manager

    def _manager(self, context: Any) -> Any:
        if self._open is not None:
            return self._open(context)
        from nonoka_x.runtime.layers import LayerManager, load_index

        return LayerManager(context.paths, load_index())

    def download_bytes(self, context: Any) -> int | None:
        # How much is missing is only known once every moved file has been hashed.
        return None

    def run(self, context: Any) -> None:
        report = self._manager(context).complete_models(context.migrated_models, log=context.log)
        context.log(
            f"model-completion: version {report.version}, kept {report.kept}, fetched {len(report.fetched)}"
            f" ({report.source or 'nothing downloaded'}), rebuilt {len(report.rebuilt)} marker(s),"
            f" removed {len(report.removed)} file(s) the layer does not list"
        )


__all__ = [
    "DIARIZATION_MODEL_IDS",
    "INSTALLABLE_MODEL_IDS",
    "PIPELINE_MODEL_IDS",
    "ModelCompletionHook",
    "missing_managed_models",
    "model_files",
]
