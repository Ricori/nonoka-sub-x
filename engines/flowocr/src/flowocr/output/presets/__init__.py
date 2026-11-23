"""内置输出预设。一个模块一个预设，入口是 `render(document, output_dir, options) -> list[Path]`（`flowocr.extensions`）。

现在有：`default_srt`（和 `default` 同一批条目的一份 SRT）、`matched_srt`（匹配产物拍平成 SRT）、`script`（只写字幕稿，阶段 3）、
叠加三个 `default` / `default_all` / `dev`（字幕稿 + 阶段 4 的最终 ASS，`_overlay`）。
用户自己写的预设不必放进来——`extensions.load("<路径或模块名>", "preset")` 同样能加载；可复制改的例子在仓库的 `examples/`。
"""
