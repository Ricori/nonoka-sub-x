import assert from "node:assert/strict";
import test from "node:test";

import { assHead, getStyleNames, mergeStyleText, resolveStyle, setStyleSheet } from "../src/editor/ass.ts";
import { DEFAULT_STYLE_SHEET } from "../src/editor/constants.ts";

test("built-in origin/cn survive whatever the local sheet says", () => {
  setStyleSheet(DEFAULT_STYLE_SHEET);
  assert.deepEqual(getStyleNames().slice(0, 2), ["origin", "cn"]);
  assert.ok(getStyleNames().includes("speaker1"));
  setStyleSheet("");
  assert.deepEqual(getStyleNames(), ["origin", "cn"]);
});

test("a local style of the same name overrides the built-in one", () => {
  setStyleSheet("[V4+ Styles]\nStyle: origin,Arial,42,&H00112233,&H000000FF,&H00000000,&H00000000,"
    + "0,0,0,0,100,100,0,0,1,2,2,8,10,10,30,1");
  assert.deepEqual(getStyleNames(), ["origin", "cn"]);
  assert.match(assHead(), /Style: origin,Arial,42,/);
  assert.match(assHead(), /Style: cn,方正准圆_GBK,70,/);
});

test("bindings the local sheet does not have fall back to origin/cn by language", () => {
  setStyleSheet(DEFAULT_STYLE_SHEET);
  assert.equal(resolveStyle("speaker1", "zh"), "speaker1");
  assert.equal(resolveStyle("某位不存在的说话人", "ja"), "origin");
  assert.equal(resolveStyle("某位不存在的说话人", "zh"), "cn");
});

test("the emitted head carries the standard Format and the sheet's PlayRes", () => {
  setStyleSheet("[Script Info]\nPlayResX: 3840\nPlayResY: 2160\n\n[V4+ Styles]\n"
    + "Style: 注释,思源黑体 Heavy,60,&H00FFFFFF,&H000000FF,&H00000000,&H00737375,"
    + "0,0,0,0,100,100,0,0,1,2,2,7,30,30,30,1");
  const head = assHead();
  assert.match(head, /PlayResX: 3840\nPlayResY: 2160/);
  assert.match(head, /\[V4\+ Styles\]\nFormat: Name, Fontname, Fontsize, PrimaryColour,/);
  assert.match(head, /Style: 注释,思源黑体 Heavy,60,/);
});

test("import merges by name and normalises a non-standard Format order", () => {
  const current = "[V4+ Styles]\nStyle: 注释,思源黑体 Heavy,60,&H00FFFFFF,&H000000FF,&H00000000,"
    + "&H00737375,0,0,0,0,100,100,0,0,1,2,2,7,30,30,30,1";
  const incoming = "[V4+ Styles]\nFormat: Name, Fontsize, Fontname\n"
    + "Style: 注释,88,Arial\nStyle: 旁白,44,Verdana\n";
  const { text, added, updated } = mergeStyleText(current, incoming);
  assert.deepEqual(added, ["旁白"]);
  assert.deepEqual(updated, ["注释"]);
  // 导入的两条都按标准 23 字段重排，Format 行里没给的字段用默认值补齐
  assert.match(text, /^Style: 注释,Arial,88,&H00FFFFFF,/m);
  assert.match(text, /^Style: 旁白,Verdana,44,&H00FFFFFF,/m);
  assert.equal(text.split("\n").filter(l => l.startsWith("Style:")).length, 2);
});

test("a file with no styles section leaves the sheet untouched", () => {
  const { added, updated } = mergeStyleText(DEFAULT_STYLE_SHEET,
    "[Events]\nDialogue: 0,0:00:00.00,0:00:01.00,JP,,0,0,0,,x");
  assert.deepEqual(added, []);
  assert.deepEqual(updated, []);
});

test("import keeps the PlayRes the current sheet declared", () => {
  const current = "[Script Info]\nPlayResX: 3840\nPlayResY: 2160\n\n[V4+ Styles]\n";
  const { text } = mergeStyleText(current, "[V4+ Styles]\nFormat: Name, Fontname\nStyle: 旁白,Verdana\n");
  assert.match(text, /PlayResX: 3840\nPlayResY: 2160/);
  assert.match(text, /Style: 旁白,Verdana,70,/);
});
