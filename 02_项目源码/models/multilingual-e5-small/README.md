# 本地多语言向量模型

此目录随项目交付真实权重，应用不会联网下载模型，也不会上传文档。

- 原始模型：[intfloat/multilingual-e5-small](https://huggingface.co/intfloat/multilingual-e5-small)，固定版本 `614241f622f53c4eeff9890bdc4f31cfecc418b3`，模型卡声明 MIT。
- ONNX 转换：[Xenova/multilingual-e5-small](https://huggingface.co/Xenova/multilingual-e5-small)，固定版本 `761b726dd34fb83930e26aab4e9ac3899aa1fa78`。本包使用 `onnx/model_quantized.onnx`，118,308,185 字节。
- MIT 正文取自原模型所属 [Microsoft UniLM](https://github.com/microsoft/unilm/blob/master/LICENSE)；保留 `LICENSE`、原模型卡和 `manifest.json` 中的 SHA256。量化转换仓库未额外声明许可，不将其误写为另一个许可证。
- 输出 384 维；CPU ONNX Runtime；默认 2 个推理线程；`query: ` / `passage: ` 前缀、attention mask 均值池化、L2 归一化。
- 单窗口 512 token。长片段按 tokenizer 实际 overflow 窗口、64 token 重叠覆盖全部文本，再平均并归一化；这是一项明确记录的本地适配，不声称与截断原模型卡实现逐位一致。
- 索引记录权重和 tokenizer 摘要、版本、维度及上述适配算法。权重或依赖缺失、校验失败时显示词法降级，不能将词频数组称为语义向量。

安装 `backend/requirements.lock.txt` 后再安装 `backend/requirements.retrieval.lock.txt`。使用现有模型目录不需要网络。显式修复缺失文件可运行：

```sh
python -m backend.retrieval.download_model --model-dir models/multilingual-e5-small
```

该修复命令逐文件校验清单，不自动更新版本。模型准确性和召回表现必须以固定测试集的实际结果说明；余弦相似度不等于答案正确率。
