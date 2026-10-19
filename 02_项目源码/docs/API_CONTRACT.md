# 本地接口约定 v5

终版开发的兼容增量见 [终版结果契约](终版结果契约.md)。以下批次记录保留其历史适用范围；新的任务授权以本目录 AGENTS.md 为准。

## 2026-10-17 第三批修复增量（当前）

HTTP 路径、前端、health产品版本0.5.0和解析版本`zhiheng-multimodal-2.1`保持不变。当前证据见 [第三批完成说明](第三批修复验收/完成说明.md)，以下新增字段不回写旧回答。

- 表格事实可带`context_annotations`，逐格绑定中同时保留相应注记。只有本条已绑定原表中可定位的表题、物理页、逻辑节、明确年度或真实行键才可成为注记；记录答案中的精确范围及源锚点。完整书名号表题或带明确“附表/表题”前缀的引号表题须与同表前方连续段落精确对应；行键须为当前格所在行的`explicit_key`，完整列标签及键值相符（支持“为/：/:”分隔）。所有定位注记采用相同业务单位后缀检查，不能将“序号5小时”中的5当成可忽略的定位数。注记数字不能作为金额、比例或其他业务值使用，显示正文不被修改；原格、行列、单位和独立语义复核仍必需。
- 跨资料正常候选的本地检查失败，可在`diagnostics.candidate_attempts`保存有界的未采纳候选审计。审计与已发布`claims`分离，不进入证据注册表，也不能成为新事实的依据。存储前按白名单与既有路径/凭据规则脱敏，截断、遗漏明确标记。
- 后续修正轮仅接收最近失败候选的有限反馈，不重复发送全部审计历史，不挤走原有证据，不增加模型调用、动作或轮数上限。生成提示要求单事实子句与绑定statement精确一致，表头业务标签与真实SQL列名分别使用。
- `completed`仍须通过精确引用、绑定、独立关系复核和分项覆盖。保存失败候选不代表采纳，也不允许将未执行的计算或错误关系当作成功结论。

## 2026-10-17 第二批修复增量（继续保留）

HTTP 路径、health产品版本0.5.0和前端保持不变；解析版本更新为 `zhiheng-multimodal-2.1`。本节说明当前新任务的约束，历史保存记录不回写、不补造新字段。最终验证范围见 [第二批完成说明](第二批修复验收/完成说明.md)。

