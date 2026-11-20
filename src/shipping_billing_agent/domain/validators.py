"""Validate billing lines and reconcile evidence within a batch."""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Callable, Sequence
from decimal import MAX_EMAX, MIN_EMIN, Decimal, localcontext
from fractions import Fraction
from itertools import combinations, product
from string import ascii_uppercase
from typing import Any
from unicodedata import normalize

from .charge_normalizer import normalize_charge_item, normalize_document_date
from .currency import is_valid_iso4217
from .entity_resolver import (
    PRINTED_CURRENCY_CONFLICT_MESSAGE,
    _is_currency_conflict,
    apply_human_overrides,
    has_explicit_parties,
    is_credit_document,
    source_format_alerts,
)
from .models import (
    ALERT_SEVERITY_MAP,
    DECIMAL_TOLERANCE,
    AlertCode,
    AlertSeverity,
    DocumentExtractionBundle,
    OcrStructuredPayload,
    OperationType,
    PackageShipmentRefs,
    PaymentTerm,
    ProcessedBillingRecord,
    RawExtractionEnvelope,
    ReviewStatus,
    ShipmentRefs,
    SourceDocTotalsSnapshot,
    SourceKind,
    SourceRefValue,
    ValidationAlert,
    dumps_decimal_json,
    sanitize_source_path,
)

_ZERO = Decimal(0)
_NUMERIC_FIELDS = ("amount", "quantity", "unit_price")
_LINE_ALERT_CODES = {
    AlertCode.LINE_ARITHMETIC_MISMATCH,
    AlertCode.NEGATIVE_NON_CREDIT_AMOUNT,
    AlertCode.INVALID_OR_MISSING_CURRENCY,
    AlertCode.INVALID_DOCUMENT_DATE,
    AlertCode.ENTITY_NOT_MATCHED,
    AlertCode.OCR_NUMERIC_CONFUSION,
    AlertCode.CONTAINER_ISO6346_MISMATCH,
}
_TOTAL_ALERT_CODES = {
    AlertCode.AMOUNT_SUBTOTAL_TAX_MISMATCH,
    AlertCode.AMOUNT_TOTAL_MISMATCH,
    AlertCode.MULTI_CURRENCY_TOTAL_AMBIGUITY,
}
_CONTEXT_ALERT_CODES = {
    *_TOTAL_ALERT_CODES,
    AlertCode.DUPLICATE_LINE_EXTRACTED,
    AlertCode.CROSS_DOCUMENT_CONFLICT,
    AlertCode.OCR_SUSPICIOUS_FLAG,
    AlertCode.MULTI_SOURCE_BL_MISMATCH,
}
_NUMERIC_TOKEN = re.compile(r"(?<!\w)[+-]?[0-9OILSoils.,]+(?:[eE][+-]?[0-9OILSoils]+)?(?!\w)")
_ISO_PREFIX = re.compile(r"[A-Z]{4}[0-9]{6}")
_LETTER_VALUES = dict(zip(ascii_uppercase, (n for n in range(10, 39) if n % 11), strict=True))
_OCR_LETTERS = {"0": "O", "1": "IL", "2": "Z", "5": "S", "8": "B"}
_OCR_DIGITS = {letter: digit for digit, letters in _OCR_LETTERS.items() for letter in letters}
_NO_SUMMARY_REASON = "单据无汇总行，免汇总对账"
_SHARED_HBL_MESSAGE = "同一费用单据在不同邮件包中对应不同的分提单号，待复核"


def compute_iso6346_check_digit(prefix10: str) -> int:
    """Compute the check digit for four ASCII letters followed by six digits."""
    prefix = prefix10.upper() if prefix10.isascii() else prefix10
    if not _ISO_PREFIX.fullmatch(prefix):
        raise ValueError("箱号的前十位必须为四个 ASCII 字母及六个数字")
    values = [*(_LETTER_VALUES[letter] for letter in prefix[:4]), *map(int, prefix[4:])]
    return sum(value << position for position, value in enumerate(values)) % 11 % 10


def validate_container_number(container_no: str) -> tuple[bool, int | None, str | None]:
    """Return validity, the check digit for a readable prefix, and a unique OCR repair."""
    token = "".join(container_no.split())
    if len(token) != 11 or not token.isascii():
        return False, None, None

    token = token.upper()
    expected = (
        compute_iso6346_check_digit(token[:10]) if _ISO_PREFIX.fullmatch(token[:10]) else None
    )
    if expected is not None and token[-1] == str(expected):
        return True, expected, None

    letters = [char if char in _LETTER_VALUES else _OCR_LETTERS.get(char, "") for char in token[:4]]
    digits = [char if char.isdigit() else _OCR_DIGITS.get(char, "") for char in token[4:]]
    suggestion = None
    for chars in product(*letters, *digits):
        candidate = "".join(chars)
        if compute_iso6346_check_digit(candidate[:10]) == int(candidate[-1]):
            if suggestion is not None:
                return False, expected, None
            suggestion = candidate
    return False, expected, suggestion


def _decimal_sum(values: Sequence[Decimal]) -> Decimal:
    terms = [value for value in values if value]
    if len(terms) < 2:
        return terms[0] if terms else _ZERO

    # Align the least significant place and budget for carries without rounding cents.
    highest = max(value.adjusted() for value in terms)
    lowest = min(value.adjusted() - len(value.as_tuple().digits) + 1 for value in terms)
    precision = highest - lowest + 1 + len(str(len(terms)))
    with localcontext(prec=precision, Emax=MAX_EMAX, Emin=MIN_EMIN):
        return sum(terms[1:], terms[0])


def _amounts_differ(left: Decimal, right: Decimal) -> bool:
    return _decimal_sum((left, right.copy_negate())).copy_abs() > DECIMAL_TOLERANCE


