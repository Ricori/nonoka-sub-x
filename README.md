<div align="center">
  <img src="desktop/frontend/public/assets/nonoka.png" alt="Nonoka" width="200">

  <h1>Nonoka Sub X</h1>

  <p><b>AI 视频字幕翻译与编辑的桌面工作台</b></p>

  <p>
    <a href="https://github.com/Ricori/nonoka-sub-x/releases/latest"><img src="https://img.shields.io/github/v/release/Ricori/nonoka-sub-x?label=Release&color=4c1&sort=semver" alt="Release"></a>
    <a href="https://github.com/Ricori/nonoka-sub-x/releases/latest"><img src="https://img.shields.io/badge/Download-Windows%20%7C%20macOS-2ea44f?logo=github&logoColor=white" alt="Download"></a>
    <a href="./LICENSE"><img src="https://img.shields.io/badge/License-GPL--3.0-blue.svg" alt="License"></a>
  </p>
  <p>
    <a href="https://wails.io/"><img src="https://img.shields.io/badge/Wails-v3-blue.svg" alt="Wails"></a>
    <a href="https://golang.org/"><img src="https://img.shields.io/badge/Go-1.25+-00ADD8.svg" alt="Go"></a>
    <a href="https://reactjs.org/"><img src="https://img.shields.io/badge/React-19-61DAFB.svg" alt="React"></a>
  </p>
</div>

<br>

Nonoka Sub X 是为外语视频翻译与字幕制作打造的桌面应用。项目整合了高精度转写流水线与易用的字幕编辑器，兼具 Aegisub 的精准可控与剪映的直观便捷，支持**本地离线**与**云端**双模式无缝切换。

