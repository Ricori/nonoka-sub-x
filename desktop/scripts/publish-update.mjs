import { createHash } from "node:crypto";
import { existsSync, readFileSync, statSync, writeFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { GetObjectCommand, PutObjectCommand, S3Client } from "@aws-sdk/client-s3";

const scriptsDir = path.dirname(fileURLToPath(import.meta.url));
const root = path.dirname(scriptsDir);
// Releases live under their own prefix in the nonoka-x-cloud bucket, served
// publicly as cloudfile.nonoka.online/desktop-updates/. Clients up to 0.5.0
// still poll the old bucket, so the first release after the move is also
// written there when explicitly requested.
const DEFAULT_PREFIX = "desktop-updates/";
const LEGACY_BUCKET = "youtube-live-cache";
const LEGACY_PREFIX = "nonoka-x-updates/wails/";

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

function normalisePrefix(value) {
  const clean = value.replace(/^\/+|\/+$/g, "");
  if (!clean || clean.includes("..")) throw new Error(`Invalid R2 prefix: ${value}`);
  return `${clean}/`;
}

function artifactKey(artifact) {
  return `${artifact.platform}/${artifact.arch}`;
}

function validateManifest(manifest) {
  if (manifest?.schemaVersion !== 1 || !/^\d+\.\d+\.\d+$/.test(manifest.version || "")) {
    throw new Error("Invalid Wails update manifest");
  }
  if (!Array.isArray(manifest.artifacts) || manifest.artifacts.length === 0) {
    throw new Error("Update manifest has no artifacts");
  }
  for (const artifact of manifest.artifacts) {
    if (!["windows", "darwin"].includes(artifact.platform)) {
      throw new Error(`Unsupported update platform: ${artifact.platform}`);
    }
    if (!artifact.url || path.basename(artifact.url) !== artifact.url || artifact.url.includes("\\")) {
      throw new Error(`Unsafe artifact URL: ${artifact.url}`);
    }
  }
}

function verifyArtifact(directory, artifact) {
  const file = path.join(directory, artifact.url);
  if (!existsSync(file) || !statSync(file).isFile()) throw new Error(`Missing artifact: ${file}`);
  const algorithm = String(artifact.digestAlgo || "").toLowerCase();
  if (!["sha256", "sha512"].includes(algorithm)) throw new Error(`Unsupported digest: ${algorithm}`);
  const digest = createHash(algorithm).update(readFileSync(file)).digest("base64");
  if (digest !== artifact.digest) throw new Error(`Digest mismatch: ${artifact.url}`);
  return file;
}

function isMissingObject(error) {
  return error?.name === "NoSuchKey" || error?.$metadata?.httpStatusCode === 404;
}

async function readRemoteManifest(client, bucket, key) {
  try {
    const response = await client.send(new GetObjectCommand({ Bucket: bucket, Key: key }));
    return JSON.parse(await response.Body.transformToString("utf8"));
  } catch (error) {
    if (isMissingObject(error)) return null;
    throw error;
  }
}

function mergeSameVersion(local, remote) {
  if (!remote || remote.version !== local.version) return local;
  validateManifest(remote);
  const artifacts = new Map(remote.artifacts.map(artifact => [artifactKey(artifact), artifact]));
  for (const artifact of local.artifacts) artifacts.set(artifactKey(artifact), artifact);
  return { ...local, artifacts: [...artifacts.values()] };
}

function contentType(file) {
  if (file.endsWith(".zip")) return "application/zip";
  if (file.endsWith(".exe")) return "application/vnd.microsoft.portable-executable";
  return "application/octet-stream";
}

const directoryArg = readArgument("--dir");
if (!directoryArg) throw new Error("Usage: node scripts/publish-update.mjs --dir bin/update/<version>");
const directory = path.resolve(root, directoryArg);
const manifestPath = path.join(directory, "latest.json");
if (!existsSync(manifestPath)) throw new Error(`Missing manifest: ${manifestPath}`);

loadEnvFile(path.join(root, ".env.release"));
const missing = ["R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET"]
  .filter(name => !process.env[name]);
if (missing.length) throw new Error(`Missing R2 credentials: ${missing.join(", ")}`);

const localManifest = JSON.parse(readFileSync(manifestPath, "utf8"));
validateManifest(localManifest);
const localFiles = new Map(localManifest.artifacts.map(artifact => [artifactKey(artifact), verifyArtifact(directory, artifact)]));
const prefix = normalisePrefix(process.env.R2_WAILS_PREFIX || DEFAULT_PREFIX);
const publishLegacyPathOnce = process.argv.includes("--publish-legacy-update-path-once");
const endpoint = process.env.R2_ENDPOINT || `https://${process.env.R2_ACCOUNT_ID}.r2.cloudflarestorage.com`;
const client = new S3Client({
  region: "auto",
  endpoint,
  forcePathStyle: process.env.R2_FORCE_PATH_STYLE === "1",
  requestChecksumCalculation: "WHEN_REQUIRED",
  responseChecksumValidation: "WHEN_REQUIRED",
  credentials: {
    accessKeyId: process.env.R2_ACCESS_KEY_ID,
    secretAccessKey: process.env.R2_SECRET_ACCESS_KEY,
  },
});

async function publishToPrefix(bucket, targetPrefix) {
  const manifestKey = `${targetPrefix}latest.json`;
  const remoteManifest = await readRemoteManifest(client, bucket, manifestKey);
  const publishedManifest = mergeSameVersion(localManifest, remoteManifest);

  for (const artifact of localManifest.artifacts) {
    const file = localFiles.get(artifactKey(artifact));
    const size = statSync(file).size;
    process.stdout.write(`Uploading ${targetPrefix}${artifact.url} (${(size / 1024 ** 2).toFixed(1)} MB)... `);
    await client.send(new PutObjectCommand({
      Bucket: bucket,
      Key: `${targetPrefix}${artifact.url}`,
      Body: readFileSync(file),
      ContentLength: size,
      ContentType: contentType(file),
      CacheControl: "public, max-age=31536000, immutable",
    }));
    console.log("done");
  }

  const manifestBody = Buffer.from(`${JSON.stringify(publishedManifest, null, 2)}\n`);
  await client.send(new PutObjectCommand({
    Bucket: bucket,
    Key: manifestKey,
    Body: manifestBody,
    ContentLength: manifestBody.length,
    ContentType: "application/json",
    CacheControl: "no-cache",
  }));
  console.log(`Published v${publishedManifest.version} to r2://${bucket}/${targetPrefix}`);
  return manifestBody;
}

const manifestBody = await publishToPrefix(process.env.R2_BUCKET, prefix);
writeFileSync(manifestPath, manifestBody);
const legacyBucket = process.env.R2_LEGACY_BUCKET || LEGACY_BUCKET;
if (publishLegacyPathOnce && !(process.env.R2_BUCKET === legacyBucket && prefix === LEGACY_PREFIX)) {
  await publishToPrefix(legacyBucket, LEGACY_PREFIX);
}
