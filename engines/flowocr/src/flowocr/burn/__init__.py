"""阶段 5：把阶段 4 的叠加 ASS 烧进源视频。两条路线：

* 底板路线（默认）：叠加 ASS 原样烧进去（底板盖住原文）。
* inpainting（`--inpaint`）：先用 LaMa 把阶段 4 打了标的底板（Effect `fo-fx|plate`）盖住的区域修补掉，再烧其余的行。

    python -m flowocr.burn out/x/x-default_all.ass                     # -> x-default_all.mp4
    python -m flowocr.burn x.ass --start 60 --end 90                   # 只烧一段先看效果

输出按源分辨率、源帧率，带源音轨；编码器和质量值自动定（`encode`），都可以覆盖。流水线和命令在 `pipeline`。
"""
