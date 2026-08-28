from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PATH = ROOT / "src/firmquant/execution/replay_runner.py"
text = PATH.read_text(encoding="utf-8")


def replace_once(old: str, new: str, *, label: str) -> None:
    global text
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected exactly one match, found {count}")
    text = text.replace(old, new, 1)


replace_once(
    "from pathlib import Path\nfrom typing import Any, Protocol, cast\n",
    "from pathlib import Path\nfrom tempfile import TemporaryDirectory\nfrom typing import Any, Protocol, cast\n",
    label="temporary directory import",
)

helper = r'''

def _decision_restart_payload(snapshot: DecisionSnapshot | None) -> dict[str, object] | None:
    if snapshot is None:
        return None
    return {
        "strategy_session": snapshot.strategy_session.isoformat(),
        "decision_id": snapshot.decision_id,
        "request_fingerprint": snapshot.request_fingerprint,
        "input_fingerprint": snapshot.input_fingerprint,
        "firmquant_commit": snapshot.firmquant_commit,
        "uquant_commit": snapshot.uquant_commit,
        "uquant_code_fingerprint": snapshot.uquant_code_fingerprint,
        "uquant_config_fingerprint": snapshot.uquant_config_fingerprint,
        "data_manifest_sha256": snapshot.data_manifest_sha256,
        "universe_manifest_sha256": snapshot.universe_manifest_sha256,
        "broker_snapshot_sha256": snapshot.broker_snapshot_sha256,
        "account_before_sha256": snapshot.account_before_sha256,
        "account_after_sha256": snapshot.account_after_sha256,
        "payload_json": snapshot.payload_json,
        "payload_sha256": snapshot.payload_sha256,
        "created_at": snapshot.created_at.isoformat(),
        "supersedes_decision_id": snapshot.supersedes_decision_id,
    }


def _restore_decision_restart_payload(value: object) -> DecisionSnapshot | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ExecutionReplayError("restart decision payload is malformed")

    def required_text(key: str) -> str:
        raw = value.get(key)
        if not isinstance(raw, str) or not raw:
            raise ExecutionReplayError(f"restart decision field is malformed: {key}")
        return raw

    supersedes = value.get("supersedes_decision_id")
    if supersedes is not None and not isinstance(supersedes, str):
        raise ExecutionReplayError("restart supersedes decision id is malformed")
    try:
        return DecisionSnapshot(
            strategy_session=date.fromisoformat(required_text("strategy_session")),
            decision_id=required_text("decision_id"),
            request_fingerprint=required_text("request_fingerprint"),
            input_fingerprint=required_text("input_fingerprint"),
            firmquant_commit=required_text("firmquant_commit"),
            uquant_commit=required_text("uquant_commit"),
            uquant_code_fingerprint=required_text("uquant_code_fingerprint"),
            uquant_config_fingerprint=required_text("uquant_config_fingerprint"),
            data_manifest_sha256=required_text("data_manifest_sha256"),
            universe_manifest_sha256=required_text("universe_manifest_sha256"),
            broker_snapshot_sha256=required_text("broker_snapshot_sha256"),
            account_before_sha256=required_text("account_before_sha256"),
            account_after_sha256=required_text("account_after_sha256"),
            payload_json=required_text("payload_json"),
            payload_sha256=required_text("payload_sha256"),
            created_at=datetime.fromisoformat(required_text("created_at")),
            supersedes_decision_id=supersedes,
        )
    except ValueError as exc:
        raise ExecutionReplayError("restart decision payload cannot be decoded") from exc


def _decode_restart_share_map(value: object, *, label: str) -> dict[str, int]:
    if not isinstance(value, dict):
        raise ExecutionReplayError(f"restart {label} payload is malformed")
    result: dict[str, int] = {}
    for symbol, shares in value.items():
        if not isinstance(symbol, str) or not symbol:
            raise ExecutionReplayError(f"restart {label} symbol is malformed")
        if isinstance(shares, bool) or not isinstance(shares, int) or shares < 0:
            raise ExecutionReplayError(f"restart {label} shares are malformed")
        result[symbol] = shares
    return result


def _decode_restart_cost_map(value: object) -> dict[str, Decimal]:
    if not isinstance(value, dict):
        raise ExecutionReplayError("restart average-cost payload is malformed")
    result: dict[str, Decimal] = {}
    for symbol, raw in value.items():
        if not isinstance(symbol, str) or not symbol or not isinstance(raw, str):
            raise ExecutionReplayError("restart average-cost entry is malformed")
        try:
            cost = Decimal(raw)
        except Exception as exc:
            raise ExecutionReplayError("restart average-cost value is malformed") from exc
        if not cost.is_finite() or cost <= 0:
            raise ExecutionReplayError("restart average-cost value is outside its permitted bound")
        result[symbol] = cost
    return result


def _restart_roundtrip(
    *,
    strategy_account: AccountStateContract,
    replay_account: ReplayAccount,
    average_costs: dict[str, Decimal],
    pending: DecisionSnapshot | None,
    source_checkout: Path,
    data_root: Path,
) -> tuple[AccountStateContract, ReplayAccount, dict[str, Decimal], DecisionSnapshot | None, _ProductionEngine]:
    account_module = importlib.import_module("uquant.account")
    save_account = getattr(account_module, "save_account", None)
    load_account = getattr(account_module, "load_account", None)
    if not callable(save_account) or not callable(load_account):
        raise ExecutionReplayError("locked uquant account persistence API is unavailable")

    before_account = _account_sha256(strategy_account)
    runtime_payload: dict[str, object] = {
        "schema": "firmquant.execution-replay-restart.v1",
        "cash": _decimal_text(replay_account.cash),
        "positions": dict(sorted(replay_account.positions.items())),
        "sellable": dict(sorted(replay_account.sellable.items())),
        "average_costs": {key: _decimal_text(value) for key, value in sorted(average_costs.items())},
        "pending": _decision_restart_payload(pending),
    }
    runtime_json = json.dumps(
        runtime_payload,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )

    with TemporaryDirectory(prefix="firmquant-execution-replay-restart-") as directory:
        root = Path(directory)
        account_path = root / "account.json"
        runtime_path = root / "runtime.json"
        save_account(strategy_account, account_path)
        runtime_path.write_text(runtime_json, encoding="utf-8")
        restored_account = cast(AccountStateContract, load_account(account_path))
        try:
            restored_payload: object = json.loads(runtime_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ExecutionReplayError("restart runtime state is missing or corrupt") from exc

    after_account = _account_sha256(restored_account)
    if before_account != after_account:
        raise ExecutionReplayError("uquant economic state changed across restart persistence")
    if not isinstance(restored_payload, dict):
        raise ExecutionReplayError("restart runtime state must be an object")
    if restored_payload.get("schema") != "firmquant.execution-replay-restart.v1":
        raise ExecutionReplayError("restart runtime state schema mismatch")
    canonical_restored = json.dumps(
        restored_payload,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    if canonical_restored != runtime_json:
        raise ExecutionReplayError("restart runtime state is not canonical")
    raw_cash = restored_payload.get("cash")
    if not isinstance(raw_cash, str):
        raise ExecutionReplayError("restart cash payload is malformed")
    try:
        cash = Decimal(raw_cash)
    except Exception as exc:
        raise ExecutionReplayError("restart cash payload cannot be decoded") from exc
    restored_replay = ReplayAccount(
        cash=cash,
        positions=_decode_restart_share_map(restored_payload.get("positions"), label="positions"),
        sellable=_decode_restart_share_map(restored_payload.get("sellable"), label="sellable"),
    )
    restored_costs = _decode_restart_cost_map(restored_payload.get("average_costs"))
    restored_pending = _restore_decision_restart_payload(restored_payload.get("pending"))
    restored_engine = _engine(source_checkout, data_root)
    return restored_account, restored_replay, restored_costs, restored_pending, restored_engine
'''
replace_once(
    "\n\ndef _max_drawdown(equity: list[Decimal]) -> Decimal:\n",
    helper + "\n\ndef _max_drawdown(equity: list[Decimal]) -> Decimal:\n",
    label="restart helpers",
)

