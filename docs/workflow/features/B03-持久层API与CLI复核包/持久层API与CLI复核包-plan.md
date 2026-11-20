# B03 SQLite 8表仓储、批处理分阶段服务、FastAPI全路由与Origin防护、Typer CLI与复核包互通 — 功能实现方案

- **关联锚点**：
  - PRD：`F-06`（工作台全字段复核、三口径导出、主数据维护与复核包互通）、`F-07`（命令行 `parse` 与评测接入），详见 [product-spec.md](../../product-spec.md)
  - 架构与安全约束：[arch-contract.md](../../arch-contract.md) §2.2（单向依赖：`api/cli -> services -> storage -> domain`；`services` 禁止导入 `harness`；`storage` 禁止导入 `gateways`）、§3–§5（原子事务、Origin 白名单防护、本地绑定保护）
  - 机器契约：[schema.sql](../../contracts/schema.sql)、[openapi.yaml](../../contracts/openapi.yaml)、[billing-output.schema.json](../../contracts/billing-output.schema.json)
- **范围与非目标**：
  - **本批次范围**：`storage/`、`services/`、`api/`、`cli/`，完整实现 8 张 SQLite 表仓储、批处理生命周期与重算闭环、`openapi.yaml` 全 **24 路径 / 29 HTTP 操作**（含 `/api/v1/batches/{batch_id}/reapply-dictionary` 与 `BillingRecordDetail.matched_bill_ref`/`inherited_fields`）、完整 `(scheme, host, port)` Origin 防护、无损 `Decimal` 入站解码与导出、CLI `parse`/`serve`/`eval`。
  - **非目标**：不改写 `B01`/`B02` 领域决策核心；不越界改动 `frontend/`；`B05` 完整评测用例集通过可注入 `eval_runner` 回调解耦接入，不在 `services/` 反向导入 `harness/`。

---

## 1. 模块与数据/接口改动设计

### 1.1 单向依赖与全链路无损 `Decimal` 约束
- **分层纪律（对齐 `test_config_and_domain.py` §423）**：
  - `storage/` 仅允许导入 `domain`, `config`, stdlib（`sqlite3`, `json`, `pathlib`, `contextlib`, `uuid`）；禁止导入 `gateways`, `parsers`, `agent`, `fastapi`。URL 展示脱敏调用 `domain` 纯函数或在 `services/` 边界完成。
  - `services/` 允许导入 `agent`, `parsers`, `gateways`, `storage`, `domain`, `config`；严禁导入 `harness`、`fastapi`。
  - `api/` 与 `cli/` 允许导入 `services`, `harness`（仅按需/可选注入）, `gateways`, `domain`, `config`。
- **入站＋出站全链路无损 `Decimal`（采纳审查 #13）**：
  - FastAPI 自定义请求解析辅助 `read_decimal_json_body(request)`：基于 `domain.loads_decimal_json(raw_body.decode("utf-8"))` 从原始 HTTP JSON 字节将未加引号的 JSON 数字直接解码为 `Decimal`，确保 `PATCH /records/{id}`、`POST /records`、`PATCH /documents/{id}`、`POST /existing-bills/import`、`POST /batches/import-review-bundle` 在校验和入库前零精度失真（`9007199254740991.01` 绝不退化为 `.0`）；
  - 出站响应：涉及金额的查询/导出详情及 `/export`（映射领域返回 `items` → `records` 契约字段）、`/export/download`（直接返回标准 6 键顶层 JSON 数组）统一采用 `DecimalJsonResponse`（底层使用 `dumps_decimal_json` 输出精确无引号十进制数字）。

