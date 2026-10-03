import { Service as ProviderService } from "../../bindings/github.com/Ricori/nonoka-x/desktop/internal/provider/index.js";
import type { Snapshot as SidecarSnapshot } from "../../bindings/github.com/Ricori/nonoka-x/desktop/internal/sidecar/models.js";
import type { LocalProviderBridge } from "../providers/localProvider.ts";

// Generated Wails bindings are the only renderer-to-Go entry point. The
// sidecar token and base transport remain private to the Go manager.
export const localProviderBridge: LocalProviderBridge = {
  Capabilities: ProviderService.Capabilities,
  ListTasks: ProviderService.ListTasks,
  StartTask: ProviderService.StartTask,
  TaskStatus: ProviderService.TaskStatus,
  TaskEvents: ProviderService.TaskEvents,
  CancelTask: ProviderService.CancelTask,
  RetryTask: ProviderService.RetryTask,
  ResumeTask: ProviderService.ResumeTask,
  TaskArtifacts: ProviderService.TaskArtifacts,
};

export const sidecarStatus = (): Promise<SidecarSnapshot> => ProviderService.SidecarStatus();

export const reportStartFailure = (provider: string, code: string): void => {
  void ProviderService.ReportStartFailure(provider, code).catch(() => undefined);
};
