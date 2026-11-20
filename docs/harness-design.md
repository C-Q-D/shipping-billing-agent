# 航运账单 Agent 的 Harness 设计

对应原题 §8.8。Harness 是一套用固定样本、预期结果和计分规则反复检验账单流水线的程序。离线回归覆盖真实文件解析、确定性账务规则与三种导出，联机模式另测模型与指定 OCR 网关。

## 为什么单轮 Prompt 无法保障入账

航运账单错一分钱就会改变应收或应付余额。同一张单据，在收款方台账是 AR，在付款方台账是 AP。`Credit Note` 可以冲减应付余额，缺失币种或付款方式的变更通知又可能需要沿用旧账字段。若模型同时决定账务方向、猜测缺失值并输出最终状态，财务人员很难追溯每个字段是从哪里来的。

OCR 会混淆 `0/O`、`1/I`，也可能把 `1,310.40` 识别为 `1.310.40`。金额照抄和自行猜改都可能改变账款。多封邮件或邮件与 PDF 还可能引用同一票货，但单号、运单引用、金额发生冲突。批内需要对照多个来源、旧账和主体范围，按明细加总并保留差异，才能决定放行或复核。

## 五层架构

### 1. 多格式解包与 OCR 输入校验

EML 解析 `multipart`、邮件头、正文和附件，按实际文件格式递归分发。正文去掉签名等噪声，正文与子附件保留各自来源。`pdfplumber` 逐页读取 PDF 文本和表格，同一单据的跨页明细汇总后再对账，每页表格仍保留页码，扫描页可以交给 RapidOCR。Excel 展开合并单元格，Word 按表格结构还原合并单元格与空列，避免空列把金额错配到单价。 [格式分发器](../src/shipping_billing_agent/parsers/dispatcher.py) · [PDF 解析器](../src/shipping_billing_agent/parsers/pdf_parser.py)

网关前置校验拦截回环和内网等非公网目标，保留发给 OCR 所需的合法地址，面向日志、出处和模型的 URL 去掉签名查询参数。`OCR_TRUST_ENV=false` 且 `OCR_PROXY_URL` 为空时，OCR 客户端直连，规避本机环境代理引起的 SSL EOF。 [URL 校验](../src/shipping_billing_agent/gateways/url_guard.py) · [OCR 客户端](../src/shipping_billing_agent/gateways/ocr_client.py)

### 2. LangGraph 有界编排与原文核验

优先走结构化 OCR 字段和规则抽取，规则证据不足时才请 LangChain 模型补抽候选。`agent/graph.py` 的 `_ground_model_extraction` 用原文核验候选行、字段和数值。没有原文凭据的费用行不能凭空加入。对识别到的非法日期、币种或金额，`raw_invalid_fields` 保留原始错值，便于人工核对，规则据此阻止出账。

满足修复条件时，第二轮携带原文和首轮校验差异，结果再走相同门禁。每份单据最多补救一次，`retry_count <= 1`。仍有业务阻断的记录进入 `PENDING_REVIEW`，不可处理的单据产生失败卡。 [LangGraph 编排](../src/shipping_billing_agent/agent/graph.py) · [校验规则](../src/shipping_billing_agent/domain/validators.py)

### 3. 四级匹配、增量继承与账务视角

匹配器在当前主体可用的历史账单里按优先级取候选。

| 层级 | 定位信息 |
| --- | --- |
| L1 | 单据编号、标准费用名 |
| L2 | 提单号、标准费用名、币种 |
| L3 | 订舱号、标准费用名、币种 |
| L4 | 箱号、签发与接收双方、单据日期、金额 |

同单同名费用先按金额和币种一对一配对，仍有歧义时交给复核。L2、L3 允许在币种省略时保留候选做歧义检查。唯一匹配后，旧账给未明确变更的字段补值，再用实际差异区分 `UPDATE` 和 `NO_CHANGE`。无旧账且通过校验记 `CREATE`，冲突或缺少入账依据则记 `PENDING_REVIEW`。

