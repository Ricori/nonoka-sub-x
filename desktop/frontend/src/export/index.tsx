import { useEffect, useMemo, useRef, useState } from 'react';
import { createRoot } from 'react-dom/client';
import { Events } from '@wailsio/runtime';
import { applyTheme, initialTheme } from '../app/theme.ts';
import { mediaLibrary } from '../bridge/library.ts';
import { installWindowsTitlebar } from '../bridge/window.ts';
import { desktopWindows } from '../bridge/windows.ts';
import { installScrollbarHover } from '../app/scrollbars.ts';
import { installDragGuard } from '../app/dragGuard.ts';
import { CustomSelect, type SelectOption } from '../components/CustomSelect.tsx';
import type { VideoExportDraft } from '../../bindings/github.com/Ricori/nonoka-x/desktop/internal/app/models.ts';
import { estimateExportBytes, formatBytes } from './estimate.ts';
import './Export.css';

const PRESETS = [
  { label: '高', crf: '18', preset: 'slow' },
  { label: '中', crf: '21', preset: 'medium' },
  { label: '低', crf: '24', preset: 'veryfast' },
];
const X264: SelectOption<string>[] = ['ultrafast', 'superfast', 'veryfast', 'faster', 'fast', 'medium', 'slow', 'slower', 'veryslow']
  .map((value) => ({ value, label: value }));
const SCALES: SelectOption<string>[] = [
  { value: '0', label: '原始' }, { value: '1080', label: '1080p' },
  { value: '720', label: '720p' }, { value: '480', label: '480p' },
];
/** Go 端 softwareExportEncoder 的 label；GPU 失败回退时进度事件里会换成它 */
const CPU_ENCODER = 'CPU · x264';
const GPU_KEY = 'nonoka.export.gpu';

type ProgressStart = { at: number; done: number };
type Phase = 'loading' | 'ready' | 'running' | 'cancelling' | 'success' | 'error';

const PHASE_LABEL: Record<Phase, string> = {
  loading: '读取中', ready: '待开始', running: '压制中', cancelling: '取消中', success: '已完成', error: '失败',
};

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
/** 成片时长写成 h:mm:ss / m:ss */
const formatClock = (seconds: number) => {
  const total = Math.max(0, Math.round(seconds));
  const h = Math.floor(total / 3600);
  const m = Math.floor(total % 3600 / 60);
  const s = String(total % 60).padStart(2, '0');
  return h ? `${h}:${String(m).padStart(2, '0')}:${s}` : `${m}:${s}`;
};
const isCancellation = (error: unknown) => {
  const text = errorText(error).toLowerCase();
  return text.includes('已取消') || text.includes('cancelled') || text.includes('canceled');
};
const fileName = (path: string) => path.split(/[\\/]/).pop() || path;

