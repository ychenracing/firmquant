"""Strict A-share identifiers and Decimal-only economic value objects."""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Final

from .errors import DomainTypeError, DomainValidationError

_PREFIX_SYMBOL: Final = re.compile(r"^(sh|sz|bj)\.?([0-9]{6})$")
_SUFFIX_SYMBOL: Final = re.compile(r"^([0-9]{6})\.?(sh|sz|bj)$")
_BARE_SYMBOL: Final = re.compile(r"^[0-9]{6}$")
_MAX_INTEGER_DIGITS: Final = 20


class Market(StrEnum):
    """Supported mainland cash-equity listing markets."""

    SH = "SH"
    SZ = "SZ"
    BJ = "BJ"


@dataclass(frozen=True, slots=True, order=True)
class Symbol:
    """Canonical A-share symbol preserving an explicit listing market."""

    market: Market
    code: str

    def __post_init__(self) -> None:
        if not isinstance(self.market, Market):
            raise DomainTypeError("symbol market must be a Market")
        if not isinstance(self.code, str) or _BARE_SYMBOL.fullmatch(self.code) is None:
            raise DomainValidationError("invalid A-share symbol code")

    @classmethod
    def parse(cls, raw: str) -> Symbol:
        """Normalize strict prefix, suffix, canonical, or six-digit A-share notation."""

        if not isinstance(raw, str):
            raise DomainTypeError("A-share symbol must be text")
        value = raw.strip().lower()
        prefix = _PREFIX_SYMBOL.fullmatch(value)
        if prefix is not None:
            market, code = prefix.groups()
            return cls(market=Market(market.upper()), code=code)
        suffix = _SUFFIX_SYMBOL.fullmatch(value)
        if suffix is not None:
            code, market = suffix.groups()
            return cls(market=Market(market.upper()), code=code)
        if _BARE_SYMBOL.fullmatch(value) is None:
            raise DomainValidationError(f"invalid A-share symbol: {raw!r}")
        if value in {"000300", "000682"} or value.startswith(("6", "9")):
            market = Market.SH
        elif value.startswith(("4", "8")):
            market = Market.BJ
        else:
            market = Market.SZ
        return cls(market=market, code=value)

    @property
    def canonical(self) -> str:
        return f"{self.market.value.lower()}{self.code}"

    @property
    def xtquant(self) -> str:
        return f"{self.code}.{self.market.value}"

    def __str__(self) -> str:
        return self.canonical


def _validate_decimal(
    value: object,
    *,
    label: str,
    allow_zero: bool,
    max_decimal_places: int,
) -> Decimal:
    if not isinstance(value, Decimal):
        raise DomainTypeError(f"{label} must be Decimal, not binary float or implicit integer")
    if not value.is_finite():
        raise DomainValidationError(f"{label} must be finite")
    if value < 0 or (not allow_zero and value == 0):
        qualifier = "nonnegative" if allow_zero else "positive"
        raise DomainValidationError(f"{label} must be {qualifier}")
    if value == 0:
        return Decimal(0)
    exponent = value.as_tuple().exponent
    if not isinstance(exponent, int):
        raise DomainValidationError(f"{label} must have a finite decimal exponent")
    decimal_places = max(0, -exponent)
    if decimal_places > max_decimal_places:
        raise DomainValidationError(
            f"{label} exceeds {max_decimal_places} decimal places"
        )
    integer_digits = max(1, value.copy_abs().adjusted() + 1)
    if integer_digits > _MAX_INTEGER_DIGITS:
        raise DomainValidationError(f"{label} exceeds {_MAX_INTEGER_DIGITS} integer digits")
    return value


def _canonical_decimal(value: Decimal) -> str:
    rendered = format(value, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered or "0"


@dataclass(frozen=True, slots=True, order=True)
class Money:
    """Finite nonnegative CNY amount with bounded ledger precision."""

    value: Decimal

    def __post_init__(self) -> None:
        validated = _validate_decimal(
            self.value,
            label="money",
            allow_zero=True,
            max_decimal_places=4,
        )
        object.__setattr__(self, "value", validated)

    @property
    def canonical(self) -> str:
        return _canonical_decimal(self.value)


@dataclass(frozen=True, slots=True, order=True)
class Price:
    """Finite positive price; instrument metadata owns the effective tick."""

    value: Decimal

    def __post_init__(self) -> None:
        validated = _validate_decimal(
            self.value,
            label="price",
            allow_zero=False,
            max_decimal_places=8,
        )
        object.__setattr__(self, "value", validated)

    @property
    def canonical(self) -> str:
        return _canonical_decimal(self.value)

    @property
    def decimal_places(self) -> int:
        exponent = self.value.as_tuple().exponent
        if not isinstance(exponent, int):
            raise DomainValidationError("price must have a finite decimal exponent")
        return max(0, -exponent)


@dataclass(frozen=True, slots=True, order=True)
class Shares:
    """Nonnegative whole-share quantity that rejects bool masquerading as int."""

    value: int

    def __post_init__(self) -> None:
        if isinstance(self.value, bool) or not isinstance(self.value, int):
            raise DomainTypeError("shares must be an integer")
        if self.value < 0:
            raise DomainValidationError("shares must be nonnegative")

    @property
    def is_positive(self) -> bool:
        return self.value > 0

    def __int__(self) -> int:
        return self.value


__all__ = ("Market", "Money", "Price", "Shares", "Symbol")
