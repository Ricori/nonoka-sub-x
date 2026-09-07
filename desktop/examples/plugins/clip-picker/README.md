# 选片小助手

读取一场直播的字幕，均分成批次交给模型分析，最后汇成一张给剪辑师用的表：每行一个时间段，带分类、详情和高能切片点，复制进 Excel 就能开工。

和其他示例不同，这个插件**用 TypeScript 写、有构建步骤**。原因见「为什么必须构建」。

> **大模型调用目前是 dummy。** `callLLM` 等 3 秒返回占位内容，用来把整条流水线跑通。接上之前表里的内容不可信 —— 界面上有提示。宿主的 `llm.complete` 能力已经可用，接法和限额见 [docs/llm-engine.md](docs/llm-engine.md)。

## 流水线

点一次「开始」，跑完全部五步：

| 步骤 | 做什么 | 代码 |
| --- | --- | --- |
| 1 | `document.read` 读字幕，丢掉时间不合法的，按 `t0` 排序 | `segment.ts` |
| 2 | 平均切成若干批，每批前后各借 10 行重叠 | `segment.ts` |
| 3 | 逐批调模型分析，system 用 `assets/analysis.md` | `analysis.ts` |
| 4 | 合并各批结果再调一次，system 用 `assets/to_excel.md`，要一个 JSON | `to_excel.ts` |
| 5 | 解析 JSON → 六列表格 → 复制成 TSV | `table.ts` |

## 运行要求

- 媒体库里至少有一个**已经跑出字幕文档**的条目。列表只有这类条目可选，其余置灰并标「· 无字幕」。
- 字幕文档由本地 Provider（sidecar）提供，所以需要 Nonoka X 的 Python 运行时已就绪。
- **不需要本地视频文件。** 云端转写后取回的占位条目「只有字幕、没有视频」，这个插件照样能用 —— 它只读字幕。

## 申请的权限

| 权限 | 用途 |
| --- | --- |
| `media.list` | 列出媒体，拿到 mediaId 和字幕文档状态 |
| `document.read` | 读取字幕文档的 `subtitles` |

只读，不写回任何东西。接入真实大模型时还要加一条对应权限。

## 目录结构

```
clip-picker/
├─ nonoka-plugin.json   manifest
├─ tsconfig.json        files 决定拼接顺序，main.ts 永远排最后
├─ build.mjs            node build.mjs [--watch]
├─ assets/              提示词，打包时内联进页面
│  ├─ analysis.md       阶段一：分批分析
│  └─ to_excel.md       阶段二：汇总成 JSON
├─ src/                 ← 手写的源码，只改这里
│  ├─ index.html        页面骨架 + <!--INLINE:...--> 标记
│  ├─ style.css
│  ├─ types.ts          宿主契约的类型（对着 Go 结构体抄）
│  ├─ host.ts           RPC、主题、状态栏、提醒条、剪贴板
│  ├─ log.ts            进度日志
│  ├─ assets.ts         取出内联进页面的 prompt
│  ├─ llm.ts            callLLM —— 换真实模型只改这一个函数体
│  ├─ segment.ts        纯函数：取句、均分切块
│  ├─ media.ts          选字幕档
│  ├─ analysis.ts       阶段一 + 把各批结果拼成全场记录
│  ├─ to_excel.ts       阶段二
│  ├─ table.ts          解析 JSON、渲染表、拼 TSV
│  └─ main.ts           接线与启动
├─ build/plugin.js      ← 产物：tsc 拼出来的单个 JS
└─ ui/index.html        ← 产物：内联后的自包含页面，manifest 的 page 指向它
```

`build/` 和 `ui/` 都是产物，已在 `.gitignore` 里。

## 开发

```powershell
node build.mjs --watch
```

开着它，改 `src/` 下任何文件都会自动重编到 `ui/index.html`。

**改完不用重新打包安装** —— 在应用里切走再切回本页面即可。Go 侧 `PageHTML` 每次调用都现读文件，前端 `PluginPageHost` 每次挂载都重新取，两处都没有缓存。

