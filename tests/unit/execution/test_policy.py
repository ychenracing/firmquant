from __future__ import annotations

from decimal import Decimal

import pytest

from firmquant.domain.broker_facts import Side
from firmquant.domain.errors import DomainTypeError, DomainValidationError
from firmquant.domain.values import Price, Shares
from firmquant.execution.policy import ExecutionPolicy, FeeSchedule, FillModel


def _fees() -> FeeSchedule:
    return FeeSchedule(
        commission_rate=Decimal("0.0003"),
        minimum_commission=Decimal("5.00"),
        stamp_duty_rate=Decimal("0.001"),
        transfer_fee_rate=Decimal("0.00001"),
        fee_quantum=Decimal("0.01"),
    )


def test_fee_schedule_uses_decimal_and_applies_stamp_duty_only_to_sell() -> None:
    schedule = _fees()

    buy = schedule.calculate(side=Side.BUY, price=Price(Decimal("10")), shares=Shares(1000))
    sell = schedule.calculate(side=Side.SELL, price=Price(Decimal("10")), shares=Shares(1000))

    assert buy.commission.value == Decimal("5.00")
    assert buy.stamp_duty.value == Decimal("0.00")
    assert buy.transfer_fee.value == Decimal("0.10")
    assert sell.commission.value == Decimal("5.00")
    assert sell.stamp_duty.value == Decimal("10.00")
    assert sell.total.value == Decimal("15.10")


@pytest.mark.parametrize("unsafe", [0.0003, float("nan"), True, 1])
def test_fee_schedule_rejects_non_decimal_rates(unsafe: object) -> None:
    with pytest.raises((DomainTypeError, DomainValidationError)):
        FeeSchedule(
            commission_rate=unsafe,  # type: ignore[arg-type]
            minimum_commission=Decimal("5.00"),
            stamp_duty_rate=Decimal("0.001"),
            transfer_fee_rate=Decimal("0.00001"),
            fee_quantum=Decimal("0.01"),
        )


def test_fill_model_and_execution_policy_are_explicit_and_bounded() -> None:
    model = FillModel(
        max_volume_participation=Decimal("0.005"),
        slippage_bps=Decimal("10"),
    )
    policy = ExecutionPolicy(fill_model=model, fee_schedule=_fees())

    assert policy.allow_auction is False
    assert model.max_volume_participation == Decimal("0.005")
    with pytest.raises(DomainValidationError, match="participation"):
        FillModel(
            max_volume_participation=Decimal("1.01"),
            slippage_bps=Decimal("0"),
        )
