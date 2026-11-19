"""Validate billing lines and reconcile evidence within a batch."""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Sequence
from decimal import MAX_EMAX, MIN_EMIN, Decimal, localcontext
from itertools import combinations, product
from string import ascii_uppercase
from unicodedata import normalize

from .charge_normalizer import normalize_charge_item, normalize_document_date
from .currency import is_valid_iso4217
from .models import (
    DECIMAL_TOLERANCE,
    AlertCode,
    AlertSeverity,
    DocumentExtractionBundle,
    OperationType,
    ProcessedBillingRecord,
    RawExtractionEnvelope,
    ReviewStatus,
    ShipmentRefs,
    SourceKind,
    SourceRefValue,
    ValidationAlert,
)

_ZERO = Decimal(0)
_NUMERIC_FIELDS = ("amount", "quantity", "unit_price")
_INVALID_FIELD_CODES = {
    "currency": AlertCode.INVALID_OR_MISSING_CURRENCY,
    "document_date": AlertCode.INVALID_DOCUMENT_DATE,
    **dict.fromkeys(_NUMERIC_FIELDS, AlertCode.OCR_NUMERIC_CONFUSION),
}
_NUMERIC_TOKEN = re.compile(r"(?<!\w)[+-]?[0-9OILSoils.,]+(?:[eE][+-]?[0-9OILSoils]+)?(?!\w)")
_ISO_PREFIX = re.compile(r"[A-Z]{4}[0-9]{6}")
_LETTER_VALUES = dict(zip(ascii_uppercase, (n for n in range(10, 39) if n % 11), strict=True))
_OCR_LETTERS = {"0": "O", "1": "IL", "2": "Z", "5": "S", "8": "B"}
_OCR_DIGITS = {letter: digit for digit, letters in _OCR_LETTERS.items() for letter in letters}
_NO_SUMMARY_REASON = "单据无汇总行，免汇总对账"


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
    record: ProcessedBillingRecord, additions: Sequence[ValidationAlert]
) -> ProcessedBillingRecord:
    candidates = [*record.validation_alerts, *additions]
    resolved = [alert for alert in candidates if alert.severity is AlertSeverity.RESOLVED]
    alerts = {}
    for alert in candidates:
        if alert.severity is not AlertSeverity.RESOLVED and any(
            prior.code is alert.code
            and prior.field in (None, alert.field)
            and prior.actual in (None, alert.actual)
            and (prior.expected is None or alert.expected in (None, prior.expected))
            for prior in resolved
        ):
            continue
        alerts.setdefault((alert.code, alert.field, alert.actual, alert.expected), alert)

    operation, status = record.operation, record.review_status
    if any(
        alert.severity in (AlertSeverity.BLOCKER, AlertSeverity.WARNING)
        for alert in alerts.values()
    ):
        operation, status = OperationType.PENDING_REVIEW, ReviewStatus.NEEDS_REVIEW
    elif status not in (ReviewStatus.APPROVED, ReviewStatus.REJECTED):
        status = (
            ReviewStatus.NEEDS_REVIEW
            if operation is OperationType.PENDING_REVIEW
            else ReviewStatus.AUTO_READY
        )
    return record.model_copy(
        update={
            "validation_alerts": list(alerts.values()),
            "operation": operation,
            "review_status": status,
        }
    )


def _line_arithmetic_alert(record: ProcessedBillingRecord) -> ValidationAlert | None:
    quantity, unit_price = record.quantity, record.unit_price
    if (
        quantity is None
        or unit_price is None
        or not (quantity.is_finite() and unit_price.is_finite())
    ):
        return None
    if {"quantity", "unit_price"}.intersection(record.raw_invalid_fields):
        return None

    if quantity >= 0 and unit_price < 0:
        return ValidationAlert.from_code(
            AlertCode.LINE_ARITHMETIC_MISMATCH,
            "数量为非负数时，单价不得为负数",
            field="unit_price",
            actual=str(unit_price),
        )

    inherited = record.inherited_fields.keys() & set(_NUMERIC_FIELDS)
    if 0 < len(inherited) < len(_NUMERIC_FIELDS) or "amount" in record.raw_invalid_fields:
        return None
    target = record.raw_explicit_amount
    if target is None:
        target = record.amount
    if target is None or not target.is_finite():
        return None
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


