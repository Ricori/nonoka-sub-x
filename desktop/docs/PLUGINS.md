# Nonoka Sub X 插件开发（API v1）

Nonoka Sub X 插件在主页左侧的“工具”分组中贡献入口。插件代码、持久数据和缓存相互分离，因此插件可以独立启用、停用、升级和卸载。

## 系统插件与用户插件

插件分两种来源，格式完全相同（同一份 manifest、同一套权限、同样的沙箱页面），只有安装位置不同：

| | 系统插件 | 用户插件 |
| --- | --- | --- |
| 来源 | 编译进 Nonoka Sub X 二进制，启动时发布到 `system-plugins/<id>/` | 用户选择 `.nonoka-plugin` 安装到 `plugins/<id>/` |
| 默认状态 | 启用 | 安装后启用 |
| 停用 | 可以 | 可以 |
| 卸载 | 不可以，属于程序的一部分 | 可以 |
| yt-dlp 参数集 | 更宽（见下） | 保守集合 |

要点：

- **信任由安装位置决定，不看 manifest。** 插件无法通过在 manifest 里写任何字段把自己变成系统插件。
- 系统插件的 id 被占用，用户安装同 id 的包会被拒绝，因此没有“冒名顶替拿到更宽权限”的路径。
- 系统插件同样要通过完整的 manifest 校验；不合法的内置包会在启动时报错，而不是静默上线。
- 发布按**内容摘要**进行，不看版本号：内置包的字节完全由构建决定，只认版本号的话，改了页面却忘了升版本就会一直用旧副本，而且完全没有提示。摘要变了就重新解压并切换 `current.json`，旧版本随后清理。用户停用过的系统插件，升级后仍保持停用。

## 插件包

`.nonoka-plugin` 是一个 ZIP 文件，根目录必须包含 `nonoka-plugin.json`。manifest 和所有页面都必须位于压缩包根目录之下；绝对路径、`..`、符号链接和重复文件会被拒绝。

```text
example.nonoka-plugin
├─ nonoka-plugin.json
└─ ui/
   └─ index.html
```

最小 manifest：

```json
{
  "id": "com.example.subtitle-tools",
  "name": "字幕工具",
  "version": "1.0.0",
  "apiVersion": 1,
  "permissions": ["media.list", "ffmpeg.extract-audio"],
  "contributes": {
    "tools": [
      {
        "id": "cleanup",
        "title": "字幕清理",
        "page": "ui/index.html",
        "order": 100
      }
    ]
  }
}
```

- `id` 和工具 `id` 只能使用小写字母、数字、点、横线和下划线。
- `version` 使用 SemVer。
- 当前仅支持 `apiVersion: 1`。
- `page` 必须是包内相对路径，并以 `.html` 结尾。
- 同一个插件可以贡献多个工具入口。
- `permissions` 只允许列出当前 API 版本认识的宿主能力，未知权限会阻止安装。

API v1 认识的权限：

| 权限 | 能力 |
| --- | --- |
| `media.list` | 列出媒体（ID、标题、时长、尺寸、字幕状态） |
| `media.import` | 把宿主下载到的文件导入媒体库 |
| `media.export-video` | 压制字幕导出 MP4 |
| `document.read` | 读取字幕文档、生成 ASS/SRT |
| `document.write` | 写回字幕文档（带 rev 乐观锁） |
| `subtitle.export` | 通过保存对话框导出字幕文件 |
| `ffmpeg.extract-audio` | 用托管 FFmpeg 导出音频 |
| `tools.yt-dlp` | 执行受控的 yt-dlp 命令 |
| `llm.complete` | 用用户已配置的模型跑一次 LLM 调用 |
| `engine.run` | 把 FineSub 流水线跑到指定阶段 |
| `engine.artifacts` | 读取或落盘该次运行的中间产物（与 `engine.run` 同时声明才有意义） |

权限只在 manifest 里声明，安装时展示在插件管理页；每次调用由 Go 侧再校验一次，停用的插件调用任何能力都会被拒绝。

开发时也可以调用 Go service 的 `Install(path)` 安装未打包目录；桌面 UI 只选择 `.nonoka-plugin` 文件。

PowerShell 打包示例：

```powershell
Compress-Archive -Path .\hello-tool\* -DestinationPath .\hello-tool.zip
Rename-Item .\hello-tool.zip hello-tool.nonoka-plugin
```

