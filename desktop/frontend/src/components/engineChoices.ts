import type { Capabilities, EngineLayers, ReadinessIssue, TranscriptionEngineInfo } from "../providers/types.ts";
import { CLOUD_TRANSCRIPTION_ENGINE, DEFAULT_TRANSCRIPTION_ENGINE } from "../providers/types.ts";
import { transcriptionEngineLabel } from "../app/format.ts";

/** One row of the engine list: an engine the local provider registered, or Nonoka Cloud's FineSub. */
export interface EngineChoice {
  id: string;
  label: string;
  kind: TranscriptionEngineInfo["kind"];
  ready: boolean;
  issues: ReadinessIssue[];
  billing: string;
  keys: string[];
  /** 要的运行环境层；读不到（旧版 sidecar、服务未连接）时缺省。云端 FineSub 不要本机的层。 */
  layers?: EngineLayers;
}

/**
 * What the list offers: the local provider's engines (FineSub first, as
 * registered), then Nonoka Cloud's FineSub. A sidecar that does not list its
 * engines yet (an older one, or one still starting) is taken to have only
 * FineSub, ready as far as its runtime says.
 */
export function engineChoices(capabilities: Capabilities | null, cloud: { authenticated: boolean; remaining?: number }): EngineChoice[] {
  const listed = capabilities?.transcription_engines;
  const local: EngineChoice[] = listed
    ? listed.filter((item) => item.kind !== "cloud-service").map((item) => ({
      id: item.id, label: item.label, kind: item.kind, ready: item.ready, issues: item.issues, billing: item.billing, keys: item.keys ?? [],
      ...(item.layers ? { layers: item.layers } : {}),
    }))
    : [{
      id: DEFAULT_TRANSCRIPTION_ENGINE,
      label: transcriptionEngineLabel(DEFAULT_TRANSCRIPTION_ENGINE),
      kind: "local-gpu",
      ready: capabilities?.runtime?.ready === true,
      issues: capabilities ? capabilities.runtime?.issues ?? [] : [{ code: "sidecar_unavailable", message: "本地服务尚未启动" }],
      billing: "",
      keys: [],
    }];
  const cloudChoice: EngineChoice = {
    id: CLOUD_TRANSCRIPTION_ENGINE,
    label: "FineSub（云端）",
    kind: "cloud-service",
    ready: cloud.authenticated,
    issues: cloud.authenticated ? [] : [{ code: "cloud_signed_out", message: "需要先登录云端账户" }],
    billing: cloud.remaining === undefined ? "每个任务扣一次云端额度" : `每个任务扣一次云端额度，剩余 ${cloud.remaining} 次`,
    keys: [],
    layers: { required: [], optional: [] },
  };
  return [...local, cloudChoice];
}
