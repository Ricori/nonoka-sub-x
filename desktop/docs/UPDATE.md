# Nonoka Sub X 桌面端更新与发布

桌面端有两条发布通道，使用同一个版本号和同一份 `CHANGELOG.md`：

| 通道 | 产物 | 执行方 | 触发方式 |
| --- | --- | --- | --- |
| GitHub Releases（手动下载） | Windows amd64 免安装 exe、macOS universal zip | GitHub Actions | 推送 `desktop/build/config.yml` 的版本变更到 `main` |
| 应用内自动更新（R2） | `nonoka-x-cloud` 桶 `desktop-updates/` 下的 `latest.json` 与安装包 | 本地脚本 | 手动执行 `release-update.ps1 -Publish` |

以下命令默认在 `desktop/` 目录下执行。

## 快速发布

以 `0.7.0` 为例：

```powershell
# 1. 在仓库根目录 CHANGELOG.md 顶部写好 "## 0.7.0" 小节

# 2. 同步版本号
.\scripts\release-update.ps1 -Version 0.7.0 -VersionOnly

# 3. 本地测试
go test ./...
npm.cmd --prefix frontend run typecheck
npm.cmd --prefix frontend test
node --test scripts/publish-update.test.mjs

# 4. 提交并推送，触发 GitHub Release
git add ../CHANGELOG.md .
git commit -m "chore: release 0.7.0"
git push origin main

# 5. 构建并发布应用内更新
.\scripts\release-update.ps1 -Version 0.7.0 -Publish
```

发布后打开 <https://cloudfile.nonoka.online/desktop-updates/latest.json>，确认 `version` 和 `artifacts` 都是本次发布的内容。

## 1. 写更新说明

在仓库根目录的 `CHANGELOG.md` 顶部新增一个版本小节。标题写成 `## x.y.z` 或 `## vx.y.z` 都可以，推荐不带 `v`：

```markdown
## 0.7.0

- 修复 xxx 导致的崩溃
- 改进 xxx 的处理速度
```

- 应用内更新：缺少对应小节或小节为空时，脚本直接报错。清单里只写入这一个小节。
- GitHub Release：优先用对应小节作为 Release 正文，找不到时退回到 GitHub 自动生成的说明。

## 2. 同步版本号

不要手工逐个改版本号，统一用脚本同步：

```powershell
.\scripts\release-update.ps1 -Version 0.7.0 -VersionOnly
```

会同步以下文件：

- `frontend/package.json`、`frontend/package-lock.json`
- `internal/selfupdate/service.go`（`const Version`，客户端用它判断是否需要更新）
- `build/config.yml`（GitHub Actions 从这里读版本号）
- `build/windows/info.json`、`build/windows/wails.exe.manifest`
- `build/darwin/Info.plist`、`build/darwin/Info.dev.plist`
- 仓库根目录 `pyproject.toml`（存在时）

## 3. GitHub Releases

`.github/workflows/release.yml` 监听 `main` 分支上 `desktop/build/config.yml` 的变更。推送版本变更后，工作流会依次：

1. 从 `build/config.yml` 读取版本号。
2. 检查 `v<版本>` Release 是否已存在，存在就跳过。
3. 分别构建 Windows exe（amd64）和 macOS universal `.app`。`.app` 用 `ditto` 打成 zip，以保留权限和符号链接。
4. 创建 tag 和 Release，并上传产物。

也可以在 GitHub Actions 页面手动运行 `Release` 工作流。勾选 `force` 后，即使同版本的 Release 已存在，也会重新构建并覆盖产物。这条通道不会发布 R2 更新清单。

> macOS 产物目前没有签名和公证。首次打开时，可能需要在 Finder 里右键应用并选择“打开”。

## 4. 应用内更新（R2）

### 4.1 首次准备

安装发布脚本的依赖（装在 `scripts/` 下）：

```powershell
npm.cmd --prefix scripts install
```

