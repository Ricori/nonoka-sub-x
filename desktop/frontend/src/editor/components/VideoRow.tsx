import { useEffect, useState } from 'react';
import { shallowEqual } from '../../home/lib/createStore';
import { PIECE_EDGE_W } from '../constants';
import { video } from '../lib/media';
import { endScrub, resetScrubPlayed, scrubBlip, scrubSound, seek } from '../lib/playback';
import {
  cutAtPlayhead, deletePiece, deleteRange, dragPieceEdge, hasContiguous, keepPiece, keepRange,
  mergeContiguous, resetVideoEdit, restoreRange, selectPiece,
} from '../lib/videoEdit';
import { deselect } from '../store/selectionStore';
import { innerLeft } from '../store/tlStore';
import { showCtx } from '../store/uiStore';
import { videoStore } from '../store/videoStore';
import { clearVsel, setVideoActive, vselStore } from '../store/vselStore';
import { curPieces, isFocused, tAtClientX, tOf, viewStore, wOf, xOf } from '../store/viewStore';
import { getVid } from '../session';
import { errText, fmtLen } from '../utils';
import { MIN_PIECE, pieceAt } from '../../subtitles/pieces.ts';
import type { Piece } from '../../subtitles/pieces.ts';
import type { CtxItem } from '../types';
import { mediaLibrary } from '../../bridge/library.ts';

// ── 缩略图条 ──────────────────────────────────────────────────────
// 按缩放挑一档采样间隔：一格不宽于一帧，于是帧挨着帧铺满整条、没有缝，一帧比格子宽的
// 部分左右各裁一点。每 16 帧拼成一张图、按固定网格对齐，缩放回到同一档、滚回同一段都能
// 直接命中缓存。

const STEPS = [0.1, 0.2, 0.25, 0.5, 1, 2, 3, 5, 10, 15, 30, 60, 120, 300, 600];
const TILE_FRAMES = 16;
// 聚焦成片时视口里的原片跨度会被删掉的部分撑大，多留几张
const DOM_TILES = 16;

interface FilmTile { url: string; start: number; step: number; frames: number }

const filmCache = new Map<string, FilmTile>();
const filmLoads = new Map<string, Promise<FilmTile>>();
const filmKey = (src: string, start: number, step: number, frames: number) =>
  `${getVid()}|${src}|${start}|${step}|${frames}`;

function loadFilmTile(src: string, start: number, step: number, frames: number, duration: number) {
  const key = filmKey(src, start, step, frames);
  const cached = filmCache.get(key);
  if (cached) return Promise.resolve(cached);
  let pending = filmLoads.get(key);
  if (!pending) {
    const request = mediaLibrary.filmstripTile(getVid(), start, duration, frames)
      .then(tile => {
        const out = { url: tile.url, start, step, frames };
        filmCache.set(key, out);
        return out;
      })
      .finally(() => filmLoads.delete(key));
    filmLoads.set(key, request);
    pending = request;
  }
  return pending;
}

/**
 * 每张图覆盖 16 帧；末尾那张按剩余时长少取几帧。不足一格的尾巴也要一帧，否则片尾一截
 * 是空的——这一格越过了片尾，后端会把采样点收回到片尾之内。
 */
function tilePlan(start: number, step: number, mediaDur: number) {
  const frames = Math.max(1, Math.min(TILE_FRAMES, Math.ceil((mediaDur - start) / step - 1e-6)));
  return { frames, duration: frames * step };
}