## 页面隔离

工具页面通过 `<iframe sandbox="allow-scripts">` 加载，没有同源权限，也不能访问 Wails bindings。API v1 页面必须是自包含 HTML：CSS 和 JavaScript 应内联，图片可使用 `data:` 或 `blob:`。宿主会注入 CSP，默认禁用网络、外部脚本和本地文件访问。

宿主**不给页面套面板**：iframe 被撑满整个工作区，边框、圆角、底色、内边距全部由插件页自己画，要不要卡片、要几张、怎么分区都由插件决定。因此页面要自己负责两件事：

- **背景**。`html`/`body` 留透明就能让宿主的工作区底色透上来；但如果同时写死了 `color-scheme`，页面比工作区短的那一截会被浏览器用该配色的默认画布涂成一块死色（深色下就是纯黑）。要么跟着主题切 `color-scheme`，要么自己铺满底色。
- **主题**。插件页是独立文档，读不到宿主的 CSS 变量，需要自己抄一份色板，并在收到 `host.getInfo` / `host.info` 的 `theme` 时切换。原生控件（`<select>` 的箭头和下拉面板）跟的是 `color-scheme`，别忘了一起切，否则浅色主题里下拉是一块突兀的深色。

内置的 `dev.nonoka.youtube-downloader` 就是按这个写的，可以直接抄。

宿主注入：

```js
window.nonoka.post("host.getInfo", {});
```

插件向宿主发送的消息格式：

```json
{
  "source": "nonoka-plugin",
  "apiVersion": 1,
  "method": "host.getInfo",
  "params": {}
}
```

宿主目前实现：

- `host.getInfo`：返回主题、locale、插件 ID 和工具 ID。
- `ui.openPluginManager` / `ui.openLibrary` / `ui.openRuntime`：切到对应的宿主页面，无返回值。
- `media.list`：需要 `media.list` 权限，返回媒体 ID、标题、时长、画面尺寸和字幕状态，不返回本地路径。
- `document.read`：需要 `document.read` 权限。参数 `{ mediaId }`，返回该媒体的字幕文档（`rev`、`subtitles`、`tracks`、`track_meta` 等，即编辑器打开的那一份）。
- `document.save`：需要 `document.write` 权限。参数 `{ mediaId, document }`，宿主只取 `rev`、`subtitles`、`tracks`、`track_meta`、`effects`、`title` 六个字段写回，返回保存后的文档。
- `subtitle.ass` / `subtitle.srt`：需要 `document.read` 权限。参数 `{ mediaId, t0?, t1?, lang? }`，返回 `{ text, rev }`。字幕由宿主用编辑器那条拼装管线现拼，因此与编辑器导出的逐字相同；给了 `t0`/`t1` 就裁成区间并把时间轴平移到 0，`lang` 只对 SRT 有效（`both` / `zh` / `ja`）。
- `subtitle.save`：需要 `subtitle.export` 权限。参数 `{ fileName, content }`，宿主弹保存对话框并落盘，返回最终路径；用户取消时返回空串。扩展名只接受 `.ass` 和 `.srt`，目录由对话框决定。
- `media.exportVideo`：需要 `media.export-video` 权限。参数 `{ mediaId, ass, fileName?, t0?, t1?, height? }`，宿主用托管的 FFmpeg 把 ASS 压制进视频，返回 `{ path, format, size }`。编码参数（CRF 21、preset medium、AAC 192k）由宿主固定，`height` 只能是 0（原分辨率）或 240–4320。压制期间宿主会向页面推送 `media.progress` 消息（`{ done, total }`，单位秒）。
- `ffmpeg.extractAudio`：需要 `ffmpeg.extract-audio` 权限。参数为 `{ mediaId, format }`，其中格式只能是 `wav`、`flac`、`mp3` 或 `m4a`。Nonoka Sub X 负责解析输入、选择输出位置、构造 FFmpeg 参数和原子发布结果。
- `tools.runYtDLP`：需要 `tools.yt-dlp` 和 `media.import` 权限。插件传入 `{ url, args }`，自行决定受支持的 yt-dlp 参数；Nonoka Sub X 解析项目托管的 yt-dlp/FFmpeg、让用户选择输出位置，并在命令成功后自动导入媒体库。yt-dlp 以 Python wheel 形式安装，因此该能力同时依赖 Nonoka Sub X 的 Python 运行时。下载单次上限 20 分钟，超时会结束子进程并报错，避免卡住的分片服务器一直占着媒体任务锁。下载期间宿主向页面推送 `download.progress` 消息（`{ done, total }`，`total` 恒为 100，即百分比），节流到 400ms 一条。
- `llm.complete`：需要 `llm.complete` 权限。参数 `{ role, messages, maxTokens?, temperature? }`，返回 `{ content, model, backend, fallbackUsed }`。详见下文「引擎能力」。
- `engine.startTask`：需要 `engine.run` 权限。参数 `{ mediaId, target, language?, skipSeparation?, correction? }`，返回任务快照（含 `task_id`）。
- `engine.status` / `engine.events` / `engine.cancel`：需要 `engine.run` 权限。参数 `{ taskId }`（`engine.events` 另接 `after` 游标），只认本插件起过的任务。
- `engine.artifacts`：需要 `engine.artifacts` 和 `engine.run` 权限。参数 `{ taskId }`，返回 `[{ name, bytes, readable }]`，按流水线顺序排列，**不含路径**。
- `engine.readArtifact`：需要 `engine.artifacts` 和 `engine.run` 权限。参数 `{ taskId, name }`，返回文本产物的内容，上限 8 MB。
- `engine.saveArtifact`：需要 `engine.artifacts` 和 `engine.run` 权限。参数 `{ taskId, name, fileName? }`，宿主弹保存对话框并落盘，返回 `{ path, format, size }`；用户取消时 `path` 为空串。

