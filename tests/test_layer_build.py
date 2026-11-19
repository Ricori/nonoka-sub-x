"""The layer build pipeline and the gate's prebuilt-layer check, without building a real layer (WP-E1).

The real layers are built by `scripts/build_layers.py` on the owner's machine; what is tested here is
the machinery: the manifest format and its checks, the build's helpers (local lock, wheel tags,
markers, RECORD hashes, the build-path scan), the markers it writes for the engine -- compared byte
for byte with what `finesub_bootstrap` itself writes -- an archive round trip through Windows'
`tar.exe`, and the gate's per-layer checks on small made-up layers.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for path in (str(ROOT / "src"), str(ROOT)):
    if path not in sys.path:
        sys.path.insert(0, path)

from nonoka_x.runtime import layer_manifest as lm  # noqa: E402
from nonoka_x.runtime import manifests  # noqa: E402

from scripts import build_layers  # noqa: E402
from tests.quality_harness import layers, pins  # noqa: E402

#: The transcription package: the original `finesub_bootstrap`, whose markers the build reproduces.
VENDOR_SOURCE = ROOT / "engines" / "finesub-transcribe" / "src"
WINDOWS = os.name == "nt"


def write(path: Path, data: bytes | str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data.encode("utf-8") if isinstance(data, str) else data)
    return path


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class TempCase(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="layer-test-"))
        self.addCleanup(shutil.rmtree, self.dir, True)


class ManifestFormatTests(TempCase):
    def tree(self) -> Path:
        root = self.dir / "layer"
        write(root / "a.txt", "alpha")
        write(root / "sub" / "b.bin", b"\x00\x01")
        (root / "empty" / "deeper").mkdir(parents=True)
        return root

    def test_render_is_canonical_and_the_version_is_its_digest_prefix(self) -> None:
        files, empty = lm.scan(self.tree())
        first = lm.LayerManifest("tools", list(reversed(files)), empty_dirs=empty, sources={"b": 1, "a": [2]})
        second = lm.LayerManifest("tools", files, empty_dirs=list(reversed(empty)), sources={"a": [2], "b": 1})
        self.assertEqual(lm.render(first), lm.render(second))
        data = lm.render(first)
        self.assertTrue(data.endswith(b"\n") and b"\r" not in data)
        self.assertEqual(lm.version_of(data), hashlib.sha256(data).hexdigest()[:16])
        self.assertEqual(lm.parse(data).to_dict(), first.to_dict())
        self.assertEqual(empty, ["empty/deeper"])
        self.assertEqual([item.path for item in files], ["a.txt", "sub/b.bin"])

    def test_paths_must_be_relative_posix(self) -> None:
        for bad in ("", "/a", "a\\b", "../a", "a/../b", "a//b", "C:/x", "./a"):
            with self.subTest(bad):
                with self.assertRaises(lm.LayerError):
                    lm.check_path(bad)
        self.assertEqual(lm.check_path("Lib/site-packages/x.py"), "Lib/site-packages/x.py")

    def test_compare_finds_every_departure(self) -> None:
        root = self.tree()
        files, empty = lm.scan(root)
        manifest = lm.LayerManifest("tools", files, empty_dirs=empty)
        write(root / lm.MANIFEST_NAME, lm.render(manifest))
        self.assertEqual(lm.compare(manifest, root, full=True), [])
        write(root / "a.txt", "ALPHA")
        self.assertEqual(lm.compare(manifest, root, full=False), [])
        self.assertEqual(lm.compare(manifest, root, full=True), ["sha256 differs: a.txt"])
        write(root / "a.txt", "alpha!")
        write(root / "new.txt", "x")
        (root / "sub" / "b.bin").unlink()
        (root / "empty" / "deeper").rmdir()
        problems = lm.compare(manifest, root, full=True)
        self.assertIn("missing: sub/b.bin", problems)
        self.assertIn("not in the manifest: new.txt", problems)
        self.assertIn("size differs: a.txt (6, manifest 5)", problems)
        self.assertIn("empty directory missing: empty/deeper", problems)
        self.assertIn("empty directory not in the manifest: empty", problems)
        self.assertIn("empty directory not in the manifest: sub", problems)

    @unittest.skipUnless(WINDOWS, "junctions are Windows'")
    def test_links_are_refused(self) -> None:
        import _winapi

        root = self.tree()
        _winapi.CreateJunction(str(root / "sub"), str(root / "link"))
        with self.assertRaises(lm.LayerError):
            lm.scan(root)
        manifest = lm.LayerManifest("tools", [])
        self.assertIn("link (layers hold only regular files): link", lm.compare(manifest, root, full=False))

    def test_differences_between_two_builds(self) -> None:
        a = lm.LayerManifest("tools", [lm.LayerFile("x", 1, "0" * 64), lm.LayerFile("y", 1, "1" * 64)])
        b = lm.LayerManifest("tools", [lm.LayerFile("x", 1, "f" * 64), lm.LayerFile("z", 1, "1" * 64)],
                             sources={"k": 1})
        self.assertEqual(lm.differences(a, b), ["sources differs", "only in the first: y", "only in the second: z",
                                                "content differs: x"])


class BuildHelperTests(TempCase):
    def test_inputs_agree_with_the_pins(self) -> None:
        recorded = pins.with_constants(pins.derive())
        self.assertEqual(build_layers.check_inputs(recorded), [])
        changed = copy.deepcopy(recorded)
        changed["interpreter"]["sha256"] = "0" * 64
        changed["models"]["whisper"]["revision"] = "1" * 40
        changed["python"]["global"] = [item for item in changed["python"]["global"] if item["name"] != "torch"]
        problems = build_layers.check_inputs(changed)
        self.assertIn("interpreter differs from RUNTIME_PINS.json", problems)
        self.assertIn("model differs from RUNTIME_PINS.json: whisper", problems)
        self.assertIn("package not pinned as locked: torch 2.11.0+cu128", problems)

    def test_the_model_layer_holds_the_default_path_models(self) -> None:
        self.assertEqual(lm.MODEL_LAYER_IDS, ("separator", "whisper", "qwen-referee", "nemotron3", "speaker-embedding"))
        self.assertEqual(lm.MOSS_MODEL_LAYER_IDS, ("moss-transcribe-diarize", "speaker-embedding"))
        models = manifests.model_manifest()["models"]
        self.assertTrue(set(lm.MODEL_LAYER_IDS) <= set(models))
        self.assertTrue(set(lm.MOSS_MODEL_LAYER_IDS) <= set(models))
        trimmed = lm.layer_model_spec("models-moss", "speaker-embedding", models["speaker-embedding"])
        self.assertTrue(trimmed.get("listedOnly"))
        self.assertEqual(
            [item["name"] for item in trimmed["files"]],
            ["wespeaker-cpu.ts", "plda/plda.npz", "plda/xvec_transform.npz"],
        )
        sys.path.insert(0, str(VENDOR_SOURCE))
        from finesub_bootstrap import model_caches

        self.assertTrue(set(model_caches.PIPELINE_MODEL_IDS) <= set(lm.MODEL_LAYER_IDS))
        self.assertEqual(model_caches.managed_model_dirs(Path("m")),
                         (Path("m") / lm.HF_HOME_DIR, Path("m") / lm.SEPARATOR_DIR))

    def test_the_engine_unverified_files_are_the_pins_moving_ones(self) -> None:
        self.assertEqual({("separator", name) for name in lm.ENGINE_UNVERIFIED}, set(pins.BOOTSTRAP_MOVING))

    def test_record_hash_and_wheel_tags(self) -> None:
        digest = sha(b"hello")
        self.assertEqual(build_layers.record_hash(digest), "sha256=LPJNul-wow4m6DsqxbninhsWHlwfp0JecwQzYpOLmCQ")
        self.assertEqual(build_layers._expand_tags("x-1.0-py2.py3-none-any.whl"), {"py2-none-any", "py3-none-any"})
        self.assertEqual(build_layers._expand_tags("torch-2.11.0%2Bcu128-cp312-cp312-win_amd64.whl"),
                         {"cp312-cp312-win_amd64"})
        self.assertEqual(build_layers.wheel_filename("https://h/p/torch-2.11.0%2Bcu128-cp312-cp312-win_amd64.whl"),
                         "torch-2.11.0+cu128-cp312-cp312-win_amd64.whl")
        self.assertEqual(build_layers.compiled_source("Lib/__pycache__/os.cpython-312.pyc"), "Lib/os.py")
        self.assertEqual(build_layers.compiled_source("__pycache__/x.cpython-312.opt-1.pyc"), "x.py")
        self.assertIsNone(build_layers.compiled_source("Lib/os.pyc"))
        self.assertIsNone(build_layers.compiled_source("__pycache__/x.cpython-311.pyc"))

    def test_markers_of_the_lock_hold_on_windows(self) -> None:
        for package in manifests.runtime_lock()["packages"]:
            with self.subTest(package["name"]):
                self.assertTrue(build_layers.marker_applies(package.get("marker")))
        self.assertFalse(build_layers.marker_applies("sys_platform == 'linux'"))
        self.assertTrue(build_layers.marker_applies("sys_platform == 'linux' or os_name == 'nt'"))
        self.assertFalse(build_layers.marker_applies("(sys_platform == 'win32') and implementation_name != 'cpython'"))
        with self.assertRaises(build_layers.BuildError):
            build_layers.marker_applies("python_version >= '3.12'")
        with self.assertRaises(build_layers.BuildError):
            build_layers.marker_applies("extra == 'x'")

    def test_the_local_lock_keeps_names_versions_markers_and_hashes(self) -> None:
        lock = manifests.runtime_lock()
        local = {artefact["url"]: self.dir / "wheels" / build_layers.wheel_filename(artefact["url"])
                 for _package, artefact in build_layers.lock_artefacts(lock)}
        rewritten = tomllib.loads(build_layers.local_lock(lock, local))
        self.assertEqual(rewritten["lock-version"], lock["lock-version"])
        self.assertEqual(rewritten["requires-python"], lock["requires-python"])
        self.assertEqual(len(rewritten["packages"]), len(lock["packages"]))
        for original, copied in zip(lock["packages"], rewritten["packages"]):
            with self.subTest(original["name"]):
                self.assertEqual((copied["name"], copied["version"], copied.get("marker")),
                                 (original["name"], original["version"], original.get("marker")))
                self.assertNotIn("sdist", copied)
                expected = [(local[item["url"]].as_posix(), item["hashes"]) for item in original.get("wheels", [])]
                self.assertEqual([(item["path"], item["hashes"]) for item in copied.get("wheels", [])], expected)
                if "archive" in original:
                    self.assertEqual(copied["archive"], {"path": local[original["archive"]["url"]].as_posix(),
                                                         "hashes": original["archive"]["hashes"]})

    def test_files_of_the_interpreters_own_packages_are_known(self) -> None:
        root = self.dir / "python"
        site = root / "Lib" / "site-packages"
        write(site / "pip" / "__init__.py", "")
        write(root / "Scripts" / "pip.exe", b"x")
        write(site / "pip-23.3.2.dist-info" / "RECORD",
              "pip/__init__.py,sha256=x,0\n../../Scripts/pip.exe,sha256=y,1\npip-23.3.2.dist-info/RECORD,,\n")
        self.assertEqual(build_layers.interpreter_packages(root), {
            "Lib/site-packages/pip/__init__.py": "pip-23.3.2.dist-info",
            "Scripts/pip.exe": "pip-23.3.2.dist-info",
            "Lib/site-packages/pip-23.3.2.dist-info/RECORD": "pip-23.3.2.dist-info",
        })

    @unittest.skipUnless(WINDOWS, "junctions are Windows'")
    def test_removing_a_staged_root_leaves_the_layers_alone(self) -> None:
        import _winapi

        layer = write(self.dir / "layer" / "keep.txt", "k").parent
        staged = self.dir / "install"
        (staged / "runtime" / "tok").mkdir(parents=True)
        _winapi.CreateJunction(str(layer), str(staged / "models"))
        _winapi.CreateJunction(str(layer), str(staged / "runtime" / "tok" / "1"))
        write(staged / "runtime" / "tok" / "current.json", "{}")
        build_layers.remove_staged(staged)
        self.assertFalse(staged.exists())
        self.assertEqual((layer / "keep.txt").read_text(), "k")

    def test_the_build_path_may_appear_only_in_the_launchers(self) -> None:
        root = self.dir / "work" / "python"
        builder = build_layers.Build(self.dir, self.dir / "out")
        write(root / "Scripts" / "tool.exe", b"MZ..." + str(root / "python.exe").encode("utf-8") + b"...")
        write(root / "Scripts" / "numba", f"#!{root}\\python.exe\nimport numba\n")
        write(root / "Lib" / "ok.py", "x = 1\n")
        found = builder.absolute_path_files(root)
        self.assertEqual([item["path"] for item in found], ["Scripts/numba", "Scripts/tool.exe"])
        write(root / "Lib" / "site-packages" / "bad.pth", str(root).replace("\\", "/").lower().encode("utf-16-le"))
        with self.assertRaises(build_layers.BuildError):
            builder.absolute_path_files(root)


class EngineMarkerTests(TempCase):
    """What the build writes for the engine equals what `finesub_bootstrap` writes itself."""

    @classmethod
    def setUpClass(cls) -> None:
        if str(VENDOR_SOURCE) not in sys.path:
            sys.path.insert(0, str(VENDOR_SOURCE))

    def test_hf_marker_digest_and_bytes(self) -> None:
        from finesub_bootstrap import hf_verify, model_manifest

        engine = model_manifest.load_manifest()
        nonoka = manifests.model_manifest()["models"]
        for model_id in lm.MODEL_LAYER_IDS:
            spec = nonoka[model_id]
            if not spec.get("repo"):
                continue
            with self.subTest(model_id):
                entry = engine[model_id]
                self.assertEqual(build_layers.engine_entry_digest(spec), hf_verify.entry_digest(entry))
                cache_dir = "models--" + spec["repo"].replace("/", "--")
                (self.dir / cache_dir).mkdir(exist_ok=True)
                hf_verify.write_marker(self.dir, cache_dir, entry)
                expected = json.dumps({"manifest": build_layers.engine_entry_digest(spec), "repo": spec["repo"],
                                       "revision": spec["revision"]}, ensure_ascii=False).encode("utf-8")
                self.assertEqual(hf_verify.marker_path(self.dir, cache_dir).read_bytes(), expected)
                self.assertEqual(hf_verify.marker_path(self.dir, cache_dir).name, lm.HF_MARKER_NAME)

    def test_separator_stamps(self) -> None:
        from finesub_bootstrap import model_fetch, model_manifest

        entry = model_manifest.load_manifest()["separator"]
        builder = build_layers.Build(self.dir, self.dir / "out")
        spec = copy.deepcopy(manifests.model_manifest()["models"]["separator"])
        engine_files = {item.name: item for item in entry.files}
        for item in spec["files"]:
            data = b"x" * 10
            item["url"], item["size"], item["sha256"] = "https://example.invalid/" + item["name"], len(data), sha(data)
            write(self.dir / "cache" / "models" / "separator" / item["name"], data)
        original = build_layers.download
        build_layers.download = lambda url, destination, sha256, size=None: destination
        try:
            builder.fixed_model("separator", spec, self.dir / "layer")
        finally:
            build_layers.download = original
        directory = self.dir / "layer" / lm.SEPARATOR_DIR
        build_layers.set_fixed_times(directory)
        for item in spec["files"]:
            with self.subTest(item["name"]):
                stamp = directory / (item["name"] + lm.VERIFIED_SUFFIX)
                if not engine_files[item["name"]].is_verifiable:
                    self.assertIn(item["name"], lm.ENGINE_UNVERIFIED)
                    self.assertFalse(stamp.exists())
                    continue
                ours = stamp.read_bytes()
                stamp.unlink()
                wanted = model_manifest.ManifestFile(name=item["name"], url=item["url"], size=item["size"],
                                                     sha256=item["sha256"])
                model_fetch._write_verified_stamp(directory / item["name"], wanted)
                self.assertEqual(stamp.read_bytes(), ours)
                self.assertTrue(model_fetch._stamp_still_describes(directory / item["name"], wanted))


@unittest.skipUnless(WINDOWS and build_layers.SYSTEM_TAR.is_file(), "needs Windows' tar.exe")
class ArchiveRoundTripTests(TempCase):
    def test_finish_packs_a_layer_that_unpacks_to_itself(self) -> None:
        builder = build_layers.Build(self.dir / "build", self.dir / "out")
        builder.prepare()
        root = builder.work / "tools"
        write(root / "ffmpeg" / "bin" / "ffmpeg.exe", b"\x00" * 1000)
        write(root / "tokcount" / "tokcount.exe", b"tok")
        (root / "node" / "empty").mkdir(parents=True)
        summary = builder.finish("tools", root, {"sources": {"tools": []}, "timings": {}}, [])
        destination = builder.out / "tools" / summary["version"]
        data = (destination / "manifest.json").read_bytes()
        self.assertEqual(lm.version_of(data), summary["version"])
        self.assertEqual(summary["files"], 2)
        unpacked = self.dir / "unpacked with space"
        unpacked.mkdir()
        build_layers.run(builder, [str(build_layers.SYSTEM_TAR), "-x", "-f", str(destination / "layer.tar.zst"),
                                   "-C", str(unpacked)], log_name="t.log")
        manifest, carried = lm.manifest_in(unpacked)
        self.assertEqual(carried, data)
        self.assertEqual(lm.compare(manifest, unpacked, full=True), [])
        self.assertEqual(os.stat(unpacked / "tokcount" / "tokcount.exe").st_mtime_ns, lm.FIXED_MTIME * 10**9)
        # The same tree packed again: the same manifest.
        again = builder.finish("tools", root, {"sources": {"tools": []}, "timings": {}}, [])
        self.assertEqual(again["version"], summary["version"])


class GateLayerCheckTests(TempCase):
    """`tests/quality_harness/layers.py` on small made-up layers."""

    def recorded(self) -> dict:
        return {
            "interpreter": {"build": "b", "version": "3.12.1", "url": "https://x/p.tar.gz", "size": 1, "sha256": "a" * 64},
            "python": {"global": [
                {"name": "demo", "version": "1.0", "wheels": [{"url": "https://x/demo-1.0-py3-none-any.whl",
                                                                  "sha256": "b" * 64}]},
                {"name": "linux-only", "version": "2.0", "marker": "sys_platform == 'linux'", "wheels": []},
            ]},
            "tools": {"resources": [
                {"id": "uv", "version": "1", "url": "https://x/uv.zip", "size": 1, "sha256": "c" * 64, "required_files": []},
                {"id": "tok", "version": "2", "url": "https://x/tok.zip", "size": 2, "sha256": "d" * 64,
                 "required_files": ["tok.exe"]},
            ]},
        }

    def python_layer(self) -> tuple[Path, lm.LayerManifest]:
        root = self.dir / "python"
        write(root / "python.exe", b"exe")
        write(root / "Lib" / "os.py", "x = 1\n")
        write(root / "Lib" / "__pycache__" / "os.cpython-312.pyc", b"pyc")
        site = root / "Lib" / "site-packages"
        module = write(site / "demo" / "__init__.py", "y = 2\n")
        launcher = write(root / "Scripts" / "demo.exe", b"launcher")
        dist = site / "demo-1.0.dist-info"
        write(dist / "METADATA", "Metadata-Version: 2.1\nName: demo\nVersion: 1.0\n")
        rows = [f"demo/__init__.py,{build_layers.record_hash(sha(module.read_bytes()))},{module.stat().st_size}",
                f"../../Scripts/demo.exe,{build_layers.record_hash(sha(launcher.read_bytes()))},{launcher.stat().st_size}",
                f"demo-1.0.dist-info/METADATA,{build_layers.record_hash(sha((dist / 'METADATA').read_bytes()))},",
                "demo-1.0.dist-info/RECORD,,"]
        write(dist / "RECORD", "\n".join(rows) + "\n")
        files, empty = lm.scan(root)
        members = [{"path": "python.exe", "size": 3, "sha256": sha(b"exe")},
                   {"path": "Lib/os.py", "size": 6, "sha256": sha(b"x = 1\n")}]
        manifest = lm.LayerManifest("python", files, empty_dirs=empty,
                                    exceptions=[{"path": "Scripts/demo.exe", "reason": "launcher"}],
                                    sources={"interpreter": {**self.recorded()["interpreter"], "files": members},
                                             "packages": [{"name": "demo", "version": "1.0", "wheel": "demo-1.0-py3-none-any.whl",
                                                           "url": "https://x/demo-1.0-py3-none-any.whl", "sha256": "b" * 64}]})
        write(root / lm.MANIFEST_NAME, lm.render(manifest))
        return root, manifest

    def test_a_good_python_layer_passes(self) -> None:
        root, manifest = self.python_layer()
        self.assertEqual(layers.check_python(manifest, root, self.recorded()), [])
        self.assertEqual(layers.check_python(manifest, None, self.recorded()), [])

    def test_python_layer_departures(self) -> None:
        root, manifest = self.python_layer()
        write(root / "Lib" / "site-packages" / "demo" / "__init__.py", "y = 3\n")
        write(root / "Lib" / "site-packages" / "stray.py", "z\n")
        write(root / "Lib" / "__pycache__" / "gone.cpython-312.pyc", b"pyc")
        manifest = lm.LayerManifest("python", lm.scan(root)[0], exceptions=manifest.exceptions + [
            {"path": "Lib/os.py", "reason": "?"}], sources=copy.deepcopy(manifest.sources))
        manifest.sources["packages"][0]["sha256"] = "e" * 64
        problems = layers.check_python(manifest, root, self.recorded())
        self.assertIn("demo-1.0.dist-info: demo/__init__.py differs from RECORD", problems)
        self.assertIn("file of no package and not the interpreter's: Lib/site-packages/stray.py", problems)
        self.assertIn("bytecode with no source in the layer: Lib/__pycache__/gone.cpython-312.pyc", problems)
        self.assertIn("exception not allowed (4.2 item 17): Lib/os.py", problems)
        self.assertTrue(any("not a locked artefact" in line for line in problems))
        recorded = self.recorded()
        recorded["interpreter"]["sha256"] = "f" * 64
        recorded["python"]["global"].append({"name": "more", "version": "1", "wheels": []})
        problems = layers.check_python(manifest, None, recorded)
        self.assertIn("locked package not in the layer: more", problems)
        self.assertTrue(any(line.startswith("interpreter sha256") for line in problems))

    def test_tool_layer(self) -> None:
        root = self.dir / "tools"
        write(root / "tok" / "tok.exe", b"t")
        manifest = lm.LayerManifest("tools", lm.scan(root)[0], sources={"tools": [
            {"id": "tok", "version": "2", "directory": "tok", "url": "https://x/tok.zip", "size": 2, "sha256": "d" * 64,
             "required_files": ["tok.exe"], "files": 1}]})
        self.assertEqual(layers.check_tools(manifest, root, self.recorded()), [])
        manifest.sources["tools"][0]["sha256"] = "0" * 64
        manifest.files.append(lm.LayerFile("other/x", 1, "0" * 64))
        problems = layers.check_tools(manifest, root, self.recorded())
        self.assertIn(f"tok: sha256 {'0' * 64!r}, pinned {'d' * 64!r}", problems)
        self.assertIn("file of no tool: other/x", problems)

    def test_model_layer(self) -> None:
        recorded = pins.with_constants(pins.derive())
        root = self.dir / "models"
        sources = []
        for model_id in lm.MODEL_LAYER_IDS:
            pinned = recorded["models"][model_id]
            files = []
            if pinned.get("repo"):
                cache_dir = "models--" + pinned["repo"].replace("/", "--")
                base = root / lm.HF_HOME_DIR / "hub" / cache_dir
                write(base / lm.HF_MARKER_NAME, json.dumps({"manifest": build_layers.engine_entry_digest(pinned),
                                                            "repo": pinned["repo"], "revision": pinned["revision"]},
                                                           ensure_ascii=False))
                base = base / "snapshots" / pinned["revision"]
            else:
                base = root / lm.SEPARATOR_DIR
            for item in pinned["files"]:
                write(base / item["name"], b"")
                files.append({"name": item["name"], "size": item["size"], "sha256": item["sha256"]})
                if not pinned.get("repo") and item["name"] not in lm.ENGINE_UNVERIFIED:
                    write(base / (item["name"] + lm.VERIFIED_SUFFIX), json.dumps(
                        {"sha256": item["sha256"], "size": item["size"], "mtime_ns": lm.FIXED_MTIME * 10**9}))
            sources.append({"id": model_id, **{key: pinned[key] for key in ("repo", "revision", "listedOnly") if key in pinned},
                            "files": files})
        build_layers.set_fixed_times(root)
        scanned = {item.path: item for item in lm.scan(root)[0]}
        # The made-up files are empty; state the pinned sizes and digests so only the content is fake.
        for source in sources:
            pinned = recorded["models"][source["id"]]
            if pinned.get("repo"):
                prefix = f"{lm.HF_HOME_DIR}/hub/models--{pinned['repo'].replace('/', '--')}/snapshots/{pinned['revision']}/"
            else:
                prefix = f"{lm.SEPARATOR_DIR}/"
            for item in source["files"]:
                scanned[prefix + item["name"]] = lm.LayerFile(prefix + item["name"], item["size"], item["sha256"])
        manifest = lm.LayerManifest("models", list(scanned.values()), sources={"models": sources})
        self.assertEqual(layers.check_models(manifest, root, recorded), [])
        manifest.files.append(lm.LayerFile("audio-separator/accel/x/probe.json", 1, "0" * 64))
        sources[1]["revision"] = "0" * 40
        problems = layers.check_models(manifest, root, recorded)
        self.assertIn("not allowed in the model layer: audio-separator/accel/x/probe.json", problems)
        self.assertTrue(any(line.startswith("whisper: revision") for line in problems))
        stamped = root / lm.SEPARATOR_DIR / recorded["models"]["separator"]["files"][0]["name"]
        os.utime(stamped, (0, 0))
        self.assertTrue(any("modification time lost" in line for line in layers.check_models(manifest, root, recorded)))


if __name__ == "__main__":
    unittest.main()
