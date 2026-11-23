"""阶段 4：特效渲染。读阶段 3 的字幕稿（`*.script.ass`，能在 Aegisub 里预览、编辑），生成最终的叠加 ASS。

    python -m flowocr.typeset out/x/x-cn-main.script.ass                  # -> x-cn-main.ass
    python -m flowocr.typeset x.script.ass --preset dev                   # 开发看：UI 黄字、非主轨浅蓝、区域号
    python -m flowocr.typeset x.ass --restore                             # 阶段 4 的输出还原成字幕稿

方案：script-fx 计划；约定在 artifacts.md 的字幕稿一节；实现在 `core`，ASS 读写在 `assfile`，
扩展效果（`--fx`）是 `flowocr.extensions` 的 `fx` 种类，内置的放在 `flowocr.typeset.fx`。
"""