def _with_alerts(
    record: ProcessedBillingRecord,
    additions: Sequence[ValidationAlert],
    recalculated: set[AlertCode],
) -> ProcessedBillingRecord:
    prior = [*record.validation_alerts, *additions]
    resolved = {
        (alert.code, alert.field, alert.actual): alert
        for alert in prior
        if alert.severity is AlertSeverity.RESOLVED
        and ALERT_SEVERITY_MAP[alert.code] is AlertSeverity.WARNING
        and alert.field
        and alert.field.strip()
        and alert.actual
        and alert.actual.strip()
    }
    upstream = [
        alert
        for alert in record.validation_alerts
        if alert.code not in recalculated
        and alert not in record.raw_parse_issues
        and not (alert.code is AlertCode.LLM_EXTRACTION_FAILED and alert.field is not None)
    ]
    alerts = {}
    for alert in (*upstream, *additions):
        if alert.severity is AlertSeverity.RESOLVED:
            severity = ALERT_SEVERITY_MAP[alert.code]
            if severity is AlertSeverity.WARNING and alert.code not in _CONTEXT_ALERT_CODES:
                continue
            alert = alert.model_copy(update={"severity": severity})
        key = (alert.code, alert.field, alert.actual)
        current = resolved.get(key, alert)
        alerts.setdefault((*key, current.expected), current)

    operation, status = record.operation, record.review_status
    decided = status in (ReviewStatus.APPROVED, ReviewStatus.REJECTED)
    requires_reapproval = record.requires_reapproval and not decided
    if any(
        alert.severity in (AlertSeverity.BLOCKER, AlertSeverity.WARNING)
        for alert in alerts.values()
    ):
        operation, status = OperationType.PENDING_REVIEW, ReviewStatus.NEEDS_REVIEW
    else:
        if operation is OperationType.PENDING_REVIEW:
            if record.matched_existing_bill_id:
                operation = OperationType.UPDATE if record.field_diffs else OperationType.NO_CHANGE
            else:
                operation = OperationType.CREATE
        if not decided:
            status = ReviewStatus.NEEDS_REVIEW if requires_reapproval else ReviewStatus.AUTO_READY
    return record.model_copy(
        update={
            "validation_alerts": list(alerts.values()),
            "operation": operation,
            "review_status": status,
            "requires_reapproval": requires_reapproval,
        }
    )


def _is_finite_number(value: object) -> bool:
    return isinstance(value, Decimal) and value.is_finite()


def _line_arithmetic_alert(
    record: ProcessedBillingRecord, invalid: set[str]
) -> ValidationAlert | None:
    quantity, unit_price, target = record.quantity, record.unit_price, record.amount
    if invalid.intersection(_NUMERIC_FIELDS) or not all(
        _is_finite_number(value) for value in (quantity, unit_price, target)
    ):
        return None
    historical_fields = record.inherited_fields.keys() - record.human_overrides.keys()
    if historical_fields.intersection(_NUMERIC_FIELDS):
        return None
    stated_fields = record.explicit_fields | record.human_overrides.keys()
    if (
        record.explicit_fields
        and "amount" in stated_fields
        and not {"quantity", "unit_price"}.issubset(stated_fields)
    ):
        return None

    if quantity >= 0 and unit_price < 0:
        return ValidationAlert.from_code(
            AlertCode.LINE_ARITHMETIC_MISMATCH,
            "数量为非负数时，单价不得为负数",
            field="unit_price",
            actual=str(unit_price),
        )

    if record.is_credit:
        target, unit_price = target.copy_abs(), unit_price.copy_abs()
    precision = len(quantity.as_tuple().digits) + len(unit_price.as_tuple().digits)
    with localcontext(prec=precision, Emax=MAX_EMAX, Emin=MIN_EMIN):
        calculated = quantity * unit_price
    if _amounts_differ(calculated, target):
        return ValidationAlert.from_code(
            AlertCode.LINE_ARITHMETIC_MISMATCH,
            "数量乘以单价与行金额不符",
            field="amount",
            expected=str(calculated),
            actual=str(target),
        )
    return None


def _has_numeric_confusion(text: str) -> bool:
    return any(
        any(char.isdigit() for char in token)
        and (token.count(".") > 1 or any(char in "OILSoils" for char in token))
        for token in _NUMERIC_TOKEN.findall(normalize("NFKC", text))
    )


def _line_alerts(
    record: ProcessedBillingRecord, corrected: set[str], alerts: list[ValidationAlert]
) -> list[ValidationAlert]:
    alerts = [*source_format_alerts(record, corrected), *alerts]
    invalid = record.raw_invalid_fields.keys() - corrected
    for field, raw in record.raw_invalid_fields.items():
        if field in invalid and _has_numeric_confusion(raw):
            alerts.append(
                ValidationAlert.from_code(
                    AlertCode.OCR_NUMERIC_CONFUSION,
                    "源单数字疑似 OCR 误识",
                    field=field,
                    actual=raw,
                )
            )

    for field in _NUMERIC_FIELDS:
        number, raw = getattr(record, field), getattr(record, f"raw_{field}_text")
        invalid_number = number is not None and not _is_finite_number(number)
        invalid_text = field not in corrected and raw and _has_numeric_confusion(raw)
        if invalid_number or invalid_text:
            alerts.append(
                ValidationAlert.from_code(
                    AlertCode.OCR_NUMERIC_CONFUSION,
                    "源单数字无效或疑似 OCR 误识",
                    field=field,
                    actual=str(number) if invalid_number else raw,
                )
            )

    if not (record.charge_item or "").strip():
        alerts.append(
            ValidationAlert.from_code(
                AlertCode.LLM_EXTRACTION_FAILED, "费用名称不能为空", field="charge_item"
            )
        )
    if (
        record.amount is None
        and "amount" not in invalid
        and not any(
            alert.field == "amount" and alert.code is AlertCode.OCR_NUMERIC_CONFUSION
            for alert in alerts
        )
    ):
        alerts.append(
            ValidationAlert.from_code(AlertCode.LLM_EXTRACTION_FAILED, "缺少行金额", field="amount")
        )
    if "currency" not in invalid and not is_valid_iso4217(record.currency):
        alerts.append(
            ValidationAlert.from_code(
                AlertCode.INVALID_OR_MISSING_CURRENCY,
                "行币种缺失或不是有效的 ISO 4217 代码",
                field="currency",
                actual=record.currency,
            )
        )
    if "document_date" not in invalid and record.document_date:
        _, error = normalize_document_date(record.document_date)
        if error:
            alerts.append(
                ValidationAlert.from_code(
                    AlertCode.INVALID_DOCUMENT_DATE,
                    error,
                    field="document_date",
                    actual=record.document_date,
                )
            )
    if record.posting_type is None:
        alerts.append(
            ValidationAlert.from_code(AlertCode.ENTITY_NOT_MATCHED, "无法确定应收或应付台账")
        )
    elif "posting_type" in corrected and has_explicit_parties(record):
        # A manual ledger value cannot hide contradictory explicit parties found by the matcher.
        alerts.extend(
            ValidationAlert.from_code(AlertCode.ENTITY_NOT_MATCHED, alert.message)
            for alert in record.validation_alerts
            if alert.code is AlertCode.ENTITY_NOT_MATCHED
        )
    if not record.is_credit and _is_finite_number(record.amount) and record.amount < 0:
        alerts.append(
            ValidationAlert.from_code(
                AlertCode.NEGATIVE_NON_CREDIT_AMOUNT,
                "非贷项单据含负数金额，待复核",
                field="amount",
                actual=str(record.amount),
            )
        )
    if arithmetic_alert := _line_arithmetic_alert(record, invalid):
        alerts.append(arithmetic_alert)

    for container_no in record.shipment_refs.container_nos:
        valid, digit, suggestion = validate_container_number(container_no)
        if valid:
            continue
        message = f"箱号 {container_no} 格式或校验码不符"
        if suggestion:
            message += f"，疑似 OCR 字符混淆，建议修正为 {suggestion}"
        elif digit is not None:
            message += f"，计算出的校验位为 {digit}"
        alerts.append(
            ValidationAlert.from_code(
                AlertCode.LLM_EXTRACTION_FAILED
                if "container_nos" in corrected
                else AlertCode.CONTAINER_ISO6346_MISMATCH,
                message,
                field="container_nos",
                expected=suggestion,
                actual=container_no,
            )
        )
    return alerts


