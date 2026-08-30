from __future__ import annotations

import dataclasses
from dataclasses import replace

import pytest

from firmquant.strategy.identity import StrategyIdentity, StrategyIdentityViolation

EXPECTED_UQUANT_COMMIT = "a17322f6330953a27c77f70d463a713c9a48ebc9"
EXPECTED_CODE_FINGERPRINT = "d1ef7977ae482e46a920381e6af58791199ec8e1a02586dbe8df451e7d4696c9"
EXPECTED_PUBLIC_API_CONTRACT_SHA256 = "b485932a5eb10b0528c2d01008c6495f8f2e1e74ead04c737cafd9c665efa6b5"
EXPECTED_UNIVERSE_SHA256 = "03f42c5066fb8e1c7b2f8e1b7dd38d508d8053f548ebb5596317ce587d7cffd0"


def test_locked_strategy_identity_verifies_the_installed_uquant_payload() -> None:
    identity = StrategyIdentity.locked()

    identity.verify()

    assert identity.uquant_commit == EXPECTED_UQUANT_COMMIT
    assert identity.economic_code_fingerprint == EXPECTED_CODE_FINGERPRINT
    assert identity.public_api_contract_sha256 == EXPECTED_PUBLIC_API_CONTRACT_SHA256
    assert identity.canonical_universe_sha256 == EXPECTED_UNIVERSE_SHA256


def test_strategy_identity_rejects_an_unreviewed_economic_fingerprint() -> None:
    identity = replace(StrategyIdentity.locked(), economic_code_fingerprint="0" * 64)

    with pytest.raises(StrategyIdentityViolation, match="economic code fingerprint"):
        identity.verify()


def test_strategy_identity_is_immutable() -> None:
    identity = StrategyIdentity.locked()

    with pytest.raises(dataclasses.FrozenInstanceError):
        identity.uquant_commit = "0" * 40  # type: ignore[misc]
