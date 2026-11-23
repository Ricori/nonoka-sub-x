# PP-OCRv6 模型的来源与许可

flowocr 默认的 ONNX Runtime 推理路径使用 PaddlePaddle 官方发布的 PP-OCRv6 模型，由 `flowocr.models` 从 Hugging Face 按固定提交号下载：

| 用途 | 仓库 | 提交 | 许可 |
| --- | --- | --- | --- |
| 文字识别（rec） | [PaddlePaddle/PP-OCRv6_medium_rec_onnx](https://huggingface.co/PaddlePaddle/PP-OCRv6_medium_rec_onnx) | `50c7eacafc52fa7bcf4194e8cd08e46f8558504b` | Apache License 2.0 |
| 文字检测（det） | [PaddlePaddle/PP-OCRv6_medium_det_onnx](https://huggingface.co/PaddlePaddle/PP-OCRv6_medium_det_onnx) | `61323801669c338b7891481ec7bac61ce31b576a` | Apache License 2.0 |

按语言另下的识别模型（`--ocr-lang ko / ru / th / auto` 用到时才下载，同样按固定提交号）：

| 用途 | 仓库 | 提交 | 许可 |
| --- | --- | --- | --- |
| 韩文识别 | [PaddlePaddle/korean_PP-OCRv5_mobile_rec_onnx](https://huggingface.co/PaddlePaddle/korean_PP-OCRv5_mobile_rec_onnx) | `5c6f574b8e2230adf4287b33e736d71b9fabd28e` | Apache License 2.0 |
| 俄文（东斯拉夫语）识别 | [PaddlePaddle/eslav_PP-OCRv5_mobile_rec_onnx](https://huggingface.co/PaddlePaddle/eslav_PP-OCRv5_mobile_rec_onnx) | `9a32171fc5718746875e1a261818884517975013` | Apache License 2.0 |
| 泰文识别 | [PaddlePaddle/th_PP-OCRv5_mobile_rec_onnx](https://huggingface.co/PaddlePaddle/th_PP-OCRv5_mobile_rec_onnx) | `1d4adbbafb1034a2fd6618498575b81ea7b69f69` | Apache License 2.0 |

模型版权归 PaddlePaddle Authors（[PaddleOCR](https://github.com/PaddlePaddle/PaddleOCR)）。
上游模型仓库未附 NOTICE 文件，也未附许可证全文；Apache License 2.0 全文在同目录的 `Apache-2.0.txt`
（取自 paddleocr 3.7.0 发行包的 `LICENSE`，含 PaddlePaddle Authors 的版权声明），亦见 <https://www.apache.org/licenses/LICENSE-2.0>。

## 我们做的修改

每个识别模型的 `inference_argmax2.onnx` 由同目录上游的 `inference.onnx` 派生（默认的 `PP-OCRv6_medium_rec_onnx` 和上面三个语言模型同样处理）：在计算图末尾追加取最大值和最大值下标的节点（默认模型沿类别维分段求、再在段间求，用 `Reshape`、`ReduceMax`、`ArgMax`、`GatherElements` 等；上面三个语言模型的图版本较老，直接用一个 `ArgMax` 和一个 `ReduceMax`），
并把图的输出从 softmax 的结果改为这两样（`cls`、`prob`）。**权重与原有节点未作任何改动。**
实现见 `src/flowocr/models.py` 的 `fuse_argmax`。

GPU 上用 CUDA 执行 det 时默认用半精度（`--det-precision fp16`；选 `fp32`、在 CPU 上跑或走 TensorRT 时用上游原图），为此在同目录生成 `inference_fp16v1.onnx`：用 ONNX Runtime 自带的转换器（`onnxruntime.transformers.float16`）把权重和中间计算转成半精度，输入和输出保持单精度（图的首尾各加一个 `Cast` 节点）；网络结构不变。实现见 `src/flowocr/models.py` 的 `fp16_derive`。