def _prepare_line(
    record: ProcessedBillingRecord,
) -> tuple[ProcessedBillingRecord, set[str], list[ValidationAlert]]:
    record, corrected, override_issues = apply_human_overrides(record)
    if record.document_type or record.debit_credit != "UNKNOWN":
        credit = is_credit_document(record.document_type, record.debit_credit)
        if credit != record.is_credit:
            record = record.model_copy(update={"is_credit": credit})
    if record.is_credit and _is_finite_number(record.amount):
        record = record.model_copy(update={"amount": _signed_amount(record, record.amount)})
    evidence = record.source_evidence.with_sanitized_paths()
    if evidence != record.source_evidence:
        record = record.model_copy(update={"source_evidence": evidence})
    return record, corrected, _line_alerts(record, corrected, override_issues)


def validate_record(record: ProcessedBillingRecord) -> ProcessedBillingRecord:
    """Apply line gates using the current fields and the original parse evidence."""
    record, _, alerts = _prepare_line(record)
    return _with_alerts(record, alerts, _LINE_ALERT_CODES)


def _document_groups(
    bundles: Sequence[DocumentExtractionBundle],
) -> list[list[DocumentExtractionBundle]]:
    groups: dict[str | int, list[DocumentExtractionBundle]] = {}
    for index, bundle in enumerate(bundles):
        groups.setdefault(bundle.document.content_sha256 or index, []).append(bundle)
    return [
        sorted(group, key=lambda bundle: not bundle.document.parent_doc_id)
        for group in groups.values()
    ]


def _document_path(doc_id: str, bundles: dict[str, DocumentExtractionBundle]) -> str:
    parts = []
    seen = set()
    while doc_id in bundles and doc_id not in seen:
        doc = bundles[doc_id].document
        parts.append(sanitize_source_path(doc.public_url or doc.original_filename))
        seen.add(doc_id)
        doc_id = doc.parent_doc_id or ""
    if (
        doc_id
        and doc_id not in bundles
        and len(parts) == 1
        and not {"/", "\\"}.intersection(parts[0])
    ):
        parts.append("邮件附件")
    return "/".join(reversed(parts))


def deduplicate_same_source_documents(
    bundles: Sequence[DocumentExtractionBundle],
) -> tuple[list[DocumentExtractionBundle], dict[str, list[str]]]:
    """Keep one bundle per non-empty hash and collect each merged group's source paths."""
    by_id = {bundle.document.doc_id: bundle for bundle in bundles}
    primary = []
    paths = {}
    for group in _document_groups(bundles):
        primary.append(group[0])
        if len(group) > 1:
            paths[group[0].document.doc_id] = list(
                dict.fromkeys(_document_path(bundle.document.doc_id, by_id) for bundle in group)
            )
    return primary, paths


def _with_reason(record: ProcessedBillingRecord, reason: str) -> ProcessedBillingRecord:
    if reason in record.ai_reasoning:
        return record
    return record.model_copy(update={"ai_reasoning": [*record.ai_reasoning, reason]})


def _merge_duplicate_sources(
    records: Sequence[ProcessedBillingRecord], bundles: Sequence[DocumentExtractionBundle]
) -> tuple[list[ProcessedBillingRecord], dict[str, DocumentExtractionBundle], dict[str, str]]:
    by_id = {bundle.document.doc_id: bundle for bundle in bundles}
    records_by_doc: dict[str, list[ProcessedBillingRecord]] = defaultdict(list)
    for record in records:
        records_by_doc[record.source_doc_id].append(record)

    canonical = {}
    canonical_ids = {}
    merged = {}
    dropped = set()
    for group in _document_groups(bundles):
        primary = group[0]
        primary_id = primary.document.doc_id
        canonical[primary_id] = primary
        canonical_ids.update((bundle.document.doc_id, primary_id) for bundle in group)
        if len(group) == 1:
            continue
        # If only a copy was extracted successfully, keep its rows under the canonical ID.
        origin = next(
            (bundle for bundle in group if bundle.document.doc_id in records_by_doc), primary
        )
        origin_id = origin.document.doc_id
        extraction = origin.extraction or next(
            (bundle.extraction for bundle in group if bundle.extraction is not None), None
        )
        if extraction is not primary.extraction:
            canonical[primary_id] = primary.model_copy(update={"extraction": extraction})
        paths = []
        for bundle in group:
            doc_id = bundle.document.doc_id
            evidence_paths = [
                sanitize_source_path(path)
                for row in records_by_doc.get(doc_id, ())
                for path in row.source_evidence.source_paths
            ]
            paths.extend(evidence_paths or [_document_path(doc_id, by_id)])
            if doc_id != origin_id:
                dropped.add(doc_id)
        merged[origin_id] = (primary_id, list(dict.fromkeys(paths)))

    result = []
    for record in records:
        if record.source_doc_id in dropped:
            continue
        if record.source_doc_id in merged:
            doc_id, paths = merged[record.source_doc_id]
            evidence = record.source_evidence.model_copy(
                update={"source_doc_id": doc_id, "source_paths": paths.copy()}
            )
            totals = record.source_doc_totals
            if totals is not None and totals.source_doc_id == record.source_doc_id:
                totals = totals.model_copy(update={"source_doc_id": doc_id})
            record = record.model_copy(
                update={
                    "source_doc_id": doc_id,
                    "source_evidence": evidence,
                    "source_doc_totals": totals,
                }
            )
            record = _with_reason(record, f"已归并同批重复提交的同源单据：{'、'.join(paths)}")
        result.append(record)
    return result, canonical, canonical_ids


