"""Compile the batch billing pipeline and enforce its per-run model budget."""

from __future__ import annotations

import asyncio
import re
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from functools import partial
from threading import Lock
from typing import NotRequired, TypedDict

import anyio
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from shipping_billing_agent.config.limits import ResourceLimits
from shipping_billing_agent.domain import (
    AccountingEntityProfile,
    AlertCode,
    BatchPipelineResult,
    ChargeDictionaryItem,
    DocumentExtractionBundle,
    ExistingBillRecord,
    FailedDocumentAlert,
    ParsedDocument,
    ProcessedBillingRecord,
    RawChargeItem,
    RawDocumentExtraction,
    RawExtractionEnvelope,
    SourceEvidence,
    ValidationAlert,
    dumps_decimal_json,
    match_and_inherit_batch,
    normalize_charge_item,
    parse_llm_raw_extraction,
    resolve_entity_perspective,
    validate_batch,
)
from shipping_billing_agent.domain.models import DocumentStatus, SourceKind
from shipping_billing_agent.gateways.llm_client import StructuredLlmClient
from shipping_billing_agent.gateways.text_sanitizer import sanitize_for_llm

from .prompts import (
    build_extraction_messages,
    build_self_correction_messages,
    format_validation_alerts,
)
from .rule_extractor import (
    _non_billing_reason,
    apply_sibling_context,
    extract_document_by_rules,
    transport_context,
)

_REPAIRABLE_CHECKS = frozenset(
    {
        AlertCode.AMOUNT_TOTAL_MISMATCH,
        AlertCode.AMOUNT_SUBTOTAL_TAX_MISMATCH,
        AlertCode.LINE_ARITHMETIC_MISMATCH,
        AlertCode.OCR_NUMERIC_CONFUSION,
        AlertCode.DUPLICATE_LINE_EXTRACTED,
    }
)
_BUDGET_EXHAUSTED = "已用完批次 LLM 调用额度"
_DEFAULT_LIMITS = ResourceLimits()
_SHEET_ROW = re.compile(r"^\[Sheet: (.+?) \| Row (\d+)\] \[Row (\d+)\]")
_TABLE_ROW = re.compile(r"^\[[^]]+\] \[Row (\d+)\]")
_SiblingSources = Sequence[tuple[ParsedDocument, RawExtractionEnvelope]]


class LlmBudgetGuard:
    """Reserve one attempted call per acquisition across all workers in a batch."""

    def __init__(self, max_calls: int) -> None:
        if max_calls < 0:
            raise ValueError("max_calls must be non-negative")
        self._max_calls = max_calls
        self._calls_used = 0
        self._async_lock = asyncio.Lock()
        self._thread_lock = Lock()

    @property
    def calls_used(self) -> int:
        with self._thread_lock:
            return self._calls_used

    @property
    def remaining(self) -> int:
        return self._max_calls - self.calls_used

    async def try_acquire(self) -> bool:
        async with self._async_lock:
            with self._thread_lock:
                if self._calls_used >= self._max_calls:
                    return False
                self._calls_used += 1
                return True


class BillingGraphInput(TypedDict):
    documents: Sequence[ParsedDocument]
    accounting_entity: str
    existing_bills: NotRequired[Sequence[ExistingBillRecord]]
    charge_dictionary: NotRequired[Sequence[ChargeDictionaryItem]]
    profiles: NotRequired[Sequence[AccountingEntityProfile]]
    batch_id: NotRequired[str | None]


class _BillingGraphOutput(TypedDict):
    result: BatchPipelineResult


class _BillingState(BillingGraphInput, _BillingGraphOutput, total=False):
    bundles: list[DocumentExtractionBundle]
    rule_extractions: dict[str, RawExtractionEnvelope]
    package_sources: dict[str, tuple[tuple[ParsedDocument, RawExtractionEnvelope], ...]]
    paths: dict[str, str]
    budget: LlmBudgetGuard
    limiter: anyio.CapacityLimiter
    failures: dict[str, str]
    doc_retry_count: dict[str, int]
    correction_alerts: dict[str, list[ValidationAlert]]
    correction_logs: dict[str, list[str]]
    correction_failures: set[str]
    records: list[ProcessedBillingRecord]


