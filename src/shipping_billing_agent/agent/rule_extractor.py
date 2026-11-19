"""Read billing evidence without a model or access to the source files."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any
from unicodedata import normalize

from shipping_billing_agent.domain import (
    AccountingEntityProfile,
    AlertCode,
    ExcludedClause,
    ParsedDocument,
    RawChargeEnvelope,
    RawDocumentExtraction,
    RawExtractionEnvelope,
    ShipmentRefs,
    SourceRefValue,
    ValidationAlert,
    dumps_decimal_json,
    normalize_currency,
    normalize_party_name,
    normalize_payment_term,
    parse_decimal_strict,
    parse_llm_raw_extraction,
)
from shipping_billing_agent.domain.models import DocumentStatus, SourceKind
from shipping_billing_agent.gateways.text_sanitizer import sanitize_for_llm

_PAGE = re.compile(r"^\[Page (\d+)(?:\s*\|[^]]+)?\]$", re.I)
_SHEET_PREFIX = re.compile(r"^\[Sheet: .*? \| Row \d+\]\s*")
_DATE = (
    r"(?:[0-9OILS]{4}[-/][0-9OILS]{1,2}[-/][0-9OILS]{1,2}"
    r"|[0-9OILS]{1,2}[-/][0-9OILS]{1,2}[-/][0-9OILS]{4})"
)
_NUMBER = r"[-+]?(?=[0-9OILS,.]*[0-9])[0-9OILS]+(?:[.,][0-9OILS]+)*(?:[Ee][-+]?[0-9]+)?"
_CURRENCY = r"(?:US\$|CA\$|HK\$|CN¥|[$€£¥]|[A-Z]{3})"
_TERM = r"(?:PP|CC|P|C|PREPAID|COLLECT)"
_REF = r"(?=[A-Z0-9./-]*[0-9])[A-Z0-9][A-Z0-9./-]*"
_BOX = re.compile(r"\b[A-Z]{3}[UJZ][0-9]{7}\b", re.I)
_LABELED_BOX = re.compile(r"\b[A-Z]{4}[0-9OILS]{7}\b", re.I)
_COMPANY = re.compile(
    r"^.+?\b(?:CO[., ]+LTD|INCORPORATED|CORPORATION|LIMITED|LTD|INC|LLC|GMBH)\b[.,]?",
    re.I,
)
_BILL_TITLE = re.compile(
    r"^(DEBIT\s*(?:NOTE|INVOICE)|CREDIT\s*NOTE|(?:TAX\s+)?INVOICE|GENERAL\s+NOTICE)\b",
    re.I,
)
_BL_FILE = re.compile(r"(?:^|[\W_])([MH]BL)(?:[\W_]|$)", re.I)
_FEE = re.compile(
    r"\b(?:charges?|fees?|wharfage|handling|detention|demurrage|trucking|storage|"
    r"freight|on carriage|thc|h/c|doc(?:umentation)?)\b|费用|費用|操作费",
    re.I,
)
_REMITTANCE = re.compile(
    r"^\s*bank\s*:|\b(?:bank\s+(?:of|account|details|transfers?)|"
    r"remit(?:tance)?\s+to|beneficiar(?:y|ies)|"
    r"ACH|WIRE|ROUTING\s*(?:NUMBER|NO\.?|#|:|[0-9]|$)|SWIFT|IBAN|ABA\s*[/#: ]|"
    r"(?:acct|account)\s*(?:no\.?|number|#|:))|银行|銀行|汇款|匯款|收款账号",
    re.I,
)
_CONDITIONAL = re.compile(
    r"\b(?:if|cancel(?:l)?ed|penalt(?:y|ies)|misuse|in the event|after expiration)\b|"
    r"^\s*fee of\s+\$|违约|違約|罚金|罰金",
    re.I,
)
_BANK_REASON = "银行汇款信息"
_CONDITIONAL_REASON = "条件收费或违约条款"
_INSTRUCTION = re.compile(r"\bignore\s+(?:all|previous|prior)\b", re.I)
_SUMMARY = re.compile(
    r"^(?P<label>sub[\s-]*total|(?:sales\s+)?tax(?:\s+amount)?|"
    r"grand\s+total(?:\s+balance\s+due)?|total\s+charges(?:\s+to\s+be\s+paid)?|"
    r"total(?:\s+(?:amount|due))?|balance\s+due|amount\s+due|小[计計]|税[额額]|[总總][计計额額])"
    r"(?=\W|$)\s*[:=]?\s*(?P<rest>.*)$",
    re.I,
)
_MONEY = re.compile(
    rf"(?:(?P<prefix>{_CURRENCY})\s*)?(?P<amount>{_NUMBER})"
    rf"(?:\s*(?P<suffix>{_CURRENCY}))?",
    re.I,
)
_SUMMARY_DUE_TO = re.compile(rf"^(?P<party>.+?)\s+(?=(?:{_CURRENCY}\s+)?{_NUMBER}(?:\s|$))", re.I)
_PAYMENT_INSTRUCTION = re.compile(
    rf"^(?P<payer>.+?)\s+(?:pays?|to pay)\s+(?P<payee>[^:]+):\s*"
    rf"(?P<name>.+?)\s*(?P<money>(?:{_CURRENCY}\s*)?{_NUMBER}(?:\s*{_CURRENCY})?)$",
    re.I,
)
_LEDGER_ROW = re.compile(
    rf"^(?P<name>.+?)\s+[-–]\s+(?P<unit_price>{_NUMBER})\s+(?P<quantity>{_NUMBER})"
    rf"\s+(?P<payment_term>{_TERM})\s+(?P<amount>{_NUMBER})"
    rf"(?:\s+(?P<credit>{_NUMBER}))?$",
    re.I,
)
_TEXT_ROW = re.compile(
    rf"^(?P<name>.+?)[ :\t]+(?:(?P<prefix>{_CURRENCY})\s*)?(?P<amount>{_NUMBER})"
    rf"(?:\s*(?P<suffix>{_CURRENCY}))?(?:\s+(?P<term>{_TERM}))?$",
    re.I,
)
_RATE_ROW = re.compile(
    rf"^(?P<name>wharfage(?: administration fee)?)\s+"
    rf"(?P<rate>{_NUMBER})(?:\s*(?P<rate_currency>{_CURRENCY}))?\s+"
    rf"(?P<amount>{_NUMBER})(?:\s*(?P<currency>{_CURRENCY}))?(?:\s+(?P<term>{_TERM}))?$",
    re.I,
)
_TRANSPORT_LABEL = re.compile(
    r"^\s*\*?(?:PORT OF (?:LOADING|DISCHARGE)|PLACE OF (?:RECEIPT|DELIVERY)|"
    r"OPERATIONAL (?:LOAD|DISCH\.) PORT)\b|\bFREIGHT\s+(?:PREPAID|COLLECT)\b",
    re.I,
)
_US_CONTEXT = re.compile(
    r"\b(?:UNITED STATES|U\.S\.A\.?|LONG BEACH|LOS ANGELES|US DOLLARS|"
    r"PORT EVERGLADES|[A-Z]+,\s*(?:CA|NY|FL|TX)\s*[0-9]{5})\b",
    re.I,
)

_COLUMN_KEYS = {
    "raw_charge_name": (
        "description",
        "charge",
        "fee",
        "charge description",
        "charge name",
        "charge item",
        "name",
        "raw charge name",
        "charge type",
        "item",
        "费用名称",
    ),
    "quantity": ("qty", "quantity", "数量"),
    "unit_price": ("rate", "unit price", "unit rate", "单价"),
    "amount": ("amount", "charge amount", "line amount", "金额"),
    "currency": ("currency", "curr", "cur", "currency code", "币种"),
    "payment_term": ("p/c", "payment term", "payment terms", "pay term", "pp/cc", "条款"),
    "debit": ("debit", "debit (+)", "debit amount"),
    "credit": ("credit", "credit (-)", "credit amount"),
    "debit_credit": ("debit credit", "d/c"),
}
_OCR_FIELDS = {
    "document_type": ("fileType", "documentType"),
    "reference_no": ("invoiceNumber", "referenceNo", "debitNoteNumber", "creditNoteNumber"),
    "document_date": ("invoiceDate", "documentDate", "date", "debitNoteDate", "creditNoteDate"),
    "issuer": ("issuer", "seller", "shipper"),
    "recipient": ("recipient", "billTo", "consignee"),
    "payer_hint": ("payer", "payerHint"),
    "payee_hint": ("payee", "payeeHint", "balanceDueTo"),
    "subtotal_amount": ("subtotal", "subtotalAmount", "netAmount"),
    "tax_amount": ("tax", "taxAmount", "vatAmount"),
    "total_amount": (
        "total",
        "totalAmount",
        "grandTotal",
        "totalCharges",
        "totalChargesToBePaid",
        "amountDue",
        "balanceDue",
    ),
    "total_currency": ("totalCurrency", "currency", "currencyCode"),
}
_REFERENCE_LABELS = {
    "reference_no": (
        r"(?:invoice|debit\s*note|credit\s*note|D/C|D/N|C/N|reference)\s*"
        r"(?:number|no\.?|#|id)|(?:debit\s*)?invoice\s*:"
    ),
    "hbl_no": r"(?:HBL|HOUSE\s+B/?L)(?:\s*(?:number|no\.?|#))?",
    "booking_no": r"(?:booking|bkg)(?:\s*(?:number|no\.?|#))?",
}
_PARTY_LABELS = {
    "issuer": r"issuer|issued by|bill from|seller|vendor",
    "recipient": r"recipient|bill to|sold to|customer",
    "payer_hint": r"payer|paid by",
    "payee_hint": r"payee|paid to|balance due to",
}


@dataclass(frozen=True, slots=True)
class _Line:
    text: str
    number: int
    page: int | None = None


@dataclass(slots=True)
class _Evidence:
    fields: dict[str, Any] = field(default_factory=dict)
    charges: list[dict[str, Any]] = field(default_factory=list)
    mbl_nos: list[SourceRefValue] = field(default_factory=list)
    container_nos: list[str] = field(default_factory=list)
    clauses: list[ExcludedClause] = field(default_factory=list)
    issues: list[ValidationAlert] = field(default_factory=list)

    def add_mbl(self, value: str, source: str) -> None:
        ref = SourceRefValue(value=value.upper(), source=source)
        if ref not in self.mbl_nos:
            self.mbl_nos.append(ref)

    def add_containers(self, value: str, *, labeled: bool = False) -> None:
        pattern = _LABELED_BOX if labeled else _BOX
        self.container_nos.extend(
            match[0].upper()
            for match in pattern.finditer(value)
            if any(c.isdigit() for c in match[0])
        )

    def add_clause(self, text: str, reason: str, page: int | None) -> None:
        clause = ExcludedClause(text=sanitize_for_llm(text), reason=reason, page=page)
        if clause not in self.clauses:
            self.clauses.append(clause)


def _text_lines(text: str) -> list[_Line]:
    lines = []
    page = None
    for number, line in enumerate(text.splitlines(), 1):
        text = _SHEET_PREFIX.sub("", normalize("NFKC", line).strip())
        if match := _PAGE.fullmatch(text):
            page = int(match[1])
        elif text:
            lines.append(_Line(text, number, page))
    return lines


def _key(text: str) -> str:
    return re.sub(r"[\W_]+", "", normalize("NFKC", text).casefold())


def _fields_for_keys(
    values: Mapping[str, Any], aliases: Mapping[str, Sequence[str]]
) -> dict[str, Any]:
    keyed = {_key(key): value for key, value in values.items()}
    fields = {}
    for field_name, keys in aliases.items():
        for key in keys:
            value = keyed.get(_key(key))
            if value is not None and value != "":
                fields[field_name] = value
                break
    return fields


def _non_billing_reason(text: str) -> str | None:
    if _REMITTANCE.search(text):
        return _BANK_REASON
    if re.search(r"\bPAID\s+AMOUNT\b|\bAMOUNT\s+PAID\b", text, re.I):
        return "已付金额"
    if re.search(r"\bCARRIER\s+FREE\s+H\s*/?\s*C\b", text, re.I):
        return "承运方免收操作费"
    if _CONDITIONAL.search(text):
        return _CONDITIONAL_REASON
    return None


def _capture_clauses(lines: Sequence[_Line], evidence: _Evidence) -> set[int]:
    skipped = set()
    index = 0
    while index < len(lines):
        start = lines[index]
        reason = _non_billing_reason(start.text)
        if not reason:
            index += 1
            continue
        clause = [start.text]
        skipped.add(start.number)
        index += 1
        # PDF line wrapping can split "per" and "cancelled" across physical lines.
        if reason == _CONDITIONAL_REASON:
            while index < len(lines) and len(clause) < 4:
                following = lines[index]
                if (
                    following.page != start.page
                    or following.number != start.number + len(clause)
                    or _non_billing_reason(following.text) != reason
                ):
                    break
                skipped.add(following.number)
                clause.append(following.text)
                index += 1
        elif reason == _BANK_REASON and index < len(lines):
            following = lines[index]
            if (
                following.page == start.page
                and following.number == start.number + 1
                and re.fullmatch(r"[0-9][0-9 -]{7,}", following.text)
            ):
                clause.append(following.text)
                skipped.add(following.number)
                index += 1
        evidence.add_clause(" ".join(clause), reason, start.page)
    return skipped


def _record_summary(text: str, evidence: _Evidence) -> bool:
    match = _SUMMARY.match(text)
    if not match:
        return False
    label, rest = match["label"].casefold(), match["rest"]
    if rest.upper().startswith("TO "):
        due_to = _SUMMARY_DUE_TO.match(rest[3:])
        if not due_to:
            return True
        evidence.fields.setdefault("payee_hint", due_to["party"].strip())
        rest = rest[3 + due_to.end() :]
    stated_currency = re.search(rf"\b(?:currency|curr|cur)\s*:?\s*({_CURRENCY})(?!\w)", rest, re.I)
    if stated_currency:
        rest = rest[: stated_currency.start()].rstrip()
    # "TOTAL THREE (3)" on a B/L is a package count.
    money = _MONEY.match(rest)
    if money is None or (money.end() < len(rest) and rest[money.end()].isalnum()):
        return True
    amount, currency = money["amount"], money["prefix"] or money["suffix"]
    tail = rest[money.end() :].strip()
    if stated_currency:
        currency = stated_currency[1]
    elif tail and not re.fullmatch(_NUMBER, tail, re.I):
        return True
    field_name = "total_amount"
    if label.startswith(("sub", "小")):
        field_name = "subtotal_amount"
    elif "tax" in label or label.startswith("税"):
        field_name = "tax_amount"
    _remember_ocr_or_printed_value(evidence, field_name, amount)
    if currency:
        evidence.fields.setdefault("total_currency", currency)
    return True


def _remember_ocr_or_printed_value(evidence: _Evidence, field_name: str, value: Any) -> None:
    previous = evidence.fields.setdefault(field_name, value)
    if previous == value:
        return
    if field_name in ("total_amount", "subtotal_amount", "tax_amount"):
        try:
            if parse_decimal_strict(previous) == parse_decimal_strict(value):
                return
        except ValueError:
            # Preserve an unreadable value in the conflict evidence for manual review.
            pass
    elif field_name != "reference_no":
        return
    issue = ValidationAlert.from_code(
        AlertCode.CROSS_DOCUMENT_CONFLICT,
        "同一文件提供了不同的源单字段值",
        field=field_name,
        expected=str(previous),
        actual=str(value),
    )
    if issue not in evidence.issues:
        evidence.issues.append(issue)


def _value_after(lines: Sequence[_Line], label: str, value: str) -> str | None:
    pattern = re.compile(rf"^\s*(?:{label})(?:(?<=:)|(?=\W|$))\s*[:#=]?\s*(.*)$", re.I)
    for index, line in enumerate(lines):
        if not (match := pattern.match(line.text)):
            continue
        rest = match[1]
        if not rest and index + 1 < len(lines) and lines[index + 1].page == line.page:
            rest = lines[index + 1].text
        if found := re.match(value, rest, re.I):
            return found[0].strip()
    return None


def _first_company(text: str) -> str:
    match = _COMPANY.match(text)
    return match[0] if match else text


def _party(lines: Sequence[_Line], label: str) -> str | None:
    value = _value_after(lines, label, r"[^\d\W][^\n]*")
    return _first_company(value) if value else None


def _collect_text_headers(
    doc: ParsedDocument, lines: Sequence[_Line], evidence: _Evidence
) -> str | None:
    title = next(
        (_BILL_TITLE.match(line.text) for line in lines if _BILL_TITLE.match(line.text)), None
    )
    if title:
        evidence.fields.setdefault(
            "document_type", title[0].upper().replace("DEBITINVOICE", "DEBIT INVOICE")
        )
    for field_name, label in _REFERENCE_LABELS.items():
        if value := _value_after(lines, label, _REF):
            evidence.fields.setdefault(field_name, value.upper())
    date = _value_after(
        lines,
        r"(?:invoice|document|issue|D/C|D/N|C/N|debit note|credit note)?\s*date|general notice",
        _DATE,
    )
    if date is None and doc.source_kind is SourceKind.EML_BODY:
        date = next((line.text for line in lines if re.fullmatch(_DATE, line.text, re.I)), None)
    if date:
        evidence.fields.setdefault("document_date", date)
    for field_name, label in _PARTY_LABELS.items():
        value = _party(lines, label)
        if value and (field_name != "payer_hint" or " " in value):
            evidence.fields.setdefault(field_name, value)
    if title:
        leading_companies = []
        for line in lines[:18]:
            if _REMITTANCE.search(line.text) or "PAYMENTS MUST BE ISSUED" in line.text.upper():
                break
            if _COMPANY.match(line.text) and not line.text.upper().startswith("AS AGENTS FOR"):
                leading_companies.append(_first_company(line.text))
        if leading_companies:
            evidence.fields.setdefault("issuer", leading_companies[0])
            if len(leading_companies) > 1 and title[0].upper() == "INVOICE":
                evidence.fields.setdefault("recipient", leading_companies[1])
    if "reference_no" not in evidence.fields and (
        notice_number := _value_after(lines, r"(?:debit|credit)\s+note|invoice", _REF)
    ):
        evidence.fields["reference_no"] = notice_number
    if "reference_no" not in evidence.fields:
        for line in lines[:18]:
            if re.fullmatch(rf"(?:DN|CN|INV)-{_REF}", line.text, re.I):
                evidence.fields["reference_no"] = line.text.upper()
                break
    return title[0] if title else None


def _collect_transport(
    doc: ParsedDocument, lines: Sequence[_Line], evidence: _Evidence, has_bill_title: bool
) -> bool:
    file_match = (
        _BL_FILE.search(doc.sanitized_filename)
        if doc.sanitized_filename.casefold().endswith(".pdf")
        else None
    )
    bl_kind = file_match[1].upper() if file_match else ""
    is_transport = not has_bill_title and (
        bool(bl_kind)
        or "BILL OF LADING" in (evidence.fields.get("document_type") or "").upper()
        or any(re.search(r"\bBILL OF LADING\b", line.text, re.I) for line in lines)
    )
    filename = doc.sanitized_filename
    mbl = _value_after(lines, r"(?:MBL|MASTER\s+B/?L)(?:\s*(?:number|no\.?|#))?", _REF)
    bl = _value_after(lines, r"B/?L(?:\s*(?:number|no\.?|#))?", _REF)
    if mbl:
        evidence.add_mbl(mbl, filename)
    if bl:
        if bl_kind == "HBL":
            evidence.fields.setdefault("hbl_no", bl.upper())
        else:
            evidence.add_mbl(bl, filename)
    for index, line in enumerate(lines):
        evidence.add_containers(
            line.text,
            labeled=bool(re.search(r"\b(?:CONTAINER|CNTR)\.?\s*(?:NO|#)", line.text, re.I)),
        )
        upper = line.text.upper()
        if index + 1 < len(lines) and lines[index + 1].page == line.page:
            following = lines[index + 1].text
            if upper.startswith("SHIPPER") and "B/L" in upper:
                refs = re.findall(rf"\s+({_REF})(?=\s|$)", following, re.I)
                if refs and "DOCUMENT NO" in upper:
                    evidence.fields.setdefault("hbl_no", refs[-1].upper())
                elif len(refs) >= 2 and "BOOKING" in upper:
                    evidence.fields.setdefault("booking_no", refs[-2].upper())
                    evidence.add_mbl(refs[-1], filename)
        if bl_kind == "HBL" and (
            exported := re.match(rf"^EXPORT REFERENCES\s+({_REF})$", line.text, re.I)
        ):
            evidence.add_mbl(exported[1], filename)
    if is_transport:
        evidence.fields.setdefault("document_type", "BILL OF LADING")
        parties = {
            "issuer": r"shipper(?:/exporter)?(?:\s+booking number.*|\s+document no.*)?",
            "recipient": r"consignee(?:\([^)]*\))?(?:\s+forwarding agent.*)?",
        }
        for field_name, label in parties.items():
            if value := _party(lines, label):
                evidence.fields.setdefault(field_name, value)
    return is_transport


def transport_context(doc: ParsedDocument) -> list[str]:
    """Select labeled port and freight terms for a transport document."""
    lines = _text_lines(doc.raw_text)
    values = []
    for index, line in enumerate(lines):
        if not _TRANSPORT_LABEL.search(line.text):
            continue
        text = line.text
        if (
            text.rstrip(":").upper()
            in {"PORT OF LOADING", "PORT OF DISCHARGE", "PLACE OF RECEIPT", "PLACE OF DELIVERY"}
            and index + 1 < len(lines)
            and lines[index + 1].page == line.page
        ):
            text = f"{text.rstrip(':')}: {lines[index + 1].text}"
        values.append(text)
    return list(dict.fromkeys(values))


def _charge_from_columns(
    fields: dict[str, Any], text: str, line_number: int, page: int | None
) -> dict[str, Any]:
    debit, credit = fields.pop("debit", None), fields.pop("credit", None)
    if fields.get("amount") is None:
        # A printed zero in the opposite column can accompany the billed amount.
        for direction, value in (("CREDIT", credit), ("DEBIT", debit)):
            if value is not None and not re.fullmatch(r"[-+]?0+(?:\.0+)?", str(value).strip()):
                fields.update(amount=value, debit_credit=direction)
                break
        else:
            if debit is not None or credit is not None:
                fields["amount"] = debit if debit is not None else credit
                fields["debit_credit"] = "DEBIT" if debit is not None else "CREDIT"
    if fields.get("debit_credit") is not None:
        fields["debit_credit"] = str(fields["debit_credit"]).strip().upper()
    return {
        **fields,
        "source_text": text,
        "line_number": line_number,
        "page": page,
    }


def _collect_tables(doc: ParsedDocument, evidence: _Evidence) -> None:
    row_number = 0
    for table in doc.tables:
        columns: dict[str, int] = {}
        page_match = _PAGE.fullmatch(table.source or "")
        page = int(page_match[1]) if page_match else None
        for row_index, row in enumerate(table.rows):
            row_number += 1
            cells = [cell.strip() for cell in row]
            by_header = _fields_for_keys(
                dict(zip(cells, range(len(cells)), strict=True)), _COLUMN_KEYS
            )
            if "raw_charge_name" in by_header and by_header.keys() & {
                "amount",
                "debit",
                "credit",
                "payment_term",
            }:
                columns = by_header
                continue
            joined = "\t".join(cells)
            if reason := _non_billing_reason(joined):
                evidence.add_clause(" ".join(filter(None, cells)), reason, page)
                continue
            if not columns:
                _record_summary(" ".join(filter(None, cells)), evidence)
                continue
            fields = {
                key: cells[index]
                for key, index in columns.items()
                if index < len(cells) and cells[index]
            }
            name = fields.get("raw_charge_name", "")
            if _SUMMARY.match(name):
                amount = fields.get("amount") or fields.get("debit") or fields.get("credit")
                currency = fields.get("currency", "")
                _record_summary(f"{name} {currency} {amount or ''}", evidence)
                continue
            if not name or not (fields.keys() & {"amount", "debit", "credit", "payment_term"}):
                continue
            source = f"{table.source or '[Table]'} [Row {row_index + 1}] {joined}"
            evidence.charges.append(_charge_from_columns(fields, source, row_number, page))


def _collect_ocr(doc: ParsedDocument, evidence: _Evidence) -> None:
    if doc.ocr_payload is None:
        return
    currency_totals = {}
    for index, item in enumerate(doc.ocr_payload.items, 1):
        page = index
        extra = item.model_extra or {}
        for name in ("page", "pageNo", "pageNumber", "pageNum", "page_index"):
            if str(raw_page := extra.get(name)).isdigit():
                page = max(1, int(raw_page))
                break
        source = f"{doc.sanitized_filename} [OCR {index}]"
        item_fields: dict[str, Any] = {}
        item_term = None
        for fields in (item.model_dump(by_alias=True, exclude_none=True), item.other):
            if not isinstance(fields, Mapping):
                continue
            for name, value in _fields_for_keys(fields, _OCR_FIELDS).items():
                item_fields.setdefault(name, value)
            terms = _fields_for_keys(
                fields, {"term": ("paymentTerm", "paymentTerms", "P/C", "terms")}
            )
            if not item_term and isinstance(terms.get("term"), str):
                item_term = normalize_payment_term(terms["term"])
            refs = _fields_for_keys(
                fields,
                {
                    "mbl": ("mblNumber", "mblNo", "masterBillOfLading"),
                    "hbl_no": ("hblNumber", "hblNo", "houseBillOfLading"),
                    "booking_no": ("bookingNumber", "bookingNo"),
                    "totals_by_currency": ("totalsByCurrency",),
                },
            )
            mbl = refs.pop("mbl", None)
            if isinstance(mbl, str) and mbl.strip():
                evidence.add_mbl(mbl.strip(), source)
            separate_totals = refs.pop("totals_by_currency", None)
            if isinstance(separate_totals, Mapping):
                totals = evidence.fields.setdefault("totals_by_currency", {})
                if isinstance(totals, dict):
                    for currency, amount in separate_totals.items():
                        totals.setdefault(currency, amount)
            elif separate_totals is not None:
                evidence.fields.setdefault("totals_by_currency", separate_totals)
            for name, value in refs.items():
                evidence.fields.setdefault(name, value)
        for ctr in item.ctr_list:
            evidence.add_containers(str(ctr), labeled=True)
        notes = _Evidence(fields=item_fields)
        for field_name in ("subtotal_amount", "tax_amount", "total_amount"):
            value = item_fields.get(field_name)
            if isinstance(value, str) and (money := _MONEY.fullmatch(value.strip())):
                item_fields[field_name] = money["amount"]
                if token := money["prefix"] or money["suffix"]:
                    item_fields.setdefault("total_currency", token)
        for value in (item.other, extra):
            if isinstance(value, Mapping):
                descriptions = [f"{key.rstrip(':： ')}: {entry}" for key, entry in value.items()]
            elif isinstance(value, str):
                descriptions = value.splitlines()
            elif isinstance(value, list):
                descriptions = [str(entry) for entry in value]
            else:
                descriptions = []
            for text in descriptions:
                if reason := _non_billing_reason(text):
                    evidence.add_clause(text, reason, page)
                else:
                    _record_summary(text, notes)
        evidence.issues.extend(issue for issue in notes.issues if issue not in evidence.issues)
        item_currency = item_fields.get("total_currency")
        currency = normalize_currency(item_currency)[0] if isinstance(item_currency, str) else None
        if currency and "total_amount" in item_fields:
            currency_totals.setdefault(currency, item_fields["total_amount"])
        for name, value in item_fields.items():
            # Different printed currencies can each carry their own total.
            if name == "total_amount" and item_currency != evidence.fields.get("total_currency"):
                evidence.fields.setdefault(name, value)
            else:
                _remember_ocr_or_printed_value(evidence, name, value)
        for row in item.charge_list:
            fields = _fields_for_keys(row, _COLUMN_KEYS)
            name = fields.get("raw_charge_name")
            if not isinstance(name, str) or not name.strip():
                continue
            text = str(row.get("sourceText") or row.get("source_text") or name)
            if reason := _non_billing_reason(f"{name} {text}"):
                evidence.add_clause(text, reason, page)
                continue
            if _SUMMARY.match(name):
                _record_summary(
                    f"{name} {fields.get('currency', '')} {fields.get('amount', '')}", evidence
                )
                continue
            if item_currency is not None:
                fields.setdefault("currency", item_currency)
            if item_term is not None:
                fields.setdefault("payment_term", item_term)
            evidence.charges.append(
                _charge_from_columns(fields, text, len(evidence.charges) + 1, page)
            )
    if len(currency_totals) > 1:
        evidence.fields.setdefault("totals_by_currency", currency_totals)


def _split_ocr_rows(lines: Sequence[_Line], evidence: _Evidence, skipped: set[int]) -> set[int]:
    """Read vertically split rate/amount columns from an OCR invoice."""
    header = next(
        (index for index, line in enumerate(lines) if "R.O.E." in line.text.upper()), None
    )
    if header is None:
        return set()
    end = len(lines)
    if end >= header + 3 and re.fullmatch(_CURRENCY, lines[-2].text, re.I):
        if re.fullmatch(_NUMBER, lines[-1].text, re.I):
            evidence.fields.setdefault("total_amount", lines[-1].text)
            evidence.fields.setdefault("total_currency", lines[-2].text)
            end -= 2
    starts = [
        index
        for index in range(header + 1, end)
        if lines[index].number not in skipped
        and _FEE.search(lines[index].text)
        and not _SUMMARY.match(lines[index].text)
        and not re.search(r"\d", lines[index].text)
    ]
    consumed = set()
    for start, stop in zip(starts, [*starts[1:], end], strict=False):
        description = lines[start]
        detail = lines[start + 1 : stop]
        numbers = [
            money
            for line in detail
            if line.page == description.page
            and line.number not in skipped
            and (money := _MONEY.fullmatch(line.text))
        ]
        if not numbers:
            continue
        currencies = [
            line.text
            for line in detail
            if re.fullmatch(_CURRENCY, line.text, re.I) and line.text.upper() == line.text
        ]
        currency = numbers[-1]["suffix"] or numbers[0]["suffix"]
        term = next((line.text for line in detail if normalize_payment_term(line.text)), None)
        evidence.charges.append(
            {
                "raw_charge_name": description.text,
                "amount": numbers[-1]["amount"],
                "unit_price": numbers[0]["amount"] if len(numbers) > 1 else None,
                "currency": currency
                or next(iter(currencies), evidence.fields.get("total_currency")),
                "payment_term": term,
                "source_text": "\n".join(line.text for line in lines[start:stop]),
                "line_number": description.number,
                "page": description.page,
            }
        )
        consumed.update(line.number for line in lines[start:stop])
    return consumed


def _collect_text_charges(
    doc: ParsedDocument, lines: Sequence[_Line], evidence: _Evidence, skipped: set[int]
) -> None:
    consumed = _split_ocr_rows(lines, evidence, skipped)
    change_term = None
    change_charge = None
    in_ledger = False
    for line in lines:
        text = line.text
        if (
            line.number in skipped
            or line.number in consumed
            or _INSTRUCTION.search(text)
            or _record_summary(text, evidence)
        ):
            continue
        if re.search(r"\b(?:DESCRIPTION|CHARGE)\b.*\bQTY\b.*\bDEBIT\b", text, re.I):
            in_ledger = True
            continue
        if payment := _PAYMENT_INSTRUCTION.fullmatch(text):
            money = _MONEY.fullmatch(payment["money"])
            if money and _FEE.search(payment["name"]):
                evidence.fields.setdefault("payer_hint", payment["payer"].strip())
                evidence.fields.setdefault("payee_hint", payment["payee"].strip())
                evidence.fields.setdefault("document_type", "EMAIL SETTLEMENT INSTRUCTION")
                fields = {
                    "raw_charge_name": "Handling Charge"
                    if _key(payment["name"]) == "hc"
                    else payment["name"],
                    "amount": money["amount"],
                    "currency": money["prefix"] or money["suffix"],
                }
                evidence.charges.append(_charge_from_columns(fields, text, line.number, line.page))
                continue
        if waybill := re.match(
            rf"^(?:[A-Z]{{4}}\s+({_REF})\s+)?Waybill\s+({_REF})\s+(.+)$", text, re.I
        ):
            if row := _TEXT_ROW.fullmatch(waybill[3]):
                evidence.fields.setdefault("reference_no", waybill[2].upper())
                if waybill[1]:
                    evidence.add_mbl(waybill[1], doc.sanitized_filename)
                text = waybill[3]
        if in_ledger and (ledger := _LEDGER_ROW.fullmatch(text)):
            fields = {key: ledger[key] for key in ("quantity", "unit_price", "payment_term")}
            fields.update(
                raw_charge_name=ledger["name"], debit=ledger["amount"], credit=ledger["credit"]
            )
            evidence.charges.append(_charge_from_columns(fields, line.text, line.number, line.page))
        elif rate_row := _RATE_ROW.fullmatch(text):
            fields = {
                "raw_charge_name": rate_row["name"],
                "unit_price": rate_row["rate"],
                "amount": rate_row["amount"],
                "currency": rate_row["currency"] or rate_row["rate_currency"],
                "payment_term": rate_row["term"],
            }
            evidence.charges.append(_charge_from_columns(fields, line.text, line.number, line.page))
        elif (row := _TEXT_ROW.fullmatch(text)) and (
            _FEE.search(row["name"]) and not _TRANSPORT_LABEL.search(row["name"])
        ):
            fields = {
                "raw_charge_name": row["name"],
                "amount": row["amount"],
                "currency": row["prefix"] or row["suffix"],
                "payment_term": row["term"],
            }
            evidence.charges.append(_charge_from_columns(fields, line.text, line.number, line.page))
        if change := re.search(
            rf"\bchange\s+(?:the\s+)?payment\s+terms?\s+to\s+({_TERM})\b", text, re.I
        ):
            change_term = change[1]
        if name := re.match(r"^(?:charge(?: item| name)?|fee|description)\s*:\s*(.+)$", text, re.I):
            change_charge = name[1]
        elif _FEE.fullmatch(text) or (
            _FEE.search(text)
            and len(text) <= 80
            and re.fullmatch(r"[^\W\d_][\w /'-]*", text)
            and not _TRANSPORT_LABEL.search(text)
        ):
            change_charge = text
    if (
        change_term
        and not evidence.charges
        and (change_charge or evidence.fields.get("reference_no"))
    ):
        evidence.charges.append(
            {"raw_charge_name": change_charge or "", "payment_term": change_term}
        )


def _extract_local(doc: ParsedDocument) -> RawExtractionEnvelope:
    if doc.status in (DocumentStatus.FAILED, DocumentStatus.SKIPPED_NOISE):
        return RawExtractionEnvelope(extraction=RawDocumentExtraction())
    empty_email = (
        doc.source_kind is SourceKind.EML
        and not doc.raw_text.strip()
        and not doc.tables
        and doc.ocr_payload is None
    )
    if empty_email:
        return RawExtractionEnvelope(extraction=RawDocumentExtraction(), metadata_only=True)

    evidence = _Evidence()
    _collect_ocr(doc, evidence)
    lines = _text_lines(doc.raw_text) if doc.ocr_payload is None else []
    title = _collect_text_headers(doc, lines, evidence)
    is_transport = _collect_transport(doc, lines, evidence, bool(title))
    skipped = _capture_clauses(lines, evidence)
    is_office = doc.source_kind in (SourceKind.EXCEL, SourceKind.WORD)
    if not evidence.charges and is_office:
        _collect_tables(doc, evidence)
    if not evidence.charges:
        _collect_text_charges(doc, lines, evidence, skipped)
    else:
        for line in lines:
            if line.number not in skipped:
                _record_summary(line.text, evidence)
    if not evidence.charges and doc.tables and not is_office:
        _collect_tables(doc, evidence)

    ocr_metadata_only = (
        doc.ocr_payload is not None
        and doc.ocr_payload.has_metadata
        and not doc.ocr_payload.has_charges
    )
    metadata_only = doc.status is DocumentStatus.METADATA_ONLY or (
        not evidence.charges
        and "total_amount" not in evidence.fields
        and (is_transport or ocr_metadata_only)
    )
    currency = evidence.fields.get("total_currency")
    us_context = _US_CONTEXT.search(doc.raw_text)
    if currency == "$" and us_context:
        currency = evidence.fields["total_currency"] = "USD"
    for charge in evidence.charges:
        if charge.get("currency") is None and currency:
            charge["currency"] = currency
        if charge.get("currency") == "$" and us_context and currency in (None, "USD"):
            charge["currency"] = "USD"

    refs = ShipmentRefs(
        mbl_nos=evidence.mbl_nos,
        hbl_no=evidence.fields.pop("hbl_no", None),
        booking_no=evidence.fields.pop("booking_no", None),
        container_nos=list(dict.fromkeys(evidence.container_nos)),
    )
    result = parse_llm_raw_extraction(
        dumps_decimal_json(
            {
                **evidence.fields,
                "shipment_refs": refs.model_dump(),
                "excluded_clauses": [clause.model_dump() for clause in evidence.clauses],
                "charges": [] if metadata_only else evidence.charges,
                "metadata_only": metadata_only,
            }
        )
    )
    result.raw_parse_issues.extend(evidence.issues)
    return result


def _package_parties(
    siblings: Sequence[tuple[ParsedDocument, RawExtractionEnvelope]],
) -> list[tuple[str, str]]:
    parties = []
    for doc, envelope in siblings:
        for name in (envelope.extraction.issuer, envelope.extraction.recipient):
            if name:
                parties.append((name, doc.sanitized_filename))
        if not envelope.metadata_only:
            continue
        lines = _text_lines(doc.raw_text)
        for current, following in zip(lines, lines[1:], strict=False):
            if "FOR CARGO ARRIVAL" in current.text.upper() and current.page == following.page:
                first = _first_company(following.text)
                second = following.text[len(first) :].strip()
                if second:
                    parties.append((_first_company(second), doc.sanitized_filename))
    return parties


def _resolve_short_party(
    name: str, candidates: Sequence[tuple[str, str]], profiles: Sequence[AccountingEntityProfile]
) -> tuple[str, str] | None:
    key = normalize_party_name(name)
    aliases = {
        normalize_party_name(profile.canonical_name)
        for profile in profiles
        if any(key == normalize_party_name(alias) for alias in profile.aliases)
    }
    if not key or len(aliases) > 1:
        return None
    matches = []
    for candidate, source in candidates:
        full_name = normalize_party_name(candidate)
        if (
            full_name in aliases
            or (
                key == "sha"
                and (full_name == "sun track express" or full_name.startswith("sun track express "))
            )
            or (
                key.isascii()
                and key.isalpha()
                and 3 <= len(key) <= 5
                and full_name.replace(" ", "").startswith(key)
                and " " in full_name
            )
        ):
            matches.append((candidate, source))
    # The same party can have different OCR word breaks in the MBL and HBL.
    if len({normalize_party_name(candidate).replace(" ", "") for candidate, _ in matches}) == 1:
        return min(matches, key=lambda match: (len(match[0]), match))
    return None


def _resolve_package_dollars(
    envelope: RawExtractionEnvelope,
    siblings: Sequence[tuple[ParsedDocument, RawExtractionEnvelope]],
) -> list[RawChargeEnvelope]:
    if not any(row.raw_invalid_fields.get("currency") == "$" for row in envelope.charges):
        return envelope.charges
    source_envelopes = [envelope, *(sibling for _, sibling in siblings)]
    known_currencies = {
        currency
        for source in source_envelopes
        for currency in (
            source.total_currency,
            *source.totals_by_currency,
            *(row.charge.currency for row in source.charges),
        )
        if currency
    }
    currency, reason = normalize_currency(
        "$",
        context_currencies=known_currencies,
        us_shipping_context=any(_US_CONTEXT.search(doc.raw_text) for doc, _ in siblings),
    )
    if currency is None:
        return envelope.charges
    resolved = []
    for row in envelope.charges:
        if row.raw_invalid_fields.get("currency") != "$":
            resolved.append(row)
            continue
        invalid = row.raw_invalid_fields.copy()
        invalid.pop("currency")
        issues = [
            issue
            for issue in row.raw_parse_issues
            if not (
                issue.code is AlertCode.INVALID_OR_MISSING_CURRENCY and issue.field == "currency"
            )
        ]
        issues.append(
            ValidationAlert.from_code(
                AlertCode.CONTEXT_INFERRED_FIELD,
                reason or "币种补自同封邮件的运输上下文",
                field="currency",
                expected=currency,
                actual="$",
            )
        )
        resolved.append(
            row.model_copy(
                update={
                    "charge": row.charge.model_copy(update={"currency": currency}),
                    "raw_invalid_fields": invalid,
                    "raw_parse_issues": issues,
                }
            )
        )
    return resolved


def apply_sibling_context(
    envelope: RawExtractionEnvelope,
    siblings: Sequence[tuple[ParsedDocument, RawExtractionEnvelope]],
    profiles: Sequence[AccountingEntityProfile],
) -> tuple[RawExtractionEnvelope, list[str]]:
    """Fill transport references and resolve payment shorthand using sibling evidence."""
    if not siblings:
        return envelope, []
    extracted = envelope.extraction
    own = extracted.shipment_refs
    refs = [candidate.extraction.shipment_refs for _, candidate in siblings]
    updates: dict[str, Any] = {}
    reasoning = []
    # Keep each source's own MBLs in the bundle for the batch conflict validator.
    mbl = list(
        {(ref.value, ref.source): ref for item in (own, *refs) for ref in item.mbl_nos}.values()
    )
    containers = list(dict.fromkeys(value for item in (own, *refs) for value in item.container_nos))
    ref_updates: dict[str, Any] = {"mbl_nos": mbl, "container_nos": containers}
    for field_name in ("hbl_no", "booking_no"):
        values = {value for item in refs if (value := getattr(item, field_name))}
        if not getattr(own, field_name) and len(values) == 1:
            ref_updates[field_name] = next(iter(values))
    merged = own.model_copy(update=ref_updates)
    if merged != own:
        updates["shipment_refs"] = merged
        reasoning.append("运输参考号补自同封邮件的正文或附件")
    candidates = _package_parties(siblings)
    for field_name, role in (("payer_hint", "付款方"), ("payee_hint", "收款方")):
        if (short := getattr(extracted, field_name)) and (
            resolved := _resolve_short_party(short, candidates, profiles)
        ):
            name, source = resolved
            updates[field_name] = name
            reasoning.append(f"{role} {short} 对应同封邮件 {source} 中的 {name}")
    charges = _resolve_package_dollars(envelope, siblings)
    if charges is not envelope.charges:
        updates["charges"] = [row.charge for row in charges]
    if not updates:
        return envelope, reasoning
    return envelope.model_copy(
        update={
            "extraction": extracted.model_copy(update=updates),
            "charges": charges,
            "explicit_fields": envelope.explicit_fields | updates.keys(),
        }
    ), reasoning


def extract_document_by_rules(
    doc: ParsedDocument,
    sibling_docs: Sequence[ParsedDocument] = (),
    profiles: Sequence[AccountingEntityProfile] = (),
) -> RawExtractionEnvelope:
    """Extract explicit source values and optionally attach shared shipment references."""
    envelope = _extract_local(doc)
    if doc.status in (DocumentStatus.FAILED, DocumentStatus.SKIPPED_NOISE):
        return envelope
    siblings = [
        (sibling, _extract_local(sibling))
        for sibling in sorted(sibling_docs, key=lambda item: (item.sanitized_filename, item.doc_id))
        if sibling.doc_id != doc.doc_id
        and sibling.status not in (DocumentStatus.FAILED, DocumentStatus.SKIPPED_NOISE)
    ]
    return apply_sibling_context(envelope, siblings, profiles)[0]
