// What the installers fetch, read from the files that decide it. Shared by
// publish-github-mirror.mjs and build-offline-package.mjs.
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
export const engine = path.join(repository, "third_party", "finesub", "src", "finesub_bootstrap");
const bootstrapSource = path.join(desktop, "internal", "app", "python_bootstrap.go");

export const PYTHON_RELEASES = "https://github.com/astral-sh/python-build-standalone/releases/download/";
const ASTRAL_RELEASES = "https://releases.astral.sh/github/python-build-standalone/releases/download/";

function readJson(file) {
  return JSON.parse(readFileSync(file, "utf8"));
}

export function runtimeAssets() {
  return readJson(path.join(engine, "runtime-manifest.json")).resources.map(resource => resource.asset);
}

export function modelManifest() {
  return readJson(path.join(engine, "model-manifest.json")).models;
}

/** Every wheel and archive in the runtime lock. uv never takes an sdist over a wheel. */
export function lockFiles() {
  const body = readFileSync(path.join(engine, "pylock.win-py312.toml"), "utf8");
  const found = /(?<!sdist = \{ )url = "(https:[^"]+)"[^\n]*sha256 = "([0-9a-f]{64})"/g;
  return [...body.matchAll(found)].map(match => ({ url: match[1], sha256: match[2] }));
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

export async function download(url, destination) {
  const response = await fetch(url, { redirect: "follow", headers: { "User-Agent": "nonoka-release-tools" } });
  if (!response.ok || !response.body) throw new Error(`${url} answered ${response.status}`);
  await pipeline(Readable.fromWeb(response.body), createWriteStream(destination));
}

export async function sha256Of(file) {
  const digest = createHash("sha256");
  await pipeline(createReadStream(file), digest);
  return digest.digest("hex");
}
