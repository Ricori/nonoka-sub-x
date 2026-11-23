"""LaMa 的派生图：从 Hugging Face 上那份 `lama_fp32.onnx`（512×512，`flowocr.models` 的 `lama`）做出修补器真正加载的图。三步，权重的值不动：

1. **折叠常量**：原导出把傅里叶单元的 FFT 写成运行时现算的稠密 DFT（216 个 Einsum、sin / cos 各 144 个）。ORT 的基础图优化
   在 batch 固定为 1 时把它们和几千个形状算子折成 fp32 常量（节点约 17480 → 2844）。
2. **扁窗**：原图的 FFT 矩阵和 64×64（512 / 8）绑死。按 LaMa 的 FourierUnit 重写每个傅里叶单元：
   `rfft2(x, norm="ortho")` → 实部虚部叠成通道 → 1×1 卷积 + ReLU（权重沿用原图）→ `irfft2`。FFT 仍写成矩阵乘（TensorRT 不吃 DFT 算子）：
   宽方向实 FFT 用 w × (w/2+1) 的 cos / sin，高方向用 h × h 的复 DFT，逆变换宽方向按厄米对称加权（直流和奈奎斯特权 1、其余权 2）。
   别的层和尺寸无关、原样保留。高、宽要能被 16 整除；512×512 时和原图等价。
3. **激活缩小**（`act`）：生成器在输入拼接（`/Concat`）和最后的 Sigmoid 之间只有卷积、反卷积、BN、ReLU、反射 Pad、矩阵乘、加减和形状算子，
   对"激活和全部偏置一起缩放"是齐次的：入口乘 1/k、卷积 / 反卷积的偏置和 BN 的 beta、mean 除以 k、Sigmoid 前乘回 k，输出不变
   （k 取 2 的幂，fp32 下只差舍入）。不缩的话 fp16 在后几个残差块和解码器上溢出（fp32 下量到约 85 万，超过 fp16 上限 65504），出 NaN。

只有第 3 步的结果落盘（模型目录里的 `lama_<高>x<宽>_act<k>_v<版本>.onnx`），中间的两份每份约 200 MB，不留。
旁边的来源标记记源文件的 sha256、做法版本（`VERSION`）和 ORT 版本（第 1 步用的是 ORT 的图优化）：对不上就重做。
"""
from __future__ import annotations

import re
import tempfile
from collections import defaultdict
from pathlib import Path

import numpy as np

from flowocr import models

VERSION = 1
"""做法的版本（文件名和来源标记都带它）：派生的做法改了，已经派生过的图要重做。"""


def path(h: int, w: int, act: int) -> str:
    """h × w 输入、激活缩小 act 倍的派生图；缺了（或过期了）就从源文件做一份。源文件缺了按 `flowocr.models` 的规矩取。"""
    import onnxruntime as ort
    m = models.MODELS["lama"]
    src = Path(models.path("lama"))
    dst = m.dir / f"lama_{h}x{w}_act{act}_v{VERSION}.onnx"
    mark = dst.with_name(dst.name + ".src-sha256")
    want = f"{m.files[m.main]} lamagraph{VERSION} ort{ort.__version__}"
    if not (dst.is_file() and mark.is_file() and mark.read_text(encoding="utf-8").strip() == want):
        print(f"[模型] 派生 LaMa {h}×{w}（折叠、扁窗、激活缩小 {act} 倍），只在第一次跑…", flush=True)
        models._save_derived(scaled(flat(folded(src), h, w), act), dst, mark, want)
        print(f"[模型] {dst.name}（{dst.stat().st_size / 2**20:.1f} MB）", flush=True)
    return str(dst)


def folded(src: Path):
    """第 1 步：ORT 基础图优化、batch 固定为 1，折叠常量。"""
    import onnx
    import onnxruntime as ort
    with tempfile.TemporaryDirectory(prefix="flowocr-lama-") as td:
        so = ort.SessionOptions()
        so.log_severity_level = 3
        so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_BASIC
        so.add_free_dimension_override_by_name("batch", 1)
        so.optimized_model_filepath = str(Path(td) / "folded.onnx")
        ort.InferenceSession(str(src), so, providers=["CPUExecutionProvider"])
        return onnx.load(so.optimized_model_filepath)