字幕文档能力有几条固定规则：

- 媒体一律用 `media.list` 给的 ID 定位，插件永远拿不到本地路径；字幕文档由本地 Provider 提供，sidecar 没起来时这些调用会失败而不是读到半份文档。
- `document.save` 必须带上读到的 `rev`。编辑器同时保存过同一份文档时 rev 已经前进，宿主返回 `revision_conflict`，插件应重新读一次再改。
- 宿主会校验句子形状（`t0`/`t1` 有限且 `t1 >= t0`，`ja`/`zh` 是文本），但不认识的字段（词级时间、置信度）原样带过去，因此「读—改—写」不会丢数据。
- `subtitle.save` 与 `media.exportVideo` 的落盘位置永远由保存对话框决定，插件给的文件名只当默认值用，路径部分会被剥掉。

`tools.runYtDLP` 当前只接受公开的 YouTube、youtu.be 和 Twitch HTTPS 地址，并且只允许以下参数：

- `--no-playlist`、`--newline`、`--embed-metadata`
- `-f` / `--format`：最佳、最高 1080p、最高 720p 三种预定义 selector
- `--merge-output-format mp4`、`--remux-video mp4`
- `-N` / `--concurrent-fragments`：`1`、`2`、`4` 或 `8`

系统插件在此基础上额外允许（用户插件用这些参数会被拒绝）：

- `-f` / `--format`：最高 1440p、最高 2160p 两种 selector
- `-N` / `--concurrent-fragments`：`16`
- `--no-part`：直接写最终文件名，配合多连接下载（它本来就是乱序写盘）

此外，系统插件调用时如果能找到 `aria2c`，宿主会自动追加 `--downloader dash,m3u8,http:<aria2c>` 和 `--downloader-args aria2c:-x 16 -s 16 -k 1M` 走多连接下载。aria2c 是「运行环境」里的**可选工具**，装了就加速，没装就用 yt-dlp 自带的下载器，只是慢一些 —— 它不是下载功能的前置条件。外部下载器不开放给用户插件，否则等于在沙箱后面多放一个独立配置的网络客户端。

### YouTube 的四道门禁

YouTube 按顺序设了四道关卡，**缺任何一道，失败都会在更早的关卡上暴露出来** —— 这就是为什么单独加任何一样都看不出效果：

| 门禁 | 宿主提供的东西 | 缺了会怎样 |
| --- | --- | --- |
| 已登录会话 | `--cookies`（用户在插件页粘贴） | 只剩 `android_vr` 兜底客户端 |
| GVS PO token | PO Token 插件 + 服务地址 | `mweb` 报 “requires a GVS PO Token” |
| n challenge | `--js-runtimes` + `--remote-components ejs:github` | 「n challenge solving failed」，格式残缺 |
| 客户端 | `youtube:player_client=mweb` | 前三样齐了才该切；否则 `mweb` 一个格式都不给 |

