import * as ProviderService from "../../bindings/github.com/Ricori/nonoka-x/desktop/internal/provider/service.js";

export interface RuntimeItem {
  id: string;
  version?: string;
  state: "missing" | "downloading" | "outdated" | "ready" | "failed";
  detail?: string;
  source?: "managed" | "system";
}

export type RuntimeInstallTarget = "media" | "runtime" | "models" | "all" | "yt-dlp" | "tokcount" | "aria2c" | "node" | "pot-provider" | "video-tools" | "optional-tools";
export type RuntimeToolGroup = "video-tools" | "optional-tools";

export interface RuntimeProvisionState {
  schema: 1;
  platform: string;
  supported: boolean;
  runtime_supported: boolean;
  media_supported: boolean;
  media_ready: boolean;
  /** Non-empty when the sidecar could not construct its provisioner: every
      install target is unavailable until it is resolved. */
  bootstrap_error?: string;
  root: string;
  runtime: RuntimeItem;
  resources: RuntimeItem[];
  models: RuntimeItem[];
  job: {
    state: "idle" | "running" | "completed" | "cancelled" | "failed";
    target: string;
    resource: string;
    stage: string;
    message: string;
    progress: { completed: number; total: number; unit: string; bytes_per_second?: number } | null;
    error: { code: string; message: string } | null;
  };
}

export interface PythonBootstrapState {
  schema: 1;
  platform: string;
  supported: boolean;
  state: "missing" | "running" | "ready" | "failed" | "unavailable";
  stage: string;
  message: string;
  python?: string;
  progress: { completed: number; total: number; unit: string; bytes_per_second?: number } | null;
}

export const fineSubRuntime = {
  pythonStatus: () => ProviderService.PythonBootstrapStatus() as Promise<unknown> as Promise<PythonBootstrapState>,
  installPython: () => ProviderService.InstallPython() as Promise<unknown> as Promise<PythonBootstrapState>,
  status: () => ProviderService.RuntimeProvisionStatus() as Promise<unknown> as Promise<RuntimeProvisionState>,
  install: (target: RuntimeInstallTarget) => ProviderService.InstallRuntime(target) as Promise<unknown> as Promise<RuntimeProvisionState>,
  cancel: () => ProviderService.CancelRuntimeInstall() as Promise<unknown> as Promise<RuntimeProvisionState>,
  removeAll: () => ProviderService.RemoveRuntime() as Promise<unknown> as Promise<RuntimeProvisionState>,
  removeGroup: (target: RuntimeToolGroup) => ProviderService.RemoveRuntimeGroup(target) as Promise<unknown> as Promise<RuntimeProvisionState>,
};
