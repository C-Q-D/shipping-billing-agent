# 航运智能账单助手 Agent

把航运邮件、账单和扫描件转成可追溯的标准费用明细。Python、LangChain 与 LangGraph 负责抽取和校验，FastAPI、SQLite 与 React 提供批次复核、三种导出和演示台账同步。指定记账主体后，可以看到每笔费用的应收或应付方向、匹配依据及待复核原因。

## 零密钥 3 分钟体验

在项目根目录操作。准备 `uv` 和 Python 3.12（见 [.python-version](.python-version)）。启动工作台还需 Node.js 和 npm。首次下载依赖的耗时取决于网络。

1. 同步 Python 依赖。

   ```sh
   uv sync
   ```

   如需运行 Playwright 浏览器自动化，另执行 `uv run playwright install chromium`。

2. 用内置 EML 和随批历史账单离线生成正式 JSON。

   ```sh
   uv run shipping-billing-agent parse examples/sample_debit_note.eml --entity "NEPTUNE SHIPPING LIMITED" --bills examples/existing_bills.json --ocr-mode REMOTE_WITH_LOCAL_FALLBACK --output-mode official
   ```

   标准输出恰好两条。签发方 NEPTUNE 为记账主体，两条均为 `AR`。原始费用名见 `charge_details.raw_charge_name`。

   | 操作 | 原始费用名 | 标准费用名 | 金额 | 付款方式 |
   | --- | --- | --- | --- | --- |
   | `UPDATE` | Handling Fee | Handling Charge | `40.00 USD` | `PP` |
   | `CREATE` | THC | Terminal Handling Charge | `120.50 USD` | `CC` |

3. 构建前端并启动一体化复核工作台。

   ```sh
   npm --prefix frontend ci && npm --prefix frontend run build
   uv run shipping-billing-agent serve --ocr-mode REMOTE_WITH_LOCAL_FALLBACK
   ```

   在浏览器打开 `http://127.0.0.1:8000`。访问 `GET http://127.0.0.1:8000/api/v1/health` 应返回 HTTP `200`，其中 `status` 为 `ok`，零密钥环境下 `llm_configured` 为 `false`。用 `Ctrl+C` 停止服务。

## 原题验收与复现入口

| 原题验收标准 | 实现与验证入口 |
| --- | --- |
| 1. EML 正文及附件 | `parsers/eml_parser.py` 与递归分发。快速体验验证纯文本，Harness C04 复验原题附件和乱序。 |
| 2. 指定 OCR API 识别 PDF、图片 | `gateways/ocr_client.py` 使用指定网关。`LIVE_E2E` 核对真实 `PLTPLT_OCR` 来源，当前为 `UNVERIFIED`。本地识别见 C01–C03。 |
| 3. Excel 和 Word 费用表格 | `parsers/excel_parser.py`、`word_parser.py`。C13 独立验证 `.xls`、`.xlsx`、`.doc`、`.docx`。 |
| 4. 单号、费用、金额、币种的提取与标准化 | `agent/rule_extractor.py`、`agent/graph.py` 和 `domain/`。快速体验及 C01–C03 核对原始名称、标准费用、金额和币种。 |
| 5. 新增、增量更新 | `domain/matcher.py` 四级匹配。快速体验展示 `UPDATE` 和 `CREATE`，C05 对照两种不同历史基线。 |
| 6. 从记账主体判断 AR/AP | `domain/entity_resolver.py`。C01 双主体、C03 分公司收款证据、C06 贷项冲减。 |
| 7. 重复、冲突与复核 | `domain/validators.py`、`services/review_service.py`。C09、C10 核对错字、阻断和冲突，工作台可查看来源、编辑和裁决。 |
| 8. Harness 稳定性与准确性 | `harness/` 的 13 组 16 项真值与对抗回归，见 [Harness 设计与指标](docs/harness-design.md)。 |
| 9. 至少五项深度思考 | 3 项业务和 3 项技术与工程思考，见 [六项思考](docs/deep-thinking.md)。 |
| 10. Python + LangChain 实现 | `pyproject.toml` 固定项目环境，`agent/graph.py` 编排，`gateways/llm_client.py` 接入 OpenAI 兼容模型。 |

