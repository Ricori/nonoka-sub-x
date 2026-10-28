import { ASS_EVENTS_HEAD, ASS_SCRIPT_INFO } from '../constants';
import { getPlayRes } from '../ass';
import { playStore } from '../store/playStore';
import { buildAss, usedFonts } from './assBuild';
import { loadFontMetrics } from '../../subtitles/metrics';
import { subCanvas, video } from './media';
import { systemFontFile } from '../../bridge/systemFonts';
import JASSUB from "jassub";
import workerUrl from "jassub/dist/worker/worker.js?worker&url";
import webViewWorkerUrl from "../vendor/jassub-worker.js?url";
import wasmUrl from "jassub/dist/wasm/jassub-worker-modern.wasm?url";

const fontUrl = (file: string) => new URL(`fonts/${file}`, document.baseURI).href;

/** 随包字体的族名别名 → 文件。libass 靠 fonts[] 拿到它们，canvas 量度量则要按名注册 */
const BUNDLED_FONT_FILES: Record<string, string> = {
  "方正准圆_gbk": "FZZhunYuan.woff2",
  "方正准圆": "FZZhunYuan.woff2",
  "fz-zhunyuan-gbk": "FZZhunYuan.woff2",
  "荆南波波黑": "JingNanBoBoHei.woff2",
  "jingnanbobohei": "JingNanBoBoHei.woff2",
};

const bundledFontUrl = (name: string): string | null => {
  const file = BUNDLED_FONT_FILES[name.trim().toLowerCase()];
  return file ? fontUrl(file) : null;
};

const bundledFontUrls = [fontUrl("FZZhunYuan.woff2"), fontUrl("JingNanBoBoHei.woff2")];

const isWebView2 = !!(window as Window & { chrome?: { webview?: unknown } }).chrome?.webview;

// 字幕预览：交给 libass 本体（JASSUB/WASM）渲染。以前那套 DOM 叠层怎么调都对不齐。
// JASSUB 打包成 IIFE 挂在全局（file:// 下 <script type="module"> 加载不了）
let jassub: JASSUB | null = null;         // 就绪前所有绘制请求直接丢掉
let jassubLoad: Promise<JASSUB> | null = null;
let lastAss = "";               // 上次喂给 libass 的 ASS：内容没变就只重画，不重建 track
/** 已向当前实例交过（或正在交）的系统字体，小写族名。换实例时清空 */
let claimedFonts = new Set<string>();

/** 随包字体已经在 fonts[] 里了；同一个字体只交一次，免得一份几十 MB 的 CJK 字体重复进 WASM 内存 */
function claimSystemFont(name: string): boolean {
  const key = name.trim().toLowerCase();
  if (!key || bundledFontUrl(key) || claimedFonts.has(key)) return false;
  claimedFonts.add(key);
  return true;
}

/**
 * WASM 里的 libass 看不到系统字体。JASSUB 缺字时会回调 _getLocalFont，原版走
 * queryLocalFonts()，WebView2 里要权限、还拿不到中文族名；改成找 Go 端按族名要文件。
 * 这条路兜的是正文里 n 换的字体——样式用到的由 loadSystemFonts 主动加载，
 * 因为缺字回调是异步的，这一帧已经按回退字体画完了。
 */
class SystemFontJASSUB extends JASSUB {
  override async _getLocalFont(font: string) {
    if (!claimSystemFont(font)) return undefined;
    const data = await systemFontFile(font);
    // worker 拿到文件后才 addFont + reloadFonts，与这里的重画请求不在同一条消息通道上，留点余量再画
    if (data) setTimeout(drawSubs, 120);
    return data ?? undefined;
  }
}

