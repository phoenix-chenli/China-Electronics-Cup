# 版本化本地混合检索

```python
service = RetrievalService(runtime / 'retrieval', LocalE5(), default_mode='hybrid')
prepared = service.prepare(source, authorized_project_id, cancelled,
                           is_current=source_snapshot_still_current)
result = answer_document(source, question, gateway, cancelled, progress,
                         retriever=prepared)
```

`LocalE5` 实例复用惰性加载的 CPU session；资料和问题不会传到 embedding 服务。`prepare` 的授权项目必须由业务层提供，不接受模型生成的项目 ID。每份资料索引键包含 source ID/SHA256/解析版本/内容修订、切片版本、embedding 版本/权重与 tokenizer 摘要/维度。

SQLite 持久保存原文切片、词项倒排表与真实向量。构建批次事务提交，取消后复用已完成批次；构建完整且原件版本仍一致才原子发布 active generation。新版构建开始即失效旧版本句柄。归档/重解析必须调用 `invalidate(project_id, source_id)`；每次检索还会调用 `is_current`，业务层发布答案前应继续校验原件摘要。失效抛 `StaleIndexError`，取消抛 `InterruptedError`。

`prepared.select(question, limit=24, character_limit=16000, focus=None, exclude_ids=(), mode=None)` 只检索该句柄绑定的已选资料和项目，返回 `(Chunk列表, 是否上下文裁切)`。原文按页/偏移排列，实际召回排名在 `audit.searches[].selected_chunk_ids`。`prepared.prepared()` 可读持久切片；`prepared.audit()` 包含版本、覆盖、降级、扫描行数和每轮检索过程。查询长于 10,000 字符或 1,500 词项时明确记录裁切，原始用户问题不改写。

- 词法：持久中文二/三字片段与英文/数字词项，BM25。
- 向量：完整索引每 256 行精确余弦扫描；不先取头部 N 行，不依赖词法候选过滤。
- 融合：RRF，候选池每路至多 96，最终上下文预算仍 16k。
- 可选重排：`ExactContextReranker` 仅做明确年份/引号实体规则优先，是透明规则而非神经重排模型。固定夹具无整体增益，因此默认不启用。
- 原页、精确偏移、表格行与表头/单位/脚注的上下文保持；标题树路径只作结构上下文，不能当未引用事实。相同文本的不同页/年份/表格不会被全局去重。
- 模型不可用时显式词法降级；已发布词法索引不会每题偷偷重试 embedding。需要恢复向量时显式失效后重建。
- `store.cleanup(older_than_seconds=86400)` 清理旧的非 active generation，保留活跃与新建的可恢复索引。

解析预算仍由 parser 控制；索引只覆盖已解析文字，另有 2M 字符切片上限，与当前 parser 文本上限一致。审计保留解析限制、截断标志、页数、已索引字符及首尾位置。未召回、解析失败和原件没有答案是不同状态，检索层不能发布否定事实。

验收：`tests/test_retrieval_index.py` 使用明确控制向量验证生命周期，不能用来宣称语义质量；`tests/test_retrieval_real.py` 使用交付的真实 E5 权重，另有 fake 回答器的 API 重启复用检查。固定人工题集与公开原件纯检索报告分别存验收目录，均不等于最终问答准确率。
