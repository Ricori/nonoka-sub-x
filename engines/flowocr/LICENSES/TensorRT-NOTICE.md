# NVIDIA TensorRT（可选后端）

flowocr 可以用 NVIDIA TensorRT 在 GPU 上执行检测和识别（使用手册 `docs/manual/reference.md` 的 TensorRT 一节）。TensorRT 是 NVIDIA 的专有软件，按 NVIDIA 软件许可协议（NVIDIA Software License Agreement）提供：

- **flowocr 不分发 TensorRT**：发行包和源码仓库里都没有它的任何文件，`pyproject.toml` 的依赖里也没有。
- 用户运行 `flowocr-trt install` 时，程序从 NVIDIA 的包索引（`https://pypi.nvidia.com/tensorrt-cu13-libs/`）取 `tensorrt-cu13-libs` 发行包里的几个文件，
  装进用户自己的数据目录。版本和每个文件的 sha256 钉在 `src/flowocr/trt.py`。
- 取的文件：`nvinfer_10.dll`、`nvinfer_plugin_10.dll`、`nvonnxparser_10.dll`，本机显卡算力对应的一份 `nvinfer_builder_resource_*_10.dll`（用它建引擎失败时补取通用的 `nvinfer_builder_resource_ptx_10.dll`），以及发行包里的许可协议全文 `LICENSE.txt`。
  下载前可在 NVIDIA 官网读 TensorRT 的许可：<https://docs.nvidia.com/deeplearning/tensorrt/latest/reference/sla.html>（官网是最新版本）；随所装版本的那份全文装好后在数据目录的 `trt\<版本>\lib\LICENSE.txt`。下载和使用这些文件即表示接受该协议。
- 这些文件按原样使用、**未作任何修改**；推理通过 onnxruntime-gpu 自带的 TensorRT 执行提供程序加载它们。
- 引擎缓存（`trt\<版本>\engines\`）是 TensorRT 在用户机器上由 flowocr 使用的 PaddlePaddle 模型（PP-OCRv6，按语言另下的识别模型是 PP-OCRv5；来源与许可见同目录的 `PP-OCRv6-NOTICE.md`）编译出的文件，只在本机使用。