## 系统图与契约

可在浏览器直接打开这四个交互式 HTML 文件。

- [系统架构](docs/workflow/diagrams/architecture.html)
- [用户与复核业务流程](docs/workflow/diagrams/user-billing-flow.html)
- [核心处理时序](docs/workflow/diagrams/pipeline-sequence.html)
- [账单操作判定](docs/workflow/diagrams/operation-decision-flow.html)

标准 JSON 保留原件来源、抽取通道、旧账定位和判定依据。接口、表结构与输出格式见 [冻结契约](docs/workflow/contracts/) · [OpenAPI](docs/workflow/contracts/openapi.yaml) · [SQL](docs/workflow/contracts/schema.sql) · [账单 JSON Schema](docs/workflow/contracts/billing-output.schema.json)。

## 命令速查

| 命令 | 作用与参数 |
| --- | --- |
| `parse` | 接收文件、目录或可重复的 `--url`，以 `--entity` 指定主体。`--bills` 载入仅对本批生效的历史 JSON，`--out` 写标准 JSON，`--bundle-out` 另存工作台可导入的复核包。三种 `--output-mode` 为 `official`、`full_standard`、`audit`。`--strict` 遇失败或待复核项返回非零。 |
| `serve` | 同时启动 API 与已构建的工作台。默认 `127.0.0.1:8000`，可用 `--host`、`--port`、`--db-path`、`--ocr-mode` 指定运行方式。 |
| `eval` | `--mode` 取 `OFFLINE_DETERMINISTIC`、`LIVE_LLM` 或 `LIVE_E2E`。`--k-trials` 指定轮数，`--out` 写 JSON 报告，`--db-path` 可隔离评测历史。真实联机的前置和命令见 [联机复验](docs/harness-design.md#联机复验)。 |

`REMOTE_ONLY` 是 PDF、图片的默认严格 OCR 模式。提供网关可访问的公网文件 URL，或为本地文件配好受控中转与发布器后，调用指定远端 OCR 网关。这一模式的失败返回单据失败信息。`REMOTE_WITH_LOCAL_FALLBACK` 在本地文件缺少中转或远端识别失败时，回落到本地 PDF 文本提取及 RapidOCR 图片或扫描页识别。直接输入 `--url` 的文件仍使用远端 OCR。CLI 支持这两种模式，参数选项中没有 `LOCAL_ONLY`。

## 原题四份真实样本

完整 Harness 会从项目根目录的 `笔试题AIAGENT/` 自动发现原题文件。可在当前进程环境设置 `SHIPPING_BILLING_REAL_SAMPLES_DIR` 指向原题目录。保留原文件名，四项匹配各需要恰好一份文件。

- `图片示例.png`
- `PDF示例账单1.pdf`
- `PDF 示例账单2.pdf`
- `深圳容宇 PRE-ALERT*.eml`（保留原长文件名）

交付 ZIP 内置自包含 EML 与历史账单，完整评测所需的原题文件需另行放置。样本齐备后运行三轮离线 Harness。

```sh
uv run shipping-billing-agent eval --mode OFFLINE_DETERMINISTIC --k-trials 3 --out offline-k3.json
```

## 环境与安全配置

离线体验可直接使用默认值。联机时对照 [.env.example](.env.example)，在本地 `.env` 或进程环境中配置。真实密钥只留在本机环境，禁止写入仓库。样本和联机 OCR 样本 URL 两个 `SHIPPING_BILLING_*` 变量由 Harness 直接读取进程环境。

| 变量 | 默认值与用途 |
| --- | --- |
| `LLM_PROVIDER`、`LLM_BASE_URL`、`LLM_MODEL` | `openai_compatible`、`https://api.deepseek.com/v1`、`deepseek-flash`。可配置兼容端点和模型。 |
| `LLM_API_KEY`、`DEEPSEEK_API_KEY` | 均为空。优先 `LLM_API_KEY`，仅在配置为 HTTPS `api.deepseek.com`（含默认端点）且通用密钥为空时回退读取 `DEEPSEEK_API_KEY`。 |
| `LLM_TRUST_ENV`、`LLM_PROXY_URL`、`LLM_TIMEOUT_SECONDS`、`LLM_MAX_RETRIES` | `false`、空、`90` 秒、`0`。显式代理可填 URL，HTTP 重试与有界自修复分别计数。 |
| `OCR_GATEWAY_URL`、`OCR_TASK` | `https://gateway.pltplt.com/ai/process`、`orderChange`。网关须能读取输入文件 URL。 |
| `OCR_TRUST_ENV`、`OCR_PROXY_URL` | `false`、空。默认直连绕过本机环境代理，规避代理引发的 SSL EOF。 |
| `OCR_CONNECT_TIMEOUT_SECONDS`、`OCR_READ_TIMEOUT_SECONDS` | `10`、`300` 秒。 |
| `PUBLIC_RELAY_BASE_URL`、`RELAY_PRESIGN_TTL_SECONDS` | 空、`300` 秒。默认关闭中转。基址需要配合注入的发布器，对原件上传须取得授权。 |
| `APP_HOST`、`APP_PORT`、`ALLOW_REMOTE_BIND` | `127.0.0.1`、`8000`、`false`。默认仅接收本机连接。 |
| `APP_DATA_DIR`、`SQLITE_DB_PATH` | `data`、`data/shipping_billing.db`。工作台和评测默认使用本地数据目录与 SQLite，单次 `parse` 未指定 `--db-path` 时使用临时库。 |
| `SHIPPING_BILLING_REAL_SAMPLES_DIR`、`SHIPPING_BILLING_OCR_SAMPLE_URL` | 可选进程环境变量。前者覆盖原题目录，后者供 `LIVE_E2E` 验证 `图片示例.png` 的实际远端 OCR。 |

## 自动化验证与实测

全量自动化回归 `uv run pytest -p no:cacheprovider -q` 结果为 **1897 passed, 0 failed, 0 skipped (1040.68s)**。离线 Harness（`OFFLINE_DETERMINISTIC`, `k=3`）完成 **16/16 个用例、48 次执行**，十项核心指标及 `pass@3`、`pass^3` 均为 `1.0`，正常记录误拦率为 `0.0`（`0/69`）。真实大模型 `LIVE_LLM`（`deepseek-flash`, `k=3`, 115 次模型调用）中，**`C01`–`C04` 4 份原题真实样本及 `C05`、`C07`–`C12`、`C13/.xls,.xlsx` 共 13/16 项均达成 3/3 全通过（`pass^3 = 0.8125`，`pass@3 = 0.875`，`conflict_recall = 1.0 (21/21)`）**；详细分子、分母、真实浏览器实测边界与剩余 3 项合成夹具差异原因见 [Harness 设计报告](docs/harness-design.md)。

**`LIVE_E2E`（`gateway.pltplt.com` 远端 URL OCR）与 `self_repair_evidence` 当前仍为 `UNVERIFIED`。** 提供网关可访问的授权样本 URL 或受控中转发布器后可复验 `LIVE_E2E`。自修复证据须有携带原文和差额的第二轮请求，并有重验通过结果。

只使用 ZIP 内文件即可运行下面的交付检查。它在仓库外空目录启动服务并验证健康端点，再验证离线样例的两条正式出账及 Schema。

```sh
uv run pytest -p no:cacheprovider tests/harness/test_b05_delivery_package.py -q
```

## 纯净交付 ZIP

```sh
uv run python scripts/package_delivery.py
```

默认生成 `dist/shipping-billing-agent-delivery.zip`，可用 `--out` 指定路径。归档包含源码、锁文件、测试、离线样例、前端源码、文档和契约。脚本过滤环境文件、运行数据、缓存和原题文件，并扫描疑似密钥。

### 投递核对清单

- [ ] 收件人填写 `LUKA@PLTPLT.COM`、`pan@pltplt.com`。
- [ ] 邮件标题使用 `应聘渠道+姓名+应聘岗位`。
- [ ] 附上纯净代码 ZIP，或提供可审阅的 Git 仓库链接。
- [ ] 确认有可独立跑通的 [README](README.md)、[离线样例](examples/)、[Harness 报告](docs/harness-design.md)、[六项深度思考](docs/deep-thinking.md)。确认归档中无 `.env`、数据库或缓存。
