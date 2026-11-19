"""Compile the batch billing pipeline and enforce its per-run model budget."""

from __future__ import annotations

import asyncio
import re
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from fractions import Fraction
from functools import partial
from threading import Lock
from typing import Any, NotRequired, TypedDict

import anyio
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from shipping_billing_agent.config.limits import ResourceLimits
from shipping_billing_agent.domain import (
    DECIMAL_TOLERANCE,
    AccountingEntityProfile,
    AlertCode,
    BatchPipelineResult,
    ChargeDictionaryItem,
    DocumentExtractionBundle,
    ExistingBillRecord,
    FailedDocumentAlert,
    ParsedDocument,
    ProcessedBillingRecord,
    RawChargeEnvelope,
    RawChargeItem,
    RawDocumentExtraction,
    RawExtractionEnvelope,
    SourceDocTotalsSnapshot,
    SourceEvidence,
    ValidationAlert,
    dumps_decimal_json,
    entity_matches,
    match_and_inherit_batch,
    normalize_charge_item,
    parse_llm_raw_extraction,
    resolve_entity_perspective,
    validate_batch,
)
from shipping_billing_agent.domain.entity_resolver import FIELD_FORMAT_ALERT_CODES
from shipping_billing_agent.domain.models import DocumentStatus, SourceKind, sanitize_source_path
from shipping_billing_agent.gateways.llm_client import (
    LlmBudgetExhaustedError,
    StructuredLlmClient,
    llm_request_budget,
)
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
_NO_CHARGE_DETAILS = "文档包含账单总额声明但未提取到有效费用明细"
_LINE_TOLERANCE = Fraction(DECIMAL_TOLERANCE)
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
            return self.try_acquire_sync()

    def try_acquire_sync(self) -> bool:
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
    package_ids: dict[str, str]
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
        parts.append(sanitize_source_path(doc.public_url or doc.original_filename) or "")
        if doc.parent_doc_id not in by_id:
            break
        doc = by_id[doc.parent_doc_id]
    return "/".join(reversed(parts))


def _redact_document_paths(doc: ParsedDocument) -> ParsedDocument:
    paths = {
        field: sanitize_source_path(getattr(doc, field))
        for field in ("public_url", "original_filename", "sanitized_filename")
    }
    return doc.model_copy(update=paths)


def _source_evidence(doc: ParsedDocument, charge: RawChargeItem, path: str) -> SourceEvidence:
    source_text = charge.source_text or ""
    sheet = row = None
    if match := _SHEET_ROW.match(source_text):
        sheet, start, relative = match.groups()
        row = int(start) + int(relative) - 1
    elif match := _TABLE_ROW.match(source_text):
        row = int(match[1])
    return SourceEvidence(
        source_file=sanitize_source_path(doc.original_filename),
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
) -> list[ProcessedBillingRecord]:
    doc, envelope = bundle.document, bundle.extraction
    if envelope is None or envelope.metadata_only:
        return []
    data = envelope.extraction
    totals = SourceDocTotalsSnapshot.from_extraction(doc.doc_id, envelope)
    records = []
    for index, entry in enumerate(envelope.charges):
        charge = entry.charge
        standard_name, from_dictionary = normalize_charge_item(
            charge.raw_charge_name, state.get("charge_dictionary", ())
        )
        if standard_name is None:
            continue
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
        raw_issues = [*envelope.raw_parse_issues, *entry.raw_parse_issues]
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
                shipment_refs=data.shipment_refs,
                source_evidence=_source_evidence(doc, charge, state["paths"][doc.doc_id]),
                source_doc_totals=totals,
                validation_alerts=perspective.validation_alerts,
                ai_reasoning=[
                    *perspective.ai_reasoning,
                    *(
                        issue.message
                        for issue in raw_issues
                        if issue.code is AlertCode.CONTEXT_INFERRED_FIELD
                    ),
                    *extra_reasoning,
                ],
                excluded_clauses=data.excluded_clauses,
                explicit_fields=envelope.explicit_fields | entry.explicit_fields,
                raw_invalid_fields={**envelope.raw_invalid_fields, **entry.raw_invalid_fields},
                raw_parse_issues=raw_issues,
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
            "billing_issuer": data.issuer,
            "billing_recipient": data.recipient,
            "transport_context": doc.metadata.get("transport_context"),
        }
        context.append(dumps_decimal_json({key: value for key, value in facts.items() if value}))
    return "\n".join(context)