### 1.2 SQLite 八表仓储与完整状态快照（`src/shipping_billing_agent/storage/`）
严格保留 `schema.sql` 8 张表结构、CHECK 约束、外键及新增兼容快照列：
1. **`storage/db.py`**：
   - `DatabaseManager(db_path: Path)`：`PRAGMA foreign_keys = ON`、`PRAGMA journal_mode = WAL`、`PRAGMA busy_timeout = 5000`，幂等执行 `schema.sql`；
   - 初始化默认种子数据：内置标准费用词典 `charge_dictionary`（对齐 `charge_normalizer.py`，`is_builtin = 1`）与默认记账主体档案（`NEPTUNE SHIPPING LIMITED` 含 `NEPTUNE SHIPPING LIMITED (LAX)`/`NEPTUNE`、`SUN TRACK EXPRESS (CHINA) CO., LTD.`、`TARGETING A INC`、`ZIM AMERICAN INTEGRATED SHIPPING SERVICES CO. LLC` 含 `ZIMAMERICAN INTEGRATED SHIPPING SERVICES CO. LLC`/`ZIM`、`ALL AMERICA FORWARDING, INC.`）；
   - **进程启动异常态自愈**：启动服务时将遗留处于 `PROCESSING` 且无活跃后台任务的孤儿批次标记 `status = 'PARTIAL_ERROR'`, `stage = 'FAILED'`，写明 `stage_message = '服务重启中断处理，可重试或切换样本恢复'`（采纳审查 #9）。
2. **`storage/repositories.py`**：
   - **完整状态双向水化（采纳审查 #2/#3/#10/#15）**：
     - `batches`：通过 `version` 实现并发乐观锁（`409 BATCH_CONFLICT`）；通过 `batch_existing_bills_json` 保存本批次独立随批传入或复核包携带的历史基准（绝不污染共享 `existing_bills` 表！）；通过 `failed_documents_json` 持久保存管线产出的全部 `FailedDocumentAlert`（覆盖解析虽为 `PARSED` 但 0 条费用行、含 `affected_record_ids`）；
     - `source_documents`：通过 `extraction_snapshot_json` 完整保存 `RawExtractionEnvelope` 快照（含规则原始汇总、分币种 `subtotals_by_currency`/`taxes_by_currency`/`totals_by_currency` 与文档级 `raw_parse_issues`/`excluded_clauses`），并在 `patchDocumentTotals` 时将人工修订映射到该快照对应字段与原 OCR 值备份；
     - `billing_records`：保留现有规范 SQL 列的同时，利用 `record_state_json` 完整序列化 `ProcessedBillingRecord` 的不可丢失内部状态：`explicit_fields`, `raw_invalid_fields`, `raw_parse_issues`, `raw_explicit_amount`, `raw_amount_abs`, `raw_amount_text`, `raw_unit_price_text`, `raw_quantity_text`, `is_credit`, `debit_credit`, `payer_hint`, `payee_hint`, `matched_bill_ref`, `matched_from_dictionary`, `candidate_bills`, `excluded_clauses`, `inherited_fields`, `source_values_before_overrides`, `source_doc_totals`, `package_sibling_refs`, `human_review_history`, `snapshot_matched_bill_id`（当命中随批隔离基准而非共享 `existing_bills` 时，SQL 外键列存 `NULL`，实际绑定 ID 与还原源由 `record_state_json` 保全！）。
   - **原地更新与外键不级联破坏（采纳审查 #1/#5）**：
     - 批次重算写回记录时采用 `INSERT ... ON CONFLICT(id) DO UPDATE` + 保留仍存在记录的 `id` 与外键（**严禁先 DELETE 整批记录再重插，避免触发 `baseline_sync_items` `ON DELETE CASCADE` 丢失已同步凭据**！仅清理因文档替换确已淘汰的多余旧行 ID）；
     - 所有嵌套路由查询强制以 `(batch_id, doc_id)` / `(batch_id, record_id)` 双字段锁定归属。
   - **单事务基线幂等同步（采纳审查 #1）**：
     - `sync_batch_to_baseline(batch_id, expected_version)` 在单个 `BEGIN IMMEDIATE` SQLite 事务内执行：筛选 `review_status in {AUTO_READY, APPROVED}` 且 `operation in {CREATE, UPDATE}` 的记录；核对三元唯一键 `(batch_id, billing_record_id, record_updated_at)` 若已存在则计 `skipped_count += 1`；否则对已通过 `baseline_sync_items` 先前以旧时间戳同步过的同一 `record_id`（即 `CREATE` 后被补改或 `UPDATE` 修订），复用原 `existing_bill_id` 原地升级版本（`version + 1`）记入 `updated_count`；对真正新增项插入 `existing_bills` 记入 `created_count`；最后写入 `baseline_sync_items(id, batch_id, billing_record_id, accounting_entity, record_updated_at, existing_bill_id, synced_at)`。

