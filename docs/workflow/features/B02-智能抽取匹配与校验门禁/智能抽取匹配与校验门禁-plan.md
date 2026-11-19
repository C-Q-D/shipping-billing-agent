# B02 航运账单LangGraph编排、AR/AP与贷项判定、四级匹配继承、确定性校验与1轮自修复 — 功能实现方案

- **关联锚点**：
  - PRD：`F-02`（多维提取与标准化）、`F-03`（记账主体视角与 AR/AP/贷项冲减）、`F-04`（四级匹配、局部更新继承与四态决策）、`F-05`（确定性校验门禁、冲突检测与受控自修复），详见 [product-spec.md](../../product-spec.md)
  - 架构与安全约束：[arch-contract.md](../../arch-contract.md) §1.3–§1.4（`json_mode` + 1 轮自修复）、§2.2（单向依赖：`domain` 零外部 IO；`agent` 不读写磁盘/DB）、§5.1 & §5.3（Agent Security 5 风险面）
  - 机器契约：[billing-output.schema.json](../../contracts/billing-output.schema.json)、[schema.sql](../../contracts/schema.sql)、[openapi.yaml](../../contracts/openapi.yaml)
- **范围与非目标**：
  - **本批次范围**：纯领域建模、标准化、匹配继承、确定性校验与三口径序列化（`domain/`），以及智能抽取、规则预抽、并发配额控制与 LangGraph `StateGraph` 编排（`agent/`），实现从 `B01` 产出的 `ParsedDocument[]` + `accounting_entity` + `existing_bills[]` + `charge_dictionary[]` → `BatchPipelineResult`（全精度 `Decimal`、`operation`、`review_status`、`field_diffs`、`validation_alerts`、`ai_reasoning`、`source_evidence`、`excluded_clauses`、`failed_document_alerts`）及严格对齐 6 键 Schema 的三口径导出。
  - **非目标（留归后续批次）**：SQLite 8 张表持久层、FastAPI HTTP 路由、Typer CLI（`B03`）；Lumno B2 前端（`B04`）；Harness >=10 评测运行器（`B05`）；真联网外部联调（Phase 5 显式标注 `UNVERIFIED`）。

---

## 1. 模块与数据/接口改动设计

### 1.1 单向依赖约束（严格遵守 `arch-contract.md` §2.2）
```text
domain/ (models, charge_normalizer, entity_resolver, matcher, validators, serializer)
  ▲  仅允许依赖 Python 标准库 (decimal, json, re, datetime, hashlib, typing, enum, dataclasses)
  │  + pydantic + iso4217 + jsonschema（仅校验契约接口内按需） + domain 内部
  │  严禁导入 pathlib.Path/open 写盘、sqlite3、httpx、fastapi、langgraph、agent/*
  │
agent/ (prompts, rule_extractor, graph)
  └── 仅允许依赖 domain/, config/limits.py, gateways/llm_client.py, gateways/text_sanitizer.py,
      anyio, langgraph, langchain_core
      禁止导入 sqlite3, fastapi, httpx, parsers/*, storage/*；不管理外部传入 LLM client 的关闭生命周期
```

### 1.2 扩展领域模型与无损/宽松解析轨道（`src/shipping_billing_agent/domain/models.py`）
在已有 `RawDocumentExtraction` / `RawChargeItem` / `ParsedDocument` / 枚举基础上补充强类型模型（保留既有字段与 `extra="forbid"`，并增设双轨显式非法值追踪）：