def dft_mats(h: int, w: int) -> dict[str, np.ndarray]:
    k = w // 2 + 1
    n = np.arange(w)
    kk = np.arange(k)
    s = 1 / np.sqrt(h * w)                                    # ortho：正反变换各乘 1/sqrt(hw)
    ang_w = 2 * np.pi * np.outer(n, kk) / w                    # [w, k]
    m = np.arange(h)
    ang_h = 2 * np.pi * np.outer(m, m) / h                     # [h, h]
    wt = np.full(k, 2.0)
    wt[0] = 1
    if w % 2 == 0:
        wt[-1] = 1
    ang_i = 2 * np.pi * np.outer(kk, n) / w                    # [k, w]
    f32 = lambda a: a.astype(np.float32)                       # noqa: E731
    return {
        "cw": f32(np.cos(ang_w) * s), "nsw": f32(-np.sin(ang_w) * s),    # 宽方向正变换（e^{-iθ}）
        "ch": f32(np.cos(ang_h)), "sh": f32(np.sin(ang_h)),               # 高方向（正变换 e^{-iθ}、逆变换 e^{+iθ} 都用它们组合）
        "aw": f32(np.cos(ang_i) * wt[:, None] * s), "bw": f32(np.sin(ang_i) * wt[:, None] * s),   # 宽方向逆变换
    }


def fu_parts(g):
    """按节点名里的 `/fu/` 把 36 个傅里叶单元分组，各找出唯一的输入、输出、1×1 卷积和 ReLU。"""
    by = defaultdict(list)
    for i, n in enumerate(g.node):
        m = re.search(r"^(.*/fu)/", n.name)
        if m:
            by[m.group(1)].append(i)
    init = {x.name for x in g.initializer}
    users = defaultdict(list)
    for i, n in enumerate(g.node):
        for x in n.input:
            users[x].append(i)
    outs_graph = {o.name for o in g.output}
    parts = []
    for pre, idx in by.items():
        s = set(idx)
        made = {o for i in idx for o in g.node[i].output}
        ins = {x for i in idx for x in g.node[i].input if x and x not in made and x not in init}
        outs = {o for i in idx for o in g.node[i].output if any(u not in s for u in users[o]) or o in outs_graph}
        convs = [g.node[i] for i in idx if g.node[i].op_type == "Conv"]
        relus = [g.node[i] for i in idx if g.node[i].op_type == "Relu"]
        other = sorted({g.node[i].op_type for i in idx} - {"MatMul", "Einsum", "Reshape", "Transpose", "Gather", "Unsqueeze",
                                                             "Squeeze", "Concat", "Slice", "Neg", "Add", "Sub", "Div", "Conv", "Relu"})
        if not (len(ins) == 1 and len(outs) == 1 and len(convs) == 1 and len(relus) == 1 and not other):
            raise ValueError(f"傅里叶单元 {pre} 的结构和预期不同（输入 {ins}、输出 {outs}、卷积 {len(convs)}、别的算子 {other}）：源模型换了？")
        parts.append((pre, idx, ins.pop(), outs.pop(), convs[0]))
    return parts


