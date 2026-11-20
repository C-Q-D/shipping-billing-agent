"""Read billing evidence without a model or access to the source files."""

from __future__ import annotations

import re
from bisect import bisect_left
from collections import defaultdict
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any
from unicodedata import normalize

from shipping_billing_agent.domain import (
    AccountingEntityProfile,
    AlertCode,
    ExcludedClause,
    ParsedDocument,
    PaymentTerm,
    RawChargeEnvelope,
    RawDocumentExtraction,
    RawExtractionEnvelope,
    ShipmentRefs,
    SourceRefValue,
    ValidationAlert,
    dumps_decimal_json,
    entity_matches,
    normalize_charge_item,
    normalize_currency,
    normalize_party_name,
    normalize_payment_term,
    parse_decimal_strict,
    parse_llm_raw_extraction,
)
from shipping_billing_agent.domain.currency import ACTIVE_ISO4217_CODES
from shipping_billing_agent.domain.entity_resolver import (
    CREDIT_DOCUMENT_PATTERN,
    PRINTED_CURRENCY_CONFLICT_MESSAGE,
    is_credit_document_type,
)
from shipping_billing_agent.domain.models import (
    DocumentStatus,
    ParsedTable,
    SourceKind,
    sanitize_source_path,
)
from shipping_billing_agent.domain.validators import _printed_currency_token
from shipping_billing_agent.gateways.text_sanitizer import sanitize_for_llm

