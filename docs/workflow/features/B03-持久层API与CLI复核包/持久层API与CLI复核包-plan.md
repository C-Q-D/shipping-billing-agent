# B03 SQLite 8表仓储、批处理分阶段服务、FastAPI全路由与Origin防护、Typer CLI与复核包互通 — 功能实现方案

- **关联锚点**：
  - PRD：`F-06`（工作台全字段复核、三口径导出、主数据维护与复核包互通）、`F-07`（命令行批处理与评测接入），详见 [product-spec.md](../../product-spec.md)
  - 架构与安全约束：[arch-contract.md](../../arch-contract.md) §2.2（单向依赖）、§5.1–§5.3（本地绑核、Origin 防护、文件大小与数量限制、URL SSRF 与签名清理）
  - 机器契约：[schema.sql](../../contracts/schema.sql)、[openapi.yaml](../../contracts/openapi.yaml)、[billing-output.schema.json](../../contracts/billing-output.schema.json)
- **范围与非目标**：
  - **本批次范围**：`storage/`（SQLite 8 张表仓储与 Decimal 文本精度存取）、`services/`（批处理生命周期、主体重算、人工修订门禁、基线幂等入库、三口径无损导出与复核包互通）、`api/`（严格对齐 `openapi.yaml` 全 23 条 REST 路由、Origin CSRF 防护、资源上限校验、静态前端托管）、`cli/`（Typer `process`/`serve`/`review-bundle`/`eval` 子命令）。
  - **非目标**：不改写 `B01`/`B02` 领域决策逻辑；`B04` React 前端留在工作树独立交付；`B05` Harness >=10 用例评估矩阵由独立大功能按回调/仓储接口挂接，不在 `services/` 内反向导入 `harness/`（严格遵守 `test_config_and_domain.py` `_ALLOWED_LAYER_IMPORTS["services"]`）。

---

## 1. 模块与数据/接口改动设计

### 1.1 分层依赖与无损精度总约束
```text
api/ · cli/  (允许导入 services, harness, gateways, domain, config)
    │
    ▼
services/    (仅允许导入 agent, parsers, gateways, storage, domain, config；严禁导入 harness!)
    │
    ▼
storage/     (仅允许导入 domain, config, sqlite3/stdlib；严禁导入 agent/parsers/gateways/fastapi)
```
- **HTTP 与磁盘全链路 `Decimal` 无损**：
  - SQLite 列遵循 `schema.sql` 字符串存储规范（`decimal_to_db_text` 存，`parse_decimal_strict` 读）；
  - `/api/v1/batches/{batch_id}/export` 与 `/export/download` 等涉及高精度金额导出响应直接返回基于 `dumps_decimal_json` 的 `Response(..., media_type="application/json")`，禁止经由 FastAPI 默认 stdlib JSON `float` 序列化破坏 `9007199254740991.01` 或 `0.0025`。

### 1.2 SQLite 八表仓储（`src/shipping_billing_agent/storage/`）
1. **`storage/db.py`**：
   - `DatabaseManager(db_path: Path)`：自动建父目录、开启 `PRAGMA foreign_keys = ON` 与 `PRAGMA journal_mode = WAL`，加载并执行 `docs/workflow/contracts/schema.sql` 幂等建表脚本；
   - 初次初始化自动写入默认种子数据（幂等 `INSERT OR IGNORE`）：
     - 记账主体 `accounting_entities`：预置笔试题与常规测试实体（`NEPTUNE SHIPPING LIMITED` 含 `NEPTUNE SHIPPING LIMITED (LAX)`/`NEPTUNE`、`SUN TRACK EXPRESS (CHINA) CO., LTD.`、`TARGETING A INC`、`ZIM AMERICAN INTEGRATED SHIPPING SERVICES CO. LLC` 含 `ZIMAMERICAN INTEGRATED SHIPPING SERVICES CO. LLC`/`ZIM`、`ALL AMERICA FORWARDING, INC.`）；
     - 内置费用词典 `charge_dictionary`（对齐 `domain/charge_normalizer.py` 标准词条，`is_builtin=1`）。