@dataclass(frozen=True, slots=True)
class _ModelAttempt:
    extraction: RawExtractionEnvelope | None = None
    failure: str | None = None
    invoked: bool = False


def _package_id(doc: ParsedDocument, by_id: dict[str, ParsedDocument]) -> str:
    seen = set()
    while doc.source_kind is not SourceKind.EML and doc.parent_doc_id:
        seen.add(doc.doc_id)
        parent = doc.parent_doc_id
        if parent in seen:
            return min(seen)
        if parent not in by_id:
            return parent
        doc = by_id[parent]
    return doc.doc_id


def _source_path(doc: ParsedDocument, by_id: dict[str, ParsedDocument]) -> str:
    parts = []
    seen = set()
    while doc.doc_id not in seen:
        seen.add(doc.doc_id)
        parts.append(doc.public_url or doc.original_filename)
        if doc.parent_doc_id not in by_id:
            break
        doc = by_id[doc.parent_doc_id]
    return "/".join(reversed(parts))


def _source_evidence(doc: ParsedDocument, charge: RawChargeItem, path: str) -> SourceEvidence:
    source_text = charge.source_text or ""
    sheet = row = None
    if match := _SHEET_ROW.match(source_text):
        sheet, start, relative = match.groups()
        row = int(start) + int(relative) - 1
    elif match := _TABLE_ROW.match(source_text):
        row = int(match[1])
    return SourceEvidence(
        source_file=doc.original_filename,
        source_doc_id=doc.doc_id,
        source_paths=[path],
        page=charge.page,
        sheet=sheet,
        row=row,
        line_number=charge.line_number,
        extraction_channel=doc.extraction_channel,
        snippet=sanitize_for_llm(source_text) if source_text else None,
    )


def _records_for_bundle(
    bundle: DocumentExtractionBundle,
    state: _BillingState,
    extra_reasoning: Sequence[str] = (),
    *,
    source_extraction: RawExtractionEnvelope | None = None,
) -> list[ProcessedBillingRecord]:
    doc, envelope = bundle.document, bundle.extraction
    if envelope is None or envelope.metadata_only:
        return []
    data = envelope.extraction
    refs = data.shipment_refs
    if source_extraction is not None and doc.source_kind is not SourceKind.EML_BODY:
        own_mbls = source_extraction.extraction.shipment_refs.mbl_nos
        if own_mbls:
            # Validate attachments' independent MBL evidence before merging divergent references.
            own_values = {ref.value for ref in own_mbls}
            refs = refs.model_copy(
                update={
                    "mbl_nos": [ref for ref in refs.mbl_nos if ref.value in own_values],
                }
            )
    records = []
    for index, entry in enumerate(envelope.charges):
        charge = entry.charge
        standard_name, from_dictionary = normalize_charge_item(
            charge.raw_charge_name, state.get("charge_dictionary", ())
        )
        perspective = resolve_entity_perspective(
            state["accounting_entity"],
            issuer=data.issuer,
            recipient=data.recipient,
            payer_hint=data.payer_hint,
            payee_hint=data.payee_hint,
            document_type=data.document_type,
            debit_credit=charge.debit_credit,
            raw_amount=charge.amount,
            profiles=state.get("profiles", ()),
        )
        batch_id = state.get("batch_id")
        record_prefix = f"{batch_id}:{doc.doc_id}" if batch_id else doc.doc_id
        records.append(
            ProcessedBillingRecord(
                id=f"{record_prefix}:{index + 1}",
                batch_id=batch_id,
                source_doc_id=doc.doc_id,
                source_line_index=index,
                document_type=data.document_type,
                reference_no=data.reference_no,
                document_date=data.document_date,
                issuer=data.issuer,
                recipient=data.recipient,
                payer_hint=data.payer_hint,
                payee_hint=data.payee_hint,
                charge_item=standard_name,
                raw_charge_name=charge.raw_charge_name,
                matched_from_dictionary=from_dictionary,
                quantity=charge.quantity,
                unit_price=charge.unit_price,
                amount=perspective.amount,
                currency=charge.currency,
                payment_term=charge.payment_term,
                accounting_entity=state["accounting_entity"],
                posting_type=perspective.posting_type,
                debit_credit=charge.debit_credit,
                is_credit=perspective.is_credit,
                raw_explicit_amount=charge.amount,
                raw_amount_abs=perspective.raw_amount_abs,
                raw_amount_text=entry.raw_amount_text,
                raw_unit_price_text=entry.raw_unit_price_text,
                raw_quantity_text=entry.raw_quantity_text,
                shipment_refs=refs,
                source_evidence=_source_evidence(doc, charge, state["paths"][doc.doc_id]),
                validation_alerts=perspective.validation_alerts,
                ai_reasoning=[*perspective.ai_reasoning, *extra_reasoning],
                excluded_clauses=data.excluded_clauses,
                explicit_fields=envelope.explicit_fields | entry.explicit_fields,
                raw_invalid_fields={**envelope.raw_invalid_fields, **entry.raw_invalid_fields},
                raw_parse_issues=[*envelope.raw_parse_issues, *entry.raw_parse_issues],
                retry_count=bundle.retry_count,
            )
        )
    return records


