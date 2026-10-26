"""Export the old app's keys and library for the Cloudflare backend. Run once.

The only code in this project that touches the old `nonoka-x-cloud` app's
resources, and it only reads them: the `nonoka-x-keys` and `nonoka-x-videos`
Dicts, the `nonoka-x-artifacts` Volume (mounted read-only), and the old bucket
named by the `r2` Secret's `R2_BUCKET`, which it reads covers from. Everything
it writes goes to the new stack's own bucket, `nonoka-x-cloud` unless
`--target-bucket` (or `TARGET_R2_BUCKET`) says otherwise. From `modal_compute/`::

    modal run -m nonoka_x_compute.migrate.export_legacy --out legacy-dump.json
    modal run -m nonoka_x_compute.migrate.export_legacy --dry-run   # writes nothing to R2

The `r2` Secret's token must be able to read the old bucket and write the new
one; both are in the same Cloudflare account, so one token scoped to the two
buckets is enough.

Four things happen:

1. Every key and library record is gathered from both of its copies: the Dict,
   and the durable envelope `records/{keys,videos}/<sha256>.json` that
   `record_store` filed beside it. The Dict wins where both have one -- every
   write went to both, and the Dict is the one that was read -- and the
   envelope supplies what a Dict entry lost to the seven-day expiry.
2. Each library entry's files `library/<owner>/<vid>/<name><suffix>` on the
   Volume are copied to the new bucket under the same key. The owner is the
   record's `ownerId`, else the key whose `video_ids` lists it -- the same rule
   `nonoka-cloud-cf/scripts/import-legacy.ts` applies when it writes the row.
3. Each entry's cover is looked up in the old bucket -- `nonoka-thumbs/<objectId>.jpg`,
   then `nonoka-thumbs/<vid>.jpg`, then `uploads/<objectId>.jpg` -- and the first
   one found (at most 5 MiB) is copied to `nonoka-thumbs/<vid>.jpg` in the new
   bucket, with `thumbnailAvailable: true` set on the exported record. The
   import drops `objectId`, so the cover has to live under the entry's own id.
   No audio is copied: in-flight jobs are not migrated.
4. The records are written locally as `{keys: [...], videos: [...]}`, the input
   of that script.

Safe to repeat: copies overwrite, and the import is `INSERT OR REPLACE`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable

import modal

from ..pipeline_rules import LIBRARY_ARTIFACT_SUFFIXES


LEGACY_ROOT = Path("/legacy")
ADMIN_OWNER_ID = "admin"
#: The new stack's bucket, which the Worker binds as `BUCKET`.
DEFAULT_TARGET_BUCKET = "nonoka-x-cloud"
TARGET_BUCKET_ENV = "TARGET_R2_BUCKET"
#: The Worker refuses larger covers on sync; an older one this size is not one.
COVER_MAX_BYTES = 5 * 1024 * 1024

app = modal.App("nonoka-x-legacy-export")
legacy_volume = modal.Volume.from_name("nonoka-x-artifacts").read_only()
legacy_keys = modal.Dict.from_name("nonoka-x-keys")
legacy_videos = modal.Dict.from_name("nonoka-x-videos")
r2_secret = modal.Secret.from_name("r2")
export_image = modal.Image.debian_slim(python_version="3.12").pip_install("boto3")


def read_envelopes(directory: Path) -> dict[str, dict]:
    """The records `record_store` filed under `directory`, by id."""

    found: dict[str, dict] = {}
    if not directory.is_dir():
        return found
    for path in sorted(directory.glob("*.json")):
        try:
            envelope = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(envelope, dict):
            continue
        identifier = envelope.get("id")
        record = envelope.get("record")
        if isinstance(identifier, str) and identifier and isinstance(record, dict) and record:
            found[identifier] = record
    return found


def merge_records(cached: Iterable[tuple[object, object]], filed: dict[str, dict]) -> list[dict]:
    """Dict entries over filed envelopes, one record per id, sorted by id."""

    merged = dict(filed)
    for identifier, record in cached:
        if isinstance(record, dict) and record:
            merged[str(identifier)] = record
    out = []
    for identifier in sorted(merged):
        record = dict(merged[identifier])
        record.setdefault("id", identifier)
        out.append(record)
    return out


def video_owners(keys: list[dict]) -> dict[str, str]:
    """Which key lists each library entry in its `video_ids`."""

    owners: dict[str, str] = {}
    for key in keys:
        identifier = str(key.get("id") or "")
        for video_id in key.get("video_ids") or []:
            owners[str(video_id)] = identifier
    return owners


def resolve_owner(video: dict, owners: dict[str, str]) -> str:
    return str(video.get("ownerId") or owners.get(str(video.get("id") or "")) or ADMIN_OWNER_ID)


def library_files(root: Path, owner_id: str, video_id: str) -> list[tuple[Path, str]]:
    """The artifact files of one entry, with the R2 key each one goes to.

    Read from the owner's directory, else from wherever the entry's files sit:
    an entry written before `ownerId` existed may be filed under the key that
    synced it, which is the owner `resolve_owner` found anyway.
    """

    directory = root / "library" / owner_id / video_id
    if not directory.is_dir():
        candidates = sorted(path for path in (root / "library").glob(f"*/{video_id}") if path.is_dir())
        if not candidates:
            return []
        directory = candidates[0]
    files = []
    for name, suffix in LIBRARY_ARTIFACT_SUFFIXES.items():
        path = directory / f"{name}{suffix}"
        if path.is_file():
            files.append((path, f"library/{owner_id}/{video_id}/{name}{suffix}"))
    return files


def cover_candidates(video: dict) -> list[str]:
    """Where the old bucket may hold this entry's cover, in the order to try."""

    video_id = str(video.get("id") or "")
    object_id = str(video.get("objectId") or "")
    keys = []
    if object_id:
        keys.append(f"nonoka-thumbs/{object_id}.jpg")
    if video_id:
        keys.append(f"nonoka-thumbs/{video_id}.jpg")
    if object_id:
        keys.append(f"uploads/{object_id}.jpg")
    return list(dict.fromkeys(keys))


