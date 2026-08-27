from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "src/firmquant/application/execution_evidence_runtime.py"
RUNNER = ROOT / "src/firmquant/execution/replay_runner.py"
WORKFLOW = ROOT / ".github/workflows/execution-evidence-runtime-fix.yml"


def replace_once(path: Path, old: str, new: str, *, label: str) -> None:
    text = path.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected one match, found {count}")
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


replace_once(
    RUNTIME,
    "class _UquantExecutionConfig(Protocol):\n    max_volume_participation: float\n    slippage_bps: float\n",
    "class _UquantExecutionConfig(Protocol):\n    max_volume_participation: float\n    slippage: float\n",
    label="uquant execution config",
)
replace_once(
    RUNTIME,
    "            slippage_bps=Decimal(str(config.slippage_bps)),\n",
    "            slippage_bps=Decimal(str(config.slippage)) * Decimal(\"10000\"),\n",
    label="shadow slippage",
)
replace_once(
    RUNTIME,
    '''    valid_execution_ids = tuple(item for item in execution_ids if item.startswith("exec_"))
    submit_count = cancel_count = 0
    if valid_execution_ids:
        placeholders = ",".join("?" for _ in valid_execution_ids)
        submit = database.scalar(
            "SELECT count(*) FROM order_commands c JOIN broker_order_attempts a ON a.attempt_id=c.attempt_id "
            f"WHERE c.command_kind='SUBMIT' AND a.execution_id IN ({placeholders})",
            valid_execution_ids,
        )
        cancel = database.scalar(
            "SELECT count(*) FROM order_commands c JOIN broker_order_attempts a ON a.attempt_id=c.attempt_id "
            f"WHERE c.command_kind='CANCEL' AND a.execution_id IN ({placeholders})",
            valid_execution_ids,
        )
        if isinstance(submit, bool) or not isinstance(submit, int):
            raise RuntimeEvidenceError("CANARY submit count is invalid")
        if isinstance(cancel, bool) or not isinstance(cancel, int):
            raise RuntimeEvidenceError("CANARY cancel count is invalid")
        submit_count, cancel_count = submit, cancel
''',
    '''    valid_execution_ids = tuple(item for item in execution_ids if item.startswith("exec_"))
    submit_count = cancel_count = 0
    for execution_id in valid_execution_ids:
        submit = database.scalar(
            "SELECT count(*) FROM order_commands c JOIN broker_order_attempts a ON a.attempt_id=c.attempt_id "
            "WHERE c.command_kind='SUBMIT' AND a.execution_id = ?",
            (execution_id,),
        )
        cancel = database.scalar(
            "SELECT count(*) FROM order_commands c JOIN broker_order_attempts a ON a.attempt_id=c.attempt_id "
            "WHERE c.command_kind='CANCEL' AND a.execution_id = ?",
            (execution_id,),
        )
        if isinstance(submit, bool) or not isinstance(submit, int):
            raise RuntimeEvidenceError("CANARY submit count is invalid")
        if isinstance(cancel, bool) or not isinstance(cancel, int):
            raise RuntimeEvidenceError("CANARY cancel count is invalid")
        submit_count += submit
        cancel_count += cancel
''',
    label="fixed command counting",
)
replace_once(
    RUNNER,
    "from datetime import UTC, date, datetime, time\n",
    "from datetime import date, datetime, time\n",
    label="runner UTC import",
)
replace_once(
    RUNNER,
    "from firmquant.application.execution_evidence import BlockerCode\n",
    "",
    label="runner blocker import",
)
replace_once(
    RUNNER,
    '''def _replay_orders(plan: ExecutionPlan, account: ReplayAccount, costs: ReplayCosts) -> tuple[ReplayOrder, ...]:
''',
    '''def _replay_orders(
    plan: ExecutionPlan,
    account: ReplayAccount,
    max_volume_participation: Decimal,
) -> tuple[ReplayOrder, ...]:
''',
    label="replay order signature",
)
replace_once(
    RUNNER,
    '''            max_volume_participation=Decimal(str(cast(Any, costs).commission_rate * 0 + Decimal("0.005"))),
''',
    '''            max_volume_participation=max_volume_participation,
''',
    label="replay participation",
)
replace_once(
    RUNNER,
    '''            orders = _replay_orders(plan, replay_account, costs)
''',
    '''            orders = _replay_orders(
                plan,
                replay_account,
                Decimal(str(engine.cfg.max_volume_participation)),
            )
''',
    label="replay participation call",
)

Path(__file__).unlink()
WORKFLOW.unlink()
