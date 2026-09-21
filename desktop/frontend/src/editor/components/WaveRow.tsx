import { useEffect, useRef, useState } from 'react';
import { shallowEqual } from '../../home/lib/createStore';
import { bindScrub } from '../lib/laneDrag';
import { effGain, waveAmp } from '../lib/wave';
import { docStore } from '../store/docStore';
import { layoutStore } from '../store/layoutStore';
import { videoStore } from '../store/videoStore';
import { isFocused, tOf, viewDur, viewStore, xOf } from '../store/viewStore';
import { joinPieces } from '../../subtitles/pieces.ts';
import { getVid } from '../session';
import { errText } from '../utils';
import { mediaLibrary } from '../../bridge/library.ts';

interface SpectrumTile {
  url: string;
  start: number;
  duration: number;
}

const SPECTRUM_TILE_SECONDS = 2 * 60;
const SPECTRUM_DOM_TILES = 8;
const spectrumCache = new Map<string, SpectrumTile>();
const spectrumLoads = new Map<string, Promise<SpectrumTile>>();

const spectrumKey = (src: string, at: number) => `${getVid()}|${src}|${at}`;

function loadSpectrumTile(src: string, at: number) {
  const key = spectrumKey(src, at);
  const cached = spectrumCache.get(key);
  if (cached) return Promise.resolve(cached);
  let pending = spectrumLoads.get(key);
  if (!pending) {
    const request = mediaLibrary.spectrogramTile(getVid(), at, SPECTRUM_TILE_SECONDS)
      .then((tile: SpectrumTile) => { spectrumCache.set(key, tile); return tile; })
      .finally(() => spectrumLoads.delete(key));
    spectrumLoads.set(key, request);
    pending = request;
  }
  return pending;
}

function mergeSpectrumTiles(current: SpectrumTile[], added: SpectrumTile[], center: number) {
  const merged = new Map(current.map(tile => [`${tile.start}-${tile.duration}`, tile]));
  for (const tile of added) merged.set(`${tile.start}-${tile.duration}`, tile);
  return [...merged.values()]
    .sort((a, b) => Math.abs(a.start + a.duration / 2 - center) - Math.abs(b.start + b.duration / 2 - center))
    .slice(0, SPECTRUM_DOM_TILES)
    .sort((a, b) => a.start - b.start);
}

/** 时间轴上连续显示的几截原片：聚焦成片时是首尾相接并好的片段，否则是整个视图窗口 */
function keptSpans() {
  const v = viewStore.get();
  return isFocused() ? joinPieces(v.pieces!) : [{ t0: v.t0, t1: v.t1 }];
}

function SpectrumRow({ height, left, w, pps, t0 }: { height: number; left: number; w: number; pps: number; t0: number }) {
  const [tiles, setTiles] = useState<SpectrumTile[]>([]);
  const [message, setMessage] = useState("正在本地计算频谱…");
  const src = videoStore.use(s => s.src);

  useEffect(() => {
    let disposed = false;
    const timer = window.setTimeout(async () => {
      const start = tOf(Math.max(0, left - w * .25));
      const end = Math.min(viewStore.get().t1, tOf(left + w * 1.25));
      const first = Math.floor(start / SPECTRUM_TILE_SECONDS) * SPECTRUM_TILE_SECONDS;
      const starts: number[] = [];
      for (let at = first; at < end; at += SPECTRUM_TILE_SECONDS) starts.push(at);
      if (!starts.length) return;
      const center = (start + end) / 2;
      const cached = starts.map(at => spectrumCache.get(spectrumKey(src, at)))
        .filter((tile): tile is SpectrumTile => tile !== undefined);
      if (cached.length) setTiles(current => mergeSpectrumTiles(current, cached, center));
      const missing = starts.filter(at => !spectrumCache.has(spectrumKey(src, at)));
      setMessage(missing.length ? "正在本地计算频谱…" : "");
      let failure: unknown;
      for (const at of missing) {
        try {
          const tile = await loadSpectrumTile(src, at);
          if (disposed) return;
          setTiles(current => mergeSpectrumTiles(current, [tile], center));
        } catch (error) {
          failure = error;
          break;
        }
      }
      if (!disposed) setMessage(failure ? errText(failure) : "");
    }, 100);
    return () => { disposed = true; clearTimeout(timer); };
  }, [left, w, pps, t0, src]);

  return <>
    <div className="spectrum-grid" style={{ left, width: w }} />
    {/* 频谱图按原片时间连续生成：聚焦成片时按留下的每一段各裁一截摆到合拢后的位置 */}
    {tiles.flatMap(tile => keptSpans().flatMap(sp => {
      const a = Math.max(sp.t0, tile.start), b = Math.min(sp.t1, tile.start + tile.duration);
      if (b <= a) return [];
      return [
        <div key={`${tile.start}-${tile.duration}-${sp.t0}`} className="spectrum-clip"
          style={{ left: xOf(a), width: (b - a) * pps, height }}>
          <img className="spectrum-tile" src={tile.url} draggable={false} alt=""
            style={{ left: -(a - tile.start) * pps, width: tile.duration * pps, height }} />
        </div>,
      ];
    }))}
    {message && <span className="spectrum-message">{message}</span>}
  </>;
}

