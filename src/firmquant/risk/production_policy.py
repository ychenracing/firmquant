"""Code-owned production safety bounds that configuration may only tighten."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, ClassVar

if TYPE_CHECKING:
    from firmquant.config import Settings


_MAX_PLAIN_DECIMAL_LENGTH = 128


def canonical_decimal_text(value: Decimal) -> str:
    """Render a finite Decimal exactly without context rounding or unbounded expansion."""

    if not isinstance(value, Decimal) or not value.is_finite():
        raise TypeError("canonical Decimal text requires a finite Decimal")
    if value.is_zero():
        return "0"
    sign, raw_digits, raw_exponent = value.as_tuple()
    if not isinstance(raw_exponent, int):
        raise TypeError("canonical Decimal text requires a finite Decimal")
    digits = list(raw_digits)
    exponent = raw_exponent
    while len(digits) > 1 and digits[-1] == 0:
        digits.pop()
        exponent += 1
    coefficient = "".join(str(digit) for digit in digits)
    prefix = "-" if sign else ""
    decimal_index = len(coefficient) + exponent
    if exponent >= 0:
        plain_length = len(prefix) + len(coefficient) + exponent
    elif decimal_index > 0:
        plain_length = len(prefix) + len(coefficient) + 1
    else:
        plain_length = len(prefix) + 2 + (-decimal_index) + len(coefficient)
    if plain_length <= _MAX_PLAIN_DECIMAL_LENGTH:
        if exponent >= 0:
            return prefix + coefficient + ("0" * exponent)
        if decimal_index > 0:
            return prefix + coefficient[:decimal_index] + "." + coefficient[decimal_index:]
        return prefix + "0." + ("0" * (-decimal_index)) + coefficient
    mantissa = coefficient[0]
    if len(coefficient) > 1:
        mantissa += "." + coefficient[1:]
    adjusted_exponent = len(coefficient) + exponent - 1
    return prefix + mantissa + "e" + str(adjusted_exponent)


@dataclass(frozen=True, slots=True)
class ProductionSafetyPolicy:
    """Validated effective safety policy for one deployment configuration."""

    MAX_QUOTE_AGE_SECONDS: ClassVar[int] = 5
    MAX_CLOCK_DRIFT_SECONDS: ClassVar[int] = 2
    MAX_DISCONNECT_SECONDS: ClassVar[int] = 30
    MAX_PRICE_DEVIATION_BPS: ClassVar[Decimal] = Decimal("200")
    MAX_EQUITY_CHANGE_FRACTION: ClassVar[Decimal] = Decimal("0.10")
    MAX_INTRADAY_LOSS_FRACTION: ClassVar[Decimal] = Decimal("0.08")
    MAX_CAPITAL_DRAWDOWN_FRACTION: ClassVar[Decimal] = Decimal("0.25")
    MIN_SHADOW_SESSIONS: ClassVar[int] = 20
    MIN_SHADOW_ORDERS: ClassVar[int] = 50
    MAX_TARGET_TRACKING_ERROR: ClassVar[Decimal] = Decimal("0.05")
    MIN_CANARY_SESSIONS: ClassVar[int] = 3
    MIN_CANARY_ORDERS: ClassVar[int] = 3
    MIN_CANARY_FILLS: ClassVar[int] = 1
    MAX_CANARY_TARGET_TRACKING_ERROR: ClassVar[Decimal] = Decimal("0.05")
    MAX_ARM_TTL_SECONDS: ClassVar[int] = 900

    max_quote_age_seconds: int
    max_clock_drift_seconds: int
    max_disconnect_seconds: int
    max_price_deviation_bps: Decimal
    max_equity_change_fraction: Decimal
    max_intraday_loss_fraction: Decimal
    max_capital_drawdown_fraction: Decimal
    min_shadow_sessions: int
    min_shadow_orders: int
    max_target_tracking_error: Decimal
    min_canary_sessions: int
    min_canary_orders: int
    min_canary_fills: int
    max_canary_target_tracking_error: Decimal
    max_arm_ttl_seconds: int

    def __post_init__(self) -> None:
        from firmquant.config import ConfigurationError

        relaxed = (
            self.max_quote_age_seconds > self.MAX_QUOTE_AGE_SECONDS
            or self.max_clock_drift_seconds > self.MAX_CLOCK_DRIFT_SECONDS
            or self.max_disconnect_seconds > self.MAX_DISCONNECT_SECONDS
            or self.max_price_deviation_bps > self.MAX_PRICE_DEVIATION_BPS
            or self.max_equity_change_fraction > self.MAX_EQUITY_CHANGE_FRACTION
            or self.max_intraday_loss_fraction > self.MAX_INTRADAY_LOSS_FRACTION
            or self.max_capital_drawdown_fraction > self.MAX_CAPITAL_DRAWDOWN_FRACTION
            or self.min_shadow_sessions < self.MIN_SHADOW_SESSIONS
            or self.min_shadow_orders < self.MIN_SHADOW_ORDERS
            or self.max_target_tracking_error > self.MAX_TARGET_TRACKING_ERROR
            or self.min_canary_sessions < self.MIN_CANARY_SESSIONS
            or self.min_canary_orders < self.MIN_CANARY_ORDERS
            or self.min_canary_fills < self.MIN_CANARY_FILLS
            or self.max_canary_target_tracking_error > self.MAX_CANARY_TARGET_TRACKING_ERROR
            or self.max_arm_ttl_seconds > self.MAX_ARM_TTL_SECONDS
        )
        if relaxed:
            raise ConfigurationError("PRODUCTION_SAFETY_POLICY_RELAXATION")

    @classmethod
    def from_settings(cls, settings: Settings) -> ProductionSafetyPolicy:
        """Validate and return the effective policy carried by ``settings``."""

        from firmquant.config import Settings

        if not isinstance(settings, Settings):
            raise TypeError("production safety policy requires Settings")
        execution = settings.execution
        promotion = settings.promotion
        return cls(
            max_quote_age_seconds=execution.max_quote_age_seconds,
            max_clock_drift_seconds=execution.max_clock_drift_seconds,
            max_disconnect_seconds=execution.max_disconnect_seconds,
            max_price_deviation_bps=execution.max_price_deviation_bps,
            max_equity_change_fraction=execution.max_equity_change_fraction,
            max_intraday_loss_fraction=execution.max_intraday_loss_fraction,
            max_capital_drawdown_fraction=execution.max_capital_drawdown_fraction,
            min_shadow_sessions=promotion.min_shadow_sessions,
            min_shadow_orders=promotion.min_shadow_orders,
            max_target_tracking_error=promotion.max_target_tracking_error,
            min_canary_sessions=promotion.min_canary_sessions,
            min_canary_orders=promotion.min_canary_orders,
            min_canary_fills=promotion.min_canary_fills,
            max_canary_target_tracking_error=promotion.max_canary_target_tracking_error,
            max_arm_ttl_seconds=execution.max_arm_ttl_seconds,
        )

    def payload(self) -> dict[str, object]:
        return {
            "schema": "firmquant.production-safety-policy.v1",
            "max_quote_age_seconds": self.max_quote_age_seconds,
            "max_clock_drift_seconds": self.max_clock_drift_seconds,
            "max_disconnect_seconds": self.max_disconnect_seconds,
            "max_price_deviation_bps": canonical_decimal_text(self.max_price_deviation_bps),
            "max_equity_change_fraction": canonical_decimal_text(self.max_equity_change_fraction),
            "max_intraday_loss_fraction": canonical_decimal_text(self.max_intraday_loss_fraction),
            "max_capital_drawdown_fraction": canonical_decimal_text(self.max_capital_drawdown_fraction),
            "min_shadow_sessions": self.min_shadow_sessions,
            "min_shadow_orders": self.min_shadow_orders,
            "max_target_tracking_error": canonical_decimal_text(self.max_target_tracking_error),
            "min_canary_sessions": self.min_canary_sessions,
            "min_canary_orders": self.min_canary_orders,
            "min_canary_fills": self.min_canary_fills,
            "max_canary_target_tracking_error": canonical_decimal_text(self.max_canary_target_tracking_error),
            "max_arm_ttl_seconds": self.max_arm_ttl_seconds,
        }

    @property
    def sha256(self) -> str:
        encoded = json.dumps(
            self.payload(),
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


__all__ = ("ProductionSafetyPolicy", "canonical_decimal_text")
