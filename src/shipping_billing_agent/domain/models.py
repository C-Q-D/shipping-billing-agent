"""Pure billing domain types and exact decimal conversion."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Annotated, Any, Literal, Never, Self, TypeVar
from urllib.parse import urlsplit, urlunsplit

from pydantic import (
    AliasChoices,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from .currency import normalize_currency

DECIMAL_TOLERANCE = Decimal("0.02")
_GROUPED_DECIMAL = re.compile(r"[+-]?[0-9]{1,3}(?:,[0-9]{3})+(?:\.[0-9]*)?(?:[eE][+-]?[0-9]+)?")
_SHA256_HEX = re.compile(r"[0-9a-f]{64}")


def parse_decimal_strict(value: object) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (Decimal, str, int, float)):
        raise ValueError("金额必须是数字或十进制字符串")

    if isinstance(value, Decimal):
        number = value
    else:
        text = str(value).strip()
        if "," in text:
            if _GROUPED_DECIMAL.fullmatch(text) is None:
                raise ValueError("金额中的逗号必须用于千位分组")
            text = text.replace(",", "")
        try:
            number = Decimal(text)
        except InvalidOperation as exc:
            raise ValueError("无效的十进制数") from exc

    if not number.is_finite():
        raise ValueError("金额必须是有限十进制数")
    return number


def decimal_to_db_text(value: Decimal | None) -> str | None:
    """Format a nullable decimal for SQLite TEXT, retaining its fractional scale."""
    if value is None:
        return None
    number = parse_decimal_strict(value)
    if number.is_zero():
        number = number.copy_abs()
    return format(number, "f")


def _reject_json_constant(token: str) -> Never:
    raise ValueError(f"非有限数值 {token} 不是合法 JSON 数字")


def loads_decimal_json(text: str) -> Any:
    return json.loads(
        text, parse_float=Decimal, parse_int=Decimal, parse_constant=_reject_json_constant
    )


def dumps_decimal_json(obj: Any) -> str:
    """Encode JSON values with finite Decimals as exact, unquoted numbers."""
    if isinstance(obj, Decimal):
        if not obj.is_finite():
            raise ValueError("JSON 数字必须是有限值")
        return format(obj, "f")
    if isinstance(obj, dict):
        members = []
        for key, value in obj.items():
            if not isinstance(key, str):
                raise TypeError("JSON 对象的键必须是字符串")
            members.append(f"{dumps_decimal_json(key)}:{dumps_decimal_json(value)}")
        return "{" + ",".join(members) + "}"
    if isinstance(obj, (list, tuple)):
        return "[" + ",".join(dumps_decimal_json(value) for value in obj) + "]"
    return json.dumps(obj, ensure_ascii=False, allow_nan=False)


def sanitize_source_path(path: str | None) -> str | None:
    if not path or not path.lstrip().lower().startswith(("http://", "https://")):
        return path
    try:
        url = urlsplit(path)
    except ValueError:
        return path.partition("?")[0].partition("#")[0]
    return urlunsplit((url.scheme, url.netloc, url.path, "", ""))


DecimalValue = Annotated[Decimal, BeforeValidator(parse_decimal_strict)]


class OcrMode(StrEnum):
    REMOTE_ONLY = "REMOTE_ONLY"
    REMOTE_WITH_LOCAL_FALLBACK = "REMOTE_WITH_LOCAL_FALLBACK"


class SourceKind(StrEnum):
    EML = "EML"
    EML_BODY = "EML_BODY"
    EML_ATTACHMENT = "EML_ATTACHMENT"
    PDF = "PDF"
    IMAGE = "IMAGE"
    EXCEL = "EXCEL"
    WORD = "WORD"
    URL = "URL"


class ExtractionChannel(StrEnum):
    PLTPLT_OCR = "PLTPLT_OCR"
    LOCAL_PDF_TEXT_FALLBACK = "LOCAL_PDF_TEXT_FALLBACK"
    LOCAL_RAPIDOCR_FALLBACK = "LOCAL_RAPIDOCR_FALLBACK"
    EXCEL_PARSER = "EXCEL_PARSER"
    WORD_PARSER = "WORD_PARSER"
    EML_TEXT_PARSER = "EML_TEXT_PARSER"
    SKIPPED_SIGNATURE = "SKIPPED_SIGNATURE"
    FAILED = "FAILED"


class DocumentStatus(StrEnum):
    PARSED = "PARSED"
    SKIPPED_NOISE = "SKIPPED_NOISE"
    METADATA_ONLY = "METADATA_ONLY"
    FAILED = "FAILED"


class OperationType(StrEnum):
    CREATE = "CREATE"
    UPDATE = "UPDATE"
    NO_CHANGE = "NO_CHANGE"
    PENDING_REVIEW = "PENDING_REVIEW"


class ReviewStatus(StrEnum):
    AUTO_READY = "AUTO_READY"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class MatchLevel(StrEnum):
    NONE = "NONE"
    L1_DOC_CHARGE = "L1_DOC_CHARGE"
    L2_BL_CHARGE_CURR = "L2_BL_CHARGE_CURR"
    L3_BOOKING_CHARGE_CURR = "L3_BOOKING_CHARGE_CURR"
    L4_CNTR_PARTY_DATE_AMT = "L4_CNTR_PARTY_DATE_AMT"
    AMBIGUOUS_MULTI = "AMBIGUOUS_MULTI"


class PaymentTerm(StrEnum):
    PP = "PP"
    CC = "CC"


class PostingType(StrEnum):
    AR = "AR"
    AP = "AP"


class AlertSeverity(StrEnum):
    WARNING = "WARNING"
    BLOCKER = "BLOCKER"
    RESOLVED = "RESOLVED"


class AlertCode(StrEnum):
    LINE_ARITHMETIC_MISMATCH = "LINE_ARITHMETIC_MISMATCH"
    NEGATIVE_NON_CREDIT_AMOUNT = "NEGATIVE_NON_CREDIT_AMOUNT"
    INVALID_OR_MISSING_CURRENCY = "INVALID_OR_MISSING_CURRENCY"
    INVALID_DOCUMENT_DATE = "INVALID_DOCUMENT_DATE"
    ENTITY_NOT_MATCHED = "ENTITY_NOT_MATCHED"
    AMOUNT_SUBTOTAL_TAX_MISMATCH = "AMOUNT_SUBTOTAL_TAX_MISMATCH"
    AMOUNT_TOTAL_MISMATCH = "AMOUNT_TOTAL_MISMATCH"
    MULTI_CURRENCY_TOTAL_AMBIGUITY = "MULTI_CURRENCY_TOTAL_AMBIGUITY"
    OCR_NUMERIC_CONFUSION = "OCR_NUMERIC_CONFUSION"
    AMBIGUOUS_EXISTING_BILL = "AMBIGUOUS_EXISTING_BILL"
    DUPLICATE_LINE_EXTRACTED = "DUPLICATE_LINE_EXTRACTED"
    CROSS_DOCUMENT_CONFLICT = "CROSS_DOCUMENT_CONFLICT"
    LLM_EXTRACTION_FAILED = "LLM_EXTRACTION_FAILED"
    CONTAINER_ISO6346_MISMATCH = "CONTAINER_ISO6346_MISMATCH"
    OCR_SUSPICIOUS_FLAG = "OCR_SUSPICIOUS_FLAG"
    MULTI_SOURCE_BL_MISMATCH = "MULTI_SOURCE_BL_MISMATCH"
    CONTEXT_INFERRED_FIELD = "CONTEXT_INFERRED_FIELD"


ALERT_SEVERITY_MAP: dict[AlertCode, AlertSeverity] = {
    AlertCode.LINE_ARITHMETIC_MISMATCH: AlertSeverity.BLOCKER,
    AlertCode.NEGATIVE_NON_CREDIT_AMOUNT: AlertSeverity.BLOCKER,
    AlertCode.INVALID_OR_MISSING_CURRENCY: AlertSeverity.BLOCKER,
    AlertCode.INVALID_DOCUMENT_DATE: AlertSeverity.BLOCKER,
    AlertCode.ENTITY_NOT_MATCHED: AlertSeverity.BLOCKER,
    AlertCode.AMOUNT_SUBTOTAL_TAX_MISMATCH: AlertSeverity.BLOCKER,
    AlertCode.AMOUNT_TOTAL_MISMATCH: AlertSeverity.BLOCKER,
    AlertCode.MULTI_CURRENCY_TOTAL_AMBIGUITY: AlertSeverity.BLOCKER,
    AlertCode.OCR_NUMERIC_CONFUSION: AlertSeverity.BLOCKER,
    AlertCode.AMBIGUOUS_EXISTING_BILL: AlertSeverity.BLOCKER,
    AlertCode.DUPLICATE_LINE_EXTRACTED: AlertSeverity.BLOCKER,
    AlertCode.CROSS_DOCUMENT_CONFLICT: AlertSeverity.BLOCKER,
    AlertCode.LLM_EXTRACTION_FAILED: AlertSeverity.BLOCKER,
    AlertCode.CONTAINER_ISO6346_MISMATCH: AlertSeverity.WARNING,
    AlertCode.OCR_SUSPICIOUS_FLAG: AlertSeverity.WARNING,
    AlertCode.MULTI_SOURCE_BL_MISMATCH: AlertSeverity.WARNING,
    AlertCode.CONTEXT_INFERRED_FIELD: AlertSeverity.WARNING,
}


class BatchStatus(StrEnum):
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    PARTIAL_ERROR = "PARTIAL_ERROR"


class BatchStage(StrEnum):
    QUEUED = "QUEUED"
    UNPACKING = "UNPACKING"
    EXTRACTING = "EXTRACTING"
    MATCHING = "MATCHING"
    VALIDATING = "VALIDATING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class InputValidationIssue(BaseModel):
    code: str
    filename_or_url: str
    message: str


class SourceRefValue(BaseModel):
    value: str
    source: str


class ShipmentRefs(BaseModel):
    mbl_nos: list[SourceRefValue] = Field(default_factory=list)
    hbl_no: str | None = None
    booking_no: str | None = None
    container_nos: list[str] = Field(default_factory=list)

    @field_validator("container_nos", mode="before")
    @classmethod
    def _unpack_container_nos(cls, value: Any) -> Any:
        if not isinstance(value, list):
            return value
        return [
            item.value
            if isinstance(item, SourceRefValue)
            else (
                item["value"]
                if isinstance(item, Mapping) and isinstance(item.get("value"), str)
                else item
            )
            for item in value
        ]


class PackageShipmentRefs(BaseModel):
    source_doc_id: str
    parent_doc_id: str | None = None
    canonical_doc_id: str | None = None
    # The mail body can contribute HBL evidence, but only attachments contribute MBLs.
    mbl_nos: list[SourceRefValue] | None = None
    hbl_nos: list[str] = Field(default_factory=list)


class ExcludedClause(BaseModel):
    text: str
    reason: str
    page: int | None = None


class ParsedTable(BaseModel):
    rows: list[list[str]]
    source: str | None = None


class OcrListItem(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="allow")

    file_type: str | None = Field(default=None, alias="fileType")
    invoice_number: str | None = Field(default=None, alias="invoiceNumber")
    mbl_number: str | None = Field(default=None, alias="mblNumber")
    shipper: str | None = None
    consignee: str | None = None
    ctr_list: list[str | dict[str, Any]] = Field(default_factory=list, alias="ctrList")
    charge_list: list[dict[str, Any]] = Field(default_factory=list, alias="chargeList")
    other: Any = None


class OcrStructuredPayload(BaseModel):
    items: list[OcrListItem]
    has_charges: bool
    has_metadata: bool


class ParsedDocument(BaseModel):
    doc_id: str
    parent_doc_id: str | None = None
    original_filename: str
    sanitized_filename: str
    media_type: str | None = None
    source_kind: SourceKind
    content_sha256: str | None = None
    public_url: str | None = None
    extraction_channel: ExtractionChannel
    status: DocumentStatus
    error_message: str | None = None
    raw_text: str = ""
    tables: list[ParsedTable] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    ocr_payload: OcrStructuredPayload | None = None
    raw_bytes: bytes | None = Field(default=None, exclude=True, repr=False)

    @model_validator(mode="after")
    def validate_content_sha256(self) -> Self:
        if self.source_kind is SourceKind.URL:
            if self.content_sha256 is not None:
                raise ValueError("公网 URL 来源的 SHA-256 必须为空")
        elif _SHA256_HEX.fullmatch(self.content_sha256 or "") is None:
            raise ValueError("本地文档的 SHA-256 必须是 64 位小写十六进制")
        return self


class RawChargeItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    raw_charge_name: str
    quantity: DecimalValue | None = None
    unit_price: DecimalValue | None = None
    amount: DecimalValue | None = None
    currency: str | None = None
    payment_term: PaymentTerm | None = None
    source_text: str | None = None
    page: int | None = None
    line_number: int | None = None
    debit_credit: Literal["DEBIT", "CREDIT", "UNKNOWN"] = "UNKNOWN"


class RawDocumentExtraction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document_type: str | None = None
    reference_no: str | None = None
    document_date: str | None = None
    issuer: str | None = None
    recipient: str | None = None
    payer_hint: str | None = None
    payee_hint: str | None = None
    shipment_refs: ShipmentRefs = Field(default_factory=ShipmentRefs)
    excluded_clauses: list[ExcludedClause] = Field(default_factory=list)
    subtotal_amount: DecimalValue | None = None
    tax_amount: DecimalValue | None = None
    total_amount: DecimalValue | None = None
    total_currency: str | None = None
    charges: list[RawChargeItem] = Field(default_factory=list)


class AccountingEntityProfile(BaseModel):
    canonical_name: str = Field(min_length=1, max_length=120)
    aliases: list[str] = Field(default_factory=list)
    is_default: bool = False


class ChargeDictionaryItem(BaseModel):
    raw_alias: str = Field(min_length=1)
    standard_name: str = Field(min_length=1)
    category: str = "general"
    is_builtin: bool = False


class ExistingBillRecord(BaseModel):
    id: str
    accounting_entity: str
    charge_item: str
    amount: DecimalValue
    currency: str
    posting_type: PostingType
    external_bill_key: str | None = None
    document_type: str | None = None
    reference_no: str | None = None
    document_date: str | None = None
    issuer: str | None = None
    recipient: str | None = None
    mbl_no: str | None = None
    hbl_no: str | None = None
    booking_no: str | None = None
    container_nos: list[str] = Field(default_factory=list)
    raw_charge_name: str | None = None
    quantity: DecimalValue | None = None
    unit_price: DecimalValue | None = None
    payment_term: PaymentTerm | None = None
    extra_fields: dict[str, Any] = Field(default_factory=dict)
    version: int = Field(default=1, ge=1)
    created_at: str | None = None
    updated_at: str | None = None


class ValidationAlert(BaseModel):
    code: AlertCode
    severity: AlertSeverity
    message: str
    field: str | None = None
    expected: str | None = None
    actual: str | None = None

    @classmethod
    def from_code(
        cls,
        code: AlertCode,
        message: str,
        *,
        field: str | None = None,
        expected: str | None = None,
        actual: str | None = None,
    ) -> Self:
        return cls(
            code=code,
            severity=ALERT_SEVERITY_MAP[code],
            message=message,
            field=field,
            expected=expected,
            actual=actual,
        )

    @model_validator(mode="after")
    def validate_severity(self) -> Self:
        default = ALERT_SEVERITY_MAP[self.code]
        if self.severity is not default and not (
            default is AlertSeverity.WARNING and self.severity is AlertSeverity.RESOLVED
        ):
            raise ValueError("告警严重度必须与错误码的固定映射一致")
        return self


class FieldDiff(BaseModel):
    field: str
    old_value: Any = None
    new_value: Any = None
    changed: bool


class CandidateBillSummary(BaseModel):
    id: str
    label: str
    charge_item: str
    amount: DecimalValue
    currency: str
    reference_no: str | None = None
    document_date: str | None = None
    match_level: MatchLevel = MatchLevel.NONE


class SourceEvidence(BaseModel):
    source_file: str | None = None
    source_doc_id: str | None = None
    source_paths: list[str] = Field(default_factory=list)
    page: int | None = None
    sheet: str | None = None
    row: int | None = None
    line_number: int | None = None
    extraction_channel: ExtractionChannel | None = None
    snippet: str | None = None

    def with_sanitized_paths(self) -> SourceEvidence:
        return self.model_copy(
            update={
                "source_file": sanitize_source_path(self.source_file),
                "source_paths": list(
                    dict.fromkeys(sanitize_source_path(path) for path in self.source_paths)
                ),
            }
        )


class HumanReviewAction(BaseModel):
    action: str
    operator: str
    reason: str | None = None
    timestamp: str
    overrides: dict[str, Any] = Field(default_factory=dict)


class FailedDocumentAlert(BaseModel):
    doc_id: str
    filename: str
    error_message: str = Field(validation_alias=AliasChoices("error_message", "reason"))
    affected_record_ids: list[str] = Field(default_factory=list)

    @property
    def reason(self) -> str:
        return self.error_message


class _RawFieldAudit(BaseModel):
    explicit_fields: set[str] = Field(default_factory=set)
    raw_invalid_fields: dict[str, str] = Field(default_factory=dict)
    raw_parse_issues: list[ValidationAlert] = Field(default_factory=list)

    def _reject_field(self, field: str, value: Any, code: AlertCode, message: str) -> None:
        original = _raw_value_text(value)
        if original is not None:
            self.raw_invalid_fields[field] = original
        self.raw_parse_issues.append(
            ValidationAlert.from_code(code, message, field=field, actual=original)
        )


class RawChargeEnvelope(_RawFieldAudit):
    charge: RawChargeItem
    raw_amount_text: str | None = None
    raw_unit_price_text: str | None = None
    raw_quantity_text: str | None = None


class RawExtractionEnvelope(_RawFieldAudit):
    extraction: RawDocumentExtraction
    charges: list[RawChargeEnvelope] = Field(default_factory=list)
    subtotals_by_currency: dict[str, DecimalValue] = Field(default_factory=dict)
    taxes_by_currency: dict[str, DecimalValue] = Field(default_factory=dict)
    totals_by_currency: dict[str, DecimalValue] = Field(default_factory=dict)
    has_explicit_total: bool = False
    metadata_only: bool = False

    @property
    def subtotal_amount(self) -> Decimal | None:
        return self.extraction.subtotal_amount

    @property
    def tax_amount(self) -> Decimal | None:
        return self.extraction.tax_amount

    @property
    def total_amount(self) -> Decimal | None:
        return self.extraction.total_amount

    @property
    def total_currency(self) -> str | None:
        return self.extraction.total_currency


class SourceDocTotalsSnapshot(BaseModel):
    source_doc_id: str
    subtotal_amount: DecimalValue | None = None
    tax_amount: DecimalValue | None = None
    total_amount: DecimalValue | None = None
    total_currency: str | None = None
    subtotals_by_currency: dict[str, DecimalValue] = Field(default_factory=dict)
    taxes_by_currency: dict[str, DecimalValue] = Field(default_factory=dict)
    totals_by_currency: dict[str, DecimalValue] = Field(default_factory=dict)
    has_explicit_total: bool = False

    @classmethod
    def from_extraction(
        cls, source_doc_id: str, extraction: RawExtractionEnvelope
    ) -> SourceDocTotalsSnapshot:
        return cls(
            source_doc_id=source_doc_id,
            subtotal_amount=extraction.subtotal_amount,
            tax_amount=extraction.tax_amount,
            total_amount=extraction.total_amount,
            total_currency=extraction.total_currency,
            subtotals_by_currency=extraction.subtotals_by_currency,
            taxes_by_currency=extraction.taxes_by_currency,
            totals_by_currency=extraction.totals_by_currency,
            has_explicit_total=(
                extraction.has_explicit_total
                or extraction.total_amount is not None
                or bool(extraction.totals_by_currency)
            ),
        )


class ProcessedBillingRecord(_RawFieldAudit):
    id: str
    batch_id: str | None = None
    source_doc_id: str
    source_line_index: int = Field(default=0, ge=0)
    operation: OperationType = OperationType.PENDING_REVIEW
    review_status: ReviewStatus = ReviewStatus.NEEDS_REVIEW
    requires_reapproval: bool = False
    match_level: MatchLevel = MatchLevel.NONE
    matched_existing_bill_id: str | None = None
    matched_bill_ref: str | None = None
    candidate_bills: list[CandidateBillSummary] = Field(default_factory=list)
    document_type: str | None = None
    reference_no: str | None = None
    document_date: str | None = None
    issuer: str | None = None
    recipient: str | None = None
    payer_hint: str | None = None
    payee_hint: str | None = None
    charge_item: str | None = None
    raw_charge_name: str | None = None
    matched_from_dictionary: bool = False
    quantity: DecimalValue | None = None
    unit_price: DecimalValue | None = None
    amount: DecimalValue | None = None
    currency: str | None = None
    payment_term: PaymentTerm | None = None
    accounting_entity: str
    posting_type: PostingType | None = None
    debit_credit: Literal["DEBIT", "CREDIT", "UNKNOWN"] = "UNKNOWN"
    is_credit: bool = False
    raw_explicit_amount: DecimalValue | None = None
    raw_amount_abs: DecimalValue | None = None
    raw_amount_text: str | None = None
    raw_unit_price_text: str | None = None
    raw_quantity_text: str | None = None
    shipment_refs: ShipmentRefs = Field(default_factory=ShipmentRefs)
    package_sibling_refs: list[PackageShipmentRefs] = Field(default_factory=list)
    source_evidence: SourceEvidence = Field(default_factory=SourceEvidence)
    source_doc_totals: SourceDocTotalsSnapshot | None = None
    field_diffs: list[FieldDiff] = Field(default_factory=list)
    validation_alerts: list[ValidationAlert] = Field(default_factory=list)
    ai_reasoning: list[str] = Field(default_factory=list)
    excluded_clauses: list[ExcludedClause] = Field(default_factory=list)
    # Values before matching, keyed by the billing fields that inheritance changed.
    inherited_fields: dict[str, Any] = Field(default_factory=dict)
    source_values_before_overrides: dict[str, Any] = Field(default_factory=dict)
    human_overrides: dict[str, Any] = Field(default_factory=dict)
    human_review_history: list[HumanReviewAction] = Field(default_factory=list)
    reject_reason: str | None = None
    confidence: float | None = Field(default=None, ge=0, le=1)
    retry_count: int = Field(default=0, ge=0, le=1)
    created_at: str | None = None
    updated_at: str | None = None


class DocumentExtractionBundle(BaseModel):
    document: ParsedDocument
    extraction: RawExtractionEnvelope | None = None
    retry_count: int = Field(default=0, ge=0, le=1)


class BatchPipelineResult(BaseModel):
    accounting_entity: str
    documents: list[DocumentExtractionBundle] = Field(default_factory=list)
    records: list[ProcessedBillingRecord] = Field(default_factory=list)
    failed_document_alerts: list[FailedDocumentAlert] = Field(default_factory=list)
    failed_doc_ids: list[str] = Field(default_factory=list)
    llm_calls_used: int = Field(default=0, ge=0)
    warnings: list[str] = Field(default_factory=list)


def _raw_value_text(value: Any) -> str | None:
    if value is None or isinstance(value, str):
        return value
    if isinstance(value, Decimal):
        return str(value)
    return dumps_decimal_json(value)


_RawModel = TypeVar("_RawModel", RawChargeItem, RawDocumentExtraction)


def _validate_raw_model(  # noqa: UP047 - Python 3.11 is supported.
    model: type[_RawModel], values: dict[str, Any], audit: _RawFieldAudit
) -> _RawModel:
    try:
        return model.model_validate(values)
    except ValidationError as exc:
        invalid_fields = dict.fromkeys(error["loc"][0] for error in exc.errors())

    for field in invalid_fields:
        audit._reject_field(
            field, values.pop(field, None), AlertCode.LLM_EXTRACTION_FAILED, "抽取字段格式无效"
        )
    if model is RawChargeItem and "raw_charge_name" in invalid_fields:
        values["raw_charge_name"] = ""
    return model.model_validate(values)


def _read_decimal(value: Any, field: str, audit: _RawFieldAudit) -> Decimal | None:
    if value is None:
        return None
    try:
        return parse_decimal_strict(value)
    except ValueError as exc:
        audit._reject_field(field, value, AlertCode.OCR_NUMERIC_CONFUSION, str(exc))
        return None


def _read_currency(
    value: Any, field: str, audit: _RawFieldAudit, context_currencies: set[str]
) -> str | None:
    if value is None:
        return None
    currency, reason = normalize_currency(
        value if isinstance(value, str) else None, context_currencies=context_currencies
    )
    if currency is None:
        audit._reject_field(
            field, value, AlertCode.INVALID_OR_MISSING_CURRENCY, reason or "币种无效，待复核"
        )
    elif reason:
        audit.raw_parse_issues.append(
            ValidationAlert.from_code(
                AlertCode.CONTEXT_INFERRED_FIELD, reason, field=field, actual=value
            )
        )
    return currency


def _read_bool(value: Any, field: str, audit: _RawFieldAudit) -> bool:
    if isinstance(value, bool):
        return value
    audit._reject_field(field, value, AlertCode.LLM_EXTRACTION_FAILED, "标记必须是布尔值")
    return False


def _parse_charge_row(row: dict[str, Any], context_currencies: set[str]) -> RawChargeEnvelope:
    from .charge_normalizer import normalize_payment_term

    values = {key: value for key, value in row.items() if value is not None}
    audit = _RawFieldAudit(explicit_fields=set(values))
    raw_numbers = {}
    for field in ("amount", "unit_price", "quantity"):
        text_field = f"raw_{field}_text"
        original = values.pop(text_field, None)
        raw_numbers[text_field] = _raw_value_text(
            original if original is not None else values.get(field)
        )
        if field in values:
            number = _read_decimal(values[field], field, audit)
            if number is not None and original is not None:
                if _read_decimal(original, field, audit) is None:
                    number = None
            values[field] = number
    if "currency" in values:
        values["currency"] = _read_currency(
            values["currency"], "currency", audit, context_currencies
        )
    if "payment_term" in values:
        original_term = values["payment_term"]
        term = normalize_payment_term(original_term) if isinstance(original_term, str) else None
        values["payment_term"] = term
        if term is None:
            audit._reject_field(
                "payment_term",
                original_term,
                AlertCode.LLM_EXTRACTION_FAILED,
                "付款方式无法明确识别",
            )
    charge = _validate_raw_model(RawChargeItem, values, audit)
    return RawChargeEnvelope(charge=charge, **raw_numbers, **audit.model_dump())


def _parse_amounts_by_currency(
    raw_amounts: Any, field: str, audit: _RawFieldAudit, context_currencies: set[str]
) -> dict[str, Decimal]:
    if not isinstance(raw_amounts, dict):
        audit._reject_field(
            field,
            raw_amounts,
            AlertCode.LLM_EXTRACTION_FAILED,
            "分币种汇总金额必须是对象",
        )
        return {}

    amounts: dict[str, Decimal] = {}
    for token, value in raw_amounts.items():
        if value is None:
            continue
        item_field = f"{field}.{token}"
        currency = _read_currency(token, item_field, audit, context_currencies)
        if currency is None:
            continue
        amount = _read_decimal(value, item_field, audit)
        if amount is None:
            continue
        if currency in amounts and amounts[currency] != amount:
            audit._reject_field(
                item_field,
                value,
                AlertCode.MULTI_CURRENCY_TOTAL_AMBIGUITY,
                "同一币种的汇总金额存在冲突",
            )
        else:
            amounts[currency] = amount
    return amounts


def _failed_extraction(message: str) -> RawExtractionEnvelope:
    return RawExtractionEnvelope(
        extraction=RawDocumentExtraction(),
        raw_parse_issues=[ValidationAlert.from_code(AlertCode.LLM_EXTRACTION_FAILED, message)],
    )


def parse_llm_raw_extraction(
    raw_text: str | None, parsed: RawDocumentExtraction | None = None
) -> RawExtractionEnvelope:
    """Prefer the original JSON so explicit invalid values and Decimal scale survive."""
    from .charge_normalizer import normalize_document_date

    if raw_text and raw_text.strip():
        try:
            payload = loads_decimal_json(raw_text)
        except (ValueError, InvalidOperation, RecursionError):
            return _failed_extraction("抽取结果不是有效的 JSON")
    elif parsed is not None:
        payload = parsed.model_dump(exclude_unset=True, exclude_none=True)
    else:
        return _failed_extraction("抽取结果为空")
    if not isinstance(payload, dict):
        return _failed_extraction("抽取结果必须是 JSON 对象")

    values = {key: value for key, value in payload.items() if value is not None}
    audit = _RawFieldAudit(explicit_fields=set(values))
    raw_rows = values.pop("charges", [])
    currency_summaries = {
        field: values.pop(field, {})
        for field in ("subtotals_by_currency", "taxes_by_currency", "totals_by_currency")
    }
    raw_totals = currency_summaries["totals_by_currency"]
    metadata_only = _read_bool(values.pop("metadata_only", False), "metadata_only", audit)
    explicit_total_flag = _read_bool(
        values.pop("has_explicit_total", False), "has_explicit_total", audit
    )
    has_explicit_total = (
        explicit_total_flag
        or "total_amount" in values
        or (
            isinstance(raw_totals, dict) and any(value is not None for value in raw_totals.values())
        )
    )

    if not isinstance(raw_rows, list):
        audit._reject_field(
            "charges", raw_rows, AlertCode.LLM_EXTRACTION_FAILED, "费用明细必须是数组"
        )
        raw_rows = []
    context_currencies = {
        token
        for token in (
            values.get("total_currency"),
            *(row.get("currency") for row in raw_rows if isinstance(row, dict)),
        )
        if isinstance(token, str)
    }
    for amounts in currency_summaries.values():
        if isinstance(amounts, dict):
            context_currencies.update(
                token for token, value in amounts.items() if value is not None
            )

    charges = []
    for index, row in enumerate(raw_rows):
        if isinstance(row, dict):
            charges.append(_parse_charge_row(row, context_currencies))
        else:
            audit._reject_field(
                f"charges.{index}", row, AlertCode.LLM_EXTRACTION_FAILED, "费用行必须是对象"
            )
    values["charges"] = [row.charge for row in charges]

    if "document_date" in values:
        original_date = values["document_date"]
        date, reason = normalize_document_date(_raw_value_text(original_date))
        values["document_date"] = date
        if reason:
            audit._reject_field(
                "document_date", original_date, AlertCode.INVALID_DOCUMENT_DATE, reason
            )
    for field in ("subtotal_amount", "tax_amount", "total_amount"):
        if field in values:
            values[field] = _read_decimal(values[field], field, audit)
    if "total_currency" in values:
        values["total_currency"] = _read_currency(
            values["total_currency"], "total_currency", audit, context_currencies
        )

    summaries = {
        field: _parse_amounts_by_currency(amounts, field, audit, context_currencies)
        for field, amounts in currency_summaries.items()
    }
    extraction = _validate_raw_model(RawDocumentExtraction, values, audit)
    return RawExtractionEnvelope(
        extraction=extraction,
        charges=charges,
        has_explicit_total=has_explicit_total,
        metadata_only=metadata_only,
        **summaries,
        **audit.model_dump(),
    )
