"""OCR 产物"跑完没有"的判据。**单独放一个文件是为了让守卫够得着。**

来历：C8 那次的教训（audit4-response 报告）——判据写在
paddleocr 实验目录 里的脚本顶层，而那个文件 import cv2 和 paddleocr，
**共享层的自检够不着，够不着就等于测不了**。这条判据同样属于"错了不会报错"
的一类，所以照 `dev_tools/predet_stop.py` 的先例抽出来。
"""
from __future__ import annotations

import os

COVER_TOL = 0.98
"""容器元数据允许虚报多少。`CAP_PROP_FRAME_COUNT` 在游戏切片上多报 1%
（声称 36,361、实际 36,001）——这不是没跑完，是元数据不准。"""


def is_complete(got: int, want: int, tol: float = COVER_TOL) -> bool:
    """推进到的帧数够不够。

    **不要再把"解码器提前返回"当成没跑完**（2026-09-08）：读到文件真正的结尾
    也会提前返回，于是九段切片每次跑完都自称未完成、退出码 3，
    驱动脚本会永远重跑它们。当时是把产物里的 `complete` 手工回填了——
    **改数据没改代码**。

    中途真断了这道门自己会拦：40% 处解码失败时 `got/want` 就是 0.4。
    """
    return got >= max(1, want) * tol


def aux_shortfall(edge_meta: dict | None, last_idx: int | None, grid) -> str | None:
    """辅助流（两遍合一遍的回抠证据）有没有**交够**：返回问题（None = 没问题）。**纯函数，守卫直接测。**

    2026-09-24 审计（Claude / Codex 两份都指出）：辅助流出错原来只记进 `_meta.edge`、`complete` 照样为真，
    于是回抠证据半路断掉的 obs 会被一直复用。**`aux_stopped_early` 不能当判据**——有丢帧缺口的素材上单路也会置上
    （辅助流按数量收口、缺口处凑不够数，f3 / f4 每一臂都是 True）。判据是两条：
    ①pts 和帧对不上（`aux_misaligned`，停下来的那一刻起证据就没了）；②**辅助流没盖到采样流的最后一帧**
    （同一个 ffmpeg 出两路，最后一个辅助帧号不该早于 `grid.cover_floor(最后一个采样帧)`；`grid` 是 `AuxGrid`）。
    `aux_last_key` 不在（旧产物 / 不经 `AuxReader` 的 nvdec / cuvid 那几路）就只判第①条。"""
    e = edge_meta or {}
    if "aux_sent" not in e:
        return None
    if e.get("aux_misaligned"):
        return f"辅助流的 pts 和帧对不上、提前停了：{e['aux_misaligned']}"
    if last_idx is None or "aux_last_key" not in e:
        return None
    k = e["aux_last_key"]
    if k is None or k < grid.cover_floor(last_idx):
        return f"辅助流只到帧 {k}、采样流到了帧 {last_idx}（辅助流半路断了，之后的回抠证据是缺的）"
    return None


