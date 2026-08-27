from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from firmquant.application.execution_evidence import (
    BlockerCode,
    EvidenceConflictError,
    EvidenceIdentity,
    EvidenceStage,
    ExecutionEvidenceStore,
    ExecutionObservation,
    FillObservation,
    OrderObservation,
    PositionObservation,
    TargetObservation,
    aggregate_observations,
)
from firmquant.persistence.database import Database

D40 = "a" * 40
U40 = "b" * 40
D64 = "c" * 64
A64 = "d" * 64
C64 = "e" * 64
DATA64 = "f" * 64
CAL64 = "1" * 64


def _observation(*, stage: EvidenceStage = EvidenceStage.SHADOW) -> ExecutionObservation:
    identity = EvidenceIdentity(
        stage=stage,
        execution_session=date(2026, 8, 27),
        firmquant_commit=D40,
        uquant_commit=U40,
        promotion_config_sha256=C64,
        account_sha256=A64,
        data_sha256=DATA64,
        calendar_sha256=CAL64,
    )
    return ExecutionObservation(
        identity=identity,
        decision_id="decision-1",
        plan_id="plan-1",
        portfolio_equity=Decimal("10000"),
        planned_orders=(
            OrderObservation(
                execution_id="exec-1",
                uquant_order_id="uq-1",
                symbol="600000.SH",
                side="BUY",
                planned_shares=100,
                filled_shares=60,
                reference_price=Decimal("10"),
                blocker=BlockerCode.VOLUME_LIMIT,
            ),
        ),
        targets=(
            TargetObservation(
                symbol="600000.SH",
                target_shares=100,
                target_weight=Decimal("0.10"),
                reference_price=Decimal("10"),
            ),
            TargetObservation(
                symbol="000001.SZ",
                target_shares=0,
                target_weight=Decimal("0"),
                reference_price=Decimal("10"),
            ),
        ),
        fills=(
            FillObservation(
                fill_id=None if stage is EvidenceStage.SHADOW else "broker-fill-1",
                execution_id="exec-1",
                symbol="600000.SH",
                side="BUY",
                shares=60,
                price=Decimal("10.01"),
                commission=Decimal("5"),
                stamp_duty=Decimal("0"),
                transfer_fee=Decimal("0.01"),
                slippage=Decimal("0.60"),
            ),
        ),
        actual_ending_positions=(PositionObservation(symbol="600000.SH", shares=60),),
        hypothetical_ending_positions=(PositionObservation(symbol="600000.SH", shares=60),),
        submit_count=0 if stage is EvidenceStage.SHADOW else 1,
        cancel_count=0,
        rejection_count=0,
        unknown_count=0,
        external_activity=0,
        duplicate_economic_orders=0,
        duplicate_fills=0,
        data_quality_failures=0,
        created_at=datetime(2026, 8, 27, 15, 10, tzinfo=UTC),
    )


def test_same_session_identity_is_idempotent_and_conflicts_fail_closed(tmp_path) -> None:
    db = Database.open(tmp_path / "ledger.sqlite3")
    try:
        store = ExecutionEvidenceStore(db)
        original = _observation()
        assert store.append(original) is True
        assert store.append(original) is False

        conflicting = replace(
            original,
            planned_orders=(replace(original.planned_orders[0], filled_shares=100, blocker=None),),
        )
        with pytest.raises(EvidenceConflictError):
            store.append(conflicting)
    finally:
        db.close()


def test_shadow_tracking_error_is_derived_from_target_and_ending_positions() -> None:
    observation = _observation()
    aggregate = aggregate_observations((observation,))

    assert aggregate.observed_sessions == 1
    assert aggregate.order_count == 1
    assert aggregate.max_tracking_error == Decimal("0.04")
    assert aggregate.mean_tracking_error == Decimal("0.02")
    assert aggregate.notional_weighted_tracking_error == Decimal("0.04")
    assert aggregate.unfilled_notional == Decimal("400")
    assert aggregate.blocker_counts[BlockerCode.VOLUME_LIMIT] == 1


def test_blocker_does_not_force_tracking_error_to_one() -> None:
    observation = replace(
        _observation(),
        planned_orders=(replace(_observation().planned_orders[0], blocker=BlockerCode.NON_TRADABLE),),
    )
    aggregate = aggregate_observations((observation,))
    assert aggregate.max_tracking_error == Decimal("0.04")
    assert aggregate.max_tracking_error != Decimal("1")


def test_canary_requires_real_fill_identifier_and_real_submit_count() -> None:
    canary = _observation(stage=EvidenceStage.CANARY)
    aggregate = aggregate_observations((canary,))
    assert aggregate.submit_count == 1
    assert aggregate.fill_count == 1

    with pytest.raises(ValueError):
        replace(
            canary,
            fills=(replace(canary.fills[0], fill_id=None),),
        )
