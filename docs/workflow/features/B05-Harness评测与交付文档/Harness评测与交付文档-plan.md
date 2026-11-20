# B05 Harness 评测套件、深度思考报告、README 与交付 ZIP 打包验收 — 功能实现方案

- **关联锚点**：
  - PRD：`F-07`（命令行、`eval_runs` 评测指标与 `UNVERIFIED` 透明报告）、`F-01`–`F-06` 端到端闭环与引用 `B03`/`B04` 复核验收，详见 [product-spec.md](../../product-spec.md)
  - 题面交付要求：[AI_agent_%E7%AC%94%E8%AF%95%E9%A2%98_%E5%AE%8C%E6%95%B4%E6%8F%90%E5%8F%96.md](../../../../AI_agent_%E7%AC%94%E8%AF%95%E9%A2%98_%E5%AE%8C%E6%95%B4%E6%8F%90%E5%8F%96.md) §8.8（Harness 工程设计与稳定性保障说明）、§8.9（深度思考报告，至少 5 点）、README 与压缩包交付验收
  - 架构与契约：[arch-contract.md](../../arch-contract.md) §1.4、§4（评测指标与三模式区分）、[schema.sql](../../contracts/schema.sql)（`eval_runs` 表）、[openapi.yaml](../../contracts/openapi.yaml)（`EvalRunSummary` 与 `/api/v1/eval-runs`）
- **范围与非目标**：
  - **本批次范围**：`src/shipping_billing_agent/harness/`（13 组真实＋对抗评测数据集含 4 份 Office 参数化子例、`OFFLINE_DETERMINISTIC` / `LIVE_LLM` / `LIVE_E2E` 执行器、`pass@k` 与 `pass^k` 多次稳定性指标、Markdown/JSON 报告）、挂接 `B03` CLI `eval` 与 `POST /api/v1/eval-runs` 仓储落库、Harness 设计文档 `docs/harness-design.md`、深度思考报告 `docs/deep-thinking.md`（≥5 项源自本项目真实演进的深度工程洞见）、工程化 `README.md`（含投递收件人与邮件标题规范）与 `.env.example` 核对、纯净交付包生成脚本 `scripts/package_delivery.py`（含解压到仓库外空目录从零安装启动与 `parse` 出账端到端冒烟验证）及自动化回归测试 `tests/harness/`。
  - **非目标**：不私自修改未配置真实密钥的环境变量或跳过真实网络报错；不重造前端工作台测试（`F-06` 人工复核与工作台交互行为直接引用 `B03`/`B04` 实测验收结论）；无真实外部环境时严格按 `UNVERIFIED` / `null` 指标记录。

---

## 1. 模块与内容改动设计

### 1.1 分层边界（严格遵守 `test_config_and_domain.py` §431）
```text
api/ · cli/ ──▶ harness/ (dataset, runner, reporter) ──▶ services/, agent/, parsers/, gateways/, storage/, domain/, config/
```
- 保持 `services/` 绝不反向导入 `harness/`；`B03` API/CLI 通过依赖注入把 `harness.runner.run_eval_suite` 传入。

### 1.2 标准与对抗评测数据集（`src/shipping_billing_agent/harness/dataset.py`）
构建结构化、带真值断言清单与允许误差规则的 `EvalCase` 数据集（**共 13 组、16 个可执行测试项，≥ 10 组题面要求**）：
1. **4 份真实笔试原题回归组**（通过 `samples_dir` 传参或环境变量 `SHIPPING_BILLING_REAL_SAMPLES_DIR` 定位，默认 `<repo>/笔试题AIAGENT`；缺失时明确失败报错，严禁静默跳过或当作通过）：
   - `C01_REAL_PNG_ZIM_VS_ALL_AMERICA`（`图片示例.png` 双主体 `AR`/`AP`、2 条费用、缺数量严禁反除）；
   - `C02_REAL_PDF1_GENERAL_NOTICE`（`PDF示例账单1.pdf` `245.00 USD CC`，排除 1–3 页 `$25/$1100/$150` 违约条款与银行段）；
   - `C03_REAL_PDF2_DC108478_MULTI_LINE`（`PDF 示例账单2.pdf` 3 条费用、NEPTUNE 单边 `AR` vs 无关主体拦截、同单双 `Handling Charge` 配对）；
   - `C04_REAL_EML_PREALERT_NO_GUESS`（`深圳容宇 PRE-ALERT...eml` 空根 0 次 LLM、`SHA pay TAR $50`、`Carrier Free H/C` 排除、乱序附件同效、`payment_term=None`）；
