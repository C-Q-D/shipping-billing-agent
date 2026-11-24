"""Execute the billing Harness and score the observed business outcomes."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Sequence
from contextlib import ExitStack
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from html import unescape
from itertools import zip_longest
from pathlib import Path
from statistics import fmean
from time import perf_counter
from typing import Any, Literal
from uuid import uuid4

import httpx
from jsonschema import Draft202012Validator
from pydantic import BaseModel

from shipping_billing_agent.agent.graph import run_billing_pipeline
from shipping_billing_agent.agent.rule_extractor import extract_document_by_rules
from shipping_billing_agent.config.settings import AppSettings, load_settings
from shipping_billing_agent.domain.entity_resolver import normalize_party_name
from shipping_billing_agent.domain.models import (
    ALERT_SEVERITY_MAP,
    AccountingEntityProfile,
    AlertCode,
    AlertSeverity,
    BatchPipelineResult,
    DocumentStatus,
    ExistingBillRecord,
    ExtractionChannel,
    OcrMode,
    OperationType,
    ParsedDocument,
    ProcessedBillingRecord,
    ReviewStatus,
    loads_decimal_json,
)
from shipping_billing_agent.domain.serializer import ExportMode, serialize_records_json
from shipping_billing_agent.gateways.llm_client import (
    FakeStructuredLlmClient,
    LlmGatewayError,
    LlmMessages,
    OpenAICompatibleLlmClient,
    StructuredLlmResponse,
)
from shipping_billing_agent.gateways.local_ocr import RapidOcrFallback
from shipping_billing_agent.gateways.ocr_client import (
    OcrGatewayError,
    OcrRecognitionResult,
    PresignedRelayPublisher,
    RemoteOcrClient,
)
from shipping_billing_agent.gateways.text_sanitizer import sanitize_for_llm
from shipping_billing_agent.gateways.url_guard import (
    redact_url_queries_in_text,
    validate_public_url,
)
from shipping_billing_agent.harness.dataset import (
    ATTACK_BANK_ACCOUNT,
    ATTACK_CASE_ID,
    ATTACK_EMAIL,
    ATTACK_PHONE,
    ATTACK_SIGNATURE,
    ATTACK_TEXT,
    NO_CHARGE_DETAILS,
    ROOT,
    EvalCase,
    EvalMode,
    ExpectedRecord,
    build_eval_cases,
    resolve_real_sample,
)
from shipping_billing_agent.parsers.dispatcher import DocumentParserDispatcher

EvalStatus = Literal["COMPLETED", "FAILED", "UNVERIFIED"]
OCR_SAMPLE_URL_ENV = "SHIPPING_BILLING_OCR_SAMPLE_URL"
METRICS = (
    "schema_valid_rate",
    "field_exact_match",
    "ar_ap_accuracy",
    "conflict_recall",
    "amount_accuracy",
    "currency_accuracy",
    "operation_accuracy",
    "false_positive_rate",
    "alert_precision",
    "pass_at_k",
    "pass_pow_k",
)
_CORE_FIELDS = (
    "reference_no",
    "charge_item",
    "amount",
    "currency",
    "issuer",
    "recipient",
    "accounting_entity",
    "posting_type",
    "debit_credit",
    "payment_term",
)
_PARTY_FIELDS = {"issuer", "recipient", "accounting_entity"}
_CONFLICTS = {
    AlertCode.AMBIGUOUS_EXISTING_BILL,
    AlertCode.CROSS_DOCUMENT_CONFLICT,
    AlertCode.DUPLICATE_LINE_EXTRACTED,
    AlertCode.MULTI_SOURCE_BL_MISMATCH,
}
_READY = {ReviewStatus.AUTO_READY, ReviewStatus.APPROVED}
_SOURCE = re.compile(
    r'<UNTRUSTED_DOCUMENT_CONTENT doc_id="([^"]+)">\n(.*?)\n</UNTRUSTED_DOCUMENT_CONTENT>',
    re.DOTALL,
)
_DIAGNOSTICS = "\n\nFirst-pass validation differences:\n"
_EMPTY_MODEL_CHARGES = "LLM 抽取结果缺少有效费用明细"


@dataclass(frozen=True, slots=True)
class EvalRunResult:
    id: str
    run_mode: EvalMode
    status: EvalStatus
    error_message: str | None
    model_name: str
    k_trials: int
    actual_trials: int
    total_cases: int
    schema_valid_rate: float | None
    field_exact_match: float | None
    ar_ap_accuracy: float | None
    operation_accuracy: float | None
    conflict_recall: float | None
    pass_at_k: float | None
    pass_pow_k: float | None
    avg_latency_ms: float | None
    report_json: dict[str, Any]
    created_at: str

    def to_eval_run_row(self) -> dict[str, Any]:
        row = asdict(self)
        row.pop("avg_latency_ms")  # Timing is stored inside the report in the frozen SQL contract.
        row["report_json"] = json.dumps(self.report_json, ensure_ascii=False, allow_nan=False)
        return row

    def to_eval_run_summary(self) -> dict[str, Any]:
        summary = asdict(self)
        summary.pop("avg_latency_ms")
        summary["report"] = summary.pop("report_json")
        return summary


def meets_thresholds(
    result: EvalRunResult, *, min_pass_at_k: float = 1.0, min_pass_pow_k: float = 1.0
) -> bool:
    if not all(0.0 <= threshold <= 1.0 for threshold in (min_pass_at_k, min_pass_pow_k)):
        raise ValueError("通过阈值必须在 0 到 1 之间")
    return (
        result.status == "COMPLETED"
        and result.pass_at_k is not None
        and result.pass_at_k >= min_pass_at_k
        and result.pass_pow_k is not None
        and result.pass_pow_k >= min_pass_pow_k
    )


@dataclass(slots=True)
class _MetricCount:
    numerator: int = 0
    denominator: int = 0

    def add(self, numerator: int, denominator: int = 1) -> None:
        self.numerator += numerator
        self.denominator += denominator

    def report(self, complete: bool) -> dict[str, float | int | None]:
        return {
            "rate": self.numerator / self.denominator if complete and self.denominator else None,
            "numerator": self.numerator,
            "denominator": self.denominator,
        }


def _new_counts() -> dict[str, _MetricCount]:
    return {metric: _MetricCount() for metric in METRICS}


def _report_value(value: Any) -> Any:
    if isinstance(value, str):
        return redact_url_queries_in_text(sanitize_for_llm(value))
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {str(k): _report_value(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [_report_value(v) for v in value]
    if isinstance(value, (set, frozenset)):
        return [_report_value(v) for v in sorted(value)]
    return value


@dataclass(frozen=True, slots=True)
class _PromptEvidence:
    doc_id: str | None
    source_hash: str | None
    diagnostics_hash: str | None
    sanitized: bool
    attack_isolated: bool


def _prompt_evidence(messages: Any) -> _PromptEvidence:
    texts = [
        message["content"]
        for message in messages
        if isinstance(message, dict) and isinstance(message.get("content"), str)
    ]
    users = [
        message["content"]
        for message in messages
        if isinstance(message, dict)
        and message.get("role") == "user"
        and isinstance(message.get("content"), str)
    ]
    prompt = users[0] if len(users) == 1 else ""
    source = _SOURCE.search(prompt)
    snippet = source[2].strip() if source else ""
    before, after = (prompt[: source.start()], prompt[source.end() :]) if source else ("", "")
    diagnostics = after.partition(_DIAGNOSTICS)[2]
    sanitized = all(text == sanitize_for_llm(text) for text in texts)
    isolated = (
        bool(snippet)
        and prompt.count("<UNTRUSTED_DOCUMENT_CONTENT ") == 1
        and prompt.count("</UNTRUSTED_DOCUMENT_CONTENT>") == 1
        and ATTACK_TEXT in snippet
        and ATTACK_TEXT not in before + after
        and "&lt;/UNTRUSTED_DOCUMENT_CONTENT&gt;" in snippet
        and "&#91;SYSTEM&#93;" in snippet
        and all(
            marker not in text
            for marker in (
                ATTACK_BANK_ACCOUNT,
                ATTACK_SIGNATURE,
                ATTACK_EMAIL,
                ATTACK_PHONE,
                "[SYSTEM]",
            )
            for text in texts
        )
        and sum(text.count(ATTACK_TEXT) for text in texts) == 1
    )
    return _PromptEvidence(
        source[1] if source else None,
        sha256(snippet.encode("utf-8")).hexdigest() if snippet else None,
        sha256(unescape(diagnostics).encode("utf-8")).hexdigest() if diagnostics else None,
        sanitized,
        isolated,
    )


class _ObservedLlm(OpenAICompatibleLlmClient):
    """Observe the real HTTPX request after the gateway has sanitized and encoded it."""

    def __init__(self, settings: AppSettings, transport: httpx.BaseTransport | None):
        # The Harness terminates on infrastructure failures. Disable SDK retries for this run.
        super().__init__(
            settings.model_copy(update={"llm_max_retries": 0}),
            transport=transport,
            async_transport=transport if isinstance(transport, httpx.AsyncBaseTransport) else None,
        )
        self.requests: list[_PromptEvidence] = []
        self.parseable_answers: dict[str, bool] = {}
        self.failure: str | None = None
        self.require_attack_evidence = False
        self._http_client.event_hooks["request"].append(self._inspect_request)
        self._http_client.event_hooks["response"].append(self._inspect_response)

    def _inspect_request(self, request: httpx.Request) -> None:
        body = json.loads(request.content)
        evidence = _prompt_evidence(body["messages"])
        if (
            not evidence.sanitized
            or not evidence.source_hash
            or (self.require_attack_evidence and not evidence.attack_isolated)
        ):
            self.failure = "LLM HTTP 请求正文未通过脱敏或不可信文档边界核查"
            raise LlmGatewayError(self.failure)
        self.requests.append(evidence)

    def _inspect_response(self, response: httpx.Response) -> None:
        if not response.is_success:
            self.failure = self.failure or f"LLM 网关返回 HTTP {response.status_code}"

    def invoke_structured(
        self, messages: LlmMessages, schema: type[BaseModel]
    ) -> StructuredLlmResponse[Any]:
        if self.failure:
            raise LlmGatewayError(self.failure)
        try:
            response = super().invoke_structured(messages, schema)
        except LlmGatewayError as exc:
            # The in-flight request is counted even when the gateway never responds.
            # Keep the gateway's error class without copying its response body into a report.
            self.failure = self.failure or str(exc).partition("）：")[0].rstrip("）") + "）"
            raise LlmGatewayError(self.failure) from exc
        doc_id = _prompt_evidence(messages).doc_id
        if doc_id is not None:
            try:
                parsed = loads_decimal_json(response.raw_text) if response.raw_text.strip() else {}
                is_object = isinstance(parsed, dict) and (
                    bool(response.raw_text.strip()) or response.parsed is not None
                )
            except (ValueError, InvalidOperation, RecursionError):
                is_object = False
            self.parseable_answers[doc_id] = is_object
        return response


class _ObservedLocalOcr(RapidOcrFallback):
    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    def recognize_image_bytes(self, content: bytes, filename: str) -> tuple[str, float]:
        self.calls += 1
        return super().recognize_image_bytes(content, filename)

    def recognize_pil_image(self, image: Any, page: int = 1) -> tuple[str, float]:
        self.calls += 1
        return super().recognize_pil_image(image, page)


class _ObservedRemoteOcr(RemoteOcrClient):
    def __init__(self, settings: AppSettings, transport: httpx.BaseTransport | None):
        super().__init__(settings, transport=transport)
        self.calls = 0
        self.failure: str | None = None

    def recognize_url(self, url: str) -> OcrRecognitionResult:
        self.calls += 1
        try:
            return super().recognize_url(url)
        except OcrGatewayError as exc:
            self.failure = str(exc)
            raise


@dataclass(slots=True)
class _RemoteInput:
    url: str | None
    publisher: PresignedRelayPublisher | None
    ttl_seconds: int
    prepared: tuple[Path, str, float] | None = None

    def publish(self, path: Path) -> str:
        assert self.publisher is not None
        return validate_public_url(
            self.publisher.publish(path.read_bytes(), path.name, ttl_seconds=self.ttl_seconds)
        )

    def for_sample(self, path: Path) -> str:
        if self.url:
            return self.url
        if self.prepared:
            prepared_path, url, expires_at = self.prepared
            self.prepared = None
            if path == prepared_path and perf_counter() < expires_at:
                return url
        return self.publish(path)


class _ExecutionFailure(RuntimeError):
    def __init__(self, reason: str, *, expected: Any = None, actual: Any = None) -> None:
        super().__init__(reason)
        self.expected = expected if expected is not None else "完成当前用例并返回可评分结果"
        self.actual = actual if actual is not None else reason


def _ready(record: ExpectedRecord | ProcessedBillingRecord) -> bool:
    return record.review_status in _READY and record.operation in (
        OperationType.CREATE,
        OperationType.UPDATE,
        OperationType.NO_CHANGE,
    )


def _core(record: ExpectedRecord | ProcessedBillingRecord, names: Sequence[str]) -> tuple[Any, ...]:
    return tuple(
        normalize_party_name(getattr(record, name))
        if name in _PARTY_FIELDS
        else getattr(record, name)
        for name in names
    )


def _pair_rows(
    expected: Sequence[ExpectedRecord], actual: Sequence[ProcessedBillingRecord]
) -> list[tuple[ExpectedRecord | None, ProcessedBillingRecord | None]]:
    """Pair by exact fields first, then keep independently scoreable errors on the same line."""
    available = list(actual)
    matches: dict[int, ProcessedBillingRecord] = {}
    for fields in (
        (*_CORE_FIELDS, "operation", "review_status"),
        ("reference_no", "charge_item", "amount", "currency"),
        ("reference_no", "charge_item"),
    ):
        for index, truth in enumerate(expected):
            if index in matches:
                continue
            key = _core(truth, fields)
            for actual_index, row in enumerate(available):
                if key == _core(row, fields):
                    matches[index] = available.pop(actual_index)
                    break
    remaining = iter(available)
    pairs = [
        (truth, matches[index] if index in matches else next(remaining, None))
        for index, truth in enumerate(expected)
    ]
    pairs.extend(zip_longest((), remaining))
    return pairs


def _alerts(
    row: ExpectedRecord | ProcessedBillingRecord | None,
) -> set[tuple[AlertCode, AlertSeverity]]:
    if row is None:
        return set()
    if isinstance(row, ExpectedRecord):
        return {(code, ALERT_SEVERITY_MAP[code]) for code in row.alerts}
    return {
        (alert.code, alert.severity)
        for alert in row.validation_alerts
        if alert.severity is not AlertSeverity.RESOLVED
    }


def _row_values(row: ExpectedRecord | ProcessedBillingRecord | None) -> dict[str, Any] | None:
    if row is None:
        return None
    values = {
        **{name: getattr(row, name) for name in _CORE_FIELDS},
        "operation": row.operation,
        "review_status": row.review_status,
        "alerts": sorted(_alerts(row)),
    }
    if isinstance(row, ExpectedRecord) and row.optional_alerts:
        values["optional_alerts"] = list(row.optional_alerts)
    return values


def _passed_repair(result: BatchPipelineResult, requests: Sequence[_PromptEvidence]) -> bool:
    for row in result.records:
        if (
            not row.retry_count
            or not _ready(row)
            or "第 2 轮自修复复检通过" not in row.ai_reasoning
        ):
            continue
        doc_requests = [receipt for receipt in requests if receipt.doc_id == row.source_doc_id]
        if len(doc_requests) != 2:
            continue
        first, second = doc_requests
        initial_difference = next(
            (
                reason.partition("\n")[2]
                for reason in row.ai_reasoning
                if reason.startswith("首轮校验差异\n")
            ),
            "",
        )
        if not initial_difference:
            continue
        expected = sha256(sanitize_for_llm(initial_difference).encode("utf-8")).hexdigest()
        if (
            first.source_hash
            and first.source_hash == second.source_hash
            and (first.diagnostics_hash is None and second.diagnostics_hash == expected)
        ):
            return True
    return False


@dataclass(slots=True)
class _CaseExecution:
    mode: EvalMode
    case: EvalCase
    schema: Draft202012Validator
    local_parser: DocumentParserDispatcher
    live_llm: _ObservedLlm | None
    remote_ocr: _ObservedRemoteOcr | None
    remote_input: _RemoteInput | None
    sample_path: Path | None = None
    checks: list[dict[str, Any]] = field(default_factory=list)
    counts: dict[str, _MetricCount] = field(default_factory=_new_counts)
    llm_document_ids: tuple[str, ...] = ()
    llm_calls: int = 0
    outbound_security_verified: bool = False
    repaired: bool = False

    def sample_documents(self) -> list[ParsedDocument]:
        path = self.sample_path
        if path is None:
            raise _ExecutionFailure("用例没有声明样本文件")
        if self.mode != "LIVE_E2E" or not self.case.remote_ocr:
            return self.parse([(path.name, path.read_bytes())])

        assert self.remote_input is not None and self.remote_ocr is not None
        self.remote_ocr.failure = None
        dispatcher = DocumentParserDispatcher(
            ocr_mode=OcrMode.REMOTE_ONLY,
            remote_ocr=self.remote_ocr,
            url_validator=validate_public_url,
        )
        calls_before = self.remote_ocr.calls
        (doc,) = dispatcher.parse_batch([], [self.remote_input.for_sample(path)])
        if self.remote_ocr.failure:
            raise _ExecutionFailure(self.remote_ocr.failure)
        checked_rows = 0
        requested_remote = self.remote_ocr.calls == calls_before + 1
        if (
            requested_remote
            and doc.status is DocumentStatus.PARSED
            and doc.extraction_channel is ExtractionChannel.REMOTE_OCR
            and doc.ocr_payload is not None
        ):
            if doc.ocr_payload.has_charges and any(
                item.charge_list for item in doc.ocr_payload.items
            ):
                checked_rows = sum(
                    bool(row.charge.raw_charge_name.strip()) and row.charge.amount is not None
                    for row in extract_document_by_rules(doc).charges
                )
        if not checked_rows:
            raise _ExecutionFailure(
                "REMOTE_OCR 未返回可用费用明细",
                expected={"channel": "REMOTE_OCR", "valid_charge_rows": "至少 1 条"},
                actual={
                    "channel": doc.extraction_channel,
                    "valid_charge_rows": checked_rows,
                    "error": doc.error_message,
                    "gateway_called": requested_remote,
                },
            )
        self.check("远端 OCR 来源", "REMOTE_OCR", doc.extraction_channel)
        self.check("远端 OCR 有有效费用明细", True, checked_rows > 0)
        return [doc]

    def parse(self, files: Sequence[tuple[str, bytes]]) -> list[ParsedDocument]:
        documents = self.local_parser.parse_batch(files)
        failed = [
            {"filename": doc.sanitized_filename, "reason": doc.error_message}
            for doc in documents
            if doc.status is DocumentStatus.FAILED
        ]
        if failed:
            raise _ExecutionFailure(
                f"{failed[0]['filename']} 解析失败：{failed[0]['reason']}", actual=failed
            )
        return documents

    def run(
        self,
        documents: Sequence[ParsedDocument],
        entity: str,
        *,
        existing_bills: Sequence[ExistingBillRecord] = (),
        profiles: Sequence[AccountingEntityProfile] = (),
        offline_reply: dict[str, Any] | None = None,
        rule_only: bool = False,
    ) -> BatchPipelineResult:
        live = self.live_llm if not rule_only else None
        fake = None
        if live is not None:
            live.requests.clear()
            live.parseable_answers.clear()
            live.failure = None
            live.require_attack_evidence = self.case.case_id == ATTACK_CASE_ID
        elif not rule_only and offline_reply is not None:
            fake = FakeStructuredLlmClient(
                StructuredLlmResponse(None, json.dumps(offline_reply), None, "offline-fake")
            )
        try:
            result = run_billing_pipeline(
                documents,
                entity,
                existing_bills=existing_bills,
                profiles=profiles,
                llm_client=live or fake,
            )
        finally:
            requests = (
                live.requests
                if live is not None
                else [_prompt_evidence(messages) for messages, _ in fake.sync_calls]
                if fake is not None
                else []
            )
            self.llm_calls += len(requests)
            self.llm_document_ids = tuple(receipt.doc_id or "" for receipt in requests)
        if live is not None and live.failure:
            raise _ExecutionFailure(live.failure)
        self.outbound_security_verified = bool(requests) and all(
            receipt.attack_isolated and receipt.sanitized for receipt in requests
        )
        if live is not None and requests:
            self.check(
                f"{entity} HTTP 脱敏请求数",
                len(requests),
                sum(receipt.sanitized for receipt in requests),
            )
        self.repaired |= _passed_repair(result, requests)
        return result

    def check(self, label: str, expected: Any, actual: Any) -> None:
        self._record_check(label, expected, actual, expected == actual)

    def _record_check(self, label: str, expected: Any, actual: Any, passed: bool) -> None:
        self.checks.append(
            {
                "check": label,
                "passed": passed,
                "expected": _report_value(expected),
                "actual": _report_value(actual),
            }
        )

    def _score_row(
        self, label: str, expected: ExpectedRecord | None, actual: ProcessedBillingRecord | None
    ) -> None:
        paired = expected is not None and actual is not None
        exact = paired and _core(expected, _CORE_FIELDS) == _core(actual, _CORE_FIELDS)
        operation = paired and expected.operation == actual.operation
        self.counts["field_exact_match"].add(exact)
        self.counts["operation_accuracy"].add(operation)
        for metric, name in (
            ("ar_ap_accuracy", "posting_type"),
            ("amount_accuracy", "amount"),
            ("currency_accuracy", "currency"),
        ):
            self.counts[metric].add(paired and getattr(expected, name) == getattr(actual, name))

        expected_alerts, actual_alerts = _alerts(expected), _alerts(actual)
        optional = (
            {(code, ALERT_SEVERITY_MAP[code]) for code in expected.optional_alerts}
            if expected is not None
            else set()
        )
        true_alerts = (expected_alerts | optional) & actual_alerts
        self.counts["alert_precision"].add(len(true_alerts), len(actual_alerts))
        for code, severity in expected_alerts:
            if code in _CONFLICTS:
                self.counts["conflict_recall"].add(
                    actual is not None and not _ready(actual) and (code, severity) in true_alerts
                )
        if expected is not None and _ready(expected):
            self.counts["false_positive_rate"].add(actual is not None and not _ready(actual))
        self._record_check(
            label,
            _row_values(expected),
            _row_values(actual),
            exact
            and operation
            and expected.review_status == actual.review_status
            and expected_alerts <= actual_alerts <= expected_alerts | optional,
        )

    def score(
        self,
        label: str,
        result: BatchPipelineResult,
        records: Sequence[ExpectedRecord],
        *,
        failure_doc_id: str | None = None,
        export_modes: Sequence[ExportMode] = ("official", "audit"),
    ) -> dict[ExportMode, list[dict[str, Any]]]:
        required = [(failure_doc_id, NO_CHARGE_DETAILS, [])] if failure_doc_id is not None else []
        observed = [
            (card.doc_id, card.reason, card.affected_record_ids)
            for card in result.failed_document_alerts
        ]
        for card in result.failed_document_alerts:
            # A valid model answer with missing charge rows is still a scored omission.
            missing_answer_rows = (
                self.live_llm is not None
                and self.live_llm.parseable_answers.get(card.doc_id, False)
                and card.reason.removeprefix("第 2 轮自修复失败。")
                in (
                    _EMPTY_MODEL_CHARGES,
                    f"{_EMPTY_MODEL_CHARGES}。{NO_CHARGE_DETAILS}",
                )
            )
            expected_card = card.doc_id == failure_doc_id and card.reason == NO_CHARGE_DETAILS
            if not expected_card and not missing_answer_rows:
                raise _ExecutionFailure(
                    f"{label} · {card.filename}：{card.reason}",
                    expected=required,
                    actual=[card.reason],
                )
        self.check(f"{label} 业务失败卡", required, observed)
        self.check(f"{label} 记录数", len(records), len(result.records))
        for index, (truth, row) in enumerate(_pair_rows(records, result.records), start=1):
            self._score_row(f"{label} 第 {index} 行", truth, row)
        counts = {
            "official": sum(_ready(record) for record in records),
            "full_standard": sum(
                record.review_status is not ReviewStatus.REJECTED for record in records
            ),
            "audit": len(records),
        }
        return {
            mode: self.export(label, result.records, mode, counts[mode]) for mode in export_modes
        }

    def export(
        self,
        label: str,
        records: Sequence[ProcessedBillingRecord],
        mode: ExportMode,
        expected_count: int,
    ) -> list[dict[str, Any]]:
        try:
            items = loads_decimal_json(serialize_records_json(records, mode))
        except (TypeError, ValueError) as exc:
            self.counts["schema_valid_rate"].add(0)
            self.check(f"{label} {mode} JSON", "可序列化的账单数组", str(exc))
            return []

        count = len(items) if isinstance(items, list) else None
        self.check(f"{label} {mode} 导出数", expected_count, count)
        error = next(self.schema.iter_errors(items), None)
        if items or expected_count or error is not None:
            valid = error is None and bool(items)
            self.counts["schema_valid_rate"].add(valid)
            description = (
                f"JSON Schema {list(error.absolute_path)}: {error.validator}"
                if error is not None
                else "空导出缺少可供校验的账单"
            )
            self.check(f"{label} {mode} Schema", "通过", "通过" if valid else description)
        return items if error is None else []


@dataclass(slots=True)
class _Run:
    mode: EvalMode
    k_trials: int
    cases: tuple[EvalCase, ...]
    id: str = field(default_factory=lambda: str(uuid4()))
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    model_name: str = "deterministic-rules"
    actual_trials: int = 0
    llm_calls: int = 0
    ocr_calls: int = 0
    repaired: bool = False
    case_results: list[dict[str, Any]] = field(default_factory=list)
    failing_cases: list[dict[str, Any]] = field(default_factory=list)
    counts: dict[str, _MetricCount] = field(default_factory=_new_counts)

    def finish(self, status: EvalStatus, error: str | None = None) -> EvalRunResult:
        completed = status == "COMPLETED"
        scored = [row for row in self.case_results if row["status"] != "ERROR"]
        if completed:
            passes: dict[str, list[bool]] = {case.case_id: [] for case in self.cases}
            for row in scored:
                passes[row["case_id"]].append(row["status"] == "PASSED")
            self.counts["pass_at_k"].add(sum(map(any, passes.values())), len(passes))
            self.counts["pass_pow_k"].add(
                sum(len(trials) == self.k_trials and all(trials) for trials in passes.values()),
                len(passes),
            )
        metrics = {kind: counts.report(completed) for kind, counts in self.counts.items()}
        latency = fmean(row["latency_ms"] for row in scored) if completed else None
        report = {
            **metrics,
            "avg_latency_ms": latency,
            "executed_cases": len(scored),
            "llm_calls": self.llm_calls,
            "ocr_calls": self.ocr_calls,
            "self_repair_evidence": "VERIFIED" if self.repaired else "UNVERIFIED",
            "failing_cases": self.failing_cases,
            "case_results": self.case_results,
        }
        return EvalRunResult(
            id=self.id,
            created_at=self.created_at,
            run_mode=self.mode,
            status=status,
            error_message=_report_value(error),
            model_name=self.model_name,
            k_trials=self.k_trials,
            actual_trials=self.actual_trials,
            total_cases=len(self.cases),
            schema_valid_rate=metrics["schema_valid_rate"]["rate"],
            field_exact_match=metrics["field_exact_match"]["rate"],
            ar_ap_accuracy=metrics["ar_ap_accuracy"]["rate"],
            operation_accuracy=metrics["operation_accuracy"]["rate"],
            conflict_recall=metrics["conflict_recall"]["rate"],
            pass_at_k=metrics["pass_at_k"]["rate"],
            pass_pow_k=metrics["pass_pow_k"]["rate"],
            avg_latency_ms=latency,
            report_json=report,
        )

    def record(
        self,
        case: EvalCase,
        trial: int,
        execution: _CaseExecution | None,
        latency_ms: float,
        llm_calls: int,
        ocr_calls: int,
        error: Exception | None = None,
    ) -> None:
        self.llm_calls += llm_calls
        self.ocr_calls += ocr_calls
        identity = {"trial": trial, "group_id": case.group_id, "case_id": case.case_id}
        checks = execution.checks if execution is not None else []
        if error is not None:
            reason = f"{type(error).__name__}: {error}"
            failure = {
                **identity,
                "reason": _report_value(reason),
                "expected": _report_value(getattr(error, "expected", "完成当前用例")),
                "actual": _report_value(getattr(error, "actual", reason)),
            }
            self.failing_cases.append(failure)
            status = "ERROR"
        else:
            assert execution is not None
            self.repaired |= execution.repaired
            for metric, counts in execution.counts.items():
                self.counts[metric].add(counts.numerator, counts.denominator)
            failures = [check for check in checks if not check["passed"]]
            if failures:
                self.failing_cases.extend(
                    {
                        **identity,
                        "expected": check["expected"],
                        "actual": check["actual"],
                        "reason": check["check"],
                    }
                    for check in failures
                )
            status = "FAILED" if failures else "PASSED"
        self.case_results.append(
            {
                **identity,
                "status": status,
                "latency_ms": latency_ms,
                "llm_calls": llm_calls,
                "ocr_calls": ocr_calls,
                "checks": checks,
            }
        )


def _remote_input(
    settings: AppSettings,
    ocr_sample_url: str | None,
    relay_publisher: PresignedRelayPublisher | None,
) -> _RemoteInput:
    url = (os.getenv(OCR_SAMPLE_URL_ENV, "") if ocr_sample_url is None else ocr_sample_url).strip()
    if url:
        return _RemoteInput(validate_public_url(url), None, settings.relay_presign_ttl_seconds)
    if settings.is_public_relay_ready(relay_publisher):
        return _RemoteInput(None, relay_publisher, settings.relay_presign_ttl_seconds)
    raise ValueError(
        f"请通过 {OCR_SAMPLE_URL_ENV} 提供图片示例.png 的公网 URL，"
        "或同时配置 PUBLIC_RELAY_BASE_URL 与 relay_publisher"
    )


def run_eval_suite(
    run_mode: EvalMode = "OFFLINE_DETERMINISTIC",
    k_trials: int = 3,
    *,
    samples_dir: Path | None = None,
    settings: AppSettings | None = None,
    cases: Sequence[EvalCase] | None = None,
    ocr_sample_url: str | None = None,
    relay_publisher: PresignedRelayPublisher | None = None,
    llm_transport: httpx.BaseTransport | None = None,
    ocr_transport: httpx.BaseTransport | None = None,
) -> EvalRunResult:
    """Run the selected suite synchronously. LIVE_E2E recognizes the original PNG remotely."""
    if run_mode not in ("OFFLINE_DETERMINISTIC", "LIVE_LLM", "LIVE_E2E"):
        raise ValueError(f"未知评测模式 {run_mode!r}")
    if type(k_trials) is not int or not 1 <= k_trials <= 10:
        raise ValueError("k_trials 必须是 1 到 10 之间的整数")
    suite = build_eval_cases() if cases is None else tuple(cases)
    if not suite or len({case.case_id for case in suite}) != len(suite):
        raise ValueError("评测套件必须包含至少一个用例，且用例 ID 必须唯一")
    run = _Run(run_mode, k_trials, suite)
    remote_input = None
    if run_mode != "OFFLINE_DETERMINISTIC":
        try:
            settings = settings if settings is not None else load_settings()
        except ValueError:
            return run.finish("UNVERIFIED", "LLM 或 OCR 配置格式无效，请检查运行配置")
        run.model_name = settings.llm_model
        if (
            settings.llm_base_url is None
            or settings.llm_api_key is None
            or not settings.is_llm_configured()
        ):
            return run.finish("UNVERIFIED", "请配置可用的 LLM_BASE_URL 和 LLM_API_KEY")
        if run_mode == "LIVE_E2E":
            first_remote = next((case for case in suite if case.remote_ocr), None)
            if first_remote is None:
                return run.finish("UNVERIFIED", "LIVE_E2E 需要至少一个远端 OCR 样本用例")
            try:
                remote_input = _remote_input(settings, ocr_sample_url, relay_publisher)
            except ValueError as exc:
                return run.finish("UNVERIFIED", f"远端 OCR 输入不可用：{exc}")
            if remote_input.publisher is not None:
                try:
                    path = resolve_real_sample(first_remote, samples_dir)
                    assert path is not None
                except OSError as exc:
                    run.record(first_remote, 1, None, 0, 0, 0, exc)
                    return run.finish("FAILED", str(exc))
                try:
                    expires = perf_counter() + remote_input.ttl_seconds
                    remote_input.prepared = (path, remote_input.publish(path), expires)
                except Exception as exc:
                    return run.finish(
                        "UNVERIFIED",
                        f"文件中转未就绪（{type(exc).__name__}），请检查中转配置与公网访问",
                    )

    schema_path = ROOT / "docs" / "workflow" / "contracts" / "billing-output.schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema, format_checker=Draft202012Validator.FORMAT_CHECKER)
    local_ocr = _ObservedLocalOcr()
    local_parser = DocumentParserDispatcher(
        ocr_mode=OcrMode.REMOTE_WITH_LOCAL_FALLBACK,
        local_ocr=local_ocr,
    )

    with ExitStack() as resources:
        live_llm = None
        remote_ocr = None
        if run_mode != "OFFLINE_DETERMINISTIC":
            assert settings is not None
            try:
                live_llm = _ObservedLlm(settings, llm_transport)
            except LlmGatewayError:
                return run.finish("UNVERIFIED", "LLM 客户端初始化失败，请检查端点、代理与超时配置")
            resources.callback(live_llm.close)
            if remote_input is not None:
                remote_ocr = _ObservedRemoteOcr(settings, ocr_transport)

        for trial in range(1, k_trials + 1):
            for case in suite:
                start = perf_counter()
                execution = _CaseExecution(
                    run_mode,
                    case,
                    validator,
                    local_parser,
                    live_llm,
                    remote_ocr,
                    remote_input,
                )
                error = None
                try:
                    execution.sample_path = resolve_real_sample(case, samples_dir)
                    case.exercise(execution)
                    if not execution.checks:
                        raise _ExecutionFailure("用例未产出评分断言")
                except Exception as exc:
                    error = exc
                ocr_calls = (
                    local_ocr.calls + (remote_ocr.calls if remote_ocr else 0) - run.ocr_calls
                )
                run.record(
                    case,
                    trial,
                    execution,
                    (perf_counter() - start) * 1000,
                    execution.llm_calls,
                    ocr_calls,
                    error,
                )
                if error is not None:
                    return run.finish("FAILED", f"第 {trial} 轮 {case.case_id}：{error}")
            run.actual_trials = trial
    return run.finish("COMPLETED")