四样齐备后，实测可以完整下载先前一直失败的长视频。凑不齐时宿主不会强切 `mweb`：没有 cookies 和 token 时 `android_vr` 至少还能列出格式，短视频仍下得动。

几条容易走弯路的地方，都实测过：

- **单独加 cookies 会更糟**：`android_vr` 会从「能列出格式」变成 0 格式，把唯一能用的兜底路径也弄坏。
- **`--remote-components ejs:github` 单独用是空转的**，它需要 JS 运行时才能执行；但四样齐备后它是承重的一环，不能撤。
- **aria2c 与成功率无关**，只影响速度；它会把同一个 403 包装成 `exit code 22`。

### PO Token 服务（本地安装）

token 由一个外部 provider 生成，它分两半：yt-dlp 插件，和真正干活的生成端。上游只发布插件（8KB wheel），**从不发布构建好的生成端** —— 后者需要 `npm ci` 加原生 `canvas`，没法表达成单个 pinned 压缩包。

所以这三样都作为**可选工具**装在用户机上，全部走「单压缩包 + sha256」的既有模型：

| 资源 id | 是什么 | 来源 |
| --- | --- | --- |
| `pot-provider` | yt-dlp 插件 **和** token 生成端 | **需自行构建发布**，见下 |
| `node` | JavaScript 运行时 | Node.js 官方 win-x64 zip |

插件和生成端上游是**配套版本**，拆成两个独立 pin 的资源会给「版本漂移」留口子，所以打成同一个包：解压出的目录同时是 `PYTHONPATH` 和 `server_home` 指向的地方，两个解析器各查各的标志文件。

这几样在「运行环境」里合并显示为一个 **视频下载工具** 分组（连同 yt-dlp、aria2c），可以整组一次装完 —— 单独装其中一样没有意义。安装目标 `video-tools` 会逐个装组员，清单里还没声明的成员（例如尚未发布的 `pot-provider`）跳过而不是让整组失败。

装齐后宿主走 **script 模式**（`youtubepot-bgutilscript:server_home=…`）：生成端按需 spawn 一次，桌面端**不常驻进程、不开监听端口**。三样缺任何一个，宿主就不追加这些参数、也不切 `mweb`，下载退回默认客户端 —— 短视频仍然能下。

`pot-provider` 需要自行构建后发布，再把 URL / `size` / `sha256` 填进清单。步骤是在上游仓库对应 tag 的 `server/` 目录里：

```bash
npm ci && npx tsc && npm prune --omit=dev
```

然后把 `build/`、裁剪后的 `node_modules/`，以及同一份检出里 `plugin/yt_dlp_plugins/` 的插件代码打进同一个 zip —— 两半必须来自同一个 tag，上游是配套版本。

两点必须注意：

- **bgutil-ytdlp-pot-provider 是 GPL-3.0。** 再分发构建产物需要随包附上许可证与对应源码，脚本因此把 `LICENSE` 和 `src/`、`package.json`、`package-lock.json`、`tsconfig.json` 一并打进包里。
- **产物是平台绑定的。** `canvas` 是原生模块，Windows x64 上构建的包只能给 Windows x64，正好与清单声明的平台一致。

### Cookie 的处理

`tools.cookies` 对所有插件开放，声明了就能用。撑住这一点的不是「谁在申请」，而是这个接口的形状：

- **按插件隔离**：jar 存在各自的 `plugin-data/<id>/` 下，插件之间互不可见
- **只写不读**：页面能存能清能查状态，但永远拿不回内容
- **沙箱无网**：CSP `connect-src 'none'`，插件页发不出请求

所以窃取这条路是堵死的。残余风险是「插件用你主动粘贴进去的会话去下载」，而这受限于 `validateVideoURL` 的站点白名单和保存对话框选定的落盘位置。

对页面来说 cookie 是**只写**的。页面能保存新的、清除已有的、查询「配没配、几条」，但永远读不回来；把凭据递进沙箱 iframe 不会让编辑流程多出任何能力。宿主侧以 `0600` 存在 `plugin-data/<id>/cookies.txt`，只有 yt-dlp 读它。

