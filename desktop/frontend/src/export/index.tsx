import { useEffect, useState } from 'react';
import { createRoot } from 'react-dom/client';
import { Events } from '@wailsio/runtime';
import { applyTheme, initialTheme } from '../app/theme.ts';
import { mediaLibrary } from '../bridge/library.ts';
import { desktopWindows } from '../bridge/windows.ts';
import type { VideoExportDraft } from '../../bindings/github.com/Ricori/nonoka-x/desktop/internal/app/models.ts';
import './Export.css';

const PRESETS = [
  { label: '高', crf: '18', preset: 'slow' },
  { label: '中', crf: '21', preset: 'medium' },
  { label: '低', crf: '24', preset: 'veryfast' },
];
const X264 = ['ultrafast', 'superfast', 'veryfast', 'faster', 'fast', 'medium', 'slow', 'slower', 'veryslow'];

type Phase = 'loading' | 'ready' | 'running' | 'cancelling' | 'success' | 'error';

const errorText = (error: unknown) => String((error as { message?: string })?.message || error || '未知错误')
  .replace(/^Error:\s*/, '');
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
  const [crf, setCrf] = useState('21');
  const [preset, setPreset] = useState('medium');
  const [presetIndex, setPresetIndex] = useState(1);
  const [scale, setScale] = useState('0');
  const [abr, setAbr] = useState('192k');
  const [result, setResult] = useState<{ path: string; size: number } | null>(null);
  const [confirmClose, setConfirmClose] = useState(false);

  const busy = phase === 'running' || phase === 'cancelling';

  useEffect(() => {
    applyTheme(initialTheme('dark'));
    desktopWindows.videoExportDraft(jobID)
      .then((value) => {
        setDraft(value);
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
    setPct(Math.max(0, Math.min(100, Math.round((progress.done || 0) / progress.total * 100))));
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
      setMessage(`导出完成 · ${(value.size / 1024 ** 2).toFixed(0)} MB`);
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
            <option value="copy">复制源音轨（不重新编码）</option>
            <option value="128k">128k</option><option value="192k">192k</option>
            <option value="256k">256k</option><option value="320k">320k</option>
          </select>
        </div>
      </section>

      {!!draft?.missingFonts?.length && (
        <aside className="warning">以下字体系统未安装，导出时会被替换：{draft.missingFonts.join('、')}</aside>
      )}

      <section className={`status ${phase}`} aria-live="polite">
        <div className="status-line"><span>{message}</span>{busy && <strong>{pct}%</strong>}</div>
        <div className="progress" hidden={!busy}><i style={{ width: `${pct}%` }} /></div>
        {result && <button type="button" className="link" onClick={() => void mediaLibrary.revealInFolder(result.path)}>
          {result.path} · 打开所在文件夹
        </button>}
      </section>

      <footer>
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
createRoot(root).render(<ExportWindow />);