def flat(m, h: int, w: int):
    """第 2 步：折叠后的图（固定 512×512）改成 h × w 输入，只重建傅里叶单元。原地改 m 并返回它。"""
    import onnx
    from onnx import helper, numpy_helper
    if h % 16 or w % 16:
        raise ValueError(f"窗的高、宽要能被 16 整除：{h}×{w}")
    g = m.graph
    hf, wf = h // 8, w // 8
    kf = wf // 2 + 1
    parts = fu_parts(g)
    drop = {i for _, idx, *_ in parts for i in idx}
    new_init = [numpy_helper.from_array(v, f"flatfu_{k}") for k, v in dft_mats(hf, wf).items()]
    shp = lambda name, v: numpy_helper.from_array(np.array(v, np.int64), name)       # noqa: E731
    new_init += [shp("flatfu_shape_in", [1, -1, hf, kf]), shp("flatfu_shape_out", [1, -1, 2, hf, kf]),
                 numpy_helper.from_array(np.array([2], np.int64), "flatfu_ax2"),
                 numpy_helper.from_array(np.array(0, np.int64), "flatfu_i0"), numpy_helper.from_array(np.array(1, np.int64), "flatfu_i1")]
    new_nodes_at = {}
    for pre, idx, x, y, conv in parts:
        p = pre.replace("/", "_") + "_flat"
        t = lambda s: f"{p}/{s}"                                                     # noqa: E731
        mm = lambda a, b, o: helper.make_node("MatMul", [a, b], [t(o)], name=t(o))   # noqa: E731
        new_nodes_at[min(idx)] = [
            mm(x, "flatfu_cw", "xw_re"), mm(x, "flatfu_nsw", "xw_im"),
            mm("flatfu_ch", t("xw_re"), "a"), mm("flatfu_sh", t("xw_im"), "b"),
            helper.make_node("Add", [t("a"), t("b")], [t("re")], name=t("re")),                 # (a+ib)(cos−i sin) 的实部
            mm("flatfu_ch", t("xw_im"), "c"), mm("flatfu_sh", t("xw_re"), "d"),
            helper.make_node("Sub", [t("c"), t("d")], [t("im")], name=t("im")),
            helper.make_node("Unsqueeze", [t("re"), "flatfu_ax2"], [t("re_u")], name=t("re_u")),
            helper.make_node("Unsqueeze", [t("im"), "flatfu_ax2"], [t("im_u")], name=t("im_u")),
            helper.make_node("Concat", [t("re_u"), t("im_u")], [t("st")], name=t("st"), axis=2),
            helper.make_node("Reshape", [t("st"), "flatfu_shape_in"], [t("spec")], name=t("spec")),
            helper.make_node("Conv", [t("spec"), *conv.input[1:]], [t("conv")], name=t("conv"),
                             **{a.name: helper.get_attribute_value(a) for a in conv.attribute}),
            helper.make_node("Relu", [t("conv")], [t("relu")], name=t("relu")),
            helper.make_node("Reshape", [t("relu"), "flatfu_shape_out"], [t("r2")], name=t("r2")),
            helper.make_node("Gather", [t("r2"), "flatfu_i0"], [t("Re")], name=t("Re"), axis=2),
            helper.make_node("Gather", [t("r2"), "flatfu_i1"], [t("Im")], name=t("Im"), axis=2),
            mm("flatfu_ch", t("Re"), "e"), mm("flatfu_sh", t("Im"), "f"),
            helper.make_node("Sub", [t("e"), t("f")], [t("yre")], name=t("yre")),               # (a+ib)(cos+i sin)
            mm("flatfu_ch", t("Im"), "g"), mm("flatfu_sh", t("Re"), "k"),
            helper.make_node("Add", [t("g"), t("k")], [t("yim")], name=t("yim")),
            mm(t("yre"), "flatfu_aw", "p"), mm(t("yim"), "flatfu_bw", "q"),
            helper.make_node("Sub", [t("p"), t("q")], [y], name=t("out")),
        ]
    out_nodes = []
    for i, n in enumerate(g.node):
        if i in new_nodes_at:
            out_nodes += new_nodes_at[i]
        if i not in drop:
            out_nodes.append(n)
    del g.node[:]
    g.node.extend(out_nodes)
    used = {x for n in g.node for x in n.input}
    keep = [x for x in g.initializer if x.name in used]
    del g.initializer[:]
    g.initializer.extend(keep + new_init)
    del g.value_info[:]
    for v in list(g.input) + list(g.output):
        d = v.type.tensor_type.shape.dim
        if len(d) == 4:
            d[2].dim_value, d[3].dim_value = h, w
    onnx.checker.check_model(m)
    return m


def scaled(m, k: int):
    """第 3 步：生成器内部的激活整体缩小 k 倍（见文件头）。原地改 m 并返回它；k = 1 原样返回。"""
    if k == 1:
        return m
    from onnx import helper, numpy_helper
    g = m.graph
    init = {t.name: t for t in g.initializer}
    cat = next(n for n in g.node if n.name == "/Concat")
    sig = next(n for n in g.node if n.op_type == "Sigmoid")
    region, live = set(), {cat.output[0]}
    for n in g.node:                                         # 节点按拓扑序：从入口拼接往下、到 Sigmoid 为止
        if n is sig:
            break
        if any(i in live for i in n.input):
            region.add(n.name)
            live.update(n.output)
    pos = {"Conv": (2,), "ConvTranspose": (2,), "BatchNormalization": (2, 3)}
    old = set()
    for n in g.node:
        for j in pos.get(n.op_type, ()) if n.name in region else ():
            if j < len(n.input):
                a = numpy_helper.to_array(init[n.input[j]]) / np.float32(k)
                name = f"{n.input[j]}__act{k}"
                g.initializer.append(numpy_helper.from_array(a.astype(np.float32), name))
                old.add(n.input[j])
                n.input[j] = name
    still = {i for n in g.node for i in n.input}
    keep = [t for t in g.initializer if t.name not in old or t.name in still]   # 换下来的旧偏置没人用了就删（不然 ORT 建会话时逐个警告）
    del g.initializer[:]
    g.initializer.extend(keep)
    g.initializer.extend([numpy_helper.from_array(np.array(1 / k, np.float32), "act_down"),
                          numpy_helper.from_array(np.array(k, np.float32), "act_up")])
    for n in g.node:
        if n.name in region:
            n.input[:] = [f"{cat.output[0]}__down" if i == cat.output[0] else i for i in n.input]
    x = sig.input[0]
    sig.input[0] = f"{x}__up"
    nodes = list(g.node)
    nodes.insert(nodes.index(cat) + 1, helper.make_node("Mul", [cat.output[0], "act_down"], [f"{cat.output[0]}__down"], name="/act_down"))
    nodes.insert(nodes.index(sig), helper.make_node("Mul", [x, "act_up"], [f"{x}__up"], name="/act_up"))
    del g.node[:]
    g.node.extend(nodes)
    return m
