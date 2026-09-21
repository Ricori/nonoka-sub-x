import assert from "node:assert/strict";
import test from "node:test";

import {
  cloneStyleText, patchStyleText, safeStyleName, styleFieldsOf, uniqueStyleName,
} from "../src/subtitles/styleEdit.ts";
import { composeSheet, parseSheet } from "../src/subtitles/styles.ts";
import {
  BUILTIN_ASS_STYLES, DEFAULT_STYLE_SHEET, DEFAULT_USER_ASS_STYLES,
} from "../src/subtitles/constants.ts";
import { formatAssColor, hexOf, parseAssColor, parseCssHex } from "../src/subtitles/color.ts";

// 图形化面板每改一个字段都调 patchStyleText，所以它的两条硬要求都要钉住：
// 改一个字段不能动到别的 22 个（AssStyle 是有损投影，最容易在这里丢东西），
// 以及除目标那一行外整份文本逐字节不变（用户手写的注释不能被洗掉）。

const SHEET = [
  "[Script Info]",
  "; 这是一条注释，必须活下来",
  "PlayResX: 1920",
  "PlayResY: 1080",
  "",
  "[V4+ Styles]",
  "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour,"
  + " Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline,"
  + " Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
  "Style: 优花,荆南波波黑,90,&H00D59B57,&H000000FF,&H00FFFFFF,&H00000000,-1,0,-1,1,110,120,3,15,3,5,0,2,10,10,30,134",
  "Style: 注释,思源黑体 Heavy,60,&H00FFFFFF,&H000000FF,&H00000000,&H00737375,0,0,0,0,100,100,0,0,1,2,2,7,30,30,30,1",
].join("\n");

const styleLineOf = (text, name) => text.split("\n").find(l => l.startsWith("Style: " + name + ","));

test("patching one field leaves the other 22 byte-identical", () => {
  const { text, normalised } = patchStyleText(SHEET, "优花", { fontsize: 120 });
  assert.equal(normalised, false);
  const before = styleLineOf(SHEET, "优花").split(",");
  const after = styleLineOf(text, "优花").split(",");
  assert.equal(after[2], "120");
  before.forEach((value, i) => { if (i !== 2) assert.equal(after[i], value, "字段 " + i + " 被动过了"); });
  // AssStyle 不建模的那六项是最容易丢的，单独点名
  const f = parseSheet(text).fields["优花"];
  assert.equal(f.secondarycolour, "&H000000FF");
  assert.equal(f.underline, "-1");
  assert.equal(f.strikeout, "1");
  assert.equal(f.angle, "15");
  assert.equal(f.borderstyle, "3");
  assert.equal(f.encoding, "134");
});

test("everything except the target line survives byte for byte", () => {
  const { text } = patchStyleText(SHEET, "优花", { primarycolour: "&H00112233" });
  const before = SHEET.split("\n");
  const after = text.split("\n");
  assert.equal(after.length, before.length);
  before.forEach((line, i) => {
    if (line.startsWith("Style: 优花,")) assert.notEqual(after[i], line);
    else assert.equal(after[i], line, "第 " + (i + 1) + " 行不该变");
  });
});

test("patching several fields at once writes them all in one pass", () => {
  const { text } = patchStyleText(SHEET, "优花", { marginl: 100, marginr: 0, marginv: 240 });
  const f = parseSheet(text).fields["优花"];
  assert.deepEqual([f.marginl, f.marginr, f.marginv], ["100", "0", "240"]);
});

test("a built-in style missing from the sheet is materialised from JP/CN", () => {
  const { text, normalised } = patchStyleText(SHEET, "JP", { fontsize: 88 });
  assert.equal(normalised, false);
  const builtin = parseSheet(BUILTIN_ASS_STYLES).fields["JP"];
  const made = parseSheet(text).fields["JP"];
  assert.equal(made.fontsize, "88");
  for (const [key, value] of Object.entries(builtin)) {
    if (key !== "fontsize" && key !== "name") {
      assert.equal(made[key], value, "JP 的 " + key + " 应当照抄内置那份");
    }
  }
  // 本机那份同名样式覆盖内置那份，所以合成后读到的就是改过的值
  assert.equal(composeSheet(text).styleMap["JP"].size, 88);
});