_PAGE = re.compile(r"^\[Page (\d+)(?:\s*\|[^]]+)?\]$", re.I)
_SHEET_PREFIX = re.compile(r"^\[Sheet: (?P<sheet>.*?) \| Row \d+\]\s*")
_DATE = (
    r"(?:[0-9OILS]{4}[-/][0-9OILS]{1,2}[-/][0-9OILS]{1,2}"
    r"|[0-9OILS]{1,2}[-/][0-9OILS]{1,2}[-/][0-9OILS]{4})"
)
_NUMBER = r"[-+]?(?=[0-9OILS,.]*[0-9])[0-9OILS]+(?:[.,][0-9OILS]+)*(?:[Ee][-+]?[0-9]+)?"
_CURRENCY_CODES = "|".join(sorted(ACTIVE_ISO4217_CODES))
_CURRENCY_SYMBOL = r"(?:US\$|CA\$|AU\$|HK\$|SG\$|CN¥|[$€£¥])"
_CURRENCY = rf"(?:{_CURRENCY_SYMBOL}|RMB|{_CURRENCY_CODES})"
_CURRENCY_SLOT = rf"(?:{_CURRENCY}|(?-i:[A-Z]{{3}}))"
_NON_CURRENCY_WORDS = (
    "THC|DOC|BAF|CAF|AMS|ISF|ENS|VGM|LCL|FCL|CFS|FEE|HBL|MBL|NET|TAX|"
    "CBM|CTN|DAY|KGS|PCS|PKG|TON|FOR|PER|QTY|INV|REF"
)
# Preserve unknown uppercase currency codes without interpreting fee abbreviations as codes.
_DETAIL_CURRENCY_SLOT = rf"(?:{_CURRENCY}|(?-i:(?!(?:{_NON_CURRENCY_WORDS}))[A-Z]{{3}}))"
_TERM = r"(?:PP|CC|P|C|PREPAID|COLLECT)"
_PARENTHESIZED_TERMS = rf"\(\s*{_TERM}(?:\s*[,/]\s*{_TERM})*\s*\)"
_TERM_TAG = rf"(?:{_PARENTHESIZED_TERMS}|{_TERM})"
_TERM_SEPARATOR = r"(?:\s*[,/]\s*|\s+(?:and|or)\s+|\s+)"
_TERM_LIST = rf"{_TERM_TAG}(?:(?:{_TERM_SEPARATOR}|(?=\()){_TERM_TAG})*"
_TERM_TOKEN = re.compile(rf"\b{_TERM}\b", re.I)
_END_TERM = rf"(?:{_TERM_SEPARATOR}{_TERM_TAG}|\s*{_PARENTHESIZED_TERMS})"
_ROW_PUNCTUATION = r"\s*[.,;]*"
_REF = r"(?=[A-Z0-9./-]*[0-9])[A-Z0-9][A-Z0-9./-]*"
_BOX = re.compile(r"\b[A-Z]{3}[UJZ][0-9]{7}\b", re.I)
_BOX_LABEL = re.compile(
    r"\b(?:CONTAINERS?\b(?:\(S\))?|CNTR\b|CTNR\b)\.?\s*"
    r"(?:(?:NO\b\.?|NUMBER\b)\s*)?[:#=]?\s*",
    re.I,
)
_LABELED_BOX = re.compile(r"\b(?=[A-Z0-9]{0,10}[A-Z])(?=[A-Z0-9]{0,10}[0-9])[A-Z0-9]{11}\b", re.I)
_COMPANY = re.compile(
    r"^.+?\b(?:CO[., ]+LTD|INCORPORATED|CORPORATION|LIMITED|LTD|INC|LLC|GMBH)\b[.,]*"
    r"(?: +(?:[^\W\d_][\w'-]* +)*Branch\b[.,]*)?(?:[\s,./-]*\([^()]*\)[.,]*)*",
    re.I,
)
_BILL_TITLE = re.compile(
    rf"^(?:{CREDIT_DOCUMENT_PATTERN.pattern}|DEBIT\s*(?:NOTE|INVOICE)|"
    r"(?:TAX\s+|CREDIT\s+CARD\s+)?INVOICE|LETTER\s+OF\s+CREDIT|GENERAL\s+NOTICE)"
    r"(?=\s|[:#=]|$)",
    re.I,
)
_TITLE_REFERENCE = re.compile(
    rf"^\s*(?:(?:number|no\.?|id)(?=\W|$)\s*)?[:#=]?\s*({_REF})(?=\s|$)", re.I
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
_NON_BILLING_NOTE = re.compile(
    r"\b(?:(?:quotation|quote|estimate)\s+only|for\s+quotation|"
    r"already\s+(?:been\s+)?included|included\s+in|free\s+of\s+charge|no\s+charge|waived|"
    r"partial\s+waiver|"
    r"for\s+(?:your\s+)?(?:reference|information)\s+only|non[-\s]billable|not\s+payable|"
    r"excluded|not\s+billed)\b|"
    r"[仅僅][供作](?:[报報][价價]|[参參]考|估[价價]|[资資][讯訊料]|信息)|估[价價]|"
    r"已[经經]?(?:包含|含|[计計]入)|免收|[减減][免半]|[无無]需(?:支付|付款)|不[计計][费費]|不收[费費]",
    re.I,
)
_NOTE_NEGATION = (
    r"(?:\b(?:not|never|cannot|no\s+longer|"
    r"(?:is|are|was|were|has|have|had|do|does|did|ca|wo|would|should|could|must)n['’]t)\s+"
    r"(?:(?:yet|currently|actually|still|already|be|been|being)\s+)*(?:a\s+)?"
    r"|(?:不[予再可曾会會能得是]?|[并並][非未]|未曾?|[没沒]有|[无無]法)\s*)"
)
_NEGATED_NOTE_PREFIX = re.compile(rf"{_NOTE_NEGATION}$", re.I)
_PARTIAL_NOTE = re.compile(
    r"\d+(?:\.\d+)?\s*%|\b(?:half|partial(?:ly)?|in\s+part)\b|部分|[减減][免半]", re.I
)
_DEMONSTRATIVE_FEE = re.compile(
    r"^(?:this|that|these|those|the\s+(?:above|preceding|previous|listed))\s+"
    r"(?:charge|fee|item|line|amount)s?\b",
    re.I,
)
_PLURAL_FEE_REFERENCE = re.compile(
    r"^(?:(?:remarks?|notes?)\s*[:：]\s*|(?:备注|備註|说明|說明)\s*[:：]?\s*)?"
    r"(?:(?:(?P<both>both)\s+(?:(?:of\s+)?(?:these|those|the(?:\s+above)?)\s+)?|"
    r"(?P<all>all)\s+(?:(?:of\s+)?(?:these|those|the)\s+)?(?:above\s+)?|"
    r"(?:these|those|the\s+(?:above|preceding|previous|listed))\s+(?:(?P<count>[\w-]+)\s+)?)"
    r"(?:charges|fees|items|lines|amounts)\b|"
    r"(?:[这這那]|上述|以上|前述)\s*(?:(?P<cn_count>[\d一二两兩三四五六七八九十]+)\s*[项項笔筆]"
    r"(?:\s*(?:费用|費用|款项|款項))?|(?P<cn_all>所有|全部)\s*(?:费用|費用|款项|款項))\s*均?)",
    re.I,
)
_FEE_COUNT_WORDS = {
    word: count
    for count, word in enumerate(
        "one two three four five six seven eight nine ten eleven twelve thirteen fourteen "
        "fifteen sixteen seventeen eighteen nineteen twenty".split(),
        1,
    )
}
_CHINESE_FEE_DIGITS = dict(zip("一二三四五六七八九", range(1, 10), strict=True))
_PLURAL_NON_BILLING_NOTE = re.compile(rf"{_NON_BILLING_NOTE.pattern}|\bincluded\b", re.I)
_NOTE_VERB = re.compile(r"\b(?:is|are|was|were|has|have)\b", re.I)
_NEGATED_NOTE = rf"(?:{_NOTE_VERB.pattern}\s+)?{_NOTE_NEGATION}(?:{_NON_BILLING_NOTE.pattern})"
_NON_BILLING_CONTINUATION = re.compile(rf"^{_NOTE_VERB.pattern}|^\(\s*waived\s*\)", re.I)
_NOTE_REASON = "报价、已含、免收或其他非记账说明"
_AMBIGUOUS_NOTE_SCOPE = "无法确定非记账说明对应的费用范围，待人工复核"
_PARTIAL_NOTE_REASON = "无法确定部分减免后的应收金额，待人工复核"
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
_SUMMARY_CURRENCY_PREFIX = re.compile(
    rf"^(?:\(\s*({_CURRENCY_SLOT})\s*\)\s*[:=]?|({_CURRENCY_SLOT})\s*[:=])\s*", re.I
)
_SUMMARY_STATED_CURRENCY = re.compile(
    rf"\b(?:currency|curr|cur)\b\s*[:=]?\s*({_CURRENCY_SLOT})(?!\w)", re.I
)
_INLINE_TOTAL = re.compile(r"\s+(?P<summary>total\s*(?:\([^()]*\)\s*)?[:=].*)$", re.I)
_DOLLAR_CURRENCIES = {
    "USD",
    "AUD",
    "CAD",
    "HKD",
    "SGD",
    "NZD",
    "TWD",
    "BBD",
    "BMD",
    "BND",
    "BSD",
    "BZD",
    "FJD",
    "GYD",
    "JMD",
    "KYD",
    "LRD",
    "NAD",
    "SBD",
    "SRD",
    "TTD",
    "XCD",
}
_TRANSPORT_TOTAL = re.compile(
    r"^(?:say\b|[a-z]+(?:[ -][a-z]+)*\s+\(\d+\)|(?:no\.?\s+of\s+|number\s+of\s+)?"
    r"(?:containers?|packages?|cartons?|pieces|pallets?|gross weight|net weight|measurement)\b)",
    re.I,
)
_MONEY = re.compile(
    rf"(?:(?P<prefix>{_CURRENCY_SLOT})\s*)?(?P<amount>{_NUMBER})"
    rf"(?:\s*(?P<suffix>{_CURRENCY_SLOT})(?!\w))?",
    re.I,
)
_DETAIL_MONEY = re.compile(
    rf"(?<![\w.,/+-])(?:(?P<prefix>{_DETAIL_CURRENCY_SLOT})\s*)?"
    rf"(?P<amount>{_NUMBER}|[-+]?\.[0-9]+)"
    rf"(?:\s*(?P<suffix>{_DETAIL_CURRENCY_SLOT})(?!\w))?"
    r"(?![\w+-]|[.,][0-9]|/(?!\s*[^\W\d_]))",
    re.I,
)
_DETAIL_CURRENCY = re.compile(rf"(?<![^\W\d_]){_CURRENCY}(?![^\W\d_])")
_PARENTHESIZED_CURRENCY = rf"\(\s*(?P<parenthesized_currency>{_CURRENCY_SLOT})\s*\)"
_END_CURRENCY_TAG = re.compile(rf"{_PARENTHESIZED_CURRENCY}[\s.,;)]*$", re.I)
_LINE_AMOUNT_LABEL = re.compile(r"\b(?:line\s+(?:amount|total)\b\s*[:=]?|amount\s*[:=])\s*", re.I)
_QUANTITY_MARKER = r"\b(?:qty\.?|quantity)(?!\w)\s*[:=#]?\s*"
_QUANTITY_PREFIX = re.compile(rf"{_QUANTITY_MARKER}$", re.I)
_QUANTITY_UNIT = re.compile(
    r"\s*(?:(?:containers?|cntrs?|ctns?|pkgs?|packages?|pcs?|units?|days?|tons?|cbm|kgs?)\b|[箱件天])",
    re.I,
)
_AT_RATE_MARKER = r"(?:@|\bat\b\s*[:=]?)"
_RATE_MARKER = rf"(?:{_AT_RATE_MARKER}|\b(?:rate|unit\s+price)\s*[:=]?)"
_MARKED_RATE_CURRENCY = re.compile(
    rf"{_RATE_MARKER}\s*({_CURRENCY_SLOT})\s*{_NUMBER}(?![\w.,])", re.I
)
_UNIT_PRICE_PREFIX = re.compile(rf"{_RATE_MARKER}\s*$", re.I)
_UNIT_PRICE_SUFFIX = re.compile(r"\s*(?:each\b|(?:per\s+|/\s*)[^\W\d_])", re.I)
_MULTIPLICATION_SIGN = r"(?:[×*]|(?<![^\W\d_])x)"
_MULTIPLIER_PREFIX = re.compile(rf"{_MULTIPLICATION_SIGN}\s*$", re.I)
_QUANTITY_MULTIPLIER_PREFIX = re.compile(
    rf"(?<![\w.,/+-]){_NUMBER}(?:{_QUANTITY_UNIT.pattern})?\s*{_MULTIPLICATION_SIGN}\s*$", re.I
)
_PRE_RATE_SUFFIX = re.compile(
    rf"\s*(?:{_AT_RATE_MARKER}|{_MULTIPLICATION_SIGN})\s*"
    rf"(?:{_DETAIL_CURRENCY_SLOT}\s*)?{_NUMBER}", re.I
)
_UNPARSED_RATE = re.compile(
    rf"{_QUANTITY_MARKER}|@|\b(?:rate|unit\s+price)\s*[:=]|"
    rf"(?:{_RATE_MARKER}|{_MULTIPLICATION_SIGN})\s*(?=(?:{_DETAIL_CURRENCY_SLOT}\s*)?{_NUMBER}|$)",
    re.I,
)
_SUMMARY_DUE_TO = re.compile(rf"^(?P<party>.+?)\s+(?=(?:{_CURRENCY}\s+)?{_NUMBER}(?:\s|$))", re.I)
_SUMMARY_MAPS = {
    "subtotal_amount": "subtotals_by_currency",
    "tax_amount": "taxes_by_currency",
    "total_amount": "totals_by_currency",
}
_PAYMENT_INSTRUCTION = re.compile(
    r"^(?P<payer>.+?)\s+(?:pays?|to pay)\s+(?P<payee>[^:]+):\s*"
    r"(?P<charge>.+)$",
    re.I,
)
_LEDGER_HEADER = re.compile(r"\b(?:DESCRIPTION|CHARGE)\b.*\bQTY\b.*\bDEBIT\b", re.I)
_TABLE_HEADER = re.compile(
    r"^R\.O\.E\.?$|\b(?:DESCRIPTION|CHARGE)\b.*\b(?:AMOUNT|CURRENCY|RATE|QTY|DEBIT|CREDIT)\b",
    re.I,
)
_CHARGE_LABEL = re.compile(r"^(?:charge(?: item| name)?|fee|description)\s*:\s*(.*)$", re.I)
_CHARGE_AMOUNT = re.compile(r"(?:charge\s+)?amount\s*:\s*(.+)", re.I)
_CHANGE_TERM = re.compile(
    rf"\bchange\s+(?:the\s+)?payment\s+terms?\s+to\s+(?P<terms>{_TERM_LIST})(?!\w)", re.I
)
_TERM_CONTINUATION = re.compile(
    rf"(?:(?:payment[ _-]*terms?|p/c|terms?)\s*[:=]\s*)?"
    rf"(?P<terms>{_TERM_LIST}){_ROW_PUNCTUATION}",
    re.I,
)
_CURRENCY_TERM_CELL = re.compile(
    rf"(?P<currency>{_DETAIL_CURRENCY_SLOT})(?:\s*(?P<terms>(?=\(){_TERM_LIST}))?", re.I
)
_LEDGER_ROW = re.compile(
    rf"^(?P<name>.+?)\s+[-–]\s+(?P<unit_price>{_NUMBER})\s+(?P<quantity>{_NUMBER})"
    rf"\s+(?P<payment_term>{_TERM})\s+(?P<amount>{_NUMBER})"
    rf"(?:\s+(?P<credit>{_NUMBER}))?$",
    re.I,
)
_AMOUNT_TAG = re.compile(
    rf"(?P<term>{_END_TERM})(?!\w)|(?:\s*,)?(?:\s*{_PARENTHESIZED_CURRENCY}"
    rf"|\s*\(\s*{_NEGATED_NOTE}\s*\)|\s+{_NEGATED_NOTE}"
    rf"|\s*(?P<bare_currency>(?!net\b){_CURRENCY_SLOT})(?!\w))",
    re.I,
)
# ISO codes also match the three-letter fallback. Keep completed tags during backtracking.
_AMOUNT_TAIL = re.compile(rf"(?P<tail>(?:{_AMOUNT_TAG.pattern})*+)", re.I)
_LABELED_MONEY = re.compile(
    rf"(?:(?P<prefix>{_CURRENCY_SLOT})\s*)?"
    rf"(?P<amount>{_NUMBER}|[-+]?\.[0-9]+){_AMOUNT_TAIL.pattern}{_ROW_PUNCTUATION}",
    re.I,
)
_DETAIL_AMOUNT = rf"(?:(?P<prefix>{_DETAIL_CURRENCY_SLOT})\s*)?(?P<amount>{_NUMBER})"
_TEXT_ROW = re.compile(
    rf"^(?P<name>.+?)(?:[ :\t]+|(?={_CURRENCY_SYMBOL}))"
    rf"{_DETAIL_AMOUNT}{_AMOUNT_TAIL.pattern}{_ROW_PUNCTUATION}$",
    re.I,
)
_LABELED_DETAIL_ROW = re.compile(
    rf"^(?P<name>.+?)[ :\t]+{_LINE_AMOUNT_LABEL.pattern}"
    rf"{_DETAIL_AMOUNT}{_AMOUNT_TAIL.pattern}{_ROW_PUNCTUATION}$",
    re.I,
)
_RATE_UNIT = r"(?:/\s*[^\W\d_]+|\beach\b|\bper\s+(?:container|unit|teu|feu|box|shipment|bl|day)\b)"
_RATE_MULTIPLIER = r"[x×*]\s*(?P<multiplier_qty>\d+(?:\.\d+)?)"
# Parenthesized line totals can qualify the amount as net.
_RATE_ROW = re.compile(
    rf"^(?P<name>.+?)(?:\s+(?:{_QUANTITY_MARKER}|for\s+(?={_NUMBER}{_QUANTITY_UNIT.pattern})"
    rf"|(?={_NUMBER}(?:{_QUANTITY_UNIT.pattern})?\s*{_AT_RATE_MARKER}))"
    rf"(?P<quantity>{_NUMBER})(?:{_QUANTITY_UNIT.pattern})?)?"
    rf"(?:\s*(?P<marker>{_RATE_MARKER})\s*|\s+)"
    rf"(?:(?P<rate_prefix>{_DETAIL_CURRENCY_SLOT})\s*)?(?P<rate>{_NUMBER})"
    rf"(?:\s*(?P<rate_suffix>{_DETAIL_CURRENCY_SLOT})(?!\w))?"
    rf"(?:\s*{_RATE_UNIT})?(?:\s*{_RATE_MULTIPLIER})?\s+"
    rf"(?:(?P<wrapped_total>\(\s*)?{_LINE_AMOUNT_LABEL.pattern})?"
    rf"{_DETAIL_AMOUNT}(?P<tail>(?:(?(wrapped_total)\s+net\b|(?!))|{_AMOUNT_TAG.pattern})*+)"
    rf"(?(wrapped_total)\s*\)){_ROW_PUNCTUATION}$",
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
    "issuer": ("issuer", "seller"),
    "recipient": ("recipient", "billTo"),
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
_SHORT_REFERENCE = r"\bref\.?(?:\s*(?:number|no\.?|#|id))?"
_REFERENCE_LABELS = {
    "reference_no": (
        r"(?:invoice|debit\s*note|credit\s*note|D/C|D/N|C/N|reference)\s*"
        rf"(?:number|no\.?|#|id)|(?:debit\s*)?invoice\s*:|{_SHORT_REFERENCE}"
    ),
    "hbl_no": r"(?:HBL|HOUSE\s+B/?L)(?:\s*(?:number|no\.?|#))?",
    "booking_no": r"(?:booking|bkg)(?:\s*(?:number|no\.?|#))?",
}
_CURRENCY_LABEL = re.compile(r"^currency\s*[:=]\s*([^\s:]+)$", re.I)
_PARTY_LABELS = {
    "issuer": r"issuer|issued by|bill from|seller|vendor",
    "recipient": r"recipient|bill to|sold to|customer",
    "payer_hint": r"payer|paid by",
    "payee_hint": r"payee|paid to|balance due to",
}
_PARTY_HEADER = re.compile(rf"^(?:{'|'.join(_PARTY_LABELS.values())}|from|to)\s*[:=]", re.I)
_INLINE_REFERENCE = re.compile(
    rf"\s+(?:{_REFERENCE_LABELS['reference_no']}|reference)\s*[:=]\s*({_REF})(?=\W|$)", re.I
)
_PARTY_VALUE_TAIL = re.compile(
    rf"\t+|(?:^|\s+)(?:reference(?:\s+(?:number|no\.?|id|#))?|{_SHORT_REFERENCE}|from|to|"
    r"(?:invoice|debit note|credit note)"
    r"\s+(?:number|no\.?|id|#)|(?:(?:invoice|document|issue|D/C|D/N|C/N)\s+)?date|currency|"
    rf"hbl(?: no\.?)?|mbl(?: no\.?)?|booking(?: no\.?)?|{'|'.join(_PARTY_LABELS.values())})"
    r"\s*[:=]",
    re.I,
)
_TRANSPORT_PARTY_LABELS = {
    "shipper": r"shipper(?:/exporter)?(?:\s+booking number.*|\s+document no.*)?",
    "consignee": r"consignee(?:\([^)]*\))?(?:\s+forwarding agent.*)?",
}


@dataclass(frozen=True, slots=True)
class _Line:
    text: str
    number: int
    page: int | None = None
    sheet: str | None = None


@dataclass(frozen=True, slots=True)
class _ChargeEvidence:
    text: str
    amount: Decimal
    currencies: frozenset[str]
    payment_terms: tuple[PaymentTerm, ...]
    is_line_amount: bool


@dataclass(slots=True)
class _Evidence:
    fields: dict[str, Any] = field(default_factory=dict)
    charges: list[dict[str, Any]] = field(default_factory=list)
    mbl_nos: list[SourceRefValue] = field(default_factory=list)
    container_nos: list[str] = field(default_factory=list)
    clauses: list[ExcludedClause] = field(default_factory=list)
    issues: list[ValidationAlert] = field(default_factory=list)
    charge_invalid_fields: dict[int, dict[str, str]] = field(default_factory=dict)
    charge_issues: dict[int, list[ValidationAlert]] = field(default_factory=dict)
    summaries: list[tuple[str, Any, str | None]] = field(default_factory=list)
    currencies: set[str] = field(default_factory=set)

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
        if reason in (_AMBIGUOUS_NOTE_SCOPE, _PARTIAL_NOTE_REASON):
            issue = ValidationAlert.from_code(
                AlertCode.LLM_EXTRACTION_FAILED,
                reason,
                field="excluded_clauses",
                actual=clause.text,
            )
            if issue not in self.issues:
                self.issues.append(issue)

    def add_charge(
        self,
        row: dict[str, Any],
        *currency_tokens: str | None,
        payment_terms: Sequence[PaymentTerm] = (),
        parse_issues: Sequence[ValidationAlert | None] = (),
        invalid_fields: Mapping[str, str] | None = None,
    ) -> None:
        invalid = dict(invalid_fields or {})
        issues = [issue for issue in parse_issues if issue is not None]
        tokens = [token for token in currency_tokens if token]
        if tokens:
            currency, conflicts = _printed_currency(tokens)
            row["currency"] = currency if not conflicts else None
            if conflicts:
                currency_issues = [
                    ValidationAlert.from_code(
                        AlertCode.CROSS_DOCUMENT_CONFLICT,
                        PRINTED_CURRENCY_CONFLICT_MESSAGE,
                        field="currency",
                        expected=currency,
                        actual=conflict,
                    )
                    for conflict in conflicts
                ]
                invalid["currency"] = " / ".join(dict.fromkeys(tokens))
                issues.extend(currency_issues)
                self.issues.extend(issue for issue in currency_issues if issue not in self.issues)
        terms = list(dict.fromkeys(payment_terms))
        if terms:
            row["payment_term"] = terms[0] if len(terms) == 1 else None
            if len(terms) > 1:
                raw = " / ".join(terms)
                invalid["payment_term"] = raw
                issues.append(_printed_payment_term_conflict(raw))
        if invalid:
            self.charge_invalid_fields[len(self.charges)] = invalid
        if issues:
            self.charge_issues[len(self.charges)] = issues
        self.charges.append(row)


def _text_lines(text: str) -> list[_Line]:
    lines = []
    page = None
    for number, line in enumerate(text.splitlines(), 1):
        text = normalize("NFKC", line).strip()
        sheet = _SHEET_PREFIX.match(text)
        if sheet:
            text = text[sheet.end() :]
        if match := _PAGE.fullmatch(text):
            page = int(match[1])
        elif text:
            lines.append(_Line(text, number, page, sheet["sheet"] if sheet else None))
    return lines


def _source_lines(doc: ParsedDocument) -> list[_Line]:
    lines = _text_lines(doc.raw_text)
    if doc.ocr_payload is not None:
        # The gateway also renders each structured item as one JSON line.
        return [line for line in lines if not line.text.startswith("{")]
    return lines


def _table_location(table: ParsedTable) -> tuple[int | None, str | None]:
    source = normalize("NFKC", table.source or "")
    page = _PAGE.fullmatch(source)
    sheet = _SHEET_PREFIX.fullmatch(source)
    return (int(page[1]) if page else None, sheet["sheet"] if sheet else None)


def _table_lines(table: ParsedTable) -> list[_Line]:
    page, sheet = _table_location(table)
    return [
        _Line(text, number, page, sheet)
        for number, row in enumerate(table.rows, 1)
        if (text := normalize("NFKC", "\t".join(" ".join(cell.split()) for cell in row)).strip())
    ]


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


def _non_billing_note(text: str) -> re.Match[str] | None:
    pattern = _PLURAL_NON_BILLING_NOTE if _PLURAL_FEE_REFERENCE.match(text) else _NON_BILLING_NOTE
    for note in pattern.finditer(text):
        if not _NEGATED_NOTE_PREFIX.search(text, 0, note.start()):
            return note
    return None


def _non_billing_reason(text: str) -> str | None:
    if _REMITTANCE.search(text):
        return _BANK_REASON
    if re.search(r"\bPAID\s+AMOUNT\b|\bAMOUNT\s+PAID\b", text, re.I):
        return "已付金额"
    if re.search(r"\bCARRIER\s+FREE\s+H\s*/?\s*C\b", text, re.I):
        return "承运方免收操作费"
    if _non_billing_note(text):
        return _PARTIAL_NOTE_REASON if _PARTIAL_NOTE.search(text) else _NOTE_REASON
    if _CONDITIONAL.search(text):
        return _CONDITIONAL_REASON
    return None


def _summary_line(text: str) -> bool:
    return any(_SUMMARY.match(cell.strip()) for cell in text.split("\t"))


def _billing_heading(text: str) -> bool:
    return bool(
        _summary_line(text)
        or _BILL_TITLE.match(text)
        or _PARTY_HEADER.match(text)
        or _TRANSPORT_LABEL.search(text)
        or (_TABLE_HEADER.search(text) and not _DETAIL_MONEY.search(text))
    )


def _adjacent(previous: _Line, following: _Line) -> bool:
    return (
        previous.page == following.page
        and previous.sheet == following.sheet
        and previous.number + 1 == following.number
    )


def _payment_terms(text: str) -> list[PaymentTerm]:
    return [
        term
        for match in _TERM_TOKEN.finditer(text)
        if (term := normalize_payment_term(match[0])) is not None
    ]


def _printed_payment_term_conflict(terms: str) -> ValidationAlert:
    return ValidationAlert.from_code(
        AlertCode.CROSS_DOCUMENT_CONFLICT,
        "费用明细行标明了相互矛盾的付款条款",
        field="payment_term",
        actual=terms,
    )


def _payment_term_continuation(text: str) -> list[PaymentTerm]:
    match = _CHANGE_TERM.search(text) or _TERM_CONTINUATION.fullmatch(text)
    return _payment_terms(match["terms"]) if match else []


def _charge_attribute(text: str) -> bool:
    return not _CHARGE_LABEL.fullmatch(text) and bool(
        _CHARGE_AMOUNT.fullmatch(text) or _payment_term_continuation(text)
    )


def _note_names_fee(text: str) -> bool:
    return bool(
        not (_NON_BILLING_CONTINUATION.match(text) or _DEMONSTRATIVE_FEE.match(text))
        and _FEE.search(_NON_BILLING_NOTE.sub("", text))
    )


def _wrapped_clause(lines: Sequence[_Line], index: int) -> tuple[int, str | None]:
    text = lines[index].text
    reason = _non_billing_reason(text)
    if reason or _billing_heading(text):
        return index + 1, reason
    for end in range(index + 1, min(index + 3, len(lines))):
        following = lines[end].text
        following_reason = _non_billing_reason(following)
        if (
            not _adjacent(lines[end - 1], lines[end])
            or _billing_heading(following)
            or _CHARGE_LABEL.fullmatch(following)
            or _DEMONSTRATIVE_FEE.match(following)
            or _PLURAL_FEE_REFERENCE.match(following)
            or following_reason not in (None, _NOTE_REASON)
            or (_FEE.search(following) and _DETAIL_MONEY.search(following))
        ):
            break
        joined = f"{text} {following}"
        if _note_names_fee(following):
            note = _non_billing_note(joined)
            if note is None or note.start() >= len(text):
                break
        if following_reason and end == index + 1 and not (
            _DEMONSTRATIVE_FEE.match(text) or _PLURAL_FEE_REFERENCE.match(text)
        ):
            # Assign a complete note through the same previous-line checks as an unwrapped note.
            break
        if reason := _non_billing_reason(joined):
            return end + 1, reason
        text = joined
    return index + 1, None


def _note_fee_name(text: str) -> str | None:
    if label := _CHARGE_LABEL.fullmatch(text):
        text = label[1]
    if row := _rate_row(text) or _text_row(text, allow_table_name=True):
        text = row["name"]
    else:
        for pattern in (_NOTE_VERB, _NON_BILLING_NOTE, _DETAIL_MONEY):
            if match := pattern.search(text):
                text = text[: match.start()]
    return normalize_charge_item(text)[0] if _FEE.search(text) else None


def _note_applies_to_previous(text: str, previous: str) -> bool:
    if not _note_names_fee(text):
        return True
    name = _note_fee_name(text)
    return bool(name and not _DETAIL_MONEY.search(text) and name == _note_fee_name(previous))


def _preceding_charge_start(lines: Sequence[_Line], end: int, skipped: set[int]) -> int:
    first = end
    while first and _adjacent(lines[first - 1], lines[first]):
        previous = lines[first - 1]
        if previous.number in skipped or _billing_heading(previous.text):
            break
        first -= 1
        if _charge_attribute(previous.text):
            continue
        if label := _CHARGE_LABEL.fullmatch(previous.text):
            if label[1].strip() and (first < end - 1 or _text_row(label[1]) or _rate_row(label[1])):
                return first
        elif first == end - 1 and (
            _rate_row(previous.text)
            or _text_row(previous.text, allow_table_name=True)
            or _LEDGER_ROW.fullmatch(previous.text)
        ):
            return first
        break
    return end


def _stated_fee_count(scope: re.Match[str]) -> int | Decimal | None:
    if scope["both"]:
        return 2
    if raw := scope["count"] or scope["cn_count"]:
        if raw.isdecimal():
            return Decimal(raw)
        if scope["count"]:
            return _FEE_COUNT_WORDS.get(raw.casefold(), 0)
        raw = raw.replace("两", "二").replace("兩", "二")
        tens, separator, units = raw.partition("十")
        if separator:
            tens_value = _CHINESE_FEE_DIGITS.get(tens or "一")
            units_value = _CHINESE_FEE_DIGITS.get(units) if units else 0
            if tens_value is not None and units_value is not None:
                return 10 * tens_value + units_value
            return 0
        return _CHINESE_FEE_DIGITS.get(raw, 0)
    return None


def _preceding_fee_scope(
    lines: Sequence[_Line], index: int, skipped: set[int], scope: re.Match[str]
) -> int | None:
    first = index
    count = 0
    while (previous := _preceding_charge_start(lines, first, skipped)) < first:
        first = previous
        count += 1
    if not count:
        return None
    required = _stated_fee_count(scope)
    if required is not None:
        return first if required == count else None
    if count < 2 and not (scope["all"] or scope["cn_all"]):
        return None

    # An unnumbered reference can reach separated charges in the same section.
    start = lines[first]
    for position in range(first - 1, -1, -1):
        line = lines[position]
        if line.page != start.page or line.sheet != start.sheet or _billing_heading(line.text):
            break
        if line.number not in skipped and (
            _CHARGE_LABEL.fullmatch(line.text)
            or (_FEE.search(line.text) and _DETAIL_MONEY.search(line.text))
            or _text_row(line.text, allow_table_name=True)
        ):
            return None
    return first


def _capture_clauses(
    lines: Sequence[_Line], evidence: _Evidence, *, bind_fee_scopes: bool = True
) -> set[int]:
    skipped = set()
    index = 0
    while index < len(lines):
        start = lines[index]
        end, reason = _wrapped_clause(lines, index)
        if not reason:
            index += 1
            continue
        first = index
        remark = " ".join(line.text for line in lines[index:end])
        scope = _PLURAL_FEE_REFERENCE.match(remark) if reason == _NOTE_REASON else None
        if scope:
            bound = _preceding_fee_scope(lines, index, skipped, scope) if bind_fee_scopes else None
            if bound is None:
                reason = _AMBIGUOUS_NOTE_SCOPE
            else:
                first = bound
        elif (
            reason == _NOTE_REASON
            and index
            and not _billing_heading(start.text)
            and not _CHARGE_LABEL.fullmatch(start.text)
        ):
            previous = lines[index - 1]
            owner_index = index - 1
            while (
                owner_index > 0
                and lines[owner_index].number not in skipped
                and _charge_attribute(lines[owner_index].text)
                and _adjacent(lines[owner_index - 1], lines[owner_index])
            ):
                owner_index -= 1
            if not _CHARGE_LABEL.fullmatch(lines[owner_index].text):
                owner_index = index - 1
            owner = lines[owner_index]
            if (
                _adjacent(previous, start)
                and previous.number not in skipped
                and owner.number not in skipped
                and not _billing_heading(owner.text)
                and (
                    _FEE.search(owner.text)
                    or _DETAIL_MONEY.search(owner.text)
                    or _CHARGE_LABEL.fullmatch(owner.text)
                )
                and _note_applies_to_previous(remark, owner.text)
            ):
                first = owner_index
        if not scope and _CHARGE_LABEL.fullmatch(lines[first].text):
            while (
                end < len(lines)
                and _adjacent(lines[end - 1], lines[end])
                and _charge_attribute(lines[end].text)
            ):
                end += 1
        # PDF line wrapping can split "per" and "cancelled" across physical lines.
        elif reason == _CONDITIONAL_REASON:
            while end < len(lines) and end - first < 4:
                if not _adjacent(lines[end - 1], lines[end]) or (
                    _non_billing_reason(lines[end].text) != reason
                ):
                    break
                end += 1
        elif reason == _BANK_REASON and end < len(lines) and (
            _adjacent(lines[end - 1], lines[end])
            and re.fullmatch(r"[0-9][0-9 -]{7,}", lines[end].text)
        ):
            end += 1
        clause = lines[first:end]
        skipped.update(line.number for line in clause)
        evidence.add_clause(" ".join(line.text for line in clause), reason, start.page)
        index = end
    return skipped


def _is_quantity_or_unit_price(text: str, money: re.Match[str]) -> bool:
    start = money.start("prefix") if money["prefix"] else money.start("amount")
    if (
        _QUANTITY_PREFIX.search(text, 0, start)
        or _QUANTITY_UNIT.match(text, money.end("amount"))
        or _UNIT_PRICE_PREFIX.search(text, 0, start)
        or _UNIT_PRICE_SUFFIX.match(text, money.end())
        or _QUANTITY_MULTIPLIER_PREFIX.search(text, 0, start)
    ):
        return True
    tokens = money.groupdict()
    if money["prefix"] or tokens.get("suffix") or any(
        _amount_currency_tokens(tokens.get("tail") or "")
    ):
        return False
    return bool(
        _MULTIPLIER_PREFIX.search(text, 0, start)
        or _PRE_RATE_SUFFIX.match(text, money.end("amount"))
    )


def _charge_evidence(
    doc: ParsedDocument, rules: RawExtractionEnvelope
) -> Iterator[_ChargeEvidence]:
    def detail_lines(lines: Sequence[_Line], skipped: set[int] | None = None) -> Iterator[str]:
        if skipped is None:
            skipped = _capture_clauses(lines, _Evidence())
        for line in lines:
            if (
                line.number not in skipped
                and not _INSTRUCTION.search(line.text)
                and not _summary_line(line.text)
            ):
                yield line.text

    def source_rows() -> Iterator[str]:
        lines = _source_lines(doc)
        skipped = _capture_clauses(lines, _Evidence())
        yield from detail_lines(lines, skipped)
        for table, locations in _table_text_locations(doc, lines):
            table_lines = _table_lines(table)
            excluded = _capture_clauses(table_lines, _Evidence())
            excluded.update(number for number, rows in locations.items() if skipped & rows)
            yield from detail_lines(table_lines, excluded)
        if doc.ocr_payload is None:
            return
        for item in doc.ocr_payload.items:
            for text in ((item.model_extra or {}).get("text"), item.other):
                if isinstance(text, str):
                    yield from detail_lines(_text_lines(text))
            for row in item.charge_list:
                fields = _fields_for_keys(row, _COLUMN_KEYS)
                name = fields.get("raw_charge_name")
                if not isinstance(name, str) or not name.strip():
                    continue
                source = str(row.get("sourceText") or row.get("source_text") or "")
                name = normalize("NFKC", name).strip()
                if (
                    _SUMMARY.match(name)
                    or any(_SUMMARY.match(line.text) for line in _text_lines(source))
                    or _non_billing_reason(f"{name} {source}")
                ):
                    continue
                values = [
                    str(fields[field])
                    for field in ("amount", "debit", "credit", "currency")
                    if field in fields
                ]
                yield from detail_lines(_text_lines("\t".join([name, *values])))
                yield from detail_lines(_text_lines(source))

    dollar_currency, _ = normalize_currency(
        "$",
        context_currencies=_envelope_currencies(rules),
        us_shipping_context=bool(_US_CONTEXT.search(doc.raw_text)),
    )
    for text in source_rows():
        label = _LINE_AMOUNT_LABEL.search(text)
        end_currency = _END_CURRENCY_TAG.search(text)
        if label:
            match = _DETAIL_MONEY.match(text, label.end())
            matches = (match,) if match else ()
            rate_text = text[: label.start()]
            row_currencies = [
                token
                for rate in _DETAIL_MONEY.finditer(rate_text)
                if _UNIT_PRICE_PREFIX.search(rate_text, 0, rate.start())
                or _UNIT_PRICE_SUFFIX.match(rate_text, rate.end())
                for token in (rate["prefix"], rate["suffix"])
                if token
            ]
        else:
            matches = _DETAIL_MONEY.finditer(text)
            row_currencies = _DETAIL_CURRENCY.findall(text)
            row_currencies.extend(_MARKED_RATE_CURRENCY.findall(text))
        for match in matches:
            adjacent = [token for token in (match["prefix"], match["suffix"]) if token]
            terms = ()
            if tail := _AMOUNT_TAIL.match(text, match.end()):
                adjacent.extend(_amount_currency_tokens(tail["tail"]))
                terms = tuple(dict.fromkeys(_row_payment_terms(tail)))
            if end_currency is not None and end_currency.start() >= match.end():
                adjacent.append(end_currency["parenthesized_currency"])
            currency, conflicts = _printed_currency([*adjacent, *row_currencies])
            is_line_amount = label is not None or not _is_quantity_or_unit_price(text, match)
            if (
                not is_line_amount
                and not conflicts
                and (currency in (None, "$") or currency in ACTIVE_ISO4217_CODES)
            ):
                continue
            try:
                amount = parse_decimal_strict(match["amount"]).copy_abs()
            except ValueError:
                continue
            if currency == "$":
                currency = dollar_currency or currency
            yield _ChargeEvidence(
                text,
                amount,
                frozenset(token for token in (currency, *conflicts) if token),
                terms,
                is_line_amount,
            )


def _record_summary(text: str, evidence: _Evidence) -> bool:
    match = _SUMMARY.match(text)
    if not match:
        return False
    label, rest = match["label"].casefold(), match["rest"]
    if label == "total" and _TRANSPORT_TOTAL.match(rest):
        return True
    field_name = "total_amount"
    if label.startswith(("sub", "小")):
        field_name = "subtotal_amount"
    elif "tax" in label or label.startswith("税"):
        field_name = "tax_amount"
    if rest.upper().startswith("TO "):
        if due_to := _SUMMARY_DUE_TO.match(rest[3:]):
            evidence.fields.setdefault("payee_hint", due_to["party"].strip())
            rest = rest[3 + due_to.end() :]
        elif label == "balance due":
            return True
    tokens = _SUMMARY_STATED_CURRENCY.findall(rest)
    rest = _SUMMARY_STATED_CURRENCY.sub("", rest).strip()
    if prefix := _SUMMARY_CURRENCY_PREFIX.match(rest):
        tokens.append(prefix[1] or prefix[2])
        rest = rest[prefix.end() :]
    money = _MONEY.match(rest)
    if money:
        tokens.extend(token for token in (money["prefix"], money["suffix"]) if token)
    currency = _summary_currency(evidence, field_name, tokens)
    if money:
        tail = rest[money.end() :]
        if not tail.strip() or (tail[0].isspace() and re.fullmatch(_NUMBER, tail.strip(), re.I)):
            evidence.summaries.append((field_name, money["amount"], currency))
            return True
    # A tax title or registration label without a money token is not a tax amount.
    if field_name != "tax_amount" or money or currency:
        evidence.summaries.append((field_name, rest, currency))
    return True


def _flag_conflicting_value(
    evidence: _Evidence, field_name: str, previous: Any, value: Any
) -> None:
    if previous == value:
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


def _printed_currency(tokens: Sequence[str]) -> tuple[str | None, list[str]]:
    currencies = list(
        dict.fromkeys(
            normalize_currency(_printed_currency_token(token))[0] or token.upper()
            for token in tokens
        )
    )
    explicit = [currency for currency in currencies if currency != "$"]
    if not explicit:
        return (currencies[0] if currencies else None), []
    first, *conflicts = explicit
    if "$" in currencies and not conflicts and first not in _DOLLAR_CURRENCIES:
        conflicts.append("$")
    return first, conflicts


def _summary_currency(evidence: _Evidence, field_name: str, tokens: Sequence[str]) -> str | None:
    first, conflicts = _printed_currency(tokens)
    for conflict in conflicts:
        _flag_conflicting_value(evidence, f"{field_name}_currency", first, conflict)
    return first


def _summary_values(evidence: _Evidence, dollar_currency: str | None) -> None:
    def scope(token: str | None) -> str | None:
        if token == "$":
            return dollar_currency or token
        if not isinstance(token, str) or not token:
            return None
        return normalize_currency(_printed_currency_token(token))[0] or token.upper()

    default = scope(evidence.fields.get("total_currency"))
    scoped_summaries = [(field, value, scope(token)) for field, value, token in evidence.summaries]
    scopes = {currency for _, _, currency in scoped_summaries if currency is not None}
    separate_currencies = len(scopes) > 1
    if not default and len(scopes) == 1:
        default = next(iter(scopes))

    by_field = defaultdict(dict)
    for field_name, value, currency in scoped_summaries:
        currency = currency or default
        previous = by_field[field_name].setdefault(currency, value)
        try:
            equal = parse_decimal_strict(previous) == parse_decimal_strict(value)
        except ValueError:
            equal = previous == value
        if not equal:
            label = f"{field_name}.{currency}" if currency and separate_currencies else field_name
            _flag_conflicting_value(evidence, label, previous, value)

    totals = by_field.get("total_amount", {})
    scalar_scope = default if default in totals else next(iter(totals), default)
    for field_name, amounts in by_field.items():
        if separate_currencies:
            evidence.fields[_SUMMARY_MAPS[field_name]] = {
                currency: amount for currency, amount in amounts.items() if currency is not None
            }
            if None in amounts:
                evidence.fields[field_name] = amounts[None]
                evidence.issues.append(
                    ValidationAlert.from_code(
                        AlertCode.MULTI_CURRENCY_TOTAL_AMBIGUITY,
                        "源单有多种汇总币种，此项汇总未标明币种",
                        field=field_name,
                        actual=str(amounts[None]),
                    )
                )
            continue
        if totals and field_name != "total_amount" and scalar_scope not in amounts:
            continue
        currency = scalar_scope if scalar_scope in amounts else next(iter(amounts))
        evidence.fields[field_name] = amounts[currency]
        if currency:
            evidence.fields.setdefault("total_currency", currency)


def _labeled_values(lines: Sequence[_Line], label: str) -> Iterator[str]:
    pattern = re.compile(rf"^\s*(?:{label})(?:(?<=:)|(?=\W|$))\s*[:#=]?\s*(.*)$", re.I)
    for index, line in enumerate(lines):
        if not (match := pattern.match(line.text)):
            continue
        rest = match[1]
        if not rest and index + 1 < len(lines) and lines[index + 1].page == line.page:
            rest = lines[index + 1].text
        yield rest


def _value_after(lines: Sequence[_Line], label: str, value: str) -> str | None:
    for rest in _labeled_values(lines, label):
        if found := re.match(value, rest, re.I):
            return found[0].strip()
    return None


def _first_company(text: str) -> str:
    match = _COMPANY.match(text)
    return match[0] if match else text


def _party(lines: Sequence[_Line], label: str, *, company_only: bool = False) -> str | None:
    for value in _labeled_values(lines, label):
        if tail := _PARTY_VALUE_TAIL.search(value):
            value = value[: tail.start()].rstrip(" ;")
        if value and (not company_only or _COMPANY.match(value)):
            return value
    return None


def _collect_text_headers(
    doc: ParsedDocument, lines: Sequence[_Line], evidence: _Evidence
) -> str | None:
    title = next((match for line in lines if (match := _BILL_TITLE.match(line.text))), None)
    if title:
        title_type = " ".join(title[0].upper().split())
        title_type = re.sub(r"^DEBIT\s*(NOTE|INVOICE)$", r"DEBIT \1", title_type)
        evidence.fields.setdefault("document_type", title_type)
    for field_name, label in _REFERENCE_LABELS.items():
        if value := _value_after(lines, label, _REF):
            evidence.fields.setdefault(field_name, value.upper())
    if title and (
        is_credit_document_type(title_type) or "INVOICE" in title_type or title_type == "DEBIT NOTE"
    ):
        if reference := _TITLE_REFERENCE.match(title.string[title.end() :]):
            evidence.fields.setdefault("reference_no", reference[1].upper())
    if "reference_no" not in evidence.fields:
        for line in lines:
            if _PARTY_HEADER.match(line.text) and (
                reference := _INLINE_REFERENCE.search(line.text)
            ):
                evidence.fields["reference_no"] = reference[1].upper()
                break
    date = _value_after(
        lines,
        r"(?:invoice|document|issue|D/C|D/N|C/N|debit note|credit note)?\s*date|general notice",
        _DATE,
    )
    if date is None and doc.source_kind is SourceKind.EML_BODY:
        date = next((line.text for line in lines if re.fullmatch(_DATE, line.text, re.I)), None)
    if date:
        evidence.fields.setdefault("document_date", date)
    for line in lines:
        if currency := _CURRENCY_LABEL.fullmatch(line.text):
            evidence.fields.setdefault("total_currency", currency[1])
            evidence.currencies.add(currency[1])
    for field_name, label in _PARTY_LABELS.items():
        value = _party(lines, label)
        if value:
            evidence.fields.setdefault(field_name, value)
    # Unqualified From/To labels can also identify ports on shipping invoices.
    for field_name, label in (("issuer", r"from\s*:"), ("recipient", r"to\s*:")):
        if field_name not in evidence.fields and (value := _party(lines, label, company_only=True)):
            evidence.fields[field_name] = value
    if title:
        leading_companies = []
        for line in lines[:18]:
            if (
                _REMITTANCE.search(line.text)
                or "PAYMENTS MUST BE ISSUED" in line.text.upper()
                or re.match(r"(?:SHIPPER|CONSIGNEE)\b", line.text, re.I)
            ):
                break
            if _COMPANY.match(line.text) and not line.text.upper().startswith("AS AGENTS FOR"):
                leading_companies.append(_first_company(line.text))
        if leading_companies:
            evidence.fields.setdefault("issuer", leading_companies[0])
            if len(leading_companies) > 1 and title[0].upper() == "INVOICE":
                evidence.fields.setdefault("recipient", leading_companies[1])
    payer = evidence.fields.get("payer_hint")
    if payer and evidence.fields.get("recipient") and re.fullmatch(r"[A-Z0-9]{8,}", payer):
        # A printed internal payer account code is separate from the named invoice recipient.
        if not entity_matches(payer, evidence.fields["recipient"]):
            evidence.fields.pop("payer_hint")
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
        evidence.add_containers(line.text)
        following = ""
        if index + 1 < len(lines) and lines[index + 1].page == line.page:
            following = lines[index + 1].text
        for label in _BOX_LABEL.finditer(line.text):
            value = line.text[label.end() :] or following
            while match := _LABELED_BOX.match(value):
                evidence.add_containers(match[0], labeled=True)
                value = value[match.end() :].lstrip(" ,;/\t")
        upper = line.text.upper()
        if following and upper.startswith("SHIPPER") and "B/L" in upper:
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
    return is_transport


def _transport_parties(doc: ParsedDocument, lines: Sequence[_Line]) -> list[tuple[str, str]]:
    parties = [
        (role, _first_company(name))
        for role, label in _TRANSPORT_PARTY_LABELS.items()
        if (name := _value_after(lines, label, r"[^\d\W][^\n]*"))
    ]
    if doc.ocr_payload is not None:
        for item in doc.ocr_payload.items:
            fields = {"shipper": item.shipper, "consignee": item.consignee}
            if isinstance(item.other, Mapping):
                fields = {
                    **_fields_for_keys(item.other, {key: (key,) for key in fields}),
                    **{key: value for key, value in fields.items() if value},
                }
            parties.extend(
                (role, name)
                for role, name in fields.items()
                if isinstance(name, str) and name.strip()
            )
    return list(dict.fromkeys(parties))


def transport_context(doc: ParsedDocument) -> list[str]:
    """Keep transport parties, ports and freight terms as context."""
    lines = _source_lines(doc)
    values = [
        f"{role.capitalize()}: {sanitize_for_llm(name)}"
        for role, name in _transport_parties(doc, lines)
    ]
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
    if isinstance(term := fields.get("payment_term"), str):
        term = term.strip()
        if term.startswith("(") and term.endswith(")"):
            fields["payment_term"] = term[1:-1]
    return {
        **fields,
        "source_text": text,
        "line_number": line_number,
        "page": page,
    }


def _table_text_locations(
    doc: ParsedDocument, lines: Sequence[_Line]
) -> Iterator[tuple[ParsedTable, dict[int, set[int]]]]:
    """Locate the text rendering of each table row, preserving repeated source occurrences."""
    if not doc.tables:
        return
    office = doc.source_kind in (SourceKind.EXCEL, SourceKind.WORD)

    def text_key(text: str) -> str:
        text = normalize("NFKC", text).strip()
        if office:
            return "\t".join(" ".join(cell.split()) for cell in text.split("\t"))
        return " ".join(text.split())

    line_keys = [(line.page, line.sheet, text_key(line.text)) for line in lines]
    indexes = defaultdict(list)
    for index, key in enumerate(line_keys):
        indexes[key].append(index)
    assigned = set()
    for table in doc.tables:
        page, sheet = _table_location(table)
        locations = {}
        cursor = 0
        for number, row in enumerate(table.rows, 1):
            keys = [
                (page, sheet, key)
                for text in "\t".join(row).splitlines()
                if (key := text_key(text))
            ]
            if not keys:
                continue
            starts = indexes[keys[0]]
            for candidate in range(bisect_left(starts, cursor), len(starts)):
                start = starts[candidate]
                end = start + len(keys)
                positions = range(start, end)
                if line_keys[start:end] == keys and assigned.isdisjoint(positions):
                    locations[number] = {lines[index].number for index in positions}
                    assigned.update(positions)
                    cursor = end
                    break
        yield table, locations


def _collect_tables(
    doc: ParsedDocument,
    lines: Sequence[_Line],
    evidence: _Evidence,
    skipped: set[int],
    *,
    include_charges: bool,
) -> set[int]:
    row_number = 0
    handled = set()
    for table, locations in _table_text_locations(doc, lines):
        columns: dict[str, int] = {}
        page, _ = _table_location(table)
        table_skipped = _capture_clauses(
            _table_lines(table), evidence, bind_fee_scopes=include_charges
        )
        for row_index, row in enumerate(table.rows, 1):
            row_number += 1
            text_positions = locations.get(row_index, set())
            if row_index in table_skipped or skipped & text_positions:
                skipped.update(text_positions)
                continue
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
            joined = "\t".join(cells).strip()
            if (
                _CHARGE_LABEL.fullmatch(joined)
                or _CHARGE_AMOUNT.fullmatch(joined)
                or _payment_term_continuation(joined)
            ):
                continue
            if not columns:
                if _record_summary(" ".join(filter(None, cells)), evidence):
                    handled.update(text_positions)
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
                _record_summary(f"{name} {amount or ''} {currency}".strip(), evidence)
                handled.update(text_positions)
                continue
            if (
                not include_charges
                or not name
                or not fields.keys() & {"amount", "debit", "credit", "payment_term"}
            ):
                continue
            source = f"{table.source or '[Table]'} [Row {row_index}] {joined}"
            line_number = min(text_positions) if text_positions else row_number
            if not text_positions and lines:
                line_number += lines[-1].number
            terms = _payment_term_continuation(fields.get("payment_term", ""))
            evidence.add_charge(
                _charge_from_columns(fields, source, line_number, page), payment_terms=terms
            )
            handled.update(text_positions)
    return handled


def _collect_ocr(doc: ParsedDocument, evidence: _Evidence) -> None:
    if doc.ocr_payload is None:
        return
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
                evidence.summaries.extend(
                    ("total_amount", amount, str(currency))
                    for currency, amount in separate_totals.items()
                    if amount is not None
                )
            elif separate_totals is not None:
                evidence.fields.setdefault("totals_by_currency", separate_totals)
            for name, value in refs.items():
                evidence.fields.setdefault(name, value)
        for ctr in item.ctr_list:
            evidence.add_containers(str(ctr), labeled=True)
        item_currency = item_fields.get("total_currency")
        if isinstance(item_currency, str):
            evidence.currencies.add(item_currency)
        for field_name in _SUMMARY_MAPS:
            value = item_fields.pop(field_name, None)
            if value is None:
                continue
            currency = item_currency
            if isinstance(value, str) and (money := _MONEY.fullmatch(value.strip())):
                value = money["amount"]
                tokens = [token for token in (money["prefix"], money["suffix"]) if token]
                currency = _summary_currency(evidence, field_name, tokens) or currency
                item_currency = item_currency or currency
            evidence.summaries.append((field_name, value, currency))
        notes = _Evidence(fields=item_fields)
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
                    if reason == _NOTE_REASON and _PLURAL_FEE_REFERENCE.match(text):
                        # Structured remarks have no physical line positions relative to charges.
                        reason = _AMBIGUOUS_NOTE_SCOPE
                    evidence.add_clause(text, reason, page)
                else:
                    _record_summary(text, notes)
        evidence.summaries.extend(
            (name, value, currency or item_currency) for name, value, currency in notes.summaries
        )
        evidence.issues.extend(notes.issues)
        for name, value in item_fields.items():
            previous = evidence.fields.setdefault(name, value)
            if name == "reference_no":
                _flag_conflicting_value(evidence, name, previous, value)
        for row in item.charge_list:
            fields = _fields_for_keys(row, _COLUMN_KEYS)
            name = fields.get("raw_charge_name")
            if not isinstance(name, str) or not name.strip():
                continue
            text = str(row.get("sourceText") or row.get("source_text") or name)
            printed_text = normalize("NFKC", text).strip()
            if reason := _non_billing_reason(f"{name} {printed_text}"):
                evidence.add_clause(text, reason, page)
                continue
            if _SUMMARY.match(name):
                _record_summary(
                    f"{name} {fields.get('currency', '')} {fields.get('amount', '')}", evidence
                )
                continue
            row_currency = fields.get("currency")
            if item_currency is not None:
                fields.setdefault("currency", item_currency)
            raw_term = fields.get("payment_term")
            terms = _payment_term_continuation(raw_term) if isinstance(raw_term, str) else []
            rate_row = _rate_row(printed_text)
            if printed_row := rate_row or _text_row(printed_text):
                terms = [*_row_payment_terms(printed_row), *terms]
            if not terms and item_term is not None:
                fields.setdefault("payment_term", item_term)
            charge = _charge_from_columns(fields, text, len(evidence.charges) + 1, page)
            if rate_row is None:
                evidence.add_charge(charge, payment_terms=terms)
                continue
            issues = _ground_ocr_rate(charge, rate_row)
            evidence.add_charge(
                charge, *_rate_currency_tokens(rate_row),
                row_currency if isinstance(row_currency, str) else None,
                payment_terms=terms, parse_issues=issues,
            )


def _split_ocr_rows(lines: Sequence[_Line], evidence: _Evidence, skipped: set[int]) -> set[int]:
    """Read vertically split rate/amount columns from an OCR invoice."""
    header = next(
        (index for index, line in enumerate(lines) if "R.O.E." in line.text.upper()), None
    )
    end = len(lines)
    if (
        header is not None
        and end >= header + 4
        and lines[header].page == lines[-2].page
        and _adjacent(lines[-2], lines[-1])
        and not {lines[-2].number, lines[-1].number} & skipped
        and not _FEE.search(lines[-3].text)
        and re.fullmatch(_CURRENCY, lines[-2].text, re.I)
        and re.fullmatch(_NUMBER, lines[-1].text, re.I)
    ):
        evidence.summaries.append(("total_amount", lines[-1].text, lines[-2].text))
        end -= 2
    consumed = set()
    for start in range(end):
        description = lines[start]
        if (
            description.number in skipped
            or not _FEE.search(description.text)
            or re.search(r"\d", description.text)
            or _billing_heading(description.text)
            or _CHARGE_LABEL.fullmatch(description.text)
        ):
            continue
        has_rate_header = (
            header is not None
            and start > header
            and description.page == lines[header].page
            and description.sheet == lines[header].sheet
        )
        stop = end
        if not has_rate_header:
            stop = start + 3
            if stop > end:
                continue
            currency_cell, amount_cell = lines[start + 1 : stop]
            if not (
                _adjacent(description, currency_cell)
                and _adjacent(currency_cell, amount_cell)
                and _CURRENCY_TERM_CELL.fullmatch(currency_cell.text)
                and re.fullmatch(_NUMBER, amount_cell.text, re.I)
            ):
                continue

        detail = []
        numbers = []
        currencies = []
        terms = []
        for cell_index in range(start + 1, stop):
            line = lines[cell_index]
            if (
                line.page != description.page
                or line.sheet != description.sheet
                or line.number in skipped
            ):
                break
            if money := _MONEY.fullmatch(line.text):
                numbers.append(money)
                currencies.extend(token for token in (money["prefix"], money["suffix"]) if token)
            elif cell := _CURRENCY_TERM_CELL.fullmatch(line.text):
                currencies.append(cell["currency"])
                terms.extend(_payment_terms(cell["terms"] or ""))
            elif continuation := _TERM_CONTINUATION.fullmatch(line.text):
                terms.extend(_payment_terms(continuation["terms"]))
            # R.O.E. layouts can contain unreadable cells between recognized columns.
            elif (
                not has_rate_header
                or _billing_heading(line.text)
                or _CHARGE_LABEL.fullmatch(line.text)
                or _CHARGE_AMOUNT.fullmatch(line.text)
                or _FEE.search(line.text)
            ):
                break
            detail.append(line)
        if not numbers:
            continue
        evidence.add_charge(
            {
                "raw_charge_name": description.text,
                "amount": numbers[-1]["amount"],
                "unit_price": numbers[0]["amount"] if len(numbers) > 1 else None,
                "currency": evidence.fields.get("total_currency"),
                "source_text": "\n".join(line.text for line in (description, *detail)),
                "line_number": description.number,
                "page": description.page,
            },
            *currencies,
            payment_terms=terms,
        )
        consumed.add(description.number)
        consumed.update(line.number for line in detail)
    return consumed


def _rate_row(text: str) -> re.Match[str] | None:
    row = _RATE_ROW.fullmatch(text)
    if row and (
        (row["marker"] and _FEE.search(row["name"]))
        or row["name"].casefold() in {"wharfage", "wharfage administration fee"}
    ):
        return row
    return None


def _line_amount_name(text: str, label: re.Match[str]) -> str:
    name = text[: label.start()].strip(" :\t(")
    return name if name and not _billing_heading(text) else ""


def _text_row(text: str, *, allow_table_name: bool = False) -> re.Match[str] | None:
    if label := _LINE_AMOUNT_LABEL.search(text):
        name = _line_amount_name(text, label)
        if (
            name
            and not _UNPARSED_RATE.search(name)
            and (row := _LABELED_DETAIL_ROW.fullmatch(text))
            and row.end("name") < label.start()
        ):
            return row
        return None
    row = _TEXT_ROW.fullmatch(text)
    if row and (
        (_FEE.search(row["name"]) or (allow_table_name and "\t" in text))
        and not _TRANSPORT_LABEL.search(row["name"])
        and not _is_quantity_or_unit_price(text, row)
    ):
        return row
    return None


def _amount_currency_tokens(tail: str) -> Iterator[str]:
    for tag in _AMOUNT_TAG.finditer(tail):
        if currency := tag["parenthesized_currency"] or tag["bare_currency"]:
            yield currency


def _row_currency_tokens(row: re.Match[str]) -> tuple[str | None, ...]:
    return (row["prefix"], *_amount_currency_tokens(row["tail"]))


def _rate_currency_tokens(row: re.Match[str]) -> tuple[str | None, ...]:
    return (row["rate_prefix"], row["rate_suffix"], *_row_currency_tokens(row))


def _row_payment_terms(row: re.Match[str]) -> list[PaymentTerm]:
    return [
        term
        for tag in _AMOUNT_TAG.finditer(row["tail"])
        if (token := tag["term"])
        for term in _payment_terms(token)
    ]


def _text_charge_fields(row: re.Match[str]) -> dict[str, str | None]:
    return {"raw_charge_name": row["name"], "amount": row["amount"]}


def _rate_numbers(row: re.Match[str]) -> dict[str, str | None]:
    return {
        "quantity": row["quantity"] or row["multiplier_qty"],
        "unit_price": row["rate"],
        "amount": row["amount"],
    }


def _quantity_conflict(row: re.Match[str]) -> ValidationAlert | None:
    quantity, multiplier = row["quantity"], row["multiplier_qty"]
    if quantity is None or multiplier is None:
        return None
    try:
        same_quantity = parse_decimal_strict(quantity) == parse_decimal_strict(multiplier)
    except ValueError:
        return None  # The decimal field parser reports an invalid printed quantity.
    if same_quantity:
        return None
    return ValidationAlert.from_code(
        AlertCode.CROSS_DOCUMENT_CONFLICT,
        "费用明细行的数量与费率后的乘数不一致",
        field="quantity",
        expected=quantity,
        actual=multiplier,
    )


def _ground_ocr_rate(
    charge: dict[str, Any], printed_row: re.Match[str]
) -> list[ValidationAlert | None]:
    issues = [_quantity_conflict(printed_row)]
    for field_name, printed in _rate_numbers(printed_row).items():
        if printed is None:
            continue
        structured = charge.get(field_name)
        charge[field_name] = printed
        if structured is None:
            continue
        try:
            agrees = parse_decimal_strict(printed) == parse_decimal_strict(structured)
        except ValueError:
            agrees = False
        if not agrees:
            issues.append(
                ValidationAlert.from_code(
                    AlertCode.CROSS_DOCUMENT_CONFLICT,
                    "OCR 结构化字段与费用行原文数字不一致",
                    field=field_name,
                    expected=printed,
                    actual=sanitize_for_llm(str(structured)),
                )
            )
    return issues


@dataclass(slots=True)
class _PendingCharge:
    name: str | None = None
    labeled: bool = False
    amount: tuple[str, _Line, re.Match[str] | None] | None = None
    payment_terms: list[PaymentTerm] = field(default_factory=list)

    def set_amount(self, value: str, line: _Line) -> None:
        money = _LABELED_MONEY.fullmatch(value)
        self.amount = (value, line, money)
        if money:
            self.payment_terms.extend(_row_payment_terms(money))

    def emit(self, evidence: _Evidence) -> None:
        if not self.name or not (self.amount or self.payment_terms):
            return
        fields: dict[str, Any] = {"raw_charge_name": self.name}
        tokens = ()
        if self.amount is not None:
            amount, line, money = self.amount
            fields["amount"] = money["amount"] if money else amount
            if money:
                tokens = _row_currency_tokens(money)
            fields.update(source_text=line.text, line_number=line.number, page=line.page)
        evidence.add_charge(fields, *tokens, payment_terms=self.payment_terms)


def _collect_text_charges(
    doc: ParsedDocument,
    lines: Sequence[_Line],
    evidence: _Evidence,
    skipped: set[int],
    table_rows: set[int],
) -> None:
    consumed = table_rows | _split_ocr_rows(lines, evidence, skipped | table_rows)
    pending = _PendingCharge()
    in_ledger = False

    def add_row(
        fields: dict[str, Any],
        line: _Line,
        *currencies: str | None,
        payment_terms: Sequence[PaymentTerm] = (),
        parse_issues: Sequence[ValidationAlert | None] = (),
        invalid_fields: Mapping[str, str] | None = None,
    ) -> None:
        nonlocal pending
        pending.emit(evidence)
        pending = _PendingCharge()
        evidence.add_charge(
            _charge_from_columns(fields, line.text, line.number, line.page),
            *currencies,
            payment_terms=payment_terms,
            parse_issues=parse_issues,
            invalid_fields=invalid_fields,
        )

    for line in lines:
        text = line.text
        label = _CHARGE_LABEL.fullmatch(text)
        closes_pending = line.number in consumed or (
            label is not None and (pending.name is not None or line.number in skipped)
        )
        if closes_pending:
            pending.emit(evidence)
            pending = _PendingCharge()
        if (
            line.number in skipped
            or line.number in consumed
            or _INSTRUCTION.search(text)
            or _record_summary(text, evidence)
        ):
            continue
        if _LEDGER_HEADER.search(text):
            in_ledger = True
            continue
        if total := _INLINE_TOTAL.search(text):
            detail = text[: total.start()]
            if _rate_row(detail) or _text_row(detail):
                _record_summary(total["summary"], evidence)
                text = detail
        if amount := _CHARGE_AMOUNT.fullmatch(text):
            if pending.name is not None:
                pending.set_amount(amount[1], line)
            continue
        if label:
            inline_row = _text_row(label[1])
            if (
                _rate_row(label[1])
                or (inline_row and any(_row_currency_tokens(inline_row)))
                or _LINE_AMOUNT_LABEL.search(label[1])
            ):
                text = label[1]
            else:
                pending.name = label[1]
                pending.labeled = True
                continue
        if (terms := _payment_term_continuation(text)) and (
            pending.name is not None or _CHANGE_TERM.search(text)
        ):
            pending.payment_terms.extend(terms)
            continue
        if payment := _PAYMENT_INSTRUCTION.fullmatch(text):
            if row := _text_row(payment["charge"]):
                evidence.fields.setdefault("payer_hint", payment["payer"].strip())
                evidence.fields.setdefault("payee_hint", payment["payee"].strip())
                evidence.fields.setdefault("document_type", "EMAIL SETTLEMENT INSTRUCTION")
                fields = _text_charge_fields(row)
                if _key(row["name"]) == "hc":
                    fields["raw_charge_name"] = "Handling Charge"
                add_row(
                    fields, line,
                    *_MARKED_RATE_CURRENCY.findall(row["name"]), *_row_currency_tokens(row),
                    payment_terms=_row_payment_terms(row),
                )
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
            add_row(fields, line)
        elif rate_row := _rate_row(text):
            add_row(
                {"raw_charge_name": rate_row["name"], **_rate_numbers(rate_row)},
                line, *_rate_currency_tokens(rate_row),
                payment_terms=_row_payment_terms(rate_row),
                parse_issues=(_quantity_conflict(rate_row),),
            )
        elif row := _text_row(text):
            add_row(
                _text_charge_fields(row), line,
                *_MARKED_RATE_CURRENCY.findall(row["name"]), *_row_currency_tokens(row),
                payment_terms=_row_payment_terms(row),
            )
        elif label_amount := _LINE_AMOUNT_LABEL.search(text):
            if not (name := _line_amount_name(text, label_amount)):
                continue
            if detail := _UNPARSED_RATE.search(name):
                name = name[: detail.start()].strip() or name
            raw_amount = sanitize_for_llm(text[label_amount.end() :])
            issue = ValidationAlert.from_code(
                AlertCode.LLM_EXTRACTION_FAILED,
                "费用行标明了行总额，但无法可靠解析，待人工复核",
                field="amount",
                actual=raw_amount,
            )
            evidence.issues.append(issue)
            add_row(
                {"raw_charge_name": name, "raw_amount_text": raw_amount},
                line,
                parse_issues=(issue,),
                invalid_fields={"amount": raw_amount},
            )
        elif not pending.labeled and (
            _FEE.search(text)
            and len(text) <= 80
            and re.fullmatch(r"[^\W\d_][\w /'-]*", text)
            and not _TRANSPORT_LABEL.search(text)
        ):
            pending.name = text
    pending.emit(evidence)


def _annotate_dollar(
    envelope: RawExtractionEnvelope | RawChargeEnvelope,
    field_name: str,
    currency: str | None,
    reason: str | None,
) -> None:
    envelope.explicit_fields.add(field_name)
    if currency is None:
        envelope.raw_invalid_fields[field_name] = "$"
    envelope.raw_parse_issues.append(
        ValidationAlert.from_code(
            AlertCode.CONTEXT_INFERRED_FIELD if currency else AlertCode.INVALID_OR_MISSING_CURRENCY,
            reason or "无法确定 $ 所指币种",
            field=field_name,
            actual="$",
            expected=currency,
        )
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
    lines = _source_lines(doc)
    title = _collect_text_headers(doc, lines, evidence)
    is_transport = _collect_transport(doc, lines, evidence, bool(title))
    has_ocr_charges = bool(evidence.charges)
    skipped = _capture_clauses(lines, evidence, bind_fee_scopes=not has_ocr_charges)
    table_rows = _collect_tables(doc, lines, evidence, skipped, include_charges=not has_ocr_charges)
    if not has_ocr_charges:
        _collect_text_charges(doc, lines, evidence, skipped, table_rows)
    else:
        for line in lines:
            if line.number not in skipped and line.number not in table_rows:
                _record_summary(line.text, evidence)

    summary_tokens = [token for _, _, token in evidence.summaries]
    tokens = (
        evidence.fields.get("total_currency"),
        *evidence.currencies,
        *(row.get("currency") for row in evidence.charges),
        *summary_tokens,
    )
    known_currencies = {
        currency
        for token in tokens
        if isinstance(token, str)
        and (currency := normalize_currency(_printed_currency_token(token))[0])
    }
    dollar_currency, dollar_reason = normalize_currency(
        "$",
        context_currencies=known_currencies,
        us_shipping_context=bool(_US_CONTEXT.search(doc.raw_text)),
    )
    _summary_values(evidence, dollar_currency)
    ocr_metadata_only = (
        doc.ocr_payload is not None
        and doc.ocr_payload.has_metadata
        and not doc.ocr_payload.has_charges
    )
    metadata_only = doc.status is DocumentStatus.METADATA_ONLY or (
        not evidence.charges and not evidence.summaries and (is_transport or ocr_metadata_only)
    )
    document_currency = evidence.fields.get("total_currency")
    dollar_summary = document_currency == "$" or "$" in summary_tokens
    if document_currency == "$":
        evidence.fields["total_currency"] = dollar_currency
    elif isinstance(document_currency, str):
        evidence.fields["total_currency"] = _printed_currency_token(document_currency)
    dollar_summary_fields = []
    for field_name in _SUMMARY_MAPS.values():
        amounts = evidence.fields.get(field_name)
        if isinstance(amounts, dict) and "$" in amounts:
            # Keep an ambiguous symbol in the audit instead of feeding it back into inference.
            amounts.pop("$")
            dollar_summary_fields.append(f"{field_name}.$")
    dollar_rows = []
    for index, charge in enumerate(evidence.charges if not metadata_only else ()):
        currency = charge.get("currency")
        if (
            currency is None
            and "currency" not in evidence.charge_invalid_fields.get(index, {})
            and len(known_currencies) <= 1
        ):
            currency = document_currency
        if currency == "$":
            dollar_rows.append(index)
            currency = dollar_currency
        if isinstance(currency, str):
            currency = _printed_currency_token(currency)
        charge["currency"] = currency

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
                "has_explicit_total": any(
                    field != "tax_amount" for field, _, _ in evidence.summaries
                ),
                "metadata_only": metadata_only,
            }
        )
    )
    result.raw_parse_issues.extend(evidence.issues)
    if not metadata_only:
        for index, fields in evidence.charge_invalid_fields.items():
            result.charges[index].explicit_fields.update(fields)
            result.charges[index].raw_invalid_fields.update(fields)
        for index, issues in evidence.charge_issues.items():
            result.charges[index].raw_parse_issues.extend(issues)
    if dollar_summary:
        for field_name in dollar_summary_fields or ("total_currency",):
            _annotate_dollar(result, field_name, dollar_currency, dollar_reason)
    for index in dollar_rows:
        _annotate_dollar(result.charges[index], "currency", dollar_currency, dollar_reason)
    return result


