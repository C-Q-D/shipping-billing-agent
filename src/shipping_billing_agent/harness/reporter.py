"""Render standalone reports from a completed or interrupted Harness run."""

from __future__ import annotations

import json
from dataclasses import asdict
from html import escape
from typing import Any

from shipping_billing_agent.harness.runner import METRICS, EvalRunResult

_METRIC_NAMES = {
    "schema_valid_rate": "JSON Schema 通过率",
    "field_exact_match": "核心字段精确率",
    "ar_ap_accuracy": "AR/AP 准确率",
    "conflict_recall": "冲突拦截召回率",
    "amount_accuracy": "金额准确率",
    "currency_accuracy": "币种准确率",
    "operation_accuracy": "四态操作准确率",
    "false_positive_rate": "正常记录误拦率",
    "alert_precision": "告警精确率",
    "pass_at_k": "pass@k",
    "pass_pow_k": "pass^k",
}


def _cell(value: Any) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return (
        escape(text, quote=False)
        .replace("|", "&#124;")
        .replace("`", "&#96;")
        .replace("[", "&#91;")
        .replace("]", "&#93;")
        .replace("\n", "<br>")
    )


def render_json(result: EvalRunResult) -> str:
    return json.dumps(asdict(result), ensure_ascii=False, allow_nan=False, indent=2) + "\n"


def render_markdown(result: EvalRunResult) -> str:
    report = result.report_json
    latency = "NULL" if result.avg_latency_ms is None else f"{result.avg_latency_ms:.2f} ms"
    lines = [
        "# Harness 评测报告",
        "",
        f"**{result.status}**",
        "",
        f"- 运行编号 `{result.id}`，时间 `{result.created_at}`",
        f"- 模式 `{result.run_mode}`，模型 {_cell(result.model_name)}",
        f"- 完整轮次 {result.actual_trials} / {result.k_trials}",
        f"- 每轮用例 {result.total_cases} 项，累计完成 {report['executed_cases']} 项",
        f"- LLM 调用 {report['llm_calls']} 次，OCR 调用 {report['ocr_calls']} 次",
        f"- 单用例平均耗时 {latency}",
        f"- 自修复证据 **{report['self_repair_evidence']}**",
    ]
    if result.error_message:
        lines.extend(["", f"> {_cell(result.error_message)}"])

    lines.extend(
        [
            "",
            "## 指标",
            "",
            "| 指标 | 契约字段 | 比率 | 分子 | 分母 |",
            "| --- | --- | ---: | ---: | ---: |",
        ]
    )
    for key in METRICS:
        metric = report[key]
        rate = "NULL" if metric["rate"] is None else f"{metric['rate']:.2%}"
        lines.append(
            f"| {_METRIC_NAMES[key]} | `{key}` | {rate} |"
            f" {metric['numerator']} | {metric['denominator']} |"
        )

    lines.extend(["", "## 用例结果", ""])
    if report["case_results"]:
        lines.extend(
            [
                "| 轮次 | 组 | 用例 | 状态 | LLM 调用 | OCR 调用 | 耗时 (ms) |",
                "| ---: | --- | --- | --- | ---: | ---: | ---: |",
            ]
        )
        for case in report["case_results"]:
            lines.append(
                f"| {case['trial']} | {_cell(case['group_id'])} | {_cell(case['case_id'])} |"
                f" {case['status']} | {case['llm_calls']} | {case['ocr_calls']} |"
                f" {case['latency_ms']:.2f} |"
            )
    else:
        lines.append("已完成用例数为 0。")

    lines.extend(["", "## 预期与实际差异", ""])
    if report["failing_cases"]:
        lines.extend(
            [
                "| 轮次 | 用例 | 预期 | 实际 | 原因 |",
                "| ---: | --- | --- | --- | --- |",
            ]
        )
        for failure in report["failing_cases"]:
            lines.append(
                f"| {failure['trial']} | {_cell(failure['case_id'])} |"
                f" {_cell(failure['expected'])} | {_cell(failure['actual'])} |"
                f" {_cell(failure['reason'])} |"
            )
    else:
        lines.append("差异记录数为 0。")
    return "\n".join(lines) + "\n"
