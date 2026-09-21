import {
  SCRUB_BACK_TOL, SCRUB_CLICK_MS, SCRUB_GRAIN_MS, SCRUB_JUMP, SCRUB_K, SCRUB_LEAD,
  SCRUB_RATE_MAX, SCRUB_RATE_MIN, SCRUB_SYNC_MS, SCRUB_TAIL_MS,
} from '../constants';
import { layoutStore } from '../store/layoutStore';
import { playStore } from '../store/playStore';
import { curSegs, select, selStore } from '../store/selectionStore';
import { setScrollLeft, tlStore } from '../store/tlStore';
import { toast } from '../store/uiStore';
import { viewStore, xOf } from '../store/viewStore';
import { MIN_PIECE, pieceAt } from '../../subtitles/pieces.ts';
import type { Piece } from '../../subtitles/pieces.ts';
import { safeSeekVideo, standbyVideo, swapVideo, video } from './media';
import { drawSubs } from './subtitles';

/** 激活轨里当前播放头落在第几句 */
export function activeIdx(): number {
  const t = playStore.get().t;
  return curSegs().findIndex(s => t >= s.t0 && t < s.t1);
}

/** 播放头变化后的连带更新：重画字幕帧 + 播放中高亮列表当前行 */
function applyPlayhead() {
  drawSubs();
  const { playing } = playStore.get();
  if (!playing) return;
  const i = activeIdx();
  if (i >= 0 && i !== selStore.get().sel) select(i, { scroll: true });
}

/**
 * opt.noVideo：只挪播放头，不写 video.currentTime——擦洗音期间由它自己按
 * 固定间隔对位（见 scrubFollow），这里再写就把声音打断了
 */
export function seek(nt: number, opt?: { noVideo?: boolean }) {
  armedAt = NaN;   // 位置变了，待命的那个得按新位置重新备
  const v = viewStore.get();
  const t = Math.min(Math.max(nt, v.t0), v.t1);
  playStore.set({ t });
  if (!opt?.noVideo) safeSeekVideo(t);
  applyPlayhead();
  const px = xOf(t), { left, w } = tlStore.get();
  if (px < left + 30 || px > left + w - 30) setScrollLeft(Math.max(px - w * 0.3, 0));
}

// ── 播放：<video> 驱动播放头 ─────────────
let raf: number | null = null;

// ── 成片播放的接力 ─────────────────────────
// 跨过删掉的部分时若让正在播的 <video> 当场 seek，它得回到关键帧重新解码，画面和声音会
// 顿一下。改成两个 <video> 接力：离拼接点还有 ARM_AHEAD 秒时让待命的那个先跳到下一段起点
// 解好帧，到点直接对调接着播。待命的没备好（关键帧太稀、还在 seek）就退回当场 seek。
const ARM_AHEAD = 2;
/** 提前这么多秒切：rAF 一帧 16ms，等越过拼接点再切会先漏出一两帧删掉的画面 */
const SWAP_LEAD = 0.03;
let armedAt = NaN;

/** 播放头所在这串首尾相接的片段在哪儿结束，以及下一段（跨过删掉部分之后）从哪开始 */
function nextJoin(pieces: Piece[], t: number): { end: number; next: Piece } | null {
  let j = pieceAt(pieces, t);
  if (j < 0) return null;
  while (j + 1 < pieces.length && pieces[j + 1].t0 - pieces[j].t1 < MIN_PIECE) j++;
  return j + 1 < pieces.length ? { end: pieces[j].t1, next: pieces[j + 1] } : null;
}

function armStandby(at: number) {
  const sb = standbyVideo();
  if (!sb || !sb.src) return;
  try { sb.pause(); sb.currentTime = at; armedAt = at; } catch { armedAt = NaN; }
}

const standbyReady = () => {
  const sb = standbyVideo();
  return !!sb && !sb.seeking && sb.readyState >= 2 && Math.abs(sb.currentTime - armedAt) < 0.1;
};

/** 对调前后台：先换角色再动播放，旧前台的 pause 事件落在「待命」身上，不会被当成停止播放 */
function handOff(): number {
  const old = video(), sb = standbyVideo()!;
  sb.playbackRate = userRateOf();
  swapVideo();
  sb.play().catch(() => { });
  old?.pause();
  const t = sb.currentTime;
  armedAt = NaN;
  return t;
}