def cover_target(video_id: str) -> str:
    return f"nonoka-thumbs/{video_id}.jpg"


def find_cover(s3, bucket: str, candidates: list[str], *, max_bytes: int = COVER_MAX_BYTES) -> str | None:
    """The first candidate that exists in `bucket` and is small enough."""

    for key in candidates:
        try:
            head = s3.head_object(Bucket=bucket, Key=key)
        except Exception as error:
            # botocore's ClientError, read by shape so this stays importable without it.
            code = str((getattr(error, "response", None) or {}).get("Error", {}).get("Code", ""))
            if code in {"404", "NoSuchKey", "NotFound"}:
                continue
            raise
        if 0 < int(head.get("ContentLength") or 0) <= max_bytes:
            return key
    return None


@app.function(
    image=export_image,
    volumes={str(LEGACY_ROOT): legacy_volume},
    secrets=[r2_secret],
    timeout=60 * 60,
)
def export(upload: bool = True, target_bucket: str = "") -> dict:
    import os

    import boto3
    from botocore.config import Config

    keys = merge_records(legacy_keys.items(), read_envelopes(LEGACY_ROOT / "records" / "keys"))
    videos = merge_records(legacy_videos.items(), read_envelopes(LEGACY_ROOT / "records" / "videos"))
    owners = video_owners(keys)

    s3 = boto3.client(
        "s3",
        endpoint_url=f"https://{os.environ['R2_ACCOUNT_ID']}.r2.cloudflarestorage.com",
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        config=Config(signature_version="s3v4"),
    )
    source_bucket = os.environ["R2_BUCKET"]
    target = target_bucket or os.environ.get(TARGET_BUCKET_ENV) or DEFAULT_TARGET_BUCKET
    if target == source_bucket:
        raise RuntimeError(f"target bucket {target!r} is the old bucket; the new stack has its own")

    files = 0
    copied = 0
    covers = 0
    missing: list[str] = []
    for video in videos:
        video_id = str(video.get("id") or "")
        entry_files = library_files(LEGACY_ROOT, resolve_owner(video, owners), video_id)
        if not entry_files and video.get("artifactNames"):
            missing.append(video_id)
        files += len(entry_files)
        if upload:
            for path, key in entry_files:
                s3.upload_file(
                    str(path),
                    target,
                    key,
                    ExtraArgs={"ContentType": "text/plain; charset=utf-8"},
                )
                copied += 1
        cover = find_cover(s3, source_bucket, cover_candidates(video))
        if cover is None:
            continue
        covers += 1
        if upload:
            body = s3.get_object(Bucket=source_bucket, Key=cover)["Body"].read()
            s3.put_object(Bucket=target, Key=cover_target(video_id), Body=body, ContentType="image/jpeg")
            # Only once the cover is really there: a dry run's dump must not
            # promise covers the new bucket does not have.
            video["thumbnailAvailable"] = True

    summary = {
        "source_bucket": source_bucket,
        "target_bucket": target,
        "keys": len(keys),
        "videos": len(videos),
        "files": files,
        "files_uploaded": copied,
        "covers": covers,
        "covers_uploaded": covers if upload else 0,
        "entries_without_files": missing,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    return {"keys": keys, "videos": videos, "summary": summary}


@app.local_entrypoint()
def main(out: str = "legacy-dump.json", dry_run: bool = False, target_bucket: str = "") -> None:
    result = export.remote(upload=not dry_run, target_bucket=target_bucket)
    Path(out).write_text(
        json.dumps({"keys": result["keys"], "videos": result["videos"]}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps({"out": out, **result["summary"]}, ensure_ascii=False, indent=2))
