"""**原型四**：行位当节点，**并不并由学出来的成对亲和度决定**——`build_tracks --cluster learned`（实验性，**不是默认**）。

**结论先说**（cluster-ruler 计划）：公平的抽样框下（照 `median` 抽 240 对 + 照它自己抽 186 对）74.2%，
比默认的 `median`（70.2%）好约 4 个点；它自己的决定里约 36% 是错的、以误合并为主；在一条标注都没见过的鸣潮上没有优势（60.6% = `median`）。
不用学的 `--cluster lines` 按管线的分组量（2026-10-05，只量 dev）：训练过的游戏三框合计 learned 76.8%、`median` 73.8%、lines 69.5%（配对上分不出：lines 对 `median` 53 : 66、p ≈ 0.27）；同时在场的上下相邻两行（尺的 S5 层）lines 拆开 15 / 38、learned 12 / 38、`median` 0 / 38——一句多行台词被拆进几个区域就是这个。训练过的游戏的产物上有用：长窗的重复认领降一半到三分之二、覆盖不少。
所以留作开关和对照臂，不进默认。

来历：逐通道消融（`pair_model.py`）说文本形态和"同屏伙伴"两条通道各自带着几何之外的独立信息（单独用 AUC ≈0.8），
硬否决试过（`slot_veto.py`）样本外等于没做——这些信号是**软**的，该进亲和度，不该当闸门。

做法：
1. 节点 = `slot_lines.line_slots` 的行位（位置是硬的、成员干净）；
2. **从"相邻行并成的带"起步**（`slot_lines.band_of(column=False)`）：同屏、同锚点、差一个行距的行是同一块多行文本，
   这条硬规则几乎不误合并，而亲和度在它上面会犹豫（hsr 居中字幕的第 1 / 第 2 行被拆、三行台词够不着剧本行）；
3. 候选边 = 几何上说得过去的行位对（字高比 ≤ `H_RATIO`，且 x 锚接近或竖直距离不远）——只为限住计算量，宁宽勿窄；
4. 边的亲和度 = 两个行位各取 `REPS` 条代表 run、两两过成对模型、取均值；
5. **平均连接**的凝聚：按亲和度从高到低看边，两个簇之间**所有**行位对的平均亲和度（没算过的对按 0 计）≥ `THR` 才并。
   没算过的按 0 计 = 保守：A–B、B–C 都像，不足以把 A 和 C 拉到一起；
6. 没进行位的 run：和附近行位的代表比，够像就挂上去；挂不上的，管线里回落到现行缝合的分组（`groups_for_pipeline`）。

⚠ 模型（`src/flowocr/analyze/models/slot_pair.json`）只在《魔裁》/ 原神 / 星铁 / 绝区零的标注上训过。特征一改就要重训（`--save`），加载时按特征表核对。
⚠ **量它要两个抽样框合起来**：照别的臂抽的样本上它显得很好（seed 3：82%），照它自己抽的显得最差（seed 4：64%）。

用法：
    python -m flowocr.analyze.slot_learned --train seed1,seed2 --test seed3 --lofo            # 留一部片子：测哪部，训练里就去掉哪部
    python -m flowocr.analyze.slot_learned --train seed1,seed2,seed3 --test seed4 --only wuwa-s1,wuwa-s2   # 没见过的游戏
    python -m flowocr.analyze.slot_learned --train seed1,seed2,seed3 --save                   # 重训随代码走的模型
"""
from __future__ import annotations

import argparse
import glob
import json
from collections import Counter
from pathlib import Path

import numpy as np

from flowocr.analyze import pair_features as PF  # noqa: E402
from flowocr.analyze import slot_lines  # noqa: E402
# ⚠ 这个模块会被 `build_tracks --cluster learned` import：顶层只许依赖轻模块。
# `pair_model` / `slot_pairs` / `cluster_layers`（它们 import build_tracks、sklearn）只在 main() 里用，在那边 import。