在 `desktop/.env.release` 里配置 R2 凭据。这个文件已被 `.gitignore` 忽略，不要提交：

```dotenv
R2_ACCOUNT_ID=...
R2_ACCESS_KEY_ID=...
R2_SECRET_ACCESS_KEY=...
R2_BUCKET=nonoka-x-cloud
```

R2 API 令牌需要 `nonoka-x-cloud` 桶的对象读写权限。可选配置：

| 变量 | 默认值 | 作用 |
| --- | --- | --- |
| `R2_WAILS_PREFIX` | `desktop-updates/` | R2 对象前缀 |
| `R2_ENDPOINT` | `https://<R2_ACCOUNT_ID>.r2.cloudflarestorage.com` | S3 兼容端点 |
| `R2_FORCE_PATH_STYLE=1` | 不启用 | 为兼容 S3 的服务启用 path-style 请求 |

### 4.2 构建与发布

`scripts/release-update.ps1` 是统一入口：

```powershell
# 构建 Windows 并发布（最常用）
.\scripts\release-update.ps1 -Version 0.7.0 -Publish

# 构建并发布 Windows 与 macOS
.\scripts\release-update.ps1 -Version 0.7.0 -Platform all -Publish

# 复用 bin/ 下已有的构建产物，只打包并发布
.\scripts\release-update.ps1 -Version 0.7.0 -SkipBuild -Publish

# 只生成产物、不上传，便于先检查
.\scripts\release-update.ps1 -Version 0.7.0

# 强制版本低于 0.6.0 的客户端更新到 0.7.0
.\scripts\release-update.ps1 -Version 0.7.0 -MinVersion 0.6.0 -Publish
```

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `-Version` | 必填 | `x.y.z` 格式 |
| `-Platform` | `windows` | `windows` / `macos` / `all` |
| `-Arch` | `amd64` | `amd64` / `arm64`（仅 Windows） |
| `-MinVersion` | 空 | 写入 `metadata.minVersion`，低于这个版本的客户端会被强制更新 |
| `-NotesFile` | `CHANGELOG.md` | 换成其他文件时，会把整份文件作为更新说明 |
| `-VersionOnly` | | 只同步版本号，然后退出 |
| `-SkipBuild` | | 跳过编译，要求 `bin/` 中已有对应平台的产物 |
| `-Publish` | | 生成产物后上传到 R2 |

脚本执行流程：

1. 同步全部版本字段。
2. 调用 Wails 构建（使用 `-SkipBuild` 时跳过）。
3. 从 `bin/Nonoka Sub X.exe` 或 `bin/Nonoka Sub X.app` 收集产物，写入 `bin/update/<版本>/`。
4. 用 `wails3 updater manifest` 生成带摘要的 `latest.json`，写入更新说明和可选的 `minVersion`。
5. 用 `wails3 updater verify` 校验清单和文件摘要。
6. 加了 `-Publish` 时，调用 `scripts/publish-update.mjs` 上传。

产物示例：

```text
desktop/bin/update/0.7.0/
├── Nonoka-Sub-X-0.7.0-windows-amd64.exe
├── Nonoka-Sub-X-0.7.0-darwin-universal.zip
└── latest.json
```

已经生成过的版本，可以单独重新上传：

```powershell
node scripts/publish-update.mjs --dir bin/update/0.7.0
```

### 4.3 发布器的行为

- 上传前会再次校验清单格式、平台、文件名和摘要。
- 安装包使用 `Cache-Control: public, max-age=31536000, immutable`，`latest.json` 使用 `no-cache`。
- 只发布一个平台时，如果远端清单已经是同一版本，会保留远端另一平台的产物并合并清单。所以 Windows 和 macOS 可以在不同机器上分别发布，前提是版本号相同。不同版本的产物不会被合并。

## 5. 客户端更新机制

正式构建默认检查：

```text
https://cloudfile.nonoka.online/desktop-updates/latest.json
```