配合一条目录联接，可以直接编辑仓库里的源码：

```powershell
$installed = "$env:APPDATA\Nonoka X\plugins\dev.nonoka.clip-picker\versions\0.1.0\ui"
Remove-Item $installed -Recurse -Force
New-Item -ItemType Junction -Path $installed -Target "<仓库>\clip-picker\ui"
```

不需要管理员权限。安装时的符号链接检查只发生在解包那一步，读页面时不查。**改了 manifest 的 `version` 重装后要重做联接**，因为会生成新的 `versions/<新版本>/` 目录。

### 加一个新文件

1. 在 `src/` 下建 `foo.ts`
2. 在 `tsconfig.json` 的 `files` 里加一行，**放在 `main.ts` 之前**

不需要 `import` —— 所有文件编译后拼进同一个作用域，直接互相调用。`main.ts` 必须排最后，因为只有它有顶层执行代码。

### 接入真实大模型

改 `llm.ts` 一个函数体，上层的批次循环、进度、日志、错误处理一行都不用动：

```ts
async function callLLM(system: string, user: string): Promise<string> {
  const answer = await rpc<LLMAnswer>("llm.complete", {
    role: "general_capable",
    messages: [
      { role: "system", content: system },
      { role: "user", content: user },
    ],
  }, 180_000);
  return answer.content;
}
```

再把 `LLM_IS_STUB` 改成 `false`（界面上的 dummy 提示会跟着消失），manifest 的 `permissions` 加 `llm.complete`，然后**重新打包安装** —— 权限变了必须重装，联接只同步页面文件、不同步 manifest。

**超时要放大** —— `rpc()` 默认 15 秒是为了让「方法名打错导致宿主静默不回」能报出来，模型调用几十秒起步。

接口全貌、`role` 怎么选、限额（尤其是 **200 KB prompt 上限**，阶段二会先撞墙）见 [docs/llm-engine.md](docs/llm-engine.md)。

## 为什么必须构建

插件页通过 `<iframe sandbox="allow-scripts">` 的 `srcdoc` 加载，**没有 origin、没有 base URL**，宿主还会注入这条 CSP：

```
default-src 'none'; img-src data: blob:; style-src 'unsafe-inline';
script-src 'unsafe-inline'; font-src data:; connect-src 'none'
```

后果：

- `<script src>`、`<link>`、ES module 的 `import` **全都用不了**。
- 运行时**读不到包里任何别的文件** —— `assets/*.md` 装到磁盘上也没人能读。
- 页面**发不出任何网络请求**，所以在页面里放 API key 输入框没有意义，调大模型只能经过宿主。
- **浏览器存储也用不了**：没有 `allow-same-origin`，页面处在不透明源下，`localStorage` / `sessionStorage` / IndexedDB 一律抛 `SecurityError`。

所以 `page` 必须是**单个自包含 HTML**。构建做两件事：`tsc` 把 `src/*.ts` 拼成一个传统 script，然后把它和 CSS、两份 md 替换进 `src/index.html` 的标记位，输出到 `ui/index.html`。

TypeScript 因此配成 `"module": "none"` + `outFile`，**不要改成 ESM**。

## 打包

```powershell
node build.mjs --pack
```

编译后直接产出 `../clip-picker.nonoka-plugin`，里面是 `nonoka-plugin.json` + `ui/` + `assets/`（`assets/` 纯存档，运行时读不到）。

`build.mjs` 自己写 ZIP，**不要改回 `Compress-Archive`** —— PowerShell 5.1 那个版本把路径分隔符写成反斜杠，Windows 上装得进去（反斜杠当分隔符），但 macOS 上会变成一个名叫 `ui\index.html` 的单文件，安装时报 `plugin tool page does not exist`。

打包后用宿主自己的代码验一遍：

```powershell
cd ..\..\..
go test ./internal/plugins/ -run TestShippedExamplePackagesInstall
```

