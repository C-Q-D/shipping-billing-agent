# B05 Harness 评测套件、深度思考报告、README 与交付 ZIP 打包验收 — 功能实现方案

- **关联锚点**：
  - PRD：`F-07`（命令行、`eval_runs` 评测指标与 `UNVERIFIED` 透明报告）、`F-01`–`F-06` 全量端到端覆盖，详见 [product-spec.md](../../product-spec.md)
  - 题面交付要求：[AI_agent_%E7%AC%94%E8%AF%95%E9%A2%98_%E5%AE%8C%E6%95%B4%E6%8F%90%E5%8F%96.md](../../../../AI_agent_%E7%AC%94%E8%AF%95%E9%A2%98_%E5%AE%8C%E6%95%B4%E6%8F%90%E5%8F%96.md) §8.8（深度思考报告）、§8.9（评估体系/评测套件）、README 与压缩包交付验收
  - 架构与契约：[arch-contract.md](../../arch-contract.md) §1.4、§4（评测指标与双模式区分）、[schema.sql](../../contracts/schema.sql)（`eval_runs` 表）、[openapi.yaml](../../contracts/openapi.yaml)（`/api/v1/eval-runs`）
- **范围与非目标**：
  - **本批次范围**：`src/shipping_billing_agent/harness/`（`>= 10` 组真值＋对抗评测数据集、`OFFLINE_DETERMINISTIC` / `LIVE_LLM` / `LIVE_E2E` 执行器、`pass@k` / `pass^k` 多次稳定性指标、Markdown/JSON 报告）、挂接 `B03` CLI `eval` 与 `POST /api/v1/eval-runs` 仓储落库、深度思考报告 `docs/deep-thinking.md`（`>= 5` 项源自本项目真实演进的深度工程洞见）、工程化 `README.md` 与 `.env.example` 检查、纯净交付包生成脚本 `scripts/package_delivery.py` 与自动化回归测试 `tests/harness/`。
  - **非目标**：不私自修改未配置真实密钥的环境变量或跳过真实网络报错，无真实外部环境时严格按 `UNVERIFIED` / `null` 指标记录。

---

## 1. 模块与内容改动设计

### 1.1 分层边界（严格遵守 `test_config_and_domain.py` §431）
```text
api/ · cli/ ──▶ harness/ (dataset, runner, reporter) ──▶ services/, agent/, parsers/, gateways/, storage/, domain/, config/
```
- 注意保持 `services/` 绝不反向导入 `harness/`；`B03` API/CLI 通过依赖注入把 `harness.runner.run_eval_suite` 传入。

### 1.2 标准与对抗评测数据集（`src/shipping_billing_agent/harness/dataset.py`）
构建结构化、带真值断言清单与允许误差规则的 `EvalCase` 数据集（**共 12 组用例 ≥ 10 组题面要求**）：
1. **4 份真实笔试原题回归组**：
   - `C01_REAL_PNG_ZIM_VS_ALL_AMERICA`（`图片示例.png` RapidOCR/OCR 双主体 `AR`/`AP`、2 条费用、缺数量严禁反除）；
   - `C02_REAL_PDF1_GENERAL_NOTICE`（`PDF示例账单1.pdf` `245.00 USD CC`，排除 1–3 页 `$25/$1100/$150` 违约条款与银行段）；
   - `C03_REAL_PDF2_DC108478_MULTI_LINE`（`PDF 示例账单2.pdf` 3 条费用、NEPTUNE 单边 `AR` vs 无关主体拦截、同单双 `Handling Charge` 配对）；
   - `C04_REAL_EML_PREALERT_NO_GUESS`（`深圳容宇 PRE-ALERT...eml` 空根 0 次 LLM、`SHA pay TAR $50`、`Carrier Free H/C` 排除、乱序附件同效、`payment_term=None`）；
2. **核心匹配、局部更新与贷项组**：
   - `C05_PARTIAL_UPDATE_DN202609001`（省略双方与金额币种仅改 `PP -> CC`，对照 V1 输出 `UPDATE`、对照 V2 输出 `NO_CHANGE`）；
   - `C06_SUPPLIER_CREDIT_NOTE_AP`（供应商 `Credit Note` `40.00` 保持 `AP` 且冲减 `-40.00`，同步反测 `Credit Card Invoice` 绝不误负）；
   - `C07_MULTI_CURRENCY_TAX_INVOICE`（多币种分币 `30+3=33 USD` & `40+4=44 EUR` 含税放行 vs 缺税错写拦截）；
3. **高风险边界、OCR 损坏与对抗注入组**：
   - `C08_PROMPT_INJECTION_IGNORE_ALL`（恶意原文试图令金额变 0 / 强批仍截为 `PENDING_REVIEW` 且出网已脱敏）；
   - `C09_ISO6346_AND_OCR_CONFUSION`（错字符箱号软告警带唯一 `WARNING` 建议 + `1.310.40`/`ZZZ`/`2026-02-30` 硬阻断）；
   - `C10_DUPLICATE_SHA256_AND_CROSS_CONFLICT`（双入口同哈希归并双路径单 `CREATE` + 跨根冲突拦截）；
   - `C11_LOSSY_DECIMAL_PRECISION`（`9007199254740991.01` & `0.0025` 全链路原样保留）；
   - `C12_EXPLICIT_ZERO_TOTAL_AND_MISSING_LINE`（`Total: 0.00` 对非零行拦截 + 有总额零明细产出失败卡）。
- 为每条 `EvalCase` 明确标注 `requires_real_samples: bool`：缺失真实样本文件夹时报错或明确标注 `SKIPPED/FAILED` 理由（绝不悄然当作通过）。

