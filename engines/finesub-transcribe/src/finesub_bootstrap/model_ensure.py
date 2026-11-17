"""Check that one Hugging Face model is on disk before the stage that needs it.

Call `ensure_hf_model` at the entry of the stage that loads the weights, where
it costs a `stat` when they are already there. That is where
`separation.place_separator_files()` already sits for the separator's own
files; this is the same shape for the Hugging Face repositories.

**It no longer downloads anything** (Nonoka X WP-R7, `LOCAL_CHANGES.md` 0012).
The models come in Nonoka's prebuilt model layer, or on the cloud from a
priming script; a model that is missing, incomplete or marked as failed is an
installation problem, and raising `ModelUnavailable` says so where the run
needs the weights -- with no download subprocess, no mirror routing and no
network. Upstream (FineSub v0.7.0) fetched the repository here instead: one
`python -m finesub_bootstrap.model_ensure <id>` child per attempt, mirror
first, then verified with `verify_and_mark`.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Callable

from .hf_verify import (
    VerificationMismatch,
    files_present,
    marker_state,
    pinned_snapshot_present,
)
from .model_caches import (
    _ENSURABLE_HF_CACHE_DIRS,
    default_hf_home,
    existing_hf_home,
    managed_model_dirs,
)
from .model_manifest import entry_for



class ModelUnavailable(VerificationMismatch):
    """A model the manifest pins is not on disk as pinned, and nothing here will fetch it.

    A `VerificationMismatch` on purpose: that is the one failure
    `finesub.speech.runtime.hf_weights.prepare` lets through rather than
    handing the loader a cache it cannot trust -- and a loader handed a
    missing pinned snapshot goes to the network for it. So every caller treats
    a missing model the way it treats a corrupt one: the ASR weights end the
    run, the referee's and the speaker models' own call sites drop only what
    those models would have added.
    """


def _hub_dir(models_root: Path | None) -> Path:
    """The cache root a download will actually write into.

    The environment wins: a worker context resolved cache reuse already, and
    re-deriving it here could name a different directory than the one the
    download used -- which is how a freshly fetched model gets verified in the
    wrong place and reported missing.

    Read live, because the reader is the download *subprocess*, which imports
    `huggingface_hub` after this process hands it an environment. For the other
    question -- where a loader in *this* process will look -- see
    `_loader_hub_dir`.

    Three variables and not one: `huggingface_hub` still honours the legacy
    `HUGGINGFACE_HUB_CACHE`, and expands `~` and `$VAR` in all of them. Reading
    only `HF_HUB_CACHE` literally sent the verification to a directory the
    download had never written to.
    """

    for name in ("HF_HUB_CACHE", "HUGGINGFACE_HUB_CACHE"):
        configured = os.environ.get(name)
        if configured:
            return _expand(configured)
    home = os.environ.get("HF_HOME")
    if home:
        return _expand(home) / "hub"
    if models_root is not None:
        managed_hf, _ = managed_model_dirs(models_root)
        return existing_hf_home(managed_hf) / "hub"
    return default_hf_home() / "hub"


def _loader_hub_dir() -> Path:
    """The cache root a loader in this process will read.

    Asked of `huggingface_hub` rather than re-derived from the environment.
    The library resolves those variables once, at import, and freezes the
    answer into `constants.HF_HUB_CACHE`; every loader then reads that constant
    and nothing else. So this is not merely a tidier way to spell `_hub_dir` --
    it is the only spelling that stays true when a variable is set after the
    library was imported, and the only one that cannot drift from the library's
    own rules.

    Falls back to the plain rules when the library is absent: a `[harness]`-only
    install has no Hugging Face weights to load, and nothing will ask.
    """

    try:
        from huggingface_hub import constants

        # Normalised the way every download entry point normalises it
        # (`Path(cache_dir).expanduser().resolve()`), because the constant is
        # not the last word: `constants.py` expands `~` *before* `$VAR`, so a
        # variable that itself resolves to `~\...` leaves a literal tilde in
        # there for the download to expand later.
        return Path(constants.HF_HUB_CACHE).expanduser().resolve()
    except Exception:  # noqa: BLE001 - absent, or too old to say
        return _hub_dir(None)


def _expand(value: str) -> Path:
    """`~` and `$VAR` in a cache variable, the way the hub expands them."""

    return Path(os.path.expandvars(value)).expanduser().resolve()


def pinned_revision(model_id: str) -> str | None:
    """The revision the manifest pins for `model_id`, if it names one.

    For the loaders: a model that was just ensured and verified at one revision
    must be loaded at that same revision, or Hugging Face may re-resolve `main`
    to something else entirely -- silently undoing the verification.
    """

    entry = entry_for(model_id)
    return (entry.revision or None) if entry is not None else None


def pinned_snapshot_loadable(model_id: str) -> bool:
    """Whether a loader can reach `model_id`'s pinned snapshot without the network.

    The question `local_files_only=True` has to answer before it is safe to
    pass: it is the difference between "skip the hub round trips" and "fail
    where a download would have worked". `ensure_hf_model` having returned is
    *not* that answer -- it writes into the root `_hub_dir` picks, which for a
    managed install with no `HF_*` in the environment is this install's own
    cache while the loader reads the conventional one.

    So the root asked about here is `_loader_hub_dir` -- the one the library
    itself resolved -- and not `_hub_dir`, which answers where a download will
    land.

    Three facts, one more than the fast path above: it also stats the files the
    manifest lists. `_hf_repo_complete` deliberately accepts a snapshot whose
    big file is gone (deleted to reclaim the space, or never linked), and that
    is the one state where an offline load fails in a way no retry can safely
    be keyed on -- CTranslate2 raises a bare `RuntimeError`, indistinguishable
    from the CUDA failures that must not be retried. Cheap, because the check
    is a `stat` each and not the digest the verification path takes.

    And one marker state, but only one. "Stale" and "absent" are reasons to
    re-fetch, never reasons to go to the network for a file list -- that was
    the whole argument for ignoring the marker here. **"Failed" is not in that
    family**: it is positive evidence that the last verification of this very
    snapshot did not pass, and `_discard_failed` is explicitly best effort --
    it says so, and it says the failure marker is what fences off whatever it
    could not delete. A file locked by another process stays behind at the
    manifest's own size, so the three facts above all answer yes and the
    loader is handed bytes we already know are wrong. Whatever `_discard_failed`
    could not remove, this has to refuse.

    ⚠ The marker is read at `_loader_hub_dir()` -- **the root this function
    stats**, not the `_hub_dir(models_root)` that `verify_and_mark` writes to.
    On a managed install with no `HF_*` in the environment those are different
    directories, and asking the download root about the loader root's snapshot
    is a category error either way: the question here is whether *this* root
    can be loaded offline. A download that failed verification somewhere else
    is `prepare`'s problem, and it raises for it.
    """

    entry = entry_for(model_id)
    cache_dir = _ENSURABLE_HF_CACHE_DIRS.get(model_id)
    if entry is None or cache_dir is None:
        return False
    from .model_caches import _hf_repo_complete

    hub = _loader_hub_dir()
    return (
        marker_state(hub, cache_dir, entry) != "failed"
        and _hf_repo_complete(hub, cache_dir)
        and pinned_snapshot_present(hub, cache_dir, entry)
        and files_present(hub, cache_dir, entry)
    )


def pinned_snapshot_dir(model_id: str, *, models_root: Path | None = None) -> Path | None:
    """The pinned snapshot holding every file the manifest lists, for a consumer that opens them.

    `pinned_snapshot_loadable` answers a Hugging Face loader, which reads only the loader's own
    root. A consumer that is handed a directory -- the diarization backends open their TorchScript
    by path -- can use either root, so both are asked: where a download lands (`_hub_dir`) first,
    then the loader's. A root whose last verification failed is skipped, for the reason
    `pinned_snapshot_loadable` gives. Stats only; `ensure_hf_model` did the hashing.
    """

    entry = entry_for(model_id)
    cache_dir = _ENSURABLE_HF_CACHE_DIRS.get(model_id)
    if entry is None or cache_dir is None or not entry.revision:
        return None
    for hub in dict.fromkeys((_hub_dir(models_root), _loader_hub_dir())):
        if marker_state(hub, cache_dir, entry) == "failed":
            continue
        if files_present(hub, cache_dir, entry):
            return hub / cache_dir / "snapshots" / entry.revision
    return None


def _missing_reason(hub: Path, cache_dir: str, entry) -> str:
    """Which of the fast path's facts failed, in the words the error shows."""

    from .model_caches import _hf_repo_complete

    state = marker_state(hub, cache_dir, entry)
    if state == "failed":
        return "上次校验没有通过"
    if state == "stale":
        return "校验标记与模型清单对不上"
    if not _hf_repo_complete(hub, cache_dir) or not pinned_snapshot_present(hub, cache_dir, entry):
        return f"没有钉住的版本 {entry.revision or '(未钉)'}"
    return "清单列的文件不全或大小不对"