1. **错误码与严重度固定映射表（对齐 `schema.sql` / `openapi.yaml`）**：
   - 统一定义 `AlertCode`（`StrEnum`）及固定 `ALERT_SEVERITY_MAP`：
     - **`BLOCKER`（硬红灯；`B03` `/approve` 存在未 `RESOLVED` 的 `BLOCKER` 返回 HTTP `422`）**：
       - `LINE_ARITHMETIC_MISMATCH`（`abs(qty * unit_price - raw_line_amount) > Decimal("0.02")`）
       - `NEGATIVE_NON_CREDIT_AMOUNT`（非贷项单据出现负数金额）
       - `INVALID_OR_MISSING_CURRENCY`（缺币种或含非 ISO-4217 字符串如 `"ZZZ"`）
       - `INVALID_DOCUMENT_DATE`（显式传入非合法历法日期如 `"2026-02-30"`）
       - `ENTITY_NOT_MATCHED`（当前记账主体与收付各方不匹配或仅见对端且无归属）
       - `AMOUNT_SUBTOTAL_TAX_MISMATCH`（小计/税额/含税总额对账差超 `Decimal("0.02")`）
       - `AMOUNT_TOTAL_MISMATCH`（明细总额与源单明确总额，含显式 `0.00`，差超 `Decimal("0.02")`）
       - `MULTI_CURRENCY_TOTAL_AMBIGUITY`（同单多币种混杂却仅有单一汇总总额）
       - `OCR_NUMERIC_CONFUSION`（原始数值串含 `0/O`/`1/I/l`/`5/S` 混淆或双小数点如 `"1.310.40"`）
       - `AMBIGUOUS_EXISTING_BILL`（匹配命中多条无法确定双射的历史账单或跨币种缺币种歧义）
       - `DUPLICATE_LINE_EXTRACTED`（同单完全同位置/同内容重复抽行导致超出源单总额）
       - `CROSS_DOCUMENT_CONFLICT`（跨根文件或 EML 与子附件就同费用/单号存在金额/币种/条款冲突）
       - `LLM_EXTRACTION_FAILED`（LLM 结构化抽取/自修复异常失败且未能产有效结果）
     - **`WARNING`（软告警；落入 `PENDING_REVIEW`，允许人工填写理由后批准并标为 `RESOLVED`）**：
       - `CONTAINER_ISO6346_MISMATCH`（第 11 位校验码不符，并附基于 `0/O`、`1/I`、`5/S` 等候选替换建议）
       - `OCR_SUSPICIOUS_FLAG`（上游 `ParsedDocument.ocr_payload` 中某条目带 `suspicious` 标记）
       - `MULTI_SOURCE_BL_MISMATCH`（多附件携带不同 MBL/HBL 且无法自动归并对齐）
       - `CONTEXT_INFERRED_FIELD`（由同包上下文自动补全的运输/条款参考字段，待确认）
     - **`RESOLVED`**：人工审阅在工作台逐条或带理由确认解除后的状态。
2. **显式字段与原始非法串双轨模型（防崩溃与防局部继承洗白）**：
   - 在 `RawChargeItem` / `RawDocumentExtraction` 上**非破坏性**扩充默认空字段（或通过 `RawExtractionEnvelope` 承载）：
     - `debit_credit: Literal["DEBIT", "CREDIT", "UNKNOWN"] = "UNKNOWN"`
     - `raw_amount_text: str | None = None`、`raw_unit_price_text: str | None = None`、`raw_quantity_text: str | None = None`
     - `explicit_fields: set[str] = Field(default_factory=set)`（区分“增量通知刻意省略” vs “明确给出了字段（即使格式错而落为 `None`）”）
     - `raw_invalid_fields: dict[str, str] = Field(default_factory=dict)`（保存非法原始值，如 `{"currency": "ZZZ", "document_date": "2026-02-30"}`）
     - `raw_parse_issues: list[ValidationAlert] = Field(default_factory=list)`
     - `payer_hint: str | None = None`、`payee_hint: str | None = None`
     - `subtotal_amount: Decimal | None = None`、`tax_amount: Decimal | None = None`、`total_amount: Decimal | None = None`、`total_currency: str | None = None`、`totals_by_currency: dict[str, Decimal] = Field(default_factory=dict)`、`has_explicit_total: bool = False`、`excluded_clauses: list[ExcludedClause] = Field(default_factory=list)`
   - 提供 **`parse_llm_raw_extraction(raw_text: str | None, parsed: RawDocumentExtraction | None = None)`**：
     - 对原始 LLM JSON 使用 **`json.loads(..., parse_float=Decimal, parse_int=Decimal)`** 解析（确保 `9007199254740991.01` 与 `0.0025` 全程不经过 IEEE-754 `float` 失真）；
     - 若某字段含 `"ZZZ"`、`"2026-02-30"` 或 `"1.310.40"` 等非法串，**绝不在构造模型时抛 `ValidationError` 崩整单**，而是把目标强类型字段置 `None`，将键写入 `explicit_fields` 与 `raw_invalid_fields`，并生成对应 `ValidationAlert`；
     - 无任何 `None` 覆盖“真正省略”的语义：只有 JSON 中确有非空值/非 null 键值且非显式 null 才进入 `explicit_fields`。
