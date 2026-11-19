# 前端界面规格指令：航运智能账单复核工作台（Lumno B2）

> **交付终点**：终点 A（自包含前端界面规格指令）。本文件与 [REST 接口契约](../../contracts/openapi.yaml)、[产品需求文档（PRD §F-06）](../../product-spec.md) 同为 `B04-财务复核工作台前端` 编码与 `browser_tester` 盲盒验收的唯一界面依据。
> **默认视觉底座**：严格锁定 `lumno-ui-style`（目标表面分支 **`B2`：印象派粉彩画境衬底 ＋ 磨砂白玻璃工作面板**），直接消费根目录复制入前端的 `frontend/src/styles/lumno-tokens.css`、`frontend/src/styles/continuous-corners.css` 与 `frontend/src/lib/lumno-background.js`。严禁另起配色、另造圆角规范或使用未授权深蓝/实心红按钮。

---

## 1. 真实用户视角推导（第一次打开页面的首屏预算与心智模型）

### 1.1 首屏黄金视口预算与双视口布局铁律
| 视口规格 | 布局形态 | 首屏（零滚动）必须直接可见 | 滚动隔离策略（禁止双滚动条套娃） |
| :--- | :--- | :--- | :--- |
| **桌面主流屏 `1280×800`** | `52px` 单行顶栏 + **`100dvh` 锁高三栏 Grid**（`296px` 左栏 / `minmax(420px, 1fr)` 中栏 / `436px` 右栏，栏间 `12px`，外围留白 `12px`） | ① 单行顶栏（主体、批次、导出主行动）<br>② 左栏单据拖拽区、`一键载入 4 份真实样本` 与 `开始智能解析` 石板主按钮<br>③ 中栏阶段条 + 状态筛选 + 前若干账单卡<br>④ 右栏当前焦点详情与底部操作条 | 外层容器 `#root` / `.wb-shell` 固定 `height: 100dvh; overflow: hidden;`；仅左、中、右各自的内容区设 `min-height: 0; overflow-y: auto;`，绝不出现整页滚动条套局部滚动条。 |
| **移动巡检屏 `390×844`** | `52px` 紧凑顶栏 + 顶部 **3 槽 Segmented Control**（`投递与文件` / `账单列表` / `详情复核`） + 单栏主体 | 首次进入默认激活`投递与文件`或已有活跃批次时切至`账单列表`；对应标签下拖拽选文件、`开始智能解析`、账单卡关键金额/状态及底部操作条零横向裁切 | 单栏内容区占满剩余 `100dvh` 高度独立滚动；顶栏次要按钮自动收拢进`更多（...）`门户菜单，保证 `390px` 宽度下零横向溢出（`scrollWidth <= clientWidth`）。 |

### 1.2 真实财务操作路径（盲测动线校验）
1. **首次开箱试跑（0 前置文档）**：进入工作台即在左栏看见可点击的虚线文件投递区与显著的次行动按钮**`一键载入 4 份真实样本`** → 点击后顶部真实阶段条按 `UNPACKING → EXTRACTING → MATCHING → VALIDATING → COMPLETED` 逐步点亮 → 合规条目直接进入 `CREATE`（无需手动审批）→ 点顶栏石板主按钮**`导出正式标准 JSON`** 即可看到精确包含/排除计数并下载符合标准数组的 JSON。
2. **主体视角即时翻转**：在顶栏主体选择器将 `NEPTUNE SHIPPING LIMITED` 切换为 `ALL AMERICA FORWARDING, INC.`（或自定义主体），前端调用 `/switch-entity` 后立即更新全批 `AR/AP`；若原主体下存在人工关联或批准记录，在列表中栏顶部弹出可关闭提醒条明确告知重置计数。
3. **文件失败兜底恢复**：若某份 PDF/图片解析返回 `FAILED`（哪怕产出 0 条费用），左栏文件树与中栏顶部同时弹出文件级失败告警卡片，财务人员可直接点击**`原地重试`**或**`选择替换文件`**恢复单文件而不丢失同批其余单据结果。
4. **待复核修订闭环与草稿防串台**：点选`待复核`卡，右栏顶部先列 AI 推理依据与 `BLOCKER`/`WARNING` 告警（点击告警直跳高亮对应表单域）；修改记录 A 尚未点保存时切去记录 B，A 的未保存改动独立暂存在内存中并在 A 卡片显示`未保存`圆点，绝不串入 B。