### 1.3 应用编排与复核闭环服务（`src/shipping_billing_agent/services/`）
1. **`services/batch_service.py`**：
   - 输入检验与落盘复用：调用 `B01 validate_batch_inputs` 对文件+URL 合计（≤50 项）、扩展名、URL SSRF 及流式大小（≤50 MiB）做前置把关（失败直接拒绝、不残余批次或文件）；安全原样字节落盘于 `app_data_dir/uploads/<batch_id>/<safe_doc_name>`（并在 `.eml` 解包提取附件后保留附件在受控目录的路径供重启后 `retry` / `force-parse` 使用，参考 `compute_retry_context`，采纳审查 #8）；
   - OCR 模式仅支持 `REMOTE_ONLY` 与 `REMOTE_WITH_LOCAL_FALLBACK`（移除契约外的 `LOCAL_ONLY`，采纳审查 #9）；
   - 支持随批 `existing_bills_json` 解析（`loads_decimal_json`）并与共享 `existing_bills` 库隔离（若随批传了 `existing_bills_json` 则本批仅用随批基准，否则读取共享库，采纳审查 #15）；
   - 真实分阶段状态推进：在拆包、OCR/LLM 抽取、匹配、校验与落库边界分别更新数据库 `batches.stage`（`QUEUED -> UNPACKING -> EXTRACTING -> MATCHING -> VALIDATING -> COMPLETED/FAILED`）与终态 `status`（全部成功 `COMPLETED`、部分文档失败或 `failed_documents` 非空 `PARTIAL_ERROR`、批次彻底失败 `COMPLETED`/`FAILED`），供 `wait=false`（HTTP `202`）异步轮询（采纳审查 #9）。
