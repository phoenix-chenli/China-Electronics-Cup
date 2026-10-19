# 可复现评测

本目录包含输入校验、隔离运行、离线评分及开发题集生成器。输入原件和 `inputs/suite.json` 与 `private-key.json` 分目录；模型只接收输入白名单，不读取参考答案。生成器只执行本地只读参考 SQL，不调用模型。

SQL100 为 40 道基础题、60 道复杂题，使用未改动的公开 Chinook/Northwind 教学原件及明确合成的边界库。context64 为 8 条根链、每链连续 8 轮；其中 5 条设计为全程可答正常链，另 3 条包含澄清或缺参后继续。跨链共享资料，64 轮不是 64 个独立样本。当前全部是 development；既有公开原件和已参与开发的转换件不能换个问法后声称保留集、真实企业数据或 SOTA。

## 生成原件、题目和独立参考答案

在源码根目录运行，下列输出目录必须尚不存在：

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m evaluation.build_sql_suite --output /absolute/path/sql-suite-new
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m evaluation.build_context_suite --output /absolute/path/context-suite-new --multimodal /absolute/path/multimodal-assets
```

上下文生成器的 `--multimodal` 是另附的受控多模态评测资料目录，须包含 `inputs/suite.json`、对应原件和 `context-support/source-manifest.json`。公式页、扫描页、参数表和参数图保留同源簇，不算独立实验。生成后核对 `reference-audit.json` 并完成独立题意/key 审查，才执行真实问答。不能根据模型输出修改答案 key。

## 隔离真实运行

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m evaluation.runner run \
  --suite /absolute/path/suite/inputs/suite.json \
  --runtime /absolute/path/new-isolated-runtime \
  --output /absolute/path/run-unique-id.json \
  --mode live --config-runtime /absolute/path/existing-config-runtime \
  --timeout 1800
```

配置只读，不复制密钥。每次运行使用独立目录、独立报告，串行执行题目；已有文件拒绝覆盖。`--timeout` 是单题墙钟取消边界，不是 API 次数限额。未知 POST 结果不自动重发；复测使用新的试验编号。若依赖的父轮失败，后续轮记录为未运行，仍保留在分母中。报告记录实际输入集合摘要、后端代码前后摘要、每条真实导出记录、执行事件和时间；代码在途中改变不能称为固定版本最终结果。

导入 API 当前只接收文件。评测准备阶段会额外把冻结清单中的 `cluster` 写为隔离 runtime 的 `source_group`，保留来源描述，并记录 `source_provenance_setup`；这属于已知原件出处的评测设置，不声称上传界面具有编辑元数据功能。通用“本地合成”描述不作为共同原件标识。设置发生在创建任何答案之前，不能写入参考事实、期望值或评分项。

`--mode fault` 仅为明确故障注入、0 外部模型调用，不能拿故障终态当成功问答。`replay` 仅复制已有记录并标明离线重放，也不是新实测。

## 离线评分和独立审阅

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m evaluation.runner score \
  --input /absolute/path/run-unique-id.json \
  --key /absolute/path/suite/private-key.json \
  --output /absolute/path/score-before-review.json
```

SQL 执行匹配保留重复行、NULL、数值容差及题目要求的顺序。只有外层 ORDER BY 才要求结果排序，窗口内部排序不自动意味着输出有序。结果行匹配单列为 `execution_matched`，不等于解释、限定、引用和上下文正确。自由文本、业务拒答以及标记 `require_text_review` 的 SQL 题需要绑定实际记录的独立审阅；单凭 completed 或 insufficient_evidence 不算正确。HTTP/传输失败与业务拒答分开。

参考 key 冻结后保持不变。审阅结论另外保存：

```json
{
  "run_sha256": "整份运行JSON的SHA256",
  "reviewer": "如实说明审阅者及方法，AI审阅不是真人用户研究",
  "cases": {
    "case-id": {
      "record_sha256": "evaluation.core.record_digest(该条record)",
      "correct": true,
      "complete": true,
      "grounded": true,
      "context_correct": true,
      "notes": "对照原件核对事实、范围、单位、否定与引用的具体依据"
    }
  }
}
```

`context_correct` 对上下文链是必要项。随后通过 `score --reviews /absolute/path/review.json` 写到新的评分文件。整份 run 摘要或单条 record 摘要不匹配时不能沿用旧审阅。没有审阅的项保留 pending，不能以模型自报 supported 代替独立评判。澄清题的“先澄清”交互策略与安全完整枚举的事实正确性应分开报告。

评分遍历冻结 key 的全部题目，崩溃只跑到一半不能缩小分母。整链成功要求该链所有 8 轮均通过；侧分支、旧 revision、资料变更、历史只读和越权恢复探针另计，不能拿 409 或失败请求凑 8 轮。长历史输入的前 3 轮参考行集超过 60k 字符只证明样本具备触发条件，实际压缩/取回是否发生要看运行记录，不能预先宣称通过。
