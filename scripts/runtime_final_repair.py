from __future__ import annotations

from pathlib import Path


def replace_once(path: str, old: str, new: str, label: str) -> None:
    file = Path(path)
    text = file.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"{label}: expected one match, found {count}")
    file.write_text(text.replace(old, new, 1), encoding="utf-8")


# Heartbeat authority applies only to the real production daemon modes.
replace_once(
    "src/firmquant/application/operations.py",
    '''        heartbeat = database.query_one("SELECT * FROM production_heartbeat WHERE singleton_id = 1")
        heartbeat_age: float | None = None
        process_health = "NOT_RUNNING"
        broker_connection = "NOT_RUNNING"
        broker_read_healthy = False
        broker_write_healthy = False
        if heartbeat is None:
            blockers.add("PROCESS_NOT_RUNNING")
        else:
            try:
                heartbeat_at = datetime.fromisoformat(str(heartbeat["observed_at"]))
                if heartbeat_at.tzinfo is None or heartbeat_at.utcoffset() is None:
                    raise ValueError
                age = now - heartbeat_at
                heartbeat_age = age.total_seconds()
                if heartbeat_age < 0:
                    raise ValueError
            except ValueError as error:
                raise OperatorCommandDenied("HEARTBEAT_INVALID") from error
            broker_connection = "CONNECTED" if int(heartbeat["broker_connected"]) == 1 else "DISCONNECTED"
            broker_read_healthy = int(heartbeat["broker_read_healthy"]) == 1
            broker_write_healthy = int(heartbeat["broker_write_healthy"]) == 1
            if heartbeat_age > 30.0:
                process_health = "STALE"
                blockers.add("HEARTBEAT_STALE")
            else:
                process_health = "HEALTHY"
        effective_state = status.state.value if process_health == "HEALTHY" else RuntimeState.HALTED.value
''',
    '''        production_mode = settings.mode in {Mode.SHADOW, Mode.CANARY, Mode.LIVE}
        heartbeat = database.query_one("SELECT * FROM production_heartbeat WHERE singleton_id = 1")
        heartbeat_age: float | None = None
        process_health = "NOT_APPLICABLE"
        broker_connection = "NOT_APPLICABLE"
        broker_read_healthy = False
        broker_write_healthy = False
        effective_state = status.state.value
        if production_mode:
            process_health = "NOT_RUNNING"
            broker_connection = "NOT_RUNNING"
            effective_state = RuntimeState.HALTED.value
            if heartbeat is None:
                blockers.add("PROCESS_NOT_RUNNING")
            else:
                try:
                    heartbeat_at = datetime.fromisoformat(str(heartbeat["observed_at"]))
                    if heartbeat_at.tzinfo is None or heartbeat_at.utcoffset() is None:
                        raise ValueError
                    age = now - heartbeat_at
                    heartbeat_age = age.total_seconds()
                    if heartbeat_age < 0:
                        raise ValueError
                except ValueError as error:
                    raise OperatorCommandDenied("HEARTBEAT_INVALID") from error
                broker_connection = (
                    "CONNECTED" if int(heartbeat["broker_connected"]) == 1 else "DISCONNECTED"
                )
                broker_read_healthy = int(heartbeat["broker_read_healthy"]) == 1
                broker_write_healthy = int(heartbeat["broker_write_healthy"]) == 1
                if heartbeat_age > 30.0:
                    process_health = "STALE"
                    blockers.add("HEARTBEAT_STALE")
                else:
                    process_health = "HEALTHY"
                    effective_state = status.state.value
''',
    "mode-scoped heartbeat authority",
)

