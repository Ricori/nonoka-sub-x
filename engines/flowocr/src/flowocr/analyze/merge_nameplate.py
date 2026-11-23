"""把**名牌区域**并进主轨，产出一条给匹配器用的 SRT。

来历（methodology-audit-2 报告）：input1 上四组对照里，只有"并名牌"这一组
能同时拉动两个指标——命中 5,814 → **5,880（+66）**、疑似真漏 133 → **94**，
而且**纯标点行的漏条率 97.6% → 52.4%**。owner 说的"上下文 momentum"就是这个：
名牌把"去掉标点一个字不剩"的 cue 撑成一条有边界、有判别力的条目。

那次挑名牌用的是**角色名表**（探针能用、修法不能用）。这个工具消费的是
`nameplate.py` 的几何判据，**一个字都不看**，所以换素材也能跑。

判据的准确性（`probe_nameplate_geom.py` 拿名表离线量的，五部整片，
产物用 `--slot-span-ratio 2.0` 建）：精确率 55–72%、召回 74–90%。
**精确率不到 100% 是这条路的成本**：挑中的 run 里有三成不是名字，
它们会作为多余条目进匹配器——代价只能靠端到端的命中/漏条量，不能靠"看着像"。

用法：
    python -m flowocr.analyze.merge_nameplate out/yuka-f1-sr20/f1-tracks.json \
        --out tmp/match/ours1-merged.srt
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from flowocr.analyze import build_tracks as bt  # noqa: E402   （只借 git_head，别再抄一份）
from flowocr.artifacts import evalkit  # noqa: E402
from flowocr.analyze import nameplate  # noqa: E402
from flowocr.artifacts import tracksio  # noqa: E402
from flowocr.artifacts import srtio  # noqa: E402


def region_srt(outdir: Path, doc: dict, r: dict) -> Path:
    """区域的 SRT 文件名**从产物里读**，不用 tag+index+label 拼。

    拼出来的名字和磁盘上的名字是两回事：label 一变就是新文件名，
    2026-09-07 那次整部 input4 量错就是拼/猜文件名的同一形状。
    """
    for tr in doc["tracks"]:
        if tr["kind"] == "region" and tr["region"] == r["i"]:
            return outdir / tr["srt"]
    raise SystemExit(f"tracks.json 里没有 region{r['i']:02d} 这条轨")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("tracks", help="build_tracks 产的 *-tracks.json")
    ap.add_argument("--out", required=True, help="并好的 SRT 写到哪")
    ap.add_argument("--no-band-repair", dest="band_repair", action="store_false",
                    help="**默认开**：把和主轨同一条带的碎片也并回来（cy 差 ≤--band-dy、"
                         "字高比在 --band-h 之间）。收紧 --slot-span-ratio 会把主字幕带"
                         "切下来一块——f5 的 region07 里有 328 种内容字 ≥6 的真台词，"
                         "命中因此掉了 261；并回来是 2,534 → 2,835。"
                         "五部里只有 f2/f5 真有碎片可并，另外三部开不开都一样，"
                         "**所以默认开是安全的**。这个开关是留给对照实验的")
    ap.add_argument("--paste", action="store_true",
                    help="**把名牌贴进时间重叠最大的那条正文 cue 的首行**，"
                         # `%%`：argparse 对 help 串做 % 格式化，单个 `%` 会让
                         # `--help` 抛 ValueError（这一处一直是坏的，2026-09-09 修）
                         "而不是让它单独成条（`nameplate.attach`，贴法 99.8%% 准）。"
                         "存在的理由（methodology-audit-3 报告）："
                         "单独成条时，只有名字的 cue 在匹配器眼里就是一条短台词，"
                         "会去抢剧本里那些含名字的行——五部实测**超额认领**"
                         "（输出里比整部剧本多出来的条目——**报警，不是已确认的错**）"
                         "从 81/138/140/68/39 "
                         "涨到 546/540/360/257/126。贴进正文则不产生新条目。"
                         "同带碎片不受影响（那是真台词，照旧独立成条）")
    ap.add_argument("--band-dy", type=float, default=0.02)
    ap.add_argument("--band-h", type=float, nargs=2, default=[0.75, 1.35])
    nameplate.add_args(ap)
    ap.add_argument("--np-source", default="track", choices=("track", "geom"),
                    help="名牌从哪来：`track` = build_tracks 打标投影的那条名牌轨"
                         "（默认，判据在 flowocr.analyze.uigate）；`geom` = nameplate.py 的七条阈值挑区域"
                         "（五部里四部挑中 0 个，留作对照）")
    a = ap.parse_args()

    tp = Path(a.tracks)
    tag = tp.name.removesuffix("-tracks.json")
    d = tracksio.load(tp)
    d["regions"] = tracksio.regions_with_runs(d)
    regs = nameplate.region_stats(d)
    evalkit.require_nonzero(len(regs), "有 run 的区域")
    main_r = max(regs, key=lambda x: x["score"])
    # **名牌从哪来**（2026-09-20 起默认走第一条）：
    #   `track`：`build_tracks` 打好标、投影成的那条 `kind="nameplate"` 轨——
    #            判据是 `tools/uigate.pick_nameplates`（框位 + 相对位置 + 时长 ≥ 台词）。
    #   `geom` ：`nameplate.py` 的七条阈值挑**区域**——五部里**四部挑中 0 个**（ui-gate 计划），
    #            而且病根在输入：三部的名牌 run 被巨型区域吞了，区域侧根本没得挑。
    np_track = next((t for t in d["tracks"] if t.get("kind") == "nameplate"), None)
    if a.np_source == "track" and np_track is None:
        raise SystemExit("这份 tracks.json 里没有名牌轨——用新版 build_tracks 重建，"
                         "或者显式 `--np-source geom` 走旧的七条阈值")
    picked = [] if a.np_source == "track" else nameplate.pick(regs, a)[1]
    geo_picked = list(picked)   # 几何判据挑的那些；下面 band_repair 还会往里加

    main_srt = region_srt(tp.parent, d, main_r)
    if not main_srt.exists():
        raise SystemExit(f"主轨 SRT 不在：{main_srt}（tracks.json 和产物目录对不上？）")
    cues = srtio.read_srt(main_srt, sort=True)
    print(f"主轨 region{main_r['i']:02d}（{main_r['label']}）{len(cues)} 条")

    if not picked and a.np_source == "geom":
        print("**没挑中任何名牌区域**——七条阈值在这部素材上不成立，原样输出主轨。"
              "⚠ 五部整片里**四部**是这个结果（ui-gate 计划）："
              "病根多半在输入——名牌的 run 被巨型区域吞了。默认的 `--np-source track` 不吃这一套。")
    if a.band_repair:
        band = [r for r in regs
                if r["i"] != main_r["i"] and r not in picked and r["n"] >= a.min_runs
                and abs(r["cy"] - main_r["cy"]) <= a.band_dy
                and a.band_h[0] <= r["h"] / main_r["h"] <= a.band_h[1]]
        if band:
            print(f"  同带碎片 {len(band)} 个（收紧闸门切下来的那些）")
        picked = picked + band

    extra, plates = [], []
    if a.np_source == "track":
        np_srt = tp.parent / np_track["srt"]
        if not np_srt.exists():
            raise SystemExit(f"名牌轨的 SRT 不在：{np_srt}")
        plates = srtio.read_srt(np_srt, sort=True)
        print(f"  名牌轨 {np_track['srt']}：{len(plates)} 条"
              + ("" if a.paste else "  ⚠ 没给 --paste，它们会独立成条——"
                                    "**别这么喂给匹配器**（methodology-audit-3：会去抢含名字的剧本行）"))
        if not a.paste:
            extra, plates = plates, []
    for r in picked:
        p = region_srt(tp.parent, d, r)
        if not p.exists():
            print(f"  ⚠ region{r['i']:02d} 的 SRT 不在（{p.name}），跳过")
            continue
        cs = srtio.read_srt(p, sort=True)
        # --paste 下，**几何判据挑中的名牌**贴进正文；同带碎片是真台词，照旧独立成条
        (plates if (a.paste and r in geo_picked) else extra).extend(cs)
        print(f"  + region{r['i']:02d}（{r['label']}）{len(cs)} 条"
              f"  cy={r['cy']:.3f} 字高 {r['h']:.0f} 时长中位 {r['dur']:.1f}s"
              + ("  [贴进正文]" if a.paste and r in geo_picked else ""))

    allc = sorted(cues + extra, key=lambda c: (c.start, c.end))
    if plates:
        plates.sort(key=lambda c: c.start)
        blocks, tagged = nameplate.attach(allc, plates)
        print(f"  名牌 {len(plates)} 条按**时间重叠最大**贴进正文："
              f"{evalkit.denom(tagged, len(blocks))} 的 cue 贴上了名字"
              f"（贴法 99.8% 准，speaker-name-line 报告）")
    else:
        blocks = [(int(round(c.start * 1e6)), int(round(c.end * 1e6)), list(c.lines))
                  for c in allc]
    n = srtio.write_srt_blocks(a.out, blocks)
    # **写完自己数一遍**：这个项目在"条数口径"上栽过一次 30 小时的错
    got = srtio.count_cues(a.out)
    if got != n:
        raise SystemExit(f"写了 {n} 条但文件里数出 {got} 条——不要拿这份数据继续算")
    print(f"-> {a.out}：{len(cues)} + {len(extra)} = **{n} 条**"
          + (f"（另有 {len(plates)} 条名牌贴进了正文，不单独成条）" if plates else "")
          + f"（`grep -c -- '-->' {a.out}` 数得到同一个数）")

    # **这一级也要能追溯到是哪一版代码、哪些参数产的**（methodology-audit-3 报告）：
    # 它和 build_tracks 一样是"产出匹配器输入"的一级代码，可之前既不写 provenance、
    # 也不落日志——挑中了哪几个区域、有没有打出"没挑中任何名牌区域"，只在终端上闪过。
    prov = tp.parent / f"{tag}-nameplate.json"
    prov.write_text(json.dumps({
        "tool": "merge_nameplate.py", "git_head": bt.git_head(),
        "argv": sys.argv[1:], "tracks": str(tp), "out": str(a.out),
        "main_region": main_r["i"], "main_cues": len(cues),
        "picked": [{"i": r["i"], "label": r["label"], "cy": round(r["cy"], 3),
                    "h": r["h"], "dur": r["dur"], "n_runs": r["n"],
                    "band_repair": r not in geo_picked} for r in picked],
        "extra_cues": len(extra), "cues": n,
        "args": {k: v for k, v in sorted(vars(a).items())},
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"   版本与选区 -> {prov.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
