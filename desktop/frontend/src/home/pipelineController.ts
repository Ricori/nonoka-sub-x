import type {
  ArtifactManifest,
  ExecutionProvider,
  TaskEvent,
  TaskRequest,
  TaskSnapshot,
} from "../providers/types.ts";

export interface PipelineState {
  snapshot: TaskSnapshot | null;
  events: TaskEvent[];
  artifacts: ArtifactManifest | null;
  error: Error | null;
}

type Listener = (state: Readonly<PipelineState>) => void;

function asError(value: unknown): Error {
  return value instanceof Error ? value : new Error(String(value));
}

// No login state, upload state, global task key, or backend URL lives here.
// All task behavior comes from the selected provider's capabilities/contract.
export class PipelineController {
  private state: PipelineState = { snapshot: null, events: [], artifacts: null, error: null };
  private listeners = new Set<Listener>();
  private generation = 0;
  private refreshInFlight: Promise<Readonly<PipelineState>> | null = null;

  private readonly provider: ExecutionProvider;

  constructor(provider: ExecutionProvider) {
    this.provider = provider;
  }

  subscribe(listener: Listener): () => void {
    this.listeners.add(listener);
    listener(this.state);
    return () => this.listeners.delete(listener);
  }

  current(): Readonly<PipelineState> {
    return this.state;
  }

  async start(request: TaskRequest): Promise<TaskSnapshot> {
    const capabilities = await this.provider.capabilities();
    if (!capabilities.runtime?.ready) {
      const issue = capabilities.runtime?.issues[0];
      throw new Error(issue?.message ?? "Selected provider runtime is not ready");
    }
    if (request.correction.media === "video" && !capabilities.features.video_multimodal) {
      throw new Error("Selected provider does not support video multimodal correction");
    }
    const rawSnapshot = await this.provider.start(request);
    const snapshot: TaskSnapshot = {
      ...rawSnapshot,
      started_at: rawSnapshot.started_at || new Date().toISOString(),
    };
    this.generation += 1;
    this.set({ snapshot, events: [], artifacts: null, error: null });
    return snapshot;
  }

  async refresh(): Promise<Readonly<PipelineState>> {
    if (this.refreshInFlight) return this.refreshInFlight;
    const refresh = this.performRefresh();
    this.refreshInFlight = refresh;
    try {
      return await refresh;
    } finally {
      if (this.refreshInFlight === refresh) this.refreshInFlight = null;
    }
  }

  // The state transition is the part that has to land. A task that just failed
  // or completed stops being active, and the whole UI reads that from here --
  // so an event page or an artifact manifest that fails to load is reported as
  // an error beside the new snapshot, never by discarding it and leaving the
  // app showing a run that already ended.
  private async performRefresh(): Promise<Readonly<PipelineState>> {
    const taskId = this.state.snapshot?.task_id;
    if (!taskId) return this.state;
    const run = this.generation;
    try {
      const [status, page] = await Promise.allSettled([
        this.provider.status(taskId),
        this.provider.events(taskId, this.state.events.at(-1)?.cursor ?? 0),
      ]);
      if (run !== this.generation) return this.state;
      if (status.status === "rejected") {
        this.set({ ...this.state, error: asError(status.reason) });
        return this.state;
      }
      const rawSnapshot = status.value;
      const snapshot: TaskSnapshot = {
        ...rawSnapshot,
        started_at: rawSnapshot.started_at || this.state.snapshot?.started_at,
      };
      let events = this.state.events;
      if (page.status === "fulfilled") {
        const byCursor = new Map(this.state.events.map((event) => [event.cursor, event]));
        for (const event of page.value.events) byCursor.set(event.cursor, event);
        events = [...byCursor.values()].sort((left, right) => left.cursor - right.cursor);
      }
      let artifacts = this.state.artifacts;
      let error = page.status === "rejected" ? asError(page.reason) : null;
      if (snapshot.state === "completed" && artifacts === null) {
        try {
          artifacts = await this.provider.artifacts(taskId);
          if (run !== this.generation) return this.state;
        } catch (value) {
          artifacts = null;
          error = asError(value);
        }
      }
      this.set({ snapshot, events, artifacts, error });
    } catch (value) {
      this.set({ ...this.state, error: asError(value) });
    }
    return this.state;
  }

  async cancel(): Promise<TaskSnapshot | null> {
    const taskId = this.state.snapshot?.task_id;
    if (!taskId) return null;
    const snapshot = await this.provider.cancel(taskId);
    this.set({ ...this.state, snapshot });
    return snapshot;
  }

  async resume(): Promise<TaskSnapshot | null> {
    const taskId = this.state.snapshot?.task_id;
    if (!taskId) return null;
    const rawSnapshot = await this.provider.resume(taskId);
    const snapshot: TaskSnapshot = {
      ...rawSnapshot,
      started_at: rawSnapshot.started_at || new Date().toISOString(),
    };
    this.generation += 1;
    this.set({ ...this.state, snapshot, error: null });
    return snapshot;
  }

  // Resume and retry differ in which states they accept: a task interrupted by
  // a shutdown resumes, one the user cancelled or that failed retries. The
  // engine reuses its checkpoints either way, so neither restarts from zero.
  async retry(): Promise<TaskSnapshot | null> {
    const taskId = this.state.snapshot?.task_id;
    if (!taskId) return null;
    const rawSnapshot = await this.provider.retry(taskId);
    const snapshot: TaskSnapshot = {
      ...rawSnapshot,
      started_at: rawSnapshot.started_at || new Date().toISOString(),
    };
    this.generation += 1;
    this.set({ ...this.state, snapshot, error: null });
    return snapshot;
  }

  private set(state: PipelineState): void {
    this.state = state;
    for (const listener of this.listeners) listener(state);
  }
}
