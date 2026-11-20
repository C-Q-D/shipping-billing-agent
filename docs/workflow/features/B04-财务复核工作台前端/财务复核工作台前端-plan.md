# B04 Lumno B2 三栏/移动双视口 React 工作台、全字段复核、三口径导出与评测看板 — 功能实现方案

- **关联锚点**：
  - PRD：`F-06`（财务复核工作台三栏分工、全字段人工修订、三口径导出、主数据维护与复核包互通），详见 [product-spec.md](../../product-spec.md)
  - 界面原型规格：[prototypes/workbench/README.md](../../prototypes/workbench/README.md)
  - 视觉系统约束：`lumno-ui-style` **分支 B2（全屏玻璃/纸面工作台）**（见 `lumno-ui-style` §1–§4、[lumno-tokens.css](../../../../frontend/src/styles/lumno-tokens.css)、[continuous-corners.css](../../../../frontend/src/styles/continuous-corners.css)、[lumno-background.d.ts](../../../../frontend/src/lib/lumno-background.d.ts)）
  - 后端接口契约：[openapi.yaml](../../contracts/openapi.yaml)、强类型定义 [schema.d.ts](../../../../frontend/src/api/schema.d.ts)
- **范围与非目标**：
  - **本批次范围**：在独立工作树 `.worktree/B04-财务复核工作台前端` 的 `frontend/` 内完成 React 19 + TypeScript 工作台、Lumno B2 视觉与艺术画境挂载、`decimalJson.ts` 无损 JSON 数值编解码、单轨 `BillingApiClient`（默认直连 `/api/v1/*`，并配合测试专用网络桩用于无后端的独立浏览器/单测验收）、全部三栏/移动端功能闭环与多视口校验。
  - **非目标**：不在前端 TypeScript 内重写一份后端匹配/校验规则；不修改后端源码或提供契约不存在的“删除记账主体”功能。

---

## 1. 模块与界面改动设计

### 1.1 视觉系统与 `lumno-ui-style` 分支 B2 严格落地
1. **艺术画境背景与双玻璃材质（范式 B）**：
   - 根舞台挂载 `.lumno-oil-stage` 与 `mountProceduralLumnoBackground(container, { preset: "coast", mode: "oil-veil" })`，叠加三段凹形白雾遮罩，卸载调用 `handle.destroy()`；支持浅色/深色 `body[data-theme="dark"]` 与 `prefers-reduced-motion: reduce`；
   - 三栏主卡片采用轻透纸面/亚克力玻璃（浅 `rgba(255,255,255,0.86)` + `backdrop-filter: blur(18px)` + `1px solid rgba(15,23,42,0.07)`），严禁在含 `backdrop-filter` 的祖先容器做 `opacity < 1` 过渡动画。
2. **微触感控件与禁用红线**：
   - 全局使用 `Open Sans` + CJK 回退（`<html data-script="cjk">`），绝不使用衬线体；
   - Primary 按钮一律用 **石板渐变 `#404859 -> #273042`** + `#12161f` 边 + 内高光（胶囊 `999px; corner-shape: round`）；
   - **Switch**（如主题、兜底 OCR、强制新建独立账单）为唯一使用品牌蓝渐变 `#4d7dcb -> #3462ad` 的控件；
   - **禁用实心红按钮**：驳回（`Reject`）、清空旧账、删自定义词条均用 Ghost/图标按钮弹出右上对齐 **Popconfirm**，弹窗确认按钮用石板 Primary；
   - 输入框静息 `rgba(15,23,42,0.04)` 灰槽，聚焦切换蓝边 + `8%` 淡蓝底，无 `3px` 外发光；筛选选中用 `#111827` 反相黑底白字或白色弹簧滑块；Remix Icon `-line`（`16px`/`14px`）；顶部居中胶囊 Toast（`top: 24px`，仅错误态用 `#991b1b`）。

### 1.2 无损十进制 JSON 传输边界（采纳审查 #1）
- 契约规定 `quantity`、`unit_price`、`amount`、`subtotal_amount`、`tax_amount`、`total_amount` 在 HTTP 请求/响应中均为 **不带引号 JSON `number`**；直接用 JS `JSON.parse`/`JSON.stringify` + `Number` 会将 `9007199254740991.01` 静默截成 `9007199254740991`！
- 实现 **`frontend/src/api/decimalJson.ts`**：
  - **入站解码（`parseLosslessJson`）**：通过轻量精确 JSON 词法扫描，在把 JSON 文本交由解析或提取时，对大整数超安全范围（`abs > 9007199254740991`）或包含小数末尾精确微分的金额字段旁保留/转换为无损十进制文本，或用精确正则词法替换/保留原数字字面量字符串映射，保证 UI 状态与输入框严格持有精确字符串 `"9007199254740991.01"` 与 `"0.0025"`；
  - **出站编码（`stringifyLosslessJson`）**：基于精确标记类型 `RawDecimalLiteral(text)` 将有效十进制输入序列化为**不带引号的原样 JSON 数字字面量**（校验合法十进制语法 `[+-]?\d+(?:\.\d+)?`），满足 `schema.d.ts` `number` 契约与零精度损失，且通过单元测试专门验证 `9007199254740991.01` 与 `0.0025` 往返。