- `SourceDetail.metadata.table_semantics?: object[]` 使用 `table-semantics-v1`。每表保留 `table_name`、format、page/sheet、source_position、header_rows/header_basis、columns[].header_path、row_headers、cells、caption、units、footnotes、context、requires_review/review_reasons、truncated；结构不可确认时可有 `structure_unknown:true`。cell保留稳定的 `R行C列` 标识、1-based row/col、原值、rowspan/colspan/covered_by及真实可用的bbox或start/end。覆盖格不产生独立值。单位与表头候选不是已核实的业务事实。
- 表格语义最多64张表，每表最多2,048格／24,000单元格文字字符，表外上下文最多4,000字符；达到限制保留明确截断状态。XLSX 的 `sql_row_mapping.runs` 为 `[first_sql_row,first_source_row,count]`，避免跳过空行后把SQL行号冒充工作表源行号。不同数据段的表头归属不明时标为unknown；原始格值和SQL导入值不被语义推断改写。
- `Evidence.table_context?: {tables,has_table,truncated,requires_review,unresolved_structure?}` 是本条实际文字范围或SQL来源范围内的有界结构投影。tables包含 `selected_cell_ids` 和 `selection_state`，并保留理解这些格所需的表头、行对象、单位、脚注。文字定位依真实offset/bbox，不以相同数值反查猜格；聚合、连接等使用 `table_only`，不编造贡献行。未知、无有效主格或预算不足保持显式限制，不能退化成无约束的普通文本引用。
- `Claim.table_bindings?: object[]` 对文档和跨源的表内事实保存已校验绑定：evidence_id、source_id、sha256、parser_version、table_name、cell_id、row/col、原格value、statement、完整column_label、row_label，以及实际cell/header_path/row_headers/unit_evidence/footnotes。同行对象位于其他列时可用entity_cell_id/entity_value。跨行或跨列的数据格、缺失表头、错误单位倍率和符号不能凭数字相同放行。聚合来源的 `kind:'query_scope'` 只保存真实已执行query和表级来源，不能读作逐格证明。
- 机械单表和全库结果由执行器生成，不要求模型生成逐格binding；涉及表格结构时独立复核必须明确返回 `table_relations:[{id:'C1',supported:true}]`，成功结果在 `verification.table_relations` 保存该判断。缺项、重复或否决不等于通过。普通CSV/数据库没有文档表结构时保留既有复核行为。
- 已执行SQL但复核失败时，`claims`为空，正文不发布结果数值；`query`保留真实sql/parameters等，`query.executed_result:{columns,rows,display_rows?}` 保留未采纳的实际执行结果，`evidence`继续保存来源。此审计字段不等于完成态主结果，不能用于绕过CSV成功状态条件。
- 表格结构上下文的SQL总预算为16,000字符、每表4,000字符；目录结构计入原有目录预算，复核不重复携带相同目录结构。单文档16,000字符上下文与多源24,000字符证据预算包含新增表结构。完整关系容不下时保留明确不足，不能只留下数值、删除约束。
- 对新文档和多源回答，coverage缺失或格式无效时 `completeness.status` 为unknown/invalid，不能返回completed；只能保留已核验部分并返回依据不足。历史completed答案即使没有coverage也不改写或追溯重判。补救仍受既有次数、动作和调用预算约束；网络超时、取消及结果未知的POST不自动重发。
- parser2.1以前只存旧网格的资料，应先重新解析再用于新的表关系结论。重新解析后仍unknown/截断的结构继续拒绝确定结论。历史答案及其快照不迁移、不伪造现代结构；草案来源保留其发布状态，不可将建议、拟议任务改写成现行已实施事实。

第二批当时的后续安排是优先修复两道跨资料失败样例，该工作已在上方第三批增量中继续处理。第三批交付后暂停，后续图表证据与页面预处理等待用户再次明确说“继续”。

## 2026-10-17 第一批修复增量

以下记录第一批引入并继续保留的字段；与第二批规则有差异时以上方当前增量为准。HTTP 路径和原有前端保持不变，`/health` 的产品版本仍为 0.5.0；精确代码版本以文件清单摘要为准。

- `AnswerResult.trace?: {stage,elapsed_ms}[]` 独立保存执行摘要。`query` 仅用于有非空 SQL 的实际查询；非 SQL 的探查/修复信息移至 `query_diagnostics`。为兼容现有前端，真实查询仍可带 `query.trace` 别名。Markdown 只输出一份摘要。
- 读取历史记录时只做深拷贝投影，不改写数据库。旧 `query.trace` 可投影为 `trace`；旧 SQL 未保存有效 parameters 时，API 使用空数组防止旧界面报错，并设置 `parameters_available:false` 和明确说明，不能据空数组推断原查询无参数。JSON 导出继续保留脱敏后的原始保存结构。
- 复核拒绝并不等于用户缺条件。无有效业务澄清项、明确 `invalid_plan`、字段/计算错误等返回 failed；真正缺少业务条件才是 needs_clarification。解释修正后的顶层 normalized_question、intent 和 query.plan 同步。
- `model_attempts` 每次调用保存 requested_model、reported_model（服务未回报时为 null）、post_started、elapsed_ms 和最终 status。status 包括 completed、cancelled、content_json_failed、schema_failed、output_truncated、response_json_failed、http_failed、timeout、transport_failed、response_too_large、failed；不会把 response_received 当作完成终态。completed 仅表示网关得到有效 JSON 对象，不代表业务语义验证通过。models 展示字段仍兼容配置回退，不可拿它代替服务回报身份。
- `completeness` 使用 explicit-coverage-v1：`status` 为 assessed/unknown/invalid/conflict，`complete` 为 true/false/null，`items` 逐项保存 id、text、status、claim_ids、evidence_ids。分项按括号外的分号、问号或换行识别，分项内部并列要求由复核提示约束；它不是完整语义解析，也不是人工验收。缺少coverage明确记unknown/null；第二批起新回答的完整状态按上方规则执行，旧保存记录保持原样。
- 单源 `coverage_evidence` 仅保存覆盖检查实际引用的片段，记录 id、source_id、sha256、parser_version、page、start、end、quote 等。恢复前后合并，默认最多48片／32,000原文字符；这属于审计数据，不扩大每次16,000字符的检索上下文。多源 coverage 引用已持久化的 evidence。
- `completion_repair` 保存是否补救、动作（retrieve/rewrite）、前后覆盖结果、预算或终止原因；未采纳尝试的事实快照另行标注，不能作为已发布事实读取。Markdown/JSON 均保留覆盖与补检审计内容。当前前端未新增覆盖率面板；非 SQL 执行摘要可从导出读取。
- 文档回答最多一次补救、四次模型调用（生成/复核各至多两次）。多源补救共用既有一次语义修复预算、10动作/8轮/24,000字符证据限制，只在已选资料中补查，最多新增两次文档检索。不会另开预算或自动重发网络超时、取消及结果未知的 POST。
- 已核验部分事实必须保留；补稿未完整保留时回退原部分结果，冲突优先停止确定结论。补不到的项继续标为未解决，不能通过重复循环变成成功。