2. **`storage/repositories.py`**：
   - 按实体/批次/单据/账单/基准/评测 8 张表封装强类型 CRUD 仓储：
     - `EntityRepository`：`list_all`, `create`, `update`, `to_profiles()`；
     - `DictionaryRepository`：`list_all`, `upsert`, `delete(entry_id)`（不允许误删非自定义或不存在返回 404），`to_items()`；
     - `ExistingBillRepository`：`list_by_entity`, `import_batch`, `clear(entity=None)`；
     - `BatchRepository`：`create_batch`, `update_stage`, `list_recent`, `get_batch_detail`；
     - `DocumentRepository`：`save_documents`, `get_document`, `update_document`, `list_by_batch`（存库前一律对 `public_url` 与展示路径做 `redact_url_query`）；
     - `RecordRepository`：`replace_batch_records`, `get_record`, `update_record`, `insert_record`（序列化保留 `explicit_fields`, `raw_invalid_fields`, `raw_parse_issues`, `source_doc_totals`, `package_sibling_refs`, `inherited_fields`, `source_values_before_overrides`, `human_overrides`, `human_review_history`, `excluded_clauses`, `retry_count`）；
     - `BaselineSyncRepository`：`has_synced(batch_id, record_id)`, `mark_synced(batch_id, record_id, existing_bill_id)`（依托 `baseline_sync_items(batch_id, record_id)` 唯一约束保证重放幂等）；
     - `EvalRunRepository`：`create_eval_run`, `list_eval_runs`。

### 1.3 应用编排与复核闭环服务（`src/shipping_billing_agent/services/`）
1. **`services/batch_service.py`**：
   - 构建外接适配器：`DocumentParserDispatcher`（按批次选择的 `OcrMode`：`REMOTE_ONLY` / `REMOTE_WITH_LOCAL_FALLBACK` / `LOCAL_ONLY`，并从环境变量/配置按需组装 `PltPltOcrClient` 与 `OpenAICompatibleLlmClient`）；
   - 上传接收校验：强制检查文件数 `<= limits.max_batch_files`（50）、单文件体积 `<= limits.max_file_bytes`（50 MiB），文件落盘到 `app_data_dir/uploads/<batch_id>/` 下，文件名先过 `sanitize_filename`（仅用安全 ASCII/安全名+UUID 前缀防路径穿越）；URL 输入前执行 `validate_public_url` SSRF 检查；
   - 支持同步执行（`wait=true`）与后台执行（`wait=false`，借助进程内有界任务/AnyIO 调度，推进 `QUEUED -> UNPACKING -> EXTRACTING -> MATCHING -> VALIDATING -> COMPLETED/PARTIAL_ERROR` 阶段供前端轮询）；
   - `POST /api/v1/batches/demo`：安全加载 `笔试题AIAGENT/` 中的 4 份真实样本，若目录不存在返回结构化 404/配置错误卡片。
2. **`services/review_service.py`**：
   - **主体切换（`switch_entity`）**：调用 `recalculate_for_entity(..., profiles=..., existing_bills=..., charge_dictionary=..., bundles=...)`，持久化更新记录并返回 `reset_approval_count`；
   - **全字段人工修订（`patch_record`）**：更新 `human_overrides`（涵盖 `charge_item`, `raw_charge_name`, `quantity`, `unit_price`, `amount`, `currency`, `payment_term`, `posting_type`, `document_type`, `reference_no`, `document_date`, `issuer`, `recipient`, `matched_existing_bill_id`, `force_create_new`, 运输号及人工绑定 `resolved_alert_codes` 等），通过同批全记录 + 原文档包重跑匹配与确定性校验保存，并将审计记录追加 `human_review_history`；
   - **手工补录漏提行（`add_manual_record`）**：允许基于某源文档新增费用明细行（携带源文件证据）并全单重跑匹配与总额对账；
   - **批准门禁（`approve_record`）**：
     - 存在任何未解决 `AlertSeverity.BLOCKER` 告警时**严格拒绝并抛出 422 业务错误**（`UNRESOLVED_BLOCKER`），绝不通过修改告警状态绕过硬红灯；
     - 存在未解决 `AlertSeverity.WARNING` 时要求提供非空 `reason`（至少 2 字符），将对应 `WARNING` 标为 `AlertSeverity.RESOLVED` 记入 `human_review_history`，把记录置 `ReviewStatus.APPROVED` 与相应 `CREATE`/`UPDATE`/`NO_CHANGE`（若无告警直接置 `APPROVED`）；
   - **驳回排除（`reject_record`）**：要求非空 `reason`，设置 `review_status = REJECTED` 与 `reject_reason`，正式口径与全量标准口径导出将其排除，审计口径保留；
   - **文档级人工纠偏、重试与强制解析**：
     - `patch_document_totals`：修正单据级 `subtotal_amount`/`tax_amount`/`total_amount`/`total_currency` 并重跑 `validate_batch`；
     - `retry_or_replace_document`：原地重跑失败文件或接收上传新文件替换破损件并局部重算（保持该文档其他记录既有有效 `human_overrides`）；
     - `force_parse_skipped_document`：对 `SKIPPED_NOISE` 内嵌图片（如 `InsertPic_C25B.jpg`）强制送 OCR 提取并合并进同包重算；
   - **导出服务（`export_batch`）**：复用 `domain/serializer.py`（`official` / `full_standard` / `audit`），返回统计汇总及合法 6 键 JSON 数组字符串（通过 `dumps_decimal_json`）。
