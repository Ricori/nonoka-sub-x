// Copies every GitHub file the installers fetch to R2, where the last download
// rung finds them: cloudfile.nonoka.online/github-mirror/<host>/<path>. Run it
// after anything that changes those files -- an engine sync, a new uv, a new
// FFmpeg pin. Files already there at the right size are left alone.
//
//   node scripts/publish-github-mirror.mjs --uv <uv 0.11.32 executable> [--dry-run]
//
// Credentials come from desktop/.env.release, as for publish-update.mjs.
import { createReadStream, existsSync, mkdtempSync, readFileSync, rmSync, statSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { HeadObjectCommand, PutObjectCommand, S3Client } from "@aws-sdk/client-s3";
import {
  PYTHON_RELEASES,
  bootstrapAssets,
  desktop,
  download,
  lockFiles,
  modelManifest,
  pythonBuilds,
  runtimeAssets,
  sha256Of,
} from "./resource-sources.mjs";

const PREFIX = "github-mirror/";
const PUBLIC_BASE = `https://cloudfile.nonoka.online/${PREFIX}`;
const IMMUTABLE = "public, max-age=31536000, immutable";
const GITHUB_FILE = /^https:\/\/(github\.com|raw\.githubusercontent\.com)\//;

function readArgument(name) {
  const index = process.argv.indexOf(name);
  return index < 0 ? "" : process.argv[index + 1] || "";
}

function loadEnvFile(file) {
  if (!existsSync(file)) return;
  for (const line of readFileSync(file, "utf8").split(/\r?\n/)) {
    const match = /^\s*([A-Z0-9_]+)\s*=\s*(.*?)\s*$/.exec(line);
    if (match && !(match[1] in process.env)) process.env[match[1]] = match[2];
  }
}

function githubAssets(uv) {
  const unique = new Map();
  const interpreters = pythonBuilds(uv, ["windows/x86_64", "macos/x86_64", "macos/aarch64"]);
  const modelFiles = Object.values(modelManifest()).flatMap(model => model.files ?? []);
  for (const asset of [...runtimeAssets(), ...modelFiles, ...lockFiles(), ...bootstrapAssets(), ...interpreters]) {
    if (asset.url && GITHUB_FILE.test(asset.url)) unique.set(asset.url, asset);
  }
  return [...unique.values()];
}

function locationPart(url) {
  return url.replace(/^https:\/\//, "");
}

function objectKey(url) {
  return PREFIX + decodeURIComponent(locationPart(url));
}

function megabytes(size) {
  return `${(size / 1024 ** 2).toFixed(1)} MB`;
}

async function remoteSize(client, bucket, key) {
  try {
    return (await client.send(new HeadObjectCommand({ Bucket: bucket, Key: key }))).ContentLength;
  } catch (error) {
    if (error?.name === "NotFound" || error?.$metadata?.httpStatusCode === 404) return null;
    throw error;
  }
}

async function verifyPublic(asset, size) {
  const url = PUBLIC_BASE + locationPart(asset.url);
  const response = await fetch(url, { method: "HEAD" });
  const length = Number(response.headers.get("content-length"));
  if (!response.ok || length !== size) throw new Error(`${url} answered ${response.status} with ${length} bytes, wanted ${size}`);
}

const uv = readArgument("--uv");
if (!uv) throw new Error("Usage: node scripts/publish-github-mirror.mjs --uv <uv executable> [--dry-run]");
const assets = githubAssets(uv);
if (process.argv.includes("--dry-run")) {
  for (const asset of assets) console.log(`${objectKey(asset.url)}${asset.size ? ` (${megabytes(asset.size)})` : ""}`);
  console.log(`${assets.length} files`);
  process.exit(0);
}

loadEnvFile(path.join(desktop, ".env.release"));
const missing = ["R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET"].filter(name => !process.env[name]);
if (missing.length) throw new Error(`Missing R2 credentials: ${missing.join(", ")}`);
const client = new S3Client({
  region: "auto",
  endpoint: process.env.R2_ENDPOINT || `https://${process.env.R2_ACCOUNT_ID}.r2.cloudflarestorage.com`,
  requestChecksumCalculation: "WHEN_REQUIRED",
  responseChecksumValidation: "WHEN_REQUIRED",
  credentials: { accessKeyId: process.env.R2_ACCESS_KEY_ID, secretAccessKey: process.env.R2_SECRET_ACCESS_KEY },
});
const bucket = process.env.R2_BUCKET;
const work = mkdtempSync(path.join(tmpdir(), "nonoka-github-mirror-"));

try {
  for (const asset of assets) {
    const key = objectKey(asset.url);
    const existing = await remoteSize(client, bucket, key);
    if (asset.size && existing === asset.size) {
      await verifyPublic(asset, asset.size);
      console.log(`Present ${key}`);
      continue;
    }
    const file = path.join(work, "download");
    process.stdout.write(`Copying ${key}... `);
    await download(asset.url, file);
    const size = statSync(file).size;
    if (asset.size && size !== asset.size) throw new Error(`${asset.url}: ${size} bytes, wanted ${asset.size}`);
    if (asset.sha256) {
      const digest = await sha256Of(file);
      if (digest !== asset.sha256) throw new Error(`${asset.url}: sha256 ${digest}, wanted ${asset.sha256}`);
    }
    await client.send(new PutObjectCommand({
      Bucket: bucket,
      Key: key,
      Body: createReadStream(file),
      ContentLength: size,
      ContentType: "application/octet-stream",
      // Only a file with no digest can change behind its URL.
      CacheControl: asset.sha256 || asset.url.startsWith(PYTHON_RELEASES) ? IMMUTABLE : "public, max-age=3600",
    }));
    await verifyPublic(asset, size);
    rmSync(file, { force: true });
    console.log(megabytes(size));
  }
} finally {
  rmSync(work, { recursive: true, force: true });
}
console.log(`${assets.length} files on ${PUBLIC_BASE}`);