def _source_audit(
    proposed: RawExtractionEnvelope | RawChargeEnvelope,
    observed: RawExtractionEnvelope | RawChargeEnvelope,
    values: RawDocumentExtraction | RawChargeItem,
) -> dict[str, Any]:
    invalid = {
        **proposed.raw_invalid_fields,
        **{
            field: token
            for field, token in observed.raw_invalid_fields.items()
            if getattr(values, field, None) is None
        },
    }
    explicit = proposed.explicit_fields | observed.explicit_fields
    issues = list(proposed.raw_parse_issues)
    for issue in observed.raw_parse_issues:
        if issue.code in FIELD_FORMAT_ALERT_CODES and issue.field:
            if getattr(values, issue.field, None) is not None:
                continue
            if issue.field in type(values).model_fields and issue.field in explicit:
                invalid.setdefault(issue.field, issue.actual or "")
        if issue not in issues:
            issues.append(issue)
    return {
        "explicit_fields": explicit,
        "raw_invalid_fields": invalid,
        "raw_parse_issues": issues,
    }


def _ground_charge(proposed: RawChargeEnvelope, observed: RawChargeEnvelope) -> RawChargeEnvelope:
    printed, model = observed.charge, proposed.charge
    updates = {
        field: value
        for field in ("source_text", "page", "line_number")
        if (value := getattr(printed, field)) is not None
    }
    # Only independent source evidence can disambiguate a printed bare dollar sign.
    bare_dollar = observed.raw_invalid_fields.get("currency") == "$" or any(
        issue.field == "currency" and issue.actual == "$" for issue in observed.raw_parse_issues
    )
    currency_conflict = printed.currency and model.currency and printed.currency != model.currency
    if bare_dollar or printed.currency:
        updates["currency"] = printed.currency
    if printed.payment_term is not None and model.payment_term is None:
        updates["payment_term"] = printed.payment_term
    charge = model.model_copy(update=updates)
    audit = _source_audit(proposed, observed, charge)
    if currency_conflict:
        audit["raw_parse_issues"].append(
            ValidationAlert.from_code(
                AlertCode.INVALID_OR_MISSING_CURRENCY,
                "模型提取的费用行币种与原文不符",
                field="currency",
                expected=printed.currency,
                actual=model.currency,
            )
        )
    for field in ("amount", "unit_price", "quantity"):
        text_field = f"raw_{field}_text"
        if field in observed.raw_invalid_fields and getattr(charge, field) is None:
            audit[text_field] = getattr(observed, text_field)
    return proposed.model_copy(update={"charge": charge, **audit})


def _amount_pairs(
    rows: Sequence[RawChargeEnvelope], indexes: Sequence[int]
) -> list[tuple[Fraction, str, int]]:
    return sorted(
        (Fraction(amount), rows[index].charge.currency or "", index)
        for index in indexes
        if (amount := rows[index].charge.amount) is not None
    )