def _sibling_context_text(sources: _SiblingSources) -> str:
    context = []
    for doc, envelope in sources:
        data = envelope.extraction
        facts = {
            "source": doc.sanitized_filename,
            "shipment_refs": data.shipment_refs.model_dump(exclude_none=True),
            "shipper_or_issuer": data.issuer,
            "consignee_or_recipient": data.recipient,
            "transport_terms": doc.metadata.get("transport_context"),
        }
        context.append(dumps_decimal_json({key: value for key, value in facts.items() if value}))
    return "\n".join(context)


def _ground_model_extraction(
    extraction: RawExtractionEnvelope, rules: RawExtractionEnvelope
) -> RawExtractionEnvelope:
    """Use an independently read, printed total to check the model's proposal."""
    data = extraction.extraction
    issues = list(extraction.raw_parse_issues)
    updates = {}
    for field in ("subtotal_amount", "tax_amount", "total_amount", "total_currency"):
        observed = getattr(rules.extraction, field)
        proposed = getattr(data, field)
        if observed is None or field in extraction.raw_invalid_fields:
            continue
        if proposed is not None and proposed != observed:
            code = {
                "subtotal_amount": AlertCode.AMOUNT_SUBTOTAL_TAX_MISMATCH,
                "tax_amount": AlertCode.AMOUNT_SUBTOTAL_TAX_MISMATCH,
                "total_amount": AlertCode.AMOUNT_TOTAL_MISMATCH,
                "total_currency": AlertCode.INVALID_OR_MISSING_CURRENCY,
            }[field]
            issues.append(
                ValidationAlert.from_code(
                    code,
                    "模型报告的汇总值与原文不符",
                    field=f"reported_{field}",
                    expected=str(observed),
                    actual=str(proposed),
                )
            )
        updates[field] = observed
    for field in ("payer_hint", "payee_hint"):
        if (observed := getattr(rules.extraction, field)) and not getattr(data, field):
            updates[field] = observed
    clauses = [
        clause.model_copy(update={"text": sanitize_for_llm(clause.text)})
        for clause in data.excluded_clauses
    ]
    for clause in rules.extraction.excluded_clauses:
        if clause not in clauses:
            clauses.append(clause)
    charges = []
    for row in extraction.charges:
        charge = row.charge
        if _non_billing_reason(f"{charge.raw_charge_name} {charge.source_text or ''}"):
            continue
        charges.append(row)
    if (
        any(row.charge.amount for row in rules.charges)
        and charges
        and all(row.charge.amount == 0 for row in charges)
    ):
        issues.append(
            ValidationAlert.from_code(
                AlertCode.LLM_EXTRACTION_FAILED,
                "源单存在非零费用，模型提取的费用全部为零",
                field="amount",
                actual="0",
            )
        )
    return extraction.model_copy(
        update={
            "extraction": data.model_copy(
                update={
                    **updates,
                    "excluded_clauses": clauses,
                    "charges": [row.charge for row in charges],
                }
            ),
            "charges": charges,
            "raw_parse_issues": issues,
            "explicit_fields": extraction.explicit_fields | updates.keys(),
            "has_explicit_total": rules.has_explicit_total or extraction.has_explicit_total,
            "totals_by_currency": rules.totals_by_currency or extraction.totals_by_currency,
            # Metadata-only eligibility is decided locally, before making a model call.
            "metadata_only": False,
        }
    )


