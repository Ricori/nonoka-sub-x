// 构建：TypeScript -> 单个自包含 HTML。
//
//   node build.mjs           编译一次
//   node build.mjs --watch   改 src/ 就自动重编（开发时开着它）
//   node build.mjs --pack    编译后打成 ../clip-picker.nonoka-plugin
//
// 为什么必须内联成一个文件：插件页通过 srcdoc 加载，没有 origin 也没有 base URL，
// 宿主注入的 CSP 是 default-src 'none'; connect-src 'none'，所以 <script src>、
// <link>、ES module 的 import 全都用不了，运行时也读不到包里任何别的文件。
//
// 产物写到 ui/index.html —— 那是 manifest 里 page 指向的路径，也是安装目录那条
// 目录联接的目标，所以编译完在应用里切走再切回页面就能看到，不用重新打包安装。

import { readFileSync, writeFileSync, existsSync, mkdirSync, readdirSync, statSync, watch } from "node:fs";
import { join, dirname, relative, sep } from "node:path";
import { fileURLToPath } from "node:url";
import { spawnSync } from "node:child_process";
import { deflateRawSync, crc32 } from "node:zlib";

const root = dirname(fileURLToPath(import.meta.url));
const shell = join(root, "src", "index.html");
const out = join(root, "ui", "index.html");

// tsc 用仓库前端现成的那份，不为插件单独装 TypeScript
const tsc = join(root, "..", "..", "..", "frontend", "node_modules", ".bin",
  process.platform === "win32" ? "tsc.cmd" : "tsc");

function build() {
  // 先自己查一次。缺 tsc 时 spawnSync 只会抛「系统找不到指定的路径」，
  // 既不说找的是什么、也不说该怎么办 —— 而这个仓库的 node_modules 被清空过
  // 好几次（杀毒软件挑走可执行文件，只留空目录），所以这条错值得说人话。
  if (!existsSync(tsc)) {
    console.error(`  FAIL  找不到 tsc：${tsc}`);
    console.error("        在 desktop/frontend 下跑一次 npm install。");
    console.error("        如果反复丢失，把 node_modules 加进杀毒软件的排除列表。");
    return { ok: false };
  }
  const compiled = spawnSync(tsc, ["-p", join(root, "tsconfig.json")], {
    cwd: root, encoding: "utf8", shell: process.platform === "win32",
  });
  const noise = (compiled.stdout || "") + (compiled.stderr || "");
  if (compiled.status !== 0) {
    process.stdout.write(noise);
    return { ok: false };
  }
  if (noise.trim()) process.stdout.write(noise);

  let html = readFileSync(shell, "utf8");
  const inlined = [];
  html = html.replace(/<!--INLINE:([^>]+?)-->/g, (_, rel) => {
    const file = join(root, rel.trim().split("/").join(sep));
    if (!existsSync(file)) throw new Error(`INLINE 目标不存在: ${rel}`);
    const text = readFileSync(file, "utf8");
    inlined.push({ rel: rel.trim(), bytes: Buffer.byteLength(text, "utf8") });
    // 内联进 <script> 后 HTML 解析器只认 </script 作为结束，正文里真出现
    // 这个串会把页面截断。JS 里 "<\/script" 和 "</script" 等价，所以拆开是安全的。
    return text.replace(/<\/script/gi, "<\\/script");
  });

  // 上一版构建曾经「成功」却打出空白页，所以这里验产物而不是相信替换的返回值
  const problems = [];
  if (/<!--INLINE:[^>]+?-->/.test(html)) problems.push("产物里还残留 INLINE 标记");
  if (!/<head>/.test(html)) problems.push("<head> 必须是裸标签，宿主按它定位 CSP 和 window.nonoka 的注入点");
  if (!html.includes("选片小助手")) problems.push("中文被破坏（编码问题）");
  if (inlined.length === 0) problems.push("src/index.html 里没有任何 <!--INLINE:...--> 标记");
  if (problems.length > 0) {
    for (const p of problems) console.error(`  FAIL  ${p}`);
    return { ok: false };
  }

  mkdirSync(dirname(out), { recursive: true });
  writeFileSync(out, html, "utf8");
  const stamp = new Date().toTimeString().slice(0, 8);
  console.log(`[${stamp}] ok  ${inlined.map((i) => `${i.rel} ${i.bytes}B`).join("  ")}`
    + `  ->  ui/index.html ${Buffer.byteLength(html, "utf8")}B`);
  return { ok: true };
}

