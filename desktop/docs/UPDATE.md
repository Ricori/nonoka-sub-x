# Nonoka Sub X 桌面端更新与发布

Nonoka Sub X 桌面端的发布分为两条互不冲突的通道：GitHub Releases 面向手动下载，Cloudflare R2 面向应用内自动更新。两条通道使用相同版本号和 `CHANGELOG.md`，但分别由 GitHub Actions 和本地发布脚本执行。

| 通道 | 产物 | 更新说明 | 执行方 |
| --- | --- | --- | --- |
| GitHub Releases | Windows amd64 免安装 exe、macOS universal zip | 仓库根目录 `CHANGELOG.md` | GitHub Actions 自动执行 |
| 应用内自动更新 | R2 `nonoka-x-cloud` 桶 `desktop-updates/` 下的 `latest.json`、Windows exe、macOS zip | 仓库根目录 `CHANGELOG.md` | 本地 PowerShell/Node.js 脚本手动执行 |

## 1. 应用内更新机制

正式构建使用 Wails Updater 检查以下默认清单（`cloudfile.nonoka.online` 是 `nonoka-x-cloud` 桶绑定的公网域名）：

```text
https://cloudfile.nonoka.online/desktop-updates/latest.json
```

0.5.0 及更早的客户端读取的是旧地址 `https://livestream.nonoka.online/nonoka-x-updates/wails/latest.json`（`youtube-live-cache` 桶），见第 6 节的一次性迁移开关。

客户端启动后立即检查一次，此后每 4 小时检查一次。检查到新版本后会在后台下载、校验并暂存安装包：

- 普通更新不会打断当前工作。下载完成后顶部会出现“更新到 vX.Y.Z”按钮；如果用户没有主动点击，安装会在退出应用时由更新助手完成，下次启动即为新版本，不会重复下载。
- 强制更新由清单中的 `metadata.minVersion` 决定。当当前版本低于该值时，更新界面会接管主窗口。
- 强制更新期间如果字幕编辑器仍然打开，应用会提示用户保存并关闭编辑器，关闭后再安装，避免丢失未保存内容。
- 更新成功后的首次启动会展示一次对应版本的更新说明。
- Windows 更新替换 exe 后，新版本会清理更新助手遗留的旧可执行文件。
- 普通下载或检查失败不会阻止应用启动；强制更新失败后每分钟重试。

开发构建默认不自动更新。需要联调更新流程时，显式设置测试清单：

```powershell
$env:NONOKA_UPDATE_MANIFEST = "http://127.0.0.1:8080/latest.json"
```

可用的客户端环境变量：

| 变量 | 作用 |
| --- | --- |
| `NONOKA_UPDATE_MANIFEST` | 直接覆盖完整清单 URL；开发构建设置后也会启用更新检查 |
| `NONOKA_UPDATE_URL` | 覆盖更新根地址，客户端会追加 `/latest.json` |
| `NONOKA_DISABLE_UPDATE=1` | 完全禁用自动更新 |

主要实现位于：

- `internal/selfupdate/service.go`：检查、下载、强制更新、重启及更新说明。
- `internal/selfupdate/install.go`：退出时替换可执行文件的更新助手（不重启，仅替换）。
- `internal/selfupdate/cleanup.go`：清理 Windows 更新替换产生的旧 exe。
- `frontend/src/components/UpdateCenter.tsx`：下载状态、普通更新按钮、强制更新界面及更新说明。
- `frontend/src/editor/hooks/useDesktopEvents.ts`：强制更新时保护编辑器中的未保存工作。

## 2. 发布前写更新说明

在仓库根目录的 `CHANGELOG.md` 顶部新增版本小节。标题可以写成 `## x.y.z` 或 `## vx.y.z`，推荐省略 `v`：

```markdown
## 0.2.0

- 修复 xxx 导致的崩溃
- 改进 xxx 的处理速度
```

本地自动更新脚本要求对应版本的小节存在且非空，并只把该小节写入更新清单。GitHub Actions 找不到对应小节时，会退回到 GitHub 自动生成的提交说明。

## 3. 同步版本号

不要手工逐个修改版本。统一使用：

```powershell
cd D:\Development\nonoka-x\desktop

# 只同步版本号，不构建、不生成更新包
.\scripts\release-update.ps1 -Version 0.2.0 -VersionOnly
```

脚本会同步以下文件中的版本字段：

- `frontend/package.json`
- `frontend/package-lock.json`
- `internal/selfupdate/service.go`
- `build/config.yml`
- `build/windows/info.json`
- `build/windows/wails.exe.manifest`
- `build/darwin/Info.plist`
- `build/darwin/Info.dev.plist`
- 仓库根目录 `pyproject.toml`（存在时）