**目录**：[下载安装](#-下载安装) · [核心特性](#-核心特性) · [快速开始](#-快速开始开发指南) · [插件开发](#-插件开发) · [项目结构](#-项目结构) · [数据与存储路径](#-数据与存储路径) · [进阶架构与规范](#-进阶架构与规范)

---

## ⬇️ 下载安装

前往 **[Releases 最新发布页](https://github.com/Ricori/nonoka-sub-x/releases/latest)** 获取对应平台的安装包：

| 平台 | 文件名 | 说明 |
| :--- | :--- | :--- |
| **Windows** | `Nonoka-Sub-X-<版本>-windows-amd64.exe` | 免安装单文件，自带环境引导，双击即用 |
| **macOS** | `Nonoka-Sub-X-<版本>-macOS.zip` | 适配 Apple Silicon 与 Intel 架构 |

> Windows 用户初次使用本地转写时，应用会自动引导部署隔离的 Python 3.12 运行环境，无需手动安装 Python 或配置系统 PATH。

---

## ✨ 核心特性

- 🎙️ **全链路 AI 字幕转写与翻译**
  - **人声分离**：采用神经网络模型精确剥离伴奏与背景噪音。
  - **精准对齐与 VAD**：结合 energy VAD 与补丁版 CTranslate2 / Whisper 引擎，实现毫秒级词级时间戳定位。
  - **智能纠错与翻译**：LLM 上下文感知纠错与高质量中日双语翻译，告别生硬机翻。
  - **知识库系统**：自动收集翻译中所学到的知识，用于改善下次翻译。
- ⚡ **本地与云端同源双执行模式**
  - **本地模式**：NVIDIA GPU 本地直接推理，私密数据不出机，支持配置用户自持的 LLM API Key，也可直接把 LLM 环节路由到本机已登录的 Codex CLI / Antigravity CLI / WorkBuddy （无需 API Key）。
  - **云端模式**：一键调度云端 GPU 算力，桌面端仅提取上传无损纯音频（零损失、保护原片隐私），断点自动保存与续跑。
  - **完全同源**：本地与云端运行完全一致的引擎与校验规则，保证产物质量一致。
- 🎬 **专业级轨道编辑与所见即所得**
  - **JASSUB 渲染引擎**：基于 WebAssembly 的专业 ASS 字幕实时渲染，与主流播放器像素级对齐。
  - **多轨可视化时间轴**：波形图、词级对齐高亮、低置信度警告段落标记、多轨道拖拽。
  - **灵活导出**：支持导出 SRT、带样式的 ASS 字幕，以及硬件加速的**字幕内嵌压制视频**。
- 🛡️ **轻量、安全与现代化体验**
  - **原生 Go + React 19 架构**：依托 Wails v3，包体仅 20M，超低内存占用，无卡顿秒启动。
  - **智能缓存管理**：内置视频缓存策略，工程受保护，支持自定义缓存上限。
  - **乐观锁存储**：工程快照与版本历史落盘保护，防止并发覆盖。

---

## 🚀 快速开始（开发指南）

### 1. 环境准备
确保开发机已安装：
- **Go (1.25+)**
- **Node.js (18+)** 与 **npm**
- **Python (3.12)**
- **[Task](https://taskfile.dev/)**（推荐）：`go install github.com/go-task/task/v3/cmd/task@latest`

### 2. 启动桌面端开发
进入 `desktop/` 目录并启动开发服务，将同时拉起 Go 后端与 Vite 前端热更新：

```bash
cd desktop
task dev
```

### 3. 常用开发与构建命令

#### 桌面端命令 (`desktop/` 目录下)
| 命令 | 说明 |
| :--- | :--- |
| `task dev` | 启动开发模式（Wails 运行时 + 前端热重载，端口 9245） |
| `task verify` | 自动生成 bindings、装配 sidecar 资源并执行完整桌面校验 |
| `task build` | 构建当前平台的生产可执行文件到 `bin/` |
| `task package` | 产出分发包（Windows 单文件 `Nonoka Sub X.exe`，macOS `Nonoka Sub X.app`） |
| `task run` | 运行已构建的桌面应用产物 |
| `go test ./...` | 运行 Go 后端单元测试 |
| `npm --prefix frontend run typecheck` | 前端 TypeScript 类型检查 |
| `npm --prefix frontend run test` | 前端单元测试 |

#### 算法引擎与云端命令
| 命令 | 说明 |
| :--- | :--- |
| `python scripts/sync_finesub.py check` | 离线校验 FineSub 固定快照与补丁完整性 |
| `python -m pytest -q` | 运行 Python 引擎契约、Projector 与文档存储测试 |
| `python -m scripts.build_finesub_bundle` | 构建本地/云端通用的 Engine Bundle |

---

## 🧩 插件开发

Nonoka Sub X 插件可以在左侧“工具”菜单中增加独立页面，并通过权限受控的宿主 API 复用媒体库、转写引擎 和 LLM 等能力。插件可单独安装、启用、停用和卸载。

### 1. 创建最小插件

插件包是一个扩展名为 `.nonoka-plugin` 的 ZIP 文件，根目录包含 manifest 和自包含的 HTML 页面：

```text
hello-tool/
├── nonoka-plugin.json
└── ui/
    └── index.html
```

`nonoka-plugin.json`：

```json
{
  "id": "com.example.hello-tool",
  "name": "Hello Tool",
  "version": "1.0.0",
  "apiVersion": 1,
  "permissions": ["media.list"],
  "contributes": {
    "tools": [
      {
        "id": "hello",
        "title": "示例工具",
        "page": "ui/index.html",
        "order": 100
      }
    ]
  }
}
```

`ui/index.html` 中通过宿主注入的 `window.nonoka.post()` 调用 API：

```html
<!doctype html>
<html lang="zh-CN">
  <body>
    <button id="load">读取媒体库</button>
    <pre id="result"></pre>
    <script>
      document.querySelector("#load").onclick = () => {
        window.nonoka.post("media.list", {}, "media-1");
      };

      window.addEventListener("message", (event) => {
        const message = event.data;
        if (message?.source !== "nonoka-host" || message.method !== "rpc.result") return;
        if (message.id === "media-1") {
          document.querySelector("#result").textContent =
            message.error || JSON.stringify(message.result, null, 2);
        }
      });
    </script>
  </body>
</html>
```

插件页面运行在隔离的 iframe 中，不能直接访问本地文件或 Wails bindings。需要使用的宿主能力必须先在 `permissions` 中声明。

### 2. 打包并安装

在插件目录中执行：

```powershell
Compress-Archive -Path .\nonoka-plugin.json,.\ui -DestinationPath .\hello-tool.zip
Rename-Item .\hello-tool.zip hello-tool.nonoka-plugin
```

启动 Nonoka Sub X，在左侧进入“工具 → 插件管理”，选择生成的 `.nonoka-plugin` 文件。安装后插件贡献的页面会自动出现在“工具”分组下。

### 3. 完整 Demo：YouTube / Twitch 下载器

[`desktop/examples/plugins/video-downloader`](desktop/examples/plugins/video-downloader) 提供了一个可直接打包安装的完整示例。它演示了：

- 插件自行构造安全的 yt-dlp 参数；
- 通过 `tools.runYtDLP` 请求 Nonoka Sub X 执行项目托管的 yt-dlp 与 FFmpeg；
- 选择视频清晰度、显示下载结果，并在完成后自动导入媒体库；
- 使用 `tools.yt-dlp` 和 `media.import` 权限隔离高风险能力。

运行 Demo 前，请先在 Nonoka Sub X“运行环境”页面安装 FFmpeg 和可选工具 yt-dlp。当前示例支持公开的 YouTube 视频、Twitch VOD 和 Twitch Clips，不读取浏览器 Cookie，也不支持需要登录的内容。

### 4. 引擎能力：LLM、阶段与中间产物

插件还可以借用引擎本身，不必自带 API Key、也不必自己装模型：

| 权限 | 能做什么 |
| :--- | :--- |
| `llm.complete` | 插件写 prompt，宿主用用户已配置的模型（API Key 或本机 Codex / Antigravity / WorkBuddy CLI）跑一次，返回文本。插件不需要拿到密钥。 |
| `engine.run` | 只把流水线跑到指定阶段（`vocal` / `aligned` / `stable` / `raw-srt` / `translated-srt` / `final-srt`），复用宿主的任务队列、事件与取消。 |
| `engine.artifacts` | 取回那次运行的中间产物：文本直接读内容，音频等二进制只能经保存对话框落盘。 |

[`desktop/examples/plugins/subtitle-studio`](desktop/examples/plugins/subtitle-studio) 把这三样都用了一遍。

更多 manifest 字段、权限、消息协议和生命周期说明请阅读 **[Nonoka Sub X 插件开发文档](desktop/docs/PLUGINS.md)**。另有 **[最小 Hello Tool 示例](desktop/examples/plugins/hello-tool)** 可用于快速复制修改。

---

## 📁 项目结构

```text
nonoka-x/
├── desktop/                  # 桌面端应用主工程
│   ├── main.go               # 桌面应用入口与资源装配
│   ├── Taskfile.yml          # 构建、打包与开发任务编排
│   ├── internal/app/         # Wails 核心服务、本地流媒体网关、缓存与更新
│   └── frontend/             # React 19 前端工程（媒体库、JASSUB 编辑器、轨道视图）
├── src/nonoka_x/             # Python 适配层、Artifact Projector 与 DocumentStore
├── third_party/finesub/      # 固定 commit 的 FineSub 算法引擎快照（生成目录）
├── patches/finesub/          # 维护的 FineSub 专用补丁栈
├── scripts/                  # 引擎同步、构建与运维脚本
└── docs/                     # 架构与核心规范文档
```

---

## 📂 数据与存储路径

应用会在系统标准应用数据目录中维护配置与工作缓存：

- **数据根目录**（可通过 `NONOKA_DATA_DIR` 环境变量重定向）：
  - **Windows**: `%APPDATA%\Nonoka X`
  - **macOS**: `~/Library/Application Support/Nonoka X`
- 首次运行改名后的版本时，如果新目录尚不存在而旧的 `Finoka` 数据目录存在，应用会完整移动该目录、提示迁移完成并退出；重新启动后使用新目录。
- **核心子目录与文件**：
  - `videos/`：大容量视频工作副本缓存（受 LRU 上限约束）。
  - `documents/`：字幕工程、波形缓存与编辑版本历史（`document.json`、`history/` 等）。
  - `tasks/`：本地转写任务中间产物与日志。
  - `thumbs/`：媒体快照缩略图。
  - `runtime/`：Windows 端由 `uv` 自动管理的隔离 Python 与模型环境。
  - `config.json`：全局偏好设置与窗口状态。
  - `library.json`：本地媒体库与工程索引。

---

## 📚 进阶架构与规范

- 🏗️ **[系统架构与契约规范](docs/architecture.md)**：深入了解 Provider 执行协议、EditDocument 投影规则、云端同步契约与后端 API。
- 🔌 **[Nonoka Sub X Provider 接口规范](docs/provider-spec.md)**：自建执行后端的完整接口契约——数据模型、状态机、事件流、错误码、产物格式与一致性检查清单。
- ⚙️ **[FineSub 引擎同步与构建规范](docs/engine.md)**：了解上游引擎同步白名单、补丁栈管理与 CTranslate2 构建规范。

---

## 📄 开源协议

本项目采用 **[GPL-3.0](./LICENSE)** 开源许可证。
底层的 FineSub 算法代码采用 **MIT** 许可证，Prompt 模板遵循 **CC BY-SA 4.0** 许可。

<div align="center">
  <br>
  <img src="desktop/frontend/public/assets/mascot-wave.png" alt="Nonoka" width="96">
  <p><sub>一起烤肉吧！</sub></p>
</div>