def _signed_amount(record: ProcessedBillingRecord, amount: Decimal) -> Decimal:
    return amount.copy_abs().copy_negate() if record.is_credit else amount


def _source_amount(record: ProcessedBillingRecord) -> Decimal | None:
    if "amount" in record.human_overrides:
        amount = record.amount
    elif "amount" in record.raw_invalid_fields or "amount" in record.inherited_fields:
        return None
    else:
        amount = record.raw_explicit_amount
        if amount is None and (not record.explicit_fields or "amount" in record.explicit_fields):
            amount = record.amount
    return amount if _is_finite_number(amount) else None


def _total_alert(
    code: AlertCode, field: str, message: str, declared: Decimal, calculated: Decimal
) -> ValidationAlert:
    return ValidationAlert.from_code(
        code, message, field=field, expected=str(declared), actual=str(calculated)
    )


def _reconcile_currency_totals(
    by_currency: dict[str, list[Decimal]], totals: SourceDocTotalsSnapshot, credit_only: bool
) -> tuple[list[ValidationAlert], dict[str, Decimal]]:
    alerts = []
    excess_by_currency = {}
    declared = totals.subtotals_by_currency.keys() | totals.totals_by_currency.keys()
    for currency in sorted(declared):
        lines = _decimal_sum(by_currency.get(currency, ()))
        subtotal = totals.subtotals_by_currency.get(currency)
        tax = totals.taxes_by_currency.get(currency, _ZERO)
        total = totals.totals_by_currency.get(currency)
        expected_total = total if total is not None else _ZERO
        if credit_only:
            lines = lines.copy_abs()
            expected_total = expected_total.copy_abs()

        if subtotal is not None:
            expected_lines = subtotal.copy_abs() if credit_only else subtotal
            if _amounts_differ(lines, expected_lines):
                alerts.append(
                    _total_alert(
                        AlertCode.AMOUNT_SUBTOTAL_TAX_MISMATCH,
                        f"subtotals_by_currency.{currency}",
                        f"{currency} 明细合计与源单同币种小计不符",
                        subtotal,
                        lines,
                    )
                )
            calculated = _decimal_sum((subtotal, tax))
            code = AlertCode.AMOUNT_SUBTOTAL_TAX_MISMATCH
            message = f"{currency} 小计加税额与源单同币种总额不符"
        else:
            line_tax = tax.copy_abs() if credit_only else tax
            expected_lines = _decimal_sum((expected_total, line_tax.copy_negate()))
            calculated = _decimal_sum((lines, line_tax))
            code = AlertCode.AMOUNT_TOTAL_MISMATCH
            message = f"{currency} 明细合计及税额与源单同币种总额不符"

        excess_by_currency[currency] = _decimal_sum((lines, expected_lines.copy_negate()))
        if total is not None:
            actual = calculated.copy_abs() if credit_only else calculated
            if _amounts_differ(actual, expected_total):
                alerts.append(
                    _total_alert(code, f"totals_by_currency.{currency}", message, total, actual)
                )

    if totals.totals_by_currency:
        for currency in sorted(by_currency.keys() - declared):
            alerts.append(
                ValidationAlert.from_code(
                    AlertCode.AMOUNT_TOTAL_MISMATCH,
                    f"源单缺少 {currency} 的分币种小计或总额",
                    field=f"totals_by_currency.{currency}",
                    actual=str(_decimal_sum(by_currency[currency])),
                )
            )
    return alerts, excess_by_currency


def _reconcile_totals(
    records: Sequence[ProcessedBillingRecord], totals: SourceDocTotalsSnapshot
) -> tuple[list[ValidationAlert], dict[str, Decimal], bool]:
    alerts = []
    excess_by_currency = {}
    amounts = []
    by_currency: dict[str, list[Decimal]] = defaultdict(list)
    credit_only = True
    for record in records:
        raw = _source_amount(record)
        if raw is None:
            continue
        signed = _signed_amount(record, raw)
        amounts.append(signed)
        credit_only = credit_only and record.is_credit
        if record.currency is not None and is_valid_iso4217(record.currency):
            by_currency[record.currency].append(signed)
    credit_only = bool(amounts) and credit_only

    if totals.subtotals_by_currency or totals.totals_by_currency:
        alerts, excess_by_currency = _reconcile_currency_totals(by_currency, totals, credit_only)
        return alerts, excess_by_currency, credit_only

    subtotal, total = totals.subtotal_amount, totals.total_amount
    if subtotal is None and total is None:
        return alerts, {}, credit_only
    currencies = set(by_currency)
    if totals.total_currency and is_valid_iso4217(totals.total_currency):
        currencies.add(totals.total_currency)
    if len(currencies) > 1:
        alerts.append(
            ValidationAlert.from_code(
                AlertCode.MULTI_CURRENCY_TOTAL_AMBIGUITY,
                "源单含多种币种，无法使用单一汇总金额对账",
                field="total_amount",
                actual="、".join(sorted(currencies)),
            )
        )
        return alerts, {}, credit_only

    lines = _decimal_sum(amounts)
    if credit_only:
        lines = lines.copy_abs()
    tax = totals.tax_amount
    if subtotal is None and tax is not None and credit_only:
        tax = tax.copy_abs()
    declared_lines = subtotal if subtotal is not None else total
    if declared_lines is not None and currencies:
        expected_lines = declared_lines.copy_abs() if credit_only else declared_lines
        if subtotal is None and tax is not None:
            expected_lines = _decimal_sum((expected_lines, tax.copy_negate()))
        excess_by_currency[next(iter(currencies))] = _decimal_sum(
            (lines, expected_lines.copy_negate())
        )
    if subtotal is not None:
        expected_subtotal = subtotal.copy_abs() if credit_only else subtotal
        if _amounts_differ(lines, expected_subtotal):
            alerts.append(
                _total_alert(
                    AlertCode.AMOUNT_SUBTOTAL_TAX_MISMATCH,
                    "subtotal_amount",
                    "明细合计与源单小计不符",
                    subtotal,
                    lines,
                )
            )
    if total is None:
        return alerts, excess_by_currency, credit_only

    expected_total = total.copy_abs() if credit_only else total
    if subtotal is not None:
        calculated = _decimal_sum((subtotal, tax or _ZERO))
        if credit_only:
            calculated = calculated.copy_abs()
        code = AlertCode.AMOUNT_SUBTOTAL_TAX_MISMATCH
        message = "源单小计加税额与总计不符"
    elif tax is not None:
        calculated = _decimal_sum((lines, tax))
        code = AlertCode.AMOUNT_SUBTOTAL_TAX_MISMATCH
        message = "明细合计加税额与总计不符"
    else:
        calculated = lines
        code = AlertCode.AMOUNT_TOTAL_MISMATCH
        message = "明细合计与源单总额不符"
    if _amounts_differ(calculated, expected_total):
        alerts.append(_total_alert(code, "total_amount", message, total, calculated))

    return alerts, excess_by_currency, credit_only