function Filmstrip({ height, left, w, pps }: { height: number; left: number; w: number; pps: number }) {
  const src = videoStore.use(s => s.src);
  const { duration, t0, pieces, focus } = viewStore.use(
    s => ({ duration: s.duration, t0: s.t0, pieces: s.pieces, focus: s.focus }), shallowEqual);
  const [tiles, setTiles] = useState<FilmTile[]>([]);
  const [message, setMessage] = useState("");
  const v = video();
  const aspect = v && v.videoWidth && v.videoHeight ? v.videoWidth / v.videoHeight : 16 / 9;
  const fh = Math.max(12, height - 10);
  const fw = fh * aspect;
  // 一格不超过一帧宽里最宽的那档：格子越接近一帧宽，裁掉的越少
  const fit = STEPS.filter(s => s * pps <= fw);
  const step = fit.length ? fit[fit.length - 1] : STEPS[0];

  useEffect(() => {
    let disposed = false;
    const timer = window.setTimeout(async () => {
      if (!src) { setMessage(""); return; }
      const span = TILE_FRAMES * step;
      const a = Math.max(0, tOf(left - w * .25));
      const b = Math.min(duration, tOf(left + w * 1.25));
      const starts: number[] = [];
      for (let at = Math.floor(a / span) * span; at < b && at < duration - 0.1; at += span) starts.push(at);
      const center = (a + b) / 2;
      const keep = (list: FilmTile[]) => list
        .filter(t => t.step === step)
        .sort((x, y) => Math.abs(x.start - center) - Math.abs(y.start - center))
        .slice(0, DOM_TILES)
        .sort((x, y) => x.start - y.start);
      const cached = starts.map(at => filmCache.get(filmKey(src, at, step, tilePlan(at, step, duration).frames)))
        .filter((t): t is FilmTile => !!t);
      setTiles(cur => keep([...cur.filter(t => !cached.includes(t)), ...cached]));
      let failure: unknown;
      for (const at of starts) {
        const plan = tilePlan(at, step, duration);
        if (filmCache.has(filmKey(src, at, step, plan.frames))) continue;
        try {
          const tile = await loadFilmTile(src, at, step, plan.frames, plan.duration);
          if (disposed) return;
          setTiles(cur => keep([...cur.filter(t => t !== tile), tile]));
        } catch (error) {
          failure = error;
          break;
        }
      }
      if (!disposed) setMessage(failure ? "缩略图生成失败：" + errText(failure) : "");
    }, 120);
    return () => { disposed = true; clearTimeout(timer); };
  }, [left, w, step, src, duration, t0, pieces, focus]);

  // 每帧占它那一格在时间轴上的实际宽度（聚焦成片时落在删掉部分里的帧宽度为 0，不画）。
  // 格子比一帧窄就居中裁：左右各露掉一点，画面主体还在；只有放大到顶时格子才可能比一帧宽，
  // 那时只画一帧宽，不然会露出拼图里下一帧的开头
  return <>
    {tiles.flatMap(tile => Array.from({ length: tile.frames }, (_, k) => {
      const at = tile.start + k * tile.step;
      const slot = wOf(at, at + tile.step);
      if (slot < 1) return [];
      const wNow = Math.min(slot, fw);
      return [
        <div key={`${tile.start}-${k}`} className="film-frame" style={{
          left: xOf(at), width: wNow, height: fh,
          backgroundImage: `url(${tile.url})`,
          backgroundSize: `${tile.frames * fw}px ${fh}px`,
          backgroundPosition: `${-k * fw - (fw - wNow) / 2}px 0`,
        }} />,
      ];
    }))}
    {message && <span className="film-message">{message}</span>}
  </>;
}

// ── 片段层 ────────────────────────────────────────────────────────

/** 片段之间（以及首段之前、末段之后）被删掉的空隙。片段按毫秒取整，片尾会差出一丝，不算空隙 */
function gapsOf(pieces: Piece[], duration: number): Piece[] {
  const out: Piece[] = [];
  let cur = 0;
  for (const p of pieces) {
    if (p.t0 - cur >= MIN_PIECE) out.push({ t0: cur, t1: p.t0 });
    cur = p.t1;
  }
  if (duration - cur >= MIN_PIECE) out.push({ t0: cur, t1: duration });
  return out;
}

const CUT_HINT = "在播放头处切分 (Ctrl+B)";

function pieceMenu(e: React.MouseEvent, i: number) {
  const edited = !!viewStore.get().pieces;
  const items: CtxItem[] = [
    { label: CUT_HINT, onClick: cutAtPlayhead },
    "-",
    { label: "删除此片段 (Delete)", onClick: () => deletePiece(i) },
    { label: "只保留此片段", onClick: () => keepPiece(i) },
  ];
  if (edited) items.push("-", ...wholeItems());
  showCtx(e, items);
}

/** 菜单末尾的整体操作：合并连续片段（有切分点时才给）+ 恢复整片 */
function wholeItems(): CtxItem[] {
  const items: CtxItem[] = [];
  if (hasContiguous()) items.push({ label: "合并连续片段", onClick: mergeContiguous });
  items.push({ label: "恢复整片", onClick: resetVideoEdit });
  return items;
}

function gapMenu(e: React.MouseEvent, g: Piece) {
  showCtx(e, [
    { label: `恢复这段（${fmtLen(g.t1 - g.t0)}）`, onClick: () => restoreRange(g.t0, g.t1) },
    "-",
    ...wholeItems(),
  ]);
}

function rangeMenu(e: React.MouseEvent, a: number, b: number) {
  showCtx(e, [
    { label: `删除所选时间段（${fmtLen(b - a)}）`, onClick: () => deleteRange(a, b) },
    { label: "只保留所选时间段", onClick: () => keepRange(a, b) },
  ]);
}

/**
 * 视频轨：缩略图条 + 片段。完整片里点片段选中并跳到那里，拖出一段时间做范围选中，
 * 拖片段两端改范围，右键更多；剪辑只改片段，字幕不动。成片视图里它只是一条预览，
 * 不响应任何操作（跳播放头用标尺）。
 */
