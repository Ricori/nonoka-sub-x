import { useEffect, useMemo, useRef, useState } from 'react';
import { createRoot } from 'react-dom/client';
import { Events } from '@wailsio/runtime';
import { applyTheme, initialTheme } from '../app/theme.ts';
import { mediaLibrary } from '../bridge/library.ts';
import { desktopWindows } from '../bridge/windows.ts';
import { installScrollbarHover } from '../app/scrollbars.ts';
import { installDragGuard } from '../app/dragGuard.ts';
import type { VideoExportDraft } from '../../bindings/github.com/Ricori/nonoka-x/desktop/internal/app/models.ts';
import { estimateExportBytes, formatBytes } from './estimate.ts';
import './Export.css';

const PRESETS = [
  { label: '高', crf: '18', preset: 'slow' },
  { label: '中', crf: '21', preset: 'medium' },
  { label: '低', crf: '24', preset: 'veryfast' },
];
const X264 = ['ultrafast', 'superfast', 'veryfast', 'faster', 'fast', 'medium', 'slow', 'slower', 'veryslow'];

type ProgressStart = { at: number; done: number };
type Phase = 'loading' | 'ready' | 'running' | 'cancelling' | 'success' | 'error';

const errorText = (error: unknown) => String((error as { message?: string })?.message || error || '未知错误')
  .replace(/^Error:\s*/, '');
/** 时长写成“3 分 20 秒”；十分钟以上只到分钟，免得数字跳个不停 */
const formatDuration = (seconds: number) => {
  const total = Math.max(1, Math.round(seconds));
  if (total < 60) return `${total} 秒`;
  if (total < 600) return `${Math.floor(total / 60)} 分 ${total % 60} 秒`;
  const minutes = Math.round(total / 60);
  return minutes < 60 ? `${minutes} 分钟` : `${Math.floor(minutes / 60)} 小时 ${minutes % 60} 分`;
};
const isCancellation = (error: unknown) => {
  const text = errorText(error).toLowerCase();
  return text.includes('已取消') || text.includes('cancelled') || text.includes('canceled');
};