test("a brand-new name is created from the Aegisub defaults", () => {
  const { text } = patchStyleText(SHEET, "说话人 A", { primarycolour: "&H0000FF00" });
  const made = parseSheet(text).fields["说话人 A"];
  assert.equal(made.primarycolour, "&H0000FF00");
  assert.equal(made.fontname, "方正准圆_GBK");
  assert.equal(made.alignment, "2");
  assert.ok(composeSheet(text).styleNames.includes("说话人 A"));
});

test("a new Style line lands inside the styles section, before [Events]", () => {
  const withEvents = SHEET + "\n\n[Events]\nFormat: Layer, Start, End, Style, Name, Text\n"
    + "Dialogue: 0,0:00:00.00,0:00:01.00,优花,,不要碰我\n";
  const { text } = patchStyleText(withEvents, "新样式", { fontsize: 40 });
  const lines = text.split("\n");
  const styleAt = lines.findIndex(l => l.startsWith("Style: 新样式,"));
  const eventsAt = lines.findIndex(l => l === "[Events]");
  assert.ok(styleAt > 0 && styleAt < eventsAt, "新样式必须在 [Events] 之前");
  assert.ok(text.includes("Dialogue: 0,0:00:00.00,0:00:01.00,优花,,不要碰我"), "[Events] 段不该被动");
  // parseSheet 遇 [Events] 就停，落在后面等于白写
  assert.ok(parseSheet(text).fields["新样式"]);
});

test("a sheet with no styles section grows one", () => {
  const source = "[Script Info]\nPlayResX: 1280\nPlayResY: 720\n";
  const { text } = patchStyleText(source, "只此一家", { fontsize: 50 });
  assert.ok(text.includes("[V4+ Styles]"));
  assert.equal(parseSheet(text).fields["只此一家"].fontsize, "50");
  assert.deepEqual(parseSheet(text).playRes, { x: 1280, y: 720 });
});

test("an empty sheet grows the whole section", () => {
  const { text } = patchStyleText("", "从零开始", { fontsize: 64 });
  assert.equal(parseSheet(text).fields["从零开始"].fontsize, "64");
});

test("a non-standard Format order is preserved when it can hold the patch", () => {
  const odd = "[V4+ Styles]\nFormat: Name, Fontsize, Fontname\nStyle: 注释,88,Arial\n";
  const { text, normalised } = patchStyleText(odd, "注释", { fontsize: 42 });
  assert.equal(normalised, false);
  assert.ok(text.includes("Format: Name, Fontsize, Fontname"), "字段序不该被重排");
  assert.equal(styleLineOf(text, "注释"), "Style: 注释,42,Arial");
  assert.equal(parseSheet(text).fields["注释"].fontname, "Arial");
});

test("a Format that cannot express the patch falls back to normalising", () => {
  const odd = "[V4+ Styles]\nFormat: Name, Fontsize, Fontname\nStyle: 注释,88,Arial\n";
  const { text, normalised } = patchStyleText(odd, "注释", { angle: 30 });
  assert.equal(normalised, true, "Format 里没有 Angle 列，只能整份重排");
  const f = parseSheet(text).fields["注释"];
  assert.equal(f.angle, "30");
  assert.equal(f.fontsize, "88", "重排也要保住原有的值");
  assert.equal(f.fontname, "Arial");
});

test("duplicate style names patch the first one, matching parseSheet", () => {
  const dup = "[V4+ Styles]\nStyle: 优花,Arial,40,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,"
    + "0,0,0,0,100,100,0,0,1,2,2,2,10,10,30,1\nStyle: 优花,Verdana,50,&H00FFFFFF,&H000000FF,"
    + "&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,2,2,2,10,10,30,1\n";
  const { text } = patchStyleText(dup, "优花", { fontsize: 99 });
  const lines = text.split("\n").filter(l => l.startsWith("Style: 优花,"));
  assert.equal(lines.length, 2);
  assert.ok(lines[0].includes("Arial,99,"), "改的应当是第一条");
  assert.ok(lines[1].includes("Verdana,50,"), "第二条不该被动");
  assert.equal(parseSheet(text).fields["优花"].fontsize, "99");
});