def _line_alerts(record: ProcessedBillingRecord) -> list[ValidationAlert]:
    alerts = list(record.raw_parse_issues)
    invalid = record.raw_invalid_fields
    for field, raw in invalid.items():
        if code := _INVALID_FIELD_CODES.get(field):
            alerts.append(
                ValidationAlert.from_code(code, "源单字段值无效，待复核", field=field, actual=raw)
            )
        if _has_numeric_confusion(raw):
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
        if (number is not None and not number.is_finite()) or (raw and _has_numeric_confusion(raw)):
            alerts.append(
                ValidationAlert.from_code(
                    AlertCode.OCR_NUMERIC_CONFUSION,
                    "源单数字无效或疑似 OCR 误识",
                    field=field,
                    actual=raw if raw is not None else str(number),
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
    if (
        not record.is_credit
        and record.amount is not None
        and record.amount.is_finite()
        and record.amount < 0
    ):
        alerts.append(
            ValidationAlert.from_code(
                AlertCode.NEGATIVE_NON_CREDIT_AMOUNT,
                "非贷项单据含负数金额，待复核",
                field="amount",
                actual=str(record.amount),
            )
        )
    if arithmetic_alert := _line_arithmetic_alert(record):
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
                AlertCode.CONTAINER_ISO6346_MISMATCH,
                message,
                field="container_nos",
                expected=suggestion,
                actual=container_no,
            )
        )
    return alerts


def validate_record(record: ProcessedBillingRecord) -> ProcessedBillingRecord:
    """Apply line gates while preserving upstream issues and manual resolutions."""
    if record.is_credit and record.amount is not None and record.amount.is_finite():
        record = record.model_copy(update={"amount": _signed_amount(record, record.amount)})
    return _with_alerts(record, _line_alerts(record))


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
        parts.append(doc.public_url or doc.original_filename)
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
) -> tuple[list[ProcessedBillingRecord], dict[str, DocumentExtractionBundle]]:
    by_id = {bundle.document.doc_id: bundle for bundle in bundles}
    records_by_doc: dict[str, list[ProcessedBillingRecord]] = defaultdict(list)
    for record in records:
        records_by_doc[record.source_doc_id].append(record)

    canonical = {}
    merged = {}
    dropped = set()
    for group in _document_groups(bundles):
        primary = group[0]
        primary_id = primary.document.doc_id
        canonical[primary_id] = primary
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
                path
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
            record = record.model_copy(
                update={"source_doc_id": doc_id, "source_evidence": evidence}
            )
            record = _with_reason(record, f"已归并同批重复提交的同源单据：{'、'.join(paths)}")
        result.append(record)
    return result, canonical


def _signed_amount(record: ProcessedBillingRecord, amount: Decimal) -> Decimal:
    return amount.copy_abs().copy_negate() if record.is_credit else amount


def _total_alert(
    code: AlertCode, field: str, message: str, declared: Decimal, calculated: Decimal
) -> ValidationAlert:
    return ValidationAlert.from_code(
        code, message, field=field, expected=str(declared), actual=str(calculated)
    )


