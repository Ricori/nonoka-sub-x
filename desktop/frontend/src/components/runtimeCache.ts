import { useCallback, useEffect, useRef, useState } from "react";
import { fineSubRuntime } from "../bridge/runtime.ts";
import type { RuntimeCacheStatus } from "../bridge/runtime.ts";
import type { NoticeTone } from "./Notice.tsx";
import { formatStorageBytes } from "./StorageRelocation.tsx";

export interface RuntimeCacheState {
  status: RuntimeCacheStatus | null;
  busy: boolean;
  message: string;
  tone: NoticeTone;
  clear: () => Promise<void>;
  dismiss: () => void;
}

/**
 * 运行环境下载缓存的占用与清理。统计由 sidecar 遍历缓存目录得出，可能要几秒，
 * 所以只在 refreshKey 变化时重算——设置页把运行环境目录和占用拼成这个 key，
 * 迁移、安装完成之后它会跟着变。
 */
export function useRuntimeCache(refreshKey: string, onCleared: () => void): RuntimeCacheState {
  const [status, setStatus] = useState<RuntimeCacheStatus | null>(null);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [tone, setTone] = useState<NoticeTone>("warn");
  const request = useRef(0);

  const load = useCallback(async () => {
    const current = ++request.current;
    try {
      const next = await fineSubRuntime.cacheStatus();
      if (current === request.current) setStatus(next);
    } catch {
      // 本地服务没起来时没有可清理的东西，整行信息直接不显示。
      if (current === request.current) setStatus(null);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load, refreshKey]);

  const clear = useCallback(async () => {
    setBusy(true);
    setMessage("");
    try {
      const result = await fineSubRuntime.clearCache();
      // 清理期间发出的统计请求晚回来时，不能把清理前的数字盖回去。
      request.current += 1;
      setStatus(result);
      const failures = result.failures ?? [];
      const freed = result.freed > 0 ? `，释放了 ${formatStorageBytes(result.freed)}` : "";
      if (failures.length > 0) {
        const named = failures.slice(0, 3).join("、") + (failures.length > 3 ? ` 等 ${failures.length} 项` : "");
        setTone("warn");
        setMessage(`部分缓存正在使用，未能删除：${named}。其余已清理${freed}。`);
      } else {
        setTone("success");
        setMessage(`下载缓存已清理${freed}。`);
      }
      onCleared();
    } catch (value) {
      setTone("warn");
      setMessage(value instanceof Error ? value.message : String(value));
    } finally {
      setBusy(false);
    }
  }, [onCleared]);

  const dismiss = useCallback(() => setMessage(""), []);

  return { status, busy, message, tone, clear, dismiss };
}