2. **`services/review_service.py`**：
   - **主体切换（`switch_entity`）**：校验 `expected_version`（可传）/ 事务重算，调用 `recalculate_for_entity(..., profiles, scoped_bills, charge_dictionary=..., bundles=...)` 并写回数据库，返回更新后 `BatchDetail` 与 `reset_approval_count`；
   - **词典变更当前批重标准化（`reapply_dictionary`）**：响应 `POST /api/v1/batches/{batch_id}/reapply-dictionary`，读取最新 `charge_dictionary`，在不修改其他批次、保留已有 `human_overrides` 与未跨主体有效决定前提下重新标准化当前批 `charge_item` 并重跑匹配与校验（采纳 `critic-B04` #7）；
   - **全字段修订（`patch_record`）**：严格按契约 `RecordPatchRequest` 接收字段（含 `issuer`, `recipient`, `document_type`, `reference_no`, `document_date`, `charge_item`, `quantity`, `unit_price`, `amount`, `currency`, `payment_term`, `posting_type`, `shipment_refs`, `matched_existing_bill_id`, `force_create_new`, `review_note`；不含 `resolved_alert_codes` 与 `raw_charge_name`，采纳审查 #4）；校验 `matched_existing_bill_id` 必须属于当前批有效基准且不可与 `force_create_new=true` 共存（#5）；区分未传字段与显式置空 `null`；全批原位重算并追加 `HumanReviewAction` 历史；
   - **手工新增漏提行（`add_manual_record`）**：校验证明 `source_doc_id` 隶属 `batch_id`，按规则规范化并合并整批重跑匹配与源单汇总对账；
   - **批准与驳回门禁（`approve_record` / `reject_record`）**：
     - 存在任何未解 `AlertSeverity.BLOCKER` 告警时，`approve` 严格拒绝返回 `422` 与错误码 **`HARD_BLOCKER_CANNOT_APPROVE`**（采纳审查 #4）；
     - 仅 `NEEDS_REVIEW`（或待审记录）进入审批：存在活跃 `WARNING` 时校验 `reason`（`2–500` 字符）并仅将**本记录当下活跃的 `WARNING`** 按 `(code, field, actual)` 精确标为 `RESOLVED`，再置 `APPROVED` 并刷新 `operation`（若全无告警且未批准亦可直接批准）；`reject` 强制要求 `2–500` 字符 `reason`，置 `REJECTED` 与 `reject_reason`；
   - **单据级修正、原地重试与强制扫图**：
     - `patch_document_totals`：修正 `subtotal_amount`/`tax_amount`/`total_amount`/`total_currency` 并同步更新 `extraction_snapshot_json`（包括单币种总计或按对应 `total_currency` 更新分币种映射）后全批重跑 `validate_batch`（采纳审查 #3）；
     - `retry_or_replace_document`：同机有原件走受控存储重建上下文重试，替换上传时先检扩展名与大小并通过 `charge_item + amount` 唯一匹配迁移 `human_overrides`（异库复核包无原件字节时返回清晰缺原件可操作错误指引补传/换 URL，采纳审查 #6/#8）；
     - `force_parse_skipped_document`：基于 `compute_retry_context` 与落盘字节对 `SKIPPED_NOISE` 内嵌图强制 OCR 并合并同包重算。
3. **`services/bundle_service.py`**：
   - 生成与校验契约一致的 `ReviewBundleV1`：**`bundle_version: "1.0"`**、`exported_at`、`accounting_entity`、`existing_bills_snapshot`（保留随批基准隔离）、`documents`（已脱敏展示）、`records`（包含完整内部审计与继承快照）（采纳审查 #7）；
   - 导入 `import_review_bundle`：校验版本 `"1.0"` 及主体/文档引用闭环后，在一个数据库事务中导入整批数据并按本批 `existing_bills_snapshot` 重新校验所有记录及恢复可证明仍成立的 `APPROVED`/`REJECTED` 状态，若中途异常完整回滚不留半成品批次（#7/#15）。

### 1.4 FastAPI 全路由、完整 Origin 白名单与 Typer CLI（`api/`, `cli/`）
1. **`api/security.py`**：对 `POST`/`PUT`/`PATCH`/`DELETE` 检查 `Origin`（无 `Origin` 时回退 `Referer` 解析 origin）：当存在头且 `(scheme, host, port)` 不在白名单集合（本请求目标 Host/Origin、`127.0.0.1:8000`、`localhost:8000`、`127.0.0.1:5173`、`localhost:5173`、`127.0.0.1:5174`、`localhost:5174` 及配置允许源）时，返回 `403 FORBIDDEN_ORIGIN`；无 `Origin`/`Referer` 的本地 CLI / `TestClient` 默认放行（采纳审查 #11）。
2. **`api/routers/`**：实现全部 **24 条路径 / 29 个 HTTP 操作**（原 23 路径 28 操作 + `/reapply-dictionary` POST），统一异常返回 `ErrorResponse`，挂载 `frontend/dist` 静态产物；`/api/v1/eval-runs` `GET` 读历史、`POST` 触发注入的 `eval_runner`（传 `run_mode`, `k_trials`）并返回当次结果或诚实 `UNVERIFIED`（采纳审查 #16/#17）。
3. **`cli/app.py`（Typer CLI）**：
   - `parse <paths...> [--url ...] --entity ... [--bills <file>] [--ocr-mode REMOTE_ONLY|REMOTE_WITH_LOCAL_FALLBACK] [--output-mode official|full_standard|audit] [--out <file>] [--bundle-out <bundle.json>] [--strict]`：
     - 支持文件、文件夹递归及公网 URL 输入；
     - 诊断信息（每个文档通道、提取条数、失败或待审摘要及“如何用复核包导入工作台”）输出到 **stderr**，标准 JSON 输出到 **stdout** 或 `--out` 文件（与机器消费解耦）；
     - 普通模式产出合法 JSON 返回退出码 `0`（除非完全无法读取或全部崩溃）；开启 `--strict` 时只要存在 `PENDING_REVIEW`、任何失败文件或导出异常即返回非零退出码（`1`/`2`）（采纳审查 #14）；
   - `serve --host 127.0.0.1 --port 8000`（复用 `AppSettings.allow_remote_bind` / `ALLOW_REMOTE_BIND`，禁止未授权监听非回环地址）；
   - `eval`（调用可选评测执行器并写入 `eval_runs`）与 `version`。

