import assert from "node:assert/strict";
import test from "node:test";

import { documentAss, documentSrt } from "../src/subtitles/document.ts";
import { DEFAULT_STYLE_SHEET } from "../src/subtitles/constants.ts";
import {
  karaokeFromDuration, karaokeFromWords, karaokeMatches, karaokeTimeline, normalizeWords,
} from "../src/subtitles/karaoke.ts";
import { defaultEffectParams, resolveLaneEffects } from "../src/subtitles/effects.ts";

// 编辑器（docStore）和插件宿主（服务端读回的 EditDocument）现在共用 src/subtitles
// 这一条拼装管线，所以这里盯的是管线本身的口径：谁贴边、谁不出、重叠怎么钳。

const document = {
  schema: 1,
  video_id: "loc_fixture",
  title: "fixture.mp4",
  source: "task",
  fp: null,
  rev: 4,
  subtitles: [
    { t0: 1.2, t1: 3.5, ja: "こんにちは", zh: "你好" },
    { t0: 3.4, t1: 5, ja: "二行目", zh: "第二行\n继续" },
  ],
  tracks: [{
    id: "tr1",
    name: "注释",
    ja: { hidden: true, style: "origin" },
    zh: { hidden: false, style: "note" },
    hja: 48,
    hzh: 48,
    segs: [{ t0: 6, t1: 7, ja: "隠れる", zh: "旁白" }],
  }],
  track_meta: { name: "默认轨", ja: { hidden: false, style: "origin" }, zh: { hidden: false, style: "cn" } },
  projection: { schema: 1, mode: "final" },
};

test("文档拼出的 ASS 只出未隐藏的 lane，并钳掉同线相邻句的重叠", () => {
  const ass = documentAss(document, DEFAULT_STYLE_SHEET);
  assert.match(ass, /Dialogue: 0,0:00:01\.20,0:00:03\.40,cn,默认轨,0,0,0,,你好/);
  assert.match(ass, /Dialogue: 0,0:00:01\.20,0:00:03\.40,origin,默认轨,0,0,0,,こんにちは/);
  assert.match(ass, /Dialogue: 0,0:00:06\.00,0:00:07\.00,note,注释,0,0,0,,旁白/);
  // 藏起来的原文 lane 不出图
  assert.ok(!ass.includes("隠れる"));
  // 1.2–3.5 撞上 3.4 开口的下一句，前句出点被钳到 3.40
  assert.ok(!ass.includes("0:00:03.50"));
  // 换行转义，译文在前的堆叠顺序保持不变
  assert.ok(ass.indexOf("第二行\\N继续") < ass.indexOf("二行目"));
});

test("样式表为空时回落到种子，origin/cn 始终存在", () => {
  const seeded = documentAss(document, "   ");
  assert.match(seeded, /\[V4\+ Styles\]\nFormat: Name, Fontname, Fontsize, PrimaryColour,/);
  assert.match(seeded, /Style: origin,/);
  assert.match(seeded, /Style: cn,/);
  assert.match(seeded, /Style: note,/);
});

test("绑到本机没有的样式回退 origin/cn，整条线不会消失", () => {
  const bound = { ...document, track_meta: { name: "默认轨", ja: { hidden: false, style: "不存在" }, zh: { hidden: false, style: "也不存在" } } };
  const ass = documentAss(bound, DEFAULT_STYLE_SHEET);
  assert.match(ass, /,origin,默认轨,0,0,0,,こんにちは/);
  assert.match(ass, /,cn,默认轨,0,0,0,,你好/);
});

test("统一特效绑定按 lane 覆盖 track/all", () => {
  const affected = {
    ...document,
    effects: [
      { id: "all", templateId: "fade", enabled: true, target: { scope: "all" }, params: { inMs: 100, outMs: 100 } },
      { id: "ja", templateId: "fade", enabled: true, target: { scope: "lane", trackId: "default", lang: "ja" }, params: { inMs: 350, outMs: 50 } },
    ],
  };
  const ass = documentAss(affected, DEFAULT_STYLE_SHEET);
  assert.match(ass, /,origin,默认轨,0,0,0,,\{\\fad\(350,50\)\}こんにちは/);
  assert.match(ass, /,cn,默认轨,0,0,0,,\{\\fad\(100,100\)\}你好/);
});

