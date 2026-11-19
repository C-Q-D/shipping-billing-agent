"""Remove remittance credentials and signed URL parameters from LLM input."""

from __future__ import annotations

import re
from urllib.parse import unquote_plus

from shipping_billing_agent.gateways.url_guard import HTTP_URL_PATTERN

_BANK_MASK = "[REDACTED_BANK_ACCT]"
_QUERY_KEYS = {
    "access_token",
    "awsaccesskeyid",
    "googleaccessid",
    "ossaccesskeyid",
    "security_token",
    "session_token",
    "sig",
    "signature",
    "token",
}
_QUERY_PREFIXES = ("x_amz_", "x_goog_", "x_oss_")

_ACCOUNT_LABEL = (
    r"\b(?:(?:bank|beneficiary)[ \t]+account(?:[ \t]+(?:no\.?|number))?"
    r"|(?:account|acct\.?|a/c)[ \t]*(?:no\.?|number|(?=[#:]))"
    r"|(?:routing|aba)(?:[ \t]+(?:no\.?|number|code))?)(?!\w)"
    r"|(?:银行|汇款|收款)(?:账号|帐号|账户)"
)
_BIC_LABEL = r"\b(?:swift(?:[ \t]*/[ \t]*bic)?|bic)(?:[ \t]+(?:code|no\.?|number))?(?!\w)"
_FIELD_SEPARATOR = r"[ \t]*[:：#=]?[ \t]*(?:\r?\n[ \t]*)?"
_BANK_FIELDS = re.compile(
    rf"(?P<label>{_ACCOUNT_LABEL}){_FIELD_SEPARATOR}"
    r"(?P<value>(?=(?:[0-9][ \t-]?){4}[0-9])[0-9]+"
    r"(?:[ \t-][0-9]+(?!\w|[.,][0-9]))*)(?!\w|[.,][0-9])",
    re.IGNORECASE,
)
_BIC_FIELD = re.compile(
    rf"(?P<label>{_BIC_LABEL}){_FIELD_SEPARATOR}"
    r"(?P<value>[A-Z]{6}[A-Z0-9]{2}(?:[A-Z0-9]{3})?)(?![A-Z0-9])",
    re.IGNORECASE,
)
_IBAN_FIELD = re.compile(
    rf"(?P<label>\biban(?:[ \t]+(?:no\.?|number))?(?!\w)){_FIELD_SEPARATOR}"
    r"(?P<value>[A-Z]{2}[0-9]{2}(?:[A-Z0-9]{11,30}"
    r"|(?:[ \t]+(?!(?:rate|date|bill)\b)[A-Z0-9]{4}(?!\w|[.,][0-9])){2,7}"
    r"(?:[ \t]+(?=[A-Z0-9]*[0-9])[A-Z0-9]{1,3})?))(?!\w|[.,][0-9])",
    re.IGNORECASE,
)
_BANK_HEADING = re.compile(
    r"^[ \t]*(?:(?:(?:beneficiary|intermediary)\s+)?bank\b|remit(?:tance)?\s+to\b"
    r"|开户行|收款银行|汇款至)",
    re.IGNORECASE,
)
_UNLABELED_ACCOUNT = re.compile(r"(?<![\w.,-])(?:[0-9][ \t-]?){8,29}[0-9][ \t]*$")
_BUSINESS_FIELD = re.compile(
    r"\b(?:invoice|bill|b/?l|mbl|hbl|booking|container|amount|total|rate|date|shipper|"
    r"consignee|reference|usd|cny|eur|rmb|payment)\b|单号|金额|费率|日期|箱号",
    re.IGNORECASE,
)


def _sanitize_signed_url(match: re.Match[str]) -> str:
    url, fragment_separator, fragment = match[0].partition("#")
    base_url, query_separator, query = url.partition("?")
    if not query_separator:
        return match[0]

    parts = re.split(r"(&amp;|&)", query, flags=re.IGNORECASE)
    for index in range(0, len(parts), 2):
        name, equals, _ = parts[index].partition("=")
        key = unquote_plus(name).lower().replace("-", "_")
        if equals and (key in _QUERY_KEYS or key.startswith(_QUERY_PREFIXES)):
            parts[index] = f"{name}=[REDACTED]"
    return f"{base_url}?{''.join(parts)}{fragment_separator}{fragment}"


def _mask_field_value(match: re.Match[str]) -> str:
    return match.string[match.start() : match.start("value")] + _BANK_MASK


def _mask_unlabeled_accounts(text: str) -> str:
    lines: list[str] = []
    in_remittance_section = False
    previous_line = ""
    for line in text.splitlines(keepends=True):
        value = line.rstrip("\r\n")
        if not value.strip():
            in_remittance_section = False
        elif _BANK_HEADING.match(value):
            in_remittance_section = True
            number = _UNLABELED_ACCOUNT.search(value)
            if number and not _BUSINESS_FIELD.search(value):
                line = value[: number.start()] + _BANK_MASK + line[len(value) :]
        elif in_remittance_section and not _BUSINESS_FIELD.search(previous_line):
            number = _UNLABELED_ACCOUNT.fullmatch(value.lstrip())
            if number:
                indentation = value[: len(value) - len(value.lstrip())]
                line = indentation + _BANK_MASK + line[len(value) :]
        lines.append(line)
        previous_line = value
    return "".join(lines)


def sanitize_for_llm(text: str) -> str:
    text = HTTP_URL_PATTERN.sub(_sanitize_signed_url, text)
    text = _BANK_FIELDS.sub(_mask_field_value, text)
    text = _BIC_FIELD.sub(_mask_field_value, text)
    text = _IBAN_FIELD.sub(_mask_field_value, text)
    return _mask_unlabeled_accounts(text)