### 1.3 去“程序员味”与零长 ID 铁律
- **主界面严禁裸露长 UUID / 哈希字符串 / 英文异常堆栈**：顶栏批次器显示 `B-1119-01 · 3/4 文件 · 待核 2`，文档树显示清洗后的真实文件名，账单卡显示 `Handling Charge`、`raw_charge_name`、单据号（如 `DC-108478`）或提单号（如 `MAEU275752922`），告警显示人话中文（例如“明细求和 1410.40 USD 与原文总计 1400.00 USD 存在 10.40 差额”）。
- **全局运行时能力状态降噪**：仅在顶栏品牌左侧保留 **1 枚紧凑能力状态点**（悬停 Tooltip 显示 `LLM: deepseek-flash 就绪 / 未配置` 与 `OCR 网关: 直连就绪`），不在卡片、副标题或输入框下堆砌重复说明。

### 1.4 单一焦点真源与面板联动契约
- **焦点状态**：`activeBatchId`（刷新后从 `/api/v1/batches` 首项或 `localStorage("wb_last_batch_id")` 自动恢复）、`activeDocFilterId`（左栏文件树点击过滤中栏）、`selectedRecordId`（中栏当前选中账单行）。
- **滚动条复位规则**：每当 `selectedRecordId` 发生变化（包括中栏点击切换或保存修订后刷新当前批次无变化以外的新焦点），必须在微任务后强制执行 **`rightPaneBodyRef.current?.scrollTo({ top: 0, behavior: "auto" })`**。
- **独立草稿隔离**：使用 `draftsByRecordId: Record<string, RecordDraftPatch>` 与 `docTotalsDrafts: Record<string, DocumentTotalsPatch>` 分别按 `record.id` / `doc.id` 键控保存未提交的表单修改；保存成功后删除对应 `draftsByRecordId[recordId]`；尝试切换批次、切换记账主体或导入 CLI 复核包时，若 `Object.keys(draftsByRecordId).length > 0`，弹出确认框防止未保存改动丢失。

---

## 2. `lumno-ui-style`（`B2` 表面分支）精确视觉参数

### 2.1 外层画境衬底与凹形雾化遮罩
1. **全屏底层 `<div className="wb-bg-stage" ref={bgStageRef} aria-hidden="true">`**：
   - `position: fixed; inset: 0; z-index: 0; pointer-events: none; overflow: hidden;`
   - 组件挂载时调用 `mountProceduralLumnoBackground(bgStageRef.current, { preset: theme === "dark" ? "nocturne" : "coast", mode: "oil-veil", seed: 2026 })`，卸载时调用 `handle.destroy()`。
   - 保留 `theme` 切换（浅色默认 `data-theme="light"`，支持切换 `data-theme="dark"` 并同步传给 `body.dataset.theme`）。
2. **三段凹形白雾呼吸层（`.wb-bg-veil`）**：
   - 浅色模式：`background: linear-gradient(180deg, rgba(255, 255, 255, 0.58) 0%, rgba(255, 255, 255, 0.24) 42%, rgba(255, 255, 255, 0.44) 100%);`
   - 深色模式：`background: linear-gradient(180deg, rgba(15, 20, 26, 0.56) 0%, rgba(15, 20, 26, 0.28) 42%, rgba(15, 20, 26, 0.58) 100%);`