function tick() {
  if (!playStore.get().playing) return;
  const v = video();
  if (!v) return;
  let t = v.currentTime;
  // 聚焦时播到末段终点就停：视频本身是整片，不停下来就直接播进成片外面去了
  if (t >= viewStore.get().t1) { setPlaying(false); seek(viewStore.get().t1); return; }
  // 剪过就跳过删掉的部分（完整片、成片都一样，听到的就是成片）：待命的 <video> 备好了就接力，
  // 否则播到空隙里当场跳到下一段起点。后面没有片段了就停在最后一段的终点
  const pieces = viewStore.get().pieces;
  if (pieces?.length) {
    const join = nextJoin(pieces, t);
    if (join) {
      if (join.end - t < ARM_AHEAD && armedAt !== join.next.t0) armStandby(join.next.t0);
      if (t >= join.end - SWAP_LEAD && armedAt === join.next.t0 && standbyReady()) t = handOff();
    }
    if (pieceAt(pieces, t) < 0) {
      const next = pieces.find(p => p.t0 > t);
      if (!next) { setPlaying(false); seek(pieces[pieces.length - 1].t1); return; }
      t = next.t0;
      safeSeekVideo(t);
    }
  }
  playStore.set({ t });
  applyPlayhead();
  const px = xOf(t), { left, w } = tlStore.get();
  if (px > left + w * 0.88) setScrollLeft(px - w * 0.15);
  raf = requestAnimationFrame(tick);
}

export function onPlayUI() {
  playStore.set({ playing: true });
  // 接力对调后新前台会再发一次 play：别让两条 rAF 循环同时跑
  if (raf) cancelAnimationFrame(raf);
  raf = requestAnimationFrame(tick);
}

export function onPauseUI() {
  playStore.set({ playing: false });
  if (raf) cancelAnimationFrame(raf);
  const v = video();
  if (v) playStore.set({ t: v.currentTime });
  applyPlayhead();
}

export function setPlaying(p: boolean) {
  const v = video();
  if (!v) return;
  if (p) {
    // 擦洗音正响着时 video 已经在播，play 事件不会再来一次，UI 得自己同步
    if (cancelScrub() && !v.paused) {
      v.playbackRate = userRateOf();   // 擦洗期间改过速率
      onPlayUI();
      return;
    }
    v.play().catch(() => toast("视频尚未就绪，无法播放"));
  } else {
    cancelScrub();
    v.pause();
  }
}

// ── 播放速率（工具栏选的，擦洗结束后要还回去）──────────────────────
const userRateOf = () => playStore.get().rate;
export function setUserRate(r: number) {
  playStore.set({ rate: r });
  const v = video();
  if (v) v.playbackRate = r;
}

// ── 擦洗音（scrub audio）────────────────
// 拖时间轴/步进时听个响。两条铁律：① 不能每帧写 currentTime，解码器会一直重启、一个音都
// 出不来，所以让 video 自己往下播、只在 scrubFollow 里偶尔对位；② 不能把 video 往回拽，
// 回跳就是重放同一段（听着像卡带），跟不上时改成暂停等。
let scrubPlay = false;
let scrubTimer: ReturnType<typeof setTimeout> | null = null;
let scrubSync: ReturnType<typeof setInterval> | null = null;
let scrubVel = 1, scrubLastT = 0, scrubLastMs = 0, scrubGrainMs = 0;
let scrubPlayed = -1;      // 已经放到的素材位置：跨多次小幅拖动保留，避免重放刚听过的
let scrubWarned = false;   // 没视频时只提示一次，别每帧弹

export const isScrubbing = () => scrubPlay;
export const resetScrubWarned = () => { scrubWarned = false; };
/** 新一次按住拖动 = 新的意图，「这段听过了」只在一次按住期间有效 */
export const resetScrubPlayed = () => { scrubPlayed = -1; };

/**
 * 起播/续命一次擦洗音。返回 true 表示擦洗已接管 video 定位，
 * 调用方这一帧就别再自己写 currentTime 了
 */
export function scrubSound(opt?: { jump?: boolean }): boolean {
  const v = video();
  if (!layoutStore.get().scrubAudio || playStore.get().playing || !v) return false;
  // 声音来自 <video>：没挂视频（只有服务端算的波形）时是真没得响，静悄悄的容易以为坏了。
  // 门槛只能卡到 HAVE_METADATA(1)：拖动中每写一次 currentTime，readyState 就掉到 1，
  // 卡 >=2 的话整个拖动过程一次都进不来
  if (!v.src || v.readyState < 1) {
    if (!scrubWarned) { scrubWarned = true; toast("擦洗音要有已加载的视频才有声音"); }
    return false;
  }
  const now = performance.now();
  const t = playStore.get().t;
  // opt.jump：←/→ 步进这类「挪到新位置」的操作，每次都该在新位置响一声。
  // 它不是连续擦洗，不能受「这段听过了」的约束
  if (opt?.jump) scrubPlayed = t;
  if (!scrubPlay) {
    scrubPlay = true;
    scrubVel = 1; scrubLastT = t; scrubLastMs = now;
    scrubGrainMs = now;
    v.playbackRate = 1;
    scrubSync = setInterval(scrubFollow, SCRUB_SYNC_MS);
    // 起播位置交给 scrubFollow 决定（它会跳过上一次小幅拖动已经放过的那一段）
    scrubFollow();
  } else if (opt?.jump) {
    scrubFollow();   // 别等下一个 50ms 心跳，按一下就响
  } else if (now - scrubLastMs > 30) {
    // 拖动速度（素材秒/真实秒），指数平滑一下，免得抖
    const vel = (t - scrubLastT) / ((now - scrubLastMs) / 1000);
    scrubVel = scrubVel * 0.5 + vel * 0.5;
    scrubLastT = t; scrubLastMs = now;
  }
  if (scrubTimer) clearTimeout(scrubTimer);
  scrubTimer = setTimeout(endScrub, SCRUB_TAIL_MS);
  return true;
}

