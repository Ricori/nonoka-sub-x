"""Publishing the layers on R2 (WP-E2), against a fake bucket: nothing here touches Cloudflare.

The fake speaks the slice of R2's S3 API the script uses -- HEAD/GET/PUT of an object, conditional
writes, multipart uploads (with R2's equal-part-size rule), ListObjectsV2 in pages -- and checks every
request's SigV4 signature and payload digest. The layers are small ones built by `build_layers.Build`
and packed with Windows' `tar.exe`, laid out the way WP-E1 leaves `dist/layers/`. The public address is
a local HTTP server serving the fake bucket's objects (plan WP-E2: `baseUrl` may be a local server).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import http.server
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
import xml.etree.ElementTree as ElementTree
from pathlib import Path
from typing import Any, Callable
from unittest import mock
from urllib.parse import parse_qsl, unquote

import httpx

ROOT = Path(__file__).resolve().parents[1]
for path in (str(ROOT / "src"), str(ROOT)):
    if path not in sys.path:
        sys.path.insert(0, path)

from nonoka_x.runtime import fetch  # noqa: E402
from nonoka_x.runtime import layer_manifest as lm  # noqa: E402

from scripts import build_layers, publish_layers as pl  # noqa: E402

WINDOWS = os.name == "nt"
ACCESS_KEY_ID = "AKIDTESTONLY00000000"
SECRET = "test-secret-not-real-0000000000000000000"
CREDENTIALS = pl.Credentials("0" * 32, ACCESS_KEY_ID, SECRET)


def write(path: Path, data: bytes | str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data.encode("utf-8") if isinstance(data, str) else data)
    return path


def s3_error(status: int, code: str, extra: str = "") -> httpx.Response:
    body = f"<?xml version='1.0'?><Error><Code>{code}</Code><Message>{code} here</Message>{extra}</Error>"
    return httpx.Response(status, content=body.encode("utf-8"), headers={"content-type": "application/xml"})


class FakeR2:
    """An in-memory bucket behind `httpx.MockTransport`, strict about signatures and digests."""

    def __init__(self, bucket: str = pl.BUCKET) -> None:
        self.bucket = bucket
        self.objects: dict[str, dict[str, Any]] = {}
        self.uploads: dict[str, dict[str, Any]] = {}
        self.calls: list[tuple[str, str]] = []
        self.page_size = 2
        #: Called with (method, key) before a write is applied; may change the bucket (a race).
        self.before_write: Callable[[str, str], None] | None = None
        #: (method, query-start) -> number of 500s still to answer with.
        self.failures: dict[tuple[str, str], int] = {}

    # -- checks
    def check(self, request: httpx.Request) -> httpx.Response | None:
        authorization = request.headers.get("authorization", "")
        if not authorization.startswith("AWS4-HMAC-SHA256 "):
            return s3_error(403, "AccessDenied")
        fields = dict(item.strip().split("=", 1) for item in authorization[len("AWS4-HMAC-SHA256 "):].split(","))
        names = fields["SignedHeaders"].split(";")
        assert {"host", "x-amz-date", "x-amz-content-sha256"} <= set(names), names
        given = {name: request.headers[name] for name in names if name not in ("host", "x-amz-date", "x-amz-content-sha256")}
        now = dt.datetime.strptime(request.headers["x-amz-date"], "%Y%m%dT%H%M%SZ")
        payload = request.headers["x-amz-content-sha256"]
        expected = pl.sign(request.method, str(request.url), given, payload, access_key_id=ACCESS_KEY_ID,
                           secret_access_key=SECRET, now=now)["Authorization"]
        if expected != authorization:
            return s3_error(403, "SignatureDoesNotMatch", f"<AWSAccessKeyId>{ACCESS_KEY_ID}</AWSAccessKeyId>")
        if hashlib.sha256(request.content).hexdigest() != payload:
            return s3_error(400, "XAmzContentSHA256Mismatch")
        return None

    def handler(self, request: httpx.Request) -> httpx.Response:
        raw = request.url.raw_path.decode("ascii")
        path, _, query_text = raw.partition("?")
        query = dict(parse_qsl(query_text, keep_blank_values=True))
        prefix = f"/{self.bucket}"
        assert path == prefix or path.startswith(prefix + "/"), path
        key = unquote(path[len(prefix) + 1:])
        self.calls.append((request.method, key + (f"?{query_text}" if query_text else "")))
        refused = self.check(request)
        if refused is not None:
            return refused
        for (method, start), left in list(self.failures.items()):
            if request.method == method and query_text.startswith(start) and left > 0:
                self.failures[(method, start)] = left - 1
                return s3_error(500, "InternalError")
        if not key:
            return self.list(query)
        if request.method in ("PUT", "POST") and self.before_write is not None:
            self.before_write(request.method, key)
        if request.method == "HEAD":
            item = self.objects.get(key)
            if item is None:
                return httpx.Response(404)
            return httpx.Response(200, headers={"content-length": str(len(item["data"])), **item["meta"]})
        if request.method == "GET":
            item = self.objects.get(key)
            return httpx.Response(200, content=item["data"]) if item else s3_error(404, "NoSuchKey")
        if request.method == "PUT" and "uploadId" in query:
            upload = self.uploads[query["uploadId"]]
            upload["parts"][int(query["partNumber"])] = request.content
            return httpx.Response(200, headers={"etag": f'"{hashlib.md5(request.content).hexdigest()}"'})
        if request.method == "PUT":
            if request.headers.get("if-none-match") == "*" and key in self.objects:
                return s3_error(412, "PreconditionFailed")
            self.objects[key] = {"data": request.content, "meta": self.meta(request)}
            return httpx.Response(200)
        if request.method == "POST" and "uploads" in query:
            upload_id = f"upload-{len(self.uploads) + 1}"
            self.uploads[upload_id] = {"key": key, "meta": self.meta(request), "parts": {}}
            body = f"<InitiateMultipartUploadResult><UploadId>{upload_id}</UploadId></InitiateMultipartUploadResult>"
            return httpx.Response(200, content=body.encode("utf-8"))
        if request.method == "POST" and "uploadId" in query:
            upload = self.uploads[query["uploadId"]]
            listed = [(int(part.findtext("PartNumber")), part.findtext("ETag"))
                      for part in ElementTree.fromstring(request.content).findall("Part")]
            parts = [upload["parts"][number] for number, _ in listed]
            for (number, etag), data in zip(listed, parts):
                assert etag == f'"{hashlib.md5(data).hexdigest()}"', number
            if len({len(data) for data in parts[:-1]}) > 1 or (len(parts) > 1 and len(parts[-1]) > len(parts[0])):
                return s3_error(400, "InvalidPart")
            if request.headers.get("if-none-match") == "*" and key in self.objects:
                return s3_error(412, "PreconditionFailed")
            self.objects[key] = {"data": b"".join(parts), "meta": upload["meta"]}
            del self.uploads[query["uploadId"]]
            return httpx.Response(200, content=b"<CompleteMultipartUploadResult/>")
        if request.method == "DELETE" and "uploadId" in query:
            self.uploads.pop(query["uploadId"], None)
            return httpx.Response(204)
        raise AssertionError(f"unexpected request: {request.method} {raw}")

    @staticmethod
    def meta(request: httpx.Request) -> dict[str, str]:
        kept = {name: value for name, value in request.headers.items() if name.startswith("x-amz-meta-")}
        kept["content-type"] = request.headers.get("content-type", "")
        kept["cache-control"] = request.headers.get("cache-control", "")
        return kept

    def list(self, query: dict[str, str]) -> httpx.Response:
        assert query.get("list-type") == "2"
        keys = sorted(key for key in self.objects if key.startswith(query.get("prefix", "")))
        start = int(query.get("continuation-token", "0"))
        page = keys[start:start + self.page_size]
        truncated = start + self.page_size < len(keys)
        body = "<ListBucketResult xmlns='http://s3.amazonaws.com/doc/2006-03-01/'>" + "".join(
            f"<Contents><Key>{key}</Key><Size>{len(self.objects[key]['data'])}</Size></Contents>" for key in page)
        body += f"<IsTruncated>{'true' if truncated else 'false'}</IsTruncated>"
        if truncated:
            body += f"<NextContinuationToken>{start + self.page_size}</NextContinuationToken>"
        return httpx.Response(200, content=(body + "</ListBucketResult>").encode("utf-8"))

    def snapshot(self) -> dict[str, tuple[bytes, dict[str, str]]]:
        return {key: (item["data"], dict(item["meta"])) for key, item in self.objects.items()}

    def writes(self) -> list[tuple[str, str]]:
        return [call for call in self.calls if call[0] in ("PUT", "POST", "DELETE")]


class PublicServer:
    """The bucket's public address: GET of `<key>` answers the fake bucket's object."""

    def __init__(self, fake: FakeR2) -> None:
        objects = fake.objects

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                item = objects.get(unquote(self.path.lstrip("/")))
                if item is None:
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header("Content-Length", str(len(item["data"])))
                self.end_headers()
                self.wfile.write(item["data"])

            def log_message(self, *args: Any) -> None:
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}/"

    def __enter__(self) -> "PublicServer":
        self.thread.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.server.shutdown()
        self.server.server_close()


class TempCase(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="nonoka-publish-"))

    def tearDown(self) -> None:
        shutil.rmtree(self.dir, ignore_errors=True)


DIRECT = fetch.NetworkRoute("直连", None)


class SignatureTests(unittest.TestCase):
    def test_matches_the_aws_get_object_example(self) -> None:
        # The "GET Object" example of AWS's Signature Version 4 documentation for S3.
        headers = pl.sign(
            "GET", "https://examplebucket.s3.amazonaws.com/test.txt", {"Range": "bytes=0-9"}, pl.EMPTY_SHA256,
            access_key_id="AKIAIOSFODNN7EXAMPLE", secret_access_key="wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
            now=dt.datetime(2013, 5, 24, tzinfo=dt.timezone.utc), region="us-east-1")
        self.assertEqual(headers["Authorization"],
                         "AWS4-HMAC-SHA256 Credential=AKIAIOSFODNN7EXAMPLE/20130524/us-east-1/s3/aws4_request, "
                         "SignedHeaders=host;range;x-amz-content-sha256;x-amz-date, "
                         "Signature=f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41")

    def test_canonical_query_sorts_and_encodes(self) -> None:
        self.assertEqual(pl.canonical_query("prefix=v1%2F&list-type=2&uploads"), "list-type=2&prefix=v1%2F&uploads=")


class CredentialTests(TempCase):
    def test_reads_the_three_keys(self) -> None:
        env = write(self.dir / ".env", f"# comment\nR2_ACCOUNT_ID={'A' * 32}\nR2_ACCESS_KEY_ID=\"{ACCESS_KEY_ID}\"\n"
                                       f"export R2_SECRET_ACCESS_KEY='{SECRET}'\n")
        credentials = pl.load_credentials([env], require_ignored=False)
        self.assertEqual(credentials.account_id, "a" * 32)
        self.assertEqual(credentials.endpoint, f"https://{'a' * 32}.r2.cloudflarestorage.com")
        self.assertEqual((credentials.access_key_id, credentials.secret_access_key), (ACCESS_KEY_ID, SECRET))
        self.assertNotIn(SECRET, repr(credentials))
        self.assertNotIn(ACCESS_KEY_ID, repr(credentials))

    def test_errors_name_keys_never_values(self) -> None:
        env = write(self.dir / ".env", f"R2_ACCOUNT_ID=nothex\nR2_SECRET_ACCESS_KEY={SECRET}\n")
        with self.assertRaises(pl.PublishError) as caught:
            pl.load_credentials([env], require_ignored=False)
        self.assertIn("R2_ACCESS_KEY_ID", str(caught.exception))
        self.assertNotIn(SECRET, str(caught.exception))
        with self.assertRaises(pl.PublishError):
            pl.load_credentials([self.dir / "missing.env"], require_ignored=False)

    def test_refuses_a_file_git_does_not_ignore(self) -> None:
        env = write(self.dir / ".env", f"R2_ACCOUNT_ID={'a' * 32}\nR2_ACCESS_KEY_ID=x\nR2_SECRET_ACCESS_KEY=y\n")
        with self.assertRaises(pl.PublishError) as caught:
            pl.load_credentials([env])  # not in a repository: git ignores nothing here
        self.assertIn("not ignored", str(caught.exception))

    def test_scripts_env_is_ignored_and_the_script_is_not(self) -> None:
        self.assertTrue(pl.git_ignored(ROOT / "scripts" / ".env"))
        self.assertFalse(pl.git_ignored(ROOT / "scripts" / "publish_layers.py"))
        candidates = pl.env_candidates()
        self.assertEqual(candidates[0], ROOT / "scripts" / ".env")
        self.assertTrue(all(path.name == ".env" and path.parent.name == "scripts" for path in candidates))


@unittest.skipUnless(WINDOWS and build_layers.SYSTEM_TAR.is_file(), "needs Windows' tar.exe")
class PublishFlowTests(TempCase):
    """index -> plan -> upload -> upload again -> verify -> unreferenced, on small layers."""

    def setUp(self) -> None:
        super().setUp()
        self.dist = self.dir / "dist" / "layers"
        builder = build_layers.Build(self.dir / "build", self.dist)
        builder.prepare()
        self.model_bytes = os.urandom(13_000)
        trees = {
            "python": {"python.exe": b"MZ" + b"\x00" * 300, "Lib/site-packages/demo/__init__.py": "x = 1\n"},
            "models": {"huggingface/hub/models--a--b/snapshots/r/model.bin": self.model_bytes,
                       "huggingface/hub/models--a--b/snapshots/r/config.json": "{}\n",
                       "huggingface/hub/models--a--b/.finesub-verified.json": "{\"ok\": true}\n",
                       "audio-separator/same-1.txt": "same\n", "audio-separator/same-2.txt": "same\n"},
            "tools": {"ffmpeg/bin/ffmpeg.exe": b"\x01" * 500, "node/node.exe": b"\x02" * 200},
        }
        for layer, files in trees.items():
            root = builder.work / layer
            for name, data in files.items():
                write(root / name, data)
            builder.finish(layer, root, {"sources": {}, "timings": {}}, [])
        self.work = self.dir / "work"
        self.fake = FakeR2()
        self.client = httpx.Client(transport=httpx.MockTransport(self.fake.handler))
        self.store = pl.R2Store(self.client, CREDENTIALS)
        patches = [mock.patch.object(pl, "SINGLE_PUT_LIMIT", 4096), mock.patch.object(pl, "PART_SIZE", 5000),
                   mock.patch.object(pl.time, "sleep")]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def tearDown(self) -> None:
        self.client.close()
        super().tearDown()

    def index(self) -> dict[str, Any]:
        built = {layer: pl.find_built(self.dist, layer) for layer in lm.LAYERS}
        return pl.check_index(json.loads(pl.render_index(pl.build_index(built))))

    def test_index_is_the_agreed_format(self) -> None:
        index = self.index()
        self.assertEqual(list(index), ["schema", "platform", "baseUrl", "layers", "files"])
        self.assertEqual((index["schema"], index["platform"], index["baseUrl"]),
                         (1, "windows-x64", "https://layers.nonoka.online/"))
        self.assertEqual(index["files"], {"layers": ["models"], "path": "v1/files/{sha256}"})
        self.assertEqual(list(index["layers"]), ["python", "models", "tools"])
        for layer, entry in index["layers"].items():
            directory = self.dist / layer / entry["version"]
            data = (directory / "manifest.json").read_bytes()
            archive = directory / "layer.tar.zst"
            self.assertEqual(set(entry), {"version", "manifest", "archive"})
            self.assertEqual(entry["version"], lm.version_of(data))
            self.assertEqual(entry["manifest"], {"path": f"v1/{layer}/{lm.digest_of(data)}.json", "size": len(data),
                                                 "sha256": lm.digest_of(data)})
            digest = lm.sha256_file(archive)
            self.assertEqual(entry["archive"], {"path": f"v1/{layer}/{digest}.tar.zst",
                                                "size": archive.stat().st_size, "sha256": digest})
        # The same inputs, the same bytes.
        again = pl.render_index(pl.build_index({layer: pl.find_built(self.dist, layer) for layer in lm.LAYERS}))
        self.assertEqual(pl.render_index(index), again)

    def test_index_command_writes_the_file(self) -> None:
        out = self.dir / "layers.json"
        self.assertEqual(pl.main(["index", "--source", str(self.dist), "--out", str(out)]), 0)
        self.assertEqual(pl.load_index(out), self.index())

    def test_a_layer_that_does_not_add_up_is_refused(self) -> None:
        version = next((self.dist / "tools").iterdir()).name
        summary_path = self.dist / "tools" / version / "summary.json"
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        summary["archive"]["sha256"] = "0" * 64
        summary_path.write_text(json.dumps(summary), encoding="utf-8")
        with self.assertRaisesRegex(pl.PublishError, "sha256"):
            pl.find_built(self.dist, "tools")
        write(self.dist / "tools" / "0123456789abcdef" / "manifest.json", "{}")
        with self.assertRaisesRegex(pl.PublishError, "exactly one"):
            pl.find_built(self.dist, "tools")

    def test_check_index_refuses_a_bad_version(self) -> None:
        index = self.index()
        index["layers"]["tools"]["version"] = "0" * 16
        with self.assertRaisesRegex(pl.PublishError, "version"):
            pl.check_index(index)

    def test_plan_lists_archives_manifests_and_each_model_file_once(self) -> None:
        index = self.index()
        objects = pl.plan_objects(index, self.dist, self.work)
        kinds = [item.kind for item in objects]
        self.assertEqual(kinds.count("archive"), 3)
        self.assertEqual(kinds.count("manifest"), 3)
        models = lm.parse((self.dist / "models" / index["layers"]["models"]["version"] / "manifest.json").read_bytes())
        self.assertEqual(kinds.count("file"), len({item.sha256 for item in models.files}))
        self.assertEqual(kinds.count("file"), 4)  # the two identical files are one object
        self.assertEqual(len({item.key for item in objects}), len(objects))
        for item in objects:
            self.assertEqual(lm.sha256_file(item.source), item.sha256)
            self.assertEqual(item.source.stat().st_size, item.size)
        self.assertIn(f"v1/files/{hashlib.sha256(self.model_bytes).hexdigest()}", {item.key for item in objects})
        self.assertIn("total", pl.describe(objects))

    def test_upload_then_again_changes_nothing_and_verifies_from_outside(self) -> None:
        index = self.index()
        objects = pl.plan_objects(index, self.dist, self.work)
        self.fake.failures[("PUT", "partNumber=2")] = 1  # one transient 500, retried
        outcome = pl.upload_all(self.store, objects)
        self.assertEqual(len(outcome["uploaded"]), len(objects))
        self.assertEqual(outcome["skipped"], [])
        for item in objects:
            stored = self.fake.objects[item.key]
            self.assertEqual(stored["data"], item.source.read_bytes())
            self.assertEqual(stored["meta"]["x-amz-meta-sha256"], item.sha256)
            self.assertEqual(stored["meta"]["cache-control"], pl.CACHE_CONTROL)
            self.assertEqual(stored["meta"]["content-type"],
                             "application/json" if item.kind == "manifest" else "application/octet-stream")
        model_key = f"v1/files/{hashlib.sha256(self.model_bytes).hexdigest()}"
        self.assertIn(("POST", f"{model_key}?uploads"), self.fake.calls)  # 13000 bytes > 4096: in parts
        self.assertEqual(self.fake.uploads, {})

        before = self.fake.snapshot()
        self.fake.calls.clear()
        again = pl.upload_all(self.store, objects)
        self.assertEqual(again["uploaded"], [])
        self.assertEqual(len(again["skipped"]), len(objects))
        self.assertEqual(self.fake.snapshot(), before)
        self.assertEqual(self.fake.writes(), [])
        self.assertEqual({method for method, _ in self.fake.calls}, {"HEAD"})

        with PublicServer(self.fake) as public:
            result = pl.verify(index, DIRECT, self.dir / "verify", base_url=public.base_url)
        self.assertEqual(result.problems, [])
        self.assertEqual(len(result.checked), 4)
        self.assertFalse((self.dir / "verify").exists())

    def test_verify_reports_a_damaged_object(self) -> None:
        index = self.index()
        pl.upload_all(self.store, pl.plan_objects(index, self.dist, self.work))
        archive = self.fake.objects[index["layers"]["tools"]["archive"]["path"]]
        archive["data"] = archive["data"][:-1] + bytes([archive["data"][-1] ^ 1])
        model_key = f"v1/files/{hashlib.sha256(self.model_bytes).hexdigest()}"
        del self.fake.objects[model_key]
        with PublicServer(self.fake) as public:
            result = pl.verify(index, DIRECT, self.dir / "verify", base_url=public.base_url)
        self.assertEqual(len(result.problems), 2, result.problems)
        self.assertTrue(result.problems[0].startswith("tools:"))
        self.assertIn("model.bin", result.problems[1])

    def test_other_content_under_a_name_stops_and_is_not_overwritten(self) -> None:
        index = self.index()
        objects = pl.plan_objects(index, self.dist, self.work)
        target = next(item for item in objects if item.kind == "manifest")
        self.fake.objects[target.key] = {"data": b"something else", "meta": {"x-amz-meta-sha256": "f" * 64}}
        with self.assertRaises(pl.Conflict):
            pl.upload_all(self.store, objects)
        self.assertEqual(self.fake.objects[target.key]["data"], b"something else")
        self.assertNotIn(("PUT", target.key), self.fake.calls)

    def test_an_object_without_a_recorded_digest_is_a_conflict(self) -> None:
        objects = pl.plan_objects(self.index(), self.dist, self.work)
        item = objects[0]
        self.fake.objects[item.key] = {"data": item.source.read_bytes(), "meta": {}}
        with self.assertRaises(pl.Conflict):
            pl.upload_object(self.store, item)

    def test_a_write_that_loses_a_race_is_not_an_overwrite(self) -> None:
        objects = pl.plan_objects(self.index(), self.dist, self.work)
        small = next(item for item in objects if item.size <= 4096)
        large = next(item for item in objects if item.size > 4096)

        def race(method: str, key: str) -> None:
            for item in (small, large):
                if key == item.key and key not in self.fake.objects and method == ("PUT" if item is small else "POST"):
                    self.fake.objects[key] = {"data": item.source.read_bytes(),
                                              "meta": {"x-amz-meta-sha256": item.sha256}}

        self.fake.before_write = race
        self.assertEqual(pl.upload_object(self.store, small), "skipped")
        self.assertEqual(pl.upload_object(self.store, large), "skipped")
        self.assertEqual(self.fake.uploads, {})  # the losing multipart upload is not left behind

        other = objects[-1]

        def clash(method: str, key: str) -> None:
            if key == other.key and key not in self.fake.objects:
                self.fake.objects[key] = {"data": b"x", "meta": {"x-amz-meta-sha256": "e" * 64}}

        self.fake.before_write = clash
        with self.assertRaises(pl.Conflict):
            pl.upload_object(self.store, other)
        self.assertEqual(self.fake.objects[other.key]["data"], b"x")

    def test_a_changed_source_aborts_the_multipart_upload(self) -> None:
        objects = pl.plan_objects(self.index(), self.dist, self.work)
        large = next(item for item in objects if item.size > 4096)
        large.source.write_bytes(os.urandom(large.size))
        with self.assertRaisesRegex(pl.PublishError, "changed"):
            pl.upload_object(self.store, large)
        self.assertNotIn(large.key, self.fake.objects)
        self.assertEqual(self.fake.uploads, {})

    def test_errors_never_echo_the_access_key(self) -> None:
        bad = pl.R2Store(self.client, pl.Credentials("0" * 32, ACCESS_KEY_ID, "wrong"))
        objects = pl.plan_objects(self.index(), self.dist, self.work)
        with self.assertRaises(pl.PublishError) as caught:
            bad.put(objects[1], objects[1].source.read_bytes())
        self.assertIn("SignatureDoesNotMatch", str(caught.exception))
        self.assertNotIn(ACCESS_KEY_ID, str(caught.exception))

    def test_unreferenced_lists_old_objects_and_deletes_nothing(self) -> None:
        index = self.index()
        pl.upload_all(self.store, pl.plan_objects(index, self.dist, self.work))
        old = {"v1/tools/" + "a" * 64 + ".tar.zst": b"old archive", "v1/tools/" + "b" * 64 + ".json": b"{}",
               "v1/files/" + "c" * 64: b"old model file"}
        for key, data in old.items():
            self.fake.objects[key] = {"data": data, "meta": {"x-amz-meta-sha256": hashlib.sha256(data).hexdigest()}}
        before = self.fake.snapshot()
        self.fake.calls.clear()
        left = pl.unreferenced(self.store, [("working tree", index)])
        self.assertEqual({item.key: item.size for item in left}, {key: len(data) for key, data in old.items()})
        self.assertEqual(self.fake.snapshot(), before)
        self.assertEqual(self.fake.writes(), [])
        self.assertGreater(sum(1 for method, key in self.fake.calls if key.startswith("?list-type=2")), 1)  # pages


class PublishedIndexTests(TempCase):
    def git(self, *args: str) -> None:
        subprocess.run(["git", *args], cwd=self.dir, check=True, capture_output=True)

    def test_reads_layers_json_at_each_v_tag(self) -> None:
        def index(version: str) -> dict[str, Any]:
            entry = lambda kind, ext: {"path": f"v1/x/{version * 4}.{ext}", "size": 1, "sha256": version * 4}  # noqa: E731
            return {"schema": 1, "platform": "windows-x64", "baseUrl": "https://layers.nonoka.online/",
                    "layers": {layer: {"version": version, "manifest": entry("m", "json"),
                                       "archive": entry("a", "tar.zst")} for layer in lm.LAYERS},
                    "files": {"layers": [], "path": "v1/files/{sha256}"}}

        self.git("init", "-q")
        self.git("config", "user.email", "t@example.invalid")
        self.git("config", "user.name", "t")
        write(self.dir / "README", "x")
        self.git("add", ".")
        self.git("commit", "-q", "-m", "one")
        self.git("tag", "v0.1.0")  # before layers.json existed
        target = write(self.dir / pl.INDEX_REPO_PATH, json.dumps(index("1" * 16)))
        self.git("add", ".")
        self.git("commit", "-q", "-m", "two")
        self.git("tag", "v0.2.0")
        self.git("tag", "other")  # not a release tag
        target.write_text(json.dumps(index("2" * 16)), encoding="utf-8")
        found = pl.published_indexes(self.dir, target)
        self.assertEqual([(label, item["layers"]["tools"]["version"]) for label, item in found],
                         [("v0.2.0", "1" * 16), ("working tree", "2" * 16)])


if __name__ == "__main__":
    unittest.main()
