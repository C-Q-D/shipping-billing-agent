# B05 >=10组真值与对抗样本Harness评测套件、Harness与深度思考报告、README与交付ZIP打包验收 — 独立验证报告

- **验证基线与提交范围**：`379b334`（B05 T01）→ `7a739a0`（B05 T02）→ `554c999`（B05 T03）→ `f6bf90f`（Wave 13A 全栈阻塞修复）→ `0771c17`（Wave 13B 根治 `verifier r1` 全部 7 项边界缺陷与 null 汇总）→ `c098aa4`（Wave 13C 补齐 ZIM 船代 `alternative` 分支、复核底栏滚动留白与基线文档对齐）。
- **本次自测**：
  - `uv run ruff check --no-cache src tests scripts/package_delivery.py` -> 退出 `0`（All checks passed）
  - `uv run python -B scripts/verify_contracts.py` -> 退出 `0`（`schema.sql` 8 表、`billing-output.schema.json` 176 币种与 6 负例、4 张交互式 SVG 图全通过）
  - `node frontend/src/api/decimalJson.test.ts` -> 退出 `0`（`PASS 1`–`PASS 35` 全绿）
  - `npm --prefix frontend run typecheck && npm --prefix frontend run build` -> 退出 `0`（47 模块、`built in 1.05s`）
  - `uv run pytest -p no:cacheprovider tests/unit/test_config_and_domain.py tests/unit/test_b02_agent_graph.py tests/unit/test_b02_pipeline.py tests/integration/test_b02_pipeline.py tests/harness/test_b05_delivery_package.py -q`（`c098aa4`）-> **`627 passed in 34.91s`**（0 failed, 0 skipped）
  - 全仓 `uv run pytest -p no:cacheprovider -q`（`0771c17` 由独立 `verifier r2` 实跑）-> **`1789 passed in 668.59s`**（0 failed, 0 skipped）；`OFFLINE_DETERMINISTIC` (`k=3`) 实跑（`48` 次执行）：`schema_valid_rate=1.0 (150/150)`、`field_exact_match/ar_ap_accuracy/operation_accuracy/amount_accuracy/currency_accuracy=1.0 (141/141)`、`conflict_recall=1.0 (21/21)`、`false_positive_rate=0.0 (0/69)`、`alert_precision=1.0 (111/111)`、`pass@3=pass^3=1.0 (16/16)`。
  - 纯净交付包 `uv run python scripts/package_delivery.py --out ".wf-tmp/shipping-billing-agent-delivery.zip"`（`c098aa4`）：`129 个条目 | 948368 字节`，仓库外空白目录独立重建 `.venv`、前端 `build`、健康检查 `200` 与 `examples/sample_debit_note.eml` 离线 `parse` 出账 `UPDATE 40.00 USD PP` + `CREATE 120.50 USD CC` 全通，且对非白名单杂文件与 6 种密钥模式均拒绝。
- **独立审查与复验结论**：
  - 独立代码审查 `verifier`（`codex/gpt-6.2-astra`）：`r1` 指出 F1–F7 阻塞项 -> `r2`（`0771c17`）逐条实测复现确认 F1–F7 及 `null` 金额预览问题全部清零，结论 **`PASS`**（`.wf-tmp/out-B05-review-r2.md`）；
  - 实时全栈浏览器 `browser_tester`（`codex/gpt-6.2-sol`，`http://127.0.0.1:18880`）：`r1 -> r2` 验证 `OCA17` 非空 `official` 导出 `245.0000`、真实 `EML` `POST /approve 200 -> APPROVED -> 重载持久回显`、`390×844` 触控 `38px`/页签 `36px`、零 `/favicon.ico 404`；`r3`（`c098aa4`）定点复验 `RV3-01`（ZIM 两条均消除 `payee_hint` 冲突）与 `RV3-02`（`1440×900` 底栏不再遮挡定位按钮、点击真聚焦）均 **`PASS`**，最终结论 **`通过可交付用户`**（`.wf-tmp/out-wave13c-browser-reverify.md`）。
- **关键状态运行证据（截图/日志路径）**：
  - 离线 `k=3` 评测 JSON：`f:\demo\ocr\.wf-tmp\b05-offline-eval-k3.json`
  - 全栈浏览器实测截图目录：`f:\demo\ocr\.wf-tmp\screenshots\wave12-e2e\`、`f:\demo\ocr\.wf-tmp\screenshots\wave13-e2e\`、`f:\demo\ocr\.wf-tmp\screenshots\wave13b-e2e\`、`f:\demo\ocr\.wf-tmp\screenshots\wave13c-e2e\`
- **转入 backlog.md 的遗留项**：无（注：需真实外部密钥与授权公网对象存储中转的 `LIVE_LLM`、`LIVE_E2E` 与联机 `self_repair_evidence` 在文档与代码汇总中均严格标注 `UNVERIFIED`）