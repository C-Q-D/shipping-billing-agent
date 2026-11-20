# Wave 15 发布与真实验证归档（UX 精简重构 × 真实大模型与浏览器闭环验证）

## 1. 本轮解决的核心问题

1. **真实业务口径与大模型联机接地修复（`deepseek-flash` + 4 份笔试真样本）**
   - `PDF示例账单1.pdf`（`C02`）：从误认顶部窗口收件人为签发方导致 `AR` 及误把费用代码 `OCA17` 当参考号，修正为财务准确口径 **`AP`（`CMA CGM (AMERICA) LLC -> NEPTUNE SHIPPING LIMITED`，`CMDUCHN4053601`，`245.0000 USD CC`）**，保留第 1–3 页 `$25/$1100/$150` 违约条款与第 2–3 页汇款账号过滤。
   - `图片示例.png`（`C01`）：识别并归一客户短编号（`Payer: USIAHALLAM`）不引发 `CROSS_DOCUMENT_CONFLICT`，同时无借贷标识发票保持 `debit_credit = UNKNOWN`，在 `ZIM AMERICAN INTEGRATED SHIPPING SERVICES CO. LLC`（`AR`）与 `ALL AMERICA FORWARDING, INC.`（`AP`）双视角下均自动就绪（`5.00 + 21.90 = 26.90 USD`）。
   - `PDF 示例账单2.pdf`（`C03`）：兼容中英文相邻合并抬头（`湃乐多（上海）航运科技有限公司 Pilot Aiship Tech co., Ltd.`）及底栏 `BALANCE DUE TO NEPTUNE SHIPPING LIMITED (LAX)` 收款方回显，同时严格拦截错填债务方或未印实体的混淆；明确标示「付款方待确认」。
   - `深圳容宇 PRE-ALERT*.eml`（`C04`）：兼容邮件非贷项通知别名（如 `Invoice` / `Billing Notice`）、问候语人名（`Dear LI`）防误采、正文 MBL/HBL 防误填 `reference_no` 与 `PATCH` 后清理旧“无法唯一匹配”推理；严格维持无 `PP/CC` 不猜与多来源提单冲突提醒。
   - 通用安全守卫（`C13` 等）：系统提示词脱毒并用 `_has_source_party_evidence` 阻断无抬头表格复制记账主体幻觉，确保持续触发 `ENTITY_NOT_MATCHED`。

2. **用户视角财务复核工作台 UX P0/P1/P2 优化**
   - **P0-1/2/3**：左栏文件筛选/搜索与右栏详情强同步、筛空清右栏防改错单；顶栏「本批公司」与左栏「下一批临时公司」物理隔离并在 `202` 接受后自动复位；`AR`/`AP` 资金流摘要准确标示「付款方待确认 / 收款方待确认」与签发方；`/favicon.ico`（200 SVG）消除直访 404，清理冗余静态碎片。
   - **P1/P2**：红灯阻断置顶并内联原文摘录；同源上下文补全合并折叠；排除条款中英文理由统一为中文标准类目并跨组去重；次要运输与历史字段默认收起；管理抽屉查表在前、新增/高危清理在后；Harness 用例展示对齐为 `13 组（16 项） / 48 项次`，并将 `POST /api/v1/eval-runs` 超时提升至 `EVAL_REQUEST_TIMEOUT_MS = 180_000` 消除 30 秒浏览器取消；全表单补全语义 `id` 与 `name`。

## 2. 终版可核对验证指标（全部于终次代码修改后实测）

- **静态与契约门禁**：`uv run ruff check --no-cache src tests scripts/package_delivery.py`、`uv run python -B scripts/verify_contracts.py`、`node frontend/src/api/decimalJson.test.ts`（**PASS 1–36**）、`npm --prefix frontend run typecheck`、`npm --prefix frontend run build`（45 modules，742ms）均通过。
- **全量回归测试**：`uv run pytest -p no:cacheprovider -q` -> **1897 passed, 0 failed, 0 skipped (1040.68s)**。
- **离线确定性 Harness（`OFFLINE_DETERMINISTIC`, `k=3`）**：**48/48 PASSED**，`pass@3 = pass^3 = 1.0`，`conflict_recall = 1.0 (21/21)`，`false_positive_rate = 0.0 (0/69)`，`avg_latency_ms = 632.96 ms`。
- **真实大模型 Harness（`LIVE_LLM`, `deepseek-flash`, `k=3`）**：**48 次执行（115 次真实模型调用）**，`C01`–`C04` 全部 4 份笔试真实样本以及 `C05`、`C07`–`C12`、`C13/.xls,.xlsx`（共 13/16 项）**均达成 3/3 全通过（`pass^3 = 0.8125`，`pass@3 = 0.875`，`conflict_recall = 1.0 (21/21)`）**；剩余 3 项差异说明见 `docs/harness-design.md`。
- **真实 Chrome 全链路浏览器复验**：完成手工多文件上传、4 真样本批、`PDF示例账单1/2`、图片双视角、EML `PATCH -> 批准 -> F5 刷新回读 -> 三口径下载 200 -> 同步基准库 200 -> 历史库检索`、同步 48s `eval-runs 201` 与排除条款 22 条跨组 0 重复核对、桌面 `1440×900` / `1280×800` 及移动端 `390×844` 实测；Console 错误/警告、`pageerror`、缺失 `id`/`name`、意外 `409`/`500` 均为 `0`。
- **UNVERIFIED 边界**：因未配置公网可访问文件中转（`PUBLIC_RELAY_BASE_URL` 为空），`LIVE_E2E`（外网 `gateway.pltplt.com` URL 识别）与 `self_repair_evidence` 仍如实保持 `UNVERIFIED`。
