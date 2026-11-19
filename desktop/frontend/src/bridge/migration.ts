import * as ProviderService from "../../bindings/github.com/Ricori/nonoka-x/desktop/internal/provider/service.js";

/** `GET /v1/migration` 的状态（`nonoka_x.migration.startup`）。 */
export type MigrationState =
  | "checking"
  | "not-needed"
  | "migrating"
  | "blocked"
  | "rolled-back"
  | "failed"
  | "downloading"
  | "download-failed"
  | "completed";

export interface MigrationStep {
  id: string;
  title: string;
  /** 迁移日志里的状态：pending / running / done / skipped / failed / undone。 */
  status: string;
  /** 下载那两步（后台进行，数据已经可以打开）。 */
  download: boolean;
}

export interface MigrationProgress {
  layer?: string | null;
  stage?: string | null;
  message?: string | null;
  completed: number;
  total: number;
  bytesPerSecond?: number;
}

/** 设置与密钥的迁移报告：只有名字，没有值。 */
export interface MigrationSettingsReport {
  carried: string[];
  routes: string[];
  renamed: Record<string, string>;
  droppedVariables: string[];
  droppedTables: string[];
  droppedKeys: string[];
  /** 旧文件在别的 Windows 账户下加密，本机打不开、没能带过来的密钥名。 */
  unreadable: string[];
  notes: string[];
}

/** 旧任务的归位与转换（`task-products` 一步的报告）：归位的任务数，以及按任务 id 列出没转成的文件与原因。 */
export interface MigrationTaskReport {
  tasks: number;
  /** 改写成新格式（`cloud_transcript`）的旧云端续跑请求数。 */
  requests?: number;
  not_converted?: Record<string, Record<string, string>>;
}

/** 过了回滚点后删掉的旧文件（主数据目录下的相对名），以及随旧密钥文件的备份一起删掉的、本机打不开的密钥名。 */
export interface MigrationCleanupReport {
  removed: string[];
  unreadableKeysRemoved?: string[];
}

export interface MigrationReport {
  settings?: MigrationSettingsReport;
  tasks?: MigrationTaskReport;
  cleanup?: MigrationCleanupReport;
}

export interface MigrationStatus {
  schema: number;
  state: MigrationState;
  message: string;
  /** false 时 sidecar 除迁移接口外一律答 503 `migrating`。 */
  dataOpen: boolean;
  step?: string;
  title?: string;
  steps?: MigrationStep[];
  progress?: MigrationProgress;
  error?: string;
  blockers?: string[];
  report?: MigrationReport;
}

export const dataMigration = {
  status: () => ProviderService.MigrationStatus() as Promise<unknown> as Promise<MigrationStatus>,
  retry: () => ProviderService.RetryMigration() as Promise<unknown> as Promise<MigrationStatus>,
};