所有 API 同源 /api。成功直接返回下述对象/数组。错误返回 {detail:string}。时间为 ISO8601 UTC，ID 是字符串。导入和问答由后台任务执行，前端仅在有任务进行时轮询真实状态。

## Types

Project = {id,name,description,created_at,updated_at}
Source = {id,project_id,name,kind:'document'|'spreadsheet'|'database'|'image',format:string,bytes:number,sha256:string,status:'queued'|'processing'|'ready'|'attention'|'failed',stage:string,progress:number,summary:string,error:string|null,warnings:string[],page_count:number,table_count:number,row_count:number,created_at,updated_at,parser_version:string,archived:boolean}
Job = {id,project_id,source_id,source_name,status:'queued'|'processing'|'completed'|'attention'|'failed',stage:string,progress:number,message:string,created_at,updated_at}
Page = {number:number,title:string,text:string,char_count:number,has_image:boolean}
Table = {name:string,columns:{name:string,type:string}[],row_count:number,preview:unknown[][]}
Draft = {id,project_id,question:string,source_ids:string[],created_at,updated_at,archived:boolean}
SourceDetail = {source:Source,pages:Page[],tables:Table[],metadata:Record<string,unknown>}

补充：Source.usable:boolean 是统一的可提问准备标志，由后端计算：未归档、ready/attention 且确有非空文字或可用表格。纯图片和无文字扫描件即使attention也为false。已OCR出文字的图片可用；前端据此禁用无内容的资料并解释原因，后端仍独立校验。问题最大4000字符。

Source 另有 review_status:'pending'|'reviewed'、review_note:string、reviewed_at:string|null、reviewed_parser_version:string|null，以及 origin/source_group/derived_from:string|null。后三项仅取实际解析元数据，不猜测来源。核对状态不改变 status、warnings 或 usable。

## Endpoints