# A CANARY READY fixture must include a fresh daemon heartbeat.
replace_once(
    "tests/integration/test_cli_operations.py",
    '''            database.write(
                """
                INSERT INTO runtime_state(
                    singleton_id, mode, state, revision, reason, blockers_json, updated_at
                ) VALUES (1, 'CANARY', 'READY', 1, 'startup reconciliation passed', '[]', ?)
                """,
                (NOW.isoformat(),),
            )
''',
    '''            database.write(
                """
                INSERT INTO runtime_state(
                    singleton_id, mode, state, revision, reason, blockers_json, updated_at
                ) VALUES (1, 'CANARY', 'READY', 1, 'startup reconciliation passed', '[]', ?)
                """,
                (NOW.isoformat(),),
            )
            database.write(
                """
                INSERT INTO production_heartbeat(
                    singleton_id, mode, runtime_state, observed_at, host_hash, process_id,
                    writer_generation, broker_connected, broker_read_healthy, broker_write_healthy,
                    pending_events, last_broker_event, last_quote, last_reconciliation,
                    last_decision, last_execution, control_request_state, processed_events,
                    decisions, executions, eod
                ) VALUES (1, 'CANARY', 'READY', ?, ?, 12345, 1, 1, 1, 1, 0, NULL, NULL, ?,
                          NULL, NULL, 'IDLE', 0, 0, 0, 0)
                """,
                (NOW.isoformat(), "h" * 64, NOW.isoformat()),
            )
''',
    "live readiness heartbeat fixture",
)

# Existing controller tests need the now-required deterministic clock evidence.
path = Path("tests/unit/execution/test_live_controller.py")
text = path.read_text(encoding="utf-8")
import_anchor = "from firmquant.risk.gate import GateAction, GateDecision\n"
if text.count(import_anchor) != 1:
    raise SystemExit("live controller clock import anchor changed")
text = text.replace(
    import_anchor,
    import_anchor + "from firmquant.scheduling.clock import ClockGuard, ClockObservation\n",
    1,
)
context_tail = '''            cash_and_positions_safe=True,
            frequency_within_limits=True,
        )
'''
context_new = '''            cash_and_positions_safe=True,
            frequency_within_limits=True,
            clock_receipt=ClockGuard(max_drift=timedelta(seconds=2)).verify(
                ClockObservation(
                    system_time=clock(),
                    reference_time=clock(),
                    local_timezone="Asia/Shanghai",
                )
            ),
        )
'''
if text.count(context_tail) != 1:
    raise SystemExit(f"live controller context tail changed: {text.count(context_tail)}")
path.write_text(text.replace(context_tail, context_new, 1), encoding="utf-8")

replace_once(
    "tests/unit/execution/test_live_controller_branches.py",
    "            submitted_at=NOW,\n",
    "            submitted_monotonic=clock.monotonic(),\n",
    "finish-open-order monotonic test parameter",
)

# Production-service branch fixtures provide all new authority facts.
replace_once(
    "tests/unit/application/test_production_services_branches.py",
    '''            ps.ProductionHeartbeat(
                mode=Mode.SHADOW,
                observed_at=NOW,
                writer_generation=hooks._writer.generation,
                pending_events=0,
                processed_events=0,
                decisions=0,
                executions=0,
                eod=0,
            )
''',
    '''            ps.ProductionHeartbeat(
                mode=Mode.SHADOW,
                runtime_state=hooks.status.state,
                observed_at=NOW,
                host_hash=hooks._writer.host_hash,
                process_id=hooks._writer.process_id,
                writer_generation=hooks._writer.generation,
                broker_connected=True,
                broker_read_healthy=True,
                broker_write_healthy=True,
                pending_events=0,
                last_broker_event=None,
                last_quote=None,
                last_reconciliation=None,
                last_decision=None,
                last_execution=None,
                control_request_state="IDLE",
                processed_events=0,
                decisions=0,
                executions=0,
                eod=0,
            )
''',
    "expanded production heartbeat branch fixture",
)
replace_once(
    "tests/unit/application/test_production_services_branches.py",
    '''        authorities = ps._ExecutionAuthorities(
            plan=plan,
            facts=facts,
            decision=decision,
            planned={planned.uquant_order_id: planned},
        )
''',
    '''        authorities = ps._ExecutionAuthorities(
            plan=plan,
            facts=facts,
            decision=decision,
            planned={planned.uquant_order_id: planned},
            reconciliation=SimpleNamespace(
                reconciliation_id="recon_" + "2" * 64,
                passed=True,
                blockers=(),
            ),
        )
''',
    "execution authority reconciliation fixture",
)
branch_path = Path("tests/unit/application/test_production_services_branches.py")
text = branch_path.read_text(encoding="utf-8")
live_call = "        assert hooks._execute(base.EXECUTION_SESSION) == 1\n"
unsafe_call = '''        with pytest.raises(ps.ProductionServicesUnavailable, match="SAFETY_FAILURE"):
            hooks._execute(base.EXECUTION_SESSION)
'''
if text.count(live_call) != 1 or text.count(unsafe_call) != 1:
    raise SystemExit("production services execute branch call shape changed")
