import { Service as LibraryService } from "../../bindings/github.com/Ricori/nonoka-x/desktop/internal/library/index.js";
import type {
  CacheStatus,
  Entry as MediaEntry,
  ImportResult,
} from "../../bindings/github.com/Ricori/nonoka-x/desktop/internal/library/models.js";

export type { CacheStatus, MediaEntry, ImportResult };

export const mediaLibrary = {
  async list(): Promise<MediaEntry[]> {
    return (await LibraryService.List()) ?? [];
  },
  get: LibraryService.Get,
  pickAndImport: LibraryService.PickAndImport,
  importPaths: LibraryService.Import,
  thumbnail: LibraryService.ThumbnailDataURL,
  rename: LibraryService.Rename,
  deleteDocument: LibraryService.DeleteDocument,
  remove: LibraryService.Remove,
  relink: LibraryService.PickRelink,
  mediaURL: LibraryService.MediaURL,
  saveSubtitle: LibraryService.SaveSubtitle,
  exportVideo: LibraryService.ExportVideo,
  exportVideoRange: LibraryService.ExportVideoRange,
  exportVideoPieces: LibraryService.ExportVideoPieces,
  cancelExport: LibraryService.CancelExport,
  transcodeToH264: LibraryService.TranscodeToH264,
  cancelTranscode: LibraryService.CancelTranscode,
  spectrogramTile: LibraryService.SpectrogramTile,
  filmstripTile: LibraryService.FilmstripTile,
  revealInFolder: LibraryService.RevealInFolder,
  cacheStatus: LibraryService.CacheStatus,
  cacheMedia: LibraryService.CacheMedia,
  setCacheLimitGB: LibraryService.SetCacheLimitGB,
  clearVideoCache: LibraryService.ClearVideoCache,
  setActiveMedia: LibraryService.SetActiveMedia,
  /** 视频轨片段；空数组 = 没剪过 */
  async getVideoEdit(id: string): Promise<{ t0: number; t1: number }[]> {
    return (await LibraryService.GetVideoEdit(id))?.pieces ?? [];
  },
  setVideoEdit: LibraryService.SetVideoEdit,
};
