from __future__ import annotations

import dataclasses
from dataclasses import replace

import pytest

from firmquant.strategy.identity import StrategyIdentity, StrategyIdentityViolation

EXPECTED_UQUANT_COMMIT = "105695aacd3d1c7e62705f64188da88d202db4cd"
EXPECTED_CODE_FINGERPRINT = "2209a539bacbc01d90b29b9f0bb78ace4991016bee0d41f9e86f38ccf5af545e"
EXPECTED_UNIVERSE_SHA256 = "03f42c5066fb8e1c7b2f8e1b7dd38d508d8053f548ebb5596317ce587d7cffd0"


def test_locked_strategy_identity_verifies_the_installed_uquant_payload() -> None:
    identity = StrategyIdentity.locked()

    identity.verify()

    assert identity.uquant_commit == EXPECTED_UQUANT_COMMIT
    assert identity.economic_code_fingerprint == EXPECTED_CODE_FINGERPRINT
    assert identity.canonical_universe_sha256 == EXPECTED_UNIVERSE_SHA256


def test_strategy_identity_rejects_an_unreviewed_economic_fingerprint() -> None:
    identity = replace(StrategyIdentity.locked(), economic_code_fingerprint="0" * 64)

    with pytest.raises(StrategyIdentityViolation, match="economic code fingerprint"):
        identity.verify()


def test_strategy_identity_is_immutable() -> None:
    identity = StrategyIdentity.locked()

    with pytest.raises(dataclasses.FrozenInstanceError):
        identity.uquant_commit = "0" * 40  # type: ignore[misc]
