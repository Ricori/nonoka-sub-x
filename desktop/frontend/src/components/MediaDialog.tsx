import type { Dispatch, SetStateAction } from "react";
import type { DialogState, SubtitleDeleteScope } from "../app/types.ts";
import "./MediaDialog.css";

interface MediaDialogProps {
  dialog: DialogState;
  busy: boolean;
  setDialog: Dispatch<SetStateAction<DialogState | null>>;
  onSubmit: () => Promise<void>;
}

interface ScopeChoice {
  scope: SubtitleDeleteScope;
  label: string;
  detail: string;
  disabled?: boolean;
}

// The choices only exist when there are two copies to choose between; with one
// the dialog is a plain confirmation of that copy.
function scopeChoices(dialog: Extract<DialogState, { kind: "delete-subtitles" }>): ScopeChoice[] {
  if (!dialog.entry.documentAvailable || !dialog.cloudEntry) return [];
  const cloudBusy = cloudTaskActive(dialog);
  return [
    { scope: "local", label: "仅本机", detail: "云端保留，之后可以随时取回。" },
    { scope: "cloud", label: "仅云端", detail: cloudBusy ? "云端任务还在进行，结束后才能删除。" : "本机保留，这份字幕不会再自动上传。", disabled: cloudBusy },
    { scope: "both", label: "本机和云端", detail: cloudBusy ? "云端任务还在进行，结束后才能删除。" : "所有设备上都不再有这份字幕。", disabled: cloudBusy },
  ];
}

function cloudTaskActive(dialog: Extract<DialogState, { kind: "delete-subtitles" }>) {
  return dialog.cloudEntry?.status === "queued" || dialog.cloudEntry?.status === "running";
}

export function MediaDialog({ dialog, busy, setDialog, onSubmit }: MediaDialogProps) {
  const isCloudRemove = dialog.kind === "cloud-remove";
  const isSubtitleRemove = dialog.kind === "delete-subtitles";
  const choices = isSubtitleRemove ? scopeChoices(dialog) : [];
  // A single cloud copy still answers to a task in flight; the menu offers the
  // dialog anyway, so the reason it cannot go is said here instead.
  const blocked = isSubtitleRemove && dialog.scope !== "local" && cloudTaskActive(dialog);
  return (
    <div className="modal-backdrop" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget && !busy) setDialog(null); }}>
      <section className="dialog" role="dialog" aria-modal="true" aria-labelledby="dialog-title">
        <h2 id="dialog-title">{dialog.kind === "rename" ? "重命名媒体" : isCloudRemove ? "删除云端字幕" : isSubtitleRemove ? "删除字幕" : "从媒体库移除视频"}</h2>
        {dialog.kind === "rename" ? (
          <label className="dialog-field">媒体标题<input autoFocus value={dialog.value} onChange={(event) => setDialog({ ...dialog, value: event.target.value })} onKeyDown={(event) => { if (event.key === "Enter") void onSubmit(); }} /></label>
        ) : isCloudRemove ? (
          <p>将永久删除“{dialog.entry.title}”的云端字幕。本机视频和本机字幕不会受到影响。</p>
        ) : isSubtitleRemove && choices.length > 0 ? (
          <>
            <p>“{dialog.entry.title}”在本机和云端各有一份字幕，选择要删除哪一份。视频本身不会被删除。</p>
            <div className="dialog-choices" role="radiogroup" aria-label="删除范围">
              {choices.map((choice) => (
                <label key={choice.scope} className={`dialog-choice ${dialog.scope === choice.scope ? "selected" : ""} ${choice.disabled ? "disabled" : ""}`}>
                  <input type="radio" name="delete-scope" checked={dialog.scope === choice.scope} disabled={choice.disabled || busy} onChange={() => setDialog({ ...dialog, scope: choice.scope })} />
                  <span><strong>{choice.label}</strong><small>{choice.detail}</small></span>
                </label>
              ))}
            </div>
          </>
        ) : isSubtitleRemove && dialog.scope === "cloud" ? (
          <p>{blocked ? `“${dialog.entry.title}”的云端任务还在进行，结束后才能删除云端字幕。` : `将永久删除“${dialog.entry.title}”的云端字幕。本机视频不会受到影响。`}</p>
        ) : isSubtitleRemove ? (
          <p>将删除“{dialog.entry.title}”的本机字幕文档。视频仍保留在媒体库，之后可以重跑任务。</p>
        ) : (
          <p>将“{dialog.entry.title}”从本机媒体库移除，卡片将不再显示。原视频文件不会被删除。</p>
        )}
        <div className="dialog-actions">
          <button disabled={busy} onClick={() => setDialog(null)}>取消</button>
          <button className={dialog.kind !== "rename" ? "danger-button" : "primary-button"} disabled={busy || blocked || (dialog.kind === "rename" && !dialog.value.trim())} onClick={() => void onSubmit()}>{busy ? "处理中…" : dialog.kind === "rename" ? "保存名称" : isCloudRemove || isSubtitleRemove ? "确认删除" : "确认移除"}</button>
        </div>
      </section>
    </div>
  );
}