同步后应检查差异，并确保 `CHANGELOG.md` 也包含同一版本。

## 4. 发布到 GitHub Releases

仓库根目录的 `.github/workflows/release.yml` 监听 `main` 分支上的 `desktop/build/config.yml`。提交并推送版本变更后会自动执行：

```powershell
git add CHANGELOG.md desktop
git commit -m "chore: release 0.2.0"
git push origin main
```

工作流依次执行：

1. 从 `desktop/build/config.yml` 读取版本号。
2. 检查 `v<版本>` Release 是否已经存在；正常推送时已存在则跳过。
3. 在 Windows runner 构建 `Nonoka Sub X.exe`（amd64）。
4. 在 macOS runner 构建 universal `Nonoka Sub X.app`，使用 `ditto` 打包 zip，以保留权限和符号链接。
5. 合并两个 runner 的产物并创建 GitHub Release 和 tag。
6. 优先使用 `CHANGELOG.md` 对应小节作为 Release 正文；缺失时自动生成说明。

也可以在 GitHub Actions 页面手动运行 `Release` 工作流。启用 `force` 后，即使相同版本的 Release 已存在，也会重新构建并覆盖其中的产物。该流程不会发布 R2 自动更新清单。

> macOS 产物目前没有签名和公证。首次打开可能需要在 Finder 中右键应用并选择“打开”。

## 5. 生成应用内更新包

`scripts/release-update.ps1` 是统一入口。默认平台为 Windows，默认架构为 amd64。

```powershell
# 同步版本、构建 Windows、生成并校验清单，但不上传
.\scripts\release-update.ps1 -Version 0.2.0 -Platform windows

# 构建并准备 Windows 与 macOS 更新包
.\scripts\release-update.ps1 -Version 0.2.0 -Platform all

# 已经完成构建：复用 bin 下的产物生成更新包
.\scripts\release-update.ps1 -Version 0.2.0 -Platform windows -SkipBuild

# 指定其他纯文本或 Markdown 文件作为完整更新说明
.\scripts\release-update.ps1 -Version 0.2.0 -NotesFile release-notes.md

# 要求低于 0.1.5 的客户端强制更新到 0.2.0
.\scripts\release-update.ps1 -Version 0.2.0 -Platform windows -MinVersion 0.1.5
```

执行过程包括：

1. 同步全部版本字段。
2. 调用 Wails 构建目标平台。
3. 从 `bin/Nonoka Sub X.exe` 或 `bin/Nonoka Sub X.app` 收集产物。
4. 将更新文件写入 `bin/update/<版本>/`。
5. 调用 `wails3 updater manifest` 生成带摘要的 `latest.json`。
6. 从 `CHANGELOG.md` 摘取对应版本说明；使用其他 `-NotesFile` 时读取整份文件。
7. 写入可选的 `metadata.minVersion`。
8. 调用 `wails3 updater verify` 校验清单和文件摘要。

生成结果示例：

```text
desktop/bin/update/0.2.0/
├── Nonoka-Sub-X-0.2.0-windows-amd64.exe
├── Nonoka-Sub-X-0.2.0-darwin-universal.zip
└── latest.json
```

`-SkipBuild` 只跳过编译，仍要求 `bin/` 中存在对应平台的最新构建产物。只想改版本号时应使用 `-VersionOnly`。

## 6. 发布应用内更新到 R2

在上述命令后增加 `-Publish`：

```powershell
# 常用：构建并发布 Windows 更新
.\scripts\release-update.ps1 -Version 0.2.0 -Platform windows -Publish

# 构建并发布两个平台
.\scripts\release-update.ps1 -Version 0.2.0 -Platform all -Publish

# 复用现有 Windows 构建并发布
.\scripts\release-update.ps1 -Version 0.2.0 -Platform windows -SkipBuild -Publish
```

首次使用前安装发布脚本依赖：

```powershell
npm install
```

发布器从环境变量或 `desktop/.env.release` 读取以下必需配置：

```dotenv
R2_ACCOUNT_ID=...
R2_ACCESS_KEY_ID=...
R2_SECRET_ACCESS_KEY=...
R2_BUCKET=nonoka-x-cloud
```

R2 API 令牌需要对 `nonoka-x-cloud` 有对象读写权限；发布一次性迁移版本时还需要对 `youtube-live-cache` 有读写权限。

可选配置：