@dataclass(frozen=True, slots=True)
class SiblingFieldUse:
    field: str
    value: str
    source: ParsedDocument
    line_index: int | None = None

    @property
    def filename(self) -> str:
        return sanitize_source_path(self.source.original_filename) or self.source.sanitized_filename


def _package_parties(
    siblings: Sequence[tuple[ParsedDocument, RawExtractionEnvelope]],
) -> list[tuple[str, ParsedDocument, str | None]]:
    parties = []
    for doc, envelope in siblings:
        for name in (envelope.extraction.issuer, envelope.extraction.recipient):
            if name:
                parties.append((name, doc, None))
        lines = _source_lines(doc)
        shipper_address = re.search(
            r"^SHIPPER\b[^\n]*(?:\n(?!CONSIGNEE\b)[^\n]*){0,5}",
            "\n".join(line.text for line in lines),
            re.I | re.M,
        )
        for role, name in _transport_parties(doc, lines):
            shanghai_shipper = (
                role == "shipper"
                and shipper_address
                and re.search(r"\bSHANGHAI\b", shipper_address[0], re.I)
            )
            parties.append((name, doc, "sha" if shanghai_shipper else None))
        if not envelope.metadata_only:
            continue
        for current, following in zip(lines, lines[1:], strict=False):
            if "FOR CARGO ARRIVAL" in current.text.upper() and current.page == following.page:
                first = _first_company(following.text)
                second = following.text[len(first) :].strip()
                if second:
                    parties.append((_first_company(second), doc, None))
    return parties


