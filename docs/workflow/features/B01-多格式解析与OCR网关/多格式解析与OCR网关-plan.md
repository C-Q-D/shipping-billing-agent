# B01 多格式文档解包、Excel/Word/EML/PDF解析、SSRF防护与OCR/LLM网关适配 — 功能实现方案（定稿）

- **关联锚点**：`PRD F-01 / F-02`，`arch-contract.md §1.2 / §1.4 / §2 / §5.3`，`schema.sql source_documents`，`openapi.yaml HealthStatus / SourceDocumentSummary`
- **范围与非目标**：
  - **本批次范围**：`src/shipping_billing_agent/config/`（`settings.py`、`limits.py`）、`src/shipping_billing_agent/domain/`（`currency.py`、`models.py`）、`src/shipping_billing_agent/gateways/`（`url_guard.py`、`text_sanitizer.py`、`ocr_client.py`、`local_ocr.py`、`llm_client.py`）、`src/shipping_billing_agent/parsers/`（`filename_sanitizer.py`、`excel_parser.py`、`word_parser.py`、`pdf_parser.py`、`eml_parser.py`、`dispatcher.py`）、更新 `api/app.py` `/api/v1/health` 与 `AppSettings` 共享配置，以及对应单测/离线集成测试与已知金额 Office 夹具。
  - **非目标**：不实现 LangGraph 抽取、主体视角 AR/AP、四级匹配或确定性校验门禁（归 `B02`）；不实现 SQLite 仓储、FastAPI 业务路由或 Typer 批处理命令（归 `B03`）；**严禁修改或移动** `AI_agent_%E7%AC%94%E8%AF%95%E9%A2%98_%E5%AE%8C%E6%95%B4%E6%8F%90%E5%8F%96.md` 与 `笔试题AIAGENT/` 原件。
- **单向依赖红线**：`{parsers, gateways} -> domain <- config`；`domain` 零外部 IO（严禁导入 `httpx`/`sqlite3`/`fastapi`/`langchain`）；`parsers` 不直接调用 LLM；`parsers` 仅通过窄 Protocol 依赖注入外部 OCR/URL 校验能力，避免与 `gateways` 发生双向硬耦合。

---

## 1. 模块与数据/接口改动设计

### 1.1 配置与资源预算边界（`src/shipping_billing_agent/config/` & `api/app.py`）
1. **`settings.py`（`AppSettings(BaseSettings)`）**：
   - 使用 `SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")`；提供 `load_settings()`（无副作用新鲜加载）与 `get_settings()`（可清除缓存的工厂）。
   - **独立 LLM 与 OCR 配置组**（绝不混用代理或密钥）：
     - LLM：`llm_provider="openai_compatible"`、`llm_base_url="https://api.deepseek.com/v1"`、`llm_api_key=""`、`deepseek_api_key=""`、`llm_model="deepseek-flash"`、`llm_trust_env=False`、`llm_proxy_url=""`、`llm_timeout_seconds=90.0`、`llm_max_retries=1`。
     - OCR：`ocr_gateway_url="https://gateway.pltplt.com/ai/process"`、`ocr_trust_env=False`（默认直连绕过系统代理以防本机代理触发 SSL EOF）、`ocr_proxy_url=""`、`ocr_connect_timeout_seconds=10.0`、`ocr_read_timeout_seconds=300.0`、`ocr_task="orderChange"`。
     - 可选受控中转：`public_relay_base_url=""`（默认禁用）、`relay_presign_ttl_seconds=300`（Pydantic 校验 `1 <= ttl <= 300`）。
     - 服务与持久化：`app_host="127.0.0.1"`、`app_port=8000`、`allow_remote_bind=False`、`app_data_dir=Path("data")`、`sqlite_db_path=Path("data/shipping_billing.db")`。
   - **凭据隔离与 HTTPS 铁律 `resolve_llm_credentials(self) -> tuple[str, str | None]`**：
     - 若 `llm_api_key.strip()` 非空，返回 `(effective_base_url, llm_api_key.strip())`；
     - 若 `llm_api_key` 为空：仅当 `llm_base_url.strip()` 为空（默认回退至 `https://api.deepseek.com/v1`）**或** `urlparse(llm_base_url)` 严格满足 `scheme.lower() == "https"` 且 `(hostname or "").lower() == "api.deepseek.com"` 时，才允许回退返回 `deepseek_api_key.strip() or None`；
     - 若配置了明文 `http://api.deepseek.com...` 且无显式 `LLM_API_KEY`，或 `LLM_BASE_URL` 指向任意非 `api.deepseek.com` 自定义地址且未设 `LLM_API_KEY`，**强制返回 `None`**（构造客户端时报错），严禁把 `DEEPSEEK_API_KEY` 发往明文或第三方端点。
   - **能力判定共享方法**：`is_llm_configured()`、`is_ocr_direct_mode()`、`is_public_relay_ready(relay_publisher=None)`（**必须同时满足 `public_relay_base_url` 非空且传入了真实非空 `relay_publisher` 实现**）。