def _reconcile_totals(
    records: Sequence[ProcessedBillingRecord], extraction: RawExtractionEnvelope
) -> tuple[list[ValidationAlert], Decimal, bool]:
    alerts = list(extraction.raw_parse_issues)
    amounts = []
    by_currency: dict[str, list[Decimal]] = defaultdict(list)
    credit_only = True
    for record in records:
        raw = record.raw_explicit_amount
        if raw is None or not raw.is_finite() or "amount" in record.raw_invalid_fields:
            continue
        signed = _signed_amount(record, raw)
        amounts.append(signed)
        credit_only = credit_only and record.is_credit
        if record.currency is not None and is_valid_iso4217(record.currency):
            by_currency[record.currency].append(signed)
    credit_only = bool(amounts) and credit_only

    if extraction.totals_by_currency:
        for currency, total in extraction.totals_by_currency.items():
            actual = _decimal_sum(by_currency.get(currency, ()))
            expected = total.copy_abs().copy_negate() if credit_only else total
            if _amounts_differ(actual, expected):
                alerts.append(
                    _total_alert(
                        AlertCode.AMOUNT_TOTAL_MISMATCH,
                        f"totals_by_currency.{currency}",
                        f"{currency} 明细合计与源单同币种总额不符",
                        total,
                        actual,
                    )
                )
        for currency in sorted(by_currency.keys() - extraction.totals_by_currency.keys()):
            alerts.append(
                ValidationAlert.from_code(
                    AlertCode.AMOUNT_TOTAL_MISMATCH,
                    f"源单缺少 {currency} 的分币种总额",
                    field=f"totals_by_currency.{currency}",
                    actual=str(_decimal_sum(by_currency[currency])),
                )
            )
        return alerts, _ZERO, credit_only

    subtotal, total = extraction.subtotal_amount, extraction.total_amount
    if subtotal is None and total is None:
        return alerts, _ZERO, credit_only
    currencies = set(by_currency)
    if extraction.total_currency and is_valid_iso4217(extraction.total_currency):
        currencies.add(extraction.total_currency)
    if len(currencies) > 1:
        alerts.append(
            ValidationAlert.from_code(
                AlertCode.MULTI_CURRENCY_TOTAL_AMBIGUITY,
                "源单含多种币种，无法使用单一汇总金额对账",
                field="total_amount",
                actual="、".join(sorted(currencies)),
            )
        )
        return alerts, _ZERO, credit_only

    lines = _decimal_sum(amounts)
    if credit_only:
        lines = lines.copy_abs()
    subtotal_balanced = False
    if subtotal is not None:
        expected_subtotal = subtotal.copy_abs() if credit_only else subtotal
        subtotal_balanced = not _amounts_differ(lines, expected_subtotal)
        if not subtotal_balanced:
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
        return alerts, _ZERO, credit_only

    expected_total = total.copy_abs() if credit_only else total
    if subtotal is not None:
        calculated = _decimal_sum((subtotal, extraction.tax_amount or _ZERO))
        if credit_only:
            calculated = calculated.copy_abs()
        code = AlertCode.AMOUNT_SUBTOTAL_TAX_MISMATCH
        message = "源单小计加税额与总计不符"
    else:
        calculated = lines
        code = AlertCode.AMOUNT_TOTAL_MISMATCH
        message = "明细合计与源单总额不符"
    if _amounts_differ(calculated, expected_total):
        alerts.append(_total_alert(code, "total_amount", message, total, calculated))

    # A balanced subtotal accounts for legitimate differences from the tax-inclusive total.
    excess = _ZERO if subtotal_balanced else _decimal_sum((lines, expected_total.copy_negate()))
    return alerts, excess, credit_only


