import { StorageService } from "../../bindings/github.com/Ricori/nonoka-x/desktop/internal/app/index.js";
import type {
  Destination as StorageDestination,
  Location as StorageLocation,
  RelocationProgress,
  StorageStatus,
} from "../../bindings/github.com/Ricori/nonoka-x/desktop/internal/app/models.js";

export type { RelocationProgress, StorageDestination, StorageLocation, StorageStatus };

/** 与 Go 端 storage.RuntimeTarget / storage.VideoTarget 一致。 */
export type StorageTarget = "runtime" | "video";

export const storageLocations = {
  status: () => StorageService.Status() as Promise<StorageStatus>,
  /** 跳过尺寸缓存重新测量；安装刚写完几 GB 之后要用这个。 */
  refresh: () => StorageService.Refresh() as Promise<StorageStatus>,
  /** 打开系统目录选择框，返回解析后的目标目录与空间信息。 */
  choose: (target: StorageTarget) => StorageService.ChooseDirectory(target) as Promise<StorageDestination>,
  relocate: (target: StorageTarget, destination: string) =>
    StorageService.Relocate(target, destination) as Promise<RelocationProgress>,
  reset: (target: StorageTarget) => StorageService.ResetLocation(target) as Promise<RelocationProgress>,
  cancel: () => StorageService.CancelRelocation() as Promise<RelocationProgress>,
  /** 在系统文件管理器里打开该目标当前所在的目录。 */
  open: (target: StorageTarget) => StorageService.OpenDirectory(target) as Promise<void>,
  progress: () => StorageService.Progress() as Promise<RelocationProgress>,
};