| 变量 | 默认值 | 作用 |
| --- | --- | --- |
| `R2_WAILS_PREFIX` | `desktop-updates/` | R2 对象前缀 |
| `R2_LEGACY_BUCKET` | `youtube-live-cache` | 一次性迁移开关写入的旧桶 |
| `R2_ENDPOINT` | `https://<R2_ACCOUNT_ID>.r2.cloudflarestorage.com` | S3 兼容端点 |
| `R2_FORCE_PATH_STYLE=1` | 未启用 | 为兼容的 S3 服务启用 path-style 请求 |

发布器在上传前会再次验证清单格式、平台、文件名和摘要。更新包使用长期 immutable 缓存，`latest.json` 使用 `no-cache`。发布单个平台时，如果远端清单已经是相同版本，脚本会保留远端另一平台的产物并合并清单；不同版本的产物不会被合并。

`-PublishLegacyUpdatePathOnce` 会把同一组产物和清单额外发布到旧客户端使用的位置 `youtube-live-cache` 桶的 `nonoka-x-updates/wails/`。0.5.0 及更早的客户端只认这个地址，所以迁移到新桶后的第一个版本必须带上该开关，让旧客户端先升级到读取新地址的版本；之后的版本省略它，发布器就只写入 `nonoka-x-cloud` 桶的 `desktop-updates/`。

```powershell
# 迁移后的首个版本：同时写入新桶和旧地址
.\scripts\release-update.ps1 -Version 0.5.1 -Platform windows -Publish -PublishLegacyUpdatePathOnce
```

> `.env.release` 包含敏感凭证，已被 `desktop/.gitignore` 忽略，禁止提交到 Git。

## 7. 推荐发布顺序

以 `0.2.0` 为例：

```powershell
# 1. 先编辑仓库根目录 CHANGELOG.md
# 2. 同步版本号
.\scripts\release-update.ps1 -Version 0.2.0 -VersionOnly

# 3. 本地测试
go test ./...
npm.cmd --prefix frontend run typecheck
npm.cmd --prefix frontend test
npm.cmd --prefix frontend run build
node --test scripts/publish-update.test.mjs

# 4. 提交并推送，触发 GitHub Release
cd ..
git add CHANGELOG.md desktop .github/workflows/release.yml
git commit -m "chore: release 0.2.0"
git push origin main

# 5. 回到 desktop，构建并发布应用内更新
cd desktop
.\scripts\release-update.ps1 -Version 0.2.0 -Platform windows -Publish
```

如果 Windows 与 macOS 分别在不同机器发布，二者必须使用同一版本号；第二次上传会合并相同版本的清单。完成后应访问默认 `latest.json` URL，确认返回的 `version` 和 `artifacts` 与本次发布一致。

## 8. 回滚与故障排查

### 客户端没有发现新版本

- 确认运行的是 production 构建；开发构建默认不检查线上更新。
- 确认 `internal/selfupdate/service.go` 的 `Version` 已同步。
- 检查 `latest.json` 的版本是否高于客户端版本。
- 检查清单是否包含当前系统对应的 `platform` 和 `arch`。
- 检查 CDN/R2 公网地址是否能直接访问清单和清单中的产物 URL。
- 检查是否设置了 `NONOKA_DISABLE_UPDATE=1` 或错误的覆盖 URL。

### `prepare-update.ps1` 报找不到产物

- 不使用 `-SkipBuild`，让脚本重新构建。
- Windows 应存在 `desktop/bin/Nonoka Sub X.exe`。
- macOS 应存在 `desktop/bin/Nonoka Sub X.app`。

### 发布器报缺少 R2 凭证

- 检查变量名称是否完全一致。
- 检查 `.env.release` 是否位于 `desktop/`，不是仓库根目录。
- 修改 `.env.release` 后重新执行发布命令。

### 需要撤回有问题的版本

不要用较低版本覆盖 `latest.json` 来依赖客户端降级；更新器只面向更高版本。应修复问题、增加补丁版本，在 `CHANGELOG.md` 中说明并重新发布。若必须暂时停止更新，可从 R2 撤下或替换清单，但操作前应保留当前清单和产物备份。

## 9. 相关文件

| 文件 | 责任 |
| --- | --- |
| `.github/workflows/release.yml` | 自动构建并发布 GitHub Release |
| `CHANGELOG.md` | 两条发布通道共用的更新说明 |
| `desktop/scripts/release-update.ps1` | 同步版本、构建、准备并可选发布更新 |
| `desktop/scripts/prepare-update.ps1` | 收集产物、生成清单、写强制版本并校验 |
| `desktop/scripts/publish-update.mjs` | 验证并上传 R2，合并同版本多平台清单 |
| `desktop/scripts/publish-update.test.mjs` | R2 清单合并行为测试 |
| `desktop/internal/selfupdate/` | 客户端自动更新实现与测试 |
| `desktop/frontend/src/components/UpdateCenter.tsx` | 更新 UI |