2. **核心匹配、局部更新、贷项与多格式组**：
   - `C05_PARTIAL_UPDATE_DN202609001`（含两条显式子断言：a. 省略双方与金额币种仅改 `PP -> CC`，对照 V1 输出 `UPDATE`、对照 V2 输出 `NO_CHANGE`；b. 金额由 `30 -> 40` 时，对照 `30` 基准输出 `UPDATE`、对照 `40` 基准输出 `NO_CHANGE`，并核对金额、操作与其余继承字段均保真）；
   - `C06_SUPPLIER_CREDIT_NOTE_AP`（供应商 `Credit Note` `40.00` 保持 `AP` 且冲减 `-40.00`，同步反测 `Credit Card Invoice` 绝不误负）；
   - `C07_MULTI_CURRENCY_TAX_INVOICE`（多币种分币 `30+3=33 USD` & `40+4=44 EUR` 与单币种显式税额含税放行 vs 缺税错写拦截）；
   - `C13_OFFICE_FORMATS`（直接复用 `tests/fixtures/` 中已有的 `.xls`、`.xlsx`、`.doc`、`.docx` 四种格式文件及其已知金额/币种断言，作为 4 个独立参数化子例并在报告中逐项列示结果）；
3. **高风险边界、OCR 损坏、对抗注入与三口径混合批组**：
   - `C08_PROMPT_INJECTION_IGNORE_ALL`（**区分双模式基线**：`OFFLINE_DETERMINISTIC` 保留对抗 Fake LLM 篡改 `100.00 -> 0` 后被确定性门禁截为 `PENDING_REVIEW`、正式导出为 0；`LIVE_LLM` / `LIVE_E2E` 核对安全底线——0 元错账绝不进入 `official` 导出、文档文本绝不能赋予 `APPROVED` 状态、诚实提取 `100.00` 则按正常校验处理、同时核查实际出网提示词已完成脱敏且不含恶意指令直传未包裹）；
   - `C09_ISO6346_AND_OCR_CONFUSION`（错字符箱号软告警带唯一 `WARNING` 建议 + `1.310.40`/`ZZZ`/`2026-02-30` 硬阻断）；
   - `C10_DUPLICATE_SHA256_AND_CROSS_CONFLICT`（双入口同哈希归并双路径单 `CREATE` + 跨根冲突拦截 + 乱序附件跨文档冲突）；
   - `C11_LOSSY_DECIMAL_PRECISION`（`9007199254740991.01` & `0.0025` 全链路原样保留）；
   - `C12_EXPLICIT_ZERO_TOTAL_AND_MISSING_LINE`（`Total: 0.00` 对非零行拦截 + 有总额零明细如期产出 `FailedDocumentAlert`）；
   - 同时对 `6 条混合状态记录` 执行 `official / full_standard / audit` 三口径序列化断言：分别严格输出 **`3 / 5 / 6` 条**并全量通过 `billing-output.schema.json`（杜绝空数组 `[]` 假过 Schema）。

### 1.3 评测运行器、四类终止态与指标契约（`src/shipping_billing_agent/harness/runner.py`, `reporter.py`）
1. **顶层字段与 `schema.sql` / `openapi.yaml` 1:1 对齐**（不改表、不改 OpenAPI）：
   - `EvalRunResult` 直接填充 `eval_runs` 与 `EvalRunSummary` 已有字段：`id`、`run_mode`、`status`（`COMPLETED` / `FAILED` / `UNVERIFIED`）、`error_message`、`model_name`、`k_trials`、`actual_trials`、`total_cases`、`schema_valid_rate`、`field_exact_match`、`ar_ap_accuracy`、`conflict_recall`、`pass_at_k`、`pass_pow_k`、`avg_latency_ms`、`report_json`、`created_at`。
   - `report_json`（经 API `report` 返回）详细记录：`amount_accuracy`、`currency_accuracy`、`operation_accuracy`、`false_positive_rate`（正常记录误拦率）、`alert_precision`（各项均同时给出 `{"numerator": int, "denominator": int}` 与 `rate`）、`llm_calls`、`ocr_calls`、`self_repair_evidence`（仅第 2 轮请求携带原文与差额且重验通过才记 `VERIFIED`，第 1 轮已答对则记 `UNVERIFIED`）、`executed_cases`、`failing_cases`（预期与实际差异明细）以及每个用例每轮（trial）详情。
