import { createHash } from "node:crypto";
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { createServer } from "node:http";
import { tmpdir } from "node:os";
import path from "node:path";
import { after, before, test } from "node:test";
import assert from "node:assert/strict";
import { buildOfflinePack } from "./build-offline-package.mjs";
import { layerObjects, readLayersIndex } from "./resource-sources.mjs";

const sha256 = data => createHash("sha256").update(data).digest("hex");
const root = mkdtempSync(path.join(tmpdir(), "nonoka-offline-pack-"));
const dist = path.join(root, "dist");
const remote = path.join(root, "remote");
const requests = [];
let server;
let baseUrl;

/** A small dist\layers like scripts/build_layers.py leaves it, and the same objects under R2's names. */
function writeLayers() {
  const layers = {};
  for (const layer of ["tools", "python", "models"]) {
    const manifest = Buffer.from(`{"layer": "${layer}"}\n`);
    const archive = Buffer.from(`${layer} archive bytes `.repeat(64));
    const version = sha256(manifest).slice(0, 16);
    const directory = path.join(dist, layer, version);
    mkdirSync(directory, { recursive: true });
    writeFileSync(path.join(directory, "manifest.json"), manifest);
    writeFileSync(path.join(directory, "layer.tar.zst"), archive);
    const entry = {
      version,
      manifest: { path: `v1/${layer}/${sha256(manifest)}.json`, size: manifest.length, sha256: sha256(manifest) },
      archive: { path: `v1/${layer}/${sha256(archive)}.tar.zst`, size: archive.length, sha256: sha256(archive) },
    };
    for (const [object, data] of [[entry.manifest, manifest], [entry.archive, archive]]) {
      mkdirSync(path.dirname(path.join(remote, object.path)), { recursive: true });
      writeFileSync(path.join(remote, object.path), data);
    }
    layers[layer] = entry;
  }
  return layers;
}

function writeIndex(name, layers, url = baseUrl) {
  const file = path.join(root, name);
  writeFileSync(file, JSON.stringify({ schema: 1, platform: "windows-x64", baseUrl: url, layers, files: { layers: [], path: "v1/files/{sha256}" } }));
  return file;
}

const quiet = () => {};
let layers;

before(async () => {
  layers = writeLayers();
  server = createServer((request, response) => {
    requests.push(request.url);
    const file = path.join(remote, decodeURIComponent(new URL(request.url, "http://x").pathname));
    if (!existsSync(file)) {
      response.writeHead(404).end();
      return;
    }
    response.writeHead(200).end(readFileSync(file));
  });
  await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
  baseUrl = `http://127.0.0.1:${server.address().port}/`;
});

after(() => {
  server.close();
  rmSync(root, { recursive: true, force: true });
});

test("the layers land under the index's paths, copied from a local build", async () => {
  const out = path.join(root, "from-dist");
  const index = readLayersIndex(writeIndex("local.json", layers));
  requests.length = 0;
  const result = await buildOfflinePack({ out, index, layersFrom: dist, launcher: false, log: quiet });
  assert.equal(result.placed, 6);
  assert.deepEqual(requests, [], "a local build needs no network");
  for (const object of layerObjects(index)) {
    const placed = readFileSync(path.join(out, "offline-pack", object.path));
    assert.equal(sha256(placed), object.sha256);
  }
  const pack = JSON.parse(readFileSync(path.join(out, "offline-pack", "pack.json"), "utf8"));
  assert.deepEqual(pack.layers, { tools: layers.tools.version, python: layers.python.version, models: layers.models.version });
  assert.equal(pack.launcher, false);
});

test("the layers are downloaded from baseUrl and come out byte for byte the same", async () => {
  const out = path.join(root, "from-remote");
  const index = readLayersIndex(writeIndex("remote.json", layers));
  requests.length = 0;
  await buildOfflinePack({ out, index, launcher: false, log: quiet });
  assert.equal(requests.length, 6);
  for (const object of layerObjects(index)) {
    assert.deepEqual(readFileSync(path.join(out, "offline-pack", object.path)), readFileSync(path.join(remote, object.path)));
  }
  // A second run keeps what is already right.
  requests.length = 0;
  await buildOfflinePack({ out, index, launcher: false, log: quiet });
  assert.deepEqual(requests, []);
});

test("a file that does not match the index is refused and not placed", async () => {
  const out = path.join(root, "tampered");
  const tampered = structuredClone(layers);
  tampered.python.archive.sha256 = "0".repeat(64);
  const index = readLayersIndex(writeIndex("tampered.json", tampered));
  await assert.rejects(buildOfflinePack({ out, index, layersFrom: dist, launcher: false, log: quiet }), /sha256/);
  assert.equal(existsSync(path.join(out, "offline-pack", tampered.python.archive.path)), false);
  assert.equal(existsSync(path.join(out, "offline-pack", `${tampered.python.archive.path}.part`)), false);
});

test("--since leaves out what the older package already has", async () => {
  const older = path.join(root, "from-dist", "offline-pack");
  const out = path.join(root, "delta");
  const index = readLayersIndex(writeIndex("delta.json", layers));
  const result = await buildOfflinePack({ out, index, layersFrom: dist, since: older, launcher: false, log: quiet });
  assert.equal(result.skipped, 6);
  assert.equal(result.placed, 0);
  assert.equal(JSON.parse(readFileSync(path.join(out, "offline-pack", "pack.json"), "utf8")).delta, true);
});

test("the index is checked the way the layer manager checks it", () => {
  const wrongVersion = structuredClone(layers);
  wrongVersion.models.version = "0123456789abcdef";
  assert.throws(() => readLayersIndex(writeIndex("wrong-version.json", wrongVersion)), /version/);
  const escaping = structuredClone(layers);
  escaping.tools.archive.path = "../outside.tar.zst";
  assert.throws(() => readLayersIndex(writeIndex("escaping.json", escaping)), /relative path/);
  const missing = structuredClone(layers);
  delete missing.models;
  assert.throws(() => readLayersIndex(writeIndex("missing.json", missing)), /no models layer/);
});

test("the launcher's interpreter needs uv", async () => {
  const index = readLayersIndex(writeIndex("launcher.json", layers));
  await assert.rejects(buildOfflinePack({ out: path.join(root, "launcher"), index, layersFrom: dist, log: quiet }), /--uv/);
});

test("the shipped index reads", () => {
  const index = readLayersIndex();
  assert.deepEqual(Object.keys(index.layers), ["tools", "python", "models"]);
  assert.match(index.baseUrl, /^https:\/\//);
});