def _duplicate_line_alerts(
    records: Sequence[ProcessedBillingRecord],
    excess_by_currency: dict[str, Decimal],
    credit_only: bool,
) -> dict[int, list[ValidationAlert]]:
    by_position: dict[tuple[str | None, int | None, int], list[int]] = defaultdict(list)
    by_charge: dict[tuple[str, str, Decimal, PaymentTerm | None, str], list[int]] = defaultdict(
        list
    )
    for index, record in enumerate(records):
        evidence = record.source_evidence
        if evidence.line_number is not None:
            by_position[evidence.sheet, evidence.page, evidence.line_number].append(index)
        currency = record.currency
        raw = _source_amount(record)
        if currency is None or excess_by_currency.get(currency, _ZERO) <= 0 or raw is None:
            continue
        contribution = raw.copy_abs() if credit_only else _signed_amount(record, raw)
        charge, _ = normalize_charge_item(record.charge_item)
        if charge and contribution > 0:
            by_charge[
                charge, currency, contribution, record.payment_term, record.debit_credit
            ].append(index)

    alerts: dict[int, list[ValidationAlert]] = defaultdict(list)
    for (_, page, line), indexes in by_position.items():
        if len(indexes) > 1:
            for index in indexes:
                alerts[index].append(
                    ValidationAlert.from_code(
                        AlertCode.DUPLICATE_LINE_EXTRACTED,
                        "源单同一位置被重复抽取",
                        field="line_number",
                        actual=f"{page if page is not None else '—'}/{line}",
                    )
                )
    tolerance = Fraction(DECIMAL_TOLERANCE)
    for (_, currency, amount, _, _), indexes in by_charge.items():
        excess = Fraction(excess_by_currency[currency])
        if any(
            abs(excess - count * Fraction(amount)) <= count * tolerance
            for count in range(1, len(indexes))
        ):
            for index in indexes:
                alerts[index].append(
                    ValidationAlert.from_code(
                        AlertCode.DUPLICATE_LINE_EXTRACTED,
                        "源单重复费用行的金额与明细超额相符",
                        field="amount",
                        actual=str(amount),
                    )
                )
    return alerts


def _reference_key(value: str | None) -> str:
    key = "".join(normalize("NFKC", value or "").upper().split())
    return key if any(character.isalnum() for character in key) else ""


def _source_refs(record: ProcessedBillingRecord) -> ShipmentRefs:
    if "shipment_refs" in record.inherited_fields:
        return ShipmentRefs.model_validate(record.inherited_fields["shipment_refs"] or {})
    return record.shipment_refs


def _business_keys(record: ProcessedBillingRecord) -> set[tuple[str, str, str]]:
    charge, _ = normalize_charge_item(record.charge_item)
    if charge is None:
        return set()
    refs = _source_refs(record)
    identifiers = (
        ("reference_no", record.inherited_fields.get("reference_no", record.reference_no)),
        ("booking_no", refs.booking_no),
        ("bl_no", refs.hbl_no),
        *(("bl_no", ref.value) for ref in refs.mbl_nos),
    )
    return {(charge, field, key) for field, value in identifiers if (key := _reference_key(value))}


def _source_charge_values(record: ProcessedBillingRecord) -> dict[str, Decimal | str]:
    values: dict[str, Decimal | str] = {}
    unavailable = (
        record.inherited_fields.keys() | record.raw_invalid_fields.keys()
    ) - record.human_overrides.keys()
    amount = _source_amount(record)
    if amount is not None:
        values["amount"] = _signed_amount(record, amount)
    if (
        record.currency is not None
        and "currency" not in unavailable
        and is_valid_iso4217(record.currency)
    ):
        values["currency"] = record.currency
    if record.payment_term and "payment_term" not in unavailable:
        values["payment_term"] = record.payment_term
    return values


def _matched_line_indexes(counterparts: dict[int, list[int]]) -> set[int]:
    """Pair compatible rows one-to-one, allowing earlier pairs to be reassigned."""
    left_by_right: dict[int, int] = {}
    right_by_left: dict[int, int] = {}
    for start in counterparts:
        frontier = [start]
        previous_left = {}
        for left in frontier:
            for right in counterparts[left]:
                if right in previous_left:
                    continue
                previous_left[right] = left
                if right not in left_by_right:
                    break
                frontier.append(left_by_right[right])
            else:
                continue

            # Walk back from the open row and reassign each pair along the way.
            available: int | None = right
            while available is not None:
                left = previous_left[available]
                displaced = right_by_left.get(left)
                left_by_right[available] = left
                right_by_left[left] = available
                available = displaced
            break
    return left_by_right.keys() | right_by_left.keys()


def _cross_document_alerts(
    records: Sequence[ProcessedBillingRecord],
) -> dict[int, list[ValidationAlert]]:
    by_business_key: dict[tuple[str, str, str], dict[str, list[int]]] = defaultdict(dict)
    for index, record in enumerate(records):
        for key in sorted(_business_keys(record)):
            by_business_key[key].setdefault(record.source_doc_id, []).append(index)
    by_document_pair: dict[tuple[str, str], set[tuple[int, int]]] = defaultdict(set)
    for sources in by_business_key.values():
        for left, right in combinations(sorted(sources), 2):
            by_document_pair[left, right].update(product(sources[left], sources[right]))

    labels = {"amount": "金额", "currency": "币种", "payment_term": "付款条款"}
    values = [_source_charge_values(record) for record in records]
    alerts: dict[int, list[ValidationAlert]] = defaultdict(list)
    for pairs in by_document_pair.values():
        counterparts: dict[int, list[int]] = defaultdict(list)
        disputed = []
        for left, right in sorted(pairs):
            fields = []
            for field in labels:
                first, second = values[left].get(field), values[right].get(field)
                if first is None or second is None:
                    continue
                if isinstance(first, Decimal) and isinstance(second, Decimal):
                    different = _amounts_differ(first, second)
                else:
                    different = first != second
                if different:
                    fields.append(field)
            if fields:
                disputed.append((left, right, fields))
            elif "amount" in values[left] and "amount" in values[right]:
                counterparts[left].append(right)
        # Two files may repeat an entire set of valid same-name lines at different prices.
        compatible = _matched_line_indexes(counterparts)
        for left, right, fields in disputed:
            if left in compatible or right in compatible:
                continue
            for field in fields:
                message = f"不同源单据对同一业务费用的{labels[field]}存在冲突"
                for index, peer in ((left, right), (right, left)):
                    alerts[index].append(
                        ValidationAlert.from_code(
                            AlertCode.CROSS_DOCUMENT_CONFLICT,
                            message,
                            field=field,
                            actual=str(values[index][field]),
                            expected=str(values[peer][field]),
                        )
                    )
    return alerts


