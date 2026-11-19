# B01 多格式文档解包、Excel/Word/EML/PDF解析、SSRF防护与OCR/LLM网关适配 — 独立验证报告

- **验证基线与提交范围**：`cf93832`（B01 Plan & `plan_critic_codex` 16/16 审查通过）至 `770d9754231a1fae70db3108afe77be030caded6`（`T01`~`T30` 全部本地提交）。核心交付覆盖配置与安全预算、文件名清洗、Excel/Word/EML/PDF/图片解包解析与 `DocumentParserDispatcher` 分发、`url_guard` SSRF 防护、`OcrGatewayClient` / `RapidOcrFallback`、`OpenAICompatibleLlmClient` `json_mode` 与凭据隔离及 `text_sanitizer` 汇款脱敏。

- **前置方案审查与安全审计**：
  - `plan_critic_codex`：16 项审查意见全数采纳合入方案与架构（`cf93832`）。
  - Agent Security 专项审计：`r3` PASS（提交基线 `61e6de2`，`.wf-tmp/out-B01-security-r3.md`），确认 SSRF（IP 校验与禁用重定向）、LLM 自定义端点 `DEEPSEEK_API_KEY` 凭据隔离、签名 Query 参数/密钥与银行账号脱敏、EML 递归深度与批上限均落地并有单测反例覆盖。

- **本次自测与最终核验（在 `770d975` 实测通过）**：
  - `uv run ruff check src tests`：退出码 `0`（`All checks passed!`）。
  - `uv run python scripts/verify_contracts.py`：退出码 `0`（SQLite 8 表契约、`billing-output.schema.json` 176 活跃 ISO 4217 与 6 反例、4 张 SVG 图全通过）。
  - `pwsh -NoProfile -File "C:/Users/Administrator/.agents/scripts/review-guard.ps1" -Repo "f:\demo\ocr" -Base cf93832c7fab8e4c731a2cb125b185e30dddb008 -Head 770d9754231a1fae70db3108afe77be030caded6`：`30 files`，`VIOLATIONS (0)`、`HIGHLIGHTS (0)`。
  - `uv run pytest tests/unit tests/integration/test_b01_real_samples.py -q`：退出码 `0`，**`645 passed in 396.41s`**（0 失败，0 跳过），完整覆盖：
    - 笔试题原题 4 份真实样本的 5 条集成测试：`2615293...eml`（含 `SHA pay TAR`、`InsertPic_C25B.jpg` 签名跳过+强制重解、`4055331281 MBL/HBL.pdf` 原始文件名不换行空格清洗与字节 SHA-256 精确保留）、`PDF示例账单1.pdf`（3 页文本提取与 6 处汇款账号脱敏验证）、`PDF 示例账单2.pdf`（`DC-108478` 三行明细与 `1,410.40` 总计）、`图片示例.png`（本地 `RapidOcrFallback` 真实识出 `ORFR1618731` 与 `5.00 / 2.85 / 21.90 / 26.90`）；
    - 多格式 Office 解析与对抗边界：`.xls`/`.xlsx`、`.docx`/`.doc`（`legacy-doc` piece-table 底层流含空单元格防串列）、SSRF、LLM 凭据隔离、银行账号脱敏、PDF 隐藏/透明矢量过滤与 EML 签名图防护。
  - 性能与内存基准：`128 KiB` 单行 HTML `_html_text` 峰值 `0.51 MiB`、`<pre>` `65,536` 短行 `x\n` 峰值 `1.30 MiB`、`<footer>` `12/24/48` 组图文 `_is_inline_element` 调用严格线性 **`12/24/48`**。

- **独立审查与复验裁决结论**：
  - `r1`~`r28` 由 `verifier`（`codex` / `gpt-6.2-astra`）及主对话规划模型全程跟进核验，主干解析器与安全门禁逻辑完整，所有先红后绿回归全绿。
  - 依据 [B01 功能实现方案 §1.4](多格式解析与OCR网关-plan.md) 分级规范，`r28` 实测确认 `T30` 完全闭合 `r27 A1~A3`、`F1` 中文换行定位与页脚线性计数；对极罕见合成竞争边界（`<footer><hr><figure>Tel:...<img alt="Amount due">` 与 `<main>` 同地标 `alt="Company logo"`）连同先前的 `r19`、`r21` 合成边角排列正式裁决记入 [backlog.md](../../backlog.md) 后续演进，产品层面已有工作台附件树一键 `force_parse_skipped_document` 人工兜底，不再阻塞下游 `B02`~`B05` 交付推进。

- **`UNVERIFIED` 项（延至 Phase 5 实连联调闭环）**：
  - 真实外部 OCR 网关出站请求：`POST https://gateway.pltplt.com/ai/process` 对公网非空账单 URL 的实际返回与映射解析；
  - 真实 DeepSeek API `json_mode` 端到端结构化抽取与 1 轮受控自修复；
  - 真实公网重定向地址跨网跳转与网络超时回归。

- **转入 [backlog.md](../../backlog.md) 的遗留项**：
  - `B01 解析器合成边界与词表扩充（r19 / r21 / r28 裁决延后）`：PDF `t=0.4` 零面积 Bezier/多子路径抵消、`r19 A2/A4/A5/B` 合成 EML 变体、`r21` `<body><hr>` 附注、`r28` 页脚无落款 `<figure>` 竞争边界与 `amount due` / `alt="Company logo"` 扩展。
