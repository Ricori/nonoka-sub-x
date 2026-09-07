#!/usr/bin/env python3
"""Synchronize a pinned FineSub source archive into third_party/finesub."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


REPOSITORY = "https://github.com/caca2331/finesub"
DEFAULT_REF = "v0.5.1"
SYNC_SCHEMA = 1
REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_VENDOR = REPO_ROOT / "third_party" / "finesub"
DEFAULT_PATCHES = REPO_ROOT / "patches" / "finesub"
#: The un-patched manifest `tests/test_finesub_patch_stack.py` replays
#: against. It lives beside the patches rather than inside the vendor
#: because it describes what the vendor would be *without* them, and a
#: file in the vendor would have to describe itself.
DEFAULT_BASELINE = DEFAULT_PATCHES / "BASELINE_FILES.json"

SOURCE_TREES = (
    "src/finesub",
    "src/finesub_bootstrap",
)
#: The version, read from the upstream repository root rather than from
#: `pyproject.toml`. FineSub 0.5.0 made `project.version` dynamic and moved the
#: single number to this file, so the metadata block no longer carries it.
VERSION_FILE = "VERSION"
SOURCE_FILES = (
    "README.md",
    "LICENSE",
    "pyproject.toml",
    VERSION_FILE,
)
#: Where the installer's own three assets live inside the snapshot. Since 0.5.0
#: they are package data resolved with `Path(__file__).with_name(...)` by
#: `finesub_bootstrap.resources` and `.environment`, so these are the copies
#: that are actually read -- a patched duplicate anywhere else would be
#: ignored in silence.
RUNTIME_MANIFEST = "src/finesub_bootstrap/runtime-manifest.json"
RUNTIME_LOCKS = (
    "src/finesub_bootstrap/pylock.win-py312.toml",
    "src/finesub_bootstrap/pylock.win-py312.cn.toml",
)
VENDOR_README = """# FineSub vendor directory

此目录保存由 `scripts/sync_finesub.py` 生成的 FineSub 引擎快照。

规则：

- 开发调试时可以直接修改 `src/finesub`；提交前必须生成对应 patch 并更新快照哈希。
- 已提交的 vendor 必须能由固定基线和 `patches/finesub` 完整重建。
- 不要把机器上安装的 `site-packages/finesub` 复制到这里。
- 只接受来自 FineSub 官方仓库固定 tag/commit 的 source archive。
- 每次同步必须生成并校验 `UPSTREAM.json` 与 `FILES.json`。
- 必要补丁放在仓库外部的 `patches/finesub`，由同步器应用。
- 本地 runtime 和云端镜像必须使用同一 `engine_bundle_id`。