replace_once(
    '''    max_price_deviation_bps: Decimal,
) -> ReplaySummary:
''',
    '''    max_price_deviation_bps: Decimal,
    restart_each_session: bool = False,
) -> ReplaySummary:
''',
    label="restart parameter",
)
replace_once(
    '''    if type(start) is not date or type(end) is not date or start >= end:
        raise ValueError("execution replay date range is invalid")
''',
    '''    if type(start) is not date or type(end) is not date or start >= end:
        raise ValueError("execution replay date range is invalid")
    if not isinstance(restart_each_session, bool):
        raise TypeError("restart_each_session must be bool")
''',
    label="restart parameter validation",
)
replace_once(
    '''        pending = _decision_snapshot(
            decision=decision,
            session=session,
            firmquant_commit=firmquant_commit,
            identity=identity,
            data_sha256=data_hash,
            broker_sha256=close_snapshot.raw_payload_sha256,
            before_sha256=before,
            after_sha256=after,
        )

    final_equity = equity[-1]
''',
    '''        pending = _decision_snapshot(
            decision=decision,
            session=session,
            firmquant_commit=firmquant_commit,
            identity=identity,
            data_sha256=data_hash,
            broker_sha256=close_snapshot.raw_payload_sha256,
            before_sha256=before,
            after_sha256=after,
        )
        if restart_each_session:
            strategy_account, replay_account, average_costs, pending, engine = _restart_roundtrip(
                strategy_account=strategy_account,
                replay_account=replay_account,
                average_costs=average_costs,
                pending=pending,
                source_checkout=source_checkout,
                data_root=data_root,
            )

    final_equity = equity[-1]
''',
    label="session restart boundary",
)

PATH.write_text(text, encoding="utf-8")