def _resolve_short_party(
    name: str,
    candidates: Sequence[tuple[str, ParsedDocument, str | None]],
    profiles: Sequence[AccountingEntityProfile],
) -> tuple[str, list[ParsedDocument]] | None:
    key = normalize_party_name(name)
    if any(normalize_party_name(profile.canonical_name) == key for profile in profiles):
        return None
    aliases = {
        normalize_party_name(profile.canonical_name): profile.canonical_name
        for profile in profiles
        if any(key == normalize_party_name(alias) for alias in profile.aliases)
    }
    if not key or len(aliases) > 1:
        return None
    canonical = next(iter(aliases.values()), None)
    matches = []
    for candidate, source, location_alias in candidates:
        full_name = normalize_party_name(candidate)
        if full_name == key:
            continue
        if canonical:
            matches_party = entity_matches(candidate, canonical, profiles)
        else:
            matches_party = key == location_alias or (
                key.isascii()
                and key.isalpha()
                and 3 <= len(key) <= 5
                and full_name.replace(" ", "").startswith(key)
                and " " in full_name
            )
        if matches_party:
            matches.append((candidate, source))
    if not matches:
        return None
    # The same party can have different OCR word breaks in the MBL and HBL.
    distinct_names = {normalize_party_name(candidate).replace(" ", "") for candidate, _ in matches}
    if canonical is None and len(distinct_names) != 1:
        return None
    full_name = canonical or min(
        (candidate for candidate, _ in matches), key=lambda value: (len(value), value)
    )
    return full_name, list({doc.doc_id: doc for _, doc in matches}.values())


