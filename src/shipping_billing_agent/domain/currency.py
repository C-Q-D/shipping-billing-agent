"""Validate ISO 4217 codes and normalize currency tokens from billing documents."""

from __future__ import annotations

from collections.abc import Iterable

from iso4217 import Currency

ACTIVE_ISO4217_CODES = frozenset(currency.code for currency in Currency) - {"XXX", "XTS"}

_CURRENCY_ALIASES = {"RMB": "CNY", "€": "EUR", "£": "GBP", "US$": "USD"}


def is_valid_iso4217(code: str | None) -> bool:
    return code in ACTIVE_ISO4217_CODES


def _canonical_token(raw: str) -> str:
    token = raw.strip().upper()
    return _CURRENCY_ALIASES.get(token, token)


def normalize_currency(
    raw: str | None,
    *,
    context_currencies: Iterable[str] = (),
    us_shipping_context: bool = False,
) -> tuple[str | None, str | None]:
    """Return the canonical currency and a reason for an inference or ambiguity."""
    token = _canonical_token(raw or "")
    if not token:
        return None, None
    if is_valid_iso4217(token):
        return token, None
    if token != "$":
        return None, "无法识别币种代码，待复核"

    known_currencies = ACTIVE_ISO4217_CODES.intersection(
        _canonical_token(code) for code in context_currencies
    )
    if known_currencies == {"USD"}:
        return "USD", "同单币种仅指向美元，将 $ 识别为 USD"
    if not known_currencies and us_shipping_context:
        return "USD", "根据美线运输上下文，将 $ 识别为 USD"
    return None, "币种符号 $ 存在多币种歧义，待复核"
