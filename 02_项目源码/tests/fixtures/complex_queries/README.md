# 复杂问数与多轮复现资产

这两份公开数据库是教学样本，不是真实企业业务案例。Chinook 包含音乐销售数据；Northwind 是食品供销教学库的 small 版本。上游仓库、固定提交、原始 URL、文件大小、SHA-256 与许可证列在 `sources.json`；许可原文随库保留。数据库原件未改动。

`cases.frozen.json` 是正式冻结修订版 2，包含 30 个单题（24 个 SQL 查询、6 个歧义/资料不足问题），以及两条各 6 轮连续追问链。真值由独立参考 SQL 直接计算。初版和正式模型运行前的勘误保留在 `cases.v1.initial.json`、`errata-before-evaluation.md`。正式验收不得改题、改真值后覆盖首次结果。

`stress.sqlite` 单独用于几十表边界验证：5 张合成维修业务表、30 张干扰表，包含真实声明的外键以及两个同名技师。它的3题和选定ID后的澄清续答真值在 `stress-cases.frozen.json`。构造代码为 `build_stress_fixture.py`；代码拒绝覆盖已有冻结库，若需重建请复制脚本到新的空文件夹。所有业务记录均为程序构造，须与公开样本成绩分开报告。

冻结 JSON 保留原始字节与 SHA，其中 stress 文件记录的历史本机路径仅作来源记录。执行器不使用该路径，始终从本文件夹按数据库 ID 查找 SQLite 文件。`sources.json` 的文件路径已改为相对路径，便于整体复制。

## 运行

在项目根目录执行。默认读取 `.runtime/model.json` 的连接配置；凭据仅复制到新建隔离运行目录，权限为 600，报告不会记录凭据。正常主运行数据库不会写入。

```sh
# 查看帮助，不读取模型配置，不发出请求
.venv/bin/python tests/accept_complex_answers.py --help

# 默认30题和两条6轮链；runtime省略时自动创建带时间戳的临时目录
.venv/bin/python tests/accept_complex_answers.py --run-live --report /tmp/zhiheng-complex-first.json

# 可明确指定一个尚不存在的隔离目录，并选择题目或整条追问链
.venv/bin/python tests/accept_complex_answers.py --run-live --runtime /tmp/zhiheng-complex-run-001 --report /tmp/zhiheng-complex-selected.json --only chinook_01 chinook_country_time_followup

# 35表合成边界，包含同会话澄清后选定ID的真实续答
.venv/bin/python tests/accept_complex_answers.py --run-live --spec stress-cases.frozen.json --report /tmp/zhiheng-stress-first.json

# 离线重评已有报告；不调用模型、不读取配置，不覆盖原报告
.venv/bin/python tests/accept_complex_answers.py --reassess /tmp/zhiheng-complex-first.json --report /tmp/zhiheng-complex-reassessed.json
```

`--fixtures` 可指向整体复制后的资产目录；`--config-runtime` 可另行指定仅用于读取模型配置的目录。没有显式 `--run-live` 或离线 `--reassess` 时程序拒绝运行；不会隐式消费模型额度。

每次真实运行都要求新隔离目录。指定已有运行目录会被拒绝。相同题集的已有报告可追加新 run，保留每题首次失败、各次尝试、模型 usage/attempts、题目和来源摘要。脚本不自动重新提交失败请求。连续三次外部服务故障会停止新增提交；最多两道已在途的问题可能继续结束，断路状态写入报告。

## 判定边界

排序题按顺序比较，其他题按行多重集比较；列按位置比较，允许别名；数值绝对误差不超过 0.005，NULL 不能替换成0。合法空结果必须有实际执行查询。额外结果列不会被静默忽略。澄清续答必须保留父答案和 conversation_id。

自动比较仅验证状态、数值和延续关系。自然语言解释是否完整、来源导航是否正确、澄清选项是否可用仍需单独审阅。公开教学库可能出现在基础模型训练数据中，不能宣传成模型从未见过的第三方盲测。
