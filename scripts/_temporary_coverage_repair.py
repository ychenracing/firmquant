from __future__ import annotations

from pathlib import Path


def replace_once(path: Path, old: str, new: str, label: str) -> None:
    text = path.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected one anchor, got {count}")
    path.write_text(text.replace(old, new, 1), encoding="utf-8", newline="\n")


def replace_all(path: Path, old: str, new: str, label: str, *, minimum: int = 1) -> None:
    text = path.read_text(encoding="utf-8")
    count = text.count(old)
    if count < minimum:
        raise RuntimeError(f"{label}: expected at least {minimum} anchors, got {count}")
    path.write_text(text.replace(old, new), encoding="utf-8", newline="\n")


app_test = Path("tests/unit/application/test_production_services_branches.py")
replace_once(
    app_test,
    "from datetime import UTC, datetime, timedelta\n",
    "from datetime import datetime, timedelta\n",
    "remove unused UTC",
)
replace_once(
    app_test,
    "from firmquant.reconciliation.models import ReconciliationKind\n",
    "",
    "remove unused reconciliation kind",
)
replace_once(
    app_test,
    "from firmquant.risk.gate import GateAction, GateDecision\n",
    "from firmquant.risk.capability import WriteCapabilityDenied\nfrom firmquant.risk.gate import GateAction, GateDecision\n",
    "write capability denial import",
)
replace_once(
    app_test,
    "    ttl: timedelta = timedelta(minutes=5),\n",
    "    ttl: timedelta = timedelta(minutes=10),\n",
    "valid arm ttl",
)
replace_once(
    app_test,
    '''        hooks.heartbeat(ps.ProductionHeartbeat(sequence=1, observed_at=NOW))\n''',
    '''        hooks.heartbeat(\n            ps.ProductionHeartbeat(\n                mode=Mode.SHADOW,\n                observed_at=NOW,\n                writer_generation=hooks._writer.generation,\n                pending_events=0,\n                processed_events=0,\n                decisions=0,\n                executions=0,\n                eod=0,\n            )\n        )\n''',
    "heartbeat shape",
)
replace_once(
    app_test,
    '''    with base.hook_case(tmp_path / "missing", mode=Mode.CANARY) as (hooks, _writer, broker, _accounts):\n        with pytest.raises(ps.ProductionServicesUnavailable, match="LEASE_REQUIRED"):\n            hooks._load_arm(broker.query_account().account_id_hash)\n''',
    '''    with (\n        base.hook_case(tmp_path / "missing", mode=Mode.CANARY) as (hooks, _writer, broker, _accounts),\n        pytest.raises(ps.ProductionServicesUnavailable, match="LEASE_REQUIRED"),\n    ):\n        hooks._load_arm(broker.query_account().account_id_hash)\n''',
    "combine arm missing contexts",
)
replace_once(
    app_test,
    '''        monkeypatch.setattr(\n            ps.StrategyIdentity,\n            "locked",\n            lambda: SimpleNamespace(uquant_commit=hooks._identity.uquant_commit),\n        )\n''',
    '''        monkeypatch.setattr(\n            ps.StrategyIdentity,\n            "locked",\n            lambda: SimpleNamespace(\n                uquant_commit=hooks._identity.uquant_commit,\n                canonical_universe_sha256="0" * 64,\n                config_fingerprint="0" * 64,\n                economic_code_fingerprint="0" * 64,\n            ),\n        )\n''',
    "complete strategy identity fixture",
)
replace_once(
    app_test,
    '''        with pytest.raises(Exception):\n            capability.submit_order(bad)\n''',
    '''        with pytest.raises(WriteCapabilityDenied):\n            capability.submit_order(bad)\n''',
    "specific capability denial",
)
replace_once(
    app_test,
    '''    with base.hook_case(tmp_path / "not-ready") as (hooks, _writer, _broker, _accounts):\n        with pytest.raises(ps.ProductionServicesUnavailable, match="NOT_READY"):\n            hooks.cycle(NOW)\n''',
    '''    with (\n        base.hook_case(tmp_path / "not-ready") as (hooks, _writer, _broker, _accounts),\n        pytest.raises(ps.ProductionServicesUnavailable, match="NOT_READY"),\n    ):\n        hooks.cycle(NOW)\n''',
    "combine cycle contexts",
)
replace_once(
    app_test,
    '''        broker.set_orders((replace(execution_snapshot().broker_snapshot.orders[0], client_order_id=None),)) if execution_snapshot().broker_snapshot.orders else None\n''',
    "",
    "remove unreachable fake setter",
)