def _has_source(doc: ParsedDocument) -> bool:
    return bool(doc.raw_text.strip() or doc.tables or doc.ocr_payload)


def _amount_snapshot(extraction: RawExtractionEnvelope) -> str:
    values = ", ".join(
        f"{row.charge.amount:f}" if row.charge.amount is not None else "缺失"
        for row in extraction.charges
    )
    total = extraction.total_amount
    return f"明细 [{values}]，总额 {format(total, 'f') if total is not None else '缺失'}"


class _BillingNodes:
    def __init__(
        self,
        client: StructuredLlmClient | None,
        extractor_mode: str,
        limits: ResourceLimits,
        *,
        use_sync_client: bool = False,
    ) -> None:
        self.client = client if extractor_mode != "rule_only" else None
        self.limits = limits
        self.use_sync_client = use_sync_client

    def compile(self) -> CompiledStateGraph:
        graph = StateGraph(
            _BillingState, input_schema=BillingGraphInput, output_schema=_BillingGraphOutput
        )
        graph.add_node("build_context_packages", self.build_context_packages)
        graph.add_node("extract_documents", self.extract_documents)
        graph.add_node("validate_raw_documents", self.validate_raw_documents)
        graph.add_node("self_correct_document", self.self_correct_document)
        graph.add_node("normalize_match_and_validate", self.normalize_match_and_validate)
        graph.add_node("finalize_output", self.finalize_output)

        graph.add_edge(START, "build_context_packages")
        graph.add_edge("build_context_packages", "extract_documents")
        graph.add_edge("extract_documents", "validate_raw_documents")
        graph.add_conditional_edges(
            "validate_raw_documents",
            self.should_self_correct,
            {
                True: "self_correct_document",
                False: "normalize_match_and_validate",
            },
        )
        graph.add_edge("self_correct_document", "validate_raw_documents")
        graph.add_edge("normalize_match_and_validate", "finalize_output")
        graph.add_edge("finalize_output", END)
        return graph.compile()

    async def build_context_packages(self, state: _BillingState) -> dict:
        groups = defaultdict(list)
        by_id = {doc.doc_id: doc for doc in state["documents"]}
        paths = {doc_id: _source_path(doc, by_id) for doc_id, doc in by_id.items()}
        bundles = []
        failures = {}
        rule_extractions = {}
        for document in state["documents"]:
            doc = document
            envelope = None
            if doc.status is DocumentStatus.FAILED:
                failures[doc.doc_id] = sanitize_for_llm(doc.error_message or "文档解析失败")
            elif doc.status is not DocumentStatus.SKIPPED_NOISE:
                try:
                    envelope = extract_document_by_rules(doc)
                except Exception as exc:
                    failures[doc.doc_id] = f"文档规则抽取失败（{type(exc).__name__}）"
                else:
                    rule_extractions[doc.doc_id] = envelope
                    if envelope.metadata_only:
                        metadata = doc.metadata.copy()
                        if context := transport_context(doc):
                            metadata["transport_context"] = context
                        doc = doc.model_copy(
                            update={
                                "status": DocumentStatus.METADATA_ONLY,
                                "metadata": metadata,
                            }
                        )
                    groups[_package_id(doc, by_id)].append((doc, envelope))
            bundles.append(DocumentExtractionBundle(document=doc, extraction=envelope))
        package_sources = {}
        for sources in groups.values():
            sources.sort(key=lambda source: (source[0].sanitized_filename, source[0].doc_id))
            for doc, _ in sources:
                package_sources[doc.doc_id] = tuple(
                    (sibling, envelope)
                    for sibling, envelope in sources
                    if sibling.doc_id != doc.doc_id and _has_source(sibling)
                )
        return {
            "bundles": bundles,
            "rule_extractions": rule_extractions,
            "package_sources": package_sources,
            "paths": paths,
            "budget": LlmBudgetGuard(self.limits.max_llm_calls_per_batch),
            "limiter": anyio.CapacityLimiter(self.limits.max_concurrency),
            "failures": failures,
            "doc_retry_count": {},
            "correction_logs": {},
            "correction_failures": set(),
        }

    async def _invoke(
        self, state: _BillingState, messages: list[dict[str, str]], rules: RawExtractionEnvelope
    ) -> _ModelAttempt:
        client = self.client
        assert client is not None
        budget = state["budget"]
        if not budget.remaining:
            return _ModelAttempt(failure=_BUDGET_EXHAUSTED)
        # The borrowed client's gateway handles the request timeout. Every new call here
        # reserves a budget slot after it receives an available concurrency slot.
        async with state["limiter"]:
            if not await budget.try_acquire():
                return _ModelAttempt(failure=_BUDGET_EXHAUSTED)
            try:
                if self.use_sync_client:
                    response = await anyio.to_thread.run_sync(
                        client.invoke_structured, messages, RawDocumentExtraction
                    )
                else:
                    response = await client.ainvoke_structured(messages, RawDocumentExtraction)
                envelope = parse_llm_raw_extraction(response.raw_text, response.parsed)
                envelope = _ground_model_extraction(envelope, rules)
            except Exception as exc:
                return _ModelAttempt(
                    failure=f"LLM 调用或解码失败（{type(exc).__name__}）", invoked=True
                )
        if not any(row.charge.raw_charge_name.strip() for row in envelope.charges):
            reason = "LLM 抽取结果缺少有效费用明细"
            if not any(
                issue.code is AlertCode.LLM_EXTRACTION_FAILED for issue in envelope.raw_parse_issues
            ):
                envelope.raw_parse_issues.append(
                    ValidationAlert.from_code(AlertCode.LLM_EXTRACTION_FAILED, reason)
                )
            return _ModelAttempt(envelope, reason, invoked=True)
        return _ModelAttempt(envelope, invoked=True)

    async def extract_documents(self, state: _BillingState) -> dict:
        bundles = state["bundles"]
        if self.client is None:
            return {}

        async def extract(bundle: DocumentExtractionBundle) -> _ModelAttempt:
            doc = bundle.document
            assert bundle.extraction is not None
            try:
                messages = build_extraction_messages(
                    doc,
                    accounting_entity=state["accounting_entity"],
                    sibling_context=_sibling_context_text(state["package_sources"][doc.doc_id]),
                )
            except Exception as exc:
                return _ModelAttempt(failure=f"无法准备 LLM 抽取证据（{type(exc).__name__}）")
            return await self._invoke(state, messages, bundle.extraction)

        candidates = [
            bundle
            for bundle in bundles
            if bundle.document.status is DocumentStatus.PARSED and bundle.extraction is not None
        ]
        attempts = await asyncio.gather(*(extract(bundle) for bundle in candidates))
        failures = state["failures"].copy()
        extracted = {}
        for bundle, attempt in zip(candidates, attempts, strict=True):
            doc_id = bundle.document.doc_id
            if attempt.failure:
                failures[doc_id] = attempt.failure
            extracted[doc_id] = bundle.model_copy(update={"extraction": attempt.extraction})
        return {
            "bundles": [extracted.get(bundle.document.doc_id, bundle) for bundle in bundles],
            "failures": failures,
        }

    def validate_raw_documents(self, state: _BillingState) -> dict:
        correction_alerts = {}
        for bundle in state["bundles"]:
            doc, envelope = bundle.document, bundle.extraction
            if envelope is None or envelope.metadata_only or not _has_source(doc):
                continue
            rows = _records_for_bundle(bundle, state)
            alerts = [
                alert
                for row in validate_batch(rows, [bundle])
                for alert in row.validation_alerts
                if alert.code in _REPAIRABLE_CHECKS
            ]
            # Missing amount on an incremental notice can be filled by the historical bill.
            # Retry explicit extraction failures here and let inheritance handle omitted fields.
            raw_issues = [*envelope.raw_parse_issues]
            for entry in envelope.charges:
                raw_issues.extend(entry.raw_parse_issues)
            alerts.extend(
                issue for issue in raw_issues if issue.code is AlertCode.LLM_EXTRACTION_FAILED
            )
            if alerts:
                correction_alerts[doc.doc_id] = alerts
        return {"correction_alerts": correction_alerts}

    def _can_retry(self, doc_id: str, state: _BillingState) -> bool:
        return (
            doc_id in state["correction_alerts"]
            and doc_id not in state["correction_failures"]
            and state["doc_retry_count"].get(doc_id, 0)
            < min(1, self.limits.max_doc_self_correction_rounds)
        )

    def should_self_correct(self, state: _BillingState) -> bool:
        return (
            self.client is not None
            and state["budget"].remaining > 0
            and any(self._can_retry(doc_id, state) for doc_id in state["correction_alerts"])
        )

    async def self_correct_document(self, state: _BillingState) -> dict:
        async def correct(bundle: DocumentExtractionBundle) -> _ModelAttempt:
            doc, previous = bundle.document, bundle.extraction
            assert previous is not None
            try:
                messages = build_self_correction_messages(
                    doc,
                    previous,
                    state["correction_alerts"][doc.doc_id],
                    accounting_entity=state["accounting_entity"],
                )
            except Exception as exc:
                return _ModelAttempt(failure=f"无法准备 LLM 自修复证据（{type(exc).__name__}）")
            return await self._invoke(state, messages, state["rule_extractions"][doc.doc_id])

        bundles = state["bundles"]
        candidates = [
            bundle for bundle in bundles if self._can_retry(bundle.document.doc_id, state)
        ]
        attempts = await asyncio.gather(*(correct(bundle) for bundle in candidates))
        failures = state["failures"].copy()
        retry_counts = state["doc_retry_count"].copy()
        logs = state["correction_logs"].copy()
        correction_failures = state["correction_failures"].copy()
        corrected = {}
        for bundle, attempt in zip(candidates, attempts, strict=True):
            doc_id = bundle.document.doc_id
            previous = bundle.extraction
            assert previous is not None
            if not attempt.invoked and attempt.failure == _BUDGET_EXHAUSTED:
                continue
            retry_counts[doc_id] = int(attempt.invoked)
            logs[doc_id] = [
                "首轮校验差异\n" + format_validation_alerts(state["correction_alerts"][doc_id]),
                "首轮提取：" + _amount_snapshot(previous),
            ]
            if attempt.failure:
                correction_failures.add(doc_id)
                failures[doc_id] = f"第 2 轮自修复失败。{attempt.failure}"
                issue = ValidationAlert.from_code(AlertCode.LLM_EXTRACTION_FAILED, failures[doc_id])
                envelope = previous.model_copy(
                    update={
                        "raw_parse_issues": [*previous.raw_parse_issues, issue],
                    }
                )
                logs[doc_id].append(failures[doc_id])
            else:
                envelope = attempt.extraction
                assert envelope is not None
                failures.pop(doc_id, None)
                logs[doc_id].append("第 2 轮提取：" + _amount_snapshot(envelope))
            corrected[doc_id] = bundle.model_copy(
                update={
                    "extraction": envelope,
                    "retry_count": retry_counts[doc_id],
                }
            )
        return {
            "bundles": [corrected.get(bundle.document.doc_id, bundle) for bundle in bundles],
            "failures": failures,
            "doc_retry_count": retry_counts,
            "correction_logs": logs,
            "correction_failures": correction_failures,
        }

    def normalize_match_and_validate(self, state: _BillingState) -> dict:
        records = []
        enriched_bundles = []
        for bundle in state["bundles"]:
            doc, envelope = bundle.document, bundle.extraction
            if envelope is None or envelope.metadata_only or not envelope.charges:
                enriched_bundles.append(bundle)
                continue
            enriched, context_reasoning = apply_sibling_context(
                envelope,
                state["package_sources"][doc.doc_id],
                state.get("profiles", ()),
            )
            enriched_bundle = bundle.model_copy(update={"extraction": enriched})
            enriched_bundles.append(enriched_bundle)
            reasoning = [*context_reasoning, *state["correction_logs"].get(doc.doc_id, ())]
            if bundle.retry_count and doc.doc_id not in state["failures"]:
                checks = state["correction_alerts"].get(doc.doc_id)
                reasoning.append(
                    "第 2 轮复检仍有差异\n" + format_validation_alerts(checks)
                    if checks
                    else "第 2 轮自修复复检通过"
                )
            records.extend(
                _records_for_bundle(enriched_bundle, state, reasoning, source_extraction=envelope)
            )
        matched = match_and_inherit_batch(
            records, state.get("existing_bills", ()), state.get("profiles", ())
        )
        return {"records": validate_batch(matched, state["bundles"]), "bundles": enriched_bundles}

    def finalize_output(self, state: _BillingState) -> dict:
        records_by_doc = defaultdict(list)
        for record in state["records"]:
            records_by_doc[record.source_doc_id].append(record.id)
        failures = [
            FailedDocumentAlert(
                doc_id=doc.doc_id,
                filename=doc.original_filename,
                reason=state["failures"][doc.doc_id],
                affected_record_ids=records_by_doc[doc.doc_id],
            )
            for doc in state["documents"]
            if doc.doc_id in state["failures"]
        ]
        budget = state["budget"]
        exhausted = _BUDGET_EXHAUSTED in state["failures"].values() or (
            self.client is not None
            and budget.remaining == 0
            and any(self._can_retry(doc_id, state) for doc_id in state["correction_alerts"])
        )
        return {
            "result": BatchPipelineResult(
                accounting_entity=state["accounting_entity"],
                documents=state["bundles"],
                records=state["records"],
                failed_document_alerts=failures,
                failed_doc_ids=[failure.doc_id for failure in failures],
                llm_calls_used=budget.calls_used,
                warnings=[_BUDGET_EXHAUSTED] if exhausted else [],
            )
        }


