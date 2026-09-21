// 编辑器里的各种阈值/上下限常量。数值与旧版逐个对齐，改动会直接改变手感。

export const MIN_DUR = 0.3;

// 时间轴缩放上下限（像素/秒）
export const ZOOM_FLOOR = 18;
export const ZOOM_MAX = 400;

// 波形显示增益：0 = 自动（按整条音轨峰值顶满）
export const WAVE_GAIN_MAX = 24;
export const WAVE_GAIN_STEPS = [1, 1.5, 2, 3, 4, 6, 8, 12, 16, 24];

// 行高上下限与默认值；隐藏轨压成 22px 标签行
export const ROW_MIN = 36;
export const ROW_MAX = 60;
export const SPECTRUM_ROW_H0 = 140;
export const WAVE_ROW_MAX = 240;
export const ROW_H0 = 48;
export const HIDDEN_H = 22;
/** 末轨之后留一段空，留出继续往下拖的余量 */
export const PAD_Y = 16;

export const RULER_H0 = 26;

// 视频轨：缩略图条的行高上下限
export const VIDEO_ROW_MIN = 36;
export const VIDEO_ROW_MAX = 96;
export const VIDEO_ROW_H0 = 52;
/** 片段边缘的拖动热区（像素） */
export const PIECE_EDGE_W = 6;

// 字幕块渲染
export const BLK_DETAIL_W = 12;   // 窄于此就不画手柄和文字
export const BLK_MARGIN = 1;      // 视口左右各多画一屏当缓冲
export const AGG_W = 7;           // 窄于此的连片块并成一条密度块
export const AGG_SPAN = 100;      // 单条密度块最多覆盖这么宽

// 列表虚拟化：上下各多渲染一屏
export const LIST_MARGIN = 1;

export const HISTORY_MAX = 60;
export const AUTOSAVE_MS = 5 * 60 * 1000;

// ── 擦洗音（scrub audio）────────────────
export const SCRUB_SYNC_MS = 50;     // 跟随（测速/纠偏）的间隔
export const SCRUB_TAIL_MS = 140;    // 停手/停止步进后多久收声
export const SCRUB_JUMP = 0.6;       // 差这么多秒以上算「跳了」，直接对位
export const SCRUB_BACK_TOL = 0.25;  // 往回拖时落后这么多就跳回光标处
export const SCRUB_K = 2.5;          // 位置误差纠偏系数
export const SCRUB_RATE_MIN = 0.8;
export const SCRUB_RATE_MAX = 4;
export const SCRUB_GRAIN_MS = 200;   // 一片至少放这么久
export const SCRUB_LEAD = 0.12;      // 允许 video 超前播放头这么多秒
export const SCRUB_CLICK_MS = 40;    // 点一下（没拖动）时响多久

/** 轨道 lane 的兜底主题色（未绑样式时用），统一 rgb() 形式便于拆出 "r,g,b" */
export const TRACK_PALETTE = [
  "rgb(217,140,179)", "rgb(201,168,107)", "rgb(143,125,224)",
  "rgb(111,191,90)", "rgb(91,192,217)", "rgb(224,151,91)",
];

// ASS 骨架常量搬到了 src/subtitles，编辑器与插件宿主共用同一份；这里按原名再导出，
// 编辑器内部的引用路径不变。
export {
  ASS_SCRIPT_INFO,
  ASS_EVENTS_HEAD,
  ASS_STYLE_FORMAT,
  BUILTIN_ASS_STYLES,
  DEFAULT_USER_ASS_STYLES,
  DEFAULT_STYLE_SHEET,
  ASS_FMT_DEFAULT,
} from '../subtitles/constants.ts';

/** 随包自带的字体（导出时整个 fonts/ 都会喂给 ffmpeg），缺字检测里直接放行；比对不分大小写 */
export const BUNDLED_FONTS = ["方正准圆_gbk", "方正准圆", "FZ-ZhunYuan-GBK",
  "荆南波波黑", "JingNanBoBoHei"];

export const LAYOUT_KEY = "ytEditorLayout";

/** 横向滚动条高度：在轨道区底部预留同高沟槽，避免滚动条盖住最后一条轨道 */
export const SB = typeof document === "undefined" ? 0 : (() => {
  const d = document.createElement("div");
  d.style.cssText = "position:absolute;top:-9999px;overflow-x:scroll;width:100px;height:100px;";
  document.body.appendChild(d);
  const h = d.offsetHeight - d.clientHeight;
  d.remove();
  return h;
})();