2. **`limits.py`（`ResourceLimits`）**：
   - `max_batch_files: int = 50`（用户顶层提交文件 + URL 总数上限）；
   - `max_expanded_documents: int = 120`（同批递归展开后的实际子文档总上限，防“宽而浅”单邮件塞入海量附件耗尽配额）；
   - `max_file_bytes: int = 50 * 1024 * 1024`、`max_eml_recursion_depth: int = 3`、`max_doc_self_correction_rounds: int = 1`、`max_concurrency: int = 4`、`max_llm_calls_per_batch: int = 100`。
   - `ALLOWED_UPLOAD_EXTENSIONS = frozenset({".eml", ".pdf", ".jpg", ".jpeg", ".png", ".tif", ".tiff", ".xls", ".xlsx", ".doc", ".docx"})`。
3. **接入 `api/app.py` `/api/v1/health`**：
   - 修改 B00 探针使其委托给 `load_settings()`（兼容 `.env` 与进程环境变量动态覆写）与 `is_llm_configured()`、`is_ocr_direct_mode()`、`is_public_relay_ready(relay_publisher)`，响应字段名与 `HealthStatus` 契约完全不变。

### 1.2 领域基础模型与货币规范（`src/shipping_billing_agent/domain/`）
1. **`currency.py`（基于实测 `iso4217 1.16.20260101`）**：
   - 加载 176 个活跃三位大写货币代码 `ACTIVE_ISO4217_CODES`（排除 `"XXX"`, `"XTS"`）。
   - `is_valid_iso4217(code)` 与 `normalize_currency(raw, *, context_currencies=(), us_shipping_context=False)`：处理 `RMB`/`€`/`£`/`US$`；歧义符号 `$` 仅在同单/美线上下文唯一指向美元时推导出 `USD` 并返回说明，否则返回 `(None, "币种符号 $ 存在多币种歧义，待复核")`。
2. **`models.py`（严格对齐 `schema.sql` 与 `openapi.yaml`）**：
   - Decimal 精度工具：`parse_decimal_strict(value)`（去千分位逗号、禁 `NaN`/`Infinity`）与 `decimal_to_db_text(value)`（规范十进制字符串、`"-0.00"` 归零为 `"0.00"`）。
   - 与 `schema.sql`/`openapi.yaml` 完全一致的枚举：`OcrMode`、`SourceKind`、`ExtractionChannel`、`DocumentStatus`、`OperationType`、`ReviewStatus`、`MatchLevel`、`PaymentTerm`、`PostingType`、`AlertSeverity`、`BatchStatus`、`BatchStage`。
   - **纯建批前校验结构**：`InputValidationIssue(code: str, filename_or_url: str, message: str)`（支持 `EMPTY_INPUT`, `TOO_MANY_FILES`, `EMPTY_FILE`, `FILE_TOO_LARGE`, `UNSUPPORTED_EXTENSION`, `SSRF_BLOCKED`, `INVALID_URL`）。
   - `ParsedDocument`：显式包含 `doc_id: str`、`parent_doc_id: str | None`（不再使用 SQL 中不存在的 `root_group_id`，同封上下文沿 `parent_doc_id` 树归集）、`original_filename: str`、`sanitized_filename: str`、`media_type: str | None`、`source_kind: SourceKind`、`content_sha256: str | None`（**纯公网 `URL` 来源置 `None` 并退出按哈希去重；本地文件与附件强制计算真实 64 位小写 SHA-256**）、`public_url: str | None`、`extraction_channel: ExtractionChannel`、`status: DocumentStatus`、`error_message: str | None`、`raw_text: str`、`tables: list[ParsedTable]`、`metadata: dict[str, Any]`、`ocr_payload: OcrStructuredPayload | None`、`raw_bytes: bytes | None`（`exclude=True`）。
   - `RawChargeItem` & `RawDocumentExtraction`（Pydantic v2 + Decimal `BeforeValidator`，供 `with_structured_output(..., method="json_mode", include_raw=True)` 与 B02 共用）。