text = text.replace(
    live_call,
    "        hooks._active_execution_deadlines = ps.ExecutionDeadlines(60.0, 90.0, 120.0)\n" + live_call,
    1,
)
text = text.replace(
    unsafe_call,
    "        hooks._active_execution_deadlines = ps.ExecutionDeadlines(60.0, 90.0, 120.0)\n"
    + unsafe_call,
    1,
)
branch_path.write_text(text, encoding="utf-8")

replace_once(
    "tests/unit/broker/test_production_factory_branches.py",
    '        with pytest.raises(BrokerDependencyMissing, match="SDK modules"):\n',
    '        with pytest.raises(BrokerDependencyMissing, match="XTQUANT_SDK_UNAVAILABLE"):\n',
    "production SDK failure code assertion",
)

# Schema tests follow the central schema version after heartbeat migration.
replace_once(
    "tests/unit/persistence/test_account_authority_schema_migration.py",
    '''        assert CURRENT_SCHEMA_VERSION == 3
        assert database.scalar("SELECT max(version) FROM schema_migrations") == 3
''',
    '''        assert CURRENT_SCHEMA_VERSION >= 4
        assert database.scalar("SELECT max(version) FROM schema_migrations") == CURRENT_SCHEMA_VERSION
''',
    "account authority current schema assertion",
)
db_path = Path("tests/unit/persistence/test_database.py")
text = db_path.read_text(encoding="utf-8")
import_anchor = '''from firmquant.persistence.database import (
    Database,
    DatabaseCorrupt,
    DatabaseUnavailable,
    TransactionRequired,
)
'''
if text.count(import_anchor) != 1:
    raise SystemExit("database schema import anchor changed")
text = text.replace(
    import_anchor,
    import_anchor + "from firmquant.persistence.schema import CURRENT_SCHEMA_VERSION\n",
    1,
)
text = text.replace(
    '        assert reader.scalar("SELECT count(*) FROM schema_migrations") == 3\n',
    '        assert reader.scalar("SELECT count(*) FROM schema_migrations") == CURRENT_SCHEMA_VERSION\n',
    1,
)
text = text.replace(
    '        assert restored.scalar("SELECT max(version) FROM schema_migrations") == 3\n',
    '        assert restored.scalar("SELECT max(version) FROM schema_migrations") == CURRENT_SCHEMA_VERSION\n',
    1,
)
db_path.write_text(text, encoding="utf-8")

replace_once(
    "README.md",
    "| `firmquant doctor` | 检查依赖、身份、数据、账本、锁、时钟、SDK、只读连接、合规和实盘锁定 |\n",
    "| `firmquant doctor` | 检查依赖、身份、数据、账本、锁、时钟、SDK、只读连接、合规和实盘锁定 |\n"
    "| `firmquant smoke-readonly` | 在真实部署机读取完整生产 authority surface 并持久化零写调用 receipt |\n",
    "README smoke command inventory",
)
