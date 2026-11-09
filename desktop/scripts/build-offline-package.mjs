// Assembles the offline edition: the application beside an offline-pack
// directory holding everything its installers would otherwise download.
//
//   node scripts/build-offline-package.mjs --uv <uv> --models <finesub/models> --exe <exe>
//       [--out <directory>] [--since <old offline-pack>]
//
// Models come from an installed copy (--models), checked against the manifest.
// Everything else is downloaded and checked here. Re-running keeps what is
// already right. --since leaves out what an older package already has, for a
// delta to merge into it.
import { copyFileSync, existsSync, mkdirSync, readFileSync, readdirSync, renameSync, rmSync, statSync, writeFileSync } from "node:fs";
import path from "node:path";
import {
  bootstrapAssets,
  desktop,
  download,
  engine,
  lockFiles,
  modelManifest,
  pythonBuilds,
  repository,
  runtimeAssets,
  sha256Of,
} from "./resource-sources.mjs";

function readArgument(name) {
  const index = process.argv.indexOf(name);
  return index < 0 ? "" : process.argv[index + 1] || "";
}

const uv = readArgument("--uv");
const models = readArgument("--models");
const exe = readArgument("--exe");
const since = readArgument("--since");
if (!uv || !models || !exe) {
  throw new Error("Usage: node scripts/build-offline-package.mjs --uv <uv> --models <finesub/models> --exe <exe> [--out <dir>] [--since <old offline-pack>]");
}
const out = path.resolve(readArgument("--out") || path.join(desktop, "bin", "offline", "Nonoka-Sub-X-Offline"));
const pack = path.join(out, "offline-pack");
let placed = 0;
let skipped = 0;
let bytes = 0;

function fileName(url) {
  return decodeURIComponent(new URL(url).pathname.split("/").pop());
}

/** Puts one file at `relative`, from `local` when that is given, else from `url`. */
async function place(relative, { url = "", local = "", sha256 = "", size = 0 }) {
  if (since && existsSync(path.join(since, relative))) {
    skipped += 1;
    return;
  }
  const target = path.join(pack, relative);
  if (existsSync(target) && (!sha256 || await sha256Of(target) === sha256)) {
    placed += 1;
    bytes += statSync(target).size;
    return;
  }
  mkdirSync(path.dirname(target), { recursive: true });
  const partial = `${target}.part`;
  const usable = local && existsSync(local);
  if (!usable && !url) throw new Error(`${local} is missing and has no download source`);
  console.log(`${usable ? "Copying" : "Downloading"} ${relative}`);
  if (usable) copyFileSync(local, partial);
  else await download(url, partial);
  const actual = statSync(partial).size;
  if (size && actual !== size) throw new Error(`${relative}: ${actual} bytes, wanted ${size}`);
  if (sha256) {
    const digest = await sha256Of(partial);
    if (digest !== sha256) throw new Error(`${relative}: sha256 ${digest}, wanted ${sha256}`);
  }
  renameSync(partial, target);
  placed += 1;
  bytes += actual;
}

function byDigest(asset) {
  return [`files/${asset.sha256}/${fileName(asset.url)}`, asset];
}

function desktopModelIds() {
  const source = readFileSync(path.join(repository, "src", "nonoka_x", "model_install.py"), "utf8");
  return ["PIPELINE_MODEL_IDS", "DIARIZATION_MODEL_IDS"].flatMap(name => {
    const match = new RegExp(`^${name} = \\(([^)]*)\\)`, "m").exec(source);
    if (!match) throw new Error(`${name} not found in model_install.py`);
    return [...match[1].matchAll(/"([^"]+)"/g)].map(found => found[1]);
  });
}

function walk(directory, base = directory) {
  return readdirSync(directory, { withFileTypes: true }).flatMap(entry => {
    const full = path.join(directory, entry.name);
    if (statSync(full).isDirectory()) return walk(full, base);
    return [path.relative(base, full).split(path.sep).join("/")];
  });
}