def _snapshot_package_source(
    bundle: DocumentExtractionBundle,
    canonical: DocumentExtractionBundle,
    bundles: dict[str, DocumentExtractionBundle],
    doc_records: Sequence[ProcessedBillingRecord],
) -> PackageShipmentRefs:
    doc = bundle.document
    extraction_bundle = bundle if bundle.extraction is not None else canonical
    extraction = extraction_bundle.extraction
    own = extraction.extraction.shipment_refs if extraction else ShipmentRefs()
    original_hbls = []
    original_mbls = []
    for record in doc_records:
        # The billing row can include BLs borrowed from siblings in its own package.
        snapshot = next(
            (
                source
                for source in record.package_sibling_refs
                if source.source_doc_id == record.source_doc_id
            ),
            None,
        )
        if snapshot is not None:
            original_hbls.extend(snapshot.hbl_nos)
            original_mbls.extend(snapshot.mbl_nos or ())
            continue
        original = (
            ShipmentRefs.model_validate(
                record.source_values_before_overrides["shipment_refs"] or {}
            )
            if "shipment_refs" in record.source_values_before_overrides
            else _source_refs(record)
        )
        original_hbls.append(original.hbl_no)
        original_mbls.extend(original.mbl_nos)
    # Rechecks can receive enriched bundles whose HBL was borrowed from another member.
    field_sources = extraction_bundle.document.metadata.get("sibling_field_sources")
    borrowed_hbl = isinstance(field_sources, list) and any(
        isinstance(source, dict) and source.get("field") == "hbl_no" for source in field_sources
    )
    hbls = [own.hbl_no] if own.hbl_no and not borrowed_hbl else original_hbls
    mbls = None
    if doc.source_kind is not SourceKind.EML_BODY:
        source_path = _document_path(doc.doc_id, bundles)
        mbls_by_source = {(ref.value, ref.source): ref for ref in (*original_mbls, *own.mbl_nos)}
        mbls = [
            SourceRefValue(value=ref.value, source=sanitize_source_path(ref.source) or source_path)
            for ref in mbls_by_source.values()
        ]
    canonical_id = canonical.document.doc_id
    return PackageShipmentRefs(
        source_doc_id=doc.doc_id,
        parent_doc_id=doc.parent_doc_id,
        canonical_doc_id=canonical_id if canonical_id != doc.doc_id else None,
        mbl_nos=mbls,
        hbl_nos=list(dict.fromkeys(hbl for hbl in hbls if hbl)),
    )


def _package_conflicts(
    sources: Sequence[PackageShipmentRefs],
    records: Sequence[ProcessedBillingRecord],
    corrected: Sequence[set[str]],
    indexes_by_doc: dict[str, list[int]],
) -> tuple[list[ValidationAlert], list[SourceRefValue], set[str]]:
    mbl_sources = []
    mbl_evidence = {}
    hbl_sources = []
    hbl_evidence = {}
    for source in sources:
        indexes = indexes_by_doc.get(source.canonical_doc_id or source.source_doc_id, ())
        edited_hbls = [
            records[index].shipment_refs.hbl_no for index in indexes if "hbl_no" in corrected[index]
        ]
        hbl_keys = set()
        for hbl in edited_hbls or source.hbl_nos:
            if key := _reference_key(hbl):
                hbl_keys.add(key)
                hbl_evidence.setdefault(key, hbl)
        if hbl_keys:
            hbl_sources.append(hbl_keys)

        mbls = source.mbl_nos
        if mbls is None:
            continue
        if any("mbl_no" in corrected[index] for index in indexes):
            refs = [
                records[index].shipment_refs
                if "mbl_no" in corrected[index]
                else _source_refs(records[index])
                for index in indexes
            ]
            mbls = [ref for row_refs in refs for ref in row_refs.mbl_nos]
        mbl_keys = set()
        for ref in mbls:
            if key := _reference_key(ref.value):
                mbl_keys.add(key)
                label = sanitize_source_path(ref.source) or source.source_doc_id
                mbl_evidence.setdefault((key, label), SourceRefValue(value=ref.value, source=label))
        if mbl_keys:
            mbl_sources.append(mbl_keys)

    alerts = []
    package_mbls = []
    if len(mbl_sources) > 1 and not set.intersection(*mbl_sources):
        package_mbls = [mbl_evidence[key] for key in sorted(mbl_evidence)]
        alerts.append(
            ValidationAlert.from_code(
                AlertCode.MULTI_SOURCE_BL_MISMATCH,
                "同一邮件包的附件提供了不同的主提单号，待复核",
                field="mbl_nos",
                actual="、".join(dict.fromkeys(ref.value for ref in package_mbls)),
            )
        )
    if len(hbl_sources) > 1 and not set.intersection(*hbl_sources):
        alerts.append(
            ValidationAlert.from_code(
                AlertCode.MULTI_SOURCE_BL_MISMATCH,
                "同一邮件包的子文档提供了不同的分提单号，待复核",
                field="hbl_no",
                actual="、".join(hbl_evidence[key] for key in sorted(hbl_evidence)),
            )
        )
    return alerts, package_mbls, set(hbl_evidence)