上游原始说明保存在 `UPSTREAM_README.md`。具体流程见
[FineSub 引擎同步与构建规范](../../docs/engine.md)。
"""


class SyncError(RuntimeError):
    """Raised when an upstream archive or generated snapshot is invalid."""


@dataclass(frozen=True)
class Upstream:
    repository: str
    ref: str
    commit: str
    committed_at: str


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_json(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()


def write_json(path: Path, value: object) -> None:
    path.write_bytes(canonical_json(value))


def github_slug(repository: str) -> str:
    parsed = urllib.parse.urlparse(repository)
    if parsed.scheme != "https" or parsed.netloc.lower() != "github.com":
        raise SyncError("only an official https://github.com/<owner>/<repo> repository is supported")
    parts = [part for part in parsed.path.removesuffix(".git").split("/") if part]
    if len(parts) != 2:
        raise SyncError(f"cannot parse GitHub repository: {repository}")
    return "/".join(parts)


def request_json(url: str) -> dict[str, object]:
    request = urllib.request.Request(
        url,
        headers={"Accept": "application/vnd.github+json", "User-Agent": "nonoka-sync-finesub/1"},
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.load(response)
    except Exception as exc:  # urllib exposes several unrelated transport errors
        raise SyncError(f"failed to request {url}: {exc}") from exc


def resolve_upstream(repository: str, ref: str) -> Upstream:
    slug = github_slug(repository)
    encoded_ref = urllib.parse.quote(ref, safe="")
    data = request_json(f"https://api.github.com/repos/{slug}/commits/{encoded_ref}")
    commit = str(data.get("sha", ""))
    commit_block = data.get("commit")
    committer = commit_block.get("committer") if isinstance(commit_block, dict) else None
    committed_at = committer.get("date") if isinstance(committer, dict) else None
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise SyncError(f"GitHub returned an invalid commit for {ref!r}")
    if not isinstance(committed_at, str):
        raise SyncError(f"GitHub returned no committer timestamp for {commit}")
    return Upstream(repository=repository, ref=ref, commit=commit, committed_at=committed_at)


def download_archive(upstream: Upstream, destination: Path) -> None:
    slug = github_slug(upstream.repository)
    url = f"https://codeload.github.com/{slug}/tar.gz/{upstream.commit}"
    request = urllib.request.Request(url, headers={"User-Agent": "nonoka-sync-finesub/1"})
    try:
        with urllib.request.urlopen(request, timeout=180) as response, destination.open("wb") as output:
            shutil.copyfileobj(response, output)
    except Exception as exc:
        raise SyncError(f"failed to download {url}: {exc}") from exc


def extract_archive(archive: Path, destination: Path, expected_commit: str) -> Path:
    try:
        with tarfile.open(archive, "r:gz") as source:
            members = source.getmembers()
            roots = {Path(member.name).parts[0] for member in members if Path(member.name).parts}
            if len(roots) != 1:
                raise SyncError("upstream archive must have exactly one root directory")
            root_name = next(iter(roots))
            if expected_commit not in root_name:
                raise SyncError(
                    f"archive root {root_name!r} does not identify expected commit {expected_commit}"
                )
            source.extractall(destination, filter="data")
    except (tarfile.TarError, OSError) as exc:
        raise SyncError(f"cannot extract {archive}: {exc}") from exc
    return destination / root_name


def validate_source(source: Path) -> str:
    required = (
        *SOURCE_TREES,
        *SOURCE_FILES,
        VERSION_FILE,
        RUNTIME_MANIFEST,
        *RUNTIME_LOCKS,
    )
    missing = [path for path in required if not (source / path).exists()]
    if missing:
        raise SyncError("upstream archive is missing required paths: " + ", ".join(missing))
    try:
        version = (source / VERSION_FILE).read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise SyncError(f"cannot read FineSub version: {exc}") from exc
    if not isinstance(version, str) or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
        raise SyncError(f"invalid FineSub version: {version!r}")
    prompt_license = source / "src/finesub/llm/prompt_templates/LICENSE.md"
    if not prompt_license.is_file():
        raise SyncError("upstream prompt template license is missing")
    return version


def copy_source(source: Path, staging: Path) -> None:
    for relative in SOURCE_TREES:
        target = staging / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source / relative, target)
    for relative in SOURCE_FILES:
        target_relative = "UPSTREAM_README.md" if relative == "README.md" else relative
        target = staging / target_relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source / relative, target)
    shutil.copy2(
        source / "src/finesub/llm/prompt_templates/LICENSE.md",
        staging / "PROMPT_LICENSE.md",
    )


def git_apply_command() -> list[str]:
    """`git apply` that leaves bytes alone.

    The snapshot's hashes are taken over the bytes on disk, so no line ending
    may be translated. Both settings are needed on Windows: `core.autocrlf`
    alone leaves `.gitattributes`' `text=auto` in force, and a `text` file is
    written with the platform's native EOL. The staging directory sits *beside*
    the vendor, so `third_party/finesub/** -text` does not cover it.
    """

    return ["git", "-c", "core.autocrlf=false", "-c", "core.eol=lf", "apply"]


def git_apply_env(staging: Path) -> dict[str, str]:
    """Environment that keeps `git apply` from noticing the surrounding repo.

    Run inside a work tree, `git apply` prefixes every path in the patch with
    the current directory's path relative to the repository root, then finds
    nothing there and prints `Skipped patch` -- with exit status 0, so the
    caller sees a clean apply that changed nothing. The staging directory lives
    under `third_party/`, i.e. inside this repository, so that is exactly what
    happened. Capping the upward search at the staging's parent leaves git with
    no work tree at all, which is the situation the patches were written for.
    """

    environment = dict(os.environ)
    environment["GIT_CEILING_DIRECTORIES"] = str(staging.resolve().parent)
    return environment


def apply_patches(staging: Path, patches_dir: Path) -> list[dict[str, str]]:
    patches = sorted(patches_dir.glob("*.patch")) if patches_dir.exists() else []
    applied: list[dict[str, str]] = []
    git = git_apply_command()
    environment = git_apply_env(staging)
    for patch in patches:
        check = subprocess.run(
            [*git, "--check", str(patch)],
            cwd=staging,
            env=environment,
            capture_output=True,
            text=True,
        )
        if check.returncode:
            raise SyncError(f"patch check failed for {patch.name}: {check.stderr.strip()}")
        result = subprocess.run(
            # `--verbose` for the sake of the check below: a skip is reported
            # only in verbose mode, and it does not set a failing status.
            [*git, "--verbose", str(patch)],
            cwd=staging,
            env=environment,
            capture_output=True,
            text=True,
        )
        if result.returncode:
            raise SyncError(f"patch apply failed for {patch.name}: {result.stderr.strip()}")
        if "Skipped patch" in result.stderr:
            raise SyncError(
                f"patch apply silently skipped hunks for {patch.name}: "
                f"{result.stderr.strip()}"
            )
        applied.append({"path": patch.name, "sha256": sha256_file(patch)})
    return applied


def iter_snapshot_files(root: Path) -> Iterable[Path]:
    # Ordered by the POSIX relative path, not by `Path`: the manifest's
    # `content_sha256` folds the order in, and `Path` sorts by the platform's
    # own spelling -- backslash-separated and case-folded on Windows. Sorting
    # the objects made the same tree hash differently on Windows and Linux, so
    # a snapshot verified on one refused to verify on the other.
    selected = [
        path
        for path in root.rglob("*")
        if path.is_file()
        and path.name not in {"FILES.json", "UPSTREAM.json"}
        and "__pycache__" not in path.parts
        and path.suffix not in {".pyc", ".pyo"}
    ]
    selected.sort(key=lambda path: path.relative_to(root).as_posix())
    return selected


def create_files_manifest(staging: Path) -> dict[str, object]:
    files = []
    tree_digest = hashlib.sha256()
    for path in iter_snapshot_files(staging):
        relative = path.relative_to(staging).as_posix()
        digest = sha256_file(path)
        size = path.stat().st_size
        files.append({"path": relative, "sha256": digest, "bytes": size})
        tree_digest.update(relative.encode())
        tree_digest.update(b"\0")
        tree_digest.update(digest.encode())
        tree_digest.update(b"\n")
    return {"schema": 1, "content_sha256": tree_digest.hexdigest(), "files": files}


def write_metadata(
    staging: Path,
    upstream: Upstream,
    archive_sha256: str,
    engine_version: str,
    patches: list[dict[str, str]],
) -> None:
    licenses = {
        "schema": 1,
        "components": [
            {
                "path": "LICENSE",
                # GPL-3.0-or-later since upstream 0.5.0; MIT up to 0.4.2.
                "license": "GPL-3.0-or-later",
                "component": "FineSub Python engine",
            },
            {
                "path": "src/finesub/llm/prompt_templates",
                "notice": "PROMPT_LICENSE.md",
                "license": "CC-BY-SA-4.0",
                "component": "FineSub prompt templates",
            },
        ],
    }
    write_json(staging / "LICENSES.json", licenses)
    files_manifest = create_files_manifest(staging)
    write_json(staging / "FILES.json", files_manifest)
    runtime_manifest = staging / RUNTIME_MANIFEST
    upstream_data = {
        "archive_sha256": archive_sha256,
        "commit": upstream.commit,
        "content_sha256": files_manifest["content_sha256"],
        "engine_bundle_id": f"finesub-{engine_version}+{upstream.commit[:12]}",
        "engine_version": engine_version,
        "files_manifest_sha256": sha256_file(staging / "FILES.json"),
        "patches": patches,
        "ref": upstream.ref,
        "repository": upstream.repository,
        "runtime_manifest_sha256": sha256_file(runtime_manifest),
        "sync_schema": SYNC_SCHEMA,
        "synced_at": upstream.committed_at,
    }
    write_json(staging / "UPSTREAM.json", upstream_data)


def atomic_replace(staging: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    backup = destination.with_name(f".{destination.name}.backup-{uuid.uuid4().hex}")
    had_destination = destination.exists()
    try:
        if had_destination:
            destination.rename(backup)
        staging.rename(destination)
    except Exception:
        if had_destination and backup.exists() and not destination.exists():
            backup.rename(destination)
        raise
    else:
        if backup.exists():
            shutil.rmtree(backup)


def sync_archive(
    archive: Path,
    upstream: Upstream,
    destination: Path = DEFAULT_VENDOR,
    patches_dir: Path = DEFAULT_PATCHES,
) -> dict[str, object]:
    archive = archive.resolve()
    if not archive.is_file():
        raise SyncError(f"archive not found: {archive}")
    with tempfile.TemporaryDirectory(prefix="nonoka-finesub-extract-") as extract_temp:
        source = extract_archive(archive, Path(extract_temp), upstream.commit)
        engine_version = validate_source(source)
        staging = destination.parent / f".{destination.name}.next-{uuid.uuid4().hex}"
        try:
            staging.mkdir(parents=True)
            existing_readme = destination / "README.md"
            if existing_readme.is_file():
                shutil.copy2(existing_readme, staging / "README.md")
            else:
                (staging / "README.md").write_text(VENDOR_README, encoding="utf-8")
            copy_source(source, staging)
            patches = apply_patches(staging, patches_dir)
            write_metadata(staging, upstream, sha256_file(archive), engine_version, patches)
            verify_snapshot(staging)
            atomic_replace(staging, destination)
        finally:
            if staging.exists():
                shutil.rmtree(staging)
    return json.loads((destination / "UPSTREAM.json").read_text(encoding="utf-8"))


def verify_snapshot(vendor: Path = DEFAULT_VENDOR) -> dict[str, object]:
    upstream_path = vendor / "UPSTREAM.json"
    manifest_path = vendor / "FILES.json"
    if not upstream_path.is_file() or not manifest_path.is_file():
        raise SyncError(f"{vendor} is not a synchronized FineSub snapshot")
    upstream = json.loads(upstream_path.read_text(encoding="utf-8"))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if upstream.get("sync_schema") != SYNC_SCHEMA or manifest.get("schema") != 1:
        raise SyncError("unsupported FineSub sync metadata schema")
    if upstream.get("files_manifest_sha256") != sha256_file(manifest_path):
        raise SyncError("FILES.json hash does not match UPSTREAM.json")
    expected_paths: set[str] = set()
    tree_digest = hashlib.sha256()
    for entry in manifest.get("files", []):
        relative = entry.get("path")
        if not isinstance(relative, str) or relative.startswith("/") or ".." in Path(relative).parts:
            raise SyncError(f"unsafe path in FILES.json: {relative!r}")
        path = vendor / relative
        if not path.is_file():
            raise SyncError(f"snapshot file is missing: {relative}")
        digest = sha256_file(path)
        if digest != entry.get("sha256") or path.stat().st_size != entry.get("bytes"):
            raise SyncError(f"snapshot file was modified: {relative}")
        expected_paths.add(relative)
        tree_digest.update(relative.encode())
        tree_digest.update(b"\0")
        tree_digest.update(digest.encode())
        tree_digest.update(b"\n")
    actual_paths = {path.relative_to(vendor).as_posix() for path in iter_snapshot_files(vendor)}
    if actual_paths != expected_paths:
        extras = sorted(actual_paths - expected_paths)
        missing = sorted(expected_paths - actual_paths)
        raise SyncError(f"snapshot file set differs (extra={extras}, missing={missing})")
    if tree_digest.hexdigest() != upstream.get("content_sha256"):
        raise SyncError("snapshot content hash does not match UPSTREAM.json")
    runtime_manifest = vendor / RUNTIME_MANIFEST
    if sha256_file(runtime_manifest) != upstream.get("runtime_manifest_sha256"):
        raise SyncError("runtime manifest hash does not match UPSTREAM.json")
    return upstream


def rebuild_baseline(
    vendor: Path = DEFAULT_VENDOR,
    patches_dir: Path = DEFAULT_PATCHES,
    baseline_path: Path = DEFAULT_BASELINE,
) -> dict[str, object]:
    """Regenerate the pinned pre-patch manifest from the current snapshot.

    `sync` deliberately does not write this: the baseline is what the vendor
    would be *without* our patches, so deriving it in the same pass that
    applies them would make the round-trip test compare a thing against
    itself. This is the separate step, and it exists as a command because the
    alternative -- reverse-applying by hand -- gets the line endings wrong on
    Windows in a way that shows up as an unexplained hash mismatch rather than
    as a bad reverse-apply.

    Reverse order, because the stack is ordered: two patches touch
    `separator_aoti.py` and the later one was written against the earlier
    one's output.
    """

    upstream = verify_snapshot(vendor)
    patches = [patches_dir / str(item["path"]) for item in upstream["patches"]]
    missing = [patch.name for patch in patches if not patch.is_file()]
    if missing:
        raise SyncError("UPSTREAM.json names patches that are gone: " + ", ".join(missing))
    git = git_apply_command()
    with tempfile.TemporaryDirectory(prefix="nonoka-finesub-baseline-") as temp:
        replay = Path(temp) / "vendor"
        shutil.copytree(vendor, replay)
        environment = git_apply_env(replay)
        for patch in reversed(patches):
            result = subprocess.run(
                [*git, "--verbose", "--reverse", str(patch)],
                cwd=replay,
                env=environment,
                capture_output=True,
                text=True,
            )
            if result.returncode:
                raise SyncError(
                    f"cannot reverse-apply {patch.name}: {result.stderr.strip()}"
                )
            if "Skipped patch" in result.stderr:
                raise SyncError(
                    f"reverse apply silently skipped hunks for {patch.name}: "
                    f"{result.stderr.strip()}"
                )
        baseline = create_files_manifest(replay)
    write_json(baseline_path, baseline)
    return baseline


def parse_upstream(args: argparse.Namespace) -> Upstream:
    if args.archive:
        if not args.commit or not re.fullmatch(r"[0-9a-f]{40}", args.commit):
            raise SyncError("--archive requires a full 40-character lowercase --commit")
        committed_at = args.committed_at or "1970-01-01T00:00:00Z"
        return Upstream(args.repository, args.ref, args.commit, committed_at)
    return resolve_upstream(args.repository, args.ref)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    sync = subparsers.add_parser("sync", help="download and synchronize a fixed upstream ref")
    sync.add_argument("--repository", default=REPOSITORY)
    sync.add_argument("--ref", default=DEFAULT_REF)
    sync.add_argument("--archive", type=Path, help="use a local source tar.gz instead of downloading")
    sync.add_argument("--commit", help="commit represented by --archive")
    sync.add_argument("--committed-at", help="deterministic upstream commit timestamp for --archive")
    sync.add_argument("--vendor-dir", type=Path, default=DEFAULT_VENDOR)
    sync.add_argument("--patches-dir", type=Path, default=DEFAULT_PATCHES)
    check = subparsers.add_parser("check", help="verify the checked-in snapshot without network")
    check.add_argument("--vendor-dir", type=Path, default=DEFAULT_VENDOR)
    baseline = subparsers.add_parser(
        "baseline", help="regenerate BASELINE_FILES.json from the checked-in snapshot"
    )
    baseline.add_argument("--vendor-dir", type=Path, default=DEFAULT_VENDOR)
    baseline.add_argument("--patches-dir", type=Path, default=DEFAULT_PATCHES)
    baseline.add_argument("--baseline-file", type=Path, default=DEFAULT_BASELINE)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "check":
            result = verify_snapshot(args.vendor_dir)
        elif args.command == "baseline":
            result = rebuild_baseline(
                args.vendor_dir, args.patches_dir, args.baseline_file
            )
        else:
            upstream = parse_upstream(args)
            if args.archive:
                archive = args.archive
                result = sync_archive(archive, upstream, args.vendor_dir, args.patches_dir)
            else:
                with tempfile.TemporaryDirectory(prefix="nonoka-finesub-download-") as temp:
                    archive = Path(temp) / f"finesub-{upstream.commit}.tar.gz"
                    download_archive(upstream, archive)
                    result = sync_archive(archive, upstream, args.vendor_dir, args.patches_dir)
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    except (SyncError, OSError, json.JSONDecodeError) as exc:
        print(f"sync-finesub: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