USE = PF.CHANNELS
"""模型吃哪些通道。试过只留 geom / text / context（消融里另外三条弱、`time` 去掉反而好）：seed 3 留一部片子 83.8% -> 81.2%，
gi2 40 分钟窗的重复认领 6 -> 18，hsr 拆行的病也没治好——**不采用**，全通道。开关留着是为了下次别再试一遍。"""
COLS = [k for k, n in enumerate(PF.FEATURE_NAMES) if n.split(":")[0] in USE]
USED_NAMES = [PF.FEATURE_NAMES[k] for k in COLS]

H_RATIO, X_NEAR, Y_NEAR_H = 2.2, 0.05, 4.0
REPS = 3
THR = 0.5


def fit(X, y):
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler
    return make_pipeline(StandardScaler(), LogisticRegression(C=0.5, max_iter=2000)).fit(X, y)


MODEL_PATH = Path(__file__).resolve().parent / "models" / "slot_pair.json"
"""随代码走的模型（`data/` 不进版本库，标注在那边；系数就这么几十个数，存成 JSON 跟着代码）。"""


class LinearModel:
    """落盘的逻辑回归：标准化 + 线性 + sigmoid。**加载不依赖 sklearn**（跑产物的 venv 里没有它）。"""

    def __init__(self, d: dict) -> None:
        self.d = d
        self.names = d["names"]
        self.mean, self.scale = np.array(d["mean"]), np.array(d["scale"])
        self.coef, self.b = np.array(d["coef"]), d["intercept"]

    def predict_proba(self, X) -> np.ndarray:
        z = ((np.asarray(X, float) - self.mean) / self.scale) @ self.coef + self.b
        p = 1.0 / (1.0 + np.exp(-z))
        return np.stack([1 - p, p], axis=1)


def save_model(pipe, names: list[str], meta: dict, path: Path = MODEL_PATH) -> None:
    sc, lr = pipe[0], pipe[-1]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"schema": "flowocr-slotpair-lr/1", **meta, "names": names,
                                "mean": sc.mean_.tolist(), "scale": sc.scale_.tolist(),
                                "coef": lr.coef_[0].tolist(), "intercept": float(lr.intercept_[0])},
                               ensure_ascii=False, indent=1), encoding="utf-8")


def load_model(path: Path = MODEL_PATH) -> LinearModel:
    m = LinearModel(json.loads(path.read_text(encoding="utf-8")))
    if m.names != USED_NAMES:
        raise SystemExit(f"{path} 的特征表和 pair_features 对不上（重训：python -m flowocr.analyze.slot_learned --save）")
    return m


