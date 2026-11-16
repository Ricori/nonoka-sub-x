"""What is left of fetching model weights: the endpoint rules and the fixed-file check.

The engine no longer downloads a model itself (Nonoka X WP-R7,
`LOCAL_CHANGES.md` 0012): the weights come in Nonoka's prebuilt model layer.
`fetch_fixed_files` checks the separator's files and raises when one is
missing or wrong instead of fetching it; `fetch_with_fallback` (mirror first,
then the official source) and `_fetch_unverified` (the separator's undigested
index) are gone with the download. The endpoint rules below stay for the
installer (`RuntimeEnvironment.worker_context`), which still sets them.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from finesub_bootstrap import download_routes

#: The resource class these downloads are accounted under, for the per-machine
#: degrade rule.
RESOURCE_CLASS = "huggingface"

HF_ENDPOINT = "HF_ENDPOINT"

#: `huggingface_hub` 1.x downloads through Xet unless this says otherwise.
HF_DISABLE_XET = "HF_HUB_DISABLE_XET"

#: Without this `huggingface_hub` sends the user's own login to whatever host `HF_ENDPOINT` names.
HF_DISABLE_IMPLICIT_TOKEN = "HF_HUB_DISABLE_IMPLICIT_TOKEN"

#: Appended to a model file's name to record that it was verified in full.
#:
#: The separator checkpoint is 639 MB and is checked before *every* separation,
#: which meant hashing it from disk on every run. The stamp records what the
#: verification saw -- size, mtime and the digest it matched -- so a file that
#: has not been touched since is taken as verified without being read again.
#: Anything that does not line up (missing stamp, edited file, a manifest that
#: now pins a different digest) falls back to the full check, so the guarantee
#: is unchanged and only the repetition is gone.
VERIFIED_SUFFIX = ".verified"


def hf_endpoint_for(data_root: Path, region: str) -> str:
    """The Hugging Face endpoint for this region, or "" for the official one."""

    return download_routes.active_mirror(data_root, RESOURCE_CLASS, region)


def apply_mirror_policy(environment: dict[str, str], *, endpoint: str) -> dict[str, str]:
    """Turn Xet and the implicit token off whenever the traffic is aimed anywhere but the official host.

    Xet is not a speed knob -- without this the `cn` route cannot install a
    model at all. A mirror does not proxy Xet: the metadata it serves still
    points at the official `cas-server.xethub.hf.co` and carries a short-lived
    token that CAS does not accept, so the metadata request succeeds and the
    *first* reconstruction request comes back 401. Plain HTTP range requests are
    what a mirror serves anyway, so nothing is given up by asking for them.

    The token: left to itself the library sends the user's own login
    (`HF_TOKEN`, or the file `huggingface-cli login` wrote) to whatever host
    `HF_ENDPOINT` names -- on the `cn` route a third party's mirror, and often a
    token with write access. Every model FineSub fetches is public. The prefetch
    subprocess passes `token=False` for the same reason
    (`model_ensure._token_for`); this covers the libraries' own lazy downloads
    in the workers (audit X-2).

    **Both switches follow the endpoint, not who chose it.** A user's own
    `HF_ENDPOINT` hits exactly the same wall and receives the same token, and
    both are facts about the host rather than the region guess that
    `apply_hf_endpoint` refuses to make on their behalf. A variable the user set
    explicitly still wins either way -- a gateway that speaks Xet, or one of
    their own that needs their token, is theirs to declare.
    """

    for name in (HF_DISABLE_XET, HF_DISABLE_IMPLICIT_TOKEN):
        if name in os.environ:
            continue
        if endpoint:
            environment[name] = "1"
        else:
            environment.pop(name, None)
    return environment


def apply_hf_endpoint(
    environment: dict[str, str],
    *,
    data_root: Path,
    region: str,
) -> dict[str, str]:
    """Point Hugging Face traffic at the configured endpoint, if any.

    A user who set `HF_ENDPOINT` themselves is left alone -- they pointed it
    somewhere on purpose, and a region guess is not a reason to overrule them.
    Their endpoint still gets the Xet treatment above, which is a different
    question with a different answer.
    """

    endpoint = os.environ.get(HF_ENDPOINT) or ""
    if not endpoint:
        endpoint = hf_endpoint_for(data_root, region)
        if endpoint:
            environment[HF_ENDPOINT] = endpoint
    return apply_mirror_policy(environment, endpoint=endpoint)


#: What a failure says when a host, rather than this machine, is at fault.
#: Needed because some of these failures cross a process boundary -- a
#: download that ran in a subprocess hands back its message, never the httpx
#: exception.
NETWORK_FAILURE_MARKERS = (
    "connection",
    "timed out",
    "timeout",
    "temporary failure",
    "network",
    "reset by peer",
    "unreachable",
    "name resolution",
    "ssl",
    "502",
    "503",
    "504",
    "429",
    "404",
    # An auth status from a host we never authenticated to is the mirror's
    # doing, and the official source is exactly what fixes it -- the Xet 401
    # (see `apply_mirror_policy`) was one shape of this. In-process these arrive
    # as `httpx.HTTPStatusError` and the branch above already catches them;
    # these two markers are the *cross-process* half, which is the half that
    # matters here because every download runs in its own interpreter.
    "401",
    "403",
)

#: Local trouble, stated explicitly so an unrecognised message cannot be
#: quietly blamed on a mirror.
LOCAL_FAILURE_MARKERS = (
    "no space left",
    "disk full",
    "permission denied",
    "access is denied",
    "out of memory",
)


def is_mirror_failure(error: BaseException) -> bool:
    """Whether the mirror could plausibly be responsible for this.

    A wrong or short body and a refused connection are the mirror's doing, and
    the official source is the fix. A full disk is not: retrying downloads
    several GB a second time to fail the same way, and recording it would
    degrade a mirror that did nothing wrong.

    Unrecognised failures are *not* blamed on the mirror. That costs a fallback
    we might have wanted, which is recoverable; the opposite spends gigabytes
    and disables a working mirror on evidence that never pointed at it.
    """

    import httpx

    from finesub_bootstrap.downloader import DownloadError, DownloadPaused
    from finesub_bootstrap.hf_verify import MISMATCH_MARKER, VerificationMismatch

    if isinstance(error, DownloadPaused):
        # The user stopped it. Starting again elsewhere is not a recovery.
        return False
    if isinstance(error, DownloadError):
        # A short or wrong body -- the mirror served something else.
        return True
    if isinstance(error, VerificationMismatch):
        # Length right, bytes wrong: HTTP called it a success and only the
        # manifest knows better. Safe to retry elsewhere because the verifier
        # already removed the corrupt files -- the fallback re-fetches them
        # rather than re-accepting them, and verifies its own result too.
        return True
    # Every httpx failure: a refused connection, a timeout, and a status the
    # proxy answered with. A GitHub proxy that 404s or 502s is the case this
    # fallback exists for, and `raise_for_status` reports it as an
    # HTTPStatusError, which is not a transport error at all.
    if isinstance(error, httpx.HTTPError):
        return True
    text = f"{error} {getattr(error, 'output', '') or ''}".lower()
    if any(marker in text for marker in LOCAL_FAILURE_MARKERS):
        return False
    if MISMATCH_MARKER in text:
        # A verification that ran in a subprocess never hands back the
        # VerificationMismatch above -- only its words cross.
        return True
    return any(marker in text for marker in NETWORK_FAILURE_MARKERS)


def verified_stamp_path(path: Path) -> Path:
    return path.with_name(f"{path.name}{VERIFIED_SUFFIX}")


def _stamp_still_describes(path: Path, expected) -> bool:
    """Whether an earlier full verification still speaks for this file."""

    try:
        status = path.stat()
        record = json.loads(
            verified_stamp_path(path).read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        return False
    if not isinstance(record, dict):
        return False
    return (
        record.get("sha256") == expected.sha256
        and record.get("size") == expected.size == status.st_size
        and record.get("mtime_ns") == status.st_mtime_ns
    )


def _write_verified_stamp(path: Path, expected) -> None:
    """Record that `path` verified in full, so the next run need not redo it.

    Best effort: a model directory that cannot be written to (a read-only
    share, a shared cache someone else owns) costs the full check again, which
    is exactly today's behaviour.
    """

    try:
        status = path.stat()
        verified_stamp_path(path).write_text(
            json.dumps(
                {
                    "sha256": expected.sha256,
                    "size": status.st_size,
                    "mtime_ns": status.st_mtime_ns,
                }
            ),
            encoding="utf-8",
        )
    except OSError:
        pass


def fetch_fixed_files(
    entry,
    directory: Path,
    *,
    data_root: Path,
    region: str,
    progress=None,
) -> None:
    """Check that a model that is a fixed list of URLs is in place, verified against the manifest.

    Used for the separator checkpoint, its config and its index. A file that is
    already here and already matches is left alone, and a verifiable one is
    stamped as before. A file that is missing or does not match raises
    `ModelUnavailable` instead of being fetched (Nonoka X WP-R7): nothing is
    downloaded, patched over or moved aside here any more.

    `data_root`, `region` and `progress` are what the caller has always passed;
    they served the download this no longer starts.
    """

    from finesub_bootstrap.model_ensure import ModelUnavailable
    from finesub_bootstrap.model_manifest import file_matches

    unavailable: list[str] = []
    for wanted in entry.files:
        destination = directory / wanted.name
        if destination.is_file():
            if wanted.is_verifiable and _stamp_still_describes(destination, wanted):
                continue
            if file_matches(destination, wanted):
                # Stamped here too: an install that predates the stamp -- or
                # one whose files came from a shared cache -- gets it on its
                # first full check rather than paying for one forever.
                if wanted.is_verifiable:
                    _write_verified_stamp(destination, wanted)
                continue
        unavailable.append(wanted.name)
    if unavailable:
        raise ModelUnavailable(
            f"模型 {entry.model_id} 的文件不在 {directory} 或与清单不符：{', '.join(unavailable)}。"
            "引擎不会自己下载模型，请在设置里安装或修复模型层。"
        )
