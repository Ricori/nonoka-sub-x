import type {
  ArtifactManifest,
  Capabilities,
  ExecutionProvider,
  TaskEventPage,
  TaskRequest,
  TaskListItem,
  TaskSnapshot,
} from "./types.ts";


export interface CloudProviderBridge {
  capabilities(): Promise<unknown>;
  listTasks(): Promise<unknown>;
  startTask(localID: string, options: Record<string, unknown>): Promise<unknown>;
  taskStatus(taskID: string): Promise<unknown>;
  taskEvents(taskID: string, afterCursor: number): Promise<unknown>;
  cancelTask(taskID: string): Promise<unknown>;
  retryTask(taskID: string): Promise<unknown>;
  resumeTask(taskID: string): Promise<unknown>;
  taskArtifacts(taskID: string): Promise<unknown>;
}

/** 云端任务现在都是 relay：云端识别到 stable.json，本机接着跑完。旧版的 `vid_` 任务
 *  不再可查，Go 侧会把它们报成失败。 */
export function isRelayTaskId(taskId: string): boolean {
  return /^rly_[0-9a-f]{24}$/.test(taskId);
}

function object(value: unknown, label: string): Record<string, unknown> {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw new TypeError(`${label} must be an object`);
  }
  return value as Record<string, unknown>;
}

function snapshot(value: unknown): TaskSnapshot {
  const body = object(value, "TaskSnapshot");
  if (body.schema !== 1 || typeof body.task_id !== "string" || body.provider !== "cloud") {
    throw new TypeError("Cloud Provider returned an incompatible TaskSnapshot");
  }
  return body as unknown as TaskSnapshot;
}

export class CloudExecutionProvider implements ExecutionProvider {
  private readonly bridge: CloudProviderBridge;

  constructor(bridge: CloudProviderBridge) {
    this.bridge = bridge;
  }

  async capabilities(): Promise<Capabilities> {
    const body = object(await this.bridge.capabilities(), "Capabilities");
    if (body.provider !== "cloud" || body.adapter_schema !== 1 || body.artifact_schema !== 1) {
      throw new TypeError("Cloud Provider returned incompatible capabilities");
    }
    return body as unknown as Capabilities;
  }

  async listTasks(): Promise<TaskListItem[]> {
    const body = object(await this.bridge.listTasks(), "TaskList");
    if (body.schema !== 1 || !Array.isArray(body.tasks)) throw new TypeError("Cloud Provider returned an incompatible task list");
    return body.tasks.map((value) => {
      const record = object(value, "TaskListItem");
      return {
        snapshot: snapshot(record.snapshot),
        media_id: typeof record.media_id === "string" ? record.media_id : "",
        title: typeof record.title === "string" ? record.title : "",
      };
    });
  }

  async start(request: TaskRequest): Promise<TaskSnapshot> {
    if (request.provider !== "cloud" || request.source.kind !== "uploaded_audio") {
      throw new TypeError("Cloud Provider requires an uploaded_audio request");
    }
    const localID = request.source.object_id;
    return snapshot(await this.bridge.startTask(localID, request as unknown as Record<string, unknown>));
  }

  async status(taskId: string): Promise<TaskSnapshot> {
    return snapshot(await this.bridge.taskStatus(taskId));
  }

  async events(taskId: string, afterCursor: number): Promise<TaskEventPage> {
    const body = object(await this.bridge.taskEvents(taskId, afterCursor), "TaskEventPage");
    if (body.schema !== 1 || body.task_id !== taskId || !Array.isArray(body.events)) {
      throw new TypeError("Cloud Provider returned an incompatible event page");
    }
    return body as unknown as TaskEventPage;
  }

  async cancel(taskId: string): Promise<TaskSnapshot> {
    return snapshot(await this.bridge.cancelTask(taskId));
  }

  async retry(taskId: string): Promise<TaskSnapshot> {
    return snapshot(await this.bridge.retryTask(taskId));
  }

  async resume(taskId: string): Promise<TaskSnapshot> {
    return snapshot(await this.bridge.resumeTask(taskId));
  }

  async artifacts(taskId: string): Promise<ArtifactManifest> {
    const body = object(await this.bridge.taskArtifacts(taskId), "ArtifactManifest");
    if (body.schema !== 1 || body.task_id !== taskId || typeof body.artifacts !== "object") {
      throw new TypeError("Cloud Provider returned an incompatible artifact manifest");
    }
    return body as unknown as ArtifactManifest;
  }
}
