"""逐通道消融：哪种信号对"这两条 run 该不该在一起"真有信息（特征见 `pair_features.py`）。

在 dev 的标注对上训一个小模型、在留出集上测，**逐通道**报：单独用这一条 / 全用 / 全用但去掉这一条。
同一批留出对上各条缝合臂的对错率摆在旁边当参照（臂只有"同槽 / 异槽"两个输出，模型是它的上界参照，不是替代品）。

⚠ 样本只有几百对：只读**方向和量级**，别读小数点。模型用带标准化的逻辑回归（可解释、不容易过拟合）；
`--gbdt` 另报一个浅的梯度提升做对照——两者方向不一致的通道不要信。

用法：
    python -m flowocr.analyze.pair_model --train dev --test acceptance
    python -m flowocr.analyze.pair_model --train dev,acceptance --test seed3        # seed 3 那批标完之后
"""
from __future__ import annotations

import argparse
import glob
import json
from pathlib import Path

import numpy as np

from flowocr.analyze import cluster_layers as CL  # noqa: E402
from flowocr.analyze import pair_features as PF  # noqa: E402
from flowocr import paths  # noqa: E402
from flowocr.analyze import slot_pairs as SP  # noqa: E402


def load(selectors: list[str]):
    """selectors：`dev` / `acceptance`（按 split）或 `seedN`（按 seed）。返回 X, y, 元信息, 特征名。"""
    X, y, meta, names = [], [], [], None
    by_tag: dict[str, list] = {}
    for p in sorted(glob.glob(str(paths.data_root() / "data/gt/slotpairs-*.json"))):
        doc = json.loads(Path(p).read_text(encoding="utf-8"))
        if not (doc.get("split") in selectors or f"seed{doc['seed']}" in selectors):
            continue
        if any(s.startswith("seed") for s in selectors) and f"seed{doc['seed']}" not in selectors:
            continue
        by_tag.setdefault(doc["tag"], []).extend(doc["items"])
    for tag, items in by_tag.items():
        L = CL.load(tag)
        F = PF.PairFeatures(L)
        key = {(r.t_start, r.t_end, r.text, tuple(round(v) for v in r.box)): i for i, r in enumerate(L.runs)}
        for it in items:
            if it["label"] not in ("same", "different"):
                continue
            i, j = key.get(SP.item_key(it["a"])), key.get(SP.item_key(it["b"]))
            if i is None or j is None:
                continue
            x, names = F.pair(i, j)
            X.append(x)
            y.append(int(it["label"] == "same"))
            meta.append((tag, it["id"], it["stratum"]))
    return np.array(X, float), np.array(y), meta, names


def main() -> int:
    from sklearn.ensemble import GradientBoostingClassifier
    from sklearn.linear_model import LogisticRegression
    from sklearn.metrics import roc_auc_score
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--train", default="dev")
    ap.add_argument("--test", default="acceptance")
    ap.add_argument("--gbdt", action="store_true")
    a = ap.parse_args()
    Xtr, ytr, _, names = load(a.train.split(","))
    Xte, yte, mte, _ = load(a.test.split(","))
    print(f"训练 {len(ytr)} 对（same {ytr.mean():.0%}）、留出 {len(yte)} 对（same {yte.mean():.0%}）；特征 {len(names)} 个")

    def fit_eval(cols: list[int], gbdt: bool = False) -> tuple[float, float]:
        m = (GradientBoostingClassifier(n_estimators=60, max_depth=2, subsample=0.8, random_state=0) if gbdt
             else make_pipeline(StandardScaler(), LogisticRegression(C=0.5, max_iter=2000)))
        m.fit(Xtr[:, cols], ytr)
        p = m.predict_proba(Xte[:, cols])[:, 1]
        return float(((p > 0.5) == yte).mean()), float(roc_auc_score(yte, p))

    chan = {c: [k for k, n in enumerate(names) if n.startswith(c + ":")] for c in PF.CHANNELS}
    allc = list(range(len(names)))
    kinds = [("LR", False)] + ([("GBDT", True)] if a.gbdt else [])
    for kind, g in kinds:
        acc, auc = fit_eval(allc, g)
        print(f"\n[{kind}] 全部通道：准确率 {acc:.1%}、AUC {auc:.3f}")
        print(f"  {'通道':<10}{'只用它':>16}{'去掉它':>16}   （准确率 / AUC）")
        for c, cols in chan.items():
            a1, u1 = fit_eval(cols, g)
            a0, u0 = fit_eval([k for k in allc if k not in cols], g)
            print(f"  {c:<10}{f'{a1:.1%} / {u1:.3f}':>16}{f'{a0:.1%} / {u0:.3f}':>16}")
    m = make_pipeline(StandardScaler(), LogisticRegression(C=0.5, max_iter=2000)).fit(Xtr, ytr)
    coef = m[-1].coef_[0]
    print("\nLR 标准化系数（正 = 倾向 same）：")
    for k in np.argsort(-np.abs(coef))[:14]:
        print(f"  {names[k]:<24}{coef[k]:+.2f}")

    # 参照：同一批留出对上各条缝合臂的准确率
    from flowocr.analyze import slot_modes
    by_tag: dict[str, list[int]] = {}
    for k, (tag, _, _) in enumerate(mte):
        by_tag.setdefault(tag, []).append(k)
    docs = {}
    for p in sorted(glob.glob(str(paths.data_root() / "data/gt/slotpairs-*.json"))):
        d = json.loads(Path(p).read_text(encoding="utf-8"))
        for it in d["items"]:
            docs[it["id"]] = it
    right: dict[str, int] = {}
    n_ref = 0
    for tag, ks in by_tag.items():
        L = CL.load(tag)
        asg = {name: SP.assignment(L, CL.stitch(L, **over)) for name, over in SP.ARMS.items()}
        asg["modes"] = SP.assignment(L, slot_modes.stitch(L))
        got = SP.pipeline_assign(L, "lines")          # 管线里 `--cluster lines` 的分组
        asg["lines"] = {(r.t_start, r.t_end, r.text, tuple(round(v) for v in r.box)): got[i] for i, r in enumerate(L.runs)}
        for k in ks:
            v = SP.verdicts(asg, docs[mte[k][1]])
            if v is None:                          # 对不回 run 的不进任何臂的分母
                continue
            n_ref += 1
            for name, ok in v.items():
                right[name] = right.get(name, 0) + int(ok)
    print("\n参照——同一批留出对上各条缝合臂的准确率（含 S1）：")
    print(f"  （{n_ref}/{len(yte)} 对在各臂上都对得回 run）  " + "  ".join(f"{n} {v / max(1, n_ref):.1%}" for n, v in right.items()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