### 1.3 安全网关、外发脱敏与外部适配（`src/shipping_billing_agent/gateways/`）
1. **`url_guard.py`**：
   - `SsrfBlockedError(ValueError)`（`code = "SSRF_BLOCKED"`）。
   - `validate_public_url(url: str, *, resolver: Callable[[str, int], list[tuple]] = socket.getaddrinfo) -> str`：仅允许 `http`/`https` 公网 URL，禁携 `username`/`password`，拒绝 `localhost` / `.localhost` / `.internal` / `.local`，校验字面 IP 与本机 DNS 解析全部结果 IP（含 IPv4 映射 IPv6 还原），拦截回环、私有、链路本地、云元数据 `169.254.0.0/16`、CGNAT `100.64.0.0/10`、多播/保留/未指定地址。
   - **验收边界诚实声明**：本机预检保证出站前本机可见地址与语法合法；远端 `gateway.pltplt.com` 服务器自身拉取阶段的二次解析与 3xx 重定向隔离能力属第三方服务黑盒（标为 `UNVERIFIED`）。
   - `redact_url_query(url: str) -> str`：隐藏 URL query 值保护预签名参数不落日志。
2. **`text_sanitizer.py`（LLM 最小外发脱敏器，#11）**：
   - `sanitize_for_llm(text: str) -> str`：统一脱敏发送给大模型前的单据文本与自修复提示片段——① 剥除/脱敏预签名 URL 中的敏感 Query 参数（如 `Signature`/`X-Amz-*`/`Token`）；② 脱敏明确的银行汇款信息段或银行账号字段（如 `Account No` / `Acct No` / `Routing` / `ABA` / `SWIFT` / `IBAN` 后的账户数字，以及与 `Bank` / `Remit To` 同段的长串银行账号），替换为 `[REDACTED_BANK_ACCT]`；③ **严格保留**账单计费与匹配所需全部核心业务要素（金额、费率、币种、单据号 `DC-108478`/`ORFR1618731`、提单号/订舱号/箱号、交易双方公司名与日期）。首次抽取与自修复均通过此函数或复用其输出。
3. **`ocr_client.py`**：
   - `PresignedRelayPublisher`（Protocol）：可选受控私有中转扩展接口，默认未注册实现。
   - `PltPltOcrClient`：
     - 构造时接收 `settings: AppSettings` 与可选测试用 `transport` / `async_transport`（无默认 `http_client` 注入以免遮蔽代理设置）；
     - 严格读取配置 **`timeout = httpx.Timeout(connect=settings.ocr_connect_timeout_seconds, read=settings.ocr_read_timeout_seconds, write=30.0, pool=10.0)`**；
     - 构建同步/异步 `httpx` 客户端均强制传入 `trust_env=settings.ocr_trust_env` 与 `proxy=settings.ocr_proxy_url or None`，不携带任何 LLM 密钥；
     - 调用 `recognize_url` 前强制过 `validate_public_url`；发送确切 JSON `{"file": file_url, "task": settings.ocr_task, "callBack": "", "taskType": ""}`；
     - **三态严格响应校验（#6）**：
       1. 若响应 JSON 非字典、HTTP 非 2xx，或明确 `payload.get("success") is False`（即使残留 `data.list`），立即抛出 `OcrGatewayError`；
       2. 若 `data.list` 缺失、非列表或为空，或所有项既无 `chargeList` 也无任意有效单据/运输字段（`fileType`/`invoiceNumber`/`mblNumber`/`shipper`/`consignee`/`ctrList`/`other`），抛出 `OcrGatewayError("OCR 网关返回空列表且无有效单据内容")`；
       3. 若存在有效运输/单据元信息但全部 `chargeList` 为空，返回 `OcrStructuredPayload(has_charges=False, has_metadata=True, ...)`，交由调度器记为 `DocumentStatus.METADATA_ONLY`。
4. **`local_ocr.py`**：
   - `RapidOcrFallback`：懒加载 `rapidocr_onnxruntime.RapidOCR()`；直接复用 `RapidOCR` 内部已排序返回的框顺序（按 #16 不重复造排序轮子），逐页读取单张图像及多帧 TIFF（`Pillow.ImageSequence.Iterator`）并标记 `[Page P]`，返回原文文本与平均置信度；损坏或空图像抛出 `LocalOcrError`。
