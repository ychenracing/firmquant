from __future__ import annotations

from datetime import date

import pytest

from firmquant.domain.values import Symbol
from firmquant.strategy.universe import UniversePolicy, UniverseViolation

CANONICAL_UNIVERSE_SHA256 = "03f42c5066fb8e1c7b2f8e1b7dd38d508d8053f548ebb5596317ce587d7cffd0"


def test_deployment_allowlist_cannot_expand_canonical_universe() -> None:
    with pytest.raises(UniverseViolation, match="exceeds canonical AI universe"):
        UniversePolicy.from_uquant(("sh600519",), as_of=date(2026, 8, 25))


def test_deployment_allowlist_rejects_member_not_active_at_point_in_time() -> None:
    with pytest.raises(UniverseViolation, match="not active on 2023-01-01"):
        UniversePolicy.from_uquant(("sh688146",), as_of=date(2023, 1, 1))


def test_allowed_requires_both_deployment_and_point_in_time_membership() -> None:
    policy = UniversePolicy.from_uquant(
        ("688146.SH", "sh603986"),
        as_of=date(2023, 4, 21),
    )

    assert policy.allowed(Symbol.parse("sh688146"), date(2023, 4, 21)) is True
    assert policy.allowed("sh688146", date(2023, 1, 1)) is False
    assert policy.allowed("sh600519", date(2023, 4, 21)) is False
    assert policy.allowed("not-a-symbol", date(2023, 4, 21)) is False


def test_universe_policy_exposes_only_locked_canonical_identity() -> None:
    policy = UniversePolicy.from_uquant(None, as_of=date(2026, 8, 25))

    assert policy.manifest_sha256 == CANONICAL_UNIVERSE_SHA256
    assert policy.deployment_symbols == (
        "sh600487",
        "sh601869",
        "sh603688",
        "sh603986",
        "sh688008",
        "sh688012",
        "sh688019",
        "sh688037",
        "sh688041",
        "sh688072",
        "sh688082",
        "sh688110",
        "sh688120",
        "sh688146",
        "sh688200",
        "sh688233",
        "sh688256",
        "sh688268",
        "sh688300",
        "sh688347",
        "sh688361",
        "sh688498",
        "sh688766",
        "sz000636",
        "sz002281",
        "sz002371",
        "sz002409",
        "sz300054",
        "sz300223",
        "sz300308",
        "sz300394",
        "sz300502",
        "sz300604",
        "sz300666",
    )
