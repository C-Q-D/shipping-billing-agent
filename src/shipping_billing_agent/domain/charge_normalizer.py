"""Normalize charge names, payment terms, and dates from source documents."""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import date
from unicodedata import normalize

from .models import ChargeDictionaryItem, PaymentTerm

_EDGE_PUNCTUATION = re.compile(r"^[\W_]+|[\W_]+$")
_LEADING_COUNT_PREFIX = re.compile(r"^\d+\s+(?=\S)")
_TRAILING_LANE_SUFFIX = re.compile(r"\s+-\s+[a-z]{2}(?:\s+[a-z]{2,4})?$")
_CHARGE_PUNCTUATION = str.maketrans({"’": "'", "‘": "'", "–": "-", "—": "-"})
_CHARGE_NAMES = {
    "handling": "Handling Charge",
    "handling charge": "Handling Charge",
    "handling fee": "Handling Charge",
    "h/d fee": "Handling Charge",
    "h/d fee rebate": "Handling Charge",
    "h/c": "Handling Charge",
    "hc": "Handling Charge",
    "doc": "Documentation Fee",
    "doc fee": "Documentation Fee",
    "documentation fee": "Documentation Fee",
    "thc": "Terminal Handling Charge",
    "terminal handling": "Terminal Handling Charge",
    "terminal handling fee": "Terminal Handling Charge",
    "terminal handling charge": "Terminal Handling Charge",
    "trucking": "Trucking Charge",
    "trucking fee": "Trucking Charge",
    "trucking charge": "Trucking Charge",
    "customs fee": "Customs Clearance Fee",
    "customs clearance": "Customs Clearance Fee",
    "customs clearance fee": "Customs Clearance Fee",
    "storage": "Storage Charge",
    "storage fee": "Storage Charge",
    "storage charge": "Storage Charge",
    "wharfage": "Wharfage",
    "wharfage administration fee": "Wharfage Administration Fee",
    "wharfage admin fee": "Wharfage Administration Fee",
    "add'l detention free time charge": "Detention Free Time Charge",
    "detention free time charge": "Detention Free Time Charge",
    "detention charge": "Detention Free Time Charge",
    "on carriage additional": "On Carriage Additional",
}
_PAYMENT_TERMS = {
    "P": PaymentTerm.PP,
    "PP": PaymentTerm.PP,
    "PREPAID": PaymentTerm.PP,
    "FREIGHT PREPAID": PaymentTerm.PP,
    "C": PaymentTerm.CC,
    "CC": PaymentTerm.CC,
    "COLLECT": PaymentTerm.CC,
    "FREIGHT COLLECT": PaymentTerm.CC,
}
_YEAR_FIRST_DATE = re.compile(r"([0-9]{4})([-/])([0-9]{1,2})\2([0-9]{1,2})")
_MONTH_FIRST_DATE = re.compile(r"([0-9]{1,2})([-/])([0-9]{1,2})\2([0-9]{4})")


def _charge_key(name: str) -> str:
    text = normalize("NFKC", name).translate(_CHARGE_PUNCTUATION).casefold()
    return " ".join(_EDGE_PUNCTUATION.sub("", text).split())


def _lookup_charge_alias(
    key: str, custom_dictionary: Sequence[ChargeDictionaryItem]
) -> str | None:
    for item in custom_dictionary:
        if _charge_key(item.raw_alias) == key:
            return item.standard_name
    return _CHARGE_NAMES.get(key)


def normalize_charge_item(
    raw_name: str | None, custom_dictionary: Sequence[ChargeDictionaryItem] = ()
) -> tuple[str | None, bool]:
    key = _charge_key(raw_name or "")
    if not key:
        return None, False
    candidates = [key]
    no_prefix = _LEADING_COUNT_PREFIX.sub("", key).strip()
    no_lane = _TRAILING_LANE_SUFFIX.sub("", key).strip()
    no_both = _TRAILING_LANE_SUFFIX.sub("", no_prefix).strip()
    for candidate in (no_prefix, no_lane, no_both):
        if candidate and candidate not in candidates:
            candidates.append(candidate)
    for candidate in candidates:
        if matched := _lookup_charge_alias(candidate, custom_dictionary):
            return matched, True
    return key.title(), False


def normalize_payment_term(raw_term: str | None) -> PaymentTerm | None:
    term = " ".join(normalize("NFKC", raw_term or "").upper().split())
    return _PAYMENT_TERMS.get(term)


def normalize_document_date(raw_date: str | None) -> tuple[str | None, str | None]:
    if raw_date is None:
        return None, None
    text = raw_date.strip()
    if match := _YEAR_FIRST_DATE.fullmatch(text):
        year, _, month, day = match.groups()
    elif match := _MONTH_FIRST_DATE.fullmatch(text):
        month, _, day, year = match.groups()
    else:
        return None, "单据日期格式无效"
    try:
        return date(int(year), int(month), int(day)).isoformat(), None
    except ValueError:
        return None, "单据日期不是有效的日历日期"
