import type { Dispatch, SetStateAction } from "react";
import type { FineSubKeyState } from "../bridge/settings.ts";
import { Mark } from "./Mark.tsx";
import "./KeyConfigurationCard.css";

interface KeyConfigurationCardProps {
  item: FineSubKeyState;
  value: string;
  busy: boolean;
  setDrafts: Dispatch<SetStateAction<Record<string, string>>>;
  onSave: (updates: Record<string, string | null>, name: string) => Promise<void>;
  /** 存的是地址而不是密钥（如 Azure Speech 端点）：明文输入，文案说「地址」。 */
  address?: boolean;
}

export function KeyConfigurationCard({ item, value, busy, setDrafts, onSave, address = false }: KeyConfigurationCardProps) {
  // 「保存 Key」与「保存地址」：英文词前留空格，与改动前的文案逐字相同。
  const noun = address ? "地址" : " Key";
  return (
    <article className="panel key-card">
      <div className="key-card-heading">
        <div><h3>{item.label}</h3><code>{item.name}</code></div>
        <Mark tone={item.configured ? "ok" : "neutral"}>{item.configured ? `${item.count} 个已配置` : "未配置"}</Mark>
      </div>
      <p>{item.purpose}</p>
      <div className="key-current-value">
        <span>当前状态</span>
        <small>{item.masked.length > 0 ? item.masked.map((entry) => `${entry.name ? `${entry.name} · ` : ""}${entry.value}`).join("，") : `尚未保存${noun}`}</small>
      </div>
      <label className="key-input-label">
        <span>{item.configured ? `替换${noun}` : address ? "地址" : "API Key"}</span>
        <input aria-label={`${item.label} ${noun.trim()}`} type={address ? "url" : "password"} autoComplete="off" autoCapitalize="none" autoCorrect="off" spellCheck={false} placeholder={item.configured ? "输入新值以替换" : address ? "https://…" : "粘贴 API Key"} value={value} onChange={(event) => setDrafts((current) => ({ ...current, [item.name]: event.target.value }))} />
      </label>
      <div className="key-card-actions">
        {item.configured && <button className="danger-link" disabled={busy} onClick={() => void onSave({ [item.name]: null }, item.name)}>清除</button>}
        <button className="primary-button" disabled={busy || !value.trim()} onClick={() => void onSave({ [item.name]: value }, item.name)}>{busy ? "正在保存…" : `保存${noun}`}</button>
      </div>
    </article>
  );
}
