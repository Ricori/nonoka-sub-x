#!/usr/bin/env python3
"""Publish the prebuilt layers on Cloudflare R2 (docs/transcription-engines-plan.md 4.2 items 18-19, WP-E2).

    python scripts/publish_layers.py index        # dist/layers -> src/nonoka_x/resources/layers.json
    python scripts/publish_layers.py plan [--remote]
    python scripts/publish_layers.py upload [--direct | --proxy URL]
    python scripts/publish_layers.py verify (--direct | --proxy URL) [--keep]
    python scripts/publish_layers.py unreferenced
    python scripts/publish_layers.py ratelimit (--direct | --proxy URL)

`index` reads what WP-E1 left in `<main checkout>/dist/layers/<layer>/<version>/` (read only: nothing
there is changed) and writes `layers.json`, the layers this desktop version wants, in the format fixed in
the plan's WP-E2 section. Before it writes anything it checks each layer: the version is the manifest's
digest, `summary.json` agrees with the files, the archive's sha256 is what the summary says, and the
manifest the archive carries (`.nonoka-layer.json`) has the same bytes as `manifest.json`.

The objects (all named by content, never changed once uploaded; 4.2 item 18):

    v1/<layer>/<manifest sha256>.json       the layer's manifest
    v1/<layer>/<archive sha256>.tar.zst     the layer's archive
    v1/files/<file sha256>                  every file of a layer `files.layers` lists, on its own

`files.layers` is empty: the model files are not hosted one by one (owner 2026-11-13; plan 4.2 item 18
had the model layer's files there for the migration's per-file fill, 4.4 item 25). The machinery stays,
driven by `layers.json` alone.

`upload` sends them to the bucket `nonoka-x-layers` over R2's S3 API (SigV4 signed here with httpx; no
other dependency). It is idempotent and never overwrites: an object that is there already with the same
size and sha256 (`x-amz-meta-sha256`) is skipped, one that is there with anything else stops the run, and
every write is conditional (`If-None-Match: *`), so a race cannot overwrite either. Objects above
`SINGLE_PUT_LIMIT` go up in parts of `PART_SIZE`; every request carries its payload's sha256, which R2
checks, and the whole object's digest is checked before the upload is completed.

`verify` downloads everything `layers.json` names from its public `baseUrl`, through the given route (a
proxy or direct, one at a time; plan WP-E2), and checks it against the manifests: sizes and digests,
the version, the manifest inside each archive, every file of each unpacked archive, every single file.

`unreferenced` lists the objects in the bucket that no published desktop version refers to, with their
sizes. A published version is a `v*` tag; what it refers to is the `layers.json` at that tag, and the one
in the working tree counts too (owner's choice 2026-11-13). Nothing is ever deleted by this script: removing
anything from R2 is a separate step the owner confirms (plan appendix B, question 60).

`ratelimit` checks that the rate-limiting rule in front of the bucket blocks a burst of requests.

Credentials (owner's choice 2026-11-13): only from a `.env` file beside this script, or in the main
checkout's `scripts/.env` when this runs in a worktree, and only if git ignores that file:

    R2_ACCOUNT_ID=<32 hex digits>
    R2_ACCESS_KEY_ID=<access key id of an R2 API token: Object Read & Write on nonoka-x-layers only>
    R2_SECRET_ACCESS_KEY=<its secret>

Their values are never printed, logged or put in an error message.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import hashlib
import hmac
import json
import re
import shutil
import subprocess
import sys
import time
import xml.etree.ElementTree as ElementTree
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator
from urllib.parse import parse_qsl, quote, urlsplit

import httpx

ROOT = Path(__file__).resolve().parents[1]
for _path in (str(ROOT / "src"), str(ROOT)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from nonoka_x.runtime import fetch  # noqa: E402
from nonoka_x.runtime import layer_manifest as lm  # noqa: E402

from scripts.build_layers import SYSTEM_TAR, main_checkout  # noqa: E402

INDEX_FILE = ROOT / "src" / "nonoka_x" / "resources" / "layers.json"
INDEX_REPO_PATH = "src/nonoka_x/resources/layers.json"
INDEX_SCHEMA = 1

BUCKET = "nonoka-x-layers"
BASE_URL = "https://layers.nonoka.online/"
PREFIX = "v1"
#: The layers whose files are also stored one by one. None: the owner decided on 2026-11-13 that the
#: model files are not hosted on their own (only the model layer's archive and manifest are).
FILE_LAYERS: tuple[str, ...] = ()
FILES_PATH = f"{PREFIX}/files/{{sha256}}"

#: Every object is immutable, so caches may keep it for good.
CACHE_CONTROL = "public, max-age=31536000, immutable"
SHA256_META = "x-amz-meta-sha256"

#: Larger objects go up in parts. R2 wants every part but the last of one size, at least 5 MiB.
SINGLE_PUT_LIMIT = 64 * 1024 * 1024
PART_SIZE = 64 * 1024 * 1024
ATTEMPTS = 4

ENV_NAME = ".env"
ENV_KEYS = ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY")

EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
_HEX64 = re.compile(r"[0-9a-f]{64}")


class PublishError(RuntimeError):
    pass


class Conflict(PublishError):
    """An object is in the bucket under a content name with other content: never overwritten."""


def log(message: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


def gib(size: int) -> str:
    return f"{size / 1024 ** 3:.2f} GiB"


def human(size: int) -> str:
    for unit, scale in (("GiB", 1024 ** 3), ("MiB", 1024 ** 2), ("KiB", 1024)):
        if size >= scale:
            return f"{size / scale:.2f} {unit}"
    return f"{size} B"


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def tar_program() -> str:
    if SYSTEM_TAR.is_file():
        return str(SYSTEM_TAR)
    found = shutil.which("tar")
    if not found:
        raise PublishError("no tar found (Windows' tar.exe reads zstd)")
    return found


# --- what WP-E1 built -----------------------------------------------------------------------------


@dataclass(frozen=True)
class Built:
    """One layer as `scripts/build_layers.py` left it in `dist/layers/<layer>/<version>/`."""

    layer: str
    version: str
    directory: Path
    manifest_data: bytes
    archive_size: int
    archive_sha256: str

    @property
    def manifest_sha256(self) -> str:
        return lm.digest_of(self.manifest_data)

    @property
    def manifest_path(self) -> Path:
        return self.directory / "manifest.json"

    @property
    def archive_path(self) -> Path:
        return self.directory / "layer.tar.zst"


def embedded_manifest(archive: Path) -> bytes:
    """The `.nonoka-layer.json` an archive carries at its root (the first entry; `-q` stops there)."""

    completed = subprocess.run([tar_program(), "-x", "-q", "-O", "-f", str(archive), lm.MANIFEST_NAME],
                               capture_output=True)
    if completed.returncode != 0:
        raise PublishError(f"{archive} carries no {lm.MANIFEST_NAME}: "
                           f"{completed.stderr.decode('utf-8', 'replace')[-300:]}")
    return completed.stdout


def find_built(source: Path, layer: str, version: str | None = None) -> Built:
    """The built layer under `source`, checked through; `version` picks one when there are several."""

    root = Path(source) / layer
    versions = sorted(item.name for item in root.iterdir() if item.is_dir()) if root.is_dir() else []
    if version is None:
        if len(versions) != 1:
            raise PublishError(f"{root}: expected exactly one built version, found {versions or 'none'}"
                               f" (pick one with --version {layer}=<version>)")
        version = versions[0]
    elif version not in versions:
        raise PublishError(f"{root}: no version {version} (found {versions or 'none'})")
    directory = root / version
    data = (directory / "manifest.json").read_bytes()
    manifest = lm.parse(data)
    if manifest.layer != layer or manifest.platform != lm.PLATFORM:
        raise PublishError(f"{directory}: the manifest is for {manifest.layer}/{manifest.platform}")
    if lm.version_of(data) != version:
        raise PublishError(f"{directory}: the manifest's version is {lm.version_of(data)}, not {version}")
    summary = json.loads((directory / "summary.json").read_text(encoding="utf-8"))
    if summary.get("version") != version or summary.get("manifest_sha256") != lm.digest_of(data):
        raise PublishError(f"{directory}: summary.json describes another manifest")
    archive = directory / "layer.tar.zst"
    size = archive.stat().st_size
    if size != summary["archive"]["size"]:
        raise PublishError(f"{archive}: {size} bytes, summary.json says {summary['archive']['size']}")
    log(f"{layer} {version}: hashing the archive ({gib(size)})")
    digest = lm.sha256_file(archive)
    if digest != summary["archive"]["sha256"]:
        raise PublishError(f"{archive}: sha256 {digest}, summary.json says {summary['archive']['sha256']}")
    if embedded_manifest(archive) != data:
        raise PublishError(f"{archive} carries another manifest than {directory / 'manifest.json'}")
    return Built(layer, version, directory, data, size, digest)


# --- layers.json ----------------------------------------------------------------------------------


def manifest_key(layer: str, sha256: str) -> str:
    return f"{PREFIX}/{layer}/{sha256}.json"


def archive_key(layer: str, sha256: str) -> str:
    return f"{PREFIX}/{layer}/{sha256}.tar.zst"


def file_key(sha256: str, pattern: str = FILES_PATH) -> str:
    return pattern.replace("{sha256}", sha256)


def build_index(built: dict[str, Built], base_url: str = BASE_URL,
                file_layers: Iterable[str] = FILE_LAYERS) -> dict[str, Any]:
    """`layers.json` in the format fixed in the plan's WP-E2 section (`files.layers` = `file_layers`)."""

    layers: dict[str, Any] = {}
    for layer in lm.LAYERS:
        item = built[layer]
        layers[layer] = {
            "version": item.version,
            "manifest": {"path": manifest_key(layer, item.manifest_sha256), "size": len(item.manifest_data),
                         "sha256": item.manifest_sha256},
            "archive": {"path": archive_key(layer, item.archive_sha256), "size": item.archive_size,
                        "sha256": item.archive_sha256},
        }
    return {
        "schema": INDEX_SCHEMA,
        "platform": lm.PLATFORM,
        "baseUrl": base_url,
        "layers": layers,
        "files": {"layers": list(file_layers), "path": FILES_PATH},
    }