3. **主数据、状态记录与失败摘要模型**：
   - `AccountingEntityProfile(canonical_name, aliases, is_default=False)`
   - `ChargeDictionaryItem(raw_alias, standard_name, category="general", is_builtin=False)`
   - `ExistingBillRecord`（对齐 `schema.sql` `existing_bills` 全部列及 `Decimal` 数值）
   - `ValidationAlert`、`FieldDiff`、`CandidateBillSummary(id, label, reference_no, charge_item, amount, currency, document_date, match_level)`、`SourceEvidence(source_file, source_doc_id, source_paths, page, sheet, row, line_number, extraction_channel, snippet)`
   - `HumanReviewAction(action, operator, reason, timestamp, overrides)`
   - `FailedDocumentAlert(doc_id, filename, reason, affected_record_ids)`（显式告知上层与前端，对应 `openapi.yaml` `FailedDocumentAlert`）
   - `ProcessedBillingRecord`：完整账单记录，字段名与 `schema.sql` `billing_records` / `openapi.yaml` `BillingRecordDetail` 一一对齐（`id` 即记录主键 `record_id`，含 `matched_bill_ref`, `matched_existing_bill_id`, `candidate_bills`, `human_overrides`, `human_review_history`, `excluded_clauses`, `raw_explicit_amount`, `retry_count`）。
   - `BatchPipelineResult`：`accounting_entity`, `documents`, `records`, `failed_document_alerts`, `failed_doc_ids`, `llm_calls_used`, `warnings`。
   - **全链路无损 `Decimal` JSON 编解码 helpers**：`loads_decimal_json(text)` 与 `dumps_decimal_json(obj)`（定制 JSON 编码器直接把有限 `Decimal` 写为**无引号十进制数字字面量**，如 `9007199254740991.01`、`0.0025`，禁止 `NaN`/`Inf` 与科学计数法失真）。

### 1.3 费用与条款标准化（`src/shipping_billing_agent/domain/charge_normalizer.py`）
1. **内置费用词典 + 自定义覆盖**：大小写折叠、空白归一化与首尾标点清洗，自定义词典优先于内置词典；完整覆盖题面 6 类与 4 份真实样本全部常见费名（`Handling Charge`, `Documentation Fee`, `Terminal Handling Charge`, `Trucking Charge`, `Customs Clearance Fee`, `Storage Charge`, `Wharfage`, `Wharfage Administration Fee`, `Detention Free Time Charge`, `On Carriage Additional` 等），保留 `raw_charge_name` 与 `matched_from_dictionary` 标记。
2. **付款方式与日期严格解析**：
   - `normalize_payment_term`：`P`/`PP`/`PREPAID`/`FREIGHT PREPAID` → `PaymentTerm.PP`；`C`/`CC`/`COLLECT`/`FREIGHT COLLECT` → `PaymentTerm.CC`；非明确指代返回 `None`（绝不靠猜将海运提单的 `FREIGHT PREPAID` 强加到独立本地代理 `H/C` 收费上）。
   - `normalize_document_date`：将 `YYYY-MM-DD`、`MM/DD/YYYY`、`MM-DD-YYYY`、`YYYY/MM/DD` 转换为严格历法合法 `YYYY-MM-DD`；若传入非法日期串则写入 `raw_invalid_fields["document_date"]` 并返 `(None, err)`。

### 1.4 记账主体视角、AR/AP 与贷项符号判定（`src/shipping_billing_agent/domain/entity_resolver.py`）
1. **严格主体匹配与别名表**：基于 `AccountingEntityProfile(canonical_name, aliases)` 执行核/分支缀归一化（例如移除 `,`/`.`/尾缀 `(LAX)` 比对），**绝不使用未在配置或当前包绑定的随意短子串去碰瓷其他公司**。
2. **资金流向判定与严格贷项归因**：
   - 方向：`payer_hint`/`payee_hint` 同时明确时优先按 `payee_hint -> creditor`, `payer_hint -> debtor`；否则 `issuer -> creditor`, `recipient -> debtor`。同时支持“单边明示收款人”（如 `DC-108478` 仅显式标明 `GRAND TOTAL BALANCE DUE TO NEPTUNE SHIPPING LIMITED (LAX)`）：若当前主体为 `NEPTUNE SHIPPING LIMITED`，精确归为 `PostingType.AR`；若当前主体为其他无关公司（如 `ALL AMERICA FORWARDING` 或 `COSCO`），**绝不猜测 Shipper/Consignee 是付款人**，输出 `posting_type = None` + `BLOCKER` `ENTITY_NOT_MATCHED`！
   - **严禁负金额反推贷项**：`is_credit` **仅当** `debit_credit == "CREDIT"` 或 `document_type` 明确属于贷项凭证（如 `Credit Note` / `C/N` / 贷项通知）时才为 `True`。非贷项遇到 `raw_amount < 0` 一律保持负值并触发 `BLOCKER` `NEGATIVE_NON_CREDIT_AMOUNT`。
   - **贷项行符号冲减与 AR/AP 保持**：当 `is_credit` 为真且确定 `posting_type in {AR, AP}` 时，保留原台账边（如我司向供应商核减仍为 `AP`，我司对客户开具贷项仍为 `AR`），先记录原始正绝对值用于行算术校验，再将账单标准净额设为 `-abs(raw_amount)`，在 `ai_reasoning` 写入 `"贷项冲减：对应台账余额减少 <abs_amount>"`。