// 偏好只是本机便利，存不下（隐私模式、被清理）就当默认值
const readGpu = () => {
  try { return localStorage.getItem(GPU_KEY) === '1'; } catch { return false; }
};
const writeGpu = (on: boolean) => {
  try { localStorage.setItem(GPU_KEY, on ? '1' : '0'); } catch { /* 本次会话内仍然生效 */ }
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
  const [container, setContainer] = useState<'mp4' | 'mkv'>('mp4');
  const [gpu, setGpu] = useState(readGpu);
  // 本次导出实际在用的编码器与输出位置，由 Go 端随进度事件报上来
  const [encoder, setEncoder] = useState('');
  const [output, setOutput] = useState('');
  const [result, setResult] = useState<{ path: string; size: number; encoder: string } | null>(null);
  const [confirmClose, setConfirmClose] = useState(false);

  const busy = phase === 'running' || phase === 'cancelling';
  const pieceCount = draft?.pieces?.length ?? 0;
  // 剪辑成片要把几段拼起来，拼出来的音轨是新的，没有源音轨可复制
  const joined = pieceCount > 1;
  const duration = useMemo(() => (draft?.pieces ?? []).reduce((sum, piece) => sum + Math.max(0, piece.t1 - piece.t0), 0), [draft]);
  const estimate = useMemo(() => draft?.source ? estimateExportBytes({
    source: draft.source, pieces: draft.pieces, crf: Number(crf), preset, scaleH: Number(scale), abr, joined, container, gpu,
  }) : null, [draft, crf, preset, scale, abr, joined, container, gpu]);
  // 选了 GPU 却报上来 CPU：说明硬件编码器都没起来，已经回退
  const fellBack = gpu && encoder === CPU_ENCODER;

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
    const progress = event.data as { id?: string; stage?: string; done?: number; total?: number; encoder?: string; output?: string };
    if (progress.stage !== 'export' || progress.id !== draft?.progressId || !progress.total) return;
    if (progress.output) setOutput(progress.output);
    const done = progress.done || 0;
    if (progress.encoder) {
      setEncoder((current) => {
        // 换了编码器就是从头重压，速度样本作废
        if (current !== progress.encoder) {
          progressStart.current = null;
          setRemaining(null);
        }
        return progress.encoder ?? current;
      });
    }
    setPct(Math.max(0, Math.min(100, Math.round(done / progress.total * 100))));
    // 每次尝试开头那条 done=0 只是报编码器，不算速度起点
    if (done <= 0) return;
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

  function chooseContainer(value: 'mp4' | 'mkv') {
    setContainer(value);
    // FLAC 只给 MKV；拼接成片在 MKV 里没有源音轨可复制，默认换成无损
    if (value === 'mp4' && abr === 'flac') setAbr('192k');
    if (value === 'mkv' && joined && abr === 'copy') setAbr('flac');
  }

  function choosePreset(index: number) {
    if (busy) return;
    setPresetIndex(index);
    setCrf(PRESETS[index].crf);
    setPreset(PRESETS[index].preset);
  }

  function toggleGpu() {
    if (busy) return;
    setGpu((on) => {
      writeGpu(!on);
      return !on;
    });
  }

  async function startExport() {
    if (!draft || busy) return;
    setPhase('running');
    setMessage('正在等待选择保存位置…');
    setPct(0);
    setRemaining(null);
    setEncoder('');
    setOutput('');
    progressStart.current = null;
    setResult(null);
    try {
      const value = await desktopWindows.runVideoExport(jobID, Number(crf), preset, Number(scale), abr, container, gpu);
      if (!value.path) {
        setPhase('ready');
        setMessage('未选择保存位置，导出尚未开始。');
        return;
      }
      setPct(100);
      setResult(value);
      setEncoder(value.encoder);
      setOutput(value.path);
      setPhase('success');
      // 进度回调在 await 期间改写了它，TS 看不到，这里显式放宽类型
      const start = progressStart.current as ProgressStart | null;
      const took = start ? ` · 用时 ${formatDuration((performance.now() - start.at) / 1000)}` : '';
      setMessage(`导出完成 · ${formatBytes(value.size)}${took}`);
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

  const source = draft?.source;
  const sourceText = source && source.width > 0
    ? `${source.width}×${source.height}${source.fps > 0 ? ` · ${Number(source.fps.toFixed(3))} fps` : ''}`
    : '—';
  const encoderText = encoder || (gpu ? 'GPU 优先（开始时自动选择）' : CPU_ENCODER);
  const runningText = phase === 'running' && encoder
    ? (pct > 0 ? '正在压制字幕；你可以切换到主窗口继续工作。' : `正在启动 ${encoder}…`)
    : message;
  const audioOptions: SelectOption<string>[] = [
    { value: 'copy', label: joined ? '复制源音轨（多段拼接时不可用）' : '复制源音轨（不重新编码）', disabled: joined },
    ...(container === 'mkv' ? [{ value: 'flac', label: 'FLAC 无损' }] : []),
    { value: '128k', label: 'AAC 128k' }, { value: '192k', label: 'AAC 192k' },
    { value: '256k', label: 'AAC 256k' }, { value: '320k', label: 'AAC 320k' },
  ];

  return (
    <div className="export-app">
      <header className="titlebar">
        <div className="brand"><b>Nonoka Sub X</b><span>导出视频</span></div>
      </header>

      <main className="export-body">
        <section className={`task ${phase}`} aria-live="polite" aria-label="当前导出任务">
          <div className="task-head">
            <div className="task-title">
              <b title={draft?.defaultName}>{draft?.defaultName || '正在读取字幕快照…'}</b>
              <span>{draft?.rangeLabel || '整段视频'}</span>
            </div>
            <span className={`pill ${phase}`}>{PHASE_LABEL[phase]}</span>
          </div>
          <dl className="task-meta">
            <div><dt>源视频</dt><dd>{sourceText}</dd></div>
            <div><dt>成片时长</dt><dd>{duration > 0 ? formatClock(duration) : '—'}{joined && ` · ${pieceCount} 段拼接`}</dd></div>
            <div><dt>编码器</dt><dd className={fellBack ? 'warn' : ''}>
              {encoderText}{fellBack && '（GPU 不可用，已回退）'}
            </dd></div>
            <div><dt>输出</dt><dd title={output}>{output ? fileName(output) : '开始后选择保存位置'}</dd></div>
          </dl>
          <div className="task-status">
            <div className="status-line">
              <span>{busy ? runningText : message}</span>
              {busy && pct > 0 && <strong>
                {phase === 'running' && (remaining === null ? '正在估算剩余时间 · ' : `剩余约 ${formatDuration(remaining)} · `)}
                {pct}%
              </strong>}
            </div>
            <div className={'progress' + (busy || phase === 'success' ? '' : ' idle')}><i style={{ width: `${pct}%` }} /></div>
            {result && <button type="button" className="link" onClick={() => void mediaLibrary.revealInFolder(result.path)}>
              打开所在文件夹
            </button>}
          </div>
        </section>

        <section className="panel" aria-busy={busy}>
          <div className="sd-section">
            <div className="sd-section-head"><span className="sd-section-title">视频编码</span></div>
            <div className="sd-section-body">
              <div className="sd-prop">
                <span className="sd-label">画质预设</span>
                <div className="sp-seg">
                  {PRESETS.map((item, index) => (
                    <button type="button" key={item.label} className={presetIndex === index ? 'on' : ''}
                      disabled={busy} onClick={() => choosePreset(index)}>{item.label}</button>
                  ))}
                </div>
              </div>
              <div className="sd-prop">
                <label className="sd-label" htmlFor="export-crf" title="0 无损、51 最差；每 +6 体积约减半。18–24 是常用区间">CRF</label>
                <input id="export-crf" className="sd-input" type="number" min="0" max="51" step="1" disabled={busy}
                  value={crf} onChange={(event) => setCrf(event.target.value)} />
              </div>
              <div className="sd-prop">
                <span className="sd-label" title={gpu ? '对应到 GPU 编码器自己的速度档位' : '越慢压得越小，画质相同'}>编码预设</span>
                <CustomSelect compact ariaLabel="编码预设" disabled={busy} value={preset} options={X264} onChange={setPreset} />
              </div>
              <div className="sd-prop">
                <span className="sd-label">分辨率</span>
                <CustomSelect compact ariaLabel="分辨率" disabled={busy} value={scale} options={SCALES} onChange={setScale} />
              </div>
              <div className="sd-prop">
                <span className="sd-label">GPU 编码</span>
                <div className="sd-control">
                  <button type="button" role="switch" aria-checked={gpu} aria-label="使用 GPU 编码"
                    className={'sd-switch' + (gpu ? ' on' : '')} disabled={busy} onClick={toggleGpu}><i /></button>
                  <span className="sd-hint">NVIDIA / Intel / AMD，失败自动改用 CPU</span>
                </div>
              </div>
              {gpu && (
                <p className="notice">GPU 编码更快，但同样的 CRF 下细节通常不如 CPU（x264），文件也会变大</p>
              )}
            </div>
          </div>

          <div className="sd-section">
            <div className="sd-section-head"><span className="sd-section-title">封装与音频</span></div>
            <div className="sd-section-body">
              <div className="sd-prop">
                <span className="sd-label" title="MP4 兼容性最好；MKV 能原样装下 PCM、FLAC、TrueHD 等音轨，并保留源文件的全部音轨">格式</span>
                <div className="sp-seg">
                  {(['mp4', 'mkv'] as const).map((value) => (
                    <button type="button" key={value} className={container === value ? 'on' : ''}
                      disabled={busy} onClick={() => chooseContainer(value)}>{value.toUpperCase()}</button>
                  ))}
                </div>
              </div>
              <div className="sd-prop">
                <span className="sd-label" title={container === 'mp4'
                  ? '复制源音轨不会重新编码；源音频编码需兼容 MP4 容器，PCM 等请改用 MKV'
                  : '复制源音轨不会重新编码；FLAC 无损重新编码，多段拼接时也能保持无损'}>音频</span>
                <CustomSelect compact ariaLabel="音频" disabled={busy} value={abr} options={audioOptions} onChange={setAbr} />
              </div>
            </div>
          </div>
        </section>

        {!!draft?.missingFonts?.length && (
          <p className="notice">以下字体系统未安装，导出时会被替换：{draft.missingFonts.join('、')}</p>
        )}
      </main>

      <footer className="export-foot">
        {estimate !== null && (
          <span className="estimate" title="按源视频码率与当前参数估算；实际体积取决于画面复杂度，可能相差三成左右">
            预计大小 <strong>约 {formatBytes(estimate)}</strong>
          </span>
        )}
        {busy ? (
          <button type="button" className="btn" disabled={phase === 'cancelling'} onClick={() => void cancelExport()}>
            {phase === 'cancelling' ? '正在取消…' : '取消导出'}
          </button>
        ) : (
          <button type="button" className="btn" onClick={() => void desktopWindows.closeVideoExport()}>关闭</button>
        )}
        <button type="button" className="btn primary" disabled={!draft || busy || phase === 'loading'} onClick={() => void startExport()}>
          {phase === 'success' ? '再次导出' : '导出'}
        </button>
      </footer>

      {confirmClose && (
        <div className="confirm-mask" role="presentation">
          <section className="confirm-card" role="dialog" aria-modal="true" aria-labelledby="close-title">
            <h2 id="close-title">取消导出并关闭？</h2>
            <p>视频仍在压制。关闭这个窗口会停止当前导出，主编辑器不受影响。</p>
            <div>
              <button type="button" className="btn" onClick={() => setConfirmClose(false)}>继续导出</button>
              <button type="button" className="btn danger" onClick={() => void desktopWindows.closeVideoExport()}>取消并关闭</button>
            </div>
          </section>
        </div>
      )}
    </div>
  );
}

const root = document.getElementById('root');
if (!root) throw new Error('Nonoka X export root element is missing');
installScrollbarHover();
installDragGuard();
void installWindowsTitlebar();
createRoot(root).render(<ExportWindow />);