### 2.2 Acrylic 磨砂工作面板与连续超椭圆曲率
- **主工作栏 & 顶栏容器（`.lumno-glass-panel`）**：
  - 浅色：`background: rgba(255, 255, 255, 0.78); backdrop-filter: blur(22px) saturate(160%); -webkit-backdrop-filter: blur(22px) saturate(160%); border: 1px solid rgba(15, 23, 42, 0.08); box-shadow: 0 8px 24px rgba(15, 23, 42, 0.07), inset 0 1px 0 rgba(255, 255, 255, 0.65); border-radius: var(--lumno-radius-dialog);`（`18px`，`continuous-corners.css` 自动增强 `superellipse(1.25)`）。
  - 深色：`background: rgba(24, 26, 32, 0.76); border-color: rgba(255, 255, 255, 0.12); box-shadow: 0 10px 28px rgba(0, 0, 0, 0.36), inset 0 1px 0 rgba(255, 255, 255, 0.08);`
- **中/右栏内部卡片（`.lumno-paper-card`）**：
  - 浅色：`background: rgba(255, 255, 255, 0.90); border: 1px solid rgba(15, 23, 42, 0.07); border-radius: 14px;`
  - **合成组红线**：严禁对任何带有 `backdrop-filter` 的父级包裹层使用 `opacity < 1` 渐变入场动画，入场微动画只允许作用于独立卡片或用纯位移完成。

### 2.3 排版、数字对齐与 Remix Icon 契约
- 正文字体 `font-family: var(--lumno-font-sans)`，基础字号 `13px`，行高 `1.45`。
- 全部金额、数量、单价、计数字段统一 `font-variant-numeric: tabular-nums;`; 提单号/箱号/单据号辅标使用 `var(--lumno-font-mono)`。
- 图标全部采用本地依赖 `remixicon` 的 **`-line` 线性图标**（默认 `16px`，紧凑按钮/内嵌图标 `14px`，`aria-hidden="true"`），仅主题切换月亮等允许极少数 `-fill`。

### 2.4 按钮体系、唯一蓝控件 Switch 与破坏性 Popconfirm 铁律
| 控件角色 | 类名与材质规格 | 严禁项 |
| :--- | :--- | :--- |
| **Primary 主按钮** | `.lumno-btn .lumno-btn-primary .lumno-pill`：背景 `var(--lumno-color-primary)`、边框 `1px solid var(--lumno-color-primary-border)`、白字 `var(--lumno-color-on-primary)`、`border-radius: 999px`、`min-height: 34px`（头部紧凑 `32px`）、`::before` 内高光 `--lumno-shadow-primary-sheen`；`:active:not(:disabled)` 执行 `translateY(1px) scale(0.98) brightness(0.95)`（`120ms ease`）；`[aria-busy="true"]` 下文字 `opacity: 0`（不卸载防宽度跳变），`240ms` 延迟显出居中 `12×12px` 旋转环。 | **严禁亮蓝、紫或纯黑平面做主按钮**。 |
| **Secondary 次按钮** | `.lumno-btn .lumno-btn-secondary .lumno-pill`：浅色 `#ffffff`（悬停 `#f8fafc`）+ 三层贴身微影 + 顶 `0.75` 白高光。 | 不做高饱和渐变填充。 |
| **Ghost 辅助按钮** | `.lumno-btn .lumno-btn-ghost`：透明底、`rgba(107, 114, 128, 0.90)`，悬停 `var(--lumno-color-control-hover)`。 | — |
| **破坏性操作**（驳回记录 / 清空历史库） | **Ghost 按钮触发行内 Popconfirm**（通过 `createPortal` 挂载 `document.body`）：Popconfirm 面板 `border-radius: 12px; padding: 12px; min-width: 240px`，内含确认原因输入与右对齐 **Secondary `取消` + Slate Primary `确认执行`**。 | **全站严禁红色实心或红色渐变按钮**；红色仅限校验错误文字 `#dc2626`（深色 `#fca5a5`）及错误 Toast `#991b1b`。 |
| **Switch 开关**（唯一品牌蓝控件） | 紧凑版 `36×20px` / 标准 `44×24px`（`.rounded-full` 豁免超椭圆），`::after` 承载 `--lumno-color-accent` 淡入，`280ms cubic-bezier(0.32, 0.72, 0, 1)` 滑动，按下旋钮宽度 `+4px`（选中态左回补保持右沿齐）。 | 不将 Switch 蓝色梯度复用到按钮或徽标上。 |
| **Input / Select 触发器** | 浅灰填充 `var(--lumno-color-control-bg)` + `1px solid var(--lumno-color-control-border)`；hover 与 focus 统一变为 `var(--lumno-color-input-focus-bg)` + `var(--lumno-color-focus-ring)`。 | **严禁**白底粗灰边框，**严禁** `box-shadow: 0 0 0 3px` 外发光晕。 |
| **Portal 浮层防裁切**（Custom Select / Popconfirm / Tooltip） | 全部浮层通过 `ReactDOM.createPortal(..., document.body)` 渲染（`z-index: var(--lumno-z-menu)` / `var(--lumno-z-dialog-menu)`），打开与 resize/scroll 时按 `getBoundingClientRect()` 计算视口坐标并在靠近底部/右侧时自动向上/向左翻转。Menu 外框 `border-radius: 16px; padding: 5px`，选项严格 `border-radius: 10px`（`16 - 1 - 5 = 10px`），**选中项用实心反相深色底白字 `--lumno-color-selected` / `--lumno-color-selected-text`**，弹出曲线 `360ms cubic-bezier(0.2, 1.45, 0.35, 1)`。 | **严禁**将下拉菜单、Popconfirm 或 Tooltip 直接渲染在 `overflow: hidden/auto` 栏内导致裁切；**严禁**选中项画蓝底+对勾；Tooltip **不用黑底、不带小三角**。 |
| **Toast 通知** | 固定 `top: 24px; left: 50%; transform: translateX(-50%)` 的全胶囊微渐变纸面；成功/提示为纸面白底黑字，报错态用 `#991b1b` 白字；默认 `2400ms` 自动收起。 | **严禁**放到右下角或用矩形方块，**严禁**绿色成功背景。 |

