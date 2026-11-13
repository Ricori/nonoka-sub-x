// Assembles the offline edition: the application beside an offline-pack
// directory holding everything its installers would otherwise download.
//
//   node scripts/build-offline-package.mjs --uv <uv> [--exe <exe>] [--out <directory>]
//       [--layers <dist\layers>] [--index <layers.json>] [--since <old offline-pack>]
//       [--layers-only]
//
// The pack holds two things:
//
// - The three prebuilt layers this release wants (src/nonoka_x/resources/layers.json):
//   each layer's manifest and archive, under the same relative paths as on R2
//   (v1/<layer>/<sha256>.json and .tar.zst), which is where the sidecar's layer
//   manager looks for them before going online. They are downloaded from the
//   index's baseUrl, or copied from a local build (--layers, the dist\layers of
//   scripts/build_layers.py); either way every file is checked against the
//   index's size and sha256, so it is byte for byte the object on R2.
// - What the launcher bootstrap (desktop/internal/app/python_bootstrap.go)
//   downloads before there is any sidecar: uv (files/<sha256>/), the
//   interpreter uv installs (python/<tag>/) and the launcher's wheels
//   (launcher/). --layers-only leaves these out.
//
// Re-running keeps what is already right. --since leaves out what an older
// package already has, for a delta to merge into it. Without --exe only the
// offline-pack directory is written.
import { copyFileSync, existsSync, mkdirSync, readFileSync, readdirSync, renameSync, rmSync, statSync, writeFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import {
  bootstrapAssets,
  desktop,
  download,
  layerObjects,
  pythonBuilds,
  readLayersIndex,
  repository,
  sha256Of,
} from "./resource-sources.mjs";

function fileName(url) {
  return decodeURIComponent(new URL(url).pathname.split("/").pop());
}

function walk(directory, base = directory) {
  return readdirSync(directory, { withFileTypes: true }).flatMap(entry => {
    const full = path.join(directory, entry.name);
    if (statSync(full).isDirectory()) return walk(full, base);
    return [path.relative(base, full).split(path.sep).join("/")];
  });
}

function gib(bytes) {
  return `${(bytes / 1024 ** 3).toFixed(2)} GiB`;
}

/**
 * Builds `<out>/offline-pack`. Returns what it placed. `index` is a
 * readLayersIndex() result; `layersFrom` a dist\layers directory to copy the
 * layers from instead of downloading them; `uv` the pinned uv, needed for the
 * launcher's interpreter unless `launcher` is false.
 */
export async function buildOfflinePack({
  out,
  index = readLayersIndex(),
  layersFrom = "",
  since = "",
  launcher = true,
  uv = "",
  exe = "",
  log = console.log,
}) {
  const pack = path.join(out, "offline-pack");
  const totals = { placed: 0, skipped: 0, bytes: 0 };

  /** Puts one file at `relative`, from `local` when that is given, else from `url`. */
  async function place(relative, { url = "", local = "", sha256 = "", size = 0 }) {
    if (since && existsSync(path.join(since, relative))) {
      totals.skipped += 1;
      return;
    }
    const target = path.join(pack, relative);
    if (existsSync(target) && (!size || statSync(target).size === size) && (!sha256 || await sha256Of(target) === sha256)) {
      totals.placed += 1;
      totals.bytes += statSync(target).size;
      return;
    }
    mkdirSync(path.dirname(target), { recursive: true });
    const partial = `${target}.part`;
    rmSync(partial, { force: true });
    if (local && !existsSync(local)) throw new Error(`${local} is missing`);
    if (!local && !url) throw new Error(`${relative} has no source`);
    log(`${local ? "Copying" : "Downloading"} ${relative}${size ? ` (${gib(size)})` : ""}`);
    if (local) copyFileSync(local, partial);
    else await download(url, partial);
    const actual = statSync(partial).size;
    if (size && actual !== size) {
      rmSync(partial, { force: true });
      throw new Error(`${relative}: ${actual} bytes, wanted ${size}`);
    }
    if (sha256) {
      const digest = await sha256Of(partial);
      if (digest !== sha256) {
        rmSync(partial, { force: true });
        throw new Error(`${relative}: sha256 ${digest}, wanted ${sha256}`);
      }
    }
    renameSync(partial, target);
    totals.placed += 1;
    totals.bytes += actual;
  }

  mkdirSync(pack, { recursive: true });

  // The layers, where the layer manager looks for them (`nonoka_x.runtime.layers`).
  for (const object of layerObjects(index)) {
    const local = layersFrom
      ? path.join(layersFrom, object.layer, object.version, object.kind === "archive" ? "layer.tar.zst" : "manifest.json")
      : "";
    await place(object.path, { url: object.url, local, sha256: object.sha256, size: object.size });
  }

  if (launcher) {
    if (!uv) throw new Error("the launcher's interpreter needs --uv");
    for (const asset of bootstrapAssets().filter(item => item.url.includes("windows"))) {
      await place(`files/${asset.sha256}/${fileName(asset.url)}`, asset);
    }
    for (const { url } of pythonBuilds(uv, ["windows/x86_64"])) {
      const [tag, name] = decodeURIComponent(new URL(url).pathname).split("/").slice(-2);
      await place(`python/${tag}/${name}`, { url });
    }
    await placeLauncherWheels(place);
  }

  // Tidy leftovers of an interrupted run; they are never looked at.
  for (const relative of walk(pack).filter(name => name.endsWith(".part"))) rmSync(path.join(pack, relative));

  const versions = Object.fromEntries(Object.entries(index.layers).map(([layer, entry]) => [layer, entry.version]));
  writeFileSync(
    path.join(pack, "pack.json"),
    `${JSON.stringify({ schema: 2, platform: index.platform, layers: versions, launcher, delta: Boolean(since), files: totals.placed }, null, 2)}\n`,
  );
  if (exe) copyFileSync(exe, path.join(out, "Nonoka Sub X.exe"));
  return { pack, ...totals };
}

async function placeLauncherWheels(place) {
  const body = readFileSync(path.join(repository, "bootstrap-requirements.py312.txt"), "utf8");
  const blocks = [...body.matchAll(/^([A-Za-z0-9_.-]+)==([^\s;\\]+)[^\n]*\\\r?\n((?:\s+--hash=sha256:[0-9a-f]{64}[ \\]*\r?\n?)+)/gm)];
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

function readArgument(name) {
  const index = process.argv.indexOf(name);
  return index < 0 ? "" : process.argv[index + 1] || "";
}

async function main() {
  const layersOnly = process.argv.includes("--layers-only");
  const uv = readArgument("--uv");
  if (!uv && !layersOnly) {
    throw new Error("Usage: node scripts/build-offline-package.mjs --uv <uv> [--exe <exe>] [--out <dir>] [--layers <dist\\layers>] [--index <layers.json>] [--since <old offline-pack>] [--layers-only]");
  }
  const exe = readArgument("--exe");
  const indexFile = readArgument("--index");
  const out = path.resolve(readArgument("--out") || path.join(desktop, "bin", "offline", "Nonoka-Sub-X-Offline"));
  const started = Date.now();
  const result = await buildOfflinePack({
    out,
    index: indexFile ? readLayersIndex(path.resolve(indexFile)) : readLayersIndex(),
    layersFrom: readArgument("--layers") ? path.resolve(readArgument("--layers")) : "",
    since: readArgument("--since"),
    launcher: !layersOnly,
    uv,
    exe,
  });
  const seconds = Math.round((Date.now() - started) / 1000);
  console.log(`${result.placed} files, ${gib(result.bytes)} in ${result.pack} (${seconds} s)${result.skipped ? `; ${result.skipped} already in the older package` : ""}`);
  if (!exe) console.log("No --exe: only the offline-pack directory was written.");
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  await main();
}