def _envelope_currencies(envelope: RawExtractionEnvelope) -> set[str]:
    return {
        currency
        for currency in (
            envelope.total_currency,
            *envelope.subtotals_by_currency,
            *envelope.taxes_by_currency,
            *envelope.totals_by_currency,
            *(row.charge.currency for row in envelope.charges),
        )
        if currency
    }


def _resolve_package_dollars(
    envelope: RawExtractionEnvelope,
    siblings: Sequence[tuple[ParsedDocument, RawExtractionEnvelope]],
    source_extraction: RawExtractionEnvelope | None,
) -> tuple[list[RawChargeEnvelope], list[SiblingFieldUse]]:
    if not any(row.raw_invalid_fields.get("currency") == "$" for row in envelope.charges):
        return envelope.charges, []
    known_currencies = _envelope_currencies(envelope).union(
        *(_envelope_currencies(sibling) for _, sibling in siblings)
    )
    if source_extraction is not None:
        known_currencies.update(_envelope_currencies(source_extraction))
    us_sources = [doc for doc, _ in siblings if _US_CONTEXT.search(doc.raw_text)]
    currency, reason = normalize_currency(
        "$", context_currencies=known_currencies, us_shipping_context=bool(us_sources)
    )
    if currency is None:
        return envelope.charges, []
    sources = [doc for doc, sibling in siblings if currency in _envelope_currencies(sibling)]
    sources = sources or us_sources
    message = reason or "根据同封邮件的上下文确定币种"
    if sources:
        filenames = dict.fromkeys(
            sanitize_source_path(doc.original_filename) or doc.sanitized_filename for doc in sources
        )
        message += f"（字段 currency 补自 {'、'.join(filenames)}）"
    resolved = []
    uses = []
    for index, row in enumerate(envelope.charges):
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
                message,
                field="currency",
                expected=currency,
                actual="$",
            )
        )
        uses.extend(SiblingFieldUse("currency", currency, doc, index) for doc in sources)
        resolved.append(
            row.model_copy(
                update={
                    "charge": row.charge.model_copy(update={"currency": currency}),
                    "raw_invalid_fields": invalid,
                    "raw_parse_issues": issues,
                }
            )
        )
    return resolved, uses