3. **主体切换即时重算（`recalculate_for_entity`）**：
   - 切换目标主体时**彻底清除**旧主体绑定的 `matched_existing_bill_id`、`matched_bill_ref`、`candidate_bills`、`match_level`、`APPROVED`/`REJECTED` 审批结果、派生 `posting_type` 及历史继承覆写字段，仅保留用户手动编辑原始业务字段的锁（如原始费用名/原始金额），随后用新主体重跑 `entity_resolver -> matcher -> validators`，返回更新后记录与 `reset_approval_count`。

### 1.5 四级匹配引擎、确定性多行双射继承与三口径序列化（`domain/matcher.py`, `domain/serializer.py`）
1. **严格主体隔离过滤**：仅筛选 `entity_matches(bill.accounting_entity, target_entity, profiles)` 为真的 `existing_bills`，绝不跨主体借用别家公司历史账单字段。
2. **四级严格优先级匹配与缺币种增量保护**：
   - 顺序：`L1_DOC_CHARGE`（有效 `reference_no` + `charge_item`）→ `L2_BL_CHARGE_CURR`（有效非空 `mbl_no`/`hbl_no` + `charge_item` + 币种）→ `L3_BOOKING_CHARGE_CURR`（有效非空 `booking_no` + `charge_item` + 币种）→ `L4_CNTR_PARTY_DATE_AMT`（非空 `container_nos` 交集 + 双方归一化名称 + `document_date` + 金额 `<= Decimal("0.02")`）。空单号/空提单号/空订舱号永远不能匹配空字段。
   - **L2/L3 缺币种增量规则**：先按 `(entity, valid_bl_or_booking, charge_item)` 捞出全量候选集（先把所有历史候选按 `bill.id` 确定性升序排列）。若新行合法省略了 `currency`（`currency is None` 且不在 `raw_invalid_fields`）：
     - 若候选池存在 `> 1` 种不同币种 → **立即中止并判定 `MatchLevel.AMBIGUOUS_MULTI`**，填充全部候选供人工点选，**绝不拿去按金额挑唯一或向下一级降级**；
     - 仅当候选池仅含单一币种时允许保留并交下一步双射配对并继承该历史币种。
   - 若新行带 `currency`（或 `raw_invalid_fields` 含非法币种如 `"ZZZ"`）：`currency` 按精确值比对；非法 `"ZZZ"` 无法命中 L2/L3 后若无命中则为 `CREATE` 但最终被门禁标为 `PENDING_REVIEW`。
   - **第一级有候选即停止向下级降级**。
3. **确定性多行配对（`DC-108478` 等同单同名多行）**：
   - 将所有历史候选稳定按 `bill.id` 排序，绝不依赖集合随机顺序或输入列表顺序。
   - 针对同文档内同一 `(source_doc_id, match_key)` 组的 K 条新行对 M 条有序历史候选：
     1. 先按精确 `(amount, currency)` 唯一锁定无冲突匹配（例如 `35.00 -> 35.00` 直接锁为 `NO_CHANGE`）；
     2. 对未锁定余集按 `(同单/同币种)` 分组：仅当某分组恰好剩余 **1 条未匹配新行与 1 条未匹配历史行**时，允许确定性一对一配对为 `UPDATE`（例如 `65.00 -> 70.00`）；
     3. 若某组任意一侧仍剩余 `> 1` 条或存在多对多同金额交叉歧义，则该组全部未定行落入 `MatchLevel.AMBIGUOUS_MULTI`，填充完整 `candidate_bills`（`label` 含单号/提单/费用/金额/币种/日期）与 `BLOCKER` `AMBIGUOUS_EXISTING_BILL`。
   - **强制每条命中记录唯一指向旧单 ID**：`audit_trail.matched_bill_ref = matched_bill.external_bill_key or matched_bill.id`，使同单双 `Handling Charge` 分别指向各自唯一历史行。