test("逐字粒子模板稳定展开主体和矢量粒子事件", () => {
  const particleDocument = {
    ...document,
    subtitles: [{ t0: 1, t1: 3, ja: "星空", zh: "" }],
    tracks: [],
    track_meta: { name: "默认轨", ja: { hidden: false, style: "origin" }, zh: { hidden: true, style: "cn" } },
    effects: [{
      id: "particle", templateId: "character-particle", enabled: true,
      target: { scope: "lane", trackId: "default", lang: "ja" },
      params: { staggerMs: 80, enterMs: 300, scaleFrom: 70, particleCount: 2, particleDurationMs: 600, scatterPx: 120 },
    }],
  };
  const first = documentAss(particleDocument, DEFAULT_STYLE_SHEET);
  const second = documentAss(particleDocument, DEFAULT_STYLE_SHEET);
  assert.equal(first, second, "固定输入必须生成固定粒子，保证预览与导出一致");
  assert.equal((first.match(/^Dialogue:/gm) ?? []).length, 6, "2 个字 ×（1 主体 + 2 粒子）");
  assert.match(first, /Dialogue: 2,.*\{\\an5\\pos\([^}]+\\t\([^}]+\}星/);
  assert.match(first, /Dialogue: 1,.*\\move\([^}]+\\p1\}m 0 0 l 4 5/);
});

test("区间 ASS 只留相交的行并把时间轴平移到 0", () => {
  const clip = documentAss(document, DEFAULT_STYLE_SHEET, { t0: 3.4, t1: 7 });
  assert.ok(!clip.includes("你好"));
  assert.match(clip, /Dialogue: 0,0:00:00\.00,0:00:01\.60,cn,默认轨,0,0,0,,第二行\\N继续/);
  assert.match(clip, /Dialogue: 0,0:00:02\.60,0:00:03.60,note,注释/);
});

test("SRT 摊平成一条时间流，按语言筛选并重排序号", () => {
  assert.match(documentSrt(document, "both"), /^1\n00:00:01,200 --> 00:00:03,500\n你好\nこんにちは\n/);
  assert.ok(!documentSrt(document, "zh").includes("こんにちは"));
  assert.ok(!documentSrt(document, "ja").includes("你好"));
  // 藏起来的 lane 在 SRT 里同样不出
  assert.ok(!documentSrt(document, "both").includes("隠れる"));
  assert.match(documentSrt(document, "both", { t0: 6, t1: 7 }), /^1\n00:00:00,000 --> 00:00:01,000\n旁白\n\n$/);
});

// ── 逐字特效：排版度量与 K 轴 ─────────────────────────────────────

const lyricDocument = {
  ...document,
  subtitles: [{
    t0: 1.26, t1: 4.5, ja: "青い帰り道", zh: "",
    words: [{ word: "青い", start: 1.26, end: 1.98 }, { word: "帰り道", start: 1.98, end: 4.5 }],
  }],
  tracks: [],
  track_meta: { name: "默认轨", ja: { hidden: false, style: "origin" }, zh: { hidden: true, style: "cn" } },
};

const posOf = (ass) => [...ass.matchAll(/\\pos\((\d+),(\d+)\)/g)].map(m => [+m[1], +m[2]]);
const startsOf = (ass, layer) =>
  [...ass.matchAll(new RegExp(`^Dialogue: ${layer},([^,]+),`, "gm"))].map(m => m[1]);

test("逐字排版按 libass 的 Fontsize 口径：字距=0.874×字号+Spacing，行高=字号", () => {
  const ass = documentAss({
    ...lyricDocument,
    effects: [{ id: "fx", templateId: "character-particle", enabled: true,
      target: { scope: "all" }, params: { particleCount: 0 } }],
  }, DEFAULT_STYLE_SHEET);
  const positions = posOf(ass);
  assert.equal(positions.length, 5);
  // JP 样式 Fontsize 70 / Spacing 7：libass 把 asc+desc 缩到 70，全角步进只有 61.2
  assert.equal(positions[1][0] - positions[0][0], 68);
  // 行高就是字号本身，贴顶（align 8、MarginV 30）时字的中心在 30+35
  assert.ok(positions.every(([, y]) => y === 65));
  // 整行在 [MarginL, PlayResX-MarginR] 之间居中
  assert.equal(positions[0][0] + positions[4][0], 1920);
});