---

## 3. 页面信息架构与区块设计（严格对齐 `openapi.yaml`）

### 3.1 单行紧凑顶栏（`52px` 高，`<= 56px`）
从左到右排列，字距紧凑、垂直居中：
1. **品牌标识与运行灯**：`ri-ship-2-line` + `航运账单智能体` + 1 枚 `8px` 状态圆点（绿=LLM 与网关就绪；琥珀=LLM 未配置/仅规则兜底；鼠标悬停 Portal Tooltip 展示 `GET /api/v1/health` 详情）。
2. **批次选择器（Portal Custom Select）**：展示最近 20 个批次的 `short_code` · `accounting_entity` · `counts` 摘要；下拉末尾附固定动作项`导入 CLI 复核包...`（打开 `ImportBundleModal`）。
3. **当前记账主体选择器（Portal Custom Select）**：直接选定或切换默认记账主体（展示 `canonical_name`，悬停/副标展示 `aliases.join(" / ")`）；选中不同主体且当前有活跃批次时，调用 `POST /api/v1/batches/{batch_id}/switch-entity` 并即时刷新批次与重置提示。
4. **右区操作组**：
   - `主数据 / 历史库` Secondary 按钮（带数字徽章显示已有历史账单条数，打开 `MasterDataDrawer`）；
   - `Harness 评测` Secondary 按钮（打开 `EvalDashboardDrawer`）；
   - `theme` 轻切换图标钮（Ghost，切日间雾海 `coast` / 夜间月夜 `nocturne`）；
   - **`导出标准 JSON`**（Slate Primary 主行动按钮，打开 `ExportModal`；无活跃批次时 disabled）。

