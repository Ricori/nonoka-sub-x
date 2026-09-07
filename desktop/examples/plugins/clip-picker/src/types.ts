// 宿主契约。字段都对着 Go 侧的结构体，改这里之前先看那边。

/** 宿主注入的桥。见 internal/plugins/service.go 的 injectPagePolicy。 */
interface NonokaBridge {
  apiVersion: number;
  post(method: string, params?: Record<string, unknown>, id?: string): void;
}

interface Window {
  nonoka: NonokaBridge;
}

/** 宿主发回页面的消息。method 是 "host.info" 或 "rpc.result"。 */
interface HostMessage {
  source?: string;
  apiVersion?: number;
  method?: string;
  id?: unknown;
  result?: unknown;
  error?: unknown;
}

/** host.getInfo 的返回。 */
interface HostInfo {
  theme: string;
  locale: string;
  pluginId: string;
  toolId: string;
}

/**
 * media.list 的一项。对应 internal/plugins/capabilities.go 的 MediaSummary。
 * 注意没有本地路径 —— 插件永远用 id 定位媒体。
 */
interface MediaSummary {
  id: string;
  title: string;
  duration: number;
  width: number;
  height: number;
  /** 视频文件在不在。云端取回的占位条目是 false，但字幕可能是全的。 */
  available: boolean;
  /** 有没有字幕文档。这个插件只认这一项。 */
  documentAvailable: boolean;
}

/**
 * llm.complete 的返回。对应 internal/plugins/engine.go 的 LLMAnswer。
 *
 * 没有 model 入参是故意的：用哪个模型是用户的配置，能指定模型的插件也就能
 * 绕开用户的选择。这里的 model 只是回报「实际是谁答的」。
 */
interface LLMAnswer {
  content: string;
  /** 实际应答的模型 */
  model: string;
  /** 走的哪条后端：API Key，或本机已登录的 CLI */
  backend: string;
  /** 首选模型没能应答、降级到链上靠后的一个 */
  fallbackUsed: boolean;
}

/** 字幕文档里的一句。宿主只校验 t0/t1/ja/zh，其余字段原样带过。 */
interface SubtitleLine {
  t0: number;
  t1: number;
  ja?: string;
  zh?: string;
  [key: string]: unknown;
}

/** document.read 的返回。写回时必须带上读到的 rev。 */
interface SubtitleDocument {
  rev: number;
  title?: string;
  subtitles?: SubtitleLine[];
  [key: string]: unknown;
}