def _ground_charges(
    proposed: list[RawChargeEnvelope],
    rules: RawExtractionEnvelope,
    dictionary: Sequence[ChargeDictionaryItem],
) -> tuple[list[RawChargeEnvelope], list[ValidationAlert]]:
    def indexes_by_name(rows: list[RawChargeEnvelope]) -> dict[str, list[int]]:
        indexes = defaultdict(list)
        for index, row in enumerate(rows):
            if name := normalize_charge_item(row.charge.raw_charge_name, dictionary)[0]:
                indexes[name].append(index)
        return indexes

    def amounts_text(rows: Sequence[RawChargeEnvelope], indexes: Sequence[int]) -> str:
        return ", ".join(rows[index].raw_amount_text or "缺失" for index in indexes) or "无费用行"

    observed = rules.charges
    grounded = proposed.copy()
    issues = []
    model_groups = indexes_by_name(proposed)
    source_groups = indexes_by_name(observed)
    for name, source_indexes in source_groups.items():
        model_indexes = model_groups.get(name, [])
        source_amounts = _amount_pairs(observed, source_indexes)
        model_amounts = _amount_pairs(proposed, model_indexes)
        paired_source, paired_model = set(), set()
        model_position = 0
        # Match sorted amounts within each fee name before falling back to unpaired rows for audit.
        for amount, _, source_index in source_amounts:
            while model_position < len(model_amounts):
                candidate, _, model_index = model_amounts[model_position]
                if candidate > amount + _LINE_TOLERANCE:
                    break
                model_position += 1
                if candidate >= amount - _LINE_TOLERANCE:
                    grounded[model_index] = _ground_charge(
                        proposed[model_index], observed[source_index]
                    )
                    paired_source.add(source_index)
                    paired_model.add(model_index)
                    break
        remainder_source = [index for index in source_indexes if index not in paired_source]
        remainder_model = [index for index in model_indexes if index not in paired_model]
        for source_index, model_index in zip(remainder_source, remainder_model, strict=False):
            grounded[model_index] = _ground_charge(proposed[model_index], observed[source_index])
        missing_source_audit = any(
            observed[index].raw_invalid_fields or observed[index].raw_parse_issues
            for index in remainder_source[len(remainder_model) :]
        )
        if (
            len(paired_source) != len(source_amounts)
            or (source_amounts and len(model_indexes) > len(source_indexes))
            or missing_source_audit
        ):
            issues.append(
                ValidationAlert.from_code(
                    AlertCode.AMOUNT_TOTAL_MISMATCH,
                    f"模型提取的 {name} 费用行与原文金额或行数不符",
                    field="reported_charge_amount",
                    expected=amounts_text(observed, source_indexes),
                    actual=amounts_text(proposed, model_indexes),
                )
            )
    summary = rules.subtotal_amount if rules.subtotal_amount is not None else rules.total_amount
    additional_fees = sorted(model_groups.keys() - source_groups.keys())
    source_amounts = [row.charge.amount for row in observed]
    if (
        summary is not None
        and source_amounts
        and additional_fees
        and all(amount is not None for amount in source_amounts)
    ):
        observed_sum = sum(Fraction(amount) for amount in source_amounts if amount is not None)
        if abs(observed_sum - Fraction(summary)) <= _LINE_TOLERANCE:
            issues.append(
                ValidationAlert.from_code(
                    AlertCode.AMOUNT_TOTAL_MISMATCH,
                    "原文已识别的费用行与汇总相符，模型另报了无法核对的费用行",
                    field="reported_charge_amount",
                    actual="、".join(additional_fees),
                    expected="、".join(source_groups),
                )
            )
    return grounded, issues


def _ground_parties(
    data: RawDocumentExtraction,
    observed: RawDocumentExtraction,
    doc: ParsedDocument,
    profiles: Sequence[AccountingEntityProfile],
) -> tuple[dict[str, str | None], list[ValidationAlert]]:
    transport_names = [
        label.partition(": ")[2]
        for label in doc.metadata.get("transport_context", ())
        if isinstance(label, str) and label.startswith(("Shipper: ", "Consignee: "))
    ]
    has_payment_direction = bool(observed.payer_hint or observed.payee_hint)
    transport_only = bool(doc.ocr_payload and transport_names) and not any(
        (observed.issuer, observed.recipient, observed.payer_hint, observed.payee_hint)
    )
    updates = {}
    issues = []
    for field, counterpart in (
        ("issuer", "payee_hint"),
        ("recipient", "payer_hint"),
        ("payer_hint", "recipient"),
        ("payee_hint", "issuer"),
    ):
        printed, model = getattr(observed, field), getattr(data, field)
        if printed:
            updates[field] = printed
        if not model or (printed and entity_matches(model, printed, profiles)):
            continue
        alternative = getattr(observed, counterpart)
        matches_transport = any(entity_matches(model, name, profiles) for name in transport_names)
        needs_source_party = (
            has_payment_direction or alternative or transport_only or matches_transport
        )
        contradicts_source = printed or (
            needs_source_party
            and not (alternative and entity_matches(model, alternative, profiles))
        )
        if contradicts_source:
            updates[field] = printed
            issues.append(
                ValidationAlert.from_code(
                    AlertCode.CROSS_DOCUMENT_CONFLICT,
                    "模型给出的结算主体与原文收付证据不符",
                    field=field,
                    expected=printed or alternative,
                    actual=model,
                )
            )
        elif has_payment_direction and field in ("payer_hint", "payee_hint"):
            # A labeled issuer or recipient does not supply the missing leg of the instruction.
            updates[field] = printed
    return updates, issues