4. **局部更新先继承后校验（`inherit_existing_bill`）**：
   - 先备份源文档自身声明的原始金额及原始汇总对账基准（供 `validators` 对账使用，防止把局部更新从旧账单继承来的金额误掺入整张局部变更单的对账总额检查中）；
   - 复制命中历史账单字段后，仅覆盖本次明确提供的字段（严格检查：真实值非 `None`，且若某字段属于 `explicit_fields` 并已在 `raw_invalid_fields` 中标错，**禁止用历史旧值把当前非法输入洗白成合法**！）；
   - 同时保留并优先尊重 `human_overrides` 锁定字段；
   - 按 `Decimal` 等值规则计算 `field_diffs`，判定 `CREATE` / `UPDATE` / `NO_CHANGE`；任何未解告警覆盖 `operation = PENDING_REVIEW`。
5. **三口径全合规序列化器（`domain/serializer.py`）**：
   - `serialize_records(records, mode: Literal["official", "full_standard", "audit"])`：
     - **三种模式的每条输出元素顶层严格只有 6 个标准键**：`operation`, `document_info`, `transaction_party`, `charge_details`, `financial_attributes`, `audit_trail`，全部通过 `billing-output.schema.json` 校验。
     - `official`：仅输出 `review_status in {AUTO_READY, APPROVED}` 且 `operation in {CREATE, UPDATE, NO_CHANGE}`；输出计数统计（`included_count`, `excluded_pending_count`, `excluded_rejected_count`）。
     - `full_standard`：输出全部非 `REJECTED` 记录。
     - `audit`：在严格保持顶层 6 键的同时，将完整追踪元数据（`record_id`, `source_doc_id`, `review_status`, `match_level`, `matched_existing_bill_id`, `shipment_refs`, `source_evidence`, `field_diffs`, `validation_alerts`, `excluded_clauses`, `human_overrides`, `human_review_history`, `reject_reason`, `retry_count`）全部嵌入 `audit_trail` 字段中。
     - **Schema 严规**：`UpdateItem` 必带非空 `audit_trail.matched_bill_ref`；`audit_trail.ai_reasoning` 至少 1 条非空字符串；`PENDING_REVIEW` 记录若 `currency` 非法或缺失，`charge_details.currency` 置 `None`（绝不输出 `"ZZZ"`）；数字走无损 `Decimal` JSON 直出通道，保证 `9007199254740991.01` 与 `0.0025` 原样零失真。

### 1.6 确定性校验门禁（`src/shipping_billing_agent/domain/validators.py`）
统一使用 `DECIMAL_TOLERANCE = Decimal("0.02")`，全部金额采用 `Decimal` 计算：
1. **单行数值、币种、日期与主体校验（`BLOCKER`）**：
   - `raw_parse_issues` 中的显式非法字段直接透传保留为对应 `BLOCKER`（如 `INVALID_OR_MISSING_CURRENCY`、`INVALID_DOCUMENT_DATE`、`OCR_NUMERIC_CONFUSION`）；
   - `charge_item` 非空；非贷项 `amount < 0` → `NEGATIVE_NON_CREDIT_AMOUNT`；
   - 行算术校验：**仅基于当前单据本身明确出现的 `quantity`、`unit_price` 与 `raw_amount_abs`（对贷项先取绝对值做 `qty * unit_price == abs(raw_amount)`，并校验单价不能为负且与净额方向一致性）**，不把继承而来的局部值拼凑误报；超 `0.02` → `LINE_ARITHMETIC_MISMATCH`；
   - `currency is None` 或不在 `ACTIVE_ISO4217_CODES` → `INVALID_OR_MISSING_CURRENCY`；
   - `posting_type is None` → `ENTITY_NOT_MATCHED`。