页面可用的方法：`downloader.settings`、`downloader.saveCookies`、`downloader.clearCookies`、`downloader.cancel`。保存时会校验是不是 Netscape 格式，避免把「粘错了」变成几分钟后的下载失败；`downloader.settings` 还会回报 PO Token 还差哪些组件，以及**当前是否有下载在跑**。

### 下载的状态归宿主管

插件页是 iframe，用户切走页面就会被销毁，页面自己的状态一点都留不下。所以运行中的下载由 Go 侧持有，页面只是附着上去：

- `downloader.settings` 的 `downloadRunning` 让**重新挂载的页面**接管进度条并禁用下载按钮，而不是显示成空闲、诱导用户再点一次（那样只会撞上「已有任务在运行」）。
- 宿主推 `download.progress`（百分比，节流 400ms）、`download.log`（yt-dlp 逐行输出）、`download.finished`（收尾）。收尾事件是必要的：真正的结果由 `rpc.result` 回给发起的那个 frame，而它可能已经不存在了。
- 事件只能带来「此刻之后」的内容，所以宿主还**留存**本轮日志（最近 200 行），页面挂载时用 `downloader.log` 取回。没有这一步，切回来的日志会从半路开始。留存会跨越任务结束，下一次下载开始时才清空 —— 任务跑完后再切回来，仍然看得到它是怎么跑完的。
- `downloader.cancel` 直接结束子进程 —— yt-dlp 没有更温和的协议。取消按「用户主动取消」处理：返回空结果而非错误（与取消保存对话框同形），并清掉那个残缺的输出文件。

日志行超过 500 字符会被截断：aria2c 报错会把整条签名后的媒体 URL 打出来，一千多字符，会把日志里所有能看的东西挤出视野。

输出参数、FFmpeg 路径和 URL 由宿主追加。`--exec`、Cookie、配置文件和任意输出路径等参数一律拒绝。

带返回值的调用应传入请求 ID：

```js
window.nonoka.post("media.list", {}, "request-1");

window.addEventListener("message", (event) => {
  const message = event.data;
  if (message?.source !== "nonoka-host" || message.method !== "rpc.result") return;
  if (message.id === "request-1") console.log(message.result, message.error);
});
```

API v1 暂不执行插件原生进程，也不开放任意 FFmpeg 参数或媒体路径。后续 Runtime API 会沿用同一 manifest 和安装生命周期，以独立进程 JSON-RPC 接入；在权限、任务取消、临时目录和输出 artifact 契约稳定之前，不应让插件直接取得任意 Wails service。

## 引擎能力

FineSub 引擎跑在托管的 Python 环境里，桌面端自己那份解释器导不动它。所以这三样能力都由宿主代跑：插件描述要什么，宿主决定用哪个模型、跑在哪块卡上、产物落在哪里。

### `llm.complete`

一次调用，用用户在设置里配好的那套路由——API Key，或者本机已登录的 Codex / Antigravity CLI。

```js
const answer = await rpc("llm.complete", {
  role: "general_capable",
  maxTokens: 2048,
  messages: [
    { role: "system", content: "你是中文字幕润色助手。" },
    { role: "user", content: "1. 这句话有点生硬" },
  ],
});
// answer.content / answer.model / answer.backend / answer.fallbackUsed
```

几条边界：

- **`role` 只有两个**：`lightweight`（轻量文本）和 `general_capable`（通用能力）。引擎另有两个多模态角色，它们存在的意义是把一段音视频递给模型，而这条接口没有办法递媒体引用——开放它们只会把纯文本路由到更贵的模型上。
- **没有 `model` 字段**。用哪个模型是用户的配置；一个能指定模型的插件也就能绕开用户的选择。
- **插件永远拿不到密钥**，也拿不到 FineSub 自己的提示词模板（CC BY-SA，属引擎内部）。
- **宿主兜底限流**：单插件同时只允许一个调用在飞，10 分钟内最多 60 次，prompt 总长上限 200 KB，`maxTokens` 上限 32768。越限直接报错而不是排队——页面显示成「卡住」而用户额度在背后流失是更坏的结果。
- 供应商自己的话会原样带回来（额度用尽、CLI 没登录、限流），因为除此之外插件没有别的办法分辨这几种失败。

### `engine.startTask`：跑到某个阶段为止

FineSub 的流水线有六个阶段，每个阶段的产物存在就跳过，所以指定一个更早的阶段既是「只要这一步」，也是「便宜地续跑」：

