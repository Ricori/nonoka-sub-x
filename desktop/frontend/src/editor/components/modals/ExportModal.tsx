import { useEffect, useRef, useState } from 'react';
import { shallowEqual } from '../../../home/lib/createStore';
import { buildClipAss, missingFonts } from '../../lib/assBuild';
import { getVid } from '../../session';
import { docStore } from '../../store/docStore';
import { exportStore } from '../../store/exportStore';
import { toast } from '../../store/uiStore';
import { viewStore } from '../../store/viewStore';
import { errText, fmt } from '../../utils';
import { mediaLibrary } from '../../../bridge/library.ts';

const PRESETS = [
  { label: "高", crf: "18", p: "slow" },
  { label: "中", crf: "21", p: "medium" },
  { label: "低", crf: "24", p: "veryfast" },
];

const X264 = ["ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow", "slower", "veryslow"];

/** 导出 MP4：裁到当前视图（完整片或切片）+ 内嵌字幕 + libx264 */
export function ExportModal() {
  const { open, clip, busy, pct } = exportStore.use(s => s, shallowEqual);
  const title = docStore.use(s => s.title);
  const [crf, setCrf] = useState("21");
  const [x264, setX264] = useState("medium");
  const [scale, setScale] = useState("0");
  const [abr, setAbr] = useState("192k");
  const [preset, setPreset] = useState(1);
  const [miss, setMiss] = useState<string[]>([]);
  const [cancelling, setCancelling] = useState(false);
  const taskRef = useRef<{ cancel: () => void } | null>(null);

  // 打开时现算一遍缺字：模板/绑定可能在上次打开之后改过。异步的，关掉了就别再 setState
  useEffect(() => {
    if (!open) return;
    let alive = true;
    missingFonts().then(m => { if (alive) setMiss(m); });
    return () => { alive = false; };
  }, [open]);

  const v = viewStore.get();
  const expClip = clip || v.curClip;
  const T0 = expClip ? expClip.t0 : v.t0, T1 = expClip ? expClip.t1 : v.t1;

  const baseName = () => (title || getVid()).replace(/\.[a-z0-9]{2,4}$/i, "") || getVid();

  const isCancel = (e: unknown) => {
    if (!e) return false;
    if ((e as any)?.name === "CancelError") return true;
    const msg = errText(e);
    return msg === "已取消" || msg === "Promise cancelled." || msg.toLowerCase().includes("cancelled") || msg.toLowerCase().includes("canceled");
  };

  async function cancelCurrentExport() {
    if (cancelling) return;
    setCancelling(true);
    if (taskRef.current) {
      taskRef.current.cancel();
    }
    try {
      await mediaLibrary.cancelExport(getVid());
    } catch {
      // 忽略取消指令本身的网络/调用异常
    }
  }

  function handleCancel() {
    if (busy) {
      void cancelCurrentExport();
    } else {
      exportStore.set({ open: false });
    }
  }

  function handleClose() {
    if (busy) {
      void cancelCurrentExport();
    }
    exportStore.set({ open: false });
  }

  function pickPreset(i: number) {
    if (busy) return;
    setPreset(i);
    setCrf(PRESETS[i].crf);
    setX264(PRESETS[i].p);
  }

  async function go() {
    const suffix = expClip ? " - " + expClip.name : "";
    const vid = getVid();
    exportStore.set({ busy: true, pct: 0 });
    setCancelling(false);
    try {
      // 字幕在本地拼（buildClipAss），出片也在本地跑 ffmpeg：整条路不碰服务端，
      // 出的就是屏幕上这份文档，不必先等一次落盘往返
      const task = mediaLibrary.exportVideoRange(
        vid, baseName() + suffix + ".mp4", buildClipAss(T0, T1),
        T0, T1, +crf, x264, +scale, abr,
      );
      taskRef.current = task;
      const r = await task;
      if (!r.path) return;
      exportStore.set({ open: false });
      toast("已导出 " + r.path + `（${(r.size / 1024 ** 2).toFixed(0)}MB）· 点此打开所在文件夹`,
        true, () => void mediaLibrary.revealInFolder(r.path), 5000, true);
    } catch (e) {
      const msg = errText(e);
      const cancelled = isCancel(e);
      toast(cancelled ? "已取消导出" : "导出失败：" + msg, !cancelled);
    } finally {
      taskRef.current = null;
      setCancelling(false);
      exportStore.set({ busy: false, pct: 0 });
    }
  }

  return (
    <div className="modal" id="exp-modal" hidden={!open}
      onKeyDown={e => {
        if (e.key === "Escape") {
          e.preventDefault();
          handleClose();
        }
        e.stopPropagation();
      }}>{/* 别让全局快捷键接管 */}
      <div className="box confirm exp">
        <button className="x-close" id="exp-close" title="关闭" onClick={handleClose}>✕</button>
        <h3>导出视频（内嵌字幕）</h3>
        <div className="hint" id="exp-range">
          {`${expClip ? "切片「" + expClip.name + "」" : "完整片"}　${fmt(T0)} → ${fmt(T1)}　共 ${(T1 - T0).toFixed(1)}s`}
        </div>

        <div className="exp-row">
          <label>画质预设</label>
          <div className="seg" id="exp-preset-seg">
            {PRESETS.map((p, i) => (
              <button key={p.label} className={i === preset ? "on" : undefined}
                disabled={busy}
                onClick={() => pickPreset(i)}>{p.label}</button>
            ))}
          </div>
        </div>
        <div className="exp-row">
          <label title="0 无损、51 最差；每 +6 体积约减半。18~24 是常用区间">CRF</label>
          <input id="exp-crf" type="number" min="0" max="51" step="1"
            disabled={busy}
            value={crf} onChange={e => setCrf(e.target.value)} />
        </div>
        <div className="exp-row">
          <label title="越慢压得越小，画质相同">x264 preset</label>
          <select id="exp-x264" value={x264} disabled={busy} onChange={e => setX264(e.target.value)}>
            {X264.map(p => <option key={p}>{p}</option>)}
          </select>
        </div>
        <div className="exp-row">
          <label>分辨率</label>
          <select id="exp-scale" value={scale} disabled={busy} onChange={e => setScale(e.target.value)}>
            <option value="0">原始</option>
            <option value="1080">1080p</option>
            <option value="720">720p</option>
            <option value="480">480p</option>
          </select>
        </div>
        <div className="exp-row">
          <label title="复制源音轨不会重新编码，可避免有损音频再次压缩；源音频编码需兼容 MP4 容器">音频</label>
          <select id="exp-abr" value={abr} disabled={busy} onChange={e => setAbr(e.target.value)}>
            <option value="copy">复制源音轨（不重新编码）</option>
            <option value="128k">128k</option>
            <option value="192k">192k</option>
            <option value="256k">256k</option>
            <option value="320k">320k</option>
          </select>
        </div>

        <div className="hint warn" id="exp-fontwarn" hidden={!miss.length}>
          {miss.length ? "以下字体系统未安装，导出时会被替换成其它字体：" + miss.join("、") : ""}
        </div>
        <div className="exp-prog" id="exp-prog" hidden={!busy}>
          <div className="bar"><i id="exp-bar" style={{ width: pct + "%" }} /></div>
          <span id="exp-pct">{pct}%</span>
        </div>
        <div className="foot">
          <button className="btn" id="exp-cancel" disabled={cancelling} onClick={handleCancel}>
            {cancelling ? "正在取消…" : (busy ? "取消导出" : "取消")}
          </button>
          <button className="btn primary" id="exp-go" disabled={busy} onClick={go}>导出</button>
        </div>
      </div>
    </div>
  );
}
