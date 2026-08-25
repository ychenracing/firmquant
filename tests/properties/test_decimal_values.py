from __future__ import annotations

from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from firmquant.domain.errors import DomainValidationError
from firmquant.domain.values import Money, Price, Shares


@given(st.decimals(allow_nan=True, allow_infinity=True))
def test_nonfinite_or_negative_money_never_enters_domain(value: Decimal) -> None:
    if not value.is_finite() or value < 0:
        with pytest.raises(DomainValidationError):
            Money(value)


@given(
    st.decimals(
        min_value=Decimal("0.00000001"),
        max_value=Decimal("99999999999999999999"),
        allow_nan=False,
        allow_infinity=False,
        places=8,
    )
)
def test_bounded_positive_decimal_price_round_trips_exactly(value: Decimal) -> None:
    price = Price(value)

    assert Decimal(price.canonical) == value
    assert price.value == value


@given(st.floats(allow_nan=True, allow_infinity=True))
def test_binary_float_never_enters_price_boundary(value: float) -> None:
    with pytest.raises(TypeError):
        Price(value)  # type: ignore[arg-type]


@given(st.integers(max_value=-1))
def test_negative_shares_never_enter_domain(value: int) -> None:
    with pytest.raises(DomainValidationError):
        Shares(value)