def _duplicate_line_alerts(
    records: Sequence[ProcessedBillingRecord], excess: Decimal, credit_only: bool
) -> dict[int, list[ValidationAlert]]:
    by_position: dict[tuple[str | None, int | None, int], list[int]] = defaultdict(list)
    by_charge: dict[tuple[str, Decimal], list[int]] = defaultdict(list)
    for index, record in enumerate(records):
        evidence = record.source_evidence
        if evidence.line_number is not None:
            by_position[evidence.sheet, evidence.page, evidence.line_number].append(index)
        raw = record.raw_explicit_amount
        if (
            excess <= 0
            or raw is None
            or not raw.is_finite()
            or "amount" in record.raw_invalid_fields
        ):
            continue
        contribution = raw.copy_abs() if credit_only else _signed_amount(record, raw)
        charge, _ = normalize_charge_item(record.charge_item)
        if (
            charge
            and contribution > 0
            and _decimal_sum((excess, DECIMAL_TOLERANCE)) >= contribution
        ):
            by_charge[charge, raw].append(index)

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
    for (_, amount), indexes in by_charge.items():
        if len(indexes) > 1:
            for index in indexes:
                alerts[index].append(
                    ValidationAlert.from_code(
                        AlertCode.DUPLICATE_LINE_EXTRACTED,
                        "源单重复费用行导致明细合计超出总额",
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
    unavailable = record.inherited_fields.keys() | record.raw_invalid_fields.keys()
    amount = record.raw_explicit_amount
    if amount is None and "amount" not in unavailable:
        amount = record.amount
    if amount is not None and amount.is_finite() and "amount" not in record.raw_invalid_fields:
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


def _merge_package_mbls(
    records: list[ProcessedBillingRecord],
    indexes_by_doc: dict[str, list[int]],
    bundles: dict[str, DocumentExtractionBundle],
) -> None:
    children: dict[str, list[str]] = defaultdict(list)
    for doc_id, bundle in bundles.items():
        if parent_id := bundle.document.parent_doc_id:
            children[parent_id].append(doc_id)
    for parent_id, child_ids in children.items():
        sources = []
        refs_by_evidence = {}
        for doc_id in child_ids:
            bundle = bundles[doc_id]
            if bundle.document.source_kind is SourceKind.EML_BODY:
                continue
            refs = [
                ref
                for index in indexes_by_doc.get(doc_id, ())
                for ref in _source_refs(records[index]).mbl_nos
            ]
            if bundle.extraction is not None:
                refs.extend(bundle.extraction.extraction.shipment_refs.mbl_nos)
            keys = set()
            for ref in refs:
                if key := _reference_key(ref.value):
                    keys.add(key)
                    source = ref.source or _document_path(doc_id, bundles)
                    refs_by_evidence.setdefault(
                        (key, source), SourceRefValue(value=ref.value, source=source)
                    )
            if keys:
                sources.append(keys)
        if len(sources) < 2 or set.intersection(*sources):
            continue

        package_refs = [refs_by_evidence[key] for key in sorted(refs_by_evidence)]
        alert = ValidationAlert.from_code(
            AlertCode.MULTI_SOURCE_BL_MISMATCH,
            "同一邮件包的附件提供了不同的主提单号，待复核",
            field="mbl_nos",
            actual="、".join(dict.fromkeys(ref.value for ref in package_refs)),
        )
        for doc_id in (parent_id, *child_ids):
            for index in indexes_by_doc.get(doc_id, ()):
                record = records[index]
                shipment_refs = record.shipment_refs
                refs = {(ref.value, ref.source): ref for ref in shipment_refs.mbl_nos}
                for ref in package_refs:
                    refs.setdefault((ref.value, ref.source), ref)
                inherited = record.inherited_fields.copy()
                inherited.setdefault("shipment_refs", shipment_refs.model_dump())
                updated_refs = shipment_refs.model_copy(update={"mbl_nos": list(refs.values())})
                records[index] = _with_alerts(
                    record.model_copy(
                        update={
                            "shipment_refs": updated_refs,
                            "inherited_fields": inherited,
                        }
                    ),
                    [alert],
                )


def validate_batch(
    records: Sequence[ProcessedBillingRecord], bundles: Sequence[DocumentExtractionBundle] = ()
) -> list[ProcessedBillingRecord]:
    """Apply line, document, and cross-document gates in input record order."""
    selected, canonical = _merge_duplicate_sources(records, bundles)
    validated = [validate_record(record) for record in selected]
    indexes_by_doc: dict[str, list[int]] = defaultdict(list)
    for index, record in enumerate(validated):
        indexes_by_doc[record.source_doc_id].append(index)

    for doc_id, indexes in indexes_by_doc.items():
        bundle = canonical.get(doc_id)
        extraction = bundle.extraction if bundle is not None else None
        lines = [validated[index] for index in indexes]
        alerts, excess, credit_only = (
            _reconcile_totals(lines, extraction) if extraction else ([], _ZERO, False)
        )
        no_summary = extraction is not None and (
            extraction.subtotal_amount is None
            and extraction.total_amount is None
            and not extraction.totals_by_currency
            and not extraction.has_explicit_total
            and "subtotal_amount" not in extraction.raw_invalid_fields
        )
        payload = bundle.document.ocr_payload if bundle is not None else None
        if payload and any(
            (item.model_extra or {}).get("suspicious") is True for item in payload.items
        ):
            alerts.append(
                ValidationAlert.from_code(AlertCode.OCR_SUSPICIOUS_FLAG, "源单 OCR 标记了可疑内容")
            )
        duplicates = _duplicate_line_alerts(lines, excess, credit_only)
        for offset, index in enumerate(indexes):
            record = validated[index]
            if no_summary:
                record = _with_reason(record, _NO_SUMMARY_REASON)
            validated[index] = _with_alerts(record, [*alerts, *duplicates.get(offset, ())])

    for index, alerts in _cross_document_alerts(validated).items():
        validated[index] = _with_alerts(validated[index], alerts)
    _merge_package_mbls(validated, indexes_by_doc, canonical)
    return validated


def validate_records(records: Sequence[ProcessedBillingRecord]) -> list[ProcessedBillingRecord]:
    """Validate after entity recalculation when only the billing lines are available."""
    return validate_batch(records)
