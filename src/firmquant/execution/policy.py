"""Explicit deployment execution assumptions used by safe simulation and routing."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from firmquant.domain.broker_facts import Side
from firmquant.domain.errors import DomainTypeError, DomainValidationError
from firmquant.domain.values import Money, Price, Shares


def _decimal(
    value: object,
    *,
    label: str,
    minimum: Decimal,
    maximum: Decimal,
    maximum_places: int = 8,
) -> Decimal:
    if not isinstance(value, Decimal):
        raise DomainTypeError(f"{label} must be Decimal")
    if not value.is_finite() or not minimum <= value <= maximum:
        raise DomainValidationError(f"{label} must be finite and between {minimum} and {maximum}")
    exponent = value.as_tuple().exponent
    if not isinstance(exponent, int) or max(0, -exponent) > maximum_places:
        raise DomainValidationError(f"{label} exceeds {maximum_places} decimal places")
    return value


@dataclass(frozen=True, slots=True)
class FeeBreakdown:
    commission: Money
    stamp_duty: Money
    transfer_fee: Money

    def __post_init__(self) -> None:
        if not all(
            isinstance(value, Money) for value in (self.commission, self.stamp_duty, self.transfer_fee)
        ):
            raise DomainTypeError("fee breakdown values must be Money")

    @property
    def total(self) -> Money:
        return Money(self.commission.value + self.stamp_duty.value + self.transfer_fee.value)


@dataclass(frozen=True, slots=True)
class FeeSchedule:
    """Paper fee assumptions; every value is explicit and Decimal-only."""

    commission_rate: Decimal
    minimum_commission: Decimal
    stamp_duty_rate: Decimal
    transfer_fee_rate: Decimal
    fee_quantum: Decimal

    def __post_init__(self) -> None:
        for label, value in (
            ("commission rate", self.commission_rate),
            ("stamp duty rate", self.stamp_duty_rate),
            ("transfer fee rate", self.transfer_fee_rate),
        ):
            _decimal(
                value,
                label=label,
                minimum=Decimal(0),
                maximum=Decimal(1),
            )
        minimum = _decimal(
            self.minimum_commission,
            label="minimum commission",
            minimum=Decimal(0),
            maximum=Decimal("1000000"),
            maximum_places=4,
        )
        quantum = _decimal(
            self.fee_quantum,
            label="fee quantum",
            minimum=Decimal("0.0001"),
            maximum=Decimal(1),
            maximum_places=4,
        )
        Money(minimum)
        Money(quantum)

    def _round(self, value: Decimal) -> Decimal:
        try:
            return value.quantize(self.fee_quantum, rounding=ROUND_HALF_UP)
        except InvalidOperation as error:
            raise DomainValidationError("fee calculation exceeds Decimal bounds") from error

    def calculate(self, *, side: Side, price: Price, shares: Shares) -> FeeBreakdown:
        if not isinstance(side, Side):
            raise DomainTypeError("fee side must be Side")
        if not isinstance(price, Price):
            raise DomainTypeError("fee price must be Price")
        if not isinstance(shares, Shares) or not shares.is_positive:
            raise DomainValidationError("fee shares must be positive Shares")
        gross = price.value * shares.value
        commission = self._round(max(self.minimum_commission, gross * self.commission_rate))
        stamp_duty = self._round(gross * self.stamp_duty_rate if side is Side.SELL else Decimal(0))
        transfer_fee = self._round(gross * self.transfer_fee_rate)
        return FeeBreakdown(
            commission=Money(commission),
            stamp_duty=Money(stamp_duty),
            transfer_fee=Money(transfer_fee),
        )


@dataclass(frozen=True, slots=True)
class FillModel:
    """Bounded deterministic paper liquidity and price-impact assumptions."""

    max_volume_participation: Decimal
    slippage_bps: Decimal

    def __post_init__(self) -> None:
        participation = _decimal(
            self.max_volume_participation,
            label="volume participation",
            minimum=Decimal(0),
            maximum=Decimal(1),
        )
        if participation == 0:
            raise DomainValidationError("volume participation must be greater than zero")
        _decimal(
            self.slippage_bps,
            label="slippage bps",
            minimum=Decimal(0),
            maximum=Decimal("1000"),
            maximum_places=4,
        )


@dataclass(frozen=True, slots=True)
class ExecutionPolicy:
    fill_model: FillModel
    fee_schedule: FeeSchedule
    allow_auction: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.fill_model, FillModel):
            raise DomainTypeError("execution fill model must be FillModel")
        if not isinstance(self.fee_schedule, FeeSchedule):
            raise DomainTypeError("execution fee schedule must be FeeSchedule")
        if not isinstance(self.allow_auction, bool):
            raise DomainTypeError("execution allow_auction must be bool")


__all__ = ("ExecutionPolicy", "FeeBreakdown", "FeeSchedule", "FillModel")