export function VideoRow({ height, left, w }: { height: number; left: number; w: number }) {
  const { pps, duration } = viewStore.use(
    s => ({ pps: s.pps, t0: s.t0, t1: s.t1, duration: s.duration, pieces: s.pieces }), shallowEqual);
  const vsel = vselStore.use(s => s.vsel);
  const pieces = curPieces();
  const edited = !!viewStore.get().pieces;
  const focused = isFocused();
  const gaps = edited ? gapsOf(pieces, duration) : [];

  /** 按下：没动就是点选（片段选中 + 跳播放头），动了就是拖一段时间出来 */
  function onDown(e: React.PointerEvent) {
    // 成片里只看不点：把事件吞掉，免得落到时间轴上变成框选
    if (focused) { e.stopPropagation(); return; }
    e.stopPropagation();
    // 点了视频轨（左右键都算）：工具栏「切分」从此切视频，直到再去点字幕
    setVideoActive(true);
    if (e.button !== 0) return;
    const el = e.currentTarget as HTMLElement;
    el.setPointerCapture(e.pointerId);
    const v = viewStore.get();
    const clampT = (t: number) => Math.min(Math.max(t, v.t0), v.t1);
    const ta = clampT(tAtClientX(e.clientX));
    const x0 = e.clientX;
    let dragging = false;
    resetScrubPlayed();
    const move = (ev: PointerEvent) => {
      if (!dragging && Math.abs(ev.clientX - x0) <= 4) return;
      if (!dragging) { dragging = true; deselect(); }
      const tb = clampT(tOf(ev.clientX - innerLeft()));
      vselStore.set({ vsel: { kind: "range", t0: Math.min(ta, tb), t1: Math.max(ta, tb) } });
    };
    const up = () => {
      el.removeEventListener("pointermove", move);
      el.removeEventListener("pointerup", up);
      el.removeEventListener("pointercancel", up);
      if (dragging) { endScrub(); return; }
      const i = pieceAt(curPieces(), ta);
      if (i >= 0) selectPiece(i); else { deselect(); clearVsel(); }
      seek(ta);
      if (scrubSound()) scrubBlip();
    };
    el.addEventListener("pointermove", move);
    el.addEventListener("pointerup", up);
    el.addEventListener("pointercancel", up);
  }

  function onCtx(e: React.MouseEvent) {
    e.preventDefault();
    e.stopPropagation();
    if (focused) return;
    const t = tAtClientX(e.clientX);
    const sel = vselStore.get().vsel;
    if (sel?.kind === "range" && t >= sel.t0 && t <= sel.t1) { rangeMenu(e, sel.t0, sel.t1); return; }
    const i = pieceAt(curPieces(), t);
    if (i >= 0) { selectPiece(i); pieceMenu(e, i); return; }
    const g = gaps.find(g => t >= g.t0 && t < g.t1);
    if (g) gapMenu(e, g);
  }

  return (
    <div className="videorow" id="videorow" style={{ height: height + "px" }}
      onPointerDown={onDown} onContextMenu={onCtx}>
      <Filmstrip height={height} left={left} w={w} pps={pps} />
      {/* 完整片里删掉的部分画成空隙；成片里它们已经合拢，不画 */}
      {!focused && gaps.map(g => {
        const gw = (g.t1 - g.t0) * pps;
        return (
          <div key={`g${g.t0}`} className="vgap" style={{ left: xOf(g.t0), width: gw }}
            title={`已删除 ${fmtLen(g.t1 - g.t0)} · 右键恢复`}>
            {gw > 70 && <span>已删除 {fmtLen(g.t1 - g.t0)}</span>}
          </div>
        );
      })}
      {pieces.map((p, i) => {
        const pw = wOf(p.t0, p.t1);
        const on = !focused && vsel?.kind === "piece" && vsel.i === i;
        // 成片里片段首尾相接连成一条：只有整条的两头有边，交界处不画分割竖线
        const seam = focused
          ? " seamless" + (i === 0 ? " first" : "") + (i === pieces.length - 1 ? " last" : "")
          : "";
        return (
          // 按下标当 key：拖边缘时 t0 在变，按时间当 key 会把手上这个元素换掉，指针捕获跟着断
          <div key={i} className={"vpiece" + (on ? " sel" : "") + seam}
            style={{ left: xOf(p.t0), width: pw }}>
            {edited && pw > 64 && <span className="nm">{`#${i + 1} · ${fmtLen(p.t1 - p.t0)}`}</span>}
            {!focused && pw > PIECE_EDGE_W * 3 && (["l", "r"] as const).map(side => (
              <div key={side} className={"vedge " + side} title="拖动改片段起止"
                onPointerDown={ev => dragPieceEdge(ev, i, side)} />
            ))}
          </div>
        );
      })}
      {vsel?.kind === "range" && (
        <div className="vrange" style={{ left: xOf(vsel.t0), width: Math.max(1, wOf(vsel.t0, vsel.t1)) }} />
      )}
    </div>
  );
}

/**
 * 完整片里，删掉的部分在所有字幕行、音频行上也盖一层暗色：这些字幕不会进成片。
 * 聚焦成片时那些部分已经合拢，什么都不画。
 */
export function CutShade() {
  const { pps, duration } = viewStore.use(
    s => ({ pps: s.pps, t0: s.t0, duration: s.duration, pieces: s.pieces, focus: s.focus }), shallowEqual);
  const pieces = viewStore.get().pieces;
  if (!pieces) return null;
  if (isFocused()) return null;
  return <>
    {gapsOf(pieces, duration).map(g => (
      <div key={g.t0} className="cut-shade" style={{ left: xOf(g.t0), width: (g.t1 - g.t0) * pps }} />
    ))}
  </>;
}