/**
 * 波形：只画可视窗口。60 分钟 × 140px/s 的整条时间轴远超 canvas 尺寸上限，
 * 波形 canvas 只做视口宽，随滚动平移重画。
 */
export function WaveRow({ height, left, w }: { height: number; left: number; w: number }) {
  const cvRef = useRef<HTMLCanvasElement>(null);
  const { pps, t0, t1, pieces, focus } = viewStore.use(
    s => ({ pps: s.pps, t0: s.t0, t1: s.t1, pieces: s.pieces, focus: s.focus }), shallowEqual);
  const gain = layoutStore.use(s => s.waveGain);
  const audioView = layoutStore.use(s => s.audioView);
  const peaks = docStore.use(s => s.peaks);

  useEffect(() => {
    const cv = cvRef.current;
    if (!cv || audioView !== "wave") return;
    const g = effGain();
    const W = Math.min(w, Math.round(viewDur() * pps));
    const H = height || 56;
    const x0 = Math.max(0, Math.min(left, viewDur() * pps - W));
    cv.width = W * 2; cv.height = H * 2;
    cv.style.width = W + "px"; cv.style.height = H + "px";
    cv.style.left = x0 + "px";
    const ctx = cv.getContext("2d");
    if (!ctx) return;
    ctx.scale(2, 2);
    ctx.clearRect(0, 0, W, H);
    ctx.fillStyle = "rgba(139,147,165,0.55)";
    const mid = H / 2;
    for (let x = 0; x < W; x++) {
      // 放大后超过满格的直接削平（顶满而不是画出行外）
      const a = Math.min(1, waveAmp(tOf(x0 + x)) * g) * (H / 2 - 3);
      ctx.fillRect(x, mid - Math.max(a, 0.6), 1, Math.max(a * 2, 1.2));
    }
    ctx.fillStyle = "rgba(139,147,165,0.18)";
    ctx.fillRect(0, mid - 0.5, W, 1);
    // 视图窗口或剪辑变了但缩放没变时也得重画：画布宽度、起点和每一列对应的原片时间都跟着变
  }, [height, left, w, pps, t0, t1, pieces, focus, gain, peaks, audioView]);

  return (
    <div className={"waverow" + (audioView === "spectrum" ? " spectrum" : "")} id="waverow" style={{ height: height + "px" }}
      onPointerDown={e => bindScrub(e, true)}>{/* 点音频行同时取消句子选定 */}
      {audioView === "wave"
        ? <canvas id="wave" ref={cvRef} />
        : <SpectrumRow height={height} left={left} w={w} pps={pps} t0={t0} />}
    </div>
  );
}