### 3.2 左栏：多格式投递与上下文文件树（桌面 `296px`）
1. **多格式单据投递卡**：
   - **当前生效记账主体回显标签**（与顶栏同源，可输入临时自定义主体名，校验 `1~120` 字符）。
   - **拖拽 / 点击多文件选择区**（支持 `.eml,.pdf,.jpg,.jpeg,.png,.tif,.tiff,.xls,.xlsx,.doc,.docx`，即时前端校验：不支持扩展名、0 字节、单文件 `> 50MB`、总量 `> 50` 个文件均在该框下方红色提示并禁止提交，支持逐个移除待传文件）。
   - **折叠“公网 URL 投递”面板**：支持多行粘贴公网 PDF/图片链接（随表单 `public_urls_json` 提交）。
   - **OCR 通道策略 Switch**：标签`本地安全兜底解析（RapidOCR + pdfplumber）` + Info Tooltip 解释：`关闭 = 仅提交至指定远端 OCR 网关（本地二进制未配公网中转将提示阻断并可重试）；开启 = 本地 PDF/图片离线兜底提取并打标来源`。
   - **随批已有账单基准条**：展示`已挂载 N 条工作台历史账单`或选附 `.json` 基准文件（提交时作 `existing_bills_json` 或复用工作台持久化库）。
   - **投递行动组**：
     - **`开始智能解析`**（Slate Primary，调用 `POST /api/v1/batches?wait=false`，点击瞬间置 `aria-busy="true"` 防连点，返回 `202` 后立即置入 `activeBatchId` 开启阶段轮询）；
     - **`一键载入 4 份真实样本`**（Secondary，空态与平时均可快速调用 `POST /api/v1/batches/demo?wait=false`，自动带入当前选中记账主体与当前 Switch `ocr_mode`）。
2. **上下文文件树（`documents: SourceDocumentSummary[]`）**：
   - 按 `parent_doc_id` 组织层级（EML 邮件作为父节点，内部附件与内嵌图缩进挂在下方）。
   - 每个节点展示：文件格式线性图标（`ri-mail-line` / `ri-file-pdf-2-line` / `ri-file-excel-2-line` / `ri-file-word-2-line` / `ri-image-line`）、`sanitized_filename`、通道徽标（如 `远端OCR` / `本地兜底` / `Excel` / `Word` / `邮件正文` / `签名图已跳过` / `仅元数据` / `解析失败`）。
   - **特殊交互状态**：
     - `status === "SKIPPED_NOISE"`：显示灰底斜体说明与行内 Ghost 按钮**`强制纳入解析`**（调 `POST .../documents/{doc_id}/force-parse`）；
     - `status === "FAILED"`：红色描边高亮 + 错误摘要 + 行内 **`重试`** 按钮与隐藏 `<input type="file">` 触发的 **`替换文件`** 按钮（调 `POST .../documents/{doc_id}/retry`）；
     - 点击任意已解析节点切换 `activeDocFilterId` 筛选中栏该文件产出的费用记录（再点一次取消筛选）。

### 3.3 中栏：真实阶段进度、失败告警与分状态账单流（桌面 `minmax(420px, 1fr)`）
1. **顶部实时阶段进度条（仅展示 `batches.stage` 真实后端推进）**：
   - 5 节点水平步骤胶囊：`收件拆包 UNPACKING` → `多模态提取 EXTRACTING` → `匹配与继承 MATCHING` → `校验门禁 VALIDATING` → `结果就绪 COMPLETED`（或 `FAILED`）。
   - 前端轮询逻辑：当 `batch.stage` 属于活跃态时每 `500ms` 拉取 `GET /api/v1/batches/{batch_id}`（组件卸载或切换批次自动清理定时器），同时显示真实 `stage_message`；**严禁使用 `setTimeout` 假进度动画伪造阶段跃迁**。
2. **置顶文件级失败告警卡片（`failed_documents.length > 0` 时显示）**：
   - 列出每一份失败 `filename`、`error_message` 与关联提示（如“同封邮件的正文费用已保留，请重试或替换此附件以补齐提单上下文”），提供直接触发**`重试`**与**`上传替换`**的按钮。
3. **跨主体切换重置横幅（当最近一次 `/switch-entity` 的 `reset_approval_count > 0` 时展示）**：
   - 提示文案：已切换记账主体，重算全批 `AR/AP` 与历史匹配并自动重置 `N` 条原主体下的人工关联/批准态待核，可手动关闭。