- 启动后立即检查一次，之后每 4 小时检查一次。发现新版本后，会在后台下载、校验并暂存安装包。
- 普通更新不打断工作。下载完成后，顶部会出现“更新到 vX.Y.Z”按钮。用户没点的话，会在退出应用时由更新助手完成安装，下次启动就是新版本。
- 当前版本低于 `metadata.minVersion` 时为强制更新，更新界面会接管主窗口。如果字幕编辑器还开着，会先提示用户保存并关闭编辑器。
- 更新成功后的首次启动，会展示一次该版本的更新说明。
- 普通的检查或下载失败不会阻止启动；强制更新失败后，每分钟重试一次。

开发构建默认不检查更新。联调时可以用环境变量指定测试清单：

| 变量 | 作用 |
| --- | --- |
| `NONOKA_UPDATE_MANIFEST` | 覆盖完整的清单 URL。开发构建设置后也会启用更新检查 |
| `NONOKA_UPDATE_URL` | 覆盖更新根地址，客户端会在后面追加 `/latest.json` |
| `NONOKA_DISABLE_UPDATE=1` | 完全禁用自动更新 |

```powershell
$env:NONOKA_UPDATE_MANIFEST = "http://127.0.0.1:8080/latest.json"
```

> 0.5.0 及更早的客户端读取的是旧地址（`youtube-live-cache` 桶的 `nonoka-x-updates/wails/`）。迁移版本发布后，旧地址已不再更新，仍停留在这些版本的用户需要从 GitHub Releases 手动下载。

## 6. 故障排查

### 客户端没有发现新版本

- 确认运行的是正式构建；开发构建默认不检查更新。
- 确认 `internal/selfupdate/service.go` 的 `Version` 已经同步。
- 确认 `latest.json` 的版本号高于客户端版本，且清单中有当前系统对应的 `platform` 和 `arch`。
- 确认清单和其中的产物 URL 可以从公网直接访问。
- 确认没有设置 `NONOKA_DISABLE_UPDATE=1`，也没有设置错误的覆盖 URL。

### `prepare-update.ps1` 报找不到产物

- 去掉 `-SkipBuild`，让脚本重新构建。
- 确认 Windows 下存在 `bin/Nonoka Sub X.exe`，macOS 下存在 `bin/Nonoka Sub X.app`。

### 发布器报 `Missing R2 credentials`

- 确认 `.env.release` 在 `desktop/` 下，而不是仓库根目录。
- 确认变量名完全一致。

### 报 `Missing or empty CHANGELOG.md section`

`CHANGELOG.md` 里缺少 `## <版本>` 小节，或者这个小节是空的。

### 撤回有问题的版本

更新器只会升级到更高版本，用低版本覆盖 `latest.json` 不会让客户端降级。正确做法是修复问题、提升补丁版本号、在 `CHANGELOG.md` 中说明后重新发布。如果必须暂停更新，可以从 R2 撤下或替换清单，操作前先备份当前的清单和产物。

## 7. 相关文件

| 文件 | 作用 |
| --- | --- |
| `CHANGELOG.md`（仓库根目录） | 两条通道共用的更新说明 |
| `.github/workflows/release.yml` | 构建并发布 GitHub Release |
| `desktop/scripts/release-update.ps1` | 统一入口：同步版本、构建、准备产物，可选发布 |
| `desktop/scripts/prepare-update.ps1` | 收集产物、生成清单、写入 `minVersion` 并校验 |
| `desktop/scripts/publish-update.mjs` | 校验并上传到 R2，合并同版本的多平台清单 |
| `desktop/scripts/publish-update.test.mjs` | 清单合并行为的测试 |
| `desktop/internal/selfupdate/` | 客户端检查、下载、安装、清理 |
| `desktop/frontend/src/components/UpdateCenter.tsx` | 更新界面 |
| `desktop/frontend/src/editor/hooks/useDesktopEvents.ts` | 强制更新时保护编辑器中未保存的内容 |