2. **三态汇总金额、显式 `0.00` 与分币种对账（`BLOCKER`）**：
   - 明确**以单据自身提取的明细原值总额参与源单汇总对账**（局部变更且未声明全单总表的继承记录不将继承总额误算成新单源总额，或仅对自身原始有声明总额的单据校验）；
   - 区分四态：
     1. 仅有 `subtotal_amount` 无 `total_amount` → 校验 `abs(sum_raw_lines - subtotal_amount) <= 0.02`；
     2. `subtotal_amount + tax_amount + total_amount` 齐备 → 校验 `abs(sum_raw_lines - subtotal_amount) <= 0.02` 且 `abs(subtotal_amount + tax_amount - total_amount) <= 0.02`；
     3. 仅有 `total_amount`（**明确包含 `total_amount == Decimal("0.00")` 零总额！**）→ 校验 `abs(sum_raw_lines - total_amount) <= 0.02`；
     4. 全部汇总字段为 `None`（如 EML 纯单行口头结算指令）→ 跳过汇总对账并在 `ai_reasoning` 写 `"单据无汇总行，免汇总对账"`。
   - 多币种情况：若源单据带有按币种拆分的 `totals_by_currency`，按各币种独立校验明细和；若多币种混杂却只有一个总金额，标记 `MULTI_CURRENCY_TOTAL_AMBIGUITY`。
3. **OCR 字符混淆与 ISO 6346 集装箱校验（`BLOCKER` / `WARNING`）**：
   - 原始数值字符串中出现 `0/O`、`1/I/l`、`5/S` 混杂数字或双小数点（如 `"1.310.40"`）→ `BLOCKER` `OCR_NUMERIC_CONFUSION`；
   - `compute_iso6346_check_digit`：4 位字母 + 7 位数字（6 位流水号 + 1 位校验码），映射 `A=10..Z=38`（跳过 `11,22,33`），权 $2^0..2^9$ 加权模 `11` 再模 `10`；若格式异常或第 11 位不符，构造替换 `0/O`、`1/I`、`5/S` 且校验合法的建议箱号，发出 **`WARNING` `CONTAINER_ISO6346_MISMATCH`**（允许人工审核后带理由批准）；
   - 上游 `ocr_payload` 中标记 `suspicious=True` 的条目 → `WARNING` `OCR_SUSPICIOUS_FLAG`。
4. **双入口同源归并、超额重复与跨根/跨文档冲突检测**：
   - **同批同源归并（`deduplicate_same_source_documents`）**：当批次内存在多个 **`content_sha256` 非空且相同** 的文档（例如整封 `.eml` 含 `X.pdf` 且用户同时又直传 `X.pdf`），保留主规范文档（优先带 `parent_doc_id` 上下文包内的附件或先到者），将另一文档路径合并至 `source_evidence.source_paths` 及 `ai_reasoning`，防止导出两笔重复 `CREATE`。
   - **同单合法多行 vs 超额重复**：同一单据多条同名行若在源表独立位置存在且分项合与源单总额平齐（如 `DC-108478` `65.00 + 35.00 + 1310.40 == 1410.40`），全部合法保留；仅当完全一致行被重复提取致使总和超单据总额（差值近似单行倍数）或同一物理位置重复产行时，标 `BLOCKER` `DUPLICATE_LINE_EXTRACTED`。
   - **跨根文档与附件冲突**：无论是同封 `.eml` 与附件间，还是跨不同根目录上传（如 `email_a` 正文写 `H/C $50` 与独立文件或另一根目录给出同 BL/同单号/同费目金额 `H/C $80`），均检测字段矛盾，标记 `BLOCKER` `CROSS_DOCUMENT_CONFLICT`；不同附件多源 `MBL` 号全量记录到 `shipment_refs.mbl_nos`（带来源标注），不一致触发 `WARNING` `MULTI_SOURCE_BL_MISMATCH`。