### 1.3 评测运行器、多轮稳定性与报告（`src/shipping_billing_agent/harness/runner.py`, `reporter.py`）
1. **三模式区分与诚实门禁**：
   - `OFFLINE_DETERMINISTIC`：使用离线解析器 + 规则引擎 + 受控确定性队列替身执行全量 12 组数据集，计算并上报：
     - `schema_pass_rate`（三口径输出对 `billing-output.schema.json` 合格率）
     - `field_exact_accuracy`（核心字段真值一致率，基于 `Decimal` 等值）
     - `operation_accuracy`（四态分类准确率）
     - `alert_precision` & `blocker_recall`（告警精准率与硬红灯零漏放召回）
     - `pass_1` & `pass_k`（执行 `k_trials` 次，核验多轮确定性稳定性，如 `k=3`）
   - `LIVE_LLM` / `LIVE_E2E`：首先显式检测外部前置条件（DeepSeek API Key、PltPlt OCR URL/公网中转条件或真实网络可达性）；**一旦条件未就绪或联机请求失败，立即产 `overall_status = "UNVERIFIED"`、`error_message = "<实际原因>"`、`executed_cases = 0`，指标全置 `None`（SQLite `eval_runs` 存 `NULL`，严禁写入 `0.0` 或拿离线替身伪装线上通过！）**；条件具备且真实跑通后，同样统计多轮真实 `k_trials` 的 `pass@1` 与 `pass^k` 及耗时、实际 `ocr_calls` / `llm_calls`。
2. **`reporter.py`**：生成机器 JSON 与人类可读 Markdown 评测报告（按 `eval_runs` 格式落盘或打印），并在末尾显著提示 `UNVERIFIED` 项。

### 1.4 深度思考报告、README 与交付打包（`docs/deep-thinking.md`, `README.md`, `scripts/package_delivery.py`）
1. **`docs/deep-thinking.md`（≥5 项真实沉淀洞见）**：结合本仓库在开发和三轮审查中的真实踩坑，深入剖析至少 6 个核心问题（禁止空泛套话）：
   - *领域确定性门禁 vs LLM 幻觉的防线划分*（为何用双轨 `raw_invalid_fields`/`_ground_model_extraction` + 单轮 `retry_count <= 1` 阻断“强行凑平”与“省略洗白”）；
   - *JS `Number` / Python `float` 与全链路无损 `Decimal` 的隐形账损*（从 OCR JSON 入口、SQLite 字符串列、FastAPI 入站到前端 `decimalJson.ts` 的 4 处截断坑）；
   - *航运视角歧义与局部更新继承先后序*（`entity_resolver -> matcher -> validators` 为什么局部 `DN-202609-001` 省略双方会踩出假红灯、括号分公司 `(UK)` vs `(HK)` 隔离与贷项不翻转收付）；
   - *防注入、敏感字段零泄露与预算原子门禁*（`sanitize_for_llm`、URL 签名参数清理、`LlmBudgetGuard` + HTTP hook 控制 SDK 隐式重试）；
   - *基于真实反例驱动的 Harness 评测体系与 `UNVERIFIED` 工程诚信原则*；
   - *前后端双轨开发如何通过单一冻结 `openapi.yaml` 避免“两套业务规则”重复劳动*。
2. **`README.md`**：项目亮点与架构概览图引、安装（`uv sync` + `npm ci` + `playwright` 浏览器说明）、`.env.example` 参数解释与安全底线、4 份真实样本放置说明与一键启动/CLI 示例、全部评测及单测/集成测试命令、实测结果与未验项目透明度说明。
3. **`scripts/package_delivery.py`**：生成面试可直接提交的纯净代码归档包（严格排除 `.venv/`、`node_modules/`、`.wf-tmp/`、`.worktree/`、`.env`、`*.db`、`__pycache__/`、临时测试产物），并自动校验压缩包解压后的核心源码、契约文档及示例结构完整。

### 1.5 小功能拆分预览（`T01`–`T03`）
- `T01`：`src/shipping_billing_agent/harness/`（12 组数据集、三模式评测运行器、Markdown/JSON 报告器、`tests/harness/test_b05_harness.py`）
- `T02`：接通 `B03` CLI `eval` 与 `POST /api/v1/eval-runs` 实库记录＋验证真实离线 12 组跑分与 `UNVERIFIED` 状态持久化
- `T03`：`docs/deep-thinking.md`、`README.md`、`scripts/package_delivery.py` 与最终洁净包生成及冒烟验证

---

## 2. 边界异常与风险处理
1. **无真实外部凭证下的测试伪绿风险**：明确区别 `OFFLINE_DETERMINISTIC` 与 `LIVE_*` 模式；缺 `DEEPSEEK_API_KEY` 或离线不可达时 `LIVE_*` 严格标记 `UNVERIFIED` 且指标为 `None`（测试必须验证该诚实行为）。
2. **评测依赖原题真实样本**：遵循已定规则，缺原题 4 样本时默认失败报错，绝不静默跳过计为 100% 通过。
3. **交付 ZIP 误含敏感凭据或垃圾缓存**：`package_delivery.py` 采用白名单+严格黑名单双重过滤，并在生成后执行 ZIP 内容列表扫描断言（无 `.env`、`*.db`、`.wf-tmp`、`.worktree`、私钥）。

---

## 3. 异构方案审查与主对话裁决记录
- 审查模型：`codex`（`plan_critic_codex`，待本轮并行审完后回填裁决）
- 裁决结论：待审查完成后补充。
