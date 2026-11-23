# LaMa 模型的来源与许可

阶段 5（`flowocr-burn --inpaint`）用 LaMa 把底板盖住的原文从画面里抹掉。模型由 `flowocr.models` 从 Hugging Face 按固定提交号下载，用到时才下载：

| 用途 | 仓库 | 文件 | 提交 | 许可 |
| --- | --- | --- | --- | --- |
| 图像修补（inpainting） | [Carve/LaMa-ONNX](https://huggingface.co/Carve/LaMa-ONNX) | `lama_fp32.onnx` | `c3c0c9e468934d62e79c329e35d82dd09ff8c444` | Apache License 2.0 |

- 上游是 [advimman/lama](https://github.com/advimman/lama)（Roman Suvorov 等，"Resolution-robust Large Mask Inpainting with Fourier Convolutions"，WACV 2022），Apache License 2.0。
  上游只发 PyTorch 权重，不发 ONNX。
- `Carve/LaMa-ONNX` 是第三方（Carve）从上游的 big-lama 权重导出的 ONNX，模型卡标的许可是 Apache License 2.0。
  这是 flowocr "模型只用官方发布的" 这条做法的一个例外：另一条路是我们自己从官方权重导出再发布，那等于我们自己当第三方。
- 两个仓库都没附 NOTICE 文件；Apache License 2.0 全文在同目录的 `Apache-2.0.txt`，亦见 <https://www.apache.org/licenses/LICENSE-2.0>。

## 我们做的修改

修补器加载的 `lama_<高>x<宽>_act<k>_v<版本>.onnx` 由同目录的 `lama_fp32.onnx` 派生，三步（实现见 `src/flowocr/burn/lamagraph.py`）：

1. **折叠常量**：用 ONNX Runtime 的基础图优化、batch 固定为 1，把运行时现算的 DFT 矩阵和形状算子折成常量。
2. **改输入尺寸**：原图的傅里叶单元和 512×512 的输入绑死。按上游 FourierUnit 的定义重写每个傅里叶单元（实 FFT → 1×1 卷积 + ReLU → 逆 FFT，FFT 写成和输入尺寸对应的矩阵乘），
   让图接受别的输入尺寸（现在用 320×768 和 448×1344）；1×1 卷积的权重沿用原图，别的层原样保留。
3. **激活整体缩小**（k = 64）：生成器入口乘 1/k、卷积 / 反卷积的偏置和 BatchNorm 的 beta、mean 除以 k、最后的 Sigmoid 前乘回 k。
   数学上输出不变（fp32 下只差舍入），作用是让半精度推理不溢出。卷积权重不变。
