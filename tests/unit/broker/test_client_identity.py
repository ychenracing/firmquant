from __future__ import annotations

import pytest

from firmquant.broker.client_identity import (
    client_order_tag,
    matches_uquant_order,
    resolve_uquant_order_id,
)


def test_client_order_tag_is_deterministic_ascii_and_fits_miniqmt_limit() -> None:
    first = client_order_tag("O000000123")
    second = client_order_tag("O000000123")

    assert first == second
    assert first.startswith("fq")
    assert first.isascii()
    assert len(first) == 24


def test_client_order_tag_resolves_back_against_known_uquant_ids() -> None:
    known = frozenset({"O000000123", "O000000124"})
    tag = client_order_tag("O000000124")

    assert matches_uquant_order(tag, "O000000124") is True
    assert matches_uquant_order(tag, "O000000123") is False
    assert resolve_uquant_order_id(tag, known) == "O000000124"
    assert resolve_uquant_order_id("O000000123", known) == "O000000123"


def test_unknown_or_ambiguous_client_identity_fails_closed() -> None:
    known = frozenset({"O000000123"})

    with pytest.raises(ValueError, match="not map"):
        resolve_uquant_order_id("manual-order", known)