def _ground_model_extraction(
    extraction: RawExtractionEnvelope,
    rules: RawExtractionEnvelope,
    doc: ParsedDocument,
    profiles: Sequence[AccountingEntityProfile],
    dictionary: Sequence[ChargeDictionaryItem],
) -> RawExtractionEnvelope:
    """Check model proposals against independent billing evidence from the source."""
    data = extraction.extraction
    updates, issues = _ground_parties(data, rules.extraction, doc, profiles)
    has_source_summary = _has_billing_summary(rules) or rules.tax_amount is not None
    for field, code in (
        ("subtotal_amount", AlertCode.AMOUNT_SUBTOTAL_TAX_MISMATCH),
        ("tax_amount", AlertCode.AMOUNT_SUBTOTAL_TAX_MISMATCH),
        ("total_amount", AlertCode.AMOUNT_TOTAL_MISMATCH),
        ("total_currency", AlertCode.INVALID_OR_MISSING_CURRENCY),
    ):
        observed = getattr(rules.extraction, field)
        proposed = getattr(data, field)
        if observed is None:
            if rules.raw_invalid_fields.get(field) == "$" or (
                has_source_summary and field not in rules.explicit_fields
            ):
                updates[field] = None
            continue
        if proposed is not None and proposed != observed:
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
    for currency, total in rules.totals_by_currency.items():
        proposed = extraction.totals_by_currency.get(currency)
        if proposed is not None and proposed != total:
            issues.append(
                ValidationAlert.from_code(
                    AlertCode.AMOUNT_TOTAL_MISMATCH,
                    "模型报告的分币种总额与原文不符",
                    field=f"reported_totals_by_currency.{currency}",
                    expected=str(total),
                    actual=str(proposed),
                )
            )
    clauses = [
        clause.model_copy(update={"text": sanitize_for_llm(clause.text)})
        for clause in data.excluded_clauses
    ]
    for clause in rules.extraction.excluded_clauses:
        if clause not in clauses:
            clauses.append(clause)
    charges, line_issues = _ground_charges(
        [
            row
            for row in extraction.charges
            if not _non_billing_reason(
                f"{row.charge.raw_charge_name} {row.charge.source_text or ''}"
            )
        ],
        rules,
        dictionary,
    )
    printed_refs, model_refs = rules.extraction.shipment_refs, data.shipment_refs
    printed_mbls = {ref.value for ref in printed_refs.mbl_nos}
    refs = model_refs.model_copy(
        update={
            "mbl_nos": [
                *printed_refs.mbl_nos,
                *(
                    ref.model_copy(update={"source": sanitize_source_path(ref.source)})
                    for ref in model_refs.mbl_nos
                    if ref.value not in printed_mbls
                ),
            ],
            "hbl_no": printed_refs.hbl_no or model_refs.hbl_no,
            "booking_no": printed_refs.booking_no or model_refs.booking_no,
            "container_nos": list(
                dict.fromkeys([*printed_refs.container_nos, *model_refs.container_nos])
            ),
        }
    )
    grounded = data.model_copy(
        update={
            **updates,
            "excluded_clauses": clauses,
            "charges": [row.charge for row in charges],
            "shipment_refs": refs,
        }
    )
    audit = _source_audit(extraction, rules, grounded)
    audit["raw_parse_issues"].extend([*issues, *line_issues])
    if not rules.totals_by_currency:
        audit["explicit_fields"].discard("totals_by_currency")
    return extraction.model_copy(
        update={
            **audit,
            "extraction": grounded,
            "charges": charges,
            "has_explicit_total": rules.has_explicit_total
            or grounded.total_amount is not None
            or "total_amount" in audit["raw_invalid_fields"],
            "totals_by_currency": rules.totals_by_currency,
            # Metadata-only eligibility is decided locally, before making a model call.
            "metadata_only": False,
        }
    )


def _has_source(doc: ParsedDocument) -> bool:
    return bool(doc.raw_text.strip() or doc.tables or doc.ocr_payload)


