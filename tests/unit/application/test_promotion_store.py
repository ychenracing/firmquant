from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from firmquant.application.promotion import ShadowPromotionEvidence
from firmquant.application.promotion_store import PromotionStore
from firmquant.persistence.database import Database

NOW = datetime(2026, 8, 25, 8, 0, tzinfo=UTC)


def evidence(*, sessions: int, orders: int, config: str = "c" * 64) -> ShadowPromotionEvidence:
    return ShadowPromotionEvidence(
        firmquant_commit="f" * 40,
        uquant_commit="1" * 40,
        config_sha256=config,
        account_hash="a" * 64,
        observed_sessions=sessions,
        hypothetical_orders=orders,
        unresolved_orders=0,
        external_orders=0,
        duplicate_economic_orders=0,
        duplicate_fills=0,
        max_target_tracking_error=Decimal("0.01"),
        created_at=NOW,
    )


def test_promotion_store_appends_and_loads_latest_matching_identity(tmp_path: Path) -> None:
    database = Database.open(tmp_path / "firmquant.sqlite3")
    try:
        store = PromotionStore(database)
        first = evidence(sessions=5, orders=10)
        latest = evidence(sessions=20, orders=60)
        other = evidence(sessions=30, orders=100, config="d" * 64)

        assert store.append(first) is True
        assert store.append(first) is False
        assert store.append(latest) is True
        assert store.append(other) is True

        loaded = store.latest(
            firmquant_commit="f" * 40,
            uquant_commit="1" * 40,
            config_sha256="c" * 64,
            account_hash="a" * 64,
        )
        assert loaded == latest
        assert database.scalar("SELECT count(*) FROM audit_events WHERE category = 'SHADOW_PROMOTION'") == 3
    finally:
        database.close()


def test_promotion_store_qualifies_only_current_identity_and_thresholds(tmp_path: Path) -> None:
    database = Database.open(tmp_path / "firmquant.sqlite3")
    try:
        store = PromotionStore(database)
        store.append(evidence(sessions=20, orders=60))

        assert store.qualifies(
            firmquant_commit="f" * 40,
            uquant_commit="1" * 40,
            config_sha256="c" * 64,
            account_hash="a" * 64,
            min_sessions=20,
            min_orders=50,
            max_tracking_error=Decimal("0.05"),
        )
        assert not store.qualifies(
            firmquant_commit="f" * 40,
            uquant_commit="1" * 40,
            config_sha256="d" * 64,
            account_hash="a" * 64,
            min_sessions=20,
            min_orders=50,
            max_tracking_error=Decimal("0.05"),
        )
    finally:
        database.close()