### 1.7 原题 4 份真实样本字段真值与对齐基准
| 样本文件 | 单号 / 日期 / 关联号 / 箱号 | 费用明细、金额与条款 | 排除项与关键视角 |
| :--- | :--- | :--- | :--- |
| `图片示例.png` | `ORFR1618731` / `2025-10-03` / B/L `ZIMUNGB1312144` / `JXLU4504463` | 1) `Wharfage Administration Fee` `5.00 USD` (`qty=1`/`None`)；2) `Wharfage` `21.90 USD` (`unit_price=2.85`, `qty=None` **严禁反算**)；总额 `26.90 USD` | 视角切换：`ZIM AMERICAN INTEGRATED SHIPPING` 为 `AR`；`ALL AMERICA FORWARDING` 为 `AP` |
| `PDF示例账单1.pdf` | `Waybill OCA17` (`CMDU CHN4053601`) / `2026-10-19` / `SAPU2040689` | `On Carriage Additional` (`On Carriage Additional - US Em`) `245.00 USD` `CC` (`C`)；汇总 `TOTAL CHARGES TO BE PAID: USD 245.0000` | 第 1–3 页重复出现 `$25.00 PER CANCELLED I.T.`、`$1100`/`$150` 违约金、收款银行账号**全进 `excluded_clauses`** |
| `PDF 示例账单2.pdf` | `DC-108478` / `2026-10-24` / MBL `MAEU275752922`, HBL `CGT260801188` / `MRSU2571577`, `TCLU8619685` | 1) `Handling Charge 65.00 USD CC`；2) `Detention Free Time Charge 1310.40 USD CC`；3) `Handling Charge 35.00 USD CC`；总额 `1410.40 USD` | 忽略 `PAID AMOUNT 0.00` 与 BOA 银行信息；单边写 `DUE TO NEPTUNE SHIPPING LIMITED (LAX)` → NEPTUNE 为 `AR`；无关主体不得乱猜付款方(`PENDING_REVIEW`) |
| `深圳容宇 PRE-ALERT...eml` | 根 `.eml` (`raw_text=""`) 0 次 LLM 调用；子节点 `.body.txt` (`2026-10-01`) + `MBL.pdf` (`HZMHK331281`, 订舱 `4055331281`, `TIIU8975975`) + `HBL.pdf` (`SSZSE26093128`, `OOLU4055331281`) | 唯一计费行：`SHA pay TAR: H/C $50` → `Handling Charge` `50.00 USD`，`SHA`(`SUN TRACK EXPRESS`) 为 `AP`，`TAR`(`TARGETING A INC`) 为 `AR`；`payment_term=None`（海运 `FREIGHT PREPAID` 不强推代理 `H/C`） | `Carrier Free H/C` 进 `excluded_clauses`；`MBL.pdf`/`HBL.pdf` 仅作 `METADATA_ONLY` 运输上下文合并关联（反序附件同效）；`InsertPic` 保持 `SKIPPED` |

### 1.8 LangGraph `StateGraph` 编排、防注入、并发预算与 1 轮受控自修复（`src/shipping_billing_agent/agent/`）
1. **`agent/prompts.py`**：
   - `sanitize_for_llm(...)` 脱敏 + 截取主账单区域最小证据片段（优先保留表头/单号/明细/汇总区，过滤银行账号和冗余免责尾文）；包裹在 `<UNTRUSTED_DOCUMENT_CONTENT doc_id="...">...</UNTRUSTED_DOCUMENT_CONTENT>` 内；
   - 明确禁止根据 `amount / unit_price` 反算 `quantity`、禁止把 `PAID AMOUNT 0.00` 或 `$25 per cancelled` 当作费用、缺省字段返回 `null`；
   - 提供第 2 轮自修复 `build_self_correction_messages(doc_id, sanitized_snippet, prev_extraction, alerts)`（附带精确差异摘要如 `Σ明细=1440.40 vs Total=1410.40`）。
2. **`agent/rule_extractor.py`**：
   - 遍历全部 `doc.ocr_payload.items`（含多页/多图 item，而非仅首条）；
   - 解析 Office 多表（`.xls/.xlsx/.doc/.docx`），严禁空单元格左移串列，跳过汇总和银行块；
   - 提取主费用区（避开 `PAID AMOUNT`、`CANCELLED`/`PENALTY`/`WIRE`/`ACH`/`IF ...` 条款，收集进 `excluded_clauses`）；
   - 处理 `EML` 空根 0 次 LLM 调用与 `body + MBL/HBL` 反序乱序上下文归并；单传纯 `MBL.pdf`/`HBL.pdf` 标 `METADATA_ONLY` 不产 0 元伪账单。