def obs_reusable(path, argv=None) -> tuple[bool, str]:
    """一份观测产物能不能直接复用。返回 `(能不能, 不能的话为什么)`。

    **判据只有这一份**（audit-5）：三个驱动脚本以前各自内联一段
    `python -c` 读 `_meta`，`run_full_ocr.sh` 那份甚至根本没读、只看
    `[ -s ]`——一次中断留下的半截 jsonl 也是非空的，于是"一次中断变成
    之后每次都已经做完"，而且不报错。

    四个条件，缺一不可：
    - `_meta.complete` 为真（跑完了）；
    - **`_meta.timebase == "pts"`**（时间轴是新口径的）；
    - **`_meta.decoder` 是当前默认**（`framesource.DEFAULT_DECODER`，两种解码器不混用）；
    - 给了 `argv`（这一趟**要跑的** `run_ocr2` 命令行）时，**生效配置**对得上
      （**跑的是同一段、同一条臂**——`--start` / `--end` 就在配置里）：用 `flowocr.extract.ocr_args` 那同一个解析器把 `argv` 解析出来，
      和产物的 `_meta.config` 逐键比，`--out` / `--progress-every` 除外。
      **缺键按 `ocr_args.ADDED_NOOP` 兜底**：后加的、默认空操作的旋钮不该让现成产物全部重建
      （2026-09-18；进那张表的前提是"默认不开时产物逐字节相同"，见那段说明）。
      比的是**解析后的值**，不是命令行字面——字面比不出"默认值翻了"：
      `--rec-bucket` 一旦进默认，对照臂旧产物的 argv 一字不变，会被当成新默认复用
      （2026-09-10 审查；旧版比的就是字面）。**没有 `config` 字段的产物一律重建**，
      "没记"和"记的是别的"在后果上没区别。三个驱动（`sampleset.sh` / `run_full_ocr.sh` /
      `run_gamestream_ocr.sh`）和 `ocr_ab.sh` 都传 `argv`。

    **窗口只有这一种判法**（审查 2026-09-10）：原来另有一条"`start`/`end` 差 0.51 s 以内算同一窗口"
    的容差判据，而给了 argv 时配置按值精确比 `start`/`end`，那条容差永远走不到——
    同一件事两套判据，删掉了。驱动传给这里的和传给 run_ocr2 的是同一串参数，不需要容差。

    timebase 那道门是 2026-09-10 补的。fps-and-rec-budget 报告 写的政策是
    「**旧产物一律作废重跑**，不做兼容、不回填」，但这条政策**没有落到代码里**：
    旧产物（`t_us = idx / src_fps`）的 `complete` 也是 true，于是
    `sampleset.sh` 对 `out/samples/` 下那 6 份一份都不重跑，
    代表性片段集会无限期停在旧时间轴上（f3 尾端差 5.1 s），**而且不报错**。
    """
    from flowocr.extract import ocr_args

    # 先解析，再看文件：驱动拼错了参数，不管产物在不在都该当场炸
    want_cfg = ocr_args.config_of(argv) if argv is not None else None
    ok, why = obs_finished(path)
    if not ok:
        return False, why
    meta = _read_meta(path)
    if meta.get("timebase") != "pts":
        return False, (f"时间轴不是 pts（产物记的是 {meta.get('timebase')!r}）"
                       f"——旧口径 `idx / src_fps` 的产物一律重跑，回填补不回丢帧缺口")
    # **解码器也要是当前默认那个**（owner 2026-09-10："不想有的用 cv2 有的用 ffmpeg"）。
    # 两种解码器的产物不逐字节相同，不拦的话，默认一改，磁盘上旧默认产的那批仍然
    # "已跑完"、被驱动脚本跳过——于是同一批里两种混着，而且不报错。和上面 timebase
    # 那道门是同一个形状：写在文档里 ≠ 代码上拦得住。
    from flowocr.extract import framesource

    if meta.get("decoder") != framesource.DEFAULT_DECODER:
        return False, (f"解码器是 {meta.get('decoder')!r}，当前默认是 "
                       f"{framesource.DEFAULT_DECODER!r}——两种解码器的产物不混用")
    if want_cfg is not None:
        got = meta.get("config")
        if got is None:
            return False, "产物没有 config 字段（记不清是拿哪组生效参数跑的）"
        if meta.get("region_crop_off") and got.get("region_crop") and want_cfg.get("region_crop") is False:
            # 请求了 --region-crop、实际没裁（外接矩形就是全屏、或旧产物的 --no-fast-det；原因在 `_meta.region_crop_off`）：
            # 产物和 --no-region-crop 跑的是同一条路，这次要的就是 --no-region-crop 时按实际比，不然回退臂会白重建一次。
            # 这次仍按默认要裁时照记录比（同样的参数再跑一遍，照旧复用）。反过来（产物没裁、这次要裁）裁不裁得了要跑起来才知道，照旧重建
            got = {**got, "region_crop": False}
        diff = ocr_args.config_differs(got, want_cfg)
        if diff:
            return False, "参数对不上：" + "，".join(
                f"{k} 产物 {got.get(k)!r} / 要的是 {want_cfg.get(k)!r}" for k in diff)
        # **采样网格和辅助流网格按生效值比**（2026-09-24 时间网格）：两者都要结合素材的帧率才定得下来，
        # 按产物自己的 `src_fps` 算要的网格。时间网格之前的产物只记整数步长：采样 `_meta.stride`；
        # 辅助流 `_meta.aux_step_effective`（加 `--aux-max-fps` 之后）或 `config.refine_step`（更早，没封顶）
        from flowocr.extract.framegrid import AuxGrid, TimeGrid

        src = meta.get("src_fps") or 0.0
        try:
            want_s = ocr_args.sample_grid(src, want_cfg["fps"], want_cfg.get("frame_select", "pts"))
            want_a = (ocr_args.aux_grid(src, want_cfg["fps"], want_cfg.get("aux_max_fps", ocr_args.AUX_MAX_FPS),
                                        want_cfg.get("frame_select", "pts")) if want_cfg.get("refine_fused") else None)
        except ValueError as exc:
            return False, str(exc)
        got_s = (TimeGrid(*meta["sample_grid"]) if meta.get("sample_grid")
                 else TimeGrid.every(meta["stride"]) if meta.get("stride") else None)
        if got_s != want_s:
            return False, (f"采样网格对不上：产物 {got_s.describe(src) if got_s else '没记'} / "
                           f"要的是 {want_s.describe(src)}（--fps）")
        if want_a is not None:
            got_a = (AuxGrid.from_list(meta["aux_grid"]) if meta.get("aux_grid")
                     else AuxGrid(TimeGrid.every(meta.get("aux_step_effective", got.get("refine_step", 1))), got_s))
            if got_a != want_a:
                return False, (f"辅助流的生效网格对不上：产物 {got_a.describe(src)} / 要的是 {want_a.describe(src)}（--aux-max-fps）")
    stale = seek_dropped_first(meta)
    if stale:
        return False, stale
    # **代码指纹对不上时至少要吭一声**（2026-09-19 复审第 3 条）。
    # `code_fp` **故意不进复用判据**——进了就等于"动一下 tools/ 就重跑所有整片"，代价和收益不成比例。
    # 但"旋钮没变、代码变了"正是 09-06 / 09-10 那一族的形状：文件名和配置都没动，驱动静默复用旧产物。
    # 这次就踩到了（2026-09-19 异步 rec 改成固定滞后结算那次，沿用旧臂名会直接 [skip]）。所以：**判据不变，但把它喊出来**；
    # 探索用的驱动可以 `FP_CHECK=1` 把它升级成"重建"。
    fp_now, fp_got = ocr_args.code_fp(), meta.get("code_fp")
    if fp_got and fp_got != fp_now:
        msg = (f"⚠ 代码指纹不同（产物 {fp_got} / 现在 {fp_now}）：旋钮一样、**代码变了**。"
               f"要重跑就换臂名，或者 FP_CHECK=1")
        if os.environ.get("FP_CHECK") == "1":
            return False, msg.replace("⚠ ", "").replace("要重跑就换臂名，或者 FP_CHECK=1",
                                                        "FP_CHECK=1 要求重建")
        return True, msg
    return True, ""