### 1.3 严格对齐 PRD 与原型的三栏＋移动端布局（采纳审查 #2/#3/#4/#5/#7/#8/#9/#11/#12/#13）
- **桌面端 `1280×800` 三栏严格职责（与 `product-spec.md` 第 215 行与 `workbench/README.md` 第 13 行 100% 一致）**：
  - **顶栏（`56px`）**：应用名 + 批次历史下拉选择器（刷新后按 URL/持久化批次 ID 自动恢复；切换批次前如有未保存草稿先弹窗提醒）、**记账主体切换器**（提醒会重置原跨主体审批计数 `reset_approval_count`；切前若有草稿先提醒）、能力健康标签（OCR 直连 / LLM 状态）、`导入复核包 (bundle_version: "1.0")`、`主数据与评测抽屉`、`写入基准库`（仅请求中防连点，支持首次/重复/补审后多次同步并清晰显示 `created_count`、`updated_count`、`skipped_count`）、**三口径导出面板**（切换 `official` / `full_standard` / `audit` 时实时调用 `/export?mode=...` 展示 `included_count`、`excluded_pending_count`、`excluded_rejected_count` 三个计数 + **预览/复制标准 JSON 数组** + 同口径 `/export/download` 原文件下载）与明暗主题切换。
  - **左栏（`310px`：投递与文件树）**：
    - 投递面板：多文件拖拽（前端校验 0 字节、单文件 >50 MiB、总项 >50 与非法 URL 并就地报错可移除后重提）、公网 URL 粘贴框、可选随批 `existing_bills_json` 上传（明确区分仅作用于本批而非写入全局历史库）、`OcrMode` 选择（默认 `REMOTE_ONLY`，配 `REMOTE_WITH_LOCAL_FALLBACK` 并清楚说明本地兜底限制）与`一键载入 4 份笔试真样本`，两种投递统一显式传 `wait=false`、当前主体与 OCR 模式，防连点并收 `202` 后按 `batch_id` 轮询；
    - 完整文档与附件树：区分 `PARSED` / `METADATA_ONLY` / `SKIPPED_NOISE` / `FAILED`；对 `SKIPPED_NOISE` 提供 `强制 OCR 提取` 按钮；对文档可触发 `重试/上传替换`，并展示与编辑 **单据级小计、税额、总额与 `total_currency`**（`PATCH /api/v1/batches/{batch_id}/documents/{doc_id}`，按 `(batch_id, doc_id)` 保存未提交草稿）。
  - **中栏（`340px`–`380px`：处理阶段、置顶失败卡与账单队列）**：
    - 顶部实时阶段进度条（`QUEUED -> UNPACKING -> EXTRACTING -> MATCHING -> VALIDATING -> COMPLETED/FAILED`，轮询刷新；若保存/修订先完成，通过请求序号屏蔽晚到的旧轮询响应！）；
    - **置顶失败文档告警卡（`failed_documents`）**：即使 `affected_record_ids` 为空（如坏附件零费用），也始终在队列最顶部可见且可直接点击`原地重试`或`替换上传`；点击有受影响记录 ID 时快速定位；
    - 账单状态分段筛选（`全部` / `待复核` / `新增` / `更新` / `无变化` / `已驳回`）与**账单明细卡片队列**（显示标准费用名、精度保留的金额+币种、`AR/AP` 标签、匹配等级徽章、`BLOCKER`/`WARNING` 徽章），以及`同单手工新增漏提行`入口。
  - **右栏（弹性主栏：原文证据、差异继承与全字段复核）**：
    - 切换选中记录时：**将右栏滚动条重置回顶部**，并按 `(batch_id, record_id)` 保留此前编辑尚未保存的草稿（切回原记录不丢草稿！仅在对应保存成功时清除该记录草稿）；
    - **证据与审计区**：**严格按纯文本（`textContent`/React 文本节点）安全渲染原文片段、错误原因与邮件正文**（即使含 `<script>`/`<img onerror=...>` 仍只呈文本标签）；展开展示 `source_paths`、`excluded_clauses`（`$25 cancelled I.T.` / `PAID AMOUNT 0.00` 等）、`ai_reasoning`、`inherited_fields`（明确标记哪几项由历史继承而非来自源单或 `field_diffs`）、`matched_bill_ref`，并支持按需拉取同批 `audit` 导出中按 `audit_trail.record_id` 关联的 `human_review_history` 与 `retry_count`（只展示次数）；
    - **全字段修订表单**（逐一映射 `RecordPatchRequest`）：`charge_item`、`raw_charge_name`（仅只读对照！不可冒充 PATCH 可写字段）、`quantity` / `unit_price` / `amount`（字符串精准编辑）、`currency`（ISO 列表）、`payment_term`（`PP`/`CC`/未定）、`posting_type`（`AR`/`AP`）、`document_type`、`reference_no`、`document_date`、**`issuer` 与 `recipient`**、`shipment_refs`（MBL/HBL/Booking/Containers）、`review_note`；仅发送用户实际改动的字段（严格区分“未修改不发”与“用户明确清空置 `null`”）；
    - **对比与匹配绑定区**：`field_diffs` 差异表、`candidate_bills` 候选单选列表（带业务单号/提单/费用/金额/日期）与`强制新增独立一笔` Switch；
    - **门禁与审批按钮区**：逐条列出 `BLOCKER` 与 `WARNING` 告警（附建议修正值如 ISO 6346 箱号一键填入）；仅当 `review_status === "NEEDS_REVIEW"` 且无未解 `BLOCKER` 时才允许点击`批准入账`（含 `WARNING` 时强制校验理由长度 **2–500 字符**）；点击`驳回排除`弹出右上 Popconfirm 填写 **2–500 字符** 驳回原因后确认提交。