def reps_of(L, mem: list[int], k: int = REPS) -> list[int]:
    good = [i for i in mem if PF.good_run(L.runs[i])] or mem
    step = max(1, len(good) // k)
    return good[::step][:k]


def assign(L, model, thr: float = THR, verbose: bool = False, leftover: set | None = None) -> dict[int, int]:
    """`leftover`（可选，传一个空 set 进来）：填上**既没进行位、也没挂上去**的 run——管线拿它们回落到现行缝合的分组。"""
    lines, free, sig = slot_lines.line_slots(L)
    F = PF.PairFeatures(L)
    n = len(lines)
    reps = [reps_of(L, mem) for _, mem in lines]

    def near(A: dict, B: dict) -> bool:
        if max(A["h"], B["h"]) / max(1.0, min(A["h"], B["h"])) > H_RATIO:
            return False
        if min(abs(A[t] - B[t]) for t in ("xl", "xc", "xr")) <= X_NEAR:
            return True
        gap = max(A["y0"], B["y0"]) - min(A["y1"], B["y1"])
        return gap <= Y_NEAR_H * max(A["h"], B["h"])

    cand = [(a, b) for a in range(n) for b in range(a + 1, n) if near(sig[a], sig[b])]
    rows, owner = [], []
    for e, (a, b) in enumerate(cand):
        for i in reps[a]:
            for j in reps[b]:
                rows.append(F.pair(i, j)[0])
                owner.append(e)
    aff: dict[tuple[int, int], float] = {}
    if rows:
        p = model.predict_proba(np.array(rows, float)[:, COLS])[:, 1]
        tot, cnt = Counter(), Counter()
        for e, v in zip(owner, p):
            tot[e] += v
            cnt[e] += 1
        aff = {cand[e]: tot[e] / cnt[e] for e in tot}
    # **从"相邻行并成的带"起步**，不从单个行位起步：同屏、同锚点、差一个行距的行是同一块多行文本，这条硬规则
    # 在尺上几乎不误合并（`slot_lines`），而学出来的亲和度在它上面反而会犹豫——hsr 居中字幕的第 1 / 第 2 行
    # 亲和度在 0.5 上下（行长不同 -> 文本长度、上下文大小都不同），三行台词被拆成两个区域，`nonoise` 覆盖 195 -> 192。
    # 只用"相邻行"那一条，不用"同一栏"（后者交给亲和度判）。
    cluster = slot_lines.band_of(sig, column=False)
    members: dict[int, list[int]] = {}
    for k, c in enumerate(cluster):
        members.setdefault(c, []).append(k)

    def between(A: list[int], B: list[int]) -> float:
        return sum(aff.get((min(x, y), max(x, y)), 0.0) for x in A for y in B) / (len(A) * len(B))

    for (a, b), v in sorted(aff.items(), key=lambda kv: -kv[1]):
        if v < thr:
            break
        ca, cb = cluster[a], cluster[b]
        if ca == cb or between(members[ca], members[cb]) < thr:
            continue
        for x in members[cb]:
            cluster[x] = ca
        members[ca] += members.pop(cb)

    out: dict[int, int] = {}
    for k, (_, mem) in enumerate(lines):
        for i in mem:
            out[i] = cluster[k]
    # 没进行位的 run：挂到附近最像的行位上
    by_y = sorted(range(n), key=lambda k: sig[k]["y0"])
    nxt, hung = n, 0
    free_good = [i for i in sorted(free) if PF.good_run(L.runs[i])]
    rows, owner, targets = [], [], []
    for i in free_good:
        r = L.runs[i]
        ks = [k for k in by_y if abs((sig[k]["y0"] + sig[k]["y1"]) / 2 - r.cy) <= 2.5 * max(r.h, sig[k]["h"])
              and max(r.h, sig[k]["h"]) / max(1.0, min(r.h, sig[k]["h"])) <= H_RATIO][:12]
        for k in ks:
            for j in reps[k][:2]:
                rows.append(F.pair(i, j)[0])
                owner.append(len(targets))
            targets.append((i, k))
    best: dict[int, tuple[float, int]] = {}
    if rows:
        p = model.predict_proba(np.array(rows, float)[:, COLS])[:, 1]
        tot, cnt = Counter(), Counter()
        for e, v in zip(owner, p):
            tot[e] += v
            cnt[e] += 1
        for e, (i, k) in enumerate(targets):
            v = tot[e] / cnt[e]
            if v >= thr and v > best.get(i, (0.0, -1))[0]:
                best[i] = (v, k)
    for i in sorted(free):
        if i in best:
            out[i] = cluster[best[i][1]]
            hung += 1
        else:
            out[i] = nxt
            nxt += 1
            if leftover is not None:
                leftover.add(i)
    if verbose:
        print(f"  [{L.tag}] 行位 {n}、候选边 {len(cand)}、≥{thr} 的边 {sum(v >= thr for v in aff.values())} -> "
              f"带 {len(set(cluster))}；散 run {len(free)}（像样的 {len(free_good)}），挂上去 {hung}", flush=True)
    return out


def groups_for_pipeline(runs: list, W: int, H: int, tag: str = "",
                        fallback: dict[int, int] | None = None, model=None) -> list[list[int]]:
    """给 `build_tracks --cluster learned` 用：run 列表 -> 每个区域的 run 下标。`model` 缺省 = 随代码走的模型；
    聚类的尺传留一部片子训的模型进来（`slot_pairs.pipeline_assign`），分组的其余步骤和管线同一份代码。

    `fallback` = run 下标 -> 现行缝合给它的槽位号。**既没进行位、也没挂上去的散 run**（单字残片、只出现过一次的字）
    按它归堆，而不是各自成一个区域：第一版不归堆，hsr 40 分钟窗上区域数 216 -> 712、其中 624 个是单 cue 的 `noise`
    ——剧本口径下不少认领（`probe_noise_fp`），但逐区域产物里多出几百个文件，而且被打成 noise 的真文字会从 `nonoise` 喂入里消失。"""
    from types import SimpleNamespace
    leftover: set[int] = set()
    asg = assign(SimpleNamespace(runs=runs, W=W, H=H, tag=tag), model if model is not None else load_model(), verbose=True, leftover=leftover)
    by: dict = {}
    for i, k in asg.items():
        key = ("fb", fallback[i]) if fallback is not None and i in leftover and i in fallback else ("c", k)
        by.setdefault(key, []).append(i)
    if fallback is not None:
        print(f"  散 run 回落到缝合分组：{len(leftover)} 条 -> {sum(1 for k in by if k[0] == 'fb')} 个区域", flush=True)
    return [sorted(v) for v in by.values()]


def cluster_groups(kind: str, runs: list, W: int, H: int, fallback: dict[int, int], tag: str = "",
                   model=None) -> list[list[int]]:
    """`build_tracks --cluster learned|lines` 的区域分组：缝合之后**换掉**"哪些 run 是一个区域"的那一步。
    管线和聚类的尺（`slot_pairs.pipeline_assign`）都调这里，尺量的就是管线真正产出的分组。
    `fallback` = run 下标 -> 现行缝合给它的槽位号（散 run 回落用）；`model` 只对 learned 有意义。"""
    if kind == "learned":
        return groups_for_pipeline(runs, W, H, tag, fallback=fallback, model=model)
    if kind == "lines":
        return slot_lines.groups_for_pipeline(runs, W, H, fallback=fallback)
    raise ValueError(f"未知的 --cluster {kind!r}")


FP_FILES = {"learned": ("slot_learned.py", "slot_lines.py", "slot_modes.py", "pair_features.py", "models/slot_pair.json"),
            "lines": ("slot_lines.py", "slot_modes.py")}
"""`--cluster learned` / `lines` 各自依赖的代码（+ 模型）。lines 不碰模型：重训模型不该让 lines 的产物失效。
lines 也不列本文件：lines 经 `cluster_groups` 分派进来，但本文件绝大部分是 learned 的代码，列进来的话改 learned 就让 lines 的产物全失效；
分派那几行改了要靠 build_tracks 自己的 `code_fp`（本文件在它的 `CODE_FP_FILES` 里）兜住。"""


def fingerprint(kind: str = "learned") -> str:
    """这条聚类路径（`FP_FILES` 的键）的代码 + 模型的内容指纹（换行归一），写进产物的 provenance。"""
    import hashlib
    h = hashlib.sha256()
    here = Path(__file__).resolve().parent
    for f in FP_FILES[kind]:
        h.update("\n".join((here / f).read_text(encoding="utf-8").splitlines()).encode())
    return h.hexdigest()[:12]


def main() -> int:
    from flowocr.analyze import pair_model
    from flowocr import paths
    from flowocr.analyze import slot_pairs as SP
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--train", default="seed1,seed2")
    ap.add_argument("--test", default="seed3")
    ap.add_argument("--lofo", action="store_true", help="留一部片子：测哪部，训练里就去掉哪部的标注")
    ap.add_argument("--split", default="dev", help="只量这个 split 的真值文件（acceptance 集留给真要翻默认时只看一次）；空 = 全部")
    ap.add_argument("--only", default="", help="只量这些 tag（逗号分隔）——分开报『训练过的游戏』和『没见过的游戏』")
    ap.add_argument("--save", action="store_true", help="用 --train 指的全部标注训一个模型、存到 src/flowocr/analyze/models/ 后退出")
    ap.add_argument("--relabel", default="", help="复核标签文件（items: id / label）：只覆盖**测试集**的标签，训练集不动（cluster-ruler）")
    a = ap.parse_args()
    relabel = ({it["id"]: it["label"] for it in json.loads(Path(a.relabel).read_text(encoding="utf-8"))["items"]}
               if a.relabel else {})
    if a.save:
        X, y, meta, names = pair_model.load(a.train.split(","))
        pipe = fit(X[:, COLS], y)
        save_model(pipe, USED_NAMES, {"train": a.train, "n_pairs": int(len(y)), "same_share": float(y.mean()),
                                 "films": sorted({m[0] for m in meta})})
        chk = LinearModel(json.loads(MODEL_PATH.read_text(encoding="utf-8"))).predict_proba(X[:, COLS])[:, 1]
        print(f"存了 {MODEL_PATH}：{len(y)} 对、训练集准确率 {((chk > 0.5) == y).mean():.1%}；"
              f"和 sklearn 的预测最大差 {np.abs(chk - pipe.predict_proba(X[:, COLS])[:, 1]).max():.2e}")
        return 0
    Xtr, ytr, mtr, _ = pair_model.load(a.train.split(","))
    tags_tr = np.array([m[0] for m in mtr])
    Xtr = Xtr[:, COLS]
    model_all = fit(Xtr, ytr)
    tot: dict[str, Counter] = {}
    pairs = []
    n_lost = 0
    for p in sorted(glob.glob(str(paths.data_root() / "data/gt/slotpairs-*.json"))):
        doc = json.loads(Path(p).read_text(encoding="utf-8"))
        if (f"seed{doc['seed']}" not in a.test.split(",") or (a.only and doc["tag"] not in a.only.split(","))
                or (a.split and doc.get("split") != a.split)):
            continue
        tag = doc["tag"]
        for it in doc["items"]:
            it["label"] = relabel.get(it["id"], it["label"])
        model = fit(Xtr[tags_tr != tag], ytr[tags_tr != tag]) if a.lofo else model_all
        extra = {"learned": lambda L, m=model: SP.pipeline_assign(L, "learned", m),     # 管线的最终分组（散 run 回落到缝合）
                 "lines": lambda L: SP.pipeline_assign(L, "lines")}
        arms = {k: v for k, v in SP.ARMS.items() if k in ("pooled", "median")}
        for name, c in SP.score_doc(doc, arms, extra).items():
            tot.setdefault(name, Counter()).update(c)
        asg = SP._ASSIGN[tag]
        for it in doc["items"]:
            if it["label"] in ("same", "different"):
                v = SP.verdicts(asg, it, SP._RUNS.get(tag))     # 和 score_doc 一样先重新锚定
                if v is None:
                    n_lost += 1
                else:
                    pairs.append(v | {"_s": it["stratum"]})
    print(f"\n训练 {len(ytr)} 对（{a.train}{'，留一部片子' if a.lofo else ''}）、测试 {len(pairs)} 对（{a.test}）"
          + (f"；**另有 {n_lost} 对至少一端对不回 run（obs 重建过？），各臂都不计**" if n_lost else ""))
    if not pairs:
        raise SystemExit("没有一对标注对得回 run——obs / 层参数变了，真值要重抽")
    print(f"{'臂':<10}{'全部 准确率':>12}{'S2-4 误合并+误拆分':>22}{'S1 误合并+误拆分':>20}")
    for name, c in tot.items():
        def cnt(strata):
            return (sum(v for k, v in c.items() if k[0] in strata and k[1:] == ("different", "together")),
                    sum(v for k, v in c.items() if k[0] in strata and k[1:] == ("same", "apart")))
        r, s1 = cnt(("S2", "S3", "S4")), cnt(("S1",))
        acc = sum(x[name] for x in pairs) / len(pairs)
        print(f"{name:<10}{acc:>12.1%}{f'{r[0]}+{r[1]}={sum(r)}':>22}{f'{s1[0]}+{s1[1]}={sum(s1)}':>20}")
    names = [n for n in pairs[0] if n != "_s"]
    print("\n逐对配对（行臂对 / 列臂错）：")
    print(" " * 10 + "".join(f"{n:>10}" for n in names))
    for x in names:
        print(f"{x:<10}" + "".join(f"{sum(1 for r in pairs if r[x] and not r[y]):>10}" for y in names))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