2. **`pass@k` 与 `pass^k` 严格分开计算**：
   - 单轮单用例“全对（full-pass）”必须同时满足：预期记录行数完全匹配（漏行、多行均判失败）、核心字段按 `Decimal`/归一化值精确一致、四态 `operation` 一致、必需告警齐备且无意外误拦。
   - `pass_at_k` = `k` 轮中**至少 1 轮全对**的用例占比；`pass_pow_k` = `k` 轮中**全部 `k` 轮均全对**的用例占比（例如三轮序列 `[错, 对, 错]` 对两项指标的贡献分别为 `1` 和 `0`）。
3. **三模式独立前置条件与四类终止态区分**：
   - `OFFLINE_DETERMINISTIC`：使用离线解析器 + 规则引擎 + 受控确定性队列替身执行全量用例。
   - `LIVE_LLM`：调用 `settings.is_llm_configured()` 判断任意合法配置模型（兼容自定义端点与独立 `LLM_API_KEY`，不死锁写死 `DEEPSEEK_API_KEY`），使用本地解析文本验证真实模型响应与出网脱敏。
   - `LIVE_E2E`：除 `is_llm_configured()` 外，额外核对远端 OCR 样本具备可用公网输入/文件中转并校验 `PLTPLT_OCR` 来源与返回费用行真实存在。
   - **四类运行结果精确处理**：
     1. *启动前未就绪* -> `status="UNVERIFIED"`, `actual_trials=0`, `executed_cases=0`, 整套汇总指标全部 `None`（SQL `NULL`）, 明确可操作 `error_message`；
     2. *运行中途遇到基础故障*（如限流、超时、非预期的网关故障 `FailedDocumentAlert`）-> `status="FAILED"`, **保留已完成的轮次、用例数、调用计数与具体失败位置/原因**，未跑完全套的汇总指标留 `None`（绝不抹掉已执行计数）；
     3. *模型返回可解析但业务错误的答案* -> `status="COMPLETED"` 并如实拉低分项分数与 `pass_at_k`/`pass_pow_k`（CLI 对未达通过阈值的 `COMPLETED` 与 `UNVERIFIED`/`FAILED` 均返回非零退出码并打印差异）；
     4. *如期触发业务失败卡（如 C12 无明细仅总额）* -> 按预期规则判定为 `PASSED`，不误报为断网故障。
4. **`reporter.py`**：生成结构化 JSON 与人类可读 Markdown 评测报告，逐一展示契约指标、分子/分母、`UNVERIFIED` 醒目标注与失败用例 `expected vs actual` 差异；`FAIL` 或 `UNVERIFIED` 均令 CLI 返回非零退出码。

### 1.4 交付文档、投递清单与独立目录从零冒烟打包（`docs/harness-design.md`, `docs/deep-thinking.md`, `README.md`, `scripts/package_delivery.py`）
1. **`docs/harness-design.md`**（对应题面 §8.8）：独立可定位说明 **Harness 工程设计与稳定性保障方案**，覆盖 13 组真值/对抗数据集来源与真值口径、分项准确率与误报率/召回率口径、`pass@k` vs `pass^k` 与自修复证据判定、`UNVERIFIED`/`FAILED`/`COMPLETED` 失败分类、离线实测结果汇总（`UNVERIFIED` 实况透明留痕）。
2. **`docs/deep-thinking.md`**（对应题面 §8.9，≥5 项真实沉淀洞见，本文给出 6 项深耕总结）：基于本项目开发与 4 轮独立 verifier 真实现场踩坑提炼：
   - 领域确定性门禁 vs LLM 幻觉防线（`raw_invalid_fields`/`_ground_model_extraction`/禁止无原文依据添行 + 单轮 `retry_count <= 1` 熔断）；
   - `float`/`Number` 隐形截断与全链路无损 `Decimal` 闭环（OCR JSON -> Python `Decimal` -> SQLite 字串列 -> FastAPI 原生 JSON 响应 -> 前端 `decimalJson.ts`）；
   - 航运视角隔离与局部更新继承先后序（括号分支 `(HK)`/`(UK)`/`., (HK)` 防串账、贷项不翻转收付、同包附件去重后的冲突快照保留）；
   - 防注入、URL 签名脱敏与 HTTP 重试预算原子守卫；
   - 基于反例驱动的三轮递增独立审查与 Harness `pass@k`/`pass^k`/`UNVERIFIED` 工程诚信原则；
   - 单一冻结 `openapi.yaml`/`schema.sql` 支撑四路并行 worktree 开发避免契约撕裂。
