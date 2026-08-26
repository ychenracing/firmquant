from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from firmquant.application.promotion import ShadowPromotionEvidence


def _evidence(*, sessions: int = 20, orders: int = 50) -> ShadowPromotionEvidence:
    return ShadowPromotionEvidence(
        firmquant_commit="f" * 40,
        uquant_commit="u" * 40,
        config_sha256="c" * 64,
        account_hash="a" * 64,
        observed_sessions=sessions,
        hypothetical_orders=orders,
        unresolved_orders=0,
        external_orders=0,
        duplicate_economic_orders=0,
        duplicate_fills=0,
        max_target_tracking_error=Decimal("0.02"),
        created_at=datetime(2026, 8, 25, 8, 0, tzinfo=UTC),
    )


def test_shadow_promotion_requires_identity_bound_multi_session_evidence() -> None:
    evidence = _evidence()

    assert evidence.qualifies(
        firmquant_commit="f" * 40,
        uquant_commit="u" * 40,
        config_sha256="c" * 64,
        account_hash="a" * 64,
        min_sessions=20,
        min_orders=50,
        max_tracking_error=Decimal("0.05"),
    )


def test_shadow_promotion_rejects_stale_identity_or_insufficient_observation() -> None:
    evidence = _evidence(sessions=19, orders=49)

    assert not evidence.qualifies(
        firmquant_commit="f" * 40,
        uquant_commit="u" * 40,
        config_sha256="c" * 64,
        account_hash="a" * 64,
        min_sessions=20,
        min_orders=50,
        max_tracking_error=Decimal("0.05"),
    )
    assert not _evidence().qualifies(
        firmquant_commit="0" * 40,
        uquant_commit="u" * 40,
        config_sha256="c" * 64,
        account_hash="a" * 64,
        min_sessions=20,
        min_orders=50,
        max_tracking_error=Decimal("0.05"),
    )
