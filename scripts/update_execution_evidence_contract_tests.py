from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ACCEPTANCE = ROOT / "tests/unit/application/test_production_services_acceptance.py"
STORE = ROOT / "tests/unit/application/test_promotion_store.py"
README = ROOT / "README.md"


def replace_once(path: Path, old: str, new: str, *, label: str) -> None:
    text = path.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected exactly one match, found {count}")
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


replace_once(
    ACCEPTANCE,
    "from firmquant.application.close_checkpoint import CloseStep\n",
    '''from firmquant.application.close_checkpoint import CloseStep
from firmquant.application.execution_evidence import (
    BlockerCode,
    EvidenceIdentity,
    EvidenceStage,
    ExecutionObservation,
    OrderObservation,
    PositionObservation,
    TargetObservation,
)
''',
    label="execution evidence imports",
)
old_promotion = '''def test_canary_promotion_gate_is_identity_bound(tmp_path: Path) -> None:
    with hook_case(tmp_path, mode=Mode.CANARY) as (hooks, _writer, broker, _accounts):
        account_hash = broker.query_account().account_id_hash
        with pytest.raises(ProductionServicesUnavailable, match="PROMOTION"):
            hooks._require_promotion(account_hash)

        thresholds = hooks._settings.promotion
        ps.PromotionStore(hooks._database).append(
            ps.ShadowPromotionEvidence(
                firmquant_commit=hooks._identity.firmquant_commit,
                uquant_commit=hooks._identity.uquant_commit,
                config_sha256=hooks._identity.promotion_config_sha256,
                account_hash=account_hash,
                observed_sessions=thresholds.min_shadow_sessions,
                hypothetical_orders=thresholds.min_shadow_orders,
                unresolved_orders=0,
                external_orders=0,
                duplicate_economic_orders=0,
                duplicate_fills=0,
                max_target_tracking_error=Decimal("0"),
                created_at=NOW,
            )
        )
        hooks._require_promotion(account_hash)
'''
new_promotion = '''def test_canary_promotion_gate_is_identity_bound(tmp_path: Path) -> None:
    with hook_case(tmp_path, mode=Mode.CANARY) as (hooks, _writer, broker, _accounts):
        account_hash = broker.query_account().account_id_hash
        with pytest.raises(ProductionServicesUnavailable, match="PROMOTION"):
            hooks._require_promotion(account_hash)

        thresholds = hooks._settings.promotion
        store = ps.PromotionStore(hooks._database)
        orders_per_session = max(1, thresholds.min_shadow_orders)
        for index in range(thresholds.min_shadow_sessions):
            session = date.fromordinal(EXECUTION_SESSION.toordinal() - thresholds.min_shadow_sessions + index)
            target = TargetObservation(
                symbol="600000.SH",
                target_shares=100,
                target_weight=Decimal("0.10"),
                reference_price=Decimal("10"),
            )
            position = PositionObservation(symbol="600000.SH", shares=100)
            orders = tuple(
                OrderObservation(
                    execution_id=f"shadow-{index}-{order_index}",
                    uquant_order_id=f"uq-shadow-{index}-{order_index}",
                    symbol="600000.SH",
                    side="BUY",
                    planned_shares=100,
                    filled_shares=0,
                    reference_price=Decimal("10"),
                    blocker=BlockerCode.TARGET_ALREADY_SATISFIED,
                )
                for order_index in range(orders_per_session)
            )
            store.append(
                ExecutionObservation(
                    identity=EvidenceIdentity(
                        stage=EvidenceStage.SHADOW,
                        execution_session=session,
                        firmquant_commit=hooks._identity.firmquant_commit,
                        uquant_commit=hooks._identity.uquant_commit,
                        promotion_config_sha256=hooks._identity.promotion_config_sha256,
                        account_sha256=account_hash,
                        data_sha256="d" * 64,
                        calendar_sha256="e" * 64,
                    ),
                    decision_id=f"decision-shadow-{index}",
                    plan_id=f"plan-shadow-{index}",
                    portfolio_equity=Decimal("10000"),
                    planned_orders=orders,
                    planning_blockers=(),
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
            )
        hooks._require_promotion(account_hash)
'''
replace_once(ACCEPTANCE, old_promotion, new_promotion, label="canary promotion test")
replace_once(
    ACCEPTANCE,
    '''        hooks._shadow_execute(plan, decision)
        hooks._shadow_execute(plan, decision)

        assert broker.submitted_commands == ()
        assert broker.cancelled_order_ids == ()
        assert hooks.real_order_calls() == 0
        evidence = ps.PromotionStore(hooks._database).latest(
            firmquant_commit=hooks._identity.firmquant_commit,
            uquant_commit=hooks._identity.uquant_commit,
            config_sha256=hooks._identity.promotion_config_sha256,
            account_hash=broker.query_account().account_id_hash,
        )
        assert evidence is not None
        assert evidence.observed_sessions == 2
        assert evidence.hypothetical_orders == len(plan.orders) * 2
''',
    '''        hooks._shadow_execute(plan, decision, facts)
        hooks._shadow_execute(plan, decision, facts)

        assert broker.submitted_commands == ()
        assert broker.cancelled_order_ids == ()
        assert hooks.real_order_calls() == 0
        evidence = ps.PromotionStore(hooks._database).aggregate(
            stage=EvidenceStage.SHADOW,
            firmquant_commit=hooks._identity.firmquant_commit,
            uquant_commit=hooks._identity.uquant_commit,
            config_sha256=hooks._identity.promotion_config_sha256,
            account_hash=broker.query_account().account_id_hash,
        )
        assert evidence is not None
        assert evidence.observed_sessions == 1
        assert evidence.order_count == len(plan.orders)
''',
    label="shadow execution idempotency test",
)

replace_once(
    STORE,
    "        planned_orders=(),\n        targets=(target,),\n",
    "        planned_orders=(),\n        planning_blockers=(),\n        targets=(target,),\n",
    label="promotion store observation fixture",
)

replace_once(
    README,
    "| `firmquant execution-replay --start YYYY-MM-DD --end YYYY-MM-DD` | 使用锁定 uquant source 与 frozen data 运行跨日 execution-aware Replay，并输出稳定 JSON 摘要 |\n",
    "| `firmquant execution-replay` | 通过 `--start YYYY-MM-DD --end YYYY-MM-DD` 使用锁定 uquant source 与 frozen data 运行跨日 execution-aware Replay，并输出稳定 JSON 摘要 |\n",
    label="README command inventory",
)