- **管理抽屉与评测看板（`ManagementDrawer.tsx`）**：
  - 记账主体页签：查询、新增、编辑名称/别名（无删除实体按钮）；
  - 费用词典页签：查询、新增/更新别名映射、删除自定义映射（Popconfirm）及**`仅重新标准化当前批次` 动作**（调用 `POST /api/v1/batches/{batch_id}/reapply-dictionary`，不干扰其他批次及已有人工修订）；
  - 历史账单基准页签：查询、导入、按主体清空（Popconfirm）；
  - **Harness 评测看板页签**：展示空列表引导、“真值准确率 vs 多次运行稳定性 `pass_k`”双区；针对指标为 `null`、状态为 `UNVERIFIED` 或 `FAILED` 的条目明确显示 `"无结果"` + `error_message` 与实际 `k_trials` 试验次数（绝不伪显示成 `0%`！）。
- **移动端 `390×844`**：顶部分段器切换 **`投递与文件` | `账单列表` | `详情复核`** 三页视图，零横向溢出。

### 1.4 职责与文件结构 & 小功能拆分（`T01`–`T03`）
- `T01`：**基础骨架、Lumno B2 视觉、无损 JSON 边界、`ApiClient` 与左/中栏初版**：搭建 `decimalJson.ts`、`client.ts`（含测试可注入 HTTP 桩支持离线组件单测与独立浏览测试，不在前端写第二套校验逻辑）、`workbench.css` + `lumno-background.js`、`TopBar`、`QueueSidebar`（投递校验/演示/文件树）、`BatchQueuePanel`（阶段/置顶失败卡/账单列表）及移动端三段框架，`npm run typecheck && npm run build` 跑绿。
- `T02`：**右栏全字段复核台、草稿/竞态防护与三口径导出**：完成 `ReviewInspector`（`issuer`/`recipient`、只读 `raw_charge_name`、精准数字草稿缓存与滚动复位、并发旧轮询屏蔽、`field_diffs`、`inherited_fields`、`audit` 历史按需读取、422 `BLOCKER` 禁用与 2–500 字理由审批/Popconfirm 驳回）、文档级 `retry`/强扫/`patchDocumentTotals`（含 `total_currency`）与三口径预览/复制/下载及 `sync-baseline` 三态展示。
- `T03`：**主数据管理、`reapply-dictionary`、评测看板、`ReviewBundleV1` 互通与多视口回归**：完成 `ManagementDrawer`（全 4 页签）、`bundle_version: "1.0"` 导入、XSS 原文纯文本渲染检查、以及桌面 `1280×800` 与移动 `390×844` Playwright/前端测试全通（待 `B03` 就绪后再加跑真实服务暂停态 `EXTRACTING` 刷新恢复 E2E）。

---

## 2. 边界异常与风险处理
1. **精度、草稿与竞态**：通过 `decimalJson.ts` 保护 17+ 位及微小数数字字面量；通过 `(batch_id, record_id)` 独立草稿和单调请求版本号避免晚到轮询覆盖刚保存字段。
2. **失败文档零账单行可见性**：严格遵循 PRD 将失败文档告警置顶在中栏队列最前方，不绑定在右侧记录卡片上，确保 `affected_record_ids=[]` 的损坏附件同样一目了然可一键恢复。
3. **无后端独立开发阶段 Honest 状态**：在 `B03` 未合入前，`B04` 通过可替换传输桩跑通完整前端编译与静态自测；真实服务端 `EXTRACTING` 断线刷新与三口径真文件下载留待联调阶段跑实测。

---

## 3. 异构方案审查与主对话裁决记录
- 审查模型：`codex`（`plan_critic_codex`，输出详见 `.wf-tmp/critic-B04.md`）。
- 审查发现统计：共 13 项（阻塞实现 × 8、建议修订与潜在风险 × 5）。
- 主对话裁决结论：**13/13 全部采纳落地**（调整三栏职责严格匹配 PRD、补入 `decimalJson.ts`、规范 `RecordPatchRequest`/`ReviewBundleV1("1.0")`/`reapply-dictionary`/`BillingRecordDetail` 契约对齐）。
