"""阶段 2 的**匹配**入口：拿一份 `*-tracks.json`，跑一个匹配器（内置的或用户自己写的），写出匹配产物。

    python -m flowocr.analyze.match out/gs-hsr/hsr-tracks.json --matcher gametext \\
        --ctx ref=out/gametext/hsr-ref.json --out out/gametext/hsr-matched.json --opt lang=cn
    python -m flowocr.analyze.match out/x/x-tracks.json --matcher D:/mine/matcher.py --out tmp/x-matched.json

匹配器从哪来、入口长什么样：`flowocr.extensions`（`match(document, context, options) -> dict`，
`cluster_patch` 是可选的另一半，由 `build_tracks --matcher` 在聚类那一步用）。
`--ctx k=v` 进 `context`（`tracks_path` / `tag` / `argv` 自动带上），`--opt k=v` 进 `options`，值都是字符串、匹配器自己解释。
建轨时给同一个匹配器的 `--matcher-ctx game=`（记在轨的 `provenance.matcher.ctx`）在这里继承，`--ctx game=` 给了以它为准：
《魔裁》建轨写过 `game=mocai`，匹配就不用再写一次（版式文件按它找，owner 2026-11-10）。

产物拿去阶段 3：`python -m flowocr.output.render <out.json> --preset matched_srt|default|dev`；
匹配器声明了推荐预设（`PRESET`，`extensions.recommendation`）时它记在产物的 `provenance.recommend`，不给 `--preset` 就照它渲
（内置匹配器 `gametext` / `naninovel` 写的是 `flowocr-matched/8`；用户匹配器写自己的格式，配自己的预设）。
⚠ 换预设不重跑匹配、换匹配器不重跑提取——这就是三个阶段各写一份产物的意义。

`scriptmatch` 的 CLI 仍在（它带着评测口径的报表和一堆 ASS 旋钮）；这里是**通用**那条，
内置和用户实现走同一条路。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from flowocr import extensions
from flowocr.provenance import portable
from flowocr.artifacts import tracksio


def kv(pairs: list[str], what: str) -> dict:
    out = {}
    for x in pairs:
        if "=" not in x:
            raise SystemExit(f"{what} 要 K=V：{x}")
        k, v = x.split("=", 1)
        out[k] = v
    return out


def context_of(doc: dict, src: Path, argv: list[str], ctx: dict[str, str], matcher: str) -> dict:
    """给匹配器的 `context`：建轨时给**同一个**匹配器的 `game`（轨的 `provenance.matcher`，`spec` 和这次的 `--matcher` 相同时），
    自动带的 `tracks_path` / `tag` / `argv`，再是这次的 `--ctx`（同名的以它为准）。
    只继承 `game`：别的键多是路径，记录时过了 `portable`（`~/…`、相对数据根），原样拿回来当参数会找不到文件；换了匹配器也不继承。
    `spec` 按记下的字面比：同一个匹配器换了写法（内置名和模块全名、`.py` 换了相对路径的起点）也不继承，这时用 `--ctx game=` 给。"""
    m = (doc.get("provenance") or {}).get("matcher") or {}
    game = (m.get("ctx") or {}).get("game") if m.get("spec") == portable(matcher) else None
    return {**({"game": game} if game else {}), "tracks_path": str(src), "tag": src.stem.removesuffix("-tracks"), "argv": argv, **ctx}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="跑一个匹配器（阶段 2）",
                                 formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    ap.add_argument("tracks", help="*-tracks.json（阶段 2 的聚类产物）")
    ap.add_argument("--matcher", required=True,
                    help="内置名（%s）/ 模块名 / .py 路径" % "、".join(extensions.builtin_names("matcher")))
    ap.add_argument("--out", required=True, help="匹配产物 JSON")
    ap.add_argument("--ctx", action="append", default=[], metavar="K=V",
                    help="给匹配器的上下文，可多次（内置 gametext 要 `ref=<gamescript 的剧本 JSON>`，"
                         "naninovel 要 `script=<Naninovel 剧本目录>`）")
    ap.add_argument("--opt", action="append", default=[], metavar="K=V", help="给匹配器的选项，可多次")
    a = ap.parse_args(argv)

    src = Path(a.tracks)
    doc = tracksio.load(src)
    context = context_of(doc, src, list(sys.argv[1:] if argv is None else argv), kv(a.ctx, "--ctx"), a.matcher)
    matcher = extensions.load(a.matcher, "matcher")
    out_doc = matcher.match(doc, context, kv(a.opt, "--opt"))
    if not isinstance(out_doc, dict):
        raise SystemExit(f"匹配器 `{a.matcher}` 的 match() 要返回 dict，给的是 {type(out_doc).__name__}")
    rec = extensions.recommendation(matcher, a.matcher)
    if rec:
        out_doc.setdefault("provenance", {})["recommend"] = rec
    p = Path(a.out)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(out_doc, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"-> {p}（schema {out_doc.get('schema', '<无>')}，{len(out_doc.get('cues') or ())} 条）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