4. **弹簧滑动计数筛选器（Segmented Tabs）**：
   - 6 档状态：`全部 (all)`、`待复核 (pending_review)`、`新增 (create)`、`更新 (update)`、`无变更 (no_change)`、`已驳回 (rejected)`；
   - 若已选 `activeDocFilterId`，在其旁渲染可一键清除的文件过滤徽章；右侧显示快速汇总（成功/失败文件数、当前列表合计/待定提示）。
5. **账单记录卡片列表（`records`）**：
   - 每卡仅呈现业务可读要素（`<= 4` 个视觉层级，禁放长 UUID）：
     - 第一行：左为 `charge_item`（加粗） + `(raw_charge_name)`（小号静音灰） + 贷项徽标（若 `amount < 0` 或 `document_type === "Credit Note"`） + `未保存` 草稿琥珀圆点；右为 `tabular-nums` 带符号金额与 `currency`；
     - 第二行：操作徽标（`CREATE` / `UPDATE` / `NO_CHANGE` / `PENDING_REVIEW`） + 命中级别徽标（`L1`~`L4` / `未匹配` / `多候选待选`） + `PP`/`CC` + `AR`/`AP`/`待核` + 校验摘要（`校验通过` / `自修复通过` / `N 项告警`）；
     - 第三行：单据类型与编号 `reference_no` · 主/分提单摘要 · `issuer → recipient`。
   - 选中态使用高对比左边框与白底微抬阴影（深色用微亮边界）。

### 3.4 右栏：单条溯源、差异对比与全维度人工裁决（桌面 `436px`）
每次 `selectedRecordId` 改变时强制将滚动容器滚回顶部（`scrollTop = 0`）。
1. **顶部焦点摘要与人工锁标**：
   - 展示 `charge_item`、`operation`、`review_status`；若 `Object.keys(human_overrides).length > 0`，展示锁定标签`含人工修订锁定字段（重验不会被原文本覆写）`。
2. **AI 推理依据与确定性告警区**：
   - 列示每条 `validation_alerts`：
     - **`BLOCKER`（硬红灯）**：明确红色图标与中文错误详情（含 `expected` vs `actual`），点击自动聚焦并短暂闪烁对应表单输入域；
     - **`WARNING`（软告警）**：琥珀色图标与中文建议（如箱号校验码疑似 `0/O` 误读、单据未印币种等），允许财务核对原图后“确认原文特例并批准”；
     - **`RESOLVED`**：静音灰删除线/已解标。
   - 列示 `ai_reasoning[]` 完整逻辑链。
3. **`UPDATE` 前后字段差异面板（`field_diffs.length > 0` 时展示）**：
   - 分为“本次变更字段（高亮 `old_value → new_value`）”与“严格继承原账单未变字段”。
4. **多候选历史账单消歧面板（`candidate_bills.length > 0` 或已绑定历史账单时展示）**：
   - 列出所有 `CandidateBillOption`（`label` / `reference_no` / `charge_item` / `amount currency` / `document_date`），财务可单选绑定到其中一条，或点击**`另建为新增记录（不关联历史账单）`**随修订一并提交。
5. **全维度人工复核表单（确保 PRD 每一种阻断均可在此关闭）**：
   - 输入/选择字段全部受 `draftsByRecordId[record.id]` 托管：
     - `charge_item`（支持输入并快捷选用词典标准名）、`raw_charge_name`（只读对照）、`amount`（数字，支持负数冲减）、`currency`（3 位大写 ISO 代码）、`payment_term`（Segmented/Select: `PP` / `CC` / `空 null`）、`posting_type`（`AR` / `AP` / `空 null`）、`quantity`、`unit_price`；
     - `document_type`、`reference_no`、`document_date`（`YYYY-MM-DD`）、`issuer`、`recipient`；
     - `mbl_no`、`hbl_no`、`booking_no`、`container_nos`（逗号分隔输入，提示 ISO 6346 格式）；
     - **`review_note`** 备注输入框。
   - **单据级总计/税额修订卡（可折叠展开，针对隶属 `source_doc_id`）**：
     - 允许修改当前单据原文识别出的 `subtotal_amount`、`tax_amount`、`total_amount`、`total_currency`，点击**`保存单据汇总并重验`** 调用 `PATCH /api/v1/batches/{batch_id}/documents/{doc_id}`，解决“明细行金额都对但 OCR 识错印章 total”的阻断。
   - **补充漏提费用行入口**：按钮**`+ 从该单据补充费用行`**，弹出 `Blur-in Modal` 提交 `POST /api/v1/batches/{batch_id}/records`。