5. **`llm_client.py`**：
   - `StructuredLlmResponse(parsed, raw_text, parsing_error, model_name, usage)` 与 `StructuredLlmClient` Protocol（含 `invoke_structured` 与 `ainvoke_structured`）。
   - `OpenAICompatibleLlmClient`：**同时**基于同一 `settings` 构造同步 `http_client = httpx.Client(trust_env=settings.llm_trust_env, proxy=settings.llm_proxy_url or None, timeout=settings.llm_timeout_seconds)` 与异步 `http_async_client = httpx.AsyncClient(trust_env=settings.llm_trust_env, proxy=settings.llm_proxy_url or None, timeout=settings.llm_timeout_seconds)`（按已核实 `langchain-openai 1.8.3` `ChatOpenAI` 字段），并调用 `.resolve_llm_credentials()`（缺密钥或明文 `http://api.deepseek.com` 回退即拒）；通过 `.with_structured_output(schema, method="json_mode", include_raw=True)` 执行抽取并在发送前对消息做 `sanitize_for_llm` 兜底检查，记录并脱敏异常日志中的任何密钥。
   - `FakeStructuredLlmClient`：测试替身，完整记录同步/异步入参供 B02 断言。

### 1.4 多格式无桌面 Office 解析引擎与调度器（`src/shipping_billing_agent/parsers/`）
1. **`filename_sanitizer.py`**：`sanitize_filename`（防 `../`/`..\`/NUL 穿越、将 `\xa0` 等非常规空白规整为 `_`、限长保留扩展名）、`detect_source_kind`（扩展名白名单校验）。
2. **`excel_parser.py`**：
   - `.xlsx` 用 `openpyxl.load_workbook(..., data_only=True, read_only=False)` 展开 `merged_cells.ranges` 向矩阵回填左上角值；
   - `.xls` 强制使用 **`xlrd.open_workbook(file_contents=data, formatting_info=True)`** 读取 `merged_cells` 并回填左上角值（仅当极端老版本 BIFF 文件抛 `NotImplementedError` 时回退 `formatting_info=False` 并记录警告）；保留每行原始 1-based 行号与工作表名 `[Sheet: <name> | Row N]`。
3. **`word_parser.py`**：
   - `.docx` 直接复用 `python-docx 1.2.0` 公开 API **`Document.iter_inner_content()`**（按原顺序遍历 `Paragraph` 与 `Table`）加 `sections` 页眉页脚提取，表格输出 `ParsedTable` 与保留列位的制表分隔行；
   - `.doc`：基于 `legacy-doc 0.2.1` `OleReader` / `_find_clx` 读取未归一化原始文本流，**同步解析 `WordDocument` 头部 `FibRgFcLcb` 中的 `PlcfBtePapx`（index 13: `fcPlcfBtePapx`/`lcbPlcfBtePapx`）→ `PapxFkp` → 段落属性 `sprmPFTtp (0x2417 == 1)` 获取精确表格行尾字符文件偏移 `ttp_fcs`**：
     - 当某个 `\x07` 字符的 `fc` 命中 `ttp_fcs`（且前接单元格结束 `\x07`）时精确判定为行尾，从而区分“行中空单元格 `\x07`”与“行结束标记”；
     - 若文件缺失有效 `PlcfBtePapx` 且纯文本出现连续 `>= 3` 个 `\x07` 歧义，明确抛出表解析失败异常（不猜测错列）；支持检测加密 `.doc`（`EncryptedPackage` / `flags & 0x0100`）与域代码剥离（`\x13...\x14...\x15` 只留展示文本）。
4. **`pdf_parser.py`**：
   - 使用 MIT 协议 `pdfplumber`；在允许本地兜底时**逐页判定**：
     - 对有足够可读文字的页（去空白 `>= 15` 字符）保留 `pdfplumber` 文本与表格（`[Page P | Text]`）；
     - 对文字稀疏（`< 15` 字符）且含图像或非空白的扫描单页，通过 `page.to_image(resolution=200).original` 转 PNG 内存图送 `LocalOcrProtocol` 识别并标记 `[Page P | OCR]`；
     - 若整份 PDF 任一页动用 `LocalOcrProtocol`，文档 `extraction_channel = LOCAL_RAPIDOCR_FALLBACK`，否则 `LOCAL_PDF_TEXT_FALLBACK`；损坏/加密 PDF 明确抛出解析失败异常。
5. **`eml_parser.py`**：
   - 使用标准库 `email`（`policy.default`）+ `beautifulsoup4`：
     - 同一 **`multipart/alternative`** 组只生成 **1 份用于下游抽取的正文文本**（优先非空 `text/plain` 保持原文本换行，否则抽取清理后的 HTML 文本；若存在 HTML 版本另保存其 DOM 用于 `cid:` 内嵌图位置识别）；不将 `plain` 与 `html` 重复拼接导致指令翻倍；
     - 递归解包附件与嵌套 `message/rfc822`；针对 `Content-Type` 为 `application/octet-stream` 的附件结合清洗后文件名扩展名判定真实格式；
     - **CID 装饰/签名图识别（不依赖 `Content-Disposition: inline`）**：对含 `Content-ID` 的图片，检查 HTML 中对应的 `<img src="cid:...">` 是否处于邮件签名/发件人落款区（如底部 `<table/div>`、前置分隔符 `-- ` / 发件人版权声明、`InsertPic_*` / `_Foxmail.` 等内嵌徽标特征）且不含账单语义文件名 → 标 `status=SKIPPED_NOISE, extraction_channel=SKIPPED_SIGNATURE`，保留原始 `raw_bytes`、`original_filename`、`sanitized_filename`、`parent_doc_id` 供手动 `force-parse`；
     - **深度 + 展开总计数双预算**：共享批级计数器，超过 `max_eml_recursion_depth (3)` 或 `max_expanded_documents (120)` 前立即停止解包并产一条显式 `FAILED` 子文档（保留已成功解包的同批文档）。
6. **`dispatcher.py`**：
   - **暴露建批前纯输入校验**：`validate_batch_inputs(files, public_urls, limits, url_validator)` → 返回 `list[InputValidationIssue]`（支持拦截空输入、超 50 项、`0` 字节文件、超 `50MB`、不支持扩展名、非法或命中 SSRF 的顶层公网 URL，供 `B03` API/CLI 直接返回 `400` 而不建批）。
   - **批内执行调度 `parse_batch(...)`**：为每份输入与递归子件生成确定或传入复用的 `doc_id`、正确挂接 `parent_doc_id`、保留 `original_filename` 与 `sanitized_filename`、本地件强制算 `content_sha256`、URL 置 `None`；严格区分 `REMOTE_ONLY` 拦截（绝不静默降级）与 `REMOTE_WITH_LOCAL_FALLBACK`；支持 `retry_single_document` / `force_parse_skipped_document` 沿用已有 `doc_id` 与 `parent_doc_id`。

---

## 2. 边界异常与风险处理

1. **所有 16 项异构审查发现闭环对照**：逐一对应上文设计，并在各测试文件补齐客观可核验断言：
   - 代理直连测试（无 `MockTransport` 的本地 `http.server` 接收端 + 假代理环境变量验证 `trust_env=False` 绕过代理与超时配置生效）；
   - `OpenAICompatibleLlmClient` 同时校验同步/异步双客户端、HTTPS-only DeepSeek 密钥回退及 `sanitize_for_llm` 脱敏账号测试；
   - `.xls` 合并单元格标题后两行共用费名夹具、`.doc` 中间空列/末尾空列 + 连续 `\x07` 真实二进制夹具、混合“文字条款页+扫描金额页”PDF 逐页兜底测试；
   - 宽而浅 EML 触发展开上限截断、嵌套 EML `depth=4` 截断、原题 `.eml`（1 正文 + 3 附件 + 1 跳过签名图 + 仅 1 次 `SHA pay TAR`）、原题 `PDF1`/`PDF2`/`PNG` 逐页逐行原文证据断言。
2. **明确范围边界**：`url_guard` 本机预检验证范围限于客户端解析；远端 OCR 网关下载侧重定向与真正外部公网 PDF OCR 识别留 `Stage 5 (@pytest.mark.live_ocr)` 标为实连验收前为 `UNVERIFIED`。

---

## 3. 异构方案审查与主对话裁决记录
- **审查模型**：`plan_critic_codex`（`gpt-6.2-astra`，会话 `01a1f180-87f1-7842-8182-c138cfd34df0`，报告 `.wf-tmp/critic-B01.md`）
- **裁决结论**：审查共提出 16 条（8 项阻塞实现 + 8 项修订与风险）。主对话已核对 `xlrd 2.0.2` 源码、`legacy-doc 0.2.1` 与 MS-DOC `PlcfBtePapx/sprmPFTtp`、`langchain-openai 1.8.3`、`python-docx 1.2.0`、`RapidOCR 1.4.4` 及原题 `.eml` 实件，**全部 16 条均证实成立并 100% 采纳修订**：
  - 采纳 #1~#3、#5~#9、#11~#16 直接修入 `B01-plan.md` 与 `B01-tasks.md` 验收判据（含立刻将 `api/app.py` 接入 `AppSettings`）；
  - 采纳 #4 同步修订契约 `docs/workflow/contracts/schema.sql`（允许 `source_kind = 'URL'` 时 `content_sha256 IS NULL`，本地文件仍强制 `NOT NULL`）与 `scripts/verify_contracts.py`；
  - 采纳 #10 在 §2 诚实界定 `url_guard` 本机预检与远端下载黑盒的 `UNVERIFIED` 边界。
