# 字幕工作台 Demo

这个插件演示 API v1 的字幕文档能力：读取一份字幕文档、批量替换文本后带 rev 写回、拿到与编辑器逐字相同的 ASS/SRT，再用宿主的 FFmpeg 把字幕压制进视频。

它还演示 FineSub 引擎的三样能力：把译文交给宿主已配置的模型润色一遍、只把流水线跑到某个阶段为止、取回那次运行留下的中间产物。

## 运行要求

- 媒体库里至少有一个已经跑出字幕文档的视频（上面那个下拉框只列 `documentAvailable` 的条目）。
- 字幕文档由本地 Provider（sidecar）提供，所以需要 Nonoka X 的 Python 运行时已就绪。
- 压制视频需要在“运行环境”里装好 FFmpeg。
- LLM 润色需要在设置里选好提供商与全局模型；阶段任务需要本地 GPU 流水线已就绪。

## 申请的权限

| 权限 | 用途 |
| --- | --- |
| `media.list` | 列出媒体，拿到 mediaId 和字幕文档状态 |
| `document.read` | 读取字幕文档、生成 ASS/SRT |
| `document.write` | 把替换结果写回字幕文档 |
| `subtitle.export` | 通过保存对话框导出 `.ass` / `.srt` |
| `media.export-video` | 压制字幕导出 MP4 |
| `llm.complete` | 用宿主已配置的模型润色译文 |
| `engine.run` | 只把流水线跑到选定阶段 |
| `engine.artifacts` | 读取或保存那次运行的中间产物 |

## 打包

在此目录执行：

```powershell
Compress-Archive -Path .\nonoka-plugin.json,.\ui -DestinationPath .\subtitle-studio.zip
Rename-Item .\subtitle-studio.zip subtitle-studio.nonoka-plugin
```

然后在 Nonoka X 左侧“工具 → 插件管理”选择生成的 `.nonoka-plugin` 文件。

## 阶段任务不会动你的东西

插件起的引擎任务由宿主强制加上 `knowledge: none` 和 `projection: none`：它跑完只留下 `artifacts.json` 里的产物，不更新知识库，也不会用这次的结果替换你正在编辑的字幕文档。同一时间只允许一个引擎任务在跑——流水线要占用 GPU 好几分钟，第二个任务只会让两个都变慢。

中间产物按类型分成两条路：文本类（`stable_json`、`raw_srt`、`annotated_csv`、`metadata_json` 等）用 `engine.readArtifact` 直接读内容，二进制类（`vocal_audio`、`vad_energy_npz`）只能用 `engine.saveArtifact` 经保存对话框落盘。插件始终拿不到本地路径。

## 写回的乐观锁

`document.save` 必须带上读文档时拿到的 `rev`。编辑器在这期间保存过同一份文档时，rev 已经前进，宿主会返回 `revision_conflict` 而不是覆盖——插件应当重新 `document.read` 再做一次替换。