- GET /health -> {service:'zhiheng',version:'0.5.0',stage:5,workspace:string,pid:number}
- GET /projects -> Project[] (fresh instance has no projects)
- POST /projects {name,description?} -> Project
- PATCH /projects/{id} {name,description?} -> Project
- GET /projects/{id}/overview -> {project:Project,stats:{sources,ready,attention,failed,processing,documents,tables,rows},recent_sources:Source[],jobs:Job[],drafts:Draft[]}
- GET /projects/{id}/sources?archived=false -> Source[]
- POST /projects/{id}/import multipart field `files` (multiple) -> {sources:Source[],jobs:Job[],duplicates:Source[]}; same hash within active project deduplicates
- POST /projects/{id}/demo -> {sources:Source[],jobs:Job[],duplicates:Source[]}; import real bundled samples, no silent initialization. samples/manifest.json array describes name,file,origin,license,note; file is relative to samples.
- GET /sources/{id} -> SourceDetail
- GET /sources/{id}/tables/{urlencoded-name}?offset=0&limit=50 -> {name,columns:{name,type}[],rows:unknown[][],row_count:number,offset:number,limit:number}; table names must come from introspection, never execute arbitrary SQL
- GET /sources/{id}/original -> original download (inline optional query download=false)
- GET /sources/{id}/pages/{number}/image -> PNG preview for PDF/image; numbers 1-based
- POST /sources/{id}/retry -> {source:Source,job:Job}
- POST /sources/{id}/archive -> {ok:true}; soft removal only, preserve original
- POST /sources/{id}/restore -> {ok:true}
- PATCH /sources/{id}/review {status:'pending'|'reviewed',note:string} -> Source; note 必填且最多2000字符；仅ready/attention可标记reviewed，归档资料不可修改。重新处理清空核对状态、时间和版本，但保留备注。
- GET /projects/{id}/jobs -> Job[]
- GET /projects/{id}/drafts?archived=false -> Draft[]; true返回已归档问题
- POST /projects/{id}/drafts {question,source_ids:string[]} -> Draft; validate nonempty, source IDs belong to project and are usable (ready, attention allowed only with readable text/table; if no readable content reject). Saved preparation, never fabricated answers.
- PATCH /projects/{id}/drafts/{draft_id} {question,source_ids:string[]} -> Draft; 同创建校验，更新原记录；已归档问题不可编辑。
- POST /projects/{id}/drafts/{draft_id}/archive -> {ok:true}; 软归档
- POST /projects/{id}/drafts/{draft_id}/restore -> {ok:true}; 恢复问题原记录，不隐式丢弃已失效的资料范围；再次编辑保存时严格校验资料可用性。

## Import constraints

Support PDF, DOCX, TXT, MD, CSV, XLSX, PNG, JPG/JPEG, SQLite/DB. Unsupported input creates a visible failed item where safe; malformed supported input creates failed job with actionable error. Size limit 80 MB/file; bounded spreadsheet cells and PDF page handling. Stage 4 uses bounded local macOS Vision OCR; extraction failures and unprocessed ranges remain visible. OCR engine confidence is not an accuracy score. Native PDF text, DOCX paragraphs/tables, CSV/XLSX tables, SQLite schema/row counts must be real. Detect empty, missing text, encrypted, malformed; keep original. Pages that are not real physical pages (DOCX) are explicitly logical sections. Source `progress` is actual stage milestones, no fake timers.

## Frontend environment

Vite dev proxies /api to 127.0.0.1:8876. FastAPI serves frontend/dist. Theme: ref #5 top white/lavender/blue, ref #1 typography/order. Four navigation sections: 工作台、资料库、数据表、导入记录。回答与草稿分离；工作台执行单资料或最多8份资料的问题，App 层持续跟踪任务。引用跳转须匹配文件摘要和解析版本；过期引用仍显示回答时快照。

## Answer and model endpoints

- GET /model -> {configured,provider,endpoint,models:{table,document,verify},available_models:string[],error?:string}; no credential.
- PATCH /model {models:{table,document,verify}} -> same; every model must be in registered service catalog. Takes effect for new jobs.
- POST /model/test -> {ok:boolean,message:string,model:string}; performs one real call to the verify route only.
- POST /projects/{id}/answers {question,source_id,mode:'table'|'database'|'document'|'multi_source',source_ids?:string[],table_name?:string,request_id:UUID,parent_answer_id?:string} -> AnswerRecord; HTTP 202. All selected sources must belong to this project. Table mode requires an explicit table. UUID is idempotent per project, changed payload gives 409. At most 4 pending jobs, 2 concurrent workers.
- GET /projects/{id}/answers -> AnswerRecord[]; newest 50, pending and terminal records included.
- GET /projects/{id}/answers/{answer_id} -> AnswerRecord.
- POST /projects/{id}/answers/{answer_id}/cancel -> AnswerRecord; terminal outcomes cannot be overwritten by late model replies.