broker_test = Path("tests/unit/broker/test_production_xtquant_branches.py")
replace_once(
    broker_test,
    "from datetime import UTC, datetime\n",
    "from datetime import UTC, datetime\nfrom decimal import Decimal\n",
    "decimal import",
)
replace_once(
    broker_test,
    '        limit_price=Price("10.10"),\n',
    '        limit_price=Price(Decimal("10.10")),\n',
    "decimal price",
)
replace_once(
    broker_test,
    '''        SdkObject(traded_id="fill-2", order_id=9002, traded_time=93102),\n        SdkObject(traded_id="fill-1", order_id=9001, traded_time=93101),\n''',
    '''        SdkObject(\n            traded_id="fill-2",\n            order_id=9002,\n            traded_time=93102,\n            traded_volume=100,\n            traded_price=10.1,\n        ),\n        SdkObject(\n            traded_id="fill-1",\n            order_id=9001,\n            traded_time=93101,\n            traded_volume=100,\n            traded_price=10.1,\n        ),\n''',
    "valid fill volumes",
)
replace_once(
    broker_test,
    '''    broker.subscribe(lambda _event: (_ for _ in ()).throw(RuntimeError("sink")))\n    with pytest.raises(RuntimeError, match="sink"):\n        facade.emit("DISCONNECTED", {})\n    assert broker.health().diagnostic_code == "CALLBACK_SINK_FAILED"\n''',
    '''    failing_broker, failing_facade, _ = base._broker()\n    failing_broker.subscribe(lambda _event: (_ for _ in ()).throw(RuntimeError("sink")))\n    with pytest.raises(RuntimeError, match="sink"):\n        failing_facade.emit("DISCONNECTED", {})\n    assert failing_broker.health().diagnostic_code == "CALLBACK_SINK_FAILED"\n''',
    "fresh callback sink broker",
)

live_test = Path("tests/unit/execution/test_live_controller_branches.py")
replace_once(
    live_test,
    "            shares=planned.uquant_authorized_shares,\n",
    "            shares=planned.uquant_authorized_shares.value,\n",
    "aggregate integer shares",
)
replace_once(
    live_test,
    '''    class BadReasons(RuntimeError):\n        reason_codes = ["SECRET"]\n''',
    '''    class BadReasons(RuntimeError):\n        def __init__(self, message: str) -> None:\n            self.reason_codes = ["SECRET"]\n            super().__init__(message)\n''',
    "mutable reason fixture",
)

# A cancel whose broker outcome is unknown must transition the aggregate to UNKNOWN,
# not merely mark the attempt row unknown while leaving economic state CANCEL_REQUESTED.
events = Path("src/firmquant/domain/events.py")
replace_once(
    events,
    '''@dataclass(frozen=True, slots=True)\nclass CancelConfirmed(OrderEvent):\n''',
    '''@dataclass(frozen=True, slots=True)\nclass CancelOutcomeUnknown(OrderEvent):\n    diagnostic_code: str\n\n    def __post_init__(self) -> None:\n        OrderEvent.__post_init__(self)\n        _reason_code(self.diagnostic_code, label="cancel diagnostic code")\n\n\n@dataclass(frozen=True, slots=True)\nclass CancelConfirmed(OrderEvent):\n''',
    "cancel unknown event",
)
replace_once(
    events,
    '''    | CancelNotAccepted\n    | CancelConfirmed\n''',
    '''    | CancelNotAccepted\n    | CancelOutcomeUnknown\n    | CancelConfirmed\n''',
    "supported cancel unknown event",
)
replace_once(
    events,
    '''    "CancelNotAccepted",\n    "CancelRequested",\n''',
    '''    "CancelNotAccepted",\n    "CancelOutcomeUnknown",\n    "CancelRequested",\n''',
    "export cancel unknown event",
)

orders = Path("src/firmquant/domain/orders.py")
replace_once(
    orders,
    '''    CancelNotAccepted,\n    CancelRequested,\n''',
    '''    CancelNotAccepted,\n    CancelOutcomeUnknown,\n    CancelRequested,\n''',
    "import cancel unknown event",
)
replace_once(
    orders,
    '''        if isinstance(event, CancelConfirmed):\n''',
    '''        if isinstance(event, CancelOutcomeUnknown):\n            if self.state is not OrderState.CANCEL_REQUESTED:\n                raise DomainTransitionError(\n                    f"illegal order transition {self.state.value} via CancelOutcomeUnknown"\n                )\n            return self._updated(event, state=OrderState.UNKNOWN)\n        if isinstance(event, CancelConfirmed):\n''',
    "apply cancel unknown transition",
)

repositories = Path("src/firmquant/persistence/repositories.py")
replace_once(
    repositories,
    '''    CancelConfirmed,\n    CancelRequested,\n''',
    '''    CancelConfirmed,\n    CancelOutcomeUnknown,\n    CancelRequested,\n''',
    "repository cancel unknown import",
)
replace_once(
    repositories,
    '''        next_aggregate = self.transition(\n            aggregate,\n            SubmitOutcomeUnknown(\n                event_id=_stable_event_id("unknown", attempt.attempt_id, diagnostic_code),\n                diagnostic_code=diagnostic_code,\n            ),\n            occurred_at=occurred_at,\n        )\n''',
    '''        event = (\n            CancelOutcomeUnknown(\n                event_id=_stable_event_id("unknown", attempt.attempt_id, diagnostic_code),\n                diagnostic_code=diagnostic_code,\n            )\n            if attempt.command_kind == "CANCEL"\n            else SubmitOutcomeUnknown(\n                event_id=_stable_event_id("unknown", attempt.attempt_id, diagnostic_code),\n                diagnostic_code=diagnostic_code,\n            )\n        )\n        next_aggregate = self.transition(\n            aggregate,\n            event,\n            occurred_at=occurred_at,\n        )\n''',
    "unknown attempt event by command kind",
)