| `target` | 跑完之后有 | 产物 |
| --- | --- | --- |
| `vocal` | 人声分离 | `vocal_audio` |
| `aligned` | VAD + 词级对齐 | 加上 `vad_json`、`vad_energy_npz`、`aligned_json` |
| `stable` | 稳定化 | 加上 `stable_json` |
| `raw-srt` | 原始字幕 | 加上 `raw_srt` |
| `translated-srt` | LLM 纠错与翻译 | 加上 `translated_srt` |
| `final-srt` | 成品字幕 | 加上 `annotated_csv`、`final_srt`、`metadata_json` |

插件能给的只有 `target`、`language`（默认 `ja`）、`skipSeparation`（默认否）和 `correction` 的几个旋钮（`media` / `retrieval` / `difficulty` / `fast` / `extraInfo` / `extraStyle`）。其余由宿主填：

- **来源**用 `media.list` 给的 `mediaId` 解析，插件依旧拿不到本地路径。
- **`device`、`gpu_tier`、`vocal_profile`** 是宿主的，与主界面同一套。
- **`knowledge` 恒为 `none`、`projection` 恒为 `none`**，这两条是锁死的：更新知识库会改用户的知识库，而投影会用这次运行的结果**替换用户正在编辑的字幕文档**。两样都不是「跑一个阶段」这个请求包含的意思，能申请就等于能在用户看不见的地方做。插件要把结果变成字幕文档，走 `document.save`——那条有 rev 乐观锁。
- **同一时间只允许一个引擎任务**。流水线占着 GPU 好几分钟，第二个任务不会失败，只会让两个都变慢，而用户正看着其中一个。

任务是异步的，页面自己轮询 `engine.status` 和 `engine.events`——与主界面的流水线视图同一套做法，宿主不再为 iframe 另开一条推送通道。任务 id 由宿主按插件记在 `plugin-data/<id>/engine-tasks.json`（页面是 iframe，用户切走就什么都不记得了），**一个插件只看得到自己起过的任务**。

### 中间产物

产物只能按任务取，而任务只有本插件起过的那些才认得，所以这三个方法同时要求 `engine.run` —— 单独声明 `engine.artifacts` 拿不到任何东西。

`engine.artifacts` 只回名字、字节数和「能不能直接读」，永远不回路径。文本产物（`.json` / `.srt` / `.csv` / `.md` / `.txt`）用 `engine.readArtifact` 拿内容，8 MB 封顶；其余（`vocal_audio` 的 `.ogg`、`vad_energy_npz` 的 `.npz`）只能用 `engine.saveArtifact` 经保存对话框落盘。分界不是「安全等级」，而是用途：几十 MB 的音频经 postMessage 递进沙箱页面，除了把内存吃光之外做不成什么事。

## 安装布局和生命周期

```text
NonokaXData/
├─ plugins/<id>/current.json
├─ plugins/<id>/versions/<version>/
├─ system-plugins/<id>/current.json
├─ system-plugins/<id>/versions/<version>/
├─ plugin-data/<id>/
└─ plugin-registry.json
```

- 安装先解压到 staging，完整校验后再原子发布。
- 安装新版本通过 `current.json` 切换活动版本。
- 停用只隐藏菜单入口，代码和数据仍保留。
- 卸载可以选择保留或删除 `plugin-data/<id>`；系统插件不能卸载。
- `plugin-registry.json` 只记录用户显式改过的开关。系统插件没有记录时算启用，用户插件没有记录时算停用。
- 插件变化通过 `plugins:changed` 事件同步到所有前端状态。

内置的系统插件：

- `desktop/internal/plugins/system/dev.nonoka.youtube-downloader`：下载公开的 YouTube/Twitch 视频并自动导入媒体库，带进度条，可选画质到 2160p、并发分片到 16；装了可选工具 aria2c 时自动走多连接加速。

可运行示例：

- `desktop/examples/plugins/hello-tool`：最小 UI、媒体列表与 FFmpeg 音频导出。
- `desktop/examples/plugins/video-downloader`：插件自行组织 yt-dlp 参数，下载公开的 YouTube/Twitch 视频并自动导入媒体库。
- `desktop/examples/plugins/subtitle-studio`：读取字幕文档、批量替换后带 rev 写回，导出 ASS/SRT 并压制视频；另外演示 LLM 润色、只跑一个流水线阶段，以及读取/落盘该次运行的中间产物。