def seek_dropped_first(meta: dict) -> str:
    """修之前的精确 seek 丢了窗口首格的产物（`framesource.SEEK_REV`）：返回作废的原因，不是就返回空串。

    代码指纹故意不进复用判据（见 `obs_reusable` 末尾），所以一次改了"读哪一帧"的修复要自己认出受影响的旧产物，
    否则沿用原臂名的驱动会一直复用坏的那份（2026-09-26 审计：`--fps 2.2` 的 yuka-f5 窗口）。认的是那个形状本身——
    没有 `seek_rev`、窗口从片中起、记了缺格、首个采样点比起点晚了至少一个采样间隔；别的旧产物照旧复用。"""
    if (meta.get("seek_rev") or 1) >= _seek_rev() or not (meta.get("start_sec") or 0) > 0:
        return ""
    fps, first = meta.get("sample_fps"), meta.get("pts_first_sec")
    if not (meta.get("grid_skipped") or 0) > 0 or not fps or first is None:
        return ""
    late = first - meta["start_sec"]
    if late < 1 / fps:
        return ""
    return (f"精确 seek 修之前的产物丢了窗口首格（首个采样点晚起点 {late:.3f} s ≥ 一个采样间隔、缺格 {meta['grid_skipped']}）"
            f"——重跑（framesource.SEEK_REV）")


def _seek_rev() -> int:
    from flowocr.extract import framesource
    return framesource.SEEK_REV


def obs_finished(path) -> tuple[bool, str]:
    """只问"写完了没有"——给**读**观测的下游用（`gametext_eval.sh`），不是给决定重不重跑的驱动用。

    读的一侧不在乎时间轴口径和解码器（文本检索不看时间，老的 gamestream 观测也照读），
    但**半截产物**照样要拦：整场 OCR 跑着的时候 `out/gamestream/<tag>.jsonl` 已经非空，
    `[ -s ]` 会放它进去，量出来是"前二十分钟"的数、而且不报错（2026-09-11 起整场绝区零 /
    星铁和评测同时在跑）。"""
    try:
        meta = _read_meta(path)
    except Exception as exc:                       # 不在、空的、半行、没有 _meta
        return False, f"读不到 _meta（{type(exc).__name__}）"
    if not meta.get("complete"):
        return False, "_meta.complete 不为真（还在跑，或者中途断了）"
    return True, ""


def _read_meta(path) -> dict:
    import json

    with open(path, encoding="utf-8") as fh:
        return json.loads(fh.readline())["_meta"]


def _main(argv: list[str] | None = None) -> int:
    """`python -m flowocr.extract.ocr_complete <obs.jsonl> [--argv <run_ocr2 的参数…>]`。

    能复用则退出码 0、不打印任何东西；不能则打印一行原因、退出码 1。
    驱动脚本按退出码分支，**不要再自己解析 jsonl**。
    """
    import argparse

    ap = argparse.ArgumentParser(
        description="观测产物能不能复用（跑完了 + pts + 默认解码器 + 同一组生效参数，含窗口）")
    ap.add_argument("obs")
    ap.add_argument("--finished", action="store_true",
                    help="只问写完了没有（读观测的下游用，见 obs_finished）；不能和 --argv 同给")
    ap.add_argument("--argv", nargs=argparse.REMAINDER, default=None,
                    help="这一趟要跑的 run_ocr2 argv，**必须放在最后**；"
                         "给了就解析成生效配置，和产物 _meta.config 逐键比")
    a = ap.parse_args(argv)
    if a.finished and a.argv is not None:
        ap.error("--finished 只问写完没有，给 --argv 说明你要的是复用判据")
    ok, why = obs_finished(a.obs) if a.finished else obs_reusable(a.obs, a.argv)
    if why:
        print(why)                 # 能复用但有话说（代码指纹不同）时也打——驱动会把它跟在 [skip] 后面
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(_main())