AnswerRecord = {id,project_id,request_id,question,source_id,source_name,source_sha256,parser_version,mode,table_name:string|null,status:'queued'|'running'|'completed'|'needs_clarification'|'insufficient_evidence'|'failed'|'cancelled',stage,created_at,updated_at,elapsed_ms,models:Record<string,string>,error:string|null,warnings:string[],result:AnswerResult|null}.

AnswerResult = {text,claims:{id,text,evidence_ids:string[]}[],columns?:string[],rows?:unknown[][],evidence:Evidence[],notes:string[],verification:{status:'passed'|'limited',checks:string[]},query?:{sql,parameters:unknown[]|Record<string,unknown>,plan?:object,limit?:number,matched_rows?:number,returned_rows?:number,result_rows_before_limit?:number,total_rows?:number|null,truncated:boolean,calculations?:object[]}}. Query字段依模式而异：单表可带plan、limit、matched_rows、returned_rows、result_rows_before_limit；全库查询使用returned_rows、total_rows；多源各次查询记录在steps与对应evidence中，不能要求存在单个顶层query。旧记录可缺失新增计数字段。

Evidence = {id,label,kind:'text'|'table',source_id,source_name,sha256,parser_version,stale:boolean,page?:number,page_label?:string,quote?:string,start?:number,end?:number,table_name?:string,row_numbers?:number[],columns?:string[],rows?:unknown[][],total_rows?:number|null}. Text offsets use Python Unicode code points within the parsed page. Table row numbers use the same stable row ordering as /tables pagination, not the filtered result order or a guessed spreadsheet line.

资料变化会阻止在途结果发布；历史记录保留快照并动态标记stale。重启将未完成问答标记中断，不自动重发可能已计费的模型请求；本地资料解析的queued/processing任务则恢复到解析队列。模型故障为failed，证据不足为insufficient_evidence，口径待明确为needs_clarification。模型产生的SQL必须经过AST检查、参数绑定及只读授权才能进入执行器。

## v4 增量（以本节及实际类型为准，兼容既有记录）

