import { createHash } from "node:crypto";
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { createServer } from "node:http";
import { tmpdir } from "node:os";
import path from "node:path";
import { spawn } from "node:child_process";
import test from "node:test";
import assert from "node:assert/strict";

const publisher = path.join(import.meta.dirname, "..", "scripts", "publish-update.mjs");

function manifest(version, artifacts) {
  return { schemaVersion: 1, version, channel: "stable", name: `Nonoka X ${version}`, artifacts };
}

function artifact(platform, arch, filename, content) {
  return {
    url: filename,
    platform,
    arch,
    size: content.length,
    digestAlgo: "sha512",
    digest: createHash("sha512").update(content).digest("base64"),
  };
}

async function runPublisher(localManifest, files, remoteManifest) {
  const directory = mkdtempSync(path.join(tmpdir(), "nonoka-publish-"));
  const requests = [];
  writeFileSync(path.join(directory, "latest.json"), JSON.stringify(localManifest));
  for (const [name, content] of Object.entries(files)) writeFileSync(path.join(directory, name), content);

  const server = createServer((request, response) => {
    const chunks = [];
    request.on("data", chunk => chunks.push(chunk));
    request.on("end", () => {
      const body = Buffer.concat(chunks);
      requests.push({ method: request.method, url: request.url, headers: request.headers, body });
      if (request.method === "GET" && remoteManifest) {
        response.writeHead(200, { "content-type": "application/json" });
        response.end(JSON.stringify(remoteManifest));
      } else if (request.method === "GET") {
        response.writeHead(404, { "content-type": "application/xml" });
        response.end("<Error><Code>NoSuchKey</Code></Error>");
      } else {
        response.writeHead(200);
        response.end();
      }
    });
  });
  await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
  const address = server.address();
  try {
    const result = await new Promise(resolve => {
      const child = spawn(process.execPath, [publisher, "--dir", directory], {
        env: {
          ...process.env,
          R2_ACCOUNT_ID: "test-account",
          R2_ACCESS_KEY_ID: "test-key",
          R2_SECRET_ACCESS_KEY: "test-secret",
          R2_BUCKET: "test-bucket",
          R2_ENDPOINT: `http://127.0.0.1:${address.port}`,
          R2_FORCE_PATH_STYLE: "1",
        },
      });
      let stdout = "";
      let stderr = "";
      child.stdout.on("data", chunk => { stdout += chunk; });
      child.stderr.on("data", chunk => { stderr += chunk; });
      child.on("close", status => resolve({ status, stdout, stderr }));
    });
    assert.equal(result.status, 0, result.stderr || result.stdout);
    return { requests, published: JSON.parse(readFileSync(path.join(directory, "latest.json"), "utf8")) };
  } finally {
    server.close();
    rmSync(directory, { recursive: true, force: true });
  }
}

test("publishes Windows first and merges same-version macOS", async () => {
  const windowsContent = Buffer.from("windows-update");
  const macContent = Buffer.from("mac-update");
  const windows = artifact("windows", "amd64", "Nonoka-X-0.5.1-windows-amd64.exe", windowsContent);
  const darwin = artifact("darwin", "universal", "Nonoka-X-0.5.1-darwin-universal.zip", macContent);
  const result = await runPublisher(
    manifest("0.5.1", [windows]),
    { [windows.url]: windowsContent },
    manifest("0.5.1", [darwin]),
  );

  assert.deepEqual(result.requests.map(request => request.method), ["GET", "PUT", "PUT"]);
  assert.match(result.requests[1].url, /Nonoka-X-0\.5\.1-windows-amd64\.exe\?/);
  assert.match(result.requests[1].headers["cache-control"], /immutable/);
  assert.match(result.requests[2].url, /latest\.json\?/);
  assert.equal(result.requests[2].headers["cache-control"], "no-cache");
  assert.deepEqual(result.published.artifacts.map(artifactKey).sort(), ["darwin/universal", "windows/amd64"]);
});

test("does not merge artifacts from another release version", async () => {
  const content = Buffer.from("windows-update");
  const windows = artifact("windows", "amd64", "Nonoka-X-0.5.2-windows-amd64.exe", content);
  const oldMac = artifact("darwin", "universal", "Nonoka-X-0.5.1-darwin-universal.zip", Buffer.from("old-mac"));
  const result = await runPublisher(
    manifest("0.5.2", [windows]),
    { [windows.url]: content },
    manifest("0.5.1", [oldMac]),
  );
  assert.deepEqual(result.published.artifacts.map(artifactKey), ["windows/amd64"]);
});

test("publishes only to the configured bucket and prefix", async () => {
  const content = Buffer.from("windows-update");
  const windows = artifact("windows", "amd64", "Nonoka-X-0.5.3-windows-amd64.exe", content);
  const result = await runPublisher(
    manifest("0.5.3", [windows]),
    { [windows.url]: content },
    null,
  );
  const urls = result.requests.map(request => request.url);
  assert.equal(urls.length, 3);
  assert.ok(urls.every(url => url.startsWith("/test-bucket/desktop-updates/")));
});

function artifactKey(value) {
  return `${value.platform}/${value.arch}`;
}
