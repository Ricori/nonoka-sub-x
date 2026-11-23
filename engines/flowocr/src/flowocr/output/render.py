"""阶段 3 的薄入口：拿一份**阶段 2 的产物**，跑一个输出预设。

    python -m flowocr.output.render out/x/x-tracks.json --outdir tmp/x                       # 默认预设 default
    python -m flowocr.output.render out/x/x-tracks.json --preset default_srt --outdir tmp/x
    python -m flowocr.output.render out/x/x-matched.json --preset dev --outdir tmp/x --opt lang=cn
    python -m flowocr.output.render out/x/x-tracks.json --preset D:/mine/preset.py --outdir tmp/x --opt start=full
    python -m flowocr.output.render out/x/x-matched.json --opt lang=cn --burn inpaint                    # 3、4、5 连跑

吃两种产物（2026-09-22 起，Codex 审计 P2：原来写死 `tracksio.load`，匹配结果连预设的门都进不去）：

| schema | 读法 | 内置预设 |
| --- | --- | --- |
| `flowocr-tracks/1` | `tracksio.load` | 叠加三个（`default` / `default_all` / `dev`，叠 OCR 原文）、`script`（只写字幕稿）、`default_srt`（主轨一份 SRT） |
| `flowocr-matched/8` | `matchedio.load` | 叠加三个（叠剧本译文）、`script`、`default_srt`（叠上去的译文一份 SRT）、`matched_srt`（拍平成 SRT） |
| 别的（用户匹配器自己的格式） | 原样 `json.load`，**不校验** | 只有声明了这个 schema 的预设收（见 `ACCEPTS`） |

不给 `--preset` 时：匹配产物里有匹配器的推荐（`provenance.recommend`，`flowocr.extensions.recommendation`）就用它（命令行 `--opt` 优先），
否则是 **`default`**（生产默认，owner 2026-09-24）：只留主轨的叠加字幕。叠加预设写两份——
可编辑的字幕稿（`*.script.ass`，阶段 3）和最终 ASS（阶段 4 `flowocr.typeset`）；三个的差别见
`flowocr.output.presets._overlay` 和 script-fx 计划。

匹配产物上画哪些由**显示协议**定：匹配器在产物里声明组和档位（`display`），内置预设都按 `--opt show=…` / `hide=…` 算可见集合
（`matchedio.visible`、`script.matched_visible`）；`keep` 只留给 tracks 产物，`layers` 已去掉，对匹配产物传这两个都报错。

预设可以声明 `ACCEPTS = ("flowocr-tracks/1", …)`：喂进来的产物不在里头就**当场报错**，
而不是让它在预设内部以 `KeyError` 的形式炸（"失败不冒充成功"，计划）。不声明就是什么都收。
`--opt k=v` 原样交给预设的 `options`（值是字符串，预设自己解释）；预设要什么参数、默认多少，是它自己的事。

`--burn plate|inpaint|hq` 在叠加预设之后接着跑阶段 5（`flowocr.burn`），把预设返回的最终 ASS 烧进源视频，编码参数全用阶段 5 的默认；
只配内置的叠加预设。`inpaint` / `hq` 走 inpainting：没给 `plate` 时替用户带上 `plate=src`（底板只盖原文，抹的也只是原文）。
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from flowocr import extensions, paths, provenance
from flowocr.artifacts import matchedio, tracksio
from flowocr.output import script
from flowocr.output.presets import _overlay

BURN = {"plate": None, "inpaint": "default", "hq": "hq"}
"""`--burn` 的取值 -> 阶段 5 的 inpainting 档（None 是底板路线，原样烧）。"""


def load_doc(path: Path) -> dict:
    """按 `schema` 挑读法。认得的两种各自校验版本和必需字段，不认得的原样返回（用户匹配器的格式）。"""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    schema = raw.get("schema") if isinstance(raw, dict) else None
    if schema == tracksio.SCHEMA:
        tracksio.validate(raw)                  # 不再 `tracksio.load` 一遍：整片的 tracks.json 几十 MB，解析两次白花
    elif schema == matchedio.SCHEMA:
        matchedio.validate(raw, where=str(path))
    return raw


def check_accepts(preset, doc: dict, spec: str) -> None:
    """预设声明了 `ACCEPTS` 就核一下，喂错产物当场说清楚。"""
    accepts = getattr(preset, "ACCEPTS", None)
    if accepts is None:
        return
    got = doc.get("schema") if isinstance(doc, dict) else None
    if got not in tuple(accepts):
        raise SystemExit(f"预设 `{spec}` 吃的是 {list(accepts)}，给的是 {got!r}——"
                         f"阶段 2 的哪份产物喂给哪个预设，见 flowocr.output.render 文件头")


def recommended_path(p: str) -> Path:
    """推荐里记的 .py（`extensions.recommendation` 记成 `portable()`）-> 本机路径。**不看 cwd**：`portable()` 的相对写法
    就是相对数据根的，`~/…` 是用户目录；照 `provenance.local()` 那样先找 cwd 的话，cwd 下恰好有同名文件就会加载另一份（二次验收 2026-10-06）。"""
    q = Path(p).expanduser()
    return q if q.is_absolute() else paths.data_root() / q


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="跑一个输出预设")
    ap.add_argument("doc", metavar="产物", help="阶段 2 的产物：*-tracks.json 或 *-matched.json")
    ap.add_argument("--preset", default=None,
                    help="内置名（%s）/ 模块名 / .py 路径。不给时用匹配产物里匹配器推荐的（provenance.recommend，连同它的选项），"
                         "没有推荐就是 default；显式给了就照用，推荐的选项不带上" % "、".join(extensions.builtin_names("preset")))
    ap.add_argument("--outdir", default="", help="默认写在产物旁边")
    ap.add_argument("--opt", action="append", default=[], metavar="K=V", help="给预设的选项，可多次")
    ap.add_argument("--burn", choices=tuple(BURN),
                    help="叠加预设写完接着跑阶段 5，把最终 ASS 烧进源视频：plate 原样烧；inpaint / hq 先抹掉底板盖住的原文"
                         "（没给 plate 时带上 plate=src）。编码参数用阶段 5 的默认，要改就对最终 ASS 另跑 flowocr-burn")
    ap.add_argument("--force", action="store_true",
                    help="字幕稿（*.script.ass）已经在时也覆盖；不给时不覆盖、报错退出（它可能是改过的）")
    a = ap.parse_args(argv)
    opts = {}
    for kv in a.opt:
        if "=" not in kv:
            ap.error(f"--opt 要 K=V：{kv}")
        k, v = kv.split("=", 1)
        opts[k] = v
    src = Path(a.doc)
    doc = load_doc(src)
    spec = a.preset
    rec = (doc.get("provenance") or {}).get("recommend")
    if spec is not None and rec and rec["preset"] != provenance.portable(spec):     # 推荐里记的是 portable() 写法
        # 显式给的照用，只提示一句（owner 2026-10-06）
        print(f"（匹配器 {rec.get('by')} 推荐的是预设 {rec['preset']}" + (f"，选项 {rec['opts']}" if rec.get("opts") else "") + "）")
    if spec is None:
        if rec:
            spec, opts = rec["preset"], {**rec.get("opts", {}), **opts}      # 命令行的 --opt 优先
            if spec.endswith(".py"):
                spec = str(recommended_path(spec))
            print(f"按匹配器 {rec.get('by')} 的推荐用预设 {spec}" + (f"（{rec['opts']}）" if rec.get("opts") else ""))
        else:
            spec = "default"
    if a.burn:
        if spec not in _overlay.PRESETS:
            raise SystemExit(f"--burn 只配内置的叠加预设（{'、'.join(_overlay.PRESETS)}）：{spec}")
        if BURN[a.burn]:
            opts.setdefault("plate", "src")
            if opts.get("plate_color") == "on":
                raise SystemExit("inpainting 读不出取过色的底板：--burn inpaint / hq 时去掉 --opt plate_color=on")
    preset = extensions.load(spec, "preset")
    check_accepts(preset, doc, spec)
    outdir = Path(a.outdir) if a.outdir else src.parent
    token = script.OVERWRITE.set(a.force)
    try:
        written = preset.render(doc, outdir, opts)
    except script.ScriptExists as ex:
        raise SystemExit(str(ex)) from None
    finally:
        script.OVERWRITE.reset(token)
    for p in written:
        print(p)
    if a.burn:
        from flowocr.burn import __main__ as burn, pipeline       # 只有连跑才要阶段 5 的依赖
        try:
            pipeline.video_of(written[0])
        except ValueError as ex:                        # 这里没有 --video：告诉用户对写好的最终 ASS 另跑
            raise SystemExit(f"{ex}：最终 ASS 已经写好，另跑 flowocr-burn {written[0]} --video <源视频>")
        return burn.main([str(written[0]), *(["--inpaint", BURN[a.burn]] if BURN[a.burn] else [])])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