def render_index(index: dict[str, Any]) -> bytes:
    return (json.dumps(index, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _check_entry(entry: Any, where: str) -> None:
    if not (isinstance(entry, dict) and isinstance(entry.get("path"), str) and isinstance(entry.get("size"), int)
            and entry["size"] >= 0 and isinstance(entry.get("sha256"), str) and _HEX64.fullmatch(entry["sha256"])):
        raise PublishError(f"layers.json: {where} needs path, size and sha256")
    try:
        lm.check_path(entry["path"])
    except lm.LayerError as error:
        raise PublishError(f"layers.json: {where}: {error}") from error


def check_index(index: Any) -> dict[str, Any]:
    """`index` as the format allows it, or `PublishError`."""

    if not isinstance(index, dict) or index.get("schema") != INDEX_SCHEMA:
        raise PublishError(f"layers.json: unknown schema {index.get('schema') if isinstance(index, dict) else None!r}")
    if index.get("platform") != lm.PLATFORM:
        raise PublishError(f"layers.json: platform {index.get('platform')!r}")
    base = index.get("baseUrl")
    if not (isinstance(base, str) and base.endswith("/") and urlsplit(base).scheme in ("http", "https")):
        raise PublishError("layers.json: baseUrl must be an http(s) URL ending in /")
    layers = index.get("layers")
    if not isinstance(layers, dict) or set(layers) != set(lm.LAYERS):
        raise PublishError(f"layers.json: layers must be exactly {', '.join(lm.LAYERS)}")
    for layer, item in layers.items():
        _check_entry(item.get("manifest"), f"{layer}.manifest")
        _check_entry(item.get("archive"), f"{layer}.archive")
        if item.get("version") != item["manifest"]["sha256"][:16]:
            raise PublishError(f"layers.json: {layer}.version is not its manifest's sha256 prefix")
    files = index.get("files")
    if not (isinstance(files, dict) and isinstance(files.get("path"), str) and "{sha256}" in files["path"]
            and isinstance(files.get("layers"), list) and set(files["layers"]) <= set(lm.LAYERS)):
        raise PublishError("layers.json: files needs layers and a path with {sha256}")
    return index


def load_index(path: Path = INDEX_FILE) -> dict[str, Any]:
    return check_index(json.loads(Path(path).read_text(encoding="utf-8")))


def index_command(args: argparse.Namespace) -> int:
    chosen = dict(item.split("=", 1) for item in args.version or [])
    built = {layer: find_built(Path(args.source), layer, chosen.get(layer)) for layer in lm.LAYERS}
    data = render_index(build_index(built, args.base_url))
    out = Path(args.out)
    if out.is_file() and out.read_bytes() == data:
        log(f"{out} is up to date")
        return 0
    out.write_bytes(data)
    log(f"wrote {out}")
    return 0


# --- the objects ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class PublishObject:
    key: str
    size: int
    sha256: str
    source: Path
    kind: str  # manifest, archive, file
    content_type: str = "application/octet-stream"


def unpack(archive: Path, target: Path) -> None:
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)
    completed = subprocess.run([tar_program(), "-x", "-f", str(archive), "-C", str(target)], capture_output=True)
    if completed.returncode != 0:
        raise PublishError(f"tar could not unpack {archive}: {completed.stderr.decode('utf-8', 'replace')[-400:]}")


def unpacked_layer(built: Built, work: Path) -> Path:
    """The layer unpacked under `work` and checked file by file (reused when it is already there and whole)."""

    target = Path(work) / built.layer / built.version
    if (target / lm.MANIFEST_NAME).is_file() and (target / lm.MANIFEST_NAME).read_bytes() == built.manifest_data:
        manifest = lm.parse(built.manifest_data)
        if not lm.compare(manifest, target, full=False):
            return target
    log(f"{built.layer} {built.version}: unpacking into {target}")
    unpack(built.archive_path, target)
    manifest, data = lm.manifest_in(target)
    problems = lm.compare(manifest, target, full=True)
    if data != built.manifest_data or problems:
        raise PublishError(f"{target} does not match its manifest: {problems[:10]}")
    return target


def plan_objects(index: dict[str, Any], source: Path, work: Path) -> list[PublishObject]:
    """Every object `index` names, each with the local file it comes from, checked against the index."""

    objects: list[PublishObject] = []
    for layer in lm.LAYERS:
        entry = index["layers"][layer]
        directory = Path(source) / layer / entry["version"]
        manifest_path = directory / "manifest.json"
        archive_path = directory / "layer.tar.zst"
        data = manifest_path.read_bytes()
        if lm.digest_of(data) != entry["manifest"]["sha256"] or len(data) != entry["manifest"]["size"]:
            raise PublishError(f"{manifest_path} is not the manifest layers.json names")
        if archive_path.stat().st_size != entry["archive"]["size"]:
            raise PublishError(f"{archive_path} is not the size layers.json names")
        objects.append(PublishObject(entry["archive"]["path"], entry["archive"]["size"], entry["archive"]["sha256"],
                                     archive_path, "archive"))
        objects.append(PublishObject(entry["manifest"]["path"], entry["manifest"]["size"],
                                     entry["manifest"]["sha256"], manifest_path, "manifest", "application/json"))
    seen = {item.key for item in objects}
    for layer in index["files"]["layers"]:
        entry = index["layers"][layer]
        built = Built(layer, entry["version"], Path(source) / layer / entry["version"],
                      (Path(source) / layer / entry["version"] / "manifest.json").read_bytes(),
                      entry["archive"]["size"], entry["archive"]["sha256"])
        root = unpacked_layer(built, work)
        for item in lm.parse(built.manifest_data).files:
            key = file_key(item.sha256, index["files"]["path"])
            if key in seen:
                continue
            seen.add(key)
            objects.append(PublishObject(key, item.size, item.sha256, root / item.path, "file"))
    return objects


def describe(objects: list[PublishObject]) -> str:
    lines = []
    for kind in ("archive", "manifest", "file"):
        chosen = [item for item in objects if item.kind == kind]
        lines.append(f"  {kind:8} {len(chosen):4} objects  {human(sum(item.size for item in chosen)):>12}")
    lines.append(f"  {'total':8} {len(objects):4} objects  {human(sum(item.size for item in objects)):>12}")
    return "\n".join(lines)


# --- credentials ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Credentials:
    account_id: str
    access_key_id: str
    secret_access_key: str = field(repr=False)

    def __repr__(self) -> str:  # never the values
        return "Credentials(<hidden>)"

    @property
    def endpoint(self) -> str:
        return f"https://{self.account_id}.r2.cloudflarestorage.com"


def env_candidates() -> list[Path]:
    own = Path(__file__).resolve().parent / ENV_NAME
    main = main_checkout().resolve() / "scripts" / ENV_NAME
    return [own] if main == own else [own, main]


def git_ignored(path: Path) -> bool:
    """Git ignores `path` (a tracked file never counts as ignored)."""

    try:
        completed = subprocess.run(["git", "check-ignore", "-q", str(path)], cwd=Path(path).parent,
                                   capture_output=True)
    except OSError:
        return False
    return completed.returncode == 0


def parse_env(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        name = name.strip()
        if name.startswith("export "):
            name = name[len("export "):].strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        values[name] = value
    return values


def load_credentials(candidates: Iterable[Path] | None = None, *, require_ignored: bool = True) -> Credentials:
    """The R2 credentials from the first `.env` that exists. Errors name files and keys, never values."""

    tried = []
    for path in candidates if candidates is not None else env_candidates():
        path = Path(path)
        tried.append(str(path))
        if not path.is_file():
            continue
        if require_ignored and not git_ignored(path):
            raise PublishError(f"{path} is not ignored by git: refusing to read credentials from it")
        values = parse_env(path.read_text(encoding="utf-8-sig"))
        missing = [name for name in ENV_KEYS if not values.get(name)]
        if missing:
            raise PublishError(f"{path} lacks {', '.join(missing)}")
        account = values["R2_ACCOUNT_ID"].lower()
        if not re.fullmatch(r"[0-9a-f]{32}", account):
            raise PublishError(f"{path}: R2_ACCOUNT_ID is not 32 hex digits")
        return Credentials(account, values["R2_ACCESS_KEY_ID"], values["R2_SECRET_ACCESS_KEY"])
    raise PublishError("no credentials: create " + " or ".join(tried) + f" with {', '.join(ENV_KEYS)}")


# --- SigV4 ----------------------------------------------------------------------------------------


def _encode(value: str, safe: str = "-_.~") -> str:
    return quote(value, safe=safe)


def canonical_query(query: str) -> str:
    pairs = parse_qsl(query, keep_blank_values=True)
    return "&".join(f"{_encode(name)}={_encode(value)}" for name, value in sorted(pairs))


def sign(method: str, url: str, headers: dict[str, str], payload_sha256: str, *, access_key_id: str,
         secret_access_key: str, now: dt.datetime, region: str = "auto", service: str = "s3") -> dict[str, str]:
    """`headers` plus `x-amz-date`, `x-amz-content-sha256` and `Authorization` (AWS Signature Version 4).

    Every header passed in is signed, and `host`. `url`'s path must be URI-encoded already (S3 signs it
    as sent, encoded once); its query is decoded and encoded again the canonical way.
    """

    parts = urlsplit(url)
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    day = stamp[:8]
    signed = {name.lower(): " ".join(str(value).strip().split()) for name, value in headers.items()}
    signed["host"] = parts.netloc
    signed["x-amz-date"] = stamp
    signed["x-amz-content-sha256"] = payload_sha256
    names = sorted(signed)
    canonical = "\n".join([
        method.upper(),
        parts.path or "/",
        canonical_query(parts.query),
        "".join(f"{name}:{signed[name]}\n" for name in names),
        ";".join(names),
        payload_sha256,
    ])
    scope = f"{day}/{region}/{service}/aws4_request"
    to_sign = "\n".join(["AWS4-HMAC-SHA256", stamp, scope, sha256_bytes(canonical.encode("utf-8"))])
    key = ("AWS4" + secret_access_key).encode("utf-8")
    for item in (day, region, service, "aws4_request"):
        key = hmac.new(key, item.encode("utf-8"), hashlib.sha256).digest()
    signature = hmac.new(key, to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
    out = {name: value for name, value in headers.items()}
    out["x-amz-date"] = stamp
    out["x-amz-content-sha256"] = payload_sha256
    out["Authorization"] = (f"AWS4-HMAC-SHA256 Credential={access_key_id}/{scope}, "
                            f"SignedHeaders={';'.join(names)}, Signature={signature}")
    return out


# --- the bucket -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class RemoteObject:
    key: str
    size: int
    sha256: str | None


def _strip_namespaces(element: ElementTree.Element) -> ElementTree.Element:
    for item in element.iter():
        if "}" in item.tag:
            item.tag = item.tag.split("}", 1)[1]
    return element


def _xml(data: bytes) -> ElementTree.Element:
    return _strip_namespaces(ElementTree.fromstring(data))


def _s3_error(response: httpx.Response) -> str:
    """The S3 error code and message only: an error body can echo the access key id, so nothing else."""

    code = message = ""
    try:
        root = _xml(response.content)
        code, message = root.findtext("Code") or "", root.findtext("Message") or ""
    except ElementTree.ParseError:
        pass
    return f"HTTP {response.status_code} {code} {message}".strip()


class R2Store:
    """The bucket over R2's S3 API (path style), every request signed."""

    def __init__(self, client: httpx.Client, credentials: Credentials, bucket: str = BUCKET,
                 clock: Callable[[], dt.datetime] | None = None) -> None:
        self.client = client
        self.credentials = credentials
        self.bucket = bucket
        self.clock = clock or (lambda: dt.datetime.now(dt.timezone.utc))
        self.requests: list[tuple[str, str]] = []

    def url(self, key: str = "", query: str = "") -> str:
        path = f"/{self.bucket}/{_encode(key, safe='/-_.~')}" if key else f"/{self.bucket}"
        return f"{self.credentials.endpoint}{path}" + (f"?{query}" if query else "")

    def request(self, method: str, key: str = "", *, query: str = "", headers: dict[str, str] | None = None,
                body: bytes = b"", payload_sha256: str | None = None) -> httpx.Response:
        url = self.url(key, query)
        digest = payload_sha256 or sha256_bytes(body)
        last: BaseException | None = None
        for attempt in range(ATTEMPTS):
            signed = sign(method, url, dict(headers or {}), digest, access_key_id=self.credentials.access_key_id,
                          secret_access_key=self.credentials.secret_access_key, now=self.clock())
            if body or method in ("PUT", "POST"):
                signed["Content-Length"] = str(len(body))
            # R2 gzips a JSON object for a client that accepts it, and then a HEAD carries no
            # Content-Length (seen 2026-11-13 on the Python layer's manifest): ask for the bytes as stored.
            signed["Accept-Encoding"] = "identity"
            try:
                response = self.client.request(method, url, headers=signed, content=body or None)
            except httpx.TransportError as error:
                last = error
            else:
                self.requests.append((method, key + (f"?{query}" if query else "")))
                if response.status_code < 500:
                    return response
                last = PublishError(f"{method} {key}: {_s3_error(response)}")
            time.sleep(min(2 ** attempt, 10))
        raise PublishError(f"{method} {key or self.bucket} failed {ATTEMPTS} times: {last}")

    def head(self, key: str) -> RemoteObject | None:
        response = self.request("HEAD", key)
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            raise PublishError(f"HEAD {key}: HTTP {response.status_code}")
        return RemoteObject(key, int(response.headers.get("content-length", "-1")),
                            response.headers.get(SHA256_META))

    def get(self, key: str) -> bytes:
        response = self.request("GET", key)
        if response.status_code != 200:
            raise PublishError(f"GET {key}: {_s3_error(response)}")
        return response.content

    def _object_headers(self, item: PublishObject) -> dict[str, str]:
        return {"Content-Type": item.content_type, "Cache-Control": CACHE_CONTROL, SHA256_META: item.sha256}

    def put(self, item: PublishObject, data: bytes) -> bool:
        """Create the object; False when it exists already (the write is conditional)."""

        headers = self._object_headers(item) | {"If-None-Match": "*"}
        response = self.request("PUT", item.key, headers=headers, body=data, payload_sha256=item.sha256)
        if response.status_code == 412:
            return False
        if response.status_code != 200:
            raise PublishError(f"PUT {item.key}: {_s3_error(response)}")
        return True

    def create_multipart(self, item: PublishObject) -> str:
        response = self.request("POST", item.key, query="uploads", headers=self._object_headers(item))
        if response.status_code != 200:
            raise PublishError(f"CreateMultipartUpload {item.key}: {_s3_error(response)}")
        upload = _xml(response.content).findtext("UploadId")
        if not upload:
            raise PublishError(f"CreateMultipartUpload {item.key}: no UploadId")
        return upload

    def upload_part(self, key: str, upload_id: str, number: int, data: bytes, digest: str) -> str:
        query = f"partNumber={number}&uploadId={_encode(upload_id)}"
        response = self.request("PUT", key, query=query, body=data, payload_sha256=digest)
        if response.status_code != 200:
            raise PublishError(f"UploadPart {key} #{number}: {_s3_error(response)}")
        return response.headers["etag"]

    def complete_multipart(self, key: str, upload_id: str, etags: list[str]) -> bool:
        body = ("<CompleteMultipartUpload>" + "".join(
            f"<Part><PartNumber>{number}</PartNumber><ETag>{etag}</ETag></Part>"
            for number, etag in enumerate(etags, start=1)) + "</CompleteMultipartUpload>").encode("utf-8")
        response = self.request("POST", key, query=f"uploadId={_encode(upload_id)}",
                                headers={"Content-Type": "application/xml", "If-None-Match": "*"}, body=body)
        if response.status_code == 412:
            return False
        if response.status_code != 200 or b"<Error>" in response.content:
            raise PublishError(f"CompleteMultipartUpload {key}: {_s3_error(response)}")
        return True

    def abort_multipart(self, key: str, upload_id: str) -> None:
        try:
            self.request("DELETE", key, query=f"uploadId={_encode(upload_id)}")
        except PublishError:
            pass  # an upload left behind is aborted by R2 after a week

    def list(self, prefix: str) -> Iterator[RemoteObject]:
        token = ""
        while True:
            query = f"list-type=2&prefix={_encode(prefix)}" + (f"&continuation-token={_encode(token)}" if token else "")
            response = self.request("GET", query=query)
            if response.status_code != 200:
                raise PublishError(f"ListObjectsV2 {prefix}: {_s3_error(response)}")
            root = _xml(response.content)
            for item in root.findall("Contents"):
                yield RemoteObject(item.findtext("Key") or "", int(item.findtext("Size") or 0), None)
            if root.findtext("IsTruncated") != "true":
                return
            token = root.findtext("NextContinuationToken") or ""


# --- upload ---------------------------------------------------------------------------------------


def check_existing(item: PublishObject, remote: RemoteObject) -> None:
    if remote.size != item.size or remote.sha256 != item.sha256:
        raise Conflict(f"{item.key} is in the bucket with size {remote.size} and sha256 {remote.sha256}, "
                       f"not {item.size} / {item.sha256}: not overwritten, stopping")


def _upload_parts(store: R2Store, item: PublishObject) -> bool:
    upload = store.create_multipart(item)
    try:
        whole = hashlib.sha256()
        etags: list[str] = []
        sent = 0
        started = time.perf_counter()
        with open(item.source, "rb") as source:
            while True:
                data = source.read(PART_SIZE)
                if not data:
                    break
                whole.update(data)
                etags.append(store.upload_part(item.key, upload, len(etags) + 1, data, sha256_bytes(data)))
                sent += len(data)
                rate = sent / max(time.perf_counter() - started, 1e-6)
                log(f"  {item.key}: {human(sent)} / {human(item.size)} ({human(int(rate))}/s)")
        if sent != item.size or whole.hexdigest() != item.sha256:
            raise PublishError(f"{item.source} changed: {sent} bytes, sha256 {whole.hexdigest()}")
        created = store.complete_multipart(item.key, upload, etags)
        if not created:
            store.abort_multipart(item.key, upload)  # lost a race: the parts are not needed
        return created
    except BaseException:
        store.abort_multipart(item.key, upload)
        raise


def upload_object(store: R2Store, item: PublishObject) -> str:
    """`skipped` (there already, same content) or `uploaded`; `Conflict` when the name holds other content."""

    remote = store.head(item.key)
    if remote is not None:
        check_existing(item, remote)
        return "skipped"
    if item.size <= SINGLE_PUT_LIMIT:
        data = Path(item.source).read_bytes()
        if len(data) != item.size or sha256_bytes(data) != item.sha256:
            raise PublishError(f"{item.source} is not the file planned ({item.key})")
        created = store.put(item, data)
    else:
        created = _upload_parts(store, item)
    remote = store.head(item.key)
    if remote is None:
        raise PublishError(f"{item.key} is not in the bucket after its upload")
    check_existing(item, remote)
    return "uploaded" if created else "skipped"


def upload_all(store: R2Store, objects: list[PublishObject]) -> dict[str, list[str]]:
    outcome: dict[str, list[str]] = {"uploaded": [], "skipped": []}
    for number, item in enumerate(objects, start=1):
        result = upload_object(store, item)
        outcome[result].append(item.key)
        log(f"[{number}/{len(objects)}] {result}: {item.key} ({human(item.size)})")
    return outcome


# --- routes ---------------------------------------------------------------------------------------


def chosen_route(args: argparse.Namespace, *, required: bool) -> fetch.NetworkRoute:
    if getattr(args, "proxy", None):
        proxy = fetch.normalise_proxy(args.proxy)
        return fetch.NetworkRoute(f"代理 {fetch.without_userinfo(proxy)}", proxy)
    if getattr(args, "direct", False):
        return fetch.NetworkRoute("直连", None)
    if required:
        raise PublishError("say how to reach the bucket: --direct or --proxy URL")
    return fetch.network_routes()[0]


def timeout() -> httpx.Timeout:
    return httpx.Timeout(connect=fetch.CONNECT_TIMEOUT_SEC, read=300.0, write=300.0, pool=30.0)


def open_store(route: fetch.NetworkRoute) -> R2Store:
    credentials = load_credentials()
    client = fetch.create_client(route, timeout=timeout())
    return R2Store(client, credentials)


def resolve_work(value: str | None) -> Path:
    return Path(value) if value else main_checkout() / ".tmp" / "layer-publish"


def plan_command(args: argparse.Namespace) -> int:
    index = load_index(Path(args.index))
    objects = plan_objects(index, Path(args.source), resolve_work(args.work))
    print(f"objects for {BUCKET} ({index['baseUrl']}):\n{describe(objects)}")
    if args.list:
        for item in objects:
            print(f"  {item.size:>12}  {item.key}")
    if args.remote:
        route = chosen_route(args, required=False)
        store = open_store(route)
        present = missing = 0
        for item in objects:
            remote = store.head(item.key)
            if remote is None:
                missing += 1
            else:
                check_existing(item, remote)
                present += 1
        print(f"in the bucket already: {present}; to upload: {missing} (route: {route.label})")
    return 0


def upload_command(args: argparse.Namespace) -> int:
    index = load_index(Path(args.index))
    objects = plan_objects(index, Path(args.source), resolve_work(args.work))
    route = chosen_route(args, required=False)
    print(f"uploading to {BUCKET} via {route.label}:\n{describe(objects)}", flush=True)
    store = open_store(route)
    outcome = upload_all(store, objects)
    print(f"uploaded {len(outcome['uploaded'])}, skipped {len(outcome['skipped'])} (already there, same content)")
    return 0


# --- verify from outside --------------------------------------------------------------------------


@dataclass
class Verification:
    checked: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    downloaded: int = 0


def _fetch(url: str, size: int, sha256: str, destination: Path, route: fetch.NetworkRoute) -> Path:
    asset = fetch.DownloadAsset(url=url, size=size, sha256=sha256)
    last = [0.0]

    def progress(state: fetch.DownloadProgress) -> None:
        now = time.monotonic()
        if state.total > 64 * 1024 * 1024 and now - last[0] > 15:
            last[0] = now
            log(f"  {destination.name}: {human(state.downloaded)} / {human(state.total)}"
                f" ({human(int(state.bytes_per_second))}/s)")

    return fetch.download_file(asset, destination, progress, routes=lambda: [route])


def verify(index: dict[str, Any], route: fetch.NetworkRoute, work: Path, *, keep: bool = False,
           base_url: str | None = None) -> Verification:
    """Download what `index` names from its public address and check it against the manifests."""

    base = base_url or index["baseUrl"]
    result = Verification()
    work = Path(work)
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    try:
        manifests: dict[str, lm.LayerManifest] = {}
        for layer in lm.LAYERS:
            entry = index["layers"][layer]
            try:
                path = _fetch(base + entry["manifest"]["path"], entry["manifest"]["size"],
                              entry["manifest"]["sha256"], work / layer / "manifest.json", route)
                data = path.read_bytes()
                manifest = lm.parse(data)
                if manifest.layer != layer or lm.version_of(data) != entry["version"]:
                    raise PublishError(f"{layer}: the manifest is {manifest.layer} {lm.version_of(data)}")
                manifests[layer] = manifest
                archive = _fetch(base + entry["archive"]["path"], entry["archive"]["size"],
                                 entry["archive"]["sha256"], work / layer / "layer.tar.zst", route)
                result.downloaded += entry["manifest"]["size"] + entry["archive"]["size"]
                target = work / layer / "unpacked"
                unpack(archive, target)
                if not keep:
                    archive.unlink()
                carried, carried_data = lm.manifest_in(target)
                if carried_data != data:
                    raise PublishError(f"{layer}: the archive carries another manifest")
                problems = lm.compare(carried, target, full=True)
                if problems:
                    raise PublishError(f"{layer}: the unpacked archive departs from its manifest: {problems[:10]}")
                if not keep:
                    shutil.rmtree(target)
                result.checked.append(f"{layer} {entry['version']}: manifest, archive, "
                                      f"{len(manifest.files)} files unpacked and hashed")
                log(result.checked[-1])
            except (PublishError, fetch.DownloadError, lm.LayerError, httpx.HTTPError) as error:
                result.problems.append(f"{layer}: {error}")
        for layer in index["files"]["layers"]:
            if layer not in manifests:
                continue
            done: set[str] = set()
            for item in manifests[layer].files:
                if item.sha256 in done:
                    continue
                done.add(item.sha256)
                destination = work / "files" / item.sha256
                try:
                    _fetch(base + file_key(item.sha256, index["files"]["path"]), item.size, item.sha256,
                           destination, route)
                    result.downloaded += item.size
                except (fetch.DownloadError, httpx.HTTPError) as error:
                    result.problems.append(f"{layer} file {item.path}: {error}")
                finally:
                    if not keep:
                        destination.unlink(missing_ok=True)
            result.checked.append(f"{layer}: {len(done)} single files")
            log(result.checked[-1])
    finally:
        if not keep:
            shutil.rmtree(work, ignore_errors=True)
    return result


def verify_command(args: argparse.Namespace) -> int:
    index = load_index(Path(args.index))
    route = chosen_route(args, required=True)
    work = Path(args.work) if args.work else main_checkout() / ".tmp" / "layer-verify"
    log(f"verifying {index['baseUrl']} via {route.label}")
    started = time.perf_counter()
    result = verify(index, route, work, keep=args.keep, base_url=args.base_url)
    seconds = time.perf_counter() - started
    print(json.dumps({"route": route.label, "baseUrl": args.base_url or index["baseUrl"], "checked": result.checked,
                      "problems": result.problems, "downloaded": result.downloaded,
                      "downloaded_human": human(result.downloaded), "seconds": round(seconds, 1)},
                     ensure_ascii=False, indent=1))
    return 1 if result.problems else 0


# --- what no published version refers to ----------------------------------------------------------


def published_indexes(repo: Path = ROOT, working: Path = INDEX_FILE) -> list[tuple[str, dict[str, Any]]]:
    """(`v*` tag or `working tree`, its `layers.json`) for every published desktop version that has one."""

    found: list[tuple[str, dict[str, Any]]] = []
    tags = subprocess.run(["git", "tag", "--list", "v*"], cwd=repo, capture_output=True, text=True, check=True)
    for tag in tags.stdout.split():
        shown = subprocess.run(["git", "show", f"{tag}:{INDEX_REPO_PATH}"], cwd=repo, capture_output=True)
        if shown.returncode == 0:
            found.append((tag, check_index(json.loads(shown.stdout.decode("utf-8")))))
    if Path(working).is_file():
        found.append(("working tree", load_index(working)))
    return found


def referenced_keys(indexes: list[tuple[str, dict[str, Any]]],
                    manifest_bytes: Callable[[str], bytes]) -> set[str]:
    """Every object key some index refers to; the per-file keys come from the manifests it names."""

    keys: set[str] = set()
    for _, index in indexes:
        for layer, entry in index["layers"].items():
            keys.add(entry["manifest"]["path"])
            keys.add(entry["archive"]["path"])
            if layer in index["files"]["layers"]:
                data = manifest_bytes(entry["manifest"]["path"])
                if lm.digest_of(data) != entry["manifest"]["sha256"]:
                    raise PublishError(f"{entry['manifest']['path']} is not the manifest the index names")
                keys.update(file_key(item.sha256, index["files"]["path"]) for item in lm.parse(data).files)
    return keys


def unreferenced(store: R2Store, indexes: list[tuple[str, dict[str, Any]]]) -> list[RemoteObject]:
    keys = referenced_keys(indexes, store.get)
    return [item for item in store.list(f"{PREFIX}/") if item.key not in keys]


def unreferenced_command(args: argparse.Namespace) -> int:
    indexes = published_indexes()
    store = open_store(chosen_route(args, required=False))
    left = unreferenced(store, indexes)
    print("published versions considered: " + (", ".join(label for label, _ in indexes) or "none"))
    groups: dict[str, list[RemoteObject]] = {}
    for item in left:
        parts = item.key.split("/")
        groups.setdefault(parts[1] if len(parts) > 2 else "?", []).append(item)
    for group, items in sorted(groups.items()):
        print(f"{group}: {len(items)} objects, {human(sum(item.size for item in items))}")
        for item in sorted(items, key=lambda item: item.key):
            name = item.key.rsplit("/", 1)[-1]
            note = f"  (manifest of version {name[:16]})" if name.endswith(".json") else ""
            print(f"  {item.size:>12}  {item.key}{note}")
    print(f"not referenced by any published version: {len(left)} objects, "
          f"{human(sum(item.size for item in left))}. Nothing was deleted; deleting is a separate step the owner confirms.")
    return 0


# --- the rate-limiting rule -----------------------------------------------------------------------


def burst(url: str, route: fetch.NetworkRoute, count: int, workers: int = 8) -> list[int]:
    """`count` GETs of `url` as fast as `workers` threads go; their status codes in order of completion."""

    statuses: list[int] = []
    with fetch.create_client(route, timeout=httpx.Timeout(20.0)) as client:
        def one(_: int) -> int:
            try:
                return client.get(url).status_code
            except httpx.HTTPError:
                return 0

        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            statuses.extend(pool.map(one, range(count)))
    return statuses


def smallest_object_url(index: dict[str, Any], base: str) -> str:
    """The smallest object `index` names (a manifest): what a burst of requests fetches."""

    entries = [entry for item in index["layers"].values() for entry in (item["manifest"], item["archive"])]
    return base + min(entries, key=lambda entry: (entry["size"], entry["path"]))["path"]


def ratelimit_command(args: argparse.Namespace) -> int:
    index = load_index(Path(args.index))
    route = chosen_route(args, required=True)
    url = smallest_object_url(index, args.base_url or index["baseUrl"])
    log(f"{args.requests} GETs of {url} via {route.label}")
    statuses = burst(url, route, args.requests)
    counts = {str(code): statuses.count(code) for code in sorted(set(statuses))}
    blocked = statuses.count(429)
    log(f"status codes: {counts}")
    log(f"waiting {args.wait}s for the block to lapse")
    time.sleep(args.wait)
    after = burst(url, route, 1, workers=1)[0]
    log(f"after the wait: {after}")
    ok = blocked > 0 and after == 200
    print("the rule blocks a burst and lets requests through again afterwards" if ok
          else "the rule did NOT behave as expected")
    return 0 if ok else 1


# --- main -----------------------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    source = str(main_checkout() / "dist" / "layers")

    def common(command: argparse.ArgumentParser, *, route: bool) -> None:
        command.add_argument("--index", default=str(INDEX_FILE))
        command.add_argument("--source", default=source, help="WP-E1's output (read only)")
        command.add_argument("--work", help="where a layer whose files are hosted one by one is unpacked "
                             "(default <main>/.tmp/layer-publish; unused while files.layers is empty)")
        if route:
            group = command.add_mutually_exclusive_group()
            group.add_argument("--direct", action="store_true", help="no proxy")
            group.add_argument("--proxy", help="through this proxy, e.g. http://127.0.0.1:7890")

    making = commands.add_parser("index", help="write layers.json from the built layers")
    making.add_argument("--source", default=source)
    making.add_argument("--out", default=str(INDEX_FILE))
    making.add_argument("--base-url", default=BASE_URL)
    making.add_argument("--version", action="append", help="<layer>=<version> when several are built")
    planning = commands.add_parser("plan", help="list the objects to upload, their number and size")
    common(planning, route=True)
    planning.add_argument("--list", action="store_true", help="every object")
    planning.add_argument("--remote", action="store_true", help="also ask the bucket which are there already")
    uploading = commands.add_parser("upload", help="upload what is missing (never overwrites)")
    common(uploading, route=True)
    verifying = commands.add_parser("verify", help="download from the public address and check")
    common(verifying, route=True)
    verifying.add_argument("--base-url", help="instead of layers.json's baseUrl")
    verifying.add_argument("--keep", action="store_true", help="keep the downloads")
    listing = commands.add_parser("unreferenced", help="objects no published version refers to (lists only)")
    common(listing, route=True)
    limiting = commands.add_parser("ratelimit", help="check that the rate-limiting rule blocks a burst")
    common(limiting, route=True)
    limiting.add_argument("--base-url")
    limiting.add_argument("--requests", type=int, default=120)
    limiting.add_argument("--wait", type=float, default=15.0)
    args = parser.parse_args(argv)
    handlers = {"index": index_command, "plan": plan_command, "upload": upload_command, "verify": verify_command,
                "unreferenced": unreferenced_command, "ratelimit": ratelimit_command}
    try:
        return handlers[args.command](args)
    except PublishError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