async function placeHuggingFaceModel(id, model) {
  const repositoryDirectory = `models--${model.repo.replace("/", "--")}`;
  const snapshot = path.join(models, "huggingface", "hub", repositoryDirectory, "snapshots", model.revision);
  if (!existsSync(snapshot)) throw new Error(`${id}: ${snapshot} is missing; install the model on this machine first`);
  const listed = new Map(model.files.map(file => [file.name, file]));
  const names = model.listedOnly ? [...listed.keys()] : walk(snapshot);
  for (const name of listed.keys()) {
    if (!names.includes(name)) throw new Error(`${id}: ${name} is not in ${snapshot}`);
  }
  for (const name of names) {
    const wanted = listed.get(name);
    await place(`huggingface/${repositoryDirectory}/${model.revision}/${name}`, {
      local: path.join(snapshot, name),
      sha256: wanted?.sha256 ?? "",
      size: wanted?.size ?? 0,
    });
  }
}

async function placeSeparator(model) {
  const directory = path.join(models, "audio-separator");
  for (const file of model.files) {
    const local = path.join(directory, file.name);
    if (file.sha256) await place(`files/${file.sha256}/${file.name}`, { url: file.url, local, sha256: file.sha256, size: file.size });
    else await place(`unverified/${file.name}`, { url: file.url, local });
  }
}

async function placeLauncherWheels() {
  const body = readFileSync(path.join(repository, "bootstrap-requirements.py312.txt"), "utf8");
  const blocks = [...body.matchAll(/^([A-Za-z0-9_.-]+)==([^\s;\\]+)[^\n]*\\\n((?:\s+--hash=sha256:[0-9a-f]{64}[ \\]*\n?)+)/gm)];
  if (blocks.length === 0) throw new Error("no requirements parsed from bootstrap-requirements.py312.txt");
  for (const [, name, version, hashes] of blocks) {
    const allowed = new Set([...hashes.matchAll(/sha256:([0-9a-f]{64})/g)].map(found => found[1]));
    const response = await fetch(`https://pypi.org/pypi/${name}/${version}/json`);
    if (!response.ok) throw new Error(`PyPI answered ${response.status} for ${name} ${version}`);
    const candidates = (await response.json()).urls.filter(file => allowed.has(file.digests.sha256) && file.filename.endsWith(".whl"));
    const wheel = candidates.find(file => /-cp312-(?:cp312|abi3)-win_amd64\.whl$/.test(file.filename))
      ?? candidates.find(file => /-abi3-win_amd64\.whl$/.test(file.filename))
      ?? candidates.find(file => /-none-any\.whl$/.test(file.filename));
    if (!wheel) throw new Error(`${name} ${version} has no wheel for Windows x64`);
    await place(`launcher/${wheel.filename}`, { url: wheel.url, sha256: wheel.digests.sha256, size: wheel.size });
  }
}

mkdirSync(pack, { recursive: true });
const unique = new Map(
  [...runtimeAssets(), ...lockFiles(), ...bootstrapAssets().filter(asset => asset.url.includes("windows"))].map(asset => [asset.sha256, asset]),
);
for (const asset of unique.values()) await place(...byDigest(asset));

for (const { url } of pythonBuilds(uv, ["windows/x86_64"])) {
  const [tag, name] = decodeURIComponent(new URL(url).pathname).split("/").slice(-2);
  await place(`python/${tag}/${name}`, { url });
}
await placeLauncherWheels();

const manifest = modelManifest();
for (const id of desktopModelIds()) {
  if (id === "separator") await placeSeparator(manifest[id]);
  else await placeHuggingFaceModel(id, manifest[id]);
}

// Tidy leftovers of an interrupted run; they are never looked at.
for (const relative of walk(pack).filter(name => name.endsWith(".part"))) rmSync(path.join(pack, relative));

const upstream = JSON.parse(readFileSync(path.join(path.dirname(path.dirname(engine)), "UPSTREAM.json"), "utf8"));
writeFileSync(
  path.join(pack, "pack.json"),
  `${JSON.stringify({ schema: 1, engine: upstream.engine_version, delta: Boolean(since), files: placed }, null, 2)}\n`,
);
copyFileSync(exe, path.join(out, "Nonoka Sub X.exe"));
console.log(`${placed} files, ${(bytes / 1024 ** 3).toFixed(2)} GB in ${out}${since ? `; ${skipped} already in ${since}` : ""}`);
