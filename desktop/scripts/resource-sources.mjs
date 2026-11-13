// What the installers fetch, read from the files that decide it: the prebuilt
// layers this release wants (src/nonoka_x/resources/layers.json) and what the
// launcher bootstrap downloads before there is a sidecar to install them
// (desktop/internal/app/python_bootstrap.go). Used by build-offline-package.mjs.
import { execFileSync } from "node:child_process";
import { createHash } from "node:crypto";
import { createReadStream, createWriteStream, readFileSync } from "node:fs";
import path from "node:path";
import { Readable } from "node:stream";
import { pipeline } from "node:stream/promises";
import { fileURLToPath } from "node:url";

const scriptsDir = path.dirname(fileURLToPath(import.meta.url));
export const desktop = path.dirname(scriptsDir);
export const repository = path.dirname(desktop);
export const layersIndexFile = path.join(repository, "src", "nonoka_x", "resources", "layers.json");
const bootstrapSource = path.join(desktop, "internal", "app", "python_bootstrap.go");

export const PYTHON_RELEASES = "https://github.com/astral-sh/python-build-standalone/releases/download/";
const ASTRAL_RELEASES = "https://releases.astral.sh/github/python-build-standalone/releases/download/";

/** The layer kinds, in the order the layer manager installs them. */
export const LAYERS = ["tools", "python", "models"];

const SHA256 = /^[0-9a-f]{64}$/;

function readJson(file) {
  return JSON.parse(readFileSync(file, "utf8"));
}

function checkObject(where, object) {
  if (!object || typeof object.path !== "string" || !Number.isSafeInteger(object.size) || object.size < 0 || !SHA256.test(object.sha256 ?? "")) {
    throw new Error(`${where}: needs path, size and sha256`);
  }
  const parts = object.path.split("/");
  if (object.path.startsWith("/") || object.path.includes("\\") || parts.some(part => part === "" || part === "." || part === "..")) {
    throw new Error(`${where}: ${object.path} is not a plain relative path`);
  }
  return { path: object.path, size: object.size, sha256: object.sha256 };
}

/**
 * The layer index (plan WP-E2), checked the way the layer manager reads it
 * (`nonoka_x.runtime.layers.parse_index`): each layer's version is the first 16
 * hex digits of its manifest's sha256, and every object names a plain relative
 * path, a size and a sha256.
 */
export function readLayersIndex(file = layersIndexFile) {
  const index = readJson(file);
  if (index.schema !== 1) throw new Error(`${file}: unknown schema ${index.schema}`);
  if (typeof index.baseUrl !== "string" || !/^https?:\/\/.+\/$/.test(index.baseUrl)) {
    throw new Error(`${file}: baseUrl must be an http(s) URL ending in /`);
  }
  if ((index.files?.layers ?? []).length) {
    throw new Error(`${file}: files.layers lists single files, which the offline package does not carry`);
  }
  const layers = {};
  for (const layer of LAYERS) {
    const entry = index.layers?.[layer];
    if (!entry) throw new Error(`${file}: no ${layer} layer`);
    const manifest = checkObject(`${layer}.manifest`, entry.manifest);
    const archive = checkObject(`${layer}.archive`, entry.archive);
    if (entry.version !== manifest.sha256.slice(0, 16)) {
      throw new Error(`${file}: ${layer} version ${entry.version} is not its manifest's sha256 prefix`);
    }
    layers[layer] = { version: entry.version, manifest, archive };
  }
  return { file, platform: index.platform, baseUrl: index.baseUrl, layers };
}

/** Every object of the index: the layer, which object it is, and where it lives under baseUrl. */
export function layerObjects(index) {
  return LAYERS.flatMap(layer => ["manifest", "archive"].map(kind => ({
    layer,
    kind,
    version: index.layers[layer].version,
    ...index.layers[layer][kind],
    url: new URL(index.layers[layer][kind].path, index.baseUrl).href,
  })));
}

export function bootstrapVersion() {
  const match = /bootstrapUVVersion\s*=\s*"([^"]+)"/.exec(readFileSync(bootstrapSource, "utf8"));
  if (!match) throw new Error("bootstrapUVVersion not found in python_bootstrap.go");
  return match[1];
}

export function bootstrapAssets() {
  const body = readFileSync(bootstrapSource, "utf8");
  const release = `https://github.com/astral-sh/uv/releases/download/${bootstrapVersion()}/`;
  const found = /Name:\s*"([^"]+)",\s*Size:\s*(\d+),\s*SHA256:\s*"([0-9a-f]{64})"/g;
  return [...body.matchAll(found)].map(match => ({ url: release + match[1], size: Number(match[2]), sha256: match[3] }));
}

/** The GitHub URLs of the interpreters the pinned uv downloads, for "<os>/<arch>" platforms. */
export function pythonBuilds(uv, platforms) {
  const version = bootstrapVersion();
  const reported = execFileSync(uv, ["--version"], { encoding: "utf8" });
  if (!reported.includes(` ${version} `) && !reported.trim().endsWith(` ${version}`)) {
    throw new Error(`--uv is ${reported.trim()}, the launcher pins ${version}`);
  }
  const listing = JSON.parse(execFileSync(
    uv,
    ["python", "list", "3.12", "--only-downloads", "--all-platforms", "--all-arches", "--output-format", "json"],
    { encoding: "utf8", maxBuffer: 16 * 1024 * 1024 },
  ));
  const wanted = new Set(platforms);
  const latest = new Map();
  for (const entry of listing) {
    const key = `${entry.os}/${entry.arch}`;
    if (entry.implementation === "cpython" && entry.variant === "default" && entry.libc === "none" && wanted.has(key) && !latest.has(key)) {
      latest.set(key, entry.url);
    }
  }
  if (latest.size !== wanted.size) throw new Error(`uv listed interpreters for only: ${[...latest.keys()].join(", ")}`);
  return [...latest.values()].map(url => {
    if (!url.startsWith(ASTRAL_RELEASES) && !url.startsWith(PYTHON_RELEASES)) throw new Error(`Unexpected interpreter URL: ${url}`);
    return { url: url.replace(ASTRAL_RELEASES, PYTHON_RELEASES) };
  });
}

/**
 * Downloads url to destination. `Accept-Encoding: identity` because R2 gzips
 * JSON objects for a client that accepts it (docs/runtime-layers.md 2.3): the
 * bytes on disk must be the object's own.
 */
export async function download(url, destination) {
  const response = await fetch(url, {
    redirect: "follow",
    headers: { "User-Agent": "nonoka-release-tools", "Accept-Encoding": "identity" },
  });
  if (!response.ok || !response.body) throw new Error(`${url} answered ${response.status}`);
  await pipeline(Readable.fromWeb(response.body), createWriteStream(destination));
}

export async function sha256Of(file) {
  const digest = createHash("sha256");
  await pipeline(createReadStream(file), digest);
  return digest.digest("hex");
}