3. **`services/baseline_service.py`**：
   - 将本批 `review_status in {AUTO_READY, APPROVED}` 且 `operation in {CREATE, UPDATE}` 的记录写入 `existing_bills`：
     - 依托 `baseline_sync_items(batch_id, record_id)` 跳过已同步项保证幂等（`synced_count` vs `skipped_count`）；
     - `CREATE` 插入新旧账（`version = 1`），`UPDATE` 仅更新变更字段并递增 `version = version + 1`。
4. **`services/bundle_service.py`**：
   - 构造与校验强约束 `ReviewBundleV1`（带 `schema_version="review-bundle/v1"`、`exported_at`、当前 `accounting_entity`、脱敏后源文档元信息、完整记录与人工修订锁），供 CLI 导出后由 `/api/v1/batches/import-review-bundle` 完整恢复进数据库与工作台继续操作（严防路径穿越/非法字段注入）。

### 1.4 FastAPI 全路由、Origin 防护与 Typer CLI（`src/shipping_billing_agent/api/`, `cli/`）
1. **`api/security.py`**：
   - 对所有状态变更路由（`POST`/`PUT`/`PATCH`/`DELETE`）：检查 HTTP `Origin`（若缺省回退 `Referer`）头部，一旦存在但主机不属于本机回环（`127.0.0.1`、`localhost`、`[::1]`）即返回 `403 FORBIDDEN_ORIGIN`，阻止第三方网页 CSRF 跨站调用本地接口。
2. **`api/routers/`**：完整实现 `openapi.yaml` 全部 23 条路由，统一异常处理输出 `ErrorResponse(code, message, details, request_id)`；当 `frontend/dist/index.html` 存在时以 `StaticFiles` 挂载同源单页工作台；新增 `POST /api/v1/eval-runs` 支持接收可注入 `eval_runner` 或读取/执行仓储评测记录。
3. **`cli/app.py`（Typer CLI）**：
   - `shipping-billing-agent process <paths...> --entity ... --bills ... --ocr-mode ... --extractor-mode ... --output-mode ... --out ... --bundle-out ...`
   - `shipping-billing-agent review-bundle export --batch-id ... --out ...` / `import <bundle.json>`
   - `shipping-billing-agent serve --host 127.0.0.1 --port 8000 [--allow-remote-bind]`（显式禁止未授权 `--allow-remote-bind` 监听非回环地址）
   - `shipping-billing-agent eval --mode offline|live`（供 `B05` 挂载或独立评测触发）。

### 1.5 小功能拆分预览（`T01`–`T05`）
- `T01`：`storage/`（SQLite 8 表、`db.py`、`repositories.py`、种子数据与持久化单元测试）
- `T02`：`services/batch_service.py`、`review_service.py`（上传拆包、后台分阶段执行、切换主体、全字段人工修订、422 硬红灯拦截、重试/强扫/总额纠偏、测试）
- `T03`：`services/baseline_service.py`、`bundle_service.py` 与 `api/security.py`、`api/routers/*`（全部 23 条路由、Origin 防护、无损 Decimal 导出）
- `T04`：`cli/app.py`（Typer 完整命令组）与端到端 CLI ↔ API ReviewBundle 互通测试
- `T05`：FastAPI `TestClient` + SQLite + 真实样本离线全路由/安全性/幂等性集成测试

---

## 2. 边界异常与风险处理
1. **精度截断风险**：`fastapi.responses.JSONResponse` 默认调用 stdlib `json.dumps` 会把 `float` 截断大数字；必须使用自定义 `DecimalJsonResponse`（底层调用 `dumps_decimal_json`）或规范十进制导出响应。
2. **后台批处理与 SQLite 并发写锁风险**：启用 SQLite `WAL` 模式、`busy_timeout=5000`，仓储写操作集中短事务提交；异步后台批次不跨线程裸传未加锁同一 SQLite `Connection` 实例。
3. **硬红灯误核准与重复入库风险**：`/approve` 严格按 `AlertSeverity.BLOCKER` 拒返 HTTP 422；`sync-baseline` 依托 `baseline_sync_items` 唯一键保证幂等；`--allow-remote-bind` 未明传前禁止监听 `0.0.0.0`。

---

## 3. 异构方案审查与主对话裁决记录
- 审查模型：`codex`（`plan_critic_codex`，待并行审完后回填裁决）
- 裁决结论：待审查完成后补充。