test("commas and newlines can never leak into a Style line", () => {
  const { text } = patchStyleText(SHEET, "优花", { fontname: "Bad,Font\nName" });
  const parts = styleLineOf(text, "优花").split(",");
  assert.equal(parts.length, 23, "字段数不该变");
  assert.equal(parseSheet(text).fields["优花"].fontname, "Bad Font Name");
  assert.equal(safeStyleName("坏,名\n字"), "坏 名 字");
});

test("styleFieldsOf always returns all 23 fields", () => {
  const own = styleFieldsOf(SHEET, "优花");
  assert.equal(own.encoding, "134");
  const builtin = styleFieldsOf(SHEET, "CN");
  assert.equal(builtin.alignment, "2");
  const unknown = styleFieldsOf(SHEET, "查无此人");
  assert.equal(unknown.name, "查无此人");
  assert.equal(unknown.fontsize, "70");
  assert.equal(Object.keys(unknown).length, 23);
});

test("cloneStyleText copies every field under the new name", () => {
  const { text } = cloneStyleText(SHEET, "优花", "优花 2");
  const from = parseSheet(text).fields["优花"];
  const to = parseSheet(text).fields["优花 2"];
  for (const key of Object.keys(from)) {
    if (key !== "name") assert.equal(to[key], from[key], "克隆丢了 " + key);
  }
});

test("uniqueStyleName avoids built-ins, existing names and stray commas", () => {
  const taken = composeSheet(DEFAULT_STYLE_SHEET).styleNames;
  assert.equal(uniqueStyleName("优花", taken), "优花 2");
  assert.equal(uniqueStyleName("JP", taken), "JP 2");
  assert.equal(uniqueStyleName("优花 2", [...taken, "优花 2"]), "优花 3");
  assert.equal(uniqueStyleName("全新", taken), "全新");
  assert.equal(uniqueStyleName("坏,名字", []), "坏 名字");
});

// ── 颜色 ──────────────────────────────────────────────────────
// ASS 的 AA 字节是「透明度」：00 不透明、FF 全透明，跟 CSS 正好反着来。

test("ASS colours parse with the alpha byte inverted", () => {
  assert.deepEqual(parseAssColor("&H00FFF9FD"), { r: 0xFD, g: 0xF9, b: 0xFF, a: 1 });
  assert.deepEqual(parseAssColor("&HFF000000"), { r: 0, g: 0, b: 0, a: 0 });
  assert.equal(parseAssColor("&H30633306").a, 1 - 0x30 / 255);
  assert.deepEqual(parseAssColor("&H0000FF&"), { r: 255, g: 0, b: 0, a: 1 });
  assert.deepEqual(parseAssColor("看不懂"), { r: 255, g: 255, b: 255, a: 1 });
});

test("every colour in the shipped sheets survives a round trip", () => {
  const text = BUILTIN_ASS_STYLES + "\n" + DEFAULT_USER_ASS_STYLES;
  const colours = text.match(/&H[0-9A-Fa-f]{8}/g) ?? [];
  assert.ok(colours.length >= 20, "取样不该是空的");
  for (const raw of colours) {
    assert.equal(formatAssColor(parseAssColor(raw)), raw.toUpperCase(), raw + " 往返不一致");
  }
});

test("hex input accepts the shapes a user might paste", () => {
  assert.deepEqual(parseCssHex("#f00"), { r: 255, g: 0, b: 0, a: 1 });
  assert.deepEqual(parseCssHex("00ff00"), { r: 0, g: 255, b: 0, a: 1 });
  assert.equal(parseCssHex("#0000ff80").a, 0x80 / 255);
  assert.deepEqual(parseCssHex("&H00FFF9FD"), { r: 0xFD, g: 0xF9, b: 0xFF, a: 1 });
  assert.equal(parseCssHex("随便打的"), null);
  assert.equal(hexOf({ r: 0xFD, g: 0xF9, b: 0xFF, a: 0.5 }), "#fdf9ff");
});
