# B04 Lumno B2 三栏/移动双视口 React 工作台、全字段复核、三口径导出与评测看板 — 功能实现方案

- **关联锚点**：
  - PRD：`F-06`（财务复核工作台、全字段人工修订、三口径导出、主数据维护与复核包导入），详见 [product-spec.md](../../product-spec.md)
  - 界面设计规格：[prototypes/workbench/README.md](../../prototypes/workbench/README.md)
  - 视觉系统约束：`lumno-ui-style` **分支 B2（全屏工作台 / 玻璃纸面工作面板）**（见 `lumno-ui-style` §1–§4、[lumno-tokens.css](../../../../frontend/src/styles/lumno-tokens.css)、[continuous-corners.css](../../../../frontend/src/styles/continuous-corners.css)、[lumno-background.d.ts](../../../../frontend/src/lib/lumno-background.d.ts)）
  - 后端接口契约：[openapi.yaml](../../contracts/openapi.yaml) 与强类型定义 [schema.d.ts](../../../../frontend/src/api/schema.d.ts)
- **范围与非目标**：
  - **本批次范围**：完全位于 `frontend/` 目录内完成工程化 React 19 + TypeScript 工作台开发、设计令牌与艺术画境挂载、双模式 API 客户端（真实 `/api/v1/*` + 合约一致 `?mode=mock` 数据适配器，便于在无后端状态下独立开发与真实浏览器多视口验收）、全部核心业务与管理弹窗功能、构建产物验证与双视口 Playwright 真实点击检查。
  - **非目标**：不修改后端源码或 `openapi.yaml`/`schema.d.ts`；不引入外部高耦合重型 UI 组件库，轻量微触感组件直接依据 `--lumno-*` 语义令牌精确构建。

---

## 1. 模块与界面改动设计

### 1.1 视觉规范与 `lumno-ui-style` 分支 B2 严格落地
1. **舞台与画境背景（范式 B）**：
   - 外层全屏容器挂载 `.lumno-oil-stage` 与 `mountProceduralLumnoBackground(container, { preset: "coast", mode: "oil-veil" })`（海运主题低饱和印象派海岸画境，卸载时调用 `handle.destroy()`），叠加三段凹形白雾空气蒙版，深浅色主题切换时平滑切换 `body[data-theme="dark"]` 与画境明暗适配；
   - 工作台三栏主卡采用轻透纸面/亚克力玻璃（浅 `rgba(255,255,255,0.84)` + `backdrop-filter: blur(18px)` + `1px solid rgba(15,23,42,0.07)` 边框），绝不出现含 `backdrop-filter` 的祖先容器做 `opacity < 1` 过渡动画。
2. **排版、控件与禁用红线**：
   - 全局字体严格使用 `Open Sans` + 中文回退（`<html data-script="cjk">`）；绝不使用衬线体 `EB Garamond`/`Cormorant Garamond`（其仅用于落地页分支 A）；
   - 主行动按钮一律用 **石板渐变 `#404859 -> #273042`** + `1px solid #12161f` + 内高光 `::before` + 按压态 `translateY(1px) scale(0.98)`（胶囊 `border-radius: 999px; corner-shape: round`）；
   - **Switch（例如“强制按原文档创建新账”、“本地 OCR 兜底”、“明暗主题”）** 是唯一承载品牌蓝渐变 `#4d7dcb -> #3462ad` 的控件；
   - **禁止出现实心红按钮**：驳回（`Reject`）、清空历史账单、删除自定义词典项均使用低调幽灵按钮/图标，点击弹出右上对齐 **Popconfirm**，弹窗确认按钮仍用石板 Primary；
   - 输入框/选择框使用 `rgba(15,23,42,0.04)` 灰槽，聚焦切换为蓝边 + `8%` 淡蓝底，**禁加 `3px` 外光晕**；筛选与导航选用 `#111827` 反相黑底白字或弹性白色分段滑块；
   - 图标统一 Remix Icon `-line`（`16px` / 紧凑 `14px`）；通知 Toast 固定顶部居中胶囊（`top: 24px`，仅错误用 `#991b1b`）；支持 `prefers-reduced-motion: reduce`。

