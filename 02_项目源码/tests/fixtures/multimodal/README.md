# 第四阶段冻结验收资料

`cases.v2.frozen.json` 是三道人工合成题：五种格式的奖金计算、缺少公式、方案冲突。`public-cases.v2.frozen.json` 是五道真实公开资料题：跨地区差额、同源对照、政策任务交叉核对、计划不能证明落地、原表口径差异。`scan-cases.frozen.json` 使用真实生态环境公报扫描原页，核对两章数字。

每份题目有对应 SHA-256 文件，预期值在首次运行前冻结，执行器只把问题和所选资料交给模型。旧 v1 题目保留；其阴性仅按拒答状态判定，存在不检索就得分的问题。v2 保留题目和正确值，增加实际检索覆盖、具体依据和单独复核条件；同源对照也须实际读取两份文件。不同版本的通过数不能直接合并。

`build_fixtures.py` 生成的销售、员工、方案和公式材料是显式测试夹具，不代表真实企业数据。五格式题正确链为销售记录→员工编号→人员映射→方案规则→参数与公式→奖金。公开文件的来源、摘要与同源关系见 `public-sources.json`；转换 DOCX、CSV 与原页截图不能作为新增独立来源。公式图必须作为二维图像读取，OCR文字不替代原页。

生态环境扫描摘录只含原公报第7、47、101、137页，依次对应摘录第1–4页；没有完成150页全量识别。扫描质量与实际双读法对照见 `docs/第四阶段验收/扫描件/`。

在项目根目录运行：

```sh
.venv/bin/python tests/accept_multimodal.py --prepare-only \
  --runtime /tmp/stage4-synthetic-runtime \
  --spec cases.v2.frozen.json --output /tmp/stage4-parse.json
.venv/bin/python tests/accept_multimodal.py --run-live \
  --runtime /tmp/stage4-synthetic-runtime \
  --spec cases.v2.frozen.json --output /tmp/stage4-live-first.json
```

运行目录必须隔离；模型配置仅存该目录、权限600。报告记录代码摘要、题目摘要、实际来源版本、回答、用量和模型返回内容，不保存密钥或请求头。真实调用可能消耗服务额度。重复运行使用新的输出文件；解析代码变更后使用新的运行目录，避免复用旧解析缓存。有限题目通过不构成通用准确率承诺。