test("自动 K 轴按词边界切段，一个词一段；摊成逐字时同词共用窗口", () => {
  const units = karaokeFromWords("青い帰り道", 1.26, 4.5,
    normalizeWords(lyricDocument.subtitles[0].words));
  // 两个词 → 两段，而不是五个字五段
  assert.deepEqual(units.map(unit => unit.text), ["青い", "帰り道"]);
  assert.equal(units[0].t0, 1.26);
  assert.equal(units[0].t1, 1.98);
  assert.equal(units[1].t1, 4.5);

  // 逐字特效拿到的是摊平后的逐字时间轴：一个词里的字共用这个词的窗口
  const timeline = karaokeTimeline("青い帰り道", 1.26, 4.5, units);
  assert.equal(timeline.fromK, true);
  assert.deepEqual(timeline.units.map(unit => [unit.text, unit.t0, unit.t1]), [
    ["青", 1.26, 1.98], ["い", 1.26, 1.98],
    ["帰", 1.98, 4.5], ["り", 1.98, 4.5], ["道", 1.98, 4.5],
  ]);

  // 按字数均分仍然是逐字的（它本来就不声称知道词边界）
  assert.equal(karaokeFromDuration("青い帰り道", 1.26, 4.5).length, 5);
});

test("K轴逐字粒子：整行在句首前逐字入场，脉冲落在各自的 K 轴时刻", () => {
  const units = karaokeFromWords("青い帰り道", 1.26, 4.5,
    normalizeWords(lyricDocument.subtitles[0].words));
  const binding = (params) => ({ id: "fx", templateId: "karaoke-particle", enabled: true,
    target: { scope: "all" }, params: { particleCount: 0, ...params } });

  // 入场错峰：靠前的字先出现，最后一个字正好在句首（对齐 Lullamoon 的 50*(i-n)）
  const staggered = documentAss({
    ...lyricDocument,
    subtitles: [{ ...lyricDocument.subtitles[0], k: units }],
    effects: [binding({ entryStaggerMs: 50 })],
  }, DEFAULT_STYLE_SHEET);
  assert.deepEqual(startsOf(staggered, 2),
    ["0:00:01.06", "0:00:01.11", "0:00:01.16", "0:00:01.21", "0:00:01.26"]);

  // 脉冲的起点（相对事件起点的毫秒偏移）才是跟着 K 轴走的那个量
  const popsOf = (ass) => [...ass.matchAll(/\\t\((\d+),\d+,\\fscx/g)].map(m => +m[1])
    .filter((_, index) => index % 2 === 0);
  const withK = documentAss({
    ...lyricDocument,
    subtitles: [{ ...lyricDocument.subtitles[0], k: units }],
    effects: [binding({ entryStaggerMs: 0 })],
  }, DEFAULT_STYLE_SHEET);
  // 逐词：青い 同时脉冲，帰り道 同时脉冲
  assert.deepEqual(popsOf(withK), [0, 0, 720, 720, 720]);

  // 没有 K 轴：整句按字数均分，每字 0.648s
  const without = documentAss({
    ...lyricDocument, effects: [binding({ entryStaggerMs: 0 })],
  }, DEFAULT_STYLE_SHEET);
  assert.deepEqual(popsOf(without), [0, 648, 1296, 1944, 2592]);
});

test("脉冲单程默认压在 280ms；上限设 0 则回到 Aegisub 模板的「跟随音节」口径", () => {
  // lyric.ass 第一句的手打 K 轴：{\kf46}青{\kf26}い{\kf118}帰{\kf28}り{\kf106}道
  const kf = [46, 26, 118, 28, 106], chars = "青い帰り道";
  let cursor = 1.26;
  const units = kf.map((cs, index) => {
    const unit = { t0: cursor, t1: cursor + cs / 100, text: chars[index] };
    cursor = unit.t1;
    return unit;
  });
  const risesOf = (params) => {
    const ass = documentAss({
      ...lyricDocument,
      subtitles: [{ t0: 1.26, t1: 4.5, ja: chars, zh: "", k: units }],
      effects: [{ id: "fx", templateId: "karaoke-particle", enabled: true, target: { scope: "all" },
        params: { ...defaultEffectParams("karaoke-particle"), particleCount: 0, ...params } }],
    }, DEFAULT_STYLE_SHEET);
    return ass.split("\n").filter(line => line.includes(",origin,")).map(line => {
      const [, from, to] = /\\t\((\d+),(\d+),\\fscx/.exec(line);
      return +to - +from;
    });
  };
  // 上限 0：\t(0,!line.duration/2!) 里 line.duration 是 retime 后的音节+停留，与模板逐字一致
  assert.deepEqual(risesOf({ popMs: 0 }), [430, 330, 790, 340, 730]);
  // 默认 280：全部压到 280，长音不再拖沓
  assert.deepEqual(risesOf({}), [280, 280, 280, 280, 280]);
  // 上限只封顶、不拉长：音节本身比上限还短时照旧跟着音节走
  assert.deepEqual(risesOf({ holdMs: 0, popMs: 280 }), [230, 130, 280, 140, 280]);
});

test("K轴逐字粒子的碎片保持大小，并在音节结束后一起散尽", () => {
  const units = karaokeFromWords("青い帰り道", 1.26, 4.5,
    normalizeWords(lyricDocument.subtitles[0].words));
  const ass = documentAss({
    ...lyricDocument,
    subtitles: [{ ...lyricDocument.subtitles[0], k: units }],
    effects: [{ id: "fx", templateId: "karaoke-particle", enabled: true, target: { scope: "all" },
      params: { particleCount: 3, spawnMs: 400, particleLifeMs: 1500, particleScale: 110 } }],
  }, DEFAULT_STYLE_SHEET);
  const shards = ass.split("\n").filter(line => line.includes("\\p1")).slice(0, 3);
  // 同一个字的碎片错峰出现，但一起在「K 段结束 + 余生」处散尽（青 所在的词 青い 到 1.98）
  assert.deepEqual(shards.map(line => /^Dialogue: 1,[^,]+,([^,]+),/.exec(line)[1]),
    ["0:00:03.48", "0:00:03.48", "0:00:03.48"]);
  assert.ok(shards[0].startsWith("Dialogue: 1,0:00:01.26,"));
  assert.ok(shards[1].startsWith("Dialogue: 1,0:00:01.46,"));
  // 起点 ≈90%、终点 ≈粒子大小：缩到十几个百分点就看不见星星了
  for (const line of shards) {
    const from = +/\\fscx(\d+)\\fscy\d+\\t\(/.exec(line)[1];
    const to = +/0\.2,\\fscx(\d+)/.exec(line)[1];
    assert.equal(from, 99);
    assert.ok(to >= 106 && to <= 110, `终点缩放 ${to} 应保持在粒子大小附近`);
  }
});

test("K轴逐字粒子的变色与单向散布对齐模板；颜色留空则跟随样式", () => {
  const build = (params) => documentAss({
    ...lyricDocument,
    subtitles: [{ t0: 1.26, t1: 4.5, ja: "青い帰り道", zh: "" }],
    effects: [{ id: "fx", templateId: "karaoke-particle", enabled: true, target: { scope: "all" },
      params: { ...defaultEffectParams("karaoke-particle"), ...params } }],
  }, DEFAULT_STYLE_SHEET);

  // 落回原大小的那段渐变里换色，与模板的 \1c&HDCC8AE&\3c&HFFFFFF& 同值
  const withColor = build({ particleCount: 2 });
  assert.ok(withColor.includes("\\blur4\\1c&HDCC8AE&\\3c&HFFFFFF&)"));
  // 粒子描边色默认留空 = 跟随样式，不写 \3c
  const shards = withColor.split("\n").filter(line => line.includes("\\p1"));
  assert.ok(shards.length);
  assert.ok(shards.every(line => !line.includes("\\3c")));
  assert.ok(shards.every(line => line.includes("\\3a&HAA&")));
  // 给了粒子描边色就写上（#2093ef 正是模板里的 &HEF9320&）
  assert.ok(build({ particleCount: 2, particleOutline: "#2093ef" })
    .split("\n").filter(line => line.includes("\\p1"))
    .every(line => line.includes("\\3c&HEF9320&\\3a&HAA&")));
  // 主体颜色留空则一个颜色标签都不写，配色完全交给样式
  const bare = build({ particleCount: 0, swapPrimary: "", swapOutline: "" });
  assert.ok(!bare.includes("\\1c"));
  assert.ok(!bare.includes("\\3c"));

  // 横向散布是单向的（模板：$center - math.random(300)），碎片只往一侧甩
  for (const line of shards) {
    const [, x0, , x1] = /\\move\((\d+),(\d+),(\d+),/.exec(line);
    assert.ok(+x1 <= +x0, `碎片终点 ${x1} 不应越过起点 ${x0}`);
  }
});

test("原文改过、K 轴字数对不上时忽略 K 轴，不会错位到别的字上", () => {
  const stale = karaokeFromWords("青い帰り道", 1.26, 4.5,
    normalizeWords(lyricDocument.subtitles[0].words));
  assert.ok(!karaokeMatches(stale, "青い道"));
  const timeline = karaokeTimeline("青い道", 1.26, 4.5, stale);
  assert.equal(timeline.fromK, false);
  assert.equal(timeline.units.length, 3);
});

test("K 轴只描述原文：同一份 K 轴不会被译文轴拿去用", () => {
  const both = {
    ...lyricDocument,
    subtitles: [{ ...lyricDocument.subtitles[0], zh: "蓝色归途", k: karaokeFromWords("青い帰り道", 1.26, 4.5,
      normalizeWords(lyricDocument.subtitles[0].words)) }],
    track_meta: { name: "默认轨", ja: { hidden: false, style: "origin" }, zh: { hidden: false, style: "cn" } },
    effects: [{ id: "fx", templateId: "karaoke-particle", enabled: true,
      target: { scope: "all" }, params: { particleCount: 0, entryStaggerMs: 0 } }],
  };
  const ass = documentAss(both, DEFAULT_STYLE_SHEET);
  const zhPops = [...ass.split("\n").filter(line => line.includes(",cn,")).join("\n")
    .matchAll(/\\t\((\d+),\d+,\\fscx/g)].map(m => +m[1]).filter((_, index) => index % 2 === 0);
  // 译文 4 个字均分 1.26–4.50（每字 0.81s），与原文那 5 个字的 K 轴无关
  assert.deepEqual(zhPops, [0, 810, 1620, 2430]);
});

test("一条 lane 只解析出一个生成型模板，范围更具体的赢；整句变换仍可叠加", () => {
  const bind = (templateId, target) => ({ id: templateId, templateId, enabled: true, target, params: {} });
  const bindings = [
    bind("fade", { scope: "all" }),
    bind("character-particle", { scope: "all" }),
    bind("karaoke-particle", { scope: "lane", trackId: "default", lang: "ja" }),
  ];
  const ja = resolveLaneEffects(bindings, "default", "ja").map(item => item.templateId);
  // 原文轴上 K轴逐字粒子 更具体，逐字粒子被挤掉；fade 是整句变换，照旧叠加
  assert.deepEqual(ja.sort(), ["fade", "karaoke-particle"]);
  // 译文轴上只命中「全部字幕轴」那条逐字粒子
  assert.deepEqual(resolveLaneEffects(bindings, "default", "zh").map(item => item.templateId).sort(),
    ["character-particle", "fade"]);
});

test("两个生成型模板都绑在同一条 lane 上时，只有一个真的展开逐字事件", () => {
  const both = {
    ...lyricDocument,
    effects: [
      { id: "a", templateId: "character-particle", enabled: true, target: { scope: "all" }, params: { particleCount: 0 } },
      { id: "b", templateId: "karaoke-particle", enabled: true, target: { scope: "all" }, params: { particleCount: 0 } },
    ],
  };
  const ass = documentAss(both, DEFAULT_STYLE_SHEET);
  // 5 个字，每个字一条主体事件——两个模板都展开的话会是 10 条
  assert.equal(ass.split("\n").filter(line => line.startsWith("Dialogue:")).length, 5);
});
