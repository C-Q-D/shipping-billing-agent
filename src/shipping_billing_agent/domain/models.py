"""Pure billing domain types and exact decimal conversion."""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Annotated, Any, Self

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, model_validator

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


class RawDocumentExtraction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document_type: str | None = None
    reference_no: str | None = None
    document_date: str | None = None
    issuer: str | None = None
    recipient: str | None = None
    shipment_refs: ShipmentRefs = Field(default_factory=ShipmentRefs)
    excluded_clauses: list[ExcludedClause] = Field(default_factory=list)
    subtotal_amount: DecimalValue | None = None
    tax_amount: DecimalValue | None = None
    total_amount: DecimalValue | None = None
    total_currency: str | None = None
    charges: list[RawChargeItem] = Field(default_factory=list)