def build_billing_graph(
    llm_client: StructuredLlmClient | None = None,
    *,
    extractor_mode: str = "auto",
    limits: ResourceLimits = _DEFAULT_LIMITS,
) -> CompiledStateGraph:
    """Compile a graph whose ainvoke accepts BillingGraphInput and returns {'result': ...}."""
    return _BillingNodes(llm_client, extractor_mode, limits).compile()


async def arun_billing_pipeline(
    documents: Sequence[ParsedDocument],
    accounting_entity: str,
    *,
    llm_client: StructuredLlmClient | None = None,
    extractor_mode: str = "auto",
    limits: ResourceLimits = _DEFAULT_LIMITS,
    existing_bills: Sequence[ExistingBillRecord] = (),
    charge_dictionary: Sequence[ChargeDictionaryItem] = (),
    profiles: Sequence[AccountingEntityProfile] = (),
    batch_id: str | None = None,
) -> BatchPipelineResult:
    """Run the batch on the current event loop with the borrowed async client."""
    graph = build_billing_graph(llm_client, extractor_mode=extractor_mode, limits=limits)
    result = await graph.ainvoke(
        BillingGraphInput(
            documents=documents,
            accounting_entity=accounting_entity,
            existing_bills=existing_bills,
            charge_dictionary=charge_dictionary,
            profiles=profiles,
            batch_id=batch_id,
        )
    )
    return result["result"]


def run_billing_pipeline(
    documents: Sequence[ParsedDocument],
    accounting_entity: str,
    *,
    llm_client: StructuredLlmClient | None = None,
    extractor_mode: str = "auto",
    limits: ResourceLimits = _DEFAULT_LIMITS,
    existing_bills: Sequence[ExistingBillRecord] = (),
    charge_dictionary: Sequence[ChargeDictionaryItem] = (),
    profiles: Sequence[AccountingEntityProfile] = (),
    batch_id: str | None = None,
) -> BatchPipelineResult:
    """Run the same graph using the borrowed synchronous client on worker threads."""
    graph = _BillingNodes(llm_client, extractor_mode, limits, use_sync_client=True).compile()
    result = anyio.run(
        partial(
            graph.ainvoke,
            BillingGraphInput(
                documents=documents,
                accounting_entity=accounting_entity,
                existing_bills=existing_bills,
                charge_dictionary=charge_dictionary,
                profiles=profiles,
                batch_id=batch_id,
            ),
        )
    )
    return result["result"]