/**
 * 每 SCRUB_SYNC_MS 跑一次的跟随。不做慢放，只按原速一片一片地放（每片至少
 * SCRUB_GRAIN_MS），追上播放头就停下等——慢拖是稀疏的正常音调，快拖首尾相接成连续声音。
 */
function scrubFollow() {
  if (!scrubPlay) return;
  const v = video();
  if (!v) return;
  const now = performance.now();
  const t = playStore.get().t;
  const e = t - v.currentTime;   // >0：video 落在播放头后面（还有新素材可放）
  // 起播：从「还没听过的地方」开始。scrubPlayed 跨会话保留——每次小幅拖动都是一次
  // 独立会话（停手 140ms 就收声），若每次都从播放头重放，短距离连着拖就会重复
  const start = () => {
    if (!v.paused) return;
    const from = Math.max(t, Math.min(scrubPlayed, t + SCRUB_JUMP));
    // 只往前跳过已听过的那一截，容差要小——放宽到几十毫秒的话，小幅拖动每次
    // 正好差这么点，就又从听过的地方起播了
    if (from - v.currentTime > 0.005) { try { v.currentTime = from; } catch { /* 未就绪 */ } }
    v.play().catch(() => { });
    scrubGrainMs = now;
  };
  // 播放头跳到别处（点标尺/换句）就重新计已听位置
  if (t < scrubPlayed - SCRUB_JUMP || t > scrubPlayed + SCRUB_JUMP) scrubPlayed = t;
  if (!v.paused) scrubPlayed = Math.max(scrubPlayed, v.currentTime);
  if (e > SCRUB_JUMP) {              // 落后太多（拖太快/刚跳过来）：直接对位，往前跳不重放
    safeSeekVideo(t);
    scrubPlayed = t;
    start();
    v.playbackRate = Math.min(Math.max(Math.abs(scrubVel) || 1, SCRUB_RATE_MIN), SCRUB_RATE_MAX);
    return;
  }
  if (scrubVel < -0.02) {
    // 往回拖：<video> 不能倒放，退而求其次——原速往前放一小片，落后一点就跳回
    // 光标处再放一片。手在动，每片素材都不一样，不会听成重放同一句
    v.playbackRate = SCRUB_RATE_MIN;
    if (e < -SCRUB_BACK_TOL) { safeSeekVideo(t); scrubPlayed = t; scrubGrainMs = now; }
    start();
    return;
  }
  if (v.paused) {
    // 只在播放头越过「已听位置」时才起下一片；还在已听范围里就继续等
    if (t > scrubPlayed - SCRUB_LEAD) start();
    return;
  }
  // 放到播放头前面了：这一片够长就停下等，太短先放完（碎成几十毫秒听不出内容）
  if (e < -SCRUB_LEAD && now - scrubGrainMs >= SCRUB_GRAIN_MS) { v.pause(); return; }
  // 拖得比 1× 快就提速跟上（顺带把落后的误差纠掉），最慢也只到 1×，不做慢放拉伸
  const rate = Math.min(Math.max(scrubVel + SCRUB_K * e, SCRUB_RATE_MIN), SCRUB_RATE_MAX);
  if (Math.abs(v.playbackRate - rate) > 0.03) v.playbackRate = rate;
}

/**
 * 点一下（不是拖）：按下到松开只有几十毫秒，松手就收声等于没响——
 * 起播的定位延迟加淡入就把这点时间吃光了。改成固定放一小段
 */
export function scrubBlip() {
  if (!scrubPlay) return;
  if (scrubTimer) clearTimeout(scrubTimer);
  scrubTimer = setTimeout(endScrub, SCRUB_CLICK_MS);
}

export function endScrub() {
  if (!cancelScrub()) return;
  const v = video();
  if (!v) return;
  v.pause();
  v.playbackRate = userRateOf();   // 把工具栏选的速率还回去
  // 擦洗时 video 自己往下跑了一截，退回播放头。pause 事件是异步派发的，
  // 等它跑时 currentTime 已经退回来，不会把 t 往前带
  safeSeekVideo(playStore.get().t);
}

/** 收摊：留着定时器会在真正播放到一半时把 video 停掉。返回擦洗当时是否在响 */
export function cancelScrub(): boolean {
  if (scrubTimer) clearTimeout(scrubTimer);
  if (scrubSync) clearInterval(scrubSync);
  scrubTimer = scrubSync = null;
  const was = scrubPlay;
  scrubPlay = false;
  return was;
}