def _shared_hbl_alert(
    hbls_by_parent: dict[str | None, set[str]],
    bundles: dict[str, DocumentExtractionBundle],
    prior_alerts: Sequence[ValidationAlert],
) -> ValidationAlert | None:
    candidates = list(hbls_by_parent.values())
    if len(candidates) < 2 or set.intersection(*candidates):
        return None
    actual = "、".join(sorted(set.union(*candidates)))
    sources = [
        (_document_path(parent, bundles) if parent is not None else "", "、".join(sorted(hbls)))
        for parent, hbls in hbls_by_parent.items()
    ]
    message = _SHARED_HBL_MESSAGE
    if all(path for path, _ in sources):
        message += "。邮件来源：" + "、".join(f"{path}（{hbls}）" for path, hbls in sorted(sources))
    else:
        # Detached records retain the HBLs and parent IDs, and the alert retains the source names.
        message = next(
            (
                alert.message
                for alert in prior_alerts
                if alert.code is AlertCode.MULTI_SOURCE_BL_MISMATCH
                and alert.field == "hbl_no"
                and alert.actual == actual
                and alert.message.startswith(_SHARED_HBL_MESSAGE)
            ),
            message,
        )
    return ValidationAlert.from_code(
        AlertCode.MULTI_SOURCE_BL_MISMATCH, message, field="hbl_no", actual=actual
    )


def _merge_package_mbls(
    records: list[ProcessedBillingRecord],
    alerts_by_line: list[list[ValidationAlert]],
    corrected: list[set[str]],
    indexes_by_doc: dict[str, list[int]],
    bundles: dict[str, DocumentExtractionBundle],
    canonical: dict[str, DocumentExtractionBundle],
    canonical_ids: dict[str, str],
    *,
    use_missing_snapshots: bool,
) -> set[str]:
    packages: dict[str, dict[str, PackageShipmentRefs]] = defaultdict(dict)
    parents_by_record_doc: dict[str, set[str]] = defaultdict(set)
    # Build each package from its original members before following duplicate IDs to billing rows.
    for source_id, bundle in sorted(bundles.items()):
        parent_id = bundle.document.parent_doc_id
        canonical_id = canonical_ids[source_id]
        if not parent_id or canonical_id not in canonical:
            continue
        packages[parent_id][source_id] = _snapshot_package_source(
            bundle,
            canonical[canonical_id],
            bundles,
            [records[index] for index in indexes_by_doc.get(canonical_id, ())],
        )
        parents_by_record_doc[canonical_id].add(parent_id)
        parents_by_record_doc[canonical_ids.get(parent_id, parent_id)].add(parent_id)

    checked_docs = set()
    for doc_id, indexes in indexes_by_doc.items():
        saved_sources = (
            [source for index in indexes for source in records[index].package_sibling_refs]
            if use_missing_snapshots
            else []
        )
        parents = set(parents_by_record_doc.get(doc_id, ()))
        for source in saved_sources:
            live = bundles.get(source.source_doc_id)
            parent_id = live.document.parent_doc_id if live else source.parent_doc_id
            if parent_id in packages:
                parents.add(parent_id)
        package_snapshots = {parent_id: packages[parent_id].copy() for parent_id in sorted(parents)}
        default_parent = next(iter(package_snapshots)) if len(package_snapshots) == 1 else None
        for source in saved_sources:
            if source.source_doc_id not in bundles:
                parent_id = source.parent_doc_id or default_parent
                package_snapshots.setdefault(parent_id, {}).setdefault(source.source_doc_id, source)
        snapshots = []
        alerts = []
        mbls = []
        hbls_by_parent = {}
        for parent, members in package_snapshots.items():
            sources = list(members.values())
            package_alerts, package_mbls, package_hbls = _package_conflicts(
                sources, records, corrected, indexes_by_doc
            )
            snapshots.extend(sources)
            alerts.extend(package_alerts)
            mbls.extend(package_mbls)
            if package_hbls:
                hbls_by_parent[parent] = package_hbls
        if shared_hbl := _shared_hbl_alert(
            hbls_by_parent, bundles, records[indexes[0]].validation_alerts
        ):
            alerts = [alert for alert in alerts if alert.field != "hbl_no"]
            alerts.append(shared_hbl)
        if not snapshots:
            continue
        checked_docs.add(doc_id)
        for index in indexes:
            alerts_by_line[index].extend(alerts)
            record = records[index]
            updates = {
                "package_sibling_refs": [source.model_copy(deep=True) for source in snapshots]
            }
            if mbls and not record.human_overrides.keys() & {"shipment_refs", "mbl_no"}:
                shipment_refs = record.shipment_refs
                refs = {(ref.value, ref.source): ref for ref in shipment_refs.mbl_nos}
                for ref in mbls:
                    refs.setdefault((ref.value, ref.source), ref)
                inherited = record.inherited_fields.copy()
                inherited.setdefault("shipment_refs", shipment_refs.model_dump())
                updates["shipment_refs"] = shipment_refs.model_copy(
                    update={"mbl_nos": list(refs.values())}
                )
                updates["inherited_fields"] = inherited
            records[index] = record.model_copy(update=updates)
    return checked_docs


def _ordered_ocr_content(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _ordered_ocr_content(value[key]) for key in sorted(value)}
    if isinstance(value, (list, tuple)):
        return [_ordered_ocr_content(item) for item in value]
    return value


def _ocr_suspicion_alerts(payload: OcrStructuredPayload | None) -> list[ValidationAlert]:
    if payload is None:
        return []
    alerts = []
    for index, item in enumerate(payload.items, 1):
        dumped = item.model_dump(by_alias=True, exclude_none=True)
        if dumped.pop("suspicious", None) is not True:
            continue
        field = f"ocr.item_{index}"
        content = {
            key: value
            for key, value in dumped.items()
            if value not in (None, [], {}) and not (isinstance(value, str) and not value.strip())
        }
        text = content.get("text")
        actual = field
        if len(content) == 1 and isinstance(text, str):
            actual = text.strip()
        elif content:
            actual = dumps_decimal_json(_ordered_ocr_content(content))
        alerts.append(
            ValidationAlert.from_code(
                AlertCode.OCR_SUSPICIOUS_FLAG,
                "源单 OCR 标记了可疑内容",
                field=field,
                actual=actual,
            )
        )
    return alerts


_PreparedLine = tuple[ProcessedBillingRecord, set[str], list[ValidationAlert]]


def _document_parse_issues(extraction: RawExtractionEnvelope) -> list[ValidationAlert]:
    # A line's currency conflict is also recorded in the extraction audit for model rechecks.
    line_conflicts = [
        issue
        for row in extraction.charges
        for issue in row.raw_parse_issues
        if _is_currency_conflict(issue)
    ]
    return [issue for issue in extraction.raw_parse_issues if issue not in line_conflicts]


