"""从 build_tracks 切好的段直接写 SRT（一级产物旁的区域 / 主轨 / 名牌 SRT），以及清掉上一轮的旧产物。

2026-09-22 从 `flowocr.analyze.build_tracks` 原样搬出来（正规化阶段 C 第二刀，project-structure：
阶段 2 产分析结果，写字幕是阶段 3 的事）。**只投影，不判断**：段怎么切、哪些行是常驻 UI 都是调用方判好传进来的。
`export.py` 是从 `<tag>-tracks.json` 投影的那条路；这里是 build 时从内存里的 run 直接写，两条路产同一批文件。
段里的元素是 `build_tracks.Run`（鸭子类型：只用 `.text`）。
"""
from __future__ import annotations

from pathlib import Path

from flowocr.artifacts import srtio


DROPPED_UI: list[tuple[str, set[str], int, int]] = []
"""`(产物名, 判为 UI 的行, 删掉的行数, 整条被删的 cue 数)`——
**删了什么必须打出来**：上一版一行日志都不打，于是"105 个空的区域 SRT"
在磁盘上躺了一整夜没人看见（methodology-audit-2 报告）。"""


def write_srt_segments(path: Path, segs: list[tuple[int, int, list]],
                       ui: set[str], ui_ids: set[int] | None = None,
                       idx: dict[int, int] | None = None) -> int:
    """**投影**：把已经切好的段按 `ui`（这条轨判为常驻 UI 的行）剔一遍再写 SRT。

    返回**写了几条 cue**——调用方应当把它打印出来，好让文档里的数
    可以直接用 `grep -c -- '-->'` 数回来（boundary-refinement 报告）。
    """
    before_cues, before_lines = len(segs), sum(len(a) for _, _, a in segs)
    def drop(r) -> bool:
        return (r.text.strip() in ui
                or (ui_ids is not None and idx is not None and idx.get(id(r)) in ui_ids))

    if ui or ui_ids:
        segs = [(s, e, keep) for s, e, keep in
                ((s, e, [r for r in a if not drop(r)]) for s, e, a in segs) if keep]
    dl = before_lines - sum(len(a) for _, _, a in segs)
    if dl:
        DROPPED_UI.append((Path(path).name, ui, dl, before_cues - len(segs)))
    return srtio.write_srt_blocks(path, ((s, e, [r.text for r in active])
                                        for s, e, active in segs))


def clear_stale_srts(outdir: Path, tag: str) -> int:
    """把上一轮的 `{tag}-region*.srt`（含已去掉的回抠 CLI 留下的 `-refined.srt`、`flowocr-export --start full` 的 `-full.srt`）、
    `{tag}-main.srt`、`{tag}-nameplate.srt`（及它们的 `-full.srt`）、已去掉的回抠 CLI 另存过的 `{tag}-tracks-refined.json`、
    作废的 `{tag}-provenance.json` 和上一轮的 `{tag}-tracks.json` 删掉，返回删了几个。

    `{tag}-tracks.json` 也要删：建轨最后才写它（回抠结算、SRT 重出都成功之后），中途失败时留着上一轮的那份，
    驱动判缓存就会拿它配这一轮写了一半的 SRT（2026-09-26 复查）。`-full.srt` 是它的派生物，同理。

    `{tag}-main.srt` 也要删：它只在 `--main-band` 打开时才产，**关掉之后
    旧的那份会留在原地**，而 provenance 里的 main_srt 已经指回区域文件——
    磁盘上于是躺着一份没人认领、看着却很正经的主轨。这正是 2026-09-07
    那次"字典序捡到隔夜坏产物"的同一形状，只是换了个文件名。

    为什么必须删（2026-09-07 实测的代价）：文件名里带 label，**label 一变就是
    新文件名**，旧的留在原地。`out/yuka-f4/` 里于是同时躺着
    `f4-region01-static-overlay.srt`（09-06，17,748 条，`gap_frames` 修好之前的坏轨）
    和 `f4-region01-subtitle.srt`（当天，4,387 条）；而 `head2head.sh` 那时按
    `f4-region01-*.srt | head -1` 取主轨，字典序 `sta` < `sub`——
    **整部 input4 的基线是拿一份隔夜的坏产物量的，一行报错都没有。**

    只删自己这个 tag 的区域 SRT：同一个 outdir 里可能有别的 tag 的产物。
    """
    stale = sorted(outdir.glob(f"{tag}-region*.srt"))
    # `-tracks-refined.json`（回抠 CLI 另存的那份；CLI 09-26 已去掉，旧目录里还可能有）：它出自上一版的轨，重建之后就过时了，
    # 留着就是又一份看着正经的一级产物（区域的 `-refined.srt` 已被上面的 `region*` 通配带走）
    for name in (f"{tag}-main.srt", f"{tag}-nameplate.srt", f"{tag}-main-full.srt", f"{tag}-nameplate-full.srt",
                 f"{tag}-provenance.json", f"{tag}-tracks-refined.json", f"{tag}-tracks.json"):
        # `-nameplate.srt` 同 `-main.srt` 的理由（2026-09-20 复审第 3 条）：它只在挑中了名牌时才产，
        # **`--no-nameplate-mark` 重建、或者这一版一条都没挑中时，旧的那份会留在原地**。
        # 现在 `merge_nameplate` 从 JSON 取名牌轨、取不到就退出，所以不会被静默读到；
        # 但"旧文件留在原地、看着很正经"就是 09-07 那次的形状，一起删。
        # `-provenance.json` **已经并进 `-tracks.json`**（`output-format-plan（已归档）`，
        # 不做兼容）。旧的那份留在原地最危险：它看着很正经，而且指向的 main_srt
        # 可能是上一版的——正是"字典序捡到隔夜坏产物"的同一形状。
        p = outdir / name
        if p.exists():
            stale.append(p)
    for p in stale:
        p.unlink()
    return len(stale)