def _has_billing_summary(envelope: RawExtractionEnvelope | None) -> bool:
    return (
        envelope is not None
        and not envelope.metadata_only
        and (
            envelope.has_explicit_total
            or envelope.total_amount is not None
            or envelope.subtotal_amount is not None
            or bool(envelope.totals_by_currency)
        )
    )


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
        documents = [_redact_document_paths(doc) for doc in state["documents"]]
        by_id = {doc.doc_id: doc for doc in documents}
        paths = {doc_id: _source_path(doc, by_id) for doc_id, doc in by_id.items()}
        package_ids = {doc_id: _package_id(doc, by_id) for doc_id, doc in by_id.items()}
        bundles = []
        failures = {}
        rule_extractions = {}
        for doc in documents:
            envelope = None
            if doc.status is DocumentStatus.FAILED:
                failures[doc.doc_id] = sanitize_for_llm(doc.error_message or "文档解析失败")
            elif doc.status is not DocumentStatus.SKIPPED_NOISE:
                try:
                    envelope = extract_document_by_rules(doc)
                    context = transport_context(doc)
                except Exception as exc:
                    failures[doc.doc_id] = f"文档规则抽取失败（{type(exc).__name__}）"
                    envelope = None
                else:
                    rule_extractions[doc.doc_id] = envelope
                    if context:
                        doc = doc.model_copy(
                            update={"metadata": {**doc.metadata, "transport_context": context}}
                        )
                    if envelope.metadata_only:
                        doc = doc.model_copy(update={"status": DocumentStatus.METADATA_ONLY})
                    groups[package_ids[doc.doc_id]].append((doc, envelope))
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
            "documents": documents,
            "bundles": bundles,
            "rule_extractions": rule_extractions,
            "package_sources": package_sources,
            "package_ids": package_ids,
            "paths": paths,
            "budget": LlmBudgetGuard(self.limits.max_llm_calls_per_batch),
            "limiter": anyio.CapacityLimiter(self.limits.max_concurrency),
            "failures": failures,
            "doc_retry_count": {},
            "correction_logs": {},
            "correction_failures": set(),
        }

    async def _invoke(
        self,
        state: _BillingState,
        messages: list[dict[str, str]],
        rules: RawExtractionEnvelope,
        doc: ParsedDocument,
    ) -> _ModelAttempt:
        client = self.client
        assert client is not None
        budget = state["budget"]
        if not budget.remaining:
            return _ModelAttempt(failure=_BUDGET_EXHAUSTED)
        # Reuse the reservation for the first POST and charge each SDK retry separately.
        async with state["limiter"]:
            if not await budget.try_acquire():
                return _ModelAttempt(failure=_BUDGET_EXHAUSTED)
            try:
                with llm_request_budget(
                    budget.try_acquire_sync, budget.try_acquire, initial_slots=1
                ):
                    if self.use_sync_client:
                        response = await anyio.to_thread.run_sync(
                            client.invoke_structured, messages, RawDocumentExtraction
                        )
                    else:
                        response = await client.ainvoke_structured(messages, RawDocumentExtraction)
                envelope = parse_llm_raw_extraction(response.raw_text, response.parsed)
                envelope = _ground_model_extraction(
                    envelope,
                    rules,
                    doc,
                    state.get("profiles", ()),
                    state.get("charge_dictionary", ()),
                )
            except LlmBudgetExhaustedError:
                return _ModelAttempt(failure=_BUDGET_EXHAUSTED, invoked=True)
            except Exception as exc:
                return _ModelAttempt(
                    failure=f"LLM 调用或解码失败（{type(exc).__name__}）", invoked=True
                )
        if not any(
            normalize_charge_item(row.charge.raw_charge_name)[0] for row in envelope.charges
        ):
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
            return await self._invoke(state, messages, bundle.extraction, doc)

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
        if self.client is None:
            return {"correction_alerts": {}}
        correction_alerts = {}
        failures = state["failures"].copy()
        bundles = state["bundles"].copy()
        for index, bundle in enumerate(bundles):
            doc, envelope = bundle.document, bundle.extraction
            if envelope is None or envelope.metadata_only or not _has_source(doc):
                continue
            try:
                rows = _records_for_bundle(bundle, state)
                alerts = [
                    alert
                    for row in validate_batch(rows, [bundle])
                    for alert in row.validation_alerts
                    if alert.code in _REPAIRABLE_CHECKS
                ]
            except Exception as exc:
                failures[doc.doc_id] = f"文档抽取复检失败（{type(exc).__name__}）"
                bundles[index] = bundle.model_copy(update={"extraction": None})
                continue
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
        return {"correction_alerts": correction_alerts, "failures": failures, "bundles": bundles}

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
            return await self._invoke(state, messages, state["rule_extractions"][doc.doc_id], doc)

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
        independent_bundles = []
        failures = state["failures"].copy()
        for bundle in state["bundles"]:
            doc, envelope = bundle.document, bundle.extraction
            source = state["rule_extractions"].get(doc.doc_id)
            enriched_bundle = bundle
            rows = []
            try:
                if envelope is not None and not envelope.metadata_only and envelope.charges:
                    enriched, field_uses = apply_sibling_context(
                        envelope,
                        state["package_sources"][doc.doc_id],
                        state.get("profiles", ()),
                        source_extraction=source,
                        # Keep attachments' independent references for matching and conflict checks.
                        merge_mbls=doc.source_kind is SourceKind.EML_BODY,
                    )
                    if field_uses:
                        sources = [
                            {
                                "field": use.field,
                                "value": use.value,
                                "sibling_doc_id": use.source.doc_id,
                                "filename": use.filename,
                                "line_index": use.line_index,
                            }
                            for use in field_uses
                        ]
                        metadata = {**doc.metadata, "sibling_field_sources": sources}
                        doc = doc.model_copy(update={"metadata": metadata})
                    enriched_bundle = bundle.model_copy(
                        update={"document": doc, "extraction": enriched}
                    )
                    reasoning = list(state["correction_logs"].get(doc.doc_id, ()))
                    if bundle.retry_count and doc.doc_id not in failures:
                        checks = state["correction_alerts"].get(doc.doc_id)
                        reasoning.append(
                            "第 2 轮复检仍有差异\n" + format_validation_alerts(checks)
                            if checks
                            else "第 2 轮自修复复检通过"
                        )
                    rows = _records_for_bundle(enriched_bundle, state, reasoning)
            except Exception as exc:
                failures[doc.doc_id] = f"文档账单记录组装失败（{type(exc).__name__}）"
                enriched_bundle = bundle
            else:
                independent_bundles.append(bundle)
            if (
                not rows
                and doc.status is DocumentStatus.PARSED
                and (_has_billing_summary(envelope) or _has_billing_summary(source))
            ):
                prior_failure = failures.get(doc.doc_id)
                failures[doc.doc_id] = (
                    f"{prior_failure}。{_NO_CHARGE_DETAILS}"
                    if prior_failure
                    else _NO_CHARGE_DETAILS
                )
            records.extend(rows)
            enriched_bundles.append(enriched_bundle)
        matched = match_and_inherit_batch(
            records, state.get("existing_bills", ()), state.get("profiles", ())
        )
        return {
            "records": validate_batch(matched, independent_bundles),
            "bundles": enriched_bundles,
            "failures": failures,
        }

    def finalize_output(self, state: _BillingState) -> dict:
        by_id = {doc.doc_id: doc for doc in state["documents"]}
        doc_ids_by_hash = defaultdict(list)
        for doc in by_id.values():
            if doc.content_sha256:
                doc_ids_by_hash[doc.content_sha256].append(doc.doc_id)
        records_by_doc = defaultdict(list)
        records_by_package = defaultdict(list)
        for record in state["records"]:
            doc = by_id[record.source_doc_id]
            sources = doc_ids_by_hash.get(doc.content_sha256, (doc.doc_id,))
            for doc_id in sources:
                records_by_doc[doc_id].append(record.id)
            for package in dict.fromkeys(state["package_ids"][doc_id] for doc_id in sources):
                records_by_package[package].append(record.id)
        failures = []
        for doc in state["documents"]:
            if doc.doc_id not in state["failures"]:
                continue
            affected = records_by_doc[doc.doc_id]
            if doc.parent_doc_id and doc.source_kind is not SourceKind.EML_BODY:
                package = state["package_ids"].get(
                    doc.parent_doc_id, state["package_ids"][doc.doc_id]
                )
                affected = list(dict.fromkeys([*affected, *records_by_package[package]]))
            failures.append(
                FailedDocumentAlert(
                    doc_id=doc.doc_id,
                    filename=doc.original_filename,
                    reason=state["failures"][doc.doc_id],
                    affected_record_ids=affected,
                )
            )
        budget = state["budget"]
        exhausted = any(_BUDGET_EXHAUSTED in reason for reason in state["failures"].values()) or (
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