3. **`agent/graph.py`（真实 `langgraph.graph.StateGraph`）**：
   - 原子 **`LlmBudgetGuard`**（`asyncio.Lock` / `threading.Lock` 在调用前抢占配额，彻底消除并发下超出 `limits.max_llm_calls_per_batch`）；
   - `anyio.CapacityLimiter(limits.max_concurrency)` 控制外部 LLM 并发；
   - 节点图：`build_context_packages` → `extract_documents` → `validate_raw_documents` → 条件边 `should_self_correct`（仅可修复错误且 `retry_count < 1` 且预算足时）→ `self_correct_document`（`retry_count += 1`，最多 1 次，带最小脱敏片段与差错重试，结束后回 `validate_raw_documents` 再门禁，随后必停）→ `normalize_match_and_validate`（严格按 `entity_resolver -> matcher -> validators` 组装与跨根冲突/去重检测）→ `finalize_output`（产出 `BatchPipelineResult` + `FailedDocumentAlert[]`）。
   - 严守 `agent` 边界：**仅借用外部注入的 `StructuredLlmClient`，不在图结束时关闭调用方 client**；对解析异常或 LLM 异常单据隔离写入 `failed_document_alerts` 与 `failed_doc_ids`，绝不拖垮同批其余合法文档。

---

## 2. 边界异常与风险处理
1. **显式非法串与增量省略混淆风险**：通过 `explicit_fields` + `raw_invalid_fields` 双轨追踪，使局部更新漏填字段安全继承历史原值，而显式传入的 `"ZZZ"`/`"2026-02-30"`/`"1.310.40"` 绝不崩溃也不被历史旧值洗白。
2. **负金额单据误判贷项导致多记冲减或颠倒收付**：严禁通过 `raw_amount < 0` 推断贷项；真贷项在原始绝对值上校对 `qty * unit_price` 后维持 `AR/AP` 不翻转仅取 `-abs(amount)`。
3. **跨币种缺币种增量更新与同单双行多态错配**：缺币种遇多币种候选即刻截停为 `AMBIGUOUS_MULTI`；多行配对严格按 `bill.id` 排序，先锁定 exact `(amount, currency)`，余集仅在同组 `1:1` 时允许 `UPDATE`，确保与 `existing_bills` 输入顺序完全无关。
4. **IEEE-754 精度丢失与 JSON Schema 越界**：从 `parse_float=Decimal` 入、`dumps_decimal_json` 原生数字出，且三口径全部严守顶层 6 键，`PENDING_REVIEW` 非法币种输出 `null`。

---

## 3. 异构方案审查与主对话裁决记录
- 审查模型：`codex`（`plan_critic_codex`，输出详见 `.wf-tmp/out-B02-plan-critic.md`）。
- 审查发现统计：共 15 项（Blocking × 7、Important × 7、Suggestion × 1）。
- 主对话裁决结论：**15/15 全部采纳并落地上述设计**：
  1. 禁用 `raw_amount < 0` 反推 `is_credit`，贷项行先按正数/列值校验算术再维持 `AR/AP` 符号冲减；
  2. 新增 `explicit_fields` / `raw_invalid_fields` / `raw_parse_issues`，优雅降级非法值并禁止继承洗白；
  3. L2/L3 缺币种增量优先查跨币候选，存在多种币种直接走 `AMBIGUOUS_MULTI`，不降级不盲配；
  4. 按 `bill.id` 确定性排序并采用“先精确后同组 1:1”配对，`matched_bill_ref` 带唯一旧单号；
  5. 严格主体别名过滤、支持 `DC-108478` 单边付款方判 `AR`、`recalculate_for_entity` 清空旧主体绑定与审批计数；
  6. 三口径顶层仅留 6 键全过 `billing-output.schema.json`，`audit` 扩展嵌入 `audit_trail`，`PENDING_REVIEW` 非法币种写 `null`；
  7. 全链路 `parse_float=Decimal` + `dumps_decimal_json` 原生十进制无引号直出；
  8. 补齐 `AlertCode` / `AlertSeverity` / SQL & `/approve` 422 映射表；
  9. 锁定 4 真实样本字段级真值基准；
  10. 空 `.eml` 根 0 次 LLM、多 `items` OCR、非空 `sha256` 去重、跨根冲突检测与 `FailedDocumentAlert`；
  11. 汇总对账支持 4 种模式、显式 `0.00`、多币种独立合计并与继承值隔离；
  12. 主费用区抽取防误采 `PAID AMOUNT 0.00`/违约金，双轮均发送脱敏最小片段；
  13. 实现原子 `LlmBudgetGuard` 与借用 client 生命周期零侵扰；
  14. 单测覆盖编译态 `StateGraph` + 队列 Fake 的完整两轮受控自修复与熔断拦截；
  15. 清晰解耦 `T02`/`T03`/`T04` 边界并在流水线层统一串联 `entity_resolver -> matcher -> validators`。