### 1.2 视口布局与响应式规划（`1280×800` & `390×844`）
1. **顶部工具栏（`56px` 固定顶条）**：
   - 左侧：产品标识、当前批次下拉切换器（支持页面刷新恢复最近批次）、服务健康能力徽章（OCR 直连 / LLM 配置 / 线上或 `?mode=mock` 状态标签）；
   - 右侧：`导入复核包 (ReviewBundleV1)` 按钮、主数据管理抽屉入口（记账主体 / 费用词典 / 历史账单 / Harness 评测看板）、`同步写入基准库`（防重复计数显示）、**三口径导出菜单**（`official` 正式入账版 / `full_standard` 四态全量版 / `audit` 完整审计版，实时显示 `included_count` 与 `excluded_pending_count`，触发 `/export/download` 原生 JSON 文件下载）与主题切换图标按钮。
2. **桌面端三栏主工作区（`1280×800` 首屏预算，全屏内部滚动无页面整体横向滚动条）**：
   - **左栏（`310px` 队列与投递区）**：
     - 折叠/展开式投递卡：支持多格式拖拽上传（PDF/图/XLS/XLSX/DOC/DOCX/EML）、公网 URL 多行粘贴、**一键载入 4 份笔试真样本按钮**（`POST /api/v1/batches/demo`）、`OcrMode`（默认远程优先）选择；批处理进行中平滑轮询展示阶段进度条；
     - 记账主体选择器 + `切换主体` 触发框（切换时醒目提醒将按新主体即时重算 AR/AP 并重置原审批计数 `reset_approval_count`）；
     - 账单状态分段筛选器（`全部` / `待复核` / `新增` / `更新` / `无变化` / `已驳回`）与账单明细队列卡片（展示费用标准名、`amount + currency`、`AR/AP` 标签、匹配等级徽章、`BLOCKER`/`WARNING` 小标点）。
   - **中栏（弹性主栏 `minmax(440px, 1fr)` 全字段复核台）**：
     - **摘要与操作头**：当前状态胶囊、`match_level` 与 `matched_bill_ref`、AI 推理依据折叠摘要；
     - **全字段人工修订表单**：`charge_item`（附映射来源提示）、`raw_charge_name`、**全精度字符串金额输入框（`quantity`、`unit_price`、`amount` 严禁用 JS `Number` 四舍五入大数，保持文本字符串提交至后端 Decimal 解析！）**、ISO `currency` 选择器、`payment_term`（`PP`/`CC`/未定）、`posting_type`（`AR`/`AP`）、`document_type`、`reference_no`、`document_date`、MBL/HBL/Booking/Containers 标签编辑；支持 `保存并重算 (PATCH)` 与 `同单新增漏提费用行` 弹框；
     - **历史对比 `field_diffs` 面板**：清晰并排渲染变更字段的 `旧值 -> 新值`；
     - **歧义候选与人工指定绑定区**：当 `candidate_bills` 非空（或需手动重绑）时展示 `CandidateBillOption` 列表（单号 | 提单 | 费用 | 金额 | 币种 | 日期），一键选择绑定历史账单或开启“忽略历史新增一笔 (`force_create_new`)”开关；
     - **校验门禁与核准驳回区**：逐条展示 `BLOCKER` 与 `WARNING` 告警及建议修正值（如 ISO 6346 建议修正箱号一键填入）；**存在未解 `BLOCKER` 时明确禁用直接核准或展示不可核准红线说明，存在 `WARNING` 时弹出必填理由框提交 `/approve`，驳回按钮走 Popconfirm 必填驳回理由提交 `/reject`**。
   - **右栏（`340px` 溯源与异常文件抽屉）**：
     - Tab1 **文档树与异常恢复**：展示当前批次全文件/EML 附件树（区分 `PARSED` / `METADATA_ONLY` / `SKIPPED_NOISE` / `FAILED`）；对 `SKIPPED_NOISE`（如 `InsertPic`）提供 `强制纳入 OCR 解析` 按钮；对 `FailedDocumentAlert` 卡片（含关联受影响账单 ID 跳转）提供 `原地重试` 或 `上传替换文件`；支持单据级小计/税额/总计人工修正 `PATCH /documents/{doc_id}`；
     - Tab2 **源文证据与排除条款**：展示 `source_evidence` 路径与片段（含多入口归并列表 `source_paths`）、`excluded_clauses` 审计列表（如 `$25 per cancelled I.T.`、`PAID AMOUNT 0.00`、`Carrier Free H/C`、银行汇款段，让财务确认 AI 没有漏算真费用）；
     - Tab3 **审计轨迹**：`human_review_history` 审批与修改历史记录及 `retry_count` 自修复轨迹。