- 第四阶段当时的 `GET /health` 为 `version:'0.4.0',stage:4`；当前版本见上方 Endpoints。
- `Source` 增加 `content_revision` 及 `reprocess_available`。后者仅对未归档、可用状态且解析版本落后的资料为true；`retry`允许这些旧版ready资料原地升级，保留ID和原件。当前版本ready仍拒绝重复解析。
- `POST /projects/{id}/answers` 现接受 `mode:'table'|'database'|'document'|'multi_source'`、`source_ids?:string[]`、`parent_answer_id?:string`。保留 `source_id` 作为首份资料。多源范围1–8份，必须包含首份，不可重复，全部属于当前项目且可用；非多源模式只能一份。不存在返回404，范围冲突409，输入格式错误422。
- `GET /projects/{id}/answers/{answer_id}/conversation` 返回当前分支由早到晚的记录；最多30轮。父回答须是当前项目同范围同版本的completed/needs_clarification/insufficient_evidence。
- `AnswerRecord` 增加 `source_ids`、`sources:[{id,name,sha256,parser_version,revision}]`、`scope_stale`、`conversation_id`、`parent_answer_id`、`turn_index`、`normalized_question`、`intent`、`usage`、`model_attempts`。revision为内容及派生修订摘要，不泄露路径。任一资料重新解析/归档/原件变化会阻止在途发布；逐条证据独立标记stale。
- `AnswerResult` 增加 `display_rows`（保留原始rows）、`steps:[{id,title,kind,depends_on,evidence_ids,summary,query?,calculation?}]`、`calculations:[{id,expression,parameters,value,unit,display_value,checks,evidence_ids}]`、`source_groups:[{group_id,source_ids}]`。`display_value`不含单位。claims可含calculation_ids。失败结果不发布未复核计算值。
- 公式 `parameters` 为 `{name,value,unit,evidence_ids,binding}`；binding只能精确文本片段、已执行SQL结果行列或前一步计算。单位/规则来源保留。受控表达式不执行Python代码。
- 表证据可含 `query_result`、`query`、`original_columns`、`supporting_evidence_ids`；这是查询结果的来源范围，不冒充原表某行。图像核对通过后的文字证据可含 `visual_checked:true`；仍明确OCR原文不等于零误识别。
- 多源依据不足也须有实际检索和单独复核，通过时 `verification.negative_review:true`。`diagnostics`保留受限的工具错误和拒绝原因，不含密钥或请求头。旧document入口遇OCR页面使用新版引擎；旧table/database入口涉及OCR表时在模型调用前失败并提示改用多源分析，原单表范围不被静默放大。
- `Page` 增加 `extraction:'native'|'ocr'|'unreadable'`、`blocks:[{text,bbox,confidence,kind,start,end}]`、`quality`。bbox为原件预览坐标中的归一化 `[x0,y0,x1,y1]`；text offset仍为Unicode码点。OCR时旋转仅用于识别，原件不被旋转覆盖。
- `quality` 含对比度/清晰度/方向修正/倾斜角和warnings；confidence为引擎内部0–1评分，不能称准确率。metadata可含 `quality_summary`、`complexity`、`outline`、`table_regions`。复杂度来自明确布局/表格/公式候选规则，不生成任意“质量分”。
- `table_regions` 含 `{table_name,page,bbox,cells,row_count,column_count,data_row_offset,has_merged_cells,requires_review,extraction}`。cells保留row/col、bbox、rowspan/colspan/covered_by及识别分歧；row/col从1开始且包括表头，DOCX的bbox为null。合并覆盖格不重复填值，表行数据存入派生SQLite，不在区域元数据重复保存。

新导入PDF/图片尝试本地OCR；旧资料不在启动时自动重解析，用户可点“重新处理”。可信来源、同源归并和主项目数据保护不变。前端工作台现支持多资料，原文/表格对照与质量诊断按需展开。模型文本生成的SQL经AST白名单、SQLite只读授权及资源限制执行，不接受任意自由执行。

## v5 增量：保存记录导出

`GET /projects/{project_id}/answers/{answer_id}/export?format=markdown&scope=answer`

- `format`: `markdown|md|json|csv`，默认 markdown，md 为别名；`scope`: `answer|conversation`，默认 answer。
- Markdown/JSON 支持所有已结束状态；conversation 按祖先链从早到晚导出，最多30轮，不含兄弟或后续分支。未结束、祖先缺失或循环返回409；项目/回答不匹配404，参数不支持422，错误体仍为 `{detail:string}`。
- CSV 仅支持本轮 completed、当前资料未过期且顶层存在非空 columns 数组与 rows 数组（允许为空）。实际空表允许仅表头并标记0行；无主结果表、会话范围或非成功状态返回409。多源步骤表保留在Markdown/JSON，不拼造CSV。
- 成功响应为文件字节，`Content-Disposition` 含ASCII fallback与UTF-8中文 `filename*`。MD为 `text/markdown`，JSON为 `application/json`，CSV为带BOM的 `text/csv`；UTF-8，无缓存。前端按blob下载，不把CSV大整数/小数字符串转成JavaScript Number。
- CSV编码名为 `zhiheng-safe-csv-v1`。NULL写作 `\N`，文本反斜杠加倍；公式前缀、前导单引号、数字字符串、所有浮点数repr、超过15位的整数前加保护单引号。先识别NULL，再去保护单引号并还原文本反斜杠；类型以JSON为准。表格软件的自动类型推断不构成精度保证。NaN/Infinity等无法可靠表示的旧记录在三种格式中均返回409，不输出部分文件。
- 所有格式响应头含 `X-Export-Snapshot:saved-record-v1`、`X-Export-Scope-Stale`、`X-Export-Redaction-Count`。CSV另含 `X-Export-Row-Count`、`X-Export-Truncated`、`X-Export-Limit-Applied`、`X-Export-CSV-Encoding`；`X-Export-Warnings` 是percent-encoded的编码、限制与脱敏说明。文件名含返回行数和适用时的截断/查询上限说明。

