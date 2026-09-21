import { useEffect, useLayoutEffect, useRef } from 'react';
import { shallowEqual } from '../../home/lib/createStore';
import { setSubCanvasEl, setVideoEl } from '../lib/media';
import { onStageDoubleClick, onStagePointerDown, shouldSwallowStageClick } from '../lib/stageDrag';
import { StageOverlay } from './StageOverlay';
import { onPauseUI, onPlayUI, resetScrubWarned, setPlaying, isScrubbing } from '../lib/playback';
import {
  attachChosen, cancelTranscode, pickVideoFile, showPlaybackError, showVideoFallback, transcodeToH264,
} from '../lib/videoSource';
import { isSmokeMode } from '../session';
import { docStore } from '../store/docStore';
import { saveLayout } from '../store/layoutStore';
import { playStore } from '../store/playStore';
import { relayout, setDuration, syncZoomRange } from '../store/viewStore';
import { videoStore } from '../store/videoStore';
import { fmt } from '../utils';

export function VideoStage() {
  const wrapRef = useRef<HTMLDivElement>(null);
  const stageRef = useRef<HTMLDivElement>(null);
  const videoRef = useRef<HTMLVideoElement>(null);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const vs = videoStore.use(s => s, shallowEqual);
  const { title, peaks } = docStore.use(s => ({ title: s.title, peaks: s.peaks }), shallowEqual);

  useLayoutEffect(() => {
    setVideoEl(videoRef.current);
    setSubCanvasEl(canvasRef.current);
    return () => { setVideoEl(null); setSubCanvasEl(null); };
  }, []);

  /** 视频舞台按视频真实宽高比适配（metadata 前默认 16:9） */
  const fitStage = () => {
    const wrap = wrapRef.current, stage = stageRef.current, v = videoRef.current;
    if (!wrap || !stage) return;
    const ar = (v?.videoWidth && v?.videoHeight) ? v.videoWidth / v.videoHeight : 16 / 9;
    const W = wrap.clientWidth, H = wrap.clientHeight;
    const w = Math.min(W, H * ar);
    stage.style.width = w + "px";
    stage.style.height = (w / ar) + "px";
  };

  useEffect(() => {
    fitStage();
    const ro = new ResizeObserver(fitStage);
    if (wrapRef.current) ro.observe(wrapRef.current);
    return () => ro.disconnect();
  }, []);

  // ── <video> 事件 ─────────────────────────────────────────────
  useEffect(() => {
    const v = videoRef.current;
    if (!v) return;
    const smoke = isSmokeMode();

    const onPlay = () => {
      if (isScrubbing()) return;   // 擦洗音起播：不进播放态，播放头由 seek 说了算
      onPlayUI();
    };
    const onPause = () => {
      // 擦洗中的暂停是跟随逻辑自己的节奏（等播放头追上来），别当成停止播放，
      // 更不能把 t 改成 video.currentTime——播放头是跟着鼠标走的
      if (isScrubbing()) return;
      onPauseUI();
    };
    const onMeta = () => {
      resetScrubWarned();   // 换了视频，擦洗音的提示重新算
      if (!(isFinite(v.duration) && v.duration > 0)) return;
      setDuration(v.duration);
      fitStage();
      relayout();
      if (syncZoomRange()) saveLayout();   // 时长变了，全览这一档跟着变
      if (smoke) v.currentTime = Math.min(v.duration * .75, v.duration - .25);
    };
    const onSeeked = () => {
      if (!smoke) return;
    };
    const onError = () => {
      if (!v.src) return;   // 还没挂视频，占位卡已经在管了
      // 本机文件播不出来多半不是文件缺失而是编码问题：Chromium 的 HEVC/H.265 依赖系统
      // 解码器，Win11 要装 HEVC 视频扩展才行。此时缩略图正常（ffmpeg 截的）但 video 报错。
      showPlaybackError("视频无法播放——若是 HEVC/H.265 编码，需要系统解码器支持"
        + "（Win11 可装「HEVC 视频扩展」）。也可以一键转码成 H.264，或改选其它文件：");
    };

    v.addEventListener("play", onPlay);
    v.addEventListener("pause", onPause);
    v.addEventListener("loadedmetadata", onMeta);
    v.addEventListener("seeked", onSeeked);
    v.addEventListener("error", onError);
    return () => {
      v.removeEventListener("play", onPlay);
      v.removeEventListener("pause", onPause);
      v.removeEventListener("loadedmetadata", onMeta);
      v.removeEventListener("seeked", onSeeked);
      v.removeEventListener("error", onError);
    };
  }, []);

  const durHint = peaks?.duration ? `（时长约 ${fmt(peaks.duration)}）` : "";
  const busyMsg = (pct: string, done: string) =>
    (pct ? `${done} ${pct}，` : "") + "完成后自动开始播放。期间可以照常编辑字幕。";
  const msg = vs.transcoding
    ? busyMsg(vs.transcodePct, "已转码") + "转好的 H.264 副本存入缓存目录。"
    : vs.retrieving
      ? busyMsg(vs.retrievePct, "已取回")
      : (vs.fbMsg || "本地没有该视频，云端也未保留原视频。请选择本地原视频文件：");
  const cardTitle = vs.transcoding ? "正在转码成 H.264"
    : vs.retrieving ? "正在从云端取回视频" : "未加载视频";

  return (
    <div className="stage-wrap" ref={wrapRef}>
      {/* 点中字幕就选中并起拖，点空白处照常播放/暂停。pointerdown 和 click 是两个事件，
          所以用一个标志位吞掉命中后那次 click，stopPropagation 在这里不管用 */}
      <div className="stage" ref={stageRef}
        onPointerDown={event => {
          if (stageRef.current) onStagePointerDown(event, stageRef.current);
        }}
        onClick={() => {
          if (shouldSwallowStageClick()) return;
          setPlaying(!playStore.get().playing);
        }}
        onDoubleClick={event => {
          if (stageRef.current) onStageDoubleClick(event, stageRef.current);
        }}>
        <video id="video" ref={videoRef} playsInline preload="auto" src={vs.src || undefined} />
        <div className="vid-cache" id="vid-cache" hidden={!vs.badge}>{vs.badge}</div>
        <div className="vid-fallback" id="vid-fallback" hidden={!vs.fallbackOpen}>
          <div className={"vf-card" + (vs.collapsed && !vs.retrieving && !vs.transcoding ? " collapsed" : "")} id="vf-card">
            <div className="vf-full">
              <div className="vf-title" id="vf-title">{cardTitle}</div>
              <div className="vf-msg" id="vf-msg">
                {msg}<br /><span className="vf-name">{title + durHint}</span>
              </div>
              <div className="vf-warn" id="vf-warn" hidden={vs.retrieving || vs.transcoding || !vs.warn}>{vs.warn}</div>
              {/* 取回中没什么可选的，别给按钮；转码中只留取消 */}
              <div className="vf-actions" id="vf-actions" hidden={vs.retrieving}>
                {vs.transcoding ? (
                  <button className="vf-btn" id="vf-tc-cancel" type="button"
                    onClick={cancelTranscode}>取消转码</button>
                ) : (<>
                  <button className="vf-btn primary" id="vf-transcode" type="button" hidden={!vs.canTranscode}
                    onClick={() => void transcodeToH264()}>转码成 H.264</button>
                  <button className={"vf-btn" + (vs.canTranscode ? "" : " primary")} id="vf-pick" type="button"
                    onClick={() => void pickVideoFile()}>选择视频文件</button>
                  <button className="vf-btn" id="vf-use" type="button" hidden={!vs.usePath}
                    onClick={() => { if (vs.usePath) void attachChosen(); }}>仍然使用</button>
                  <button className="vf-btn" id="vf-close" type="button"
                    onClick={() => showVideoFallback(true)}>暂不加载</button>
                </>)}
              </div>
            </div>
            <div className="vf-mini" id="vf-mini" onClick={() => showVideoFallback(false)}>
              {vs.retrieving ? "正在从云端取回视频…" : "未加载视频 · 点击选择文件"}
            </div>
          </div>
        </div>
        <div className="sub-overlay" id="sub-overlay"><canvas id="sub-canvas" ref={canvasRef} /></div>
        <StageOverlay stageRef={stageRef} />
        <div className="sub-busy" id="sub-busy" hidden={!vs.subBusy}>{vs.subBusy}</div>
      </div>
    </div>
  );
}
