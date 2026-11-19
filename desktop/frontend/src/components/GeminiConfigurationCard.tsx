import type { Dispatch, SetStateAction } from "react";
import type { FineSubKeyState } from "../bridge/settings.ts";
import { Mark } from "./Mark.tsx";
import "./GeminiConfigurationCard.css";

interface GeminiConfigurationCardProps {
  free?: FineSubKeyState;
  paid?: FineSubKeyState;
  drafts: Record<string, string>;
  busy: boolean;
  setDrafts: Dispatch<SetStateAction<Record<string, string>>>;
  onSave: (updates: Record<string, string | null>, name: string) => Promise<void>;
}

interface GeminiKeyRowProps {
  item: FineSubKeyState;
  kind: "free" | "paid";
  value: string;
  busy: boolean;
  setDrafts: Dispatch<SetStateAction<Record<string, string>>>;
  onSave: GeminiConfigurationCardProps["onSave"];
}

function GeminiKeyRow({ item, kind, value, busy, setDrafts, onSave }: GeminiKeyRowProps) {
  return (
    <div className="gemini-key-row">
      <div className="gemini-key-copy">
        <span className={`gemini-tier gemini-tier-${kind}`}>{kind === "free" ? "推荐" : "可选"}</span>
        <div><strong>{kind === "free" ? "免费额度 Key" : "付费额度 Key"}</strong><small>{kind === "free" ? "适合开始使用和日常处理" : "适合更高配额与质量优先任务"}</small></div>
      </div>
      <div className="gemini-key-status"><span>{item.configured ? `${item.count} 个已配置` : "未配置"}</span><small>{item.masked.length > 0 ? item.masked.map((entry) => `${entry.name ? `${entry.name} · ` : ""}${entry.value}`).join("，") : item.name}</small></div>
      <div className="gemini-key-input">
        <input aria-label={`${item.label} Key`} type="password" autoComplete="off" autoCapitalize="none" autoCorrect="off" spellCheck={false} placeholder={item.configured ? "输入新值以替换" : "粘贴 Gemini API Key"} value={value} onChange={(event) => setDrafts((current) => ({ ...current, [item.name]: event.target.value }))} />
        {item.configured && <button className="danger-link" disabled={busy} onClick={() => void onSave({ [item.name]: null }, item.name)}>清除</button>}
      </div>
    </div>
  );
}

export function GeminiConfigurationCard({ free, paid, drafts, busy, setDrafts, onSave }: GeminiConfigurationCardProps) {
  const items = [free, paid].filter((item): item is FineSubKeyState => item !== undefined);
  const configured = items.filter((item) => item.configured).length;
  const updates = Object.fromEntries(items.map((item) => [item.name, drafts[item.name]?.trim()]).filter(([, value]) => value));

  return (
    <article className="panel gemini-config-card">
      <div className="gemini-config-heading">
        <div><span className="eyebrow">Minimum configuration</span><h2>Gemini API Key</h2><p>免费额度与付费额度任选其一即可；也可以同时配置，按任务需求选择。</p></div>
        <Mark tone={configured > 0 ? "ok" : "neutral"}>{configured > 0 ? `已配置 ${configured} 项` : "任选其一"}</Mark>
      </div>
      <div className="gemini-key-list">
        {free && <GeminiKeyRow item={free} kind="free" value={drafts[free.name] ?? ""} busy={busy} setDrafts={setDrafts} onSave={onSave} />}
        {paid && <GeminiKeyRow item={paid} kind="paid" value={drafts[paid.name] ?? ""} busy={busy} setDrafts={setDrafts} onSave={onSave} />}
      </div>
      <div className="gemini-config-footer">
        <span>{configured > 0 ? "最小配置已完成" : "保存任意一项即可启用主要字幕处理能力"}</span>
        <button className="primary-button" disabled={busy || Object.keys(updates).length === 0} onClick={() => void onSave(updates, "Gemini")}>{busy ? "正在保存…" : "保存 Gemini 配置"}</button>
      </div>
    </article>
  );
}