function ExportWindow() {
  const jobID = new URLSearchParams(window.location.search).get('job') ?? '';
  const [draft, setDraft] = useState<VideoExportDraft | null>(null);
  const [phase, setPhase] = useState<Phase>('loading');
  const [message, setMessage] = useState('正在准备导出…');
  const [pct, setPct] = useState(0);
  const [remaining, setRemaining] = useState<number | null>(null);
  // 第一条进度到达的时刻与位置：保存对话框和 ffmpeg 启动的时间不算进压制速度
  const progressStart = useRef<ProgressStart | null>(null);
  const [crf, setCrf] = useState('21');
  const [preset, setPreset] = useState('medium');
  const [presetIndex, setPresetIndex] = useState(1);
  const [scale, setScale] = useState('0');
  const [abr, setAbr] = useState('192k');
  const [result, setResult] = useState<{ path: string; size: number } | null>(null);
  const [confirmClose, setConfirmClose] = useState(false);

  const busy = phase === 'running' || phase === 'cancelling';
  // 剪辑成片要把几段拼起来，拼出来的音轨是新的，没有源音轨可复制
  const joined = (draft?.pieces?.length ?? 0) > 1;
  const estimate = useMemo(() => draft?.source ? estimateExportBytes({
    source: draft.source, pieces: draft.pieces, crf: Number(crf), preset, scaleH: Number(scale), abr, joined,
  }) : null, [draft, crf, preset, scale, abr, joined]);

  useEffect(() => {
    applyTheme(initialTheme('dark'));
    desktopWindows.videoExportDraft(jobID)
      .then((value) => {
        setDraft(value);
        if ((value.pieces?.length ?? 0) > 1) setAbr((current) => current === 'copy' ? '192k' : current);
        setPhase('ready');
        setMessage('选择参数后开始导出。压制期间可以继续编辑其它项目。');
      })
      .catch((error) => {
        setPhase('error');
        setMessage('读取导出任务失败：' + errorText(error));
      })
      .finally(() => void Events.Emit('export:ready', jobID));
  }, [jobID]);

  useEffect(() => Events.On('media:progress', (event) => {
    const progress = event.data as { id?: string; stage?: string; done?: number; total?: number };
    if (progress.stage !== 'export' || progress.id !== draft?.progressId || !progress.total) return;
    const done = progress.done || 0;
    setPct(Math.max(0, Math.min(100, Math.round(done / progress.total * 100))));
    const now = performance.now();
    const start = progressStart.current;
    if (!start) {
      progressStart.current = { at: now, done };
      return;
    }
    // 按开始以来的平均速度外推；头三秒样本太少，先不报
    const elapsed = (now - start.at) / 1000;
    const rate = (done - start.done) / elapsed;
    if (elapsed >= 3 && rate > 0) setRemaining(Math.max(0, progress.total - done) / rate);
  }), [draft?.progressId]);

  useEffect(() => Events.On('export:request-close', (event) => {
    if (event.data !== jobID) return;
    if (busy) {
      setConfirmClose(true);
    } else {
      void desktopWindows.closeVideoExport();
    }
  }), [busy, jobID]);

  function choosePreset(index: number) {
    if (busy) return;
    setPresetIndex(index);
    setCrf(PRESETS[index].crf);
    setPreset(PRESETS[index].preset);
  }

  async function startExport() {
    if (!draft || busy) return;
    setPhase('running');
    setMessage('正在压制字幕；你可以切换到主窗口继续工作。');
    setPct(0);
    setRemaining(null);
    progressStart.current = null;
    setResult(null);
    try {
      const value = await desktopWindows.runVideoExport(jobID, Number(crf), preset, Number(scale), abr);
      if (!value.path) {
        setPhase('ready');
        setMessage('未选择保存位置，导出尚未开始。');
        return;
      }
      setPct(100);
      setResult(value);
      setPhase('success');
      // 进度回调在 await 期间改写了它，TS 看不到，这里显式放宽类型
      const start = progressStart.current as ProgressStart | null;
      const took = start ? ` · 用时 ${formatDuration((performance.now() - start.at) / 1000)}` : '';
      setMessage(`导出完成 · ${(value.size / 1024 ** 2).toFixed(0)} MB${took}`);
    } catch (error) {
      if (isCancellation(error)) {
        setPhase('ready');
        setPct(0);
        setMessage('导出已取消，可以调整参数后重新开始。');
      } else {
        setPhase('error');
        setMessage('导出失败：' + errorText(error));
      }
    }
  }

  async function cancelExport() {
    if (!draft || !busy || phase === 'cancelling') return;
    setPhase('cancelling');
    setMessage('正在取消导出…');
    try {
      await mediaLibrary.cancelExport(draft.mediaId);
    } catch {
      setPhase('running');
      setMessage('取消指令未能送达，导出仍在继续。');
    }
  }

  return (
    <main className="export-window">
      <header>
        <span className="eyebrow">Nonoka Sub X</span>
        <h1>导出视频</h1>
        <p>{draft?.rangeLabel || '正在读取字幕快照…'}</p>
      </header>

      <section className="settings" aria-busy={busy}>
        <div className="setting-row">
          <label>画质预设</label>
          <div className="segments">
            {PRESETS.map((item, index) => (
              <button type="button" key={item.label} className={presetIndex === index ? 'selected' : ''}
                disabled={busy} onClick={() => choosePreset(index)}>{item.label}</button>
            ))}
          </div>
        </div>
        <div className="setting-row">
          <label htmlFor="export-crf" title="0 无损、51 最差；每 +6 体积约减半。18–24 是常用区间">CRF</label>
          <input id="export-crf" type="number" min="0" max="51" step="1" disabled={busy}
            value={crf} onChange={(event) => setCrf(event.target.value)} />
        </div>
        <div className="setting-row">
          <label htmlFor="export-preset" title="越慢压得越小，画质相同">x264 preset</label>
          <select id="export-preset" disabled={busy} value={preset} onChange={(event) => setPreset(event.target.value)}>
            {X264.map((value) => <option key={value}>{value}</option>)}
          </select>
        </div>
        <div className="setting-row">
          <label htmlFor="export-scale">分辨率</label>
          <select id="export-scale" disabled={busy} value={scale} onChange={(event) => setScale(event.target.value)}>
            <option value="0">原始</option><option value="1080">1080p</option>
            <option value="720">720p</option><option value="480">480p</option>
          </select>
        </div>
        <div className="setting-row">
          <label htmlFor="export-audio" title="复制源音轨不会重新编码；源音频编码需兼容 MP4 容器">音频</label>
          <select id="export-audio" disabled={busy} value={abr} onChange={(event) => setAbr(event.target.value)}>
            <option value="copy" disabled={joined}>
              {joined ? '复制源音轨（多段拼接时不可用）' : '复制源音轨（不重新编码）'}
            </option>
            <option value="128k">128k</option><option value="192k">192k</option>
            <option value="256k">256k</option><option value="320k">320k</option>
          </select>
        </div>
      </section>

      {!!draft?.missingFonts?.length && (
        <aside className="warning">以下字体系统未安装，导出时会被替换：{draft.missingFonts.join('、')}</aside>
      )}

      <section className={`status ${phase}`} aria-live="polite">
        <div className="status-line"><span>{message}</span>{busy && <strong>
          {phase === 'running' && pct > 0 && (remaining === null ? '正在估算剩余时间 · ' : `剩余约 ${formatDuration(remaining)} · `)}
          {pct}%
        </strong>}</div>
        <div className="progress" hidden={!busy}><i style={{ width: `${pct}%` }} /></div>
        {result && <button type="button" className="link" onClick={() => void mediaLibrary.revealInFolder(result.path)}>
          {result.path} · 打开所在文件夹
        </button>}
      </section>

      <footer>
        {estimate !== null && (
          <span className="estimate" title="按源视频码率与当前参数估算；实际体积取决于画面复杂度，可能相差三成左右">
            预计大小 <strong>约 {formatBytes(estimate)}</strong>
          </span>
        )}
        {busy ? (
          <button type="button" className="secondary" disabled={phase === 'cancelling'} onClick={() => void cancelExport()}>
            {phase === 'cancelling' ? '正在取消…' : '取消导出'}
          </button>
        ) : (
          <button type="button" className="secondary" onClick={() => void desktopWindows.closeVideoExport()}>关闭</button>
        )}
        <button type="button" className="primary" disabled={!draft || busy || phase === 'loading'} onClick={() => void startExport()}>
          {phase === 'success' ? '再次导出' : '导出'}
        </button>
      </footer>

      {confirmClose && (
        <div className="confirm-mask" role="presentation">
          <section className="confirm-card" role="dialog" aria-modal="true" aria-labelledby="close-title">
            <h2 id="close-title">取消导出并关闭？</h2>
            <p>视频仍在压制。关闭这个窗口会停止当前导出，主编辑器不受影响。</p>
            <div>
              <button type="button" className="secondary" onClick={() => setConfirmClose(false)}>继续导出</button>
              <button type="button" className="danger" onClick={() => void desktopWindows.closeVideoExport()}>取消并关闭</button>
            </div>
          </section>
        </div>
      )}
    </main>
  );
}

const root = document.getElementById('root');
if (!root) throw new Error('Nonoka X export root element is missing');
installScrollbarHover();
installDragGuard();
createRoot(root).render(<ExportWindow />);