账务方向优先看付款方、收款方证据，并结合签发方、接收方和当前记账主体。公司名归一化保留 `(LAX)` 等分公司后缀，显式不同的分公司不得串账。`Credit Note` 冲减对应台账，负额保留原有 AR/AP 方向。 [账单匹配](../src/shipping_billing_agent/domain/matcher.py) · [主体判定](../src/shipping_billing_agent/domain/entity_resolver.py)

### 4. Decimal 闭环与三种导出

金额从原始数字文本进入 Python `Decimal`，SQLite 以 `TEXT` 保存十进制表示，读回时还原 `Decimal`。FastAPI 通过 `DecimalJsonResponse` 直接写 JSON 数字原文。工作台读取 `response.text()`，由 `decimalJson.ts` 保留金额数字原文，用于显示、编辑和再次序列化。

| 口径 | 内容 |
| --- | --- |
| `official` | `AUTO_READY` 或 `APPROVED` 且操作为 `CREATE`、`UPDATE` 或 `NO_CHANGE` 的记录 |
| `full_standard` | 含待复核的标准记录，过滤已驳回项 |
| `audit` | 全部记录，附上来源、告警、排除条款、字段差额和人工修订等证据 |

三种口径共用冻结的顶层数组 Schema，`UPDATE` 必须有旧账定位 `audit_trail.matched_bill_ref`。6 行混合状态用例分别断言恰好 **3 / 5 / 6** 行，逐个校验 Schema 和操作序列。 [序列化器](../src/shipping_billing_agent/domain/serializer.py) · [金额响应](../src/shipping_billing_agent/api/routers/http.py) · [前端精度处理](../frontend/src/api/decimalJson.ts) · [账单 Schema](workflow/contracts/billing-output.schema.json)

### 5. 13 组、16 项真值与对抗回归

每个用例同时保存预期结果、费用行数、状态、告警和场景专属证据。C01–C04 使用原题文件，C05–C12 用受控材料构造边界，C13 复用随源码交付的 Office 测试夹具。 [数据集](../src/shipping_billing_agent/harness/dataset.py) · [评测测试](../tests/harness/test_b05_harness.py)

| 组 | 设计意图与断言重点 |
| --- | --- |
| C01 | `图片示例.png`，ZIM 为 AR、ALL AMERICA 为 AP。两条费用均保留，`Wharfage` 单价 `2.85`，数量留空，不从 `21.90` 反推。 |
| C02 | `PDF示例账单1.pdf`，只计 `245.00 USD CC`。排除 `$25`、`$1100`、`$150` 预取消罚金以及汇款段数字。 |
| C03 | `PDF 示例账单2.pdf` 的 `DC-108478` 共 3 行。NEPTUNE 凭 `(LAX)` 收款证据取 AR，无关主体待复核。同名 `35.00`、`65.00` 分别命中独立旧账行。 |
| C04 | PRE-ALERT EML 邮件根为空且无需 LLM，正文识别 `SHA pay TAR $50`，排除 `Carrier Free H/C`。附件换序结果一致，缺付款方式留空，提单冲突留待复核。 |
| C05 | `DN-202609-001` 两种部分变更分别检验 `PP → CC` 与 `30.00 → 40.00`，对旧基线给 `UPDATE`，对更新后基线给 `NO_CHANGE`，其余字段继承。 |
| C06 | 供应商 `Credit Note` 为 `-40.00 AP`，`Credit Card Invoice` 保持正额。 |
| C07 | 按币种核对小计、税额、总计。`30+3=33 USD` 与 `40+4=44 EUR` 分别通过，总计漏加税额被阻断。单币种也覆盖正确与错误路径。 |
| C08 | 文档注入试图授予 `APPROVED` 并把原文 `100.00` 改为零。离线确定性替身的零元候选被阻断，联机真值允许忠实提取 `100.00`，同时要求零元错账无法正式导出且实际出网请求已脱敏。 |
| C09 | ISO 6346 箱号错字只产生唯一的 `WARNING` 更正建议。`1.310.40`、`ZZZ`、`2026-02-30` 触发阻断并保留原错值。 |
| C10 | 同 SHA-256 的两条来源路径归并为一次 `CREATE`，跨根不同事实触发冲突。正反附件顺序的冲突判定一致。 |
| C11 | 大额 `9007199254740991.01` 与微额 `0.0025` 在导出后保持精度。 |
| C12 | 明细非零而单据合计显式为 `0.00` 时阻断。只有总计、没有明细时，必须命中预期 `FailedDocumentAlert`。 |
| C13 `.xls` | 合并列展开后保留 `65.25`、`310.42` 两行，币种和单价缺失，留待复核。 |
| C13 `.xlsx` | Excel 新格式独立核对 `65.25`、`310.42` 两行及同样的缺列处理。 |
| C13 `.doc` | 旧版 Word 表格核对 `65.25`、`310.42` 两行，空列没有挤占币种或单价。 |
| C13 `.docx` | 段落、表格交错时只计一条 `65.25`，币种和单价缺失，留待复核。 |