JSON根对象为 `{schema_version:'zhiheng.answer-export.v1',exported_at,scope,selected_answer_id,project:{id,name},boundary,snapshot_hash_definition,csv_policy,answers:AnswerRecord[],record_metadata,redactions}`。`record_metadata[answer_id]` 保存原数据库body UTF-8字节摘要与当前版本核对结果 `freshness:{scope_stale,sources,legacy_revision_notice}`。`answers` 保留历史字段，不覆盖其旧stale值；导出时版本以freshness为准。路径/连接凭据按字段与已知文本模式隐去，redactions列出变化，因此脱敏导出内容不能直接与原body摘要比对。

导出只读同一数据库快照，不启动恢复、不重跑查询或模型。它是回答记录，不含完整原件。当前资料移除、归档、不可用、SHA/parser/revision不符时，Markdown/JSON标记过期，CSV返回409。旧记录无revision时仅核对已保存SHA和parser；不重新计算原件摘要、不补造历史字段。Markdown转义HTML/链接语法，代码围栏按内容长度适配。完整约定见 [导出说明](第五阶段验收/导出.md)。

## v5 增量：单表查询计数与运行恢复

单表 `AnswerResult.query` 新增下列计数；历史记录可缺失，不以其他字段猜填：

| 字段 | 含义 |
| --- | --- |
| limit | 本次经校验的查询返回上限，最多100；可对应用户TopN要求 |
| returned_rows | 本次实际保存的结果行数 |
| result_rows_before_limit | 应用limit前的结果行数；分组后按组计数，比例计算为1行 |
| matched_rows | 匹配筛选条件的原始记录数，不是分组后的结果数 |

旧单表 `truncated` 仍表示应用limit后省略了结果，不能直接解释为没有完整回答问题。例如匹配100条原始记录、分成16组、用户要求Top3时，四个字段可分别为3、3、16、100，答复是否满足问题需按口径判断。单表导出显示“查询上限/已返回/限制前结果”，CSV用 `X-Export-Limit-Applied`；全库/多源SQL的系统结果截断继续明确标记，不混淆语义。

启动器核对health中的workspace和操作系统进程身份；启动/停止操作加锁，后端还为运行目录持有独占锁，防止同一运行数据被并发实例操作。停止确认真实进程退出，必要时结束该实例及其解析子进程，不以监听端口关闭代替退出确认，不停止其他目录服务。

启动时未完成的本地解析重新排队；已完成资料不自动重解析。上次仍queued/running的问答标记failed/处理中断，不自动重发已可能发送的模型POST。主动取消或已有终态不被迟到响应覆盖；同一request_id仍返回保存记录。再次提问须使用新请求编号。第五阶段全量测试记录为410项通过、39.88秒；这不等于真实模型正确率。


## 第四批图表与页面质量扩展

解析版本为 `zhiheng-multimodal-2.2`。页面 `quality.preprocessing` 保留触发原因、最多一次候选、额外识别次数、耗时、选择结果、文字与数值符号差异及截断标记；`visual_review_required` 表示必须核对原页。增强文字只作候选，有差异时不覆盖原识别。

证据可附 `chart_context`（原页观测ID、文字、坐标、置信/待核对状态及范围、截断）；事实可附 `chart_bindings`（类别、印刷值、单位、系列、时期及原观测锚点），验证字段可含 `chart_relations`。JSON和Markdown导出保留图表依据。通过本地绑定仍须查看对应原页并独立复核。

混合文字与图像的PDF在明确图表问题中可按需读取图内文字：每次请求最多三页图像，局部OCR共享35秒剩余预算，原生正文和偏移不变。没有印刷数字、标签歧义或低置信观测时保持有限结论/依据不足，不由视觉高度生成精确值。以上是工程预算，不是比赛官方评分阈值。