它会 glob `examples/plugins/*.nonoka-plugin`，走一遍真实的 ZIP 解包、路径安全检查、manifest 校验、权限检查和带 CSP 注入的 `PageHTML` —— 装不上会在这里就炸，不用等手动安装。

## 设计上的几个决定

### 平均切分，不是切满上限

先算出「至少要几批才能让每批都不超过 800 行」，再把行数平均分下去，各批最多差 1 行。1700 行切成 `567 / 567 / 566`，而不是 `800 / 800 / 100` —— 后者那个 100 行的尾巴，分析粒度跟前面两批完全不是一回事，合并时很突兀。

### 每批两个范围：core 和 span

- **core** —— 这一批真正负责的区间。时间轴和「句数」按它算，各批首尾相接，**不会重复计时**。
- **span** —— core 前后各借 10 行。发给模型的正文取这个。

重叠是为了让模型看懂批次边界上的上下文。但 user prompt 里**必须把两者分开讲**：

```
你只负责 01:06:09 – 02:12:17 这一段，时间轴条目不要超出这个范围。

【上文，共 10 行，仅供理解上下文，不要为它们生成条目】
【本批内容，共 567 行】
【下文，共 10 行，仅供理解上下文，不要为它们生成条目】
```

不说清楚的话，模型会把重叠行也写进时间轴，相邻两批就给出重叠的时间段 —— 而 `analysis.md` 明确要求时间轴头尾相接。

字幕每行带 `[hh:mm:ss]` 时间戳，模型要靠它输出时间轴。

### JSON 要能从垃圾里挖出来

就算 prompt 写死「只输出 JSON」，模型也照样裹 ` ```json ` 围栏或前后加句客套话。`extractJSON` 取第一个 `{` 到最后一个 `}`，比要求用户手动清理可靠。

缺字段补空串而不是整份失败 —— 模型偶尔漏一个 `editor`，不该让整场直播的结果作废。但真的坏输入（纯文本、没有 `rows`、`rows` 是空的）会明确报错，不会静默出一张空表。

### 结果活不过页面切换

**这是插件页的根本约束，不是 bug。** 页面是 iframe，切走就被销毁，而沙箱不透明源下浏览器存储也用不了，API v1 也没有任何通用存储能力。所以：

- 处理期间会显示红色提醒，让你别切页面
- 跑完后提醒变成「先复制为表格存走」

内置的 video-downloader 之所以「切页面不中断」，是因为它把整个下载**一次性委托给了 Go**（`context.Background()`，和页面无关），页面只是观察者；而且它的产物是磁盘上的文件和媒体库记录，本来就在页面外。我们这条流水线的循环跑在页面的 JS 里，产物也只在页面的变量里 —— 要达到同样效果，得给宿主加一条存储能力。

## 几个容易踩的地方

**`<head>` 必须是裸标签。** 宿主按字面量 `<head>` 定位注入点，写成 `<head lang="...">` 就匹配不上，CSP 和 `window.nonoka` 桥都进不去 —— 页面会静默失去和宿主通信的能力。`build.mjs` 会检查这一条。

**主题要自己切。** 插件页读不到宿主的 CSS 变量。收到 `host.info` 时除了换配色，还要一起改 `color-scheme`，否则浅色主题下 `<select>` 的下拉面板会是一块突兀的深色。

**空列表要分情况提示。** `media.list` 读的是**本地**媒体库。云端跑完的字幕在「取回本地」之前不在这个列表里，所以最常见的原因不是「还没转写」，而是云端结果还没落到本机 —— 提示里直接告诉用户去主页点「编辑字幕」。

**剪贴板会被拒。** 不透明源下 `navigator.clipboard` 经常直接失败。`copyText` 三级降级：Clipboard API → `execCommand` → 把文本放进文本框并全选让用户按 Ctrl+C。**走到第三级不算失败。**

**别在内联脚本里写出字面量的 `</script`。** HTML 解析器会就地截断整个 script 元素。`build.mjs` 内联时会把它拆成 `<\/script`，`assets.ts` 读回来时再还原。