/** 加载遮罩期间先取资源并启动 worker/WASM，字幕数据回来后只需换入正式 track */
export function preloadSubtitles(): Promise<any> {
  if (!jassubLoad) jassubLoad = (async () => {
    lastAss = ASS_SCRIPT_INFO + ASS_EVENTS_HEAD;
    const canvas = subCanvas();
    if (!canvas) throw new Error("字幕预览画布尚未挂载");
    claimedFonts = new Set();
    const inst = new SystemFontJASSUB({
      canvas,
      subContent: lastAss,
      workerUrl: isWebView2 ? webViewWorkerUrl : workerUrl,
      wasmUrl, modernWasmUrl: wasmUrl,
      fonts: bundledFontUrls,
      defaultFont: "方正准圆_gbk",
      queryFonts: "local",
    });
    await inst.ready;
    jassub = inst;
    return inst;
  })();
  return jassubLoad;
}

/** 字幕数据到手后换入正式 track。busy 提示由调用方按返回/抛出来控制 */
export async function initSubtitles(setBusy: (text: string | null) => void) {
  const showBusy = setTimeout(() => setBusy("字幕预览加载中…"), 300);
  try {
    const inst = await preloadSubtitles();
    const ass = buildAss();
    if (ass !== lastAss) {
      lastAss = ass;
      await inst.renderer.setTrack(ass);
    }
    drawSubs();
    setBusy(null);
  } catch (e) {
    // 失败就把提示留在画面上，具体原因交给调用处 toast
    setBusy("字幕预览加载失败");
    throw e;
  } finally {
    clearTimeout(showBusy);
  }
}

/** 字幕内容或模板变了：重建 track（libass 要重新解析整份 ASS） */
export function syncSubs() {
  if (!jassub) return;
  const ass = buildAss();
  if (ass !== lastAss) {
    lastAss = ass;
    jassub.renderer.setTrack(ass).catch(() => { });
  }
  drawSubs();
}

/**
 * 逐字特效自己复算排版坐标，得按字体的真实度量来（见 src/subtitles/metrics.ts）。
 * 字体是异步就绪的，量到之前先用兜底比例，就绪后重排一次预览。
 */
export async function refreshFontMetrics() {
  const fonts = usedFonts();
  const [metricsChanged, fontsAdded] = await Promise.all([
    loadFontMetrics(fonts, bundledFontUrl).catch(() => false),
    loadSystemFonts(fonts).catch(() => false),
  ]);
  if (metricsChanged) syncSubs();
  else if (fontsAdded) drawSubs();
}

/** 样式里用到的系统字体交给 libass；返回是否真加了新字体（加了要重画） */
async function loadSystemFonts(fonts: Set<string>): Promise<boolean> {
  const inst = jassub ?? await jassubLoad?.catch(() => null);
  if (!inst || inst !== jassub) return false;
  const files = await Promise.all([...fonts].filter(claimSystemFont).map(systemFontFile));
  const found = files.filter((data): data is Uint8Array<ArrayBuffer> => !!data);
  // 等字体的工夫可能已经换了实例（关掉又打开了编辑器）
  if (!found.length || inst !== jassub) return false;
  await inst.renderer.addFonts(found);
  return true;
}

// 文本框逐字输入也会改字幕内容，但每敲一个字符就 setTrack 等于让 libass 把整份 ASS
// 重新解析一遍（IME 组字期间 input 事件更密）：合并成一次，末次输入后 80ms 再同步。
let subsSyncTimer: ReturnType<typeof setTimeout> | undefined;
export function syncSubsSoon() {
  clearTimeout(subsSyncTimer);
  subsSyncTimer = setTimeout(syncSubs, 80);
}

export function destroySubtitles() {
  clearTimeout(subsSyncTimer);
  const current = jassub;
  jassub = null;
  jassubLoad = null;
  lastAss = "";
  if (current) void current.destroy();
}

/** 只是时间变了：重画当前帧。JASSUB 内部有 busy 去重，拖播放头时多余的调用会自己丢掉 */
export function drawSubs() {
  if (!jassub) return;
  const v = video();
  const pr = getPlayRes();
  jassub.manualRender({
    mediaTime: playStore.get().t,
    expectedDisplayTime: performance.now(),
    // 视频还没加载时按模板 PlayRes 当画面尺寸，字幕照样能预览
    width: v?.videoWidth || pr.x,
    height: v?.videoHeight || pr.y,
  }, true).catch(() => { });
}