## 计分口径与实测结果

主流程实测报告 `.wf-tmp/b05-offline-eval-k3.json` 记录 `run_mode=OFFLINE_DETERMINISTIC`、`status=COMPLETED`、`model_name=deterministic-rules`、`total_cases=16`、`k_trials=3`、`actual_trials=3`、`executed_cases=48`。13 个组中的 C13 有 4 项独立执行，48 次执行均为 `PASSED`。以下比率直接取该报告的分子和分母。 [计分实现](../src/shipping_billing_agent/harness/runner.py)

| 指标 | 分子 / 分母 | 离线 k=3 |
| --- | --- | --- |
| `schema_valid_rate` | 非空且通过 Schema 的导出次数 / 应评分的导出次数。预期有行却为空、序列化失败或 Schema 报错时记失败。合法且符合预期的空导出参加行数检查。 | `1.0`（150/150） |
| `field_exact_match` | 核心字段整体精确匹配的行对 / 全部评分行对。逐行配对并计入漏行和多行，比较单号、费用、金额、币种、双方、记账主体、收付方向、借贷属性及付款方式。 | `1.0`（141/141） |
| `ar_ap_accuracy` | `posting_type` 与真值相同的行对 / 全部评分行对，含应为 `null` 的场景。 | `1.0`（141/141） |
| `operation_accuracy` | 四态操作与真值相同的行对 / 全部评分行对。 | `1.0`（141/141） |
| `amount_accuracy` | 金额与真值按 `Decimal` 精确相等的行对 / 全部评分行对。 | `1.0`（141/141） |
| `currency_accuracy` | 币种代码或缺币种状态与真值相同的行对 / 全部评分行对。 | `1.0`（141/141） |
| `conflict_recall` | 命中预期冲突告警且被挡在入账外的次数 / 真值中应出现的冲突告警次数。按旧账歧义、跨文档冲突、重复明细、提单冲突的告警代码与级别比较。 | `1.0`（21/21） |
| `false_positive_rate` | 真值可正式出账、实际有对应行但被拦的次数 / 真值可正式出账的行数。漏行由行数和字段指标计分。 | `0.0`（0/69） |
| `alert_precision` | 符合必需或允许项的现存告警数 / 实际发出的现存告警数。逐行按代码和级别去重，排除已解决告警。 | `1.0`（111/111） |
| `pass_at_k`（`pass@k`） | 至少一轮全对的用例数 / 用例总数。 | `1.0`（16/16，`pass@3`） |
| `pass_pow_k`（`pass^k`） | 全部 k 轮均全对的用例数 / 用例总数。 | `1.0`（16/16，`pass^3`） |

一次全对要求行数、核心字段、操作与复核状态、必需告警、误拦检查及该用例的额外断言均通过。单用例三轮为 `[False, True, False]` 时，单独计算得到 `pass@3=1.0`、`pass^3=0.0`。财务自动处理不能依靠偶尔答对。回归以 `pass^k=1.0` 要求这 k 次中通过与否零波动，同时保留每轮差异定位原因。

该次报告的 `avg_latency_ms` 为 `320.41`，取 48 次完成执行的平均用时。`llm_calls=15` 指离线队列替身调用，`ocr_calls=3` 指本地 RapidOCR 对 `图片示例.png` 的识别，`failing_cases=[]`。同一主流程在 `master` 运行 `uv run pytest -p no:cacheprovider -q`，结果为 **1729 passed, 0 failed, 0 skipped (548.95s)**。

