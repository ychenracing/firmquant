from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

from firmquant.application.execution_evidence import (
    EvidenceIdentity,
    EvidenceStage,
    ExecutionObservation,
    PositionObservation,
    TargetObservation,
)
from firmquant.application.promotion_store import PromotionStore
from firmquant.persistence.database import Database

NOW = datetime(2026, 8, 25, 8, 0, tzinfo=UTC)


def observation(*, stage: EvidenceStage, session: date, config: str = "c" * 64) -> ExecutionObservation:
    identity = EvidenceIdentity(
        stage=stage,
        execution_session=session,
        firmquant_commit="f" * 40,
        uquant_commit="1" * 40,
        promotion_config_sha256=config,
        account_sha256="a" * 64,
        data_sha256="d" * 64,
        calendar_sha256="e" * 64,
    )
    position = PositionObservation(symbol="600000.SH", shares=100)
    target = TargetObservation(
        symbol="600000.SH",
        target_shares=100,
        target_weight=Decimal("0.10"),
        reference_price=Decimal("10"),
    )
    return ExecutionObservation(
        identity=identity,
        decision_id="decision-" + session.isoformat(),
        plan_id="plan-" + session.isoformat(),
        portfolio_equity=Decimal("10000"),
        planned_orders=(),
        targets=(target,),
        fills=(),
        actual_ending_positions=(position,),
        hypothetical_ending_positions=(position,),
        submit_count=0,
        cancel_count=0,
        rejection_count=0,
        unknown_count=0,
        external_activity=0,
        duplicate_economic_orders=0,
        duplicate_fills=0,
        data_quality_failures=0,
        created_at=NOW,
    )


def test_promotion_store_derives_sessions_from_immutable_details(tmp_path: Path) -> None:
    database = Database.open(tmp_path / "firmquant.sqlite3")
    try:
        store = PromotionStore(database)
        first = observation(stage=EvidenceStage.SHADOW, session=date(2026, 8, 24))
        second = observation(stage=EvidenceStage.SHADOW, session=date(2026, 8, 25))

        assert store.append(first) is True
        assert store.append(first) is False
        assert store.append(second) is True

        aggregate = store.aggregate(
            stage=EvidenceStage.SHADOW,
            firmquant_commit="f" * 40,
            uquant_commit="1" * 40,
            config_sha256="c" * 64,
            account_hash="a" * 64,
        )
        assert aggregate is not None
        assert aggregate.observed_sessions == 2
        assert aggregate.order_count == 0
        assert aggregate.max_tracking_error == Decimal("0")
        assert (
            database.scalar("SELECT count(*) FROM audit_events WHERE category = 'EXECUTION_OBSERVATION'") == 2
        )
    finally:
        database.close()


def test_shadow_and_canary_qualification_are_independent(tmp_path: Path) -> None:
    database = Database.open(tmp_path / "firmquant.sqlite3")
    try:
        store = PromotionStore(database)
        shadow = observation(stage=EvidenceStage.SHADOW, session=date(2026, 8, 24))
        canary = replace(
            observation(stage=EvidenceStage.CANARY, session=date(2026, 8, 25)),
            submit_count=1,
        )
        store.append(shadow)
        store.append(canary)

        assert store.qualifies(
            stage=EvidenceStage.SHADOW,
            firmquant_commit="f" * 40,
            uquant_commit="1" * 40,
            config_sha256="c" * 64,
            account_hash="a" * 64,
            min_sessions=1,
            min_orders=0,
            min_fills=0,
            max_tracking_error=Decimal("0.05"),
        )
        assert store.qualifies(
            stage=EvidenceStage.CANARY,
            firmquant_commit="f" * 40,
            uquant_commit="1" * 40,
            config_sha256="c" * 64,
            account_hash="a" * 64,
            min_sessions=1,
            min_orders=0,
            min_fills=0,
            max_tracking_error=Decimal("0.05"),
        )
        assert not store.qualifies(
            stage=EvidenceStage.CANARY,
            firmquant_commit="f" * 40,
            uquant_commit="1" * 40,
            config_sha256="d" * 64,
            account_hash="a" * 64,
            min_sessions=1,
            min_orders=0,
            min_fills=0,
            max_tracking_error=Decimal("0.05"),
        )
    finally:
        database.close()