/**
 * 打成 .nonoka-plugin。
 *
 * 自己写 ZIP 而不是用 PowerShell 的 Compress-Archive：PS 5.1 那个版本把路径
 * 分隔符写成反斜杠，Windows 上装得进去（反斜杠当分隔符），但 macOS 上会变成
 * 一个名叫 "ui\index.html" 的单文件，安装时报 plugin tool page does not exist。
 *
 * 进包的只有 manifest、内联好的 ui/、和 assets/（后者纯存档，运行时读不到）。
 * src/、build/、构建脚本都不进。
 */
function pack() {
  const entries = [];
  const take = (dir) => {
    for (const name of readdirSync(dir)) {
      const full = join(dir, name);
      if (statSync(full).isDirectory()) take(full);
      else entries.push({ name: relative(root, full).split(sep).join("/"), data: readFileSync(full) });
    }
  };
  entries.push({ name: "nonoka-plugin.json", data: readFileSync(join(root, "nonoka-plugin.json")) });
  take(join(root, "ui"));
  if (existsSync(join(root, "assets"))) take(join(root, "assets"));
  entries.sort((a, b) => (a.name < b.name ? -1 : 1));

  const local = [];
  const central = [];
  let offset = 0;
  for (const entry of entries) {
    const name = Buffer.from(entry.name, "utf8");
    const body = deflateRawSync(entry.data);
    const sum = crc32(entry.data);

    const head = Buffer.alloc(30);
    head.writeUInt32LE(0x04034b50, 0);
    head.writeUInt16LE(20, 4);
    head.writeUInt16LE(0x0800, 6);     // 文件名是 UTF-8
    head.writeUInt16LE(8, 8);          // deflate
    head.writeUInt16LE(0x21, 12);      // 1980-01-01，让产物可复现
    head.writeUInt32LE(sum, 14);
    head.writeUInt32LE(body.length, 18);
    head.writeUInt32LE(entry.data.length, 22);
    head.writeUInt16LE(name.length, 26);
    local.push(head, name, body);

    const dir = Buffer.alloc(46);
    dir.writeUInt32LE(0x02014b50, 0);
    dir.writeUInt16LE(20, 4);
    dir.writeUInt16LE(20, 6);
    dir.writeUInt16LE(0x0800, 8);
    dir.writeUInt16LE(8, 10);
    dir.writeUInt16LE(0x21, 14);
    dir.writeUInt32LE(sum, 16);
    dir.writeUInt32LE(body.length, 20);
    dir.writeUInt32LE(entry.data.length, 24);
    dir.writeUInt16LE(name.length, 28);
    // >>> 0：JS 的 << 走有符号 32 位，0o100644<<16 会翻成负数
    dir.writeUInt32LE((0o100644 << 16) >>> 0, 38);
    dir.writeUInt32LE(offset, 42);
    central.push(dir, name);

    offset += head.length + name.length + body.length;
  }

  const cd = Buffer.concat(central);
  const end = Buffer.alloc(22);
  end.writeUInt32LE(0x06054b50, 0);
  end.writeUInt16LE(entries.length, 8);
  end.writeUInt16LE(entries.length, 10);
  end.writeUInt32LE(cd.length, 12);
  end.writeUInt32LE(offset, 16);

  const target = join(dirname(root), "clip-picker.nonoka-plugin");
  writeFileSync(target, Buffer.concat([...local, cd, end]));
  for (const entry of entries) console.log(`  ${String(entry.data.length).padStart(7)}  ${entry.name}`);
  console.log(`  ->  ${target}  ${statSync(target).size}B`);
}

const watching = process.argv.includes("--watch");
const first = build();
if (first.ok && process.argv.includes("--pack")) pack();
if (!watching) process.exit(first.ok ? 0 : 1);

console.log("watching src/ … 改完在应用里切走再切回页面即可（Ctrl+C 退出）");
let timer = null;
watch(join(root, "src"), { recursive: true }, () => {
  // 编辑器保存一次会触发好几个事件，攒一下再编译
  if (timer) clearTimeout(timer);
  timer = setTimeout(() => { timer = null; build(); }, 150);
});