### 1.5 小功能拆分计划（对应 `持久层API与CLI复核包-tasks.md` `T01`–`T05`）
- `T01`：`storage/`（8 表初始化、默认种子、完整内部状态双向水化、`batches.version` 与单事务基线幂等三元同步、`eval_runs` 读写、单元测试）
- `T02`：`services/batch_service.py` 与 `services/review_service.py`（输入检验落盘、真实分阶段调度、主体切换重算、词典重标准化、全字段 PATCH、422 硬阻断/理由批准、单据总额修订、重试/强扫/加行、单元测试）
- `T03`：`services/bundle_service.py`、`api/security.py`、`api/routers/*`、`api/app.py`（`bundle_version: "1.0"` 跨库互通、完整 Origin 源防护、24 路径/29 操作 + 无损 `loads_decimal_json`/`DecimalJsonResponse` 导出）
- `T04`：`cli/app.py`（`parse --strict`、stderr/stdout 分离、`serve` 非回环拦截、`eval`、CLI ↔ Web 跨库 `ReviewBundleV1` 互通测试）
- `T05`：`tests/integration/test_b03_api_and_persistence.py`（覆盖 29 操作路由注册核对、PRD `F-06.AC1` 6 条混合三口径导出、`9007199254740991.01` 跨库 API/SQLite/下载全链路、真实样本 `wait=false` 阻塞恢复 `EXTRACTING` 测试、并发双写冲突、CLI vs 工作台独立基准隔离验证）

---

## 2. 边界异常与风险处理
1. **单事务基线防重与记录更新冲突**：`sync_batch_to_baseline` 采用 `BEGIN IMMEDIATE` 打包查验与写入；`billing_records` 更新永不先删后插以保 `baseline_sync_items`；批写接口检查 `batches.version` 防并发后提交覆盖前人修订。
2. **随批隔离基准与共享表 FK 冲突**：`batch_existing_bills_json` 或复核包快照中的账单命中记录，外键 `matched_existing_bill_id` 若指向非共享表 ID 则存 `NULL`，实际绑定 ID 保存在 `record_state_json` 并于重算/查看时透明恢复。
3. **CLI/API 精度与来源安全**：所有数值入口经 `loads_decimal_json`，所有写操作经完整 Origin 三元组校验。

---

## 3. 异构方案审查与主对话裁决记录
- 审查模型：`codex`（`plan_critic_codex`，输出详见 `.wf-tmp/critic-B03.md`）。
- 审查发现统计：共 17 项（阻塞实现 × 14、建议精简/修订与潜在风险 × 3）。
- 主对话裁决结论：**17/17 全部采纳落地**（同步微小增补 `schema.sql` 快照列与 `openapi.yaml` `reapply-dictionary` / `matched_bill_ref` / `inherited_fields` 保持 100% 机器契约一致，详见 `verify_contracts.py` 与 `schema.d.ts`）。