3. **移动端单栏工作流（`390×844`）**：
   - 自动收窄顶部工具栏并把三栏切换为顶部/底部固定分段切换器（`队列` | `复核` | `证据`），保证零水平溢出、触控区域 `>= 40px`、全部核心修订与导出操作可用。

### 1.3 代码目录与双轨 API 设计（`frontend/src/`）
```text
frontend/src/
  api/
    schema.d.ts            # 已有 OpenAPI 3.1 自动生成类型契约（只读不改）
    client.ts              # 统一 BillingApiClient 接口（live fetch 客户端 + 错误码 ErrorResponse 解析）
    mockClient.ts          # 合约一致内存 Mock 适配器（预置 4 样本、DN-202609-001 局部更新与冲突/驳回用例，支持切换主体、人工修订校验、422 BLOCKER 模拟、三口径导出）
  components/
    LumnoPrimitives.tsx    # StonePrimaryButton / SecondaryButton / GhostButton / Popconfirm / LumnoSwitch / SegmentedControl / PaperToast / Modal / CustomSelect
    TopBar.tsx             # 顶栏：批次选择、导出菜单、复核包导入、主数据入口、主题切换
    QueueSidebar.tsx       # 左栏：上传投递、实体切换、状态过滤与账单列表
    ReviewInspector.tsx    # 中栏：全字段修订、Diff对比、候选历史绑定、告警与Approve/Reject门禁
    EvidencePanel.tsx      # 右栏：文件树(强制OCR/替换重试/改总额)、ExcludedClauses、AI推理与审计历史
    ManagementDrawer.tsx   # 记账主体/费用词典/历史账单基准/Harness评测看板管理抽屉
  styles/
    lumno-tokens.css       # 已有
    continuous-corners.css # 已有
    workbench.css          # 分支 B2 画境舞台、三栏布局、响应式规则、微触感与纸面样式
  App.tsx                  # 主工作台状态协调与快捷键支持
```

### 1.4 小功能拆分预览（`T01`–`T03`）
- `T01`：**基础骨架、Lumno B2 视觉底座、双轨 API 与左栏投递/队列**：完成 `LumnoPrimitives`、`workbench.css`（挂载 `lumno-background.js`）、`client.ts`/`mockClient.ts`、`TopBar` 与 `QueueSidebar`（投递上传、4 样本演示触发、主体切换、队列过滤与基础只读概览），尽早可构建跑通桌面首屏视觉验收。
- `T02`：**中/右两栏完整复核闭环、全字段修订与三口径导出**：完成 `ReviewInspector`（全字段字符串精度编辑、`field_diffs`、`candidate_bills` 点选、手工加行、422 `BLOCKER` vs 带理由 `WARNING` 审批与 Popconfirm 驳回）与 `EvidencePanel`（文档树、强制 OCR 扫噪声图、替换重试、修正单据总额、`excluded_clauses` 审计与 `human_review_history`）以及三口径下载 & 基准幂等入库。
- `T03`：**主数据管理抽屉、Harness 评测看板、`ReviewBundleV1` 互通与移动端 `390×844` 精修**：完成 `ManagementDrawer`（实体/词典 CRUD、历史账单导入清理、评测列表与触发按钮）+ 移动双视口适配精修 + Playwright/Typecheck/Build 全检。

---

## 2. 边界异常与风险处理
1. **JS 浮点数精度破坏风险**：前端金额字段（`amount`, `unit_price`, `quantity`, `total_amount`）一律当作十进制文本 string 持有与编辑提交；展示层直接渲染原始格式化 string，避免 `"9007199254740991.01"` 在输入框被浏览器 `type="number"` 默默截为 `.00`！
2. **假安全按钮误点驳回/删除**：所有驳回与清库入口绝不用实心红按钮，一律强制经 Popconfirm 或二次确认框输入非空原因/确认。
3. **无后端状态下的独立并行验证**：借助 `?mode=mock` 或 `VITE_API_MODE=mock`（严格实现 `schema.d.ts` 所有请求/响应结构与 HTTP 422 BLOCKER 业务规则），允许在 `.worktree/B04-财务复核工作台前端` 中与 `B03` 后端完全并行地进行真实 Chromium `1280×800` 与 `390×844` 点击与视觉验收；后端合入后零改造切换至真实 `/api/v1`。

---

## 3. 异构方案审查与主对话裁决记录
- 审查模型：`codex`（`plan_critic_codex`，待并行审完后回填裁决）
- 裁决结论：待审查完成后补充。