def _validate_document(
    doc_id: str, records: Sequence[ProcessedBillingRecord], bundle: DocumentExtractionBundle | None
) -> tuple[list[_PreparedLine], set[AlertCode]]:
    extraction = bundle.extraction if bundle is not None else None
    document_issues = _document_parse_issues(extraction) if extraction is not None else []
    # Older rows copied every printed line conflict into the document-wide audit.
    printed_conflicts = [
        issue
        for record in records
        if "currency" in record.raw_invalid_fields
        for issue in record.raw_parse_issues
        if _is_currency_conflict(issue) and issue.message == PRINTED_CURRENCY_CONFLICT_MESSAGE
    ]
    prepared = []
    for record in records:
        raw_issues = record.raw_parse_issues
        if printed_conflicts and "currency" not in record.raw_invalid_fields:
            raw_issues = [issue for issue in raw_issues if issue not in printed_conflicts]
        updates: dict[str, Any] = {}
        if raw_issues is not record.raw_parse_issues:
            updates["raw_parse_issues"] = raw_issues
        if extraction is not None:
            # Route document parse issues through the same per-row override checks.
            updates["raw_invalid_fields"] = (
                extraction.raw_invalid_fields | record.raw_invalid_fields
            )
            updates["raw_parse_issues"] = [
                *raw_issues, *(issue for issue in document_issues if issue not in raw_issues)
            ]
        prepared.append(_prepare_line(record.model_copy(update=updates) if updates else record))

    lines = [record for record, _, _ in prepared]
    totals = (
        SourceDocTotalsSnapshot.from_extraction(doc_id, extraction)
        if extraction is not None
        else next(
            (
                line.source_doc_totals
                for line in lines
                if line.source_doc_totals is not None
                and line.source_doc_totals.source_doc_id == doc_id
            ),
            None,
        )
    )
    alerts, excess, credit_only = (
        _reconcile_totals(lines, totals) if totals is not None else ([], {}, False)
    )
    source_currency_conflicts = [
        issue
        for line in lines
        for issue in line.raw_parse_issues
        if _is_currency_conflict(issue)
    ]
    for _, _, line_alerts in prepared:
        alerts.extend(
            alert for alert in line_alerts if _is_currency_conflict(alert) and alert not in alerts
        )
    recalculated = _LINE_ALERT_CODES.copy()
    if totals is not None:
        recalculated.update(_TOTAL_ALERT_CODES)
    if len(lines) > 1 or bundle is not None:
        recalculated.add(AlertCode.DUPLICATE_LINE_EXTRACTED)
    if bundle is not None:
        recalculated.add(AlertCode.OCR_SUSPICIOUS_FLAG)
        alerts.extend(_ocr_suspicion_alerts(bundle.document.ocr_payload))
    no_summary = totals is not None and (
        totals.subtotal_amount is None
        and totals.total_amount is None
        and not totals.subtotals_by_currency
        and not totals.totals_by_currency
        and not totals.has_explicit_total
        and (extraction is None or "subtotal_amount" not in extraction.raw_invalid_fields)
    )
    duplicates = _duplicate_line_alerts(lines, excess, credit_only)
    checked = []
    for index, (record, corrected, line_alerts) in enumerate(prepared):
        record = record.model_copy(
            update={
                "source_doc_totals": totals,
                "validation_alerts": [
                    alert
                    for alert in record.validation_alerts
                    if alert not in source_currency_conflicts
                ],
            }
        )
        if no_summary:
            record = _with_reason(record, _NO_SUMMARY_REASON)
        elif _NO_SUMMARY_REASON in record.ai_reasoning:
            record = record.model_copy(
                update={
                    "ai_reasoning": [
                        reason for reason in record.ai_reasoning if reason != _NO_SUMMARY_REASON
                    ]
                }
            )
        checked.append((record, corrected, [*line_alerts, *alerts, *duplicates.get(index, ())]))
    return checked, recalculated


def _validate_batch(
    records: Sequence[ProcessedBillingRecord],
    bundles: Sequence[DocumentExtractionBundle],
    *,
    on_document_error: Callable[[str, Exception], None] | None = None,
) -> list[ProcessedBillingRecord]:
    selected, canonical, canonical_ids = _merge_duplicate_sources(records, bundles)
    input_indexes: dict[str, list[int]] = defaultdict(list)
    for index, record in enumerate(selected):
        input_indexes[record.source_doc_id].append(index)

    by_index = {}
    recalculated_by_doc = {}
    for doc_id, indexes in input_indexes.items():
        try:
            checked, recalculated = _validate_document(
                doc_id, [selected[index] for index in indexes], canonical.get(doc_id)
            )
        except Exception as exc:
            if on_document_error is None:
                raise
            on_document_error(doc_id, exc)
            canonical.pop(doc_id, None)
            continue
        by_index.update(zip(indexes, checked, strict=True))
        recalculated_by_doc[doc_id] = recalculated

    validated = []
    alerts_by_line = []
    corrected_by_line = []
    indexes_by_doc: dict[str, list[int]] = defaultdict(list)
    for index in range(len(selected)):
        if index not in by_index:
            continue
        record, corrected, alerts = by_index[index]
        indexes_by_doc[record.source_doc_id].append(len(validated))
        validated.append(record)
        alerts_by_line.append(alerts)
        corrected_by_line.append(corrected)
    if len(indexes_by_doc) > 1 or bundles:
        for recalculated in recalculated_by_doc.values():
            recalculated.add(AlertCode.CROSS_DOCUMENT_CONFLICT)

    for index, alerts in _cross_document_alerts(validated).items():
        alerts_by_line[index].extend(alerts)
    for doc_id in _merge_package_mbls(
        validated,
        alerts_by_line,
        corrected_by_line,
        indexes_by_doc,
        {bundle.document.doc_id: bundle for bundle in bundles},
        canonical,
        canonical_ids,
        use_missing_snapshots=on_document_error is None,
    ):
        recalculated_by_doc[doc_id].add(AlertCode.MULTI_SOURCE_BL_MISMATCH)
    return [
        _with_alerts(record, alerts, recalculated_by_doc[record.source_doc_id])
        for record, alerts in zip(validated, alerts_by_line, strict=True)
    ]


def validate_batch(
    records: Sequence[ProcessedBillingRecord], bundles: Sequence[DocumentExtractionBundle] = ()
) -> list[ProcessedBillingRecord]:
    """Apply line, document, and cross-document gates in input record order."""
    return _validate_batch(records, bundles)


def validate_records(records: Sequence[ProcessedBillingRecord]) -> list[ProcessedBillingRecord]:
    """Validate after entity recalculation when only the billing lines are available."""
    return validate_batch(records)
