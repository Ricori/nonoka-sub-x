import { Service as CloudService } from "../../bindings/github.com/Ricori/nonoka-x/desktop/internal/cloud/index.js";
import type { AdminKey as CloudAdminKey, Entry as CloudEntry, IssuedAdminKey, Session as CloudSession } from "../../bindings/github.com/Ricori/nonoka-x/desktop/internal/cloud/models.js";

export type { CloudAdminKey, CloudEntry, CloudSession, IssuedAdminKey };

// Whether a cloud entry has subtitles to hand back, which is not the same
// question as whether its newest attempt succeeded. Re-transcribing media
// reuses the library id, so `status` follows the new run while the previous
// run's artifacts stay in place until a completing run replaces them --
// reading the status instead made a finished subtitle set unreachable for as
// long as the retry was queued, running, or failed. Mirrors `Entry.HasSubtitles`
// on the Go side; both ends have to agree or the button and the call disagree.
//
// A type predicate, so a card guarding on it gets the entry narrowed the way
// the `status === "completed"` comparison it replaced used to.
export function hasCloudSubtitles(entry: CloudEntry | null | undefined): entry is CloudEntry {
  return (entry?.artifactNames?.length ?? 0) > 0;
}

export const DEFAULT_CLOUD_BACKEND = "https://cloud.nonoka.online";

export const cloudAccount = {
  session: CloudService.Session,
  refreshSession: CloudService.RefreshSession,
  login: CloudService.Login,
  logout: CloudService.Logout,
  async library(): Promise<CloudEntry[]> {
    return (await CloudService.Library()) ?? [];
  },
  deleteLibraryEntry: CloudService.DeleteLibraryEntry,
  thumbnail: CloudService.ThumbnailDataURL,
  adoptLibraryEntry: CloudService.AdoptLibraryEntry,
  adoptCloudEntry: CloudService.AdoptCloudEntry,
  syncLocalTask: CloudService.SyncLocalTask,
  capabilities: CloudService.Capabilities,
  listTasks: CloudService.ListTasks,
  startTask: CloudService.StartTask,
  taskStatus: CloudService.TaskStatus,
  taskEvents: CloudService.TaskEvents,
  cancelTask: CloudService.CancelTask,
  retryTask: CloudService.RetryTask,
  resumeTask: CloudService.ResumeTask,
  taskArtifacts: CloudService.TaskArtifacts,
  async adminKeys(): Promise<CloudAdminKey[]> {
    return (await CloudService.AdminKeys()) ?? [];
  },
  createAdminKey: CloudService.CreateAdminKey,
  updateAdminKey: CloudService.UpdateAdminKey,
  deleteAdminKey: CloudService.DeleteAdminKey,
};

// The library as of a session response. `/v1/session` returns it beside the
// session fields now, because opening the app asked for the same records three
// times over -- the session, the library to render, and the library again
// inside the sync check -- each one a separate authentication and walk.
//
// `null` is a backend older than that field, not an empty library: the Go
// struct declares `videos` without omitempty precisely so the two stay
// distinguishable, and only the former still costs a request.
export async function cloudLibraryOf(session: CloudSession): Promise<CloudEntry[]> {
  return session.videos ?? (await cloudAccount.library());
}