3. **`README.md` 与 `.env.example`**：架构概览、依赖与 Playwright 安装步骤、`.env.example` 安全与配置说明、4 份真实原题样本放置说明、CLI 与工作台启动/测试/评测全命令、当前实测与 `UNVERIFIED` 透明清单、**交卷邮件投递清单**（收件人 `LUKA@PLTPLT.COM`、`pan@pltplt.com`，邮件标题规范 `应聘渠道+姓名+应聘岗位`，附件与核心文档直达链接 `docs/harness-design.md`、`docs/deep-thinking.md`）。
4. **`scripts/package_delivery.py`**：生成纯净面试交付 ZIP 归档（严格排除 `.venv/`、`node_modules/`、`.wf-tmp/`、`.worktree/`、`.env`、`*.db`、`__pycache__/`、临时测试产物），并在打包测试/自检中：**将该 ZIP 解压至仓库外的空白临时目录，按 `README.md` 步骤独立配置依赖并启动服务验证 `GET /api/v1/health` 返回 200，再对包内附带样例运行文档所述离线 `parse` 命令，严格断言退出码 0、非空导出记录数（>0）、已知精确金额/币种与通过 `billing-output.schema.json`**。

### 1.5 小功能拆分清单（`T01`–`T03`）
- `T01`（无前置编码阻塞，可立即与 B03/B04/B02 并行）：`src/shipping_billing_agent/harness/`（13 组数据集含 C05 金额分支与 C13 4 Office 子例、契约对齐 Runner、JSON/Markdown Reporter）+ `tests/harness/test_b05_harness.py`。
- `T02`（依赖 `T01` + `B03` 合入主干后执行）：接通 `B03` CLI `eval` 与 `POST /api/v1/eval-runs`（201 新增 1 条记录 + GET 历史回读一致）、运行真实离线 13 组跑分并固化 `UNVERIFIED` 入库行为与差异打印。
- `T03`（依赖 `T02`）：完成 `docs/harness-design.md`、`docs/deep-thinking.md`、`README.md`、`scripts/package_delivery.py`，生成纯净 ZIP 包并完成**仓库外空目录解压 -> 启动服务 `/api/v1/health` -> 离线 `parse` 非空合规出账**实测。

---

## 2. 边界异常与风险处理
1. **无真实外部凭证下的测试伪绿风险**：明确区别 `OFFLINE_DETERMINISTIC` 与 `LIVE_LLM` / `LIVE_E2E`；缺模型配置时 `LIVE_*` 启动前记 `UNVERIFIED`（指标 `None`、`actual_trials=0`），中途断网记 `FAILED`（保留已执行数、指标 `None`），模型答错记 `COMPLETED`（拉低分数并返回非零）。
2. **评测依赖原题真实样本**：支持 `samples_dir` 与 `SHIPPING_BILLING_REAL_SAMPLES_DIR`，缺 4 份原题样本时默认失败报错，绝不静默跳过计为 100% 通过。
3. **空数组 `[]` 假过 JSON Schema 风险**：`3/5/6` 混合批和 ZIP 独立目录端到端验证均强制检查非空记录数与已知精确金额/币种。
4. **交付 ZIP 误含敏感凭据或路径耦合**：双层过滤 + 内容扫描（无 `.env`/`*.db`/`.wf-tmp`/`.worktree`），并在外部空目录真实走通启动与 `parse`。

---

## 3. 异构方案审查与主对话裁决记录
- 审查模型：`codex`（`plan_critic_codex`，审查意见见 `.wf-tmp/critic-B05.md`，共 9 条发现，包含 7 项阻塞与 2 项精简/修订建议）。
- 裁决结论：**9/9 全部采纳并已完整合入上文 §1–§2 设计**（修正 §8.8 vs §8.9 对应并补充 `docs/harness-design.md`；区分 `pass@k` vs `pass^k`；拆清启动前 `UNVERIFIED` / 中途 `FAILED` / 答错 `COMPLETED` / 预期 `C12` 四态；指标字段 1:1 对齐 `schema.sql`/`openapi.yaml` 并把分项分子分母纳入 `report_json`；拆清 `LIVE_LLM` vs `LIVE_E2E` 前置与自修复证据；补齐 `C13` 四种 Office 样本、C05 `30->40`、C08 联机安全底线与 `3/5/6` 三口径实数检查；明确 `T02` 依赖 `B03` 与引用 `B03/B04` 验收；在 `README.md` 补全 `LUKA@PLTPLT.COM` / `pan@pltplt.com` 交卷清单；规定 ZIP 必须在仓库外空目录完成解压启动、`/api/v1/health` 与非空 `parse` 端到端冒烟）。