def _context_alerts(uses: Sequence[SiblingFieldUse]) -> list[ValidationAlert]:
    filenames = defaultdict(list)
    for use in uses:
        filenames[use.field, use.value].append(use.filename)
    return [
        ValidationAlert.from_code(
            AlertCode.CONTEXT_INFERRED_FIELD,
            f"字段 {field_name} 补自同封邮件的 {'、'.join(dict.fromkeys(sources))}",
            field=field_name,
            actual=value,
        )
        for (field_name, value), sources in filenames.items()
    ]


def apply_sibling_context(
    envelope: RawExtractionEnvelope,
    siblings: Sequence[tuple[ParsedDocument, RawExtractionEnvelope]],
    profiles: Sequence[AccountingEntityProfile],
    *,
    source_extraction: RawExtractionEnvelope | None = None,
    merge_mbls: bool = True,
) -> tuple[RawExtractionEnvelope, list[SiblingFieldUse]]:
    """Fill transport references and resolve payment shorthand using sibling evidence."""
    if not siblings:
        return envelope, []
    extracted = envelope.extraction
    own = extracted.shipment_refs
    independent = source_extraction.extraction.shipment_refs if source_extraction else own
    refs = [(doc, candidate.extraction.shipment_refs) for doc, candidate in siblings]
    updates: dict[str, Any] = {}
    uses = []
    own_mbls = {ref.value for ref in independent.mbl_nos}
    sibling_mbls = {}
    containers = list(own.container_nos)
    for doc, source_refs in refs:
        for ref in source_refs.mbl_nos:
            if merge_mbls or not own_mbls or ref.value in own_mbls:
                sibling_mbls[ref.value, ref.source] = ref
                if ref.value not in own_mbls:
                    uses.append(SiblingFieldUse("mbl_nos", ref.value, doc))
        for value in source_refs.container_nos:
            containers.append(value)
            if value not in independent.container_nos:
                uses.append(SiblingFieldUse("container_nos", value, doc))
    borrowed_mbls = {value for value, _ in sibling_mbls} - own_mbls
    mbls = {(ref.value, ref.source): ref for ref in own.mbl_nos if ref.value not in borrowed_mbls}
    mbls.update(sibling_mbls)
    ref_updates: dict[str, Any] = {
        "mbl_nos": list(mbls.values()),
        "container_nos": list(dict.fromkeys(containers)),
    }
    for field_name in ("hbl_no", "booking_no"):
        sibling_values = [
            (doc, value) for doc, item in refs if (value := getattr(item, field_name))
        ]
        values = {value for _, value in sibling_values}
        current = getattr(own, field_name)
        if not current and len(values) == 1:
            current = ref_updates[field_name] = next(iter(values))
        if current and not getattr(independent, field_name):
            uses.extend(
                SiblingFieldUse(field_name, value, doc)
                for doc, value in sibling_values
                if value == current
            )
    merged = own.model_copy(update=ref_updates)
    if merged != own:
        updates["shipment_refs"] = merged
    candidates = _package_parties(siblings)
    for field_name in ("payer_hint", "payee_hint"):
        if (short := getattr(extracted, field_name)) and (
            resolved := _resolve_short_party(short, candidates, profiles)
        ):
            name, sources = resolved
            updates[field_name] = name
            uses.extend(SiblingFieldUse(field_name, name, source) for source in sources)
    charges, currency_uses = _resolve_package_dollars(envelope, siblings, source_extraction)
    uses.extend(currency_uses)
    if charges is not envelope.charges:
        updates["charges"] = [row.charge for row in charges]
    if not updates and not uses:
        return envelope, []
    alerts = _context_alerts([use for use in uses if use.line_index is None])
    return envelope.model_copy(
        update={
            "extraction": extracted.model_copy(update=updates),
            "charges": charges,
            "raw_parse_issues": [*envelope.raw_parse_issues, *alerts],
        }
    ), uses


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
