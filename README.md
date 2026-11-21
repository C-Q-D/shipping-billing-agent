# 航运智能账单助手 Agent（Shipping Intelligent Billing Assistant）

面向跨境航运与货代财务对账场景的多格式智能账单解析与复核系统。输入 **EML 邮件（含递归附件）、PDF（含多页跨表/扫描件）、图片（PNG/JPG/TIFF）以及 Excel / Word（`.xls`/`.xlsx`/`.doc`/`.docx`）** 后，自动完成费用抽取、名词标准化、**以当前记账公司视角判定应收/应付（AR/AP）与借贷冲减**、四级历史账单增量匹配与门禁校验，并配套一体化的 **Web 财务复核工作台**（含**工作台内原件对照预览**与三口径导出）。

---

## 目录

- [1. 面试官 3 分钟快速上手](#1-面试官-3-分钟快速上手)
  - [环境前置](#环境前置)
  - [方式 A：零密钥离线 CLI 体验](#方式-a零密钥离线-cli-体验)
  - [方式 B：启动 Web 复核工作台（完整交互体验）](#方式-b启动-web-复核工作台完整交互体验)
  - [方式 C：配置真实大模型与运行 Harness 评测](#方式-c配置真实大模型与运行-harness-评测)
- [2. 真实样本浏览器实测结果（一眼看懂）](#2-真实样本浏览器实测结果一眼看懂)
  - [整批上传 4 份原题样本（`NEPTUNE SHIPPING LIMITED` 视角）](#整批上传-4-份原题样本neptune-shipping-limited-视角)
  - [跨记账主体视角切换验证（同一原件自动重算 AR/AP）](#跨记账主体视角切换验证同一原件自动重算-arap)
  - [工作台原件对照与三口径导出实测](#工作台原件对照与三口径导出实测)
- [3. 超出笔试基础要求的工程与体验增强](#3-超出笔试基础要求的工程与体验增强)
- [4. 原题 10 项验收标准对照](#4-原题-10-项验收标准对照)
- [5. 架构设计、目录结构与冻结契约](#5-架构设计目录结构与冻结契约)
- [6. CLI 命令与环境变量速查](#6-cli-命令与环境变量速查)
- [7. 自动化验证、Harness 评测与边界声明](#7-自动化验证harness-评测与边界声明)
- [8. 纯净交付包与投递核对](#8-纯净交付包与投递核对)

---

## 1. 面试官 3 分钟快速上手

### 环境前置

- **Python 3.12**（见 [.python-version](.python-version)）与 [`uv`](https://docs.astral.sh/uv/)
- **Node.js ≥ 18 + npm**（仅首次构建前端静态产物时需要；交付包与仓储均附带完整前端源码）

```sh
# 1) 同步后端与解析依赖（含 pdfplumber / RapidOCR / LangChain / FastAPI 等）
uv sync

# 2) （可选）如需运行 Playwright 浏览器端到端脚本
uv run playwright install chromium
```

### 方式 A：零密钥离线 CLI 体验

无需任何外部 API Key，直接用仓库内置样例 [examples/sample_debit_note.eml](examples/sample_debit_note.eml) 与历史账单 [examples/existing_bills.json](examples/existing_bills.json) 跑通「解析 → 历史四级匹配 → 增量继承 → 标准 JSON 输出」：

```sh
uv run shipping-billing-agent parse examples/sample_debit_note.eml \
  --entity "NEPTUNE SHIPPING LIMITED" \
  --bills examples/existing_bills.json \
  --ocr-mode REMOTE_WITH_LOCAL_FALLBACK \
  --output-mode official
```

**预期标准输出（恰好 2 条，均以签发方 `NEPTUNE SHIPPING LIMITED` 判定为应收 `AR`）：**

| `operation` | 原件费用名 (`raw_charge_name`) | 标准费用名 (`charge_item`) | 金额 | 付款条款 | 匹配结果 (`audit_trail.matched_bill_ref`) |
| --- | --- | --- | --- | --- | --- |
| `UPDATE` | `Handling Fee` | `Handling Charge` | `40.00 USD` | `PP` | 命中 `L1_DOC_CHARGE`（`example-dn-202609-001-handling`） |
| `CREATE` | `THC` | `Terminal Handling Charge` | `120.50 USD` | `CC` | 新增入账 |

### 方式 B：启动 Web 复核工作台（完整交互体验）

```sh
# 1) 构建前端静态产物（产出至 frontend/dist/）
npm --prefix frontend ci && npm --prefix frontend run build

# 2) 启动一体化服务（FastAPI 后端 + React 工作台，默认监听 http://127.0.0.1:8000）
uv run shipping-billing-agent serve --ocr-mode REMOTE_WITH_LOCAL_FALLBACK
```

打开浏览器访问 `http://127.0.0.1:8000`（停止服务按 `Ctrl+C`）：
- 健康检查：`GET http://127.0.0.1:8000/api/v1/health` 返回 HTTP `200`，零密钥环境下 `"status": "ok"`, `"llm_configured": false`。

**浏览器 5 步快速操作指引：**
1. **上传解析**：把 EML / PDF / 图片 / Excel / Word 文件或整个目录拖入左栏投递区（亦可点「临时更换」切换如 `TARGETING A INC` 等记账公司，或点开「示例演练」了解用法）。
2. **查看原件分屏对照**：点击文件树中任意文件旁的 **`查看原件`**，或右栏顶部/原文摘录旁的 **`对照原件 · 第 N 页`**，即可在左侧停靠画布直接浏览 PDF（支持跳页）、图片（支持缩放与 90° 旋转）或一键切换 EML 邮件正文与 `HBL`/`MBL`/内嵌图片附件；右上角支持切换 **`浮窗`** 或 **`新窗口`**，右栏 **`◀ 上一条` / `下一条 ▶`** 支持边看原件边连续审单，按 `Esc` 收起。
3. **核对与人工修正**：待核队列默认按 `待复核` 置顶排序并提示红色阻断（`[暂不能入账]`）与黄色核对（`[请核对]`）。在右栏核对金额、币种、交易双方或点击「核对合计」修正单据总额后保存重验，或输入 2–500 字理由执行「人工批准 / 驳回排除」。
4. **即时切换主体视角**：点击顶部「本批公司」切换本批记账实体，后端自动重新计算整批 `AR / AP` 与历史库归属差异（并留存修订记录）。
5. **三口径导出与同步历史库**：点击右上角 **`导出可入账`**，可在 **`可入账账单` (`official`)**、**`含待复核全量` (`full_standard`)**、**`完整审计留痕` (`audit`)** 间切换预览分币种汇总与无损 JSON 下载；通过顶部「管理与工具」可维护记账主体/别名、费用词典、历史账单库，将已确认记录幂等写入历史库，或打开「管理员自检（Harness 评测）」。

### 方式 C：配置真实大模型与运行 Harness 评测

复制 [.env.example](.env.example) 为本地 `.env`（**切勿提交真实密钥**）并填入兼容 OpenAI 的模型配置：

```env
LLM_PROVIDER=openai_compatible
LLM_BASE_URL=https://api.deepseek.com/v1
LLM_MODEL=deepseek-flash
LLM_API_KEY=<YOUR_API_KEY>
```

重启 `serve` 后健康接口 `"llm_configured"` 变为 `true`，工作台与 CLI 将自动启用「确定性规则基线 + LangGraph 大模型语义抽取与原文逐行接地核验」双路架构。若暂缺公网文件 URL 或公网中转，建议使用 `--ocr-mode REMOTE_WITH_LOCAL_FALLBACK` 自动回退至本地 `pdfplumber` + `RapidOCR` 多格式识别。

---

## 2. 真实样本浏览器实测结果（一眼看懂）

以下结果全部于 **2026-11-21** 在本地启动的工作台（`http://127.0.0.1:8000`，`deepseek-flash` + `--ocr-mode REMOTE_WITH_LOCAL_FALLBACK`，1440×900 视口）通过真实浏览器逐一上传原题 4 份样本点击跑出（全程 **HTTP ≥400 错误数为 0**）。

### 整批上传 4 份原题样本（`NEPTUNE SHIPPING LIMITED` 视角）

在默认记账公司 `NEPTUNE SHIPPING LIMITED` 下一次性上传全部 4 份原题文件（批次 `B-8BF214B98BBC`，`4 / 4 份` 全部成功）：

| 来源文件（自动识别通道） | 票号 / 运单号 | 提取费用项（原件原文） | 金额与币种 | 付款 | AR/AP 与流向判定 | 状态与风控要点 |
| --- | --- | --- | --- | --- | --- | --- |
| **`PDF示例账单1.pdf`**<br>本地 PDF 文本提取 | `CMDUCHN4053601`<br>箱号 `SAPU2040689` | `On Carriage Additional`<br>(`On Carriage Additional - US Em`) | **`245.0000 USD`** | `CC` | **`我司应付 (AP)`**<br>`NEPTUNE → CMA CGM (AMERICA) LLC` | **`已自动就绪`** (`CREATE`)<br>正确隔离 `OCA17` 代码；排除 2 类 18 条原文（含 `$25`/`$1100`/`$150` 违约金与 9 条脱敏汇款信息） |
| **`PDF 示例账单2.pdf`**<br>本地 PDF 文本提取<br>（单据合计 `1410.40 USD`） | `DC-108478`<br>MBL `MAEU275752922`<br>HBL `CGT260801188` | 1. `Handling Charge` (`HANDLING CHARGE`)<br>2. `Detention Free Time Charge` (`4 ADD'L DETENTION FREE TIME CHARGE`)<br>3. `Handling Charge` (`HANDLING CHARGE`) | **`65.00 USD`**<br>**`1310.40 USD`**<br>**`35.00 USD`** | `CC`<br>`CC`<br>`CC` | **`我司应收 (AR)`**<br>`付款方待确认 → NEPTUNE (签发：Pilot Aiship)` | **`已自动就绪 ×3`** (`CREATE`)<br>识别签发转发方 Pilot Aiship 与收款归属 NEPTUNE；明确标出「付款方待确认」；排除 `PAID AMOUNT 0.00`（已付金额）与 BOA 汇款信息 |
| **`图片示例.png`**<br>本地 RapidOCR 回退<br>（单据合计 `26.90 USD`） | `ORFR1618731`<br>MBL/BKG `ZIMUNGB1312144`<br>箱号 `JXLU4504463` | 1. `Wharfage Administration Fee`<br>2. `Wharfage` (`单价 2.85`，数量留空) | **`5.00 USD`**<br>**`21.90 USD`** | `CC`<br>未定 | `方向待定`<br>`ZIMAMERICAN → ALL AMERICA` | **`待复核 ×2`** (`[暂不能入账]`：当前实体是 NEPTUNE，非单据双方)<br>守住禁止从 `21.90 / 2.85` 反推数量；排除收款账户条款 |
| **`深圳容宇 PRE-ALERT...eml`**<br>EML 邮件正文解析 + 递归拆包 | MBL `OOLU4055331281`<br>HBL `SSZSE26093128`<br>箱号 `TIIU8975975` | `Handling Charge` (`H/C`)<br>正文：`SHA pay TAR:H/C $50` | **`50.00 USD`** | 未定 | `方向待定`<br>`未识别签发方 → 未识别接收方` | **`待复核 ×1`**（含「跨附件 MBL 差异：`HZMHK331281` vs `OOLU4055331281`」及 `TARGETING A INC`/`SUN TRACK` 补全线索）<br>**自动递归拆包**：提取 `body.txt`、`MBL.pdf`、`HBL.pdf` 辅助凭据，自动跳过签名图 `InsertPic_C25B.jpg`（`SKIPPED_NOISE`）；排除 `Carrier Free H/C` |

### 跨记账主体视角切换验证（同一原件自动重算 AR/AP）

在工作台点击「临时更换」分别以两家真实交易主体视角重新提交同一批原件：

| 测试视角（记账公司） | 上传样本 | 判定结果 | 核对流向与关键行为 |
| --- | --- | --- | --- |
| **`TARGETING A INC`**（`B-8716FBA8162C`） | `深圳容宇 PRE-ALERT...eml` | **`我司应收 (AR)`**（`50.00 USD`，`待复核`） | 即使原件未列明 `issuer`/`recipient`，系统依据 `payee_hint: TARGETING A INC` 自动判定 `AR`，流向展示 **`付款方待确认 → TARGETING A INC（款付给我方）`**；**0 项红色阻断**，保留 8 项核对线索（MBL 差异与上下文字段），排除 `Carrier Free H/C`。 |
| **`ALL AMERICA FORWARDING, INC.`**（`B-3EBD68EABA7F`） | `图片示例.png` | **`我司应付 (AP)` ×2**（`5.00 + 21.90 = 26.90 USD`，全部 **`已自动就绪`**） | 2 条费用均自动转为 **`已自动就绪` (`CREATE`)**、0 阻断 0 告警，流向清晰显示 **`ALL AMERICA FORWARDING, INC. → ZIMAMERICAN INTEGRATED SHIPPING SERVICES CO. LLC（我方需付款）`**，可直接正式导出。 |

### 工作台原件对照与三口径导出实测

- **三口径导出精度合计实测（第 1 批 `B-8BF214B98BBC`）**：
  - **`可入账账单` (`official`)**：**4 条**，分币种精确合计 **`USD 1655.4000`**（排除 3 条待复核；并明确提示 3 条无收付款方名称的记录标为「付款方待确认」）
  - **`含待复核全量` (`full_standard`)**：**7 条**，分币种精确合计 **`USD 1732.3000`**
  - **`完整审计留痕` (`audit`)**：**7 条**，分币种精确合计 **`USD 1732.3000`**
- **原件对照预览实测**：支持在停靠分屏中一边浏览 `PDF 示例账单2.pdf` / `PDF示例账单1.pdf` / `图片示例.png`（含即时 90° 旋转、适应缩放至 125%–300%）/ EML 附件标签（`邮件正文`、`4055331281 HBL.pdf`、`4055331281 MBL.pdf` 与已跳过图片 `InsertPic_C25B.jpg`），一边使用右栏 `◀ 上一条` / `下一条 ▶` 核对；并可随时切换 `文本` 提取兜底、锁定参照文件或切换浮窗。

---

## 3. 超出笔试基础要求的工程与体验增强

在完整实现原题 10 项核心能力之外，项目从财务日常作业与工程可靠性出发额外完成了以下 6 项实质性增强：

| 增强维度 | 额外落地的能力 | 解决的真实痛点 |
| --- | --- | --- |
| **1. 一体化 Web 财务复核工作台** | 三栏响应式工作台（左栏上传与文件/附件树、中栏分状态队列与搜索、右栏全字段精编台）+ 失败文件原地重试/替换 + 跳过签名图一键「强制识别」 + 单据级「核对合计」弹窗/折叠修正与「同单补充漏提行」 | 原题仅要求抽取、匹配与导出 JSON；工作台让人工可以在统一界面完成疑难拦截处理、单据总计核销与最终放行。 |
| **2. 工作台内「原件对照预览」** | 新增只读 [`getDocumentContent`](docs/workflow/contracts/openapi.yaml) 接口（磁盘 SHA-256 校验、文件魔数识别、TIFF 服务端转 PNG、XSS/SVG 强制附件下载隔离、中文 RFC 5987 文件名）及前端 `DocumentPreviewPane`（默认停靠不遮挡右栏、可拖拽分隔条、浮窗/新窗口切换、PDF 跳页、图片缩放旋转、EML 家族水平附件标签、`◀ 上一条`/`下一条 ▶` 自动跟随与锁定原件、文本安全高亮） | 解决「上传后看不见原件，审单必须另开 PDF/看图/邮件客户端来回切换」的高频痛点。 |
| **3. 端到端 `Decimal` 无损金额闭环与三口径导出** | Python `Decimal` 解析 → SQLite `TEXT` 持久化 → `DecimalJsonResponse` 原文数字流输出 → 前端状态机 [`decimalJson.ts`](frontend/src/api/decimalJson.ts) 保留大额（如 `9007199254740991.01`）与高精度小数（如 `0.0025`），规避 JS `Number` 精度丢失；提供 `official` / `full_standard` / `audit` 三口径预览、下载与 `ReviewBundleV1` 复核包跨人导入 | 保障财务金额从抽取到浏览编辑、导出入账全链路 zero-drift 零误差，且兼容外部 CLI 离线复核协同。 |
| **4. 持久化主数据维护与幂等历史同步** | SQLite 8 表持久化批次数千条明细、记账公司与别名档案（`accounting_entities`）、费用标准词典（`charge_dictionary`，支持批内一键重算）、历史账单基准导入/清空（`existing_bills`）、批内字段修订审计日志，以及基于 `baseline_sync_items` 唯一键的幂等历史库同步 | 消除「每次重跑从零开始」的断层，支持跨批次连续对账而不因重复同步造成脏账。 |
| **5. 工程安全、并发锁与网络防御** | ① `expected_version` / `updated_at` 乐观锁防多窗口覆盖，前端按 `(batch_id, record_id)` 隔离草稿并丢弃过期轮询；<br>② URL 投递强制 SSRF 私网/回环拦截与签名参数脱敏；<br>③ 汇款账号/邮箱/电话在出网喂给 LLM 前脱敏为 `[REDACTED_BANK_ACCT]`；<br>④ 写操作强制全匹配 Origin/Referer CSRF 校验；<br>⑤ 浏览器本地 `OCR_TRUST_ENV=false` 默认直连绕过错误本机代理引发的网关 SSL EOF | 让 Agent 系统具备可直接上线的安全与并发一致性基础。 |
| **6. 本地自动回退与内置评测看板** | 在严守远端 `OCR_GATEWAY_URL` 主路径外，提供 `--ocr-mode REMOTE_WITH_LOCAL_FALLBACK` 自动结合 `pdfplumber` 与 `RapidOCR` 本地识别；并将 13 组 16 项真值回归接入工作台「管理员自检（Harness 评测）」看板持久化追踪 `pass@k` / `pass^k` | 兼顾零密钥本地离线演练、网络受限环境可用性与回归质量可视化度量。 |

---

## 4. 原题 10 项验收标准对照

| 原题验收标准 | 实现模块与验证入口 |
| --- | --- |
| 1. EML 正文及附件 | `parsers/eml_parser.py` 与 `parsers/dispatcher.py` 递归分发。CLI 快速体验验证纯文本，浏览器实测与 Harness `C04` 复验 EML + `HBL.pdf` / `MBL.pdf` 附件乱序一致与签名图自动忽略。 |
| 2. 指定 OCR API 识别 PDF、图片 | `gateways/ocr_client.py` 对接指定网关。`LIVE_E2E` 核对真实 `PLTPLT_OCR` 来源（当前 `UNVERIFIED`）；`PDF` / 图片本地解析与端到端提取见 `C01`–`C03`。 |
| 3. Excel 和 Word 费用表格 | `parsers/excel_parser.py`、`parsers/word_parser.py`（含合并单元格展开、空列对齐、图文交错过滤）。`C13` 独立验证 `.xls`、`.xlsx`、`.doc`、`.docx`。 |
| 4. 单号、费用、金额、币种提取与标准化 | `agent/rule_extractor.py`、`agent/graph.py` 与 `domain/charge_normalizer.py`、`domain/currency.py`。快速体验及 `C01`–`C03` 核对 `raw_charge_name`、ISO 4217 币种、原件条款隔离。 |
| 5. 新增与增量更新 | `domain/matcher.py` 四级优先匹配（L1–L4）。快速体验展示 `UPDATE` 与 `CREATE`，`C05` 对照不同基线，`C03` 一对多金额精准配对。 |
| 6. 从记账主体判断 AR/AP | `domain/entity_resolver.py`（兼顾别名、`payee_hint`/`payer_hint`、显式分公司隔离与 `Credit Note` 负额冲减）。`C01`、`C03`、`C06` 与浏览器实测三主体视角均已验证。 |
| 7. 重复、冲突与复核 | `domain/validators.py`、`services/review_service.py`。`C09`（ISO 6346 箱号建议与非法格式阻断）、`C10`（跨根冲突）、`C12`（合计冲突）。 |
| 8. Harness 稳定性与准确性 | `harness/` 的 13 组 16 项真值与对抗回归，含 `pass@k` 与 `pass^k` 度量，详见 [Harness 设计与指标](docs/harness-design.md)。 |
| 9. 至少五项深度思考 | 从业务与工程实践总结的 6 项深度思考，详见 [六项思考](docs/deep-thinking.md)。 |
| 10. Python + LangChain 实现 | [pyproject.toml](pyproject.toml) 固定 Python 3.12 环境，`agent/graph.py` 用 LangGraph 编排、`gateways/llm_client.py` 接入 LangChain `ChatOpenAI`。 |

---

## 5. 架构设计、目录结构与冻结契约

### 交互式系统图（浏览器可直接打开）

- [系统架构（五层总览与安全边界）](docs/workflow/diagrams/architecture.html)
- [用户与财务复核业务流程](docs/workflow/diagrams/user-billing-flow.html)
- [核心解析与复核时序图](docs/workflow/diagrams/pipeline-sequence.html)
- [账单操作与状态判定决策树](docs/workflow/diagrams/operation-decision-flow.html)

### 冻结工程契约

- [OpenAPI 3.1 接口契约（25 路径 / 30 操作）](docs/workflow/contracts/openapi.yaml)
- [SQLite 持久化表结构（8 张表与 Decimal/乐观锁约束）](docs/workflow/contracts/schema.sql)
- [标准账单输出 JSON Schema（Draft 2020-12，176 个活跃 ISO 4217 币种）](docs/workflow/contracts/billing-output.schema.json)

### 核心源码布局

```text
├── src/shipping_billing_agent/
│   ├── agent/          # LangGraph 编排、提示词防注入边界、确定性规则抽取
│   ├── api/            # FastAPI 应用、Origin CSRF 守卫、Decimal 流响应、6 组业务路由
│   ├── cli/            # Typer CLI (parse / serve / eval)
│   ├── config/         # Pydantic 设置、大小/页数限制
│   ├── domain/         # 核心模型、实体视角判定、4 级匹配器、校验器、费用词典、三口径导出
│   ├── gateways/       # OpenAI 兼容 LLM、远端/本地 OCR、SSRF URL 校验、出网文本脱敏
│   ├── harness/        # 13 组 16 项回归基准、Runner 与 Markdown/JSON 报告器
│   ├── parsers/        # EML/PDF/图片/Excel/Word 解析与安全文件名规范化
│   ├── services/       # 批处理并发服务、原件安全读取、复核修订服务、CLI 复核包导入
│   └── storage/        # SQLite 连接管理、8 表初始化、仓储与幂等历史同步
├── frontend/src/       # React 19 + TypeScript 复核工作台（含 DocumentPreviewPane 与无损金额状态机）
├── docs/               # Harness 设计、六项深度思考、系统图与冻结契约
├── examples/           # 零密钥自包含 EML 与历史账单示例
└── scripts/            # verify_contracts.py 契约全检与 package_delivery.py 纯净打包脚本
```

---

## 6. CLI 命令与环境变量速查

| 命令 | 说明与关键参数 |
| --- | --- |
| `parse` | 解析文件、目录或 `--url`，以 `--entity` 指定记账主体。`--bills` 载入本批生效历史账单 JSON，`--out` 输出标准 JSON，`--bundle-out` 导出工作台可导入复核包，`--output-mode` 支持 `official` / `full_standard` / `audit`，`--strict` 遇待核或失败非零退出。 |
| `serve` | 同时提供 REST API 与编译后的工作台 UI。默认 `127.0.0.1:8000`，支持 `--host`、`--port`、`--db-path`、`--ocr-mode`。 |
| `eval` | 运行内置 Harness：`--mode` 取 `OFFLINE_DETERMINISTIC`、`LIVE_LLM` 或 `LIVE_E2E`；`--k-trials` 指定轮数；`--out` 输出 JSON 报告。 |

`REMOTE_ONLY` 为 PDF/图片的严格远端 OCR 模式（需提供网关可读的公网 URL 或配置可控中转 `PUBLIC_RELAY_BASE_URL`）；`REMOTE_WITH_LOCAL_FALLBACK` 在缺少公网中转或远端识别失败时自动回落至本地 `pdfplumber` + `RapidOCR`（`--url` 仍走远端）。CLI 参数设计不含独立的 `LOCAL_ONLY`。

### 环境变量一览（见 [.env.example](.env.example)）

| 变量 | 默认值与用途 |
| --- | --- |
| `LLM_PROVIDER`、`LLM_BASE_URL`、`LLM_MODEL` | `openai_compatible`、`https://api.deepseek.com/v1`、`deepseek-flash`。可配置任意兼容端点与模型。 |
| `LLM_API_KEY`、`DEEPSEEK_API_KEY` | 均为空。优先 `LLM_API_KEY`；当端点为 HTTPS `api.deepseek.com` 且通用密钥为空时回退读取 `DEEPSEEK_API_KEY`。 |
| `LLM_TRUST_ENV`、`LLM_PROXY_URL`、`LLM_TIMEOUT_SECONDS`、`LLM_MAX_RETRIES` | `false`、空、`90`、`0`。支持显式代理配置、请求重试与有界自修复计数。 |
| `OCR_GATEWAY_URL`、`OCR_TASK` | `https://gateway.pltplt.com/ai/process`、`orderChange`。 |
| `OCR_TRUST_ENV`、`OCR_PROXY_URL`、`OCR_CONNECT_TIMEOUT_SECONDS`、`OCR_READ_TIMEOUT_SECONDS` | `false`、空、`10`、`300`。默认直连绕过本机环境代理以规避网关 SSL EOF。 |
| `PUBLIC_RELAY_BASE_URL`、`RELAY_PRESIGN_TTL_SECONDS` | 空、`300`。默认关闭公网中转。 |
| `APP_HOST`、`APP_PORT`、`ALLOW_REMOTE_BIND`、`APP_ALLOWED_ORIGINS` | `127.0.0.1`、`8000`、`false`、空。 |
| `APP_DATA_DIR`、`SQLITE_DB_PATH` | `data`、`data/shipping_billing.db`。单次 CLI `parse` 未指定 `--db-path` 时使用临时数据库。 |
| `SHIPPING_BILLING_REAL_SAMPLES_DIR`、`SHIPPING_BILLING_OCR_SAMPLE_URL` | 可选进程环境变量，分别覆盖原题样本目录与供 `LIVE_E2E` 使用的授权公网图片 URL。 |

---

## 7. 自动化验证、Harness 评测与边界声明

### 四份原题真实样本离线评测复现

在项目根目录放置原题目录 `笔试题AIAGENT/`（或设置 `SHIPPING_BILLING_REAL_SAMPLES_DIR`）包含 `图片示例.png`、`PDF示例账单1.pdf`、`PDF 示例账单2.pdf` 及 `深圳容宇 PRE-ALERT*.eml` 后，可运行 3 轮离线 Harness：

```sh
uv run shipping-billing-agent eval --mode OFFLINE_DETERMINISTIC --k-trials 3 --out offline-k3.json
```

### 实测数据汇总

| 验证维度 | 命令 / 范围 | 实测结果 |
| --- | --- | --- |
| **原件预览与接口契约专项回归** | `uv run ruff check --no-cache src tests scripts/package_delivery.py`<br>`uv run python -B scripts/verify_contracts.py`<br>`uv run pytest -q tests/unit/test_b03_api_routes.py tests/integration/test_b03_api_and_persistence.py`<br>`node frontend/src/api/decimalJson.test.ts` + `tsc --noEmit` + `vite build` | 代码规范检查 0 告警；SQL/OpenAPI(25 路径/30 操作)/JSON Schema/4 张 SVG 图全部 `PASSED`；API 路由与持久层 **`107 passed`**；前端无损金额、状态机、E2E 工作台与原件对照回归 **`PASS 1` – `PASS 38` 全通过**。 |
| **纯净打包与隔离运行验证** | `uv run pytest -p no:cacheprovider tests/harness/test_b05_delivery_package.py -q` | **`14 passed`**（解压到独立临时目录启动真实 FastAPI 并校验健康端点及 CLI 离线双行 Schema 输出）。 |
| **全量自动化测试套件基线** | `uv run pytest -p no:cacheprovider -q`（交付基线） | **`1897 passed, 0 failed, 0 skipped`**（`1040.68s`）。 |
| **离线 Harness (`k=3`)** | `OFFLINE_DETERMINISTIC`, 16 项 × 3 轮 = 48 次执行 | 十项指标及 `pass@3`、`pass^3` 均为 **`1.0` (`16/16`)**，正常记录误拦率 **`0.0` (`0/69`)**，冲突召回 **`1.0` (`21/21`)**。 |
| **真实大模型 Harness (`k=3`)** | `LIVE_LLM` (`deepseek-flash`, 115 次调用) | **`C01`–`C04` 全部 4 份原题真实样本及 `C05`/`C07`–`C12`/`C13(.xls,.xlsx)` 均实现 `3/3 (100%)` 稳定全通**（`pass@3 = 0.875`, `pass^3 = 0.8125`, `conflict_recall = 1.0 (21/21)`）；详细分子分母与剩余 3 项合成夹具差异原因见 [Harness 报告](docs/harness-design.md)。 |

> **诚实未验证边界（`UNVERIFIED`）**：`LIVE_E2E`（依赖网关可访问公网图片 URL 触发 `gateway.pltplt.com` 真实 `PLTPLT_OCR` 回源）与 `self_repair_evidence`（模型二轮自修复触发证据）当前仍标为 **`UNVERIFIED`**；配置已授权公网图片 URL 或受控中转发布器后，即可按 [联机复验](docs/harness-design.md#联机复验) 立即复测。

---

## 8. 纯净交付包与投递核对

生成干净交付 ZIP（仅打包白名单源码、前端、契约、文档与离线样例，自动剔除 `.env`、数据库、缓存、原题测试目录并执行疑似密钥扫描）：

```sh
uv run python scripts/package_delivery.py
```

默认产物为 `dist/shipping-billing-agent-delivery.zip`。

### 投递核对清单

- [ ] 收件人填写 `LUKA@PLTPLT.COM`、`pan@pltplt.com`。
- [ ] 邮件标题使用 `应聘渠道+姓名+应聘岗位`。
- [ ] 附上纯净代码 ZIP，或提供可审阅的 Git 仓库链接。
- [ ] 确认包含完整可复现的 [README](README.md)、[离线样例](examples/)、[Harness 报告](docs/harness-design.md)、[六项深度思考](docs/deep-thinking.md)，且归档内无 `.env`、数据库或缓存。
