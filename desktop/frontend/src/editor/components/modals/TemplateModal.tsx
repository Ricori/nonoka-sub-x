import { useEffect, useRef, useState } from 'react';
import { mergeStyleText, parseSheet } from '../../ass';
import { DEFAULT_STYLE_SHEET } from '../../constants';
import { pushHistory } from '../../lib/history';
import { machineDefaultStyles, saveAsMachineDefault, setDocStyles } from '../../lib/styleEdit';
import { refreshFontMetrics } from '../../lib/subtitles';
import { docStore } from '../../store/docStore';
import { modalStore, toast } from '../../store/uiStore';
import { errText } from '../../utils';

/**
 * ASS 样式的文本编辑口。样式表跟着视频走（存 document.json 的 styles 字段），
 * 所以这里改的只是当前这个视频——别的视频各有各的一份。
 *
 * 写死的 origin/cn 不出现在这里：它们在 ass.ts::setStyleSheet 里跟这份合并，同名以这份为准，
 * 所以想改默认轨的样子，在这里写一条同名的 Style 就行。
 *
 * 图形化的改法在预览下方那条属性条；这里是兜底入口——整段粘贴、导入别人的 ASS、
 * 或者直接改那些属性条没暴露的字段。
 */
export function TemplateModal() {
  const open = modalStore.use(s => s.tplOpen);
  const stored = docStore.use(s => s.styles);
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const fileRef = useRef<HTMLInputElement>(null);

  useEffect(() => { if (open) setText(stored); }, [open, stored]);

  const close = () => modalStore.set({ tplOpen: false });
  const dirty = text !== stored;

  function save() {
    // 有内容却一条 Style 都读不出来，多半是粘错了东西：这时候存下去等于把样式表清空
    if (text.trim() && !parseSheet(text).order.length) {
      toast("没解析出任何 Style 行，请检查内容（可只保留 [V4+ Styles] 段）");
      return;
    }
    pushHistory();               // 手写的改动也要能 Ctrl+Z，且与预览里的拖动按时序交错
    setDocStyles(text);          // 落盘随文档走：Ctrl+S 或 5 分钟后的自动保存
    void refreshFontMetrics();   // 换了字体的话，逐字特效的排版度量也要重量一遍
    toast("样式已应用 · 随文档保存", false, null, undefined, true);
    close();
  }

  /** 把当前这套存成本机默认，之后新视频都从它开头 */
  async function saveDefault() {
    setBusy(true);
    try {
      setDocStyles(text);
      await saveAsMachineDefault();
      toast("已存为本机默认样式，新视频会从这套开头", false, null, undefined, true);
    } catch (e) {
      toast("存为本机默认失败：" + errText(e));
    } finally {
      setBusy(false);
    }
  }

  async function resetToDefault() {
    try {
      setText(await machineDefaultStyles());
    } catch {
      setText(DEFAULT_STYLE_SHEET);
    }
  }

  async function importFile(file: File) {
    try {
      const incoming = await readAssText(file);
      const { text: merged, added, updated } = mergeStyleText(text, incoming);
      if (!added.length && !updated.length) {
        toast("这个文件里没有 [V4+ Styles] 段，没什么可导入的");
        return;
      }
      setText(merged);
      const parts = [];
      if (added.length) parts.push("新增 " + added.join("、"));
      if (updated.length) parts.push("覆盖 " + updated.join("、"));
      toast("已导入：" + parts.join("；") + " · 点应用生效");
    } catch (e) {
      toast("读取 ASS 失败：" + errText(e));
    }
  }

  const onKey = (e: React.KeyboardEvent) => {
    if (e.key === "Escape") { e.preventDefault(); close(); }
    // 编辑器全局的 Ctrl+S 在 TEXTAREA 里也生效，会把文档存了；弹窗开着时改应用样式
    else if (e.key.toLowerCase() === "s" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); save(); }
    e.stopPropagation();
  };

  return (
    <div className="modal" id="tpl-modal" hidden={!open} onKeyDown={onKey}
      onClick={e => { if (e.target === e.currentTarget) close(); }}>
      <div className="box">
        <button className="x-close" id="tpl-close" title="关闭" onClick={close}>✕</button>
        <h3 id="tpl-title">字幕样式</h3>
        <div className="hint">这里填写 ASS 样式，轨道通过样式名绑定其中的 Style。样式跟着这个视频走，
          改动随文档保存（Ctrl+S），不影响别的视频。可只留 [V4+ Styles] 段，
          也可以粘完整 ASS 头（[Events] 段会被忽略）。</div>
        <textarea id="tpl-text" spellCheck={false} wrap="off" value={text}
          onChange={e => setText(e.target.value)} />
        <div className="foot" id="tpl-foot">
          <input ref={fileRef} type="file" accept=".ass,.ssa" hidden
            onChange={e => {
              const file = e.target.files?.[0];
              e.target.value = "";        // 同一个文件连选两次也要触发 change
              if (file) void importFile(file);
            }} />
          <button className="btn" id="tpl-import" style={{ marginRight: "auto" }}
            onClick={() => fileRef.current?.click()}>导入 ASS 样式…</button>
          <button className="btn" id="tpl-save-default" disabled={busy}
            title="新建视频时默认用这一套" onClick={() => void saveDefault()}>存为本机默认</button>
          <button className="btn" id="tpl-reset"
            onClick={() => void resetToDefault()}>恢复本机默认</button>
          <button className="btn" onClick={close}>取消</button>
          <button className="btn primary" id="tpl-save" disabled={busy || !dirty}
            onClick={save}>应用</button>
        </div>
      </div>
    </div>
  );
}

/**
 * 读一个 ASS/SSA 文件。字幕文件的编码相当杂：先认 BOM，再按 UTF-8 严格解码，
 * 解不动才当 GBK——老片源的 .ass 十有八九是 GBK，静默解成乱码比报错更难查。
 */
async function readAssText(file: File): Promise<string> {
  const bytes = new Uint8Array(await file.arrayBuffer());
  if (bytes[0] === 0xff && bytes[1] === 0xfe) return new TextDecoder("utf-16le").decode(bytes.subarray(2));
  if (bytes[0] === 0xfe && bytes[1] === 0xff) return new TextDecoder("utf-16be").decode(bytes.subarray(2));
  const body = bytes[0] === 0xef && bytes[1] === 0xbb && bytes[2] === 0xbf ? bytes.subarray(3) : bytes;
  try {
    return new TextDecoder("utf-8", { fatal: true }).decode(body);
  } catch {
    return new TextDecoder("gbk").decode(body);
  }
}