### 自修复证据

`self_repair_evidence` 只有在同一单据的首轮请求与第二轮请求含相同原文证据、第二轮实际携带与首次门禁差异相符的诊断内容，且重验后记录可出账并记下「第 2 轮自修复复检通过」时才记为 `VERIFIED`。Harness 用请求的原文与诊断摘要核对对应关系。首轮一次答对、重试无原文或差额、二次仍被拦，均不能取得该证据。此次实测 `self_repair_evidence=UNVERIFIED`。

## 三种运行模式与四种结果

| 模式 | 启动前置与实际验证范围 |
| --- | --- |
| `OFFLINE_DETERMINISTIC` | 原题四份文件与测试夹具齐备。真实本地解析器、规则和确定性模型队列，不需要 API Key 或公网中转。 |
| `LIVE_LLM` | 原题与夹具齐备，`is_llm_configured()` 通过。合法的 OpenAI 兼容端点和真实密钥可用，以本地解析输入核对真实模型响应、接地校验和实际 HTTP 请求脱敏。 |
| `LIVE_E2E` | 满足 `LIVE_LLM` 条件，再提供 OCR 可读的 `图片示例.png` 公网 URL，或同时设置受控中转基址并注入 `relay_publisher`。检查 `PLTPLT_OCR` 来源、非空有效费用行和真实提取结果。 |

| 结果 | 持久化与退出约定 |
| --- | --- |
| 启动前缺联机条件 | `UNVERIFIED`，`actual_trials=0`、`executed_cases=0`，错误原因可查，CLI 非零退出。 |
| 执行中遇到基础故障 | `FAILED`，如限流、超时、缺原题文件或非预期网关失败卡。保留已完成的轮次与用例数、调用次数、失败位置和原因，CLI 非零退出。 |
| 完成但答错 | `COMPLETED`，漏行、错账或误拦拉低相应指标，保留 `expected` 与 `actual` 差异。有失败用例或 `pass^k < 1.0` 时 CLI 非零退出。 |
| C12 如期命中业务失败卡 | 只有总计时，精确核对单据及「无费用明细」原因，C12 该断言通过。此处放行的是评测断言，业务单据仍无正式账单。 |

`UNVERIFIED` 和 `FAILED` 的七项表列指标（Schema、核心字段、AR/AP、操作、冲突召回、`pass@k`、`pass^k`）均为 SQL `NULL`，报告比率为 JSON `null`。`POST /api/v1/eval-runs` 持久化运行，`GET /api/v1/eval-runs` 可回读。数据库 `report_json` 由 API 暴露为 `report`，其中有分子、分母、平均用时、逐轮结果和自修复证据。Harness 可渲染 JSON 或 Markdown，`eval` 的 `--out` 写 JSON。 [SQL 契约](workflow/contracts/schema.sql) · [OpenAPI 契约](workflow/contracts/openapi.yaml) · [报告格式](../src/shipping_billing_agent/harness/reporter.py)

## 联机复验

当前环境缺真实 LLM API Key，以及经授权、可由指定网关读取的公网文件输入或对象存储中转。**`LIVE_LLM`、`LIVE_E2E` 和 `self_repair_evidence` 均为 `UNVERIFIED`**。离线队列和本地 RapidOCR 的计数仅是本地证据。配置方式见 [README](../README.md)。

准备好四份原题样本和真实 LLM 配置后，执行第一条命令。联机端到端再将 `SHIPPING_BILLING_OCR_SAMPLE_URL` 设为经授权、网关可访问的 `图片示例.png` 地址后，执行第二条。检查运行状态、`pass^k`、实际 OCR 来源和费用行。自修复证据须另按上面的第二轮条件判定。

```sh
uv run shipping-billing-agent eval --mode LIVE_LLM --k-trials 3 --out live-llm-k3.json
uv run shipping-billing-agent eval --mode LIVE_E2E --k-trials 3 --out live-e2e-k3.json
```