6. **吸底操作动作栏**：
   - **`保存修订并重验`**（Slate Primary）：仅当存在未保存修改、选定候选历史账单或强制新建时激活（未改也可触发单条重验），调 `PATCH /api/v1/batches/{batch_id}/records/{record_id}`；
   - **`确认特例并批准`**（Secondary）：仅当记录处于 `NEEDS_REVIEW` 且不存在任何 `severity === "BLOCKER"` 告警时可点击（硬红灯时明确 `disabled` 并通过 Portal Tooltip 说明`存在未解决硬性数值或平账错误，请先修正表单或驳回`）；点击后打开紧凑 Popover/内联原因栏（>=2 字）提交 `POST .../approve`；
   - **`驳回排除`**（Ghost，无红色实心底）：点击弹出 **Portal Popconfirm** 输入驳回原因（默认`非真实计费/确认无效`）确认执行 `POST .../reject`。
7. **原文溯源、多源提单标识与已排除非计费条款区（默认展开摘要、可折叠原文片段）**：
   - 展示来源文件名、页码/工作表与行号、原始文本片段 `source_evidence.snippet`；
   - 若同上下文存在多个 `shipment_refs.mbl_nos`（如 `OOLU4055331281 (EML正文)` vs `HZMHK331281 (MBL附件)`），逐条附来源标签并排展示；
   - 展示当前所属文档的 `excluded_clauses[]`（被排除的免责违约金条款、免除项如 `Carrier Free H/C` 等），确保财务一眼确认智能体没有误删漏提。
   - **安全铁律**：全部原文、邮件 HTML 摘录与报错信息严格使用 React 文本节点渲染，**严禁 `dangerouslySetInnerHTML`**。

---

## 4. 弹窗与抽屉交互规范（Blur-in Modal，`border-radius: 18px`）

1. **三口径导出与基准同步弹窗（`ExportModal`）**：
   - 顶部分段器选择 3 种导出模式：
     - **`official` 正式标准 JSON**：仅包含合格可入账终态（`CREATE`/`UPDATE`/`NO_CHANGE` 及已批准记录），自动排除 `PENDING_REVIEW` 与 `REJECTED`；
     - **`full_standard` 四态全量标准 JSON**：严格按题目第 5 节结构导出含 `PENDING_REVIEW` 在内的全部非驳回条目；
     - **`audit` 完整审计版 JSON**：保留全部标准字段 + `source_evidence` + `validation_results` + `excluded_clauses` + `human_overrides` + 含 `REJECTED` 记录及原因。
   - 实时从 `GET /api/v1/batches/{batch_id}/export?mode=...` 读取计数并在顶部醒目横条播报：**“本次将导出 `X` 条记录，自动排除 `Y` 条未决待复核与 `Z` 条已驳回记录”**，下方附带语法等宽 JSON 预览窗。
   - 底部操作：
     - **`同步已确认记录到历史库`**（Secondary，调 `POST .../sync-baseline`，成功 Toast 提示新增/更新/幂等跳过条数）；
     - **`复制 JSON`**（Secondary）；
     - **`下载标准 JSON`**（Slate Primary，触发浏览下载 `/api/v1/batches/{batch_id}/export/download?mode=...`）。