def ensure_hf_model(
    model_id: str,
    *,
    data_root: Path,
    models_root: Path | None = None,
    log: Callable[[str], None] | None = None,
) -> None:
    """Raise `ModelUnavailable` unless `model_id`'s pinned snapshot is on disk.

    A no-op -- one directory check -- when the weights are there, which is the
    common case and the reason this can sit on a hot path. Nothing is raised
    for a model the manifest does not describe: an unlisted model keeps the
    behaviour it has always had, which is that its own library loads it.

    `data_root` and `log` are what the callers have always passed; they served
    the download this no longer starts (Nonoka X WP-R7).
    """

    entry = entry_for(model_id)
    cache_dir = _ENSURABLE_HF_CACHE_DIRS.get(model_id)
    if entry is None or cache_dir is None:
        return
    hub = _hub_dir(models_root)
    from .model_caches import _hf_repo_complete

    # "Present" means the revision the manifest pins, not any revision -- the
    # loaders are handed that pin. A marker saying "stale" or "failed" also
    # voids the fast path; "absent" does not, because a cache that predates the
    # mechanism is not evidence of damage.
    # ⚠ And every file the manifest lists, at its size. A snapshot interrupted
    # between two files is non-empty with nothing `.incomplete`, which the
    # checks above call done; a consumer that opens the files by path (the
    # diarization backends) has no fallback (Codex review 2026-09-21). Stats only.
    if (
        _hf_repo_complete(hub, cache_dir)
        and pinned_snapshot_present(hub, cache_dir, entry)
        and files_present(hub, cache_dir, entry)
        and marker_state(hub, cache_dir, entry) in ("absent", "current")
    ):
        return
    raise ModelUnavailable(
        f"模型 {model_id} 不可用：{hub / cache_dir}（{_missing_reason(hub, cache_dir, entry)}）。"
        "引擎不会自己下载模型，请在设置里安装或修复模型层。"
    )