2. **主数据与已有历史账单抽屉（`MasterDataDrawer`）**：
   - Tab 1 **记账主体档案**：列表展示各主体 `canonical_name`、`aliases`、默认徽标；支持新增/编辑主体及别名数组（`/api/v1/entities`）。
   - Tab 2 **费用标准映射词典**：展示内置与自定义别名→标准名映射表（标注内置不可误删 vs 自定义可删除）；支持新增词条，修改后一键回跑当前批次重算；支持点击切换或删除自定义词条（Ghost + Popconfirm）。
   - Tab 3 **已有历史账单基准库**：展示当前库内历史账单表（过滤按主体/全部）、支持从文件上传或粘贴 JSON 导入（`POST /api/v1/existing-bills/import`），提供`清空历史库` Ghost 按钮 + **Popconfirm** 确认后调用 `DELETE /api/v1/existing-bills`。
3. **CLI 复核包导入弹窗（`ImportBundleModal`）**：
   - 支持选择或粘贴 `ReviewBundleV1` JSON，本地预检 `bundle_version === "1.0"` 及必含数组，提交 `POST /api/v1/batches/import-review-bundle` 并在工作台打开该批次继续人工复核。
4. **Harness 评测看板抽屉（`EvalDashboardDrawer`）**：
   - 调用 `GET /api/v1/eval-runs` 展示历史评测记录；
   - **空数据诚实态**：无评测数据时明确提示`尚未运行过 Harness 评测基准`并仅展示触发按钮，不伪造任何示例高分；
   - **双轨指标分开渲染**：
     - 轨道一（确定性真值质量）：JSON Schema 合规率、关键字段 Exact Match、AR/AP 判定准确率、四态决策准确率、异常冲突拦截召回率；
     - 轨道二（多次采样稳定性）：`pass@k` 与 `pass^k`（标注 `actual_trials / k_trials`）；
     - 若选定记录 `status === "UNVERIFIED"` 或 `"FAILED"`，如实显示红字状态与 `error_message`（不把 null 渲染成 0% 误导）。
   - 顶部提供 **`运行离线基准评测`**（Primary，`run_mode: "OFFLINE_DETERMINISTIC"`）与 **`运行联机 LLM 稳定性评测 (k=3)`**（Secondary，`run_mode: "LIVE_LLM"`，无密钥时由后端真实记为 `UNVERIFIED`）。

---

## 5. 开发实现与验证落地约束（交接 `frontend_implementer` 与 `browser_tester`）

1. **类型真源**：所有 API 请求与响应类型必须引用运行 `npm --prefix frontend run gen:api` 自 `openapi.yaml` 产出的 `frontend/src/api/schema.d.ts`，禁止在组件中手抄不一致接口结构。
2. **文件结构建议**：`frontend/src/` 内划分 `api/client.ts`（统一封装 JSON / FormData 上传及 `ErrorResponse` 解析）、`components/lumno/`（Portal `CustomSelect`, `Popconfirm`, `Tooltip`, `Switch`, `SegmentedTabs`, `Modal`, `ToastBanner`）、`components/workbench/`（`TopBar`, `IngestionPanel`, `DocumentTree`, `StageProgressBar`, `RecordList`, `RecordDetailPanel`, `ExportModal`, `MasterDataDrawer`, `EvalDashboardDrawer`, `ImportBundleModal`, `ManualRecordModal`）。
3. **独立隔离浏览器双视口验收清单**（严禁共用本机其他项目的浏览器实例，必须独立临时 `--user-data-dir` 与端口启动）：
   - `1280×800` 下验证：`wb-shell` 无整页滚动、左/中/右三栏各自可独立滚动、背景油画 + 雾化层可见、任意下拉/Popconfirm 均不被裁切；
   - `390×844` 下验证：顶栏无溢出、3 槽分段切换流畅、输入投递与主按钮首屏直达；
   - 真实交互验证：轮询阶段推进、一键载入 4 样本、切换主体重置提示、草稿切换不串台、Popconfirm 驳回与三口径导出计数一致。
