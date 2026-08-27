from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def write(path: str, text: str) -> None:
    (ROOT / path).write_text(text, encoding="utf-8")


def replace_once(text: str, old: str, new: str, *, label: str) -> str:
    count = text.count(old)
    if count == 0 and new in text:
        return text
    if count != 1:
        raise RuntimeError(f"{label}: expected exactly one old fragment, found {count}")
    return text.replace(old, new, 1)


def patch_schema() -> None:
    path = "src/firmquant/persistence/schema.py"
    text = read(path)
    marker = """    Migration.create(\n        version=3,\n        name=\"account_authority\",\n        statements=_ACCOUNT_AUTHORITY_SCHEMA,\n    ),\n)\n"""
    replacement = """    Migration.create(\n        version=3,\n        name=\"account_authority\",\n        statements=_ACCOUNT_AUTHORITY_SCHEMA,\n    ),\n    Migration.create(\n        version=4,\n        name=\"production_heartbeat\",\n        statements=(\n            \"\"\"\n            CREATE TABLE production_heartbeat (\n                singleton_id INTEGER PRIMARY KEY CHECK (singleton_id = 1),\n                mode TEXT NOT NULL CHECK (mode IN ('SHADOW','CANARY','LIVE')),\n                runtime_state TEXT NOT NULL CHECK (runtime_state IN (\n                    'DISARMED','STARTING','RECONCILING','READY','EXECUTING','DEGRADED','HALTED','STOPPING'\n                )),\n                observed_at TEXT NOT NULL,\n                host_hash TEXT NOT NULL CHECK (length(host_hash) = 64),\n                process_id INTEGER NOT NULL CHECK (process_id > 0),\n                writer_generation INTEGER NOT NULL CHECK (writer_generation > 0),\n                broker_connected INTEGER NOT NULL CHECK (broker_connected IN (0, 1)),\n                broker_read_healthy INTEGER NOT NULL CHECK (broker_read_healthy IN (0, 1)),\n                broker_write_healthy INTEGER NOT NULL CHECK (broker_write_healthy IN (0, 1)),\n                pending_events INTEGER NOT NULL CHECK (pending_events >= 0),\n                last_broker_event TEXT,\n                last_quote TEXT,\n                last_reconciliation TEXT,\n                last_decision TEXT,\n                last_execution TEXT,\n                control_request_state TEXT NOT NULL,\n                processed_events INTEGER NOT NULL CHECK (processed_events >= 0),\n                decisions INTEGER NOT NULL CHECK (decisions >= 0),\n                executions INTEGER NOT NULL CHECK (executions >= 0),\n                eod INTEGER NOT NULL CHECK (eod >= 0)\n            ) STRICT\n            \"\"\",\n        ),\n    ),\n)\n"""
    text = replace_once(text, marker, replacement, label="schema heartbeat migration")
    write(path, text)


def patch_config_and_risk() -> None:
    path = "src/firmquant/config.py"
    text = read(path)
    text = text.replace("    max_replacements: PositiveInteger = 2\n", "")
    write(path, text)

    path = "src/firmquant/risk/runtime.py"
    text = read(path)
    text = text.replace("        max_replacements=runtime.max_replacements,\n", "")
    write(path, text)

    path = "src/firmquant/risk/gate.py"
    text = read(path)
    text = text.replace("    max_replacements: int\n", "")
    text = text.replace('            ("max replacements", self.max_replacements),\n', "")
    text = text.replace("    disconnect_duration: timedelta\n", "    disconnect_duration: timedelta | None\n")
    text = text.replace("    replacement_count: int\n", "")
    text = text.replace("    unexplained_position_change: bool\n", "    unexplained_position_change: bool | None\n")
    text = text.replace("    corporate_action_suspected: bool\n", "    corporate_action_suspected: bool | None\n")
    text = text.replace("    clock_drift: timedelta\n", "    clock_drift: timedelta | None\n")
    text = text.replace('            ("replacement count", self.replacement_count),\n', "")
    text = text.replace(
        '        _duration(self.disconnect_duration, label="disconnect duration", allow_zero=True)\n'
        '        _duration(self.clock_drift, label="clock drift", allow_zero=True)\n',
        '        if self.disconnect_duration is not None:\n'
        '            _duration(self.disconnect_duration, label="disconnect duration", allow_zero=True)\n'
        '        if self.clock_drift is not None:\n'
        '            _duration(self.clock_drift, label="clock drift", allow_zero=True)\n',
    )
    text = text.replace(
        '            ("unexplained position change", self.unexplained_position_change),\n'
        '            ("corporate action suspected", self.corporate_action_suspected),\n',
        "",
    )
    bool_anchor = '        if not isinstance(self.limits, RiskLimits):\n'
    bool_insert = (
        '        for optional_label, optional_value in (\n'
        '            ("unexplained position change", self.unexplained_position_change),\n'
        '            ("corporate action suspected", self.corporate_action_suspected),\n'
        '        ):\n'
        '            if optional_value is not None and not isinstance(optional_value, bool):\n'
        '                raise DomainTypeError(f"risk {optional_label} must be bool or null")\n'
        + bool_anchor
    )
    text = replace_once(text, bool_anchor, bool_insert, label="optional risk booleans")
    text = text.replace(
        '        if context.unexplained_position_change:\n'
        '            halt.append("UNEXPLAINED_POSITION_CHANGE")\n'
        '        if context.corporate_action_suspected:\n'
        '            halt.append("CORPORATE_ACTION_SUSPECTED")\n',
        '        if context.unexplained_position_change is None:\n'
        '            halt.append("POSITION_CHANGE_UNVERIFIED")\n'
        '        elif context.unexplained_position_change:\n'
        '            halt.append("UNEXPLAINED_POSITION_CHANGE")\n'
        '        if context.corporate_action_suspected is None:\n'
        '            halt.append("CORPORATE_ACTION_UNVERIFIED")\n'
        '        elif context.corporate_action_suspected:\n'
        '            halt.append("CORPORATE_ACTION_SUSPECTED")\n',
    )
    text = text.replace(
        '        if context.clock_drift > limits.max_clock_drift:\n'
        '            halt.append("CLOCK_DRIFT_LIMIT")\n',
        '        if context.clock_drift is None:\n'
        '            halt.append("CLOCK_DRIFT_UNVERIFIED")\n'
        '        elif context.clock_drift > limits.max_clock_drift:\n'
        '            halt.append("CLOCK_DRIFT_LIMIT")\n',
    )
    text = text.replace(
        '        if not context.broker_connected and (context.disconnect_duration > limits.max_disconnect_duration):\n'
        '            halt.append("BROKER_DISCONNECT_LIMIT")\n',
        '        if not context.broker_connected:\n'
        '            if context.disconnect_duration is None:\n'
        '                halt.append("BROKER_DISCONNECT_DURATION_UNVERIFIED")\n'
        '            elif context.disconnect_duration > limits.max_disconnect_duration:\n'
        '                halt.append("BROKER_DISCONNECT_LIMIT")\n',
    )
    text = text.replace(
        '        if context.replacement_count >= limits.max_replacements:\n'
        '            block.append("REPLACEMENT_LIMIT")\n',
        "",
    )
    write(path, text)


def patch_composition() -> None:
    path = "src/firmquant/application/composition.py"
    text = read(path)
    text = replace_once(
        text,
        "from firmquant.broker.production_factory import build_production_xtquant_gateway\n",
        "from firmquant.broker.production_factory import (\n"
        "    build_production_xtquant_gateway,\n"
        "    build_readonly_xtquant_gateway,\n"
        ")\n",
        label="composition production factory import",
    )
    old = '''    def doctor_broker(self) -> BrokerGateway:\n        """Build a fresh read-only diagnostic gateway without write capability."""\n\n        identity = StrategyIdentity.locked()\n        try:\n            identity.verify()\n        except Exception as error:\n            raise OperatorCommandDenied("UQUANT_IDENTITY_UNAVAILABLE") from error\n        settings = self._settings()\n        account = _safe_account(self._account_path(settings))\n        return self._gateway(settings, account)\n'''
    new = '''    def doctor_broker(self) -> object:\n        """Build a fresh diagnostic broker; production returns a read-only XtQuant facade."""\n\n        identity = StrategyIdentity.locked()\n        try:\n            identity.verify()\n        except Exception as error:\n            raise OperatorCommandDenied("UQUANT_IDENTITY_UNAVAILABLE") from error\n        settings = self._settings()\n        if settings.mode in {Mode.SHADOW, Mode.CANARY, Mode.LIVE}:\n            try:\n                return build_readonly_xtquant_gateway(settings=settings, clock=self._clock)\n            except Exception as error:\n                code = str(error)\n                if "XTQUANT_SDK_UNAVAILABLE" in code:\n                    raise OperatorCommandDenied("XTQUANT_SDK_UNAVAILABLE") from error\n                raise OperatorCommandDenied("XTQUANT_RUNTIME_PREREQUISITES_UNAVAILABLE") from error\n        account = _safe_account(self._account_path(settings))\n        return self._gateway(settings, account)\n'''
    text = replace_once(text, old, new, label="composition readonly doctor")
    write(path, text)


def patch_operations() -> None:
    path = "src/firmquant/application/operations.py"
    text = read(path)
    text = replace_once(
        text,
        '        broker: ReadOnlyDoctorBroker | None = None\n'
        '        if self._doctor_broker_provider is not None:\n'
        '            try:\n'
        '                broker = self._doctor_broker_provider()\n'
        '            except Exception:\n'
        '                broker = None\n',
        '        broker: ReadOnlyDoctorBroker | None = None\n'
        '        if self._doctor_broker_provider is not None:\n'
        '            broker = self._doctor_broker_provider()\n',
        label="doctor construction failure",
    )
    old_return = '''        return {\n            "mode": settings.mode.value,\n            "runtime_state": status.state.value,\n            "armed": armed,\n            "arm_expires_at": expires_at,\n            "firmquant_commit": firmquant_commit,\n            "uquant_commit": source.uquant_commit,\n            "strategy_session": strategy_session,\n            "broker_connection": "UNKNOWN",\n            "last_quote": last_quote,\n            "last_reconciliation": (\n                None if latest_reconciliation is None else latest_reconciliation["completed_at"]\n            ),\n            "unresolved_orders": unresolved,\n            "current_cash": current_cash,\n            "actual_gross": actual_gross,\n            "target_gross": self._target_gross(database),\n            "kill_switch": self._kill_switch_tripped(database, status),\n            "blockers": sorted(blockers),\n        }\n'''
    new_return = '''        heartbeat = database.query_one(\n            "SELECT * FROM production_heartbeat WHERE singleton_id = 1"\n        )\n        heartbeat_age: float | None = None\n        process_health = "NOT_RUNNING"\n        broker_connection = "NOT_RUNNING"\n        broker_read_healthy = False\n        broker_write_healthy = False\n        if heartbeat is None:\n            blockers.add("PROCESS_NOT_RUNNING")\n        else:\n            try:\n                heartbeat_at = datetime.fromisoformat(str(heartbeat["observed_at"]))\n                if heartbeat_at.tzinfo is None or heartbeat_at.utcoffset() is None:\n                    raise ValueError\n                age = now - heartbeat_at\n                heartbeat_age = age.total_seconds()\n                if heartbeat_age < 0:\n                    raise ValueError\n            except ValueError as error:\n                raise OperatorCommandDenied("HEARTBEAT_INVALID") from error\n            broker_connection = (\n                "CONNECTED" if int(heartbeat["broker_connected"]) == 1 else "DISCONNECTED"\n            )\n            broker_read_healthy = int(heartbeat["broker_read_healthy"]) == 1\n            broker_write_healthy = int(heartbeat["broker_write_healthy"]) == 1\n            if heartbeat_age > 30.0:\n                process_health = "STALE"\n                blockers.add("HEARTBEAT_STALE")\n            else:\n                process_health = "HEALTHY"\n        effective_state = (\n            status.state.value if process_health == "HEALTHY" else RuntimeState.HALTED.value\n        )\n        return {\n            "mode": settings.mode.value,\n            "runtime_state": effective_state,\n            "stored_runtime_state": status.state.value,\n            "process_health": process_health,\n            "heartbeat_age": heartbeat_age,\n            "armed": armed,\n            "arm_expires_at": expires_at,\n            "firmquant_commit": firmquant_commit,\n            "uquant_commit": source.uquant_commit,\n            "strategy_session": strategy_session,\n            "broker_connection": broker_connection,\n            "broker_read_healthy": broker_read_healthy,\n            "broker_write_healthy": broker_write_healthy,\n            "last_quote": (last_quote if heartbeat is None else heartbeat["last_quote"]),\n            "last_reconciliation": (\n                None if heartbeat is None else heartbeat["last_reconciliation"]\n            ),\n            "last_broker_event": None if heartbeat is None else heartbeat["last_broker_event"],\n            "last_decision": None if heartbeat is None else heartbeat["last_decision"],\n            "last_execution": None if heartbeat is None else heartbeat["last_execution"],\n            "control_request_state": None if heartbeat is None else heartbeat["control_request_state"],\n            "writer_generation": None if heartbeat is None else heartbeat["writer_generation"],\n            "process_id": None if heartbeat is None else heartbeat["process_id"],\n            "host_hash": None if heartbeat is None else heartbeat["host_hash"],\n            "pending_events": None if heartbeat is None else heartbeat["pending_events"],\n            "unresolved_orders": unresolved,\n            "current_cash": current_cash,\n            "actual_gross": actual_gross,\n            "target_gross": self._target_gross(database),\n            "kill_switch": self._kill_switch_tripped(database, status),\n            "blockers": sorted(blockers),\n        }\n'''
    text = replace_once(text, old_return, new_return, label="status heartbeat authority")
    write(path, text)


def main() -> None:
    patch_schema()
    patch_config_and_risk()
    patch_composition()
    patch_operations()


if __name__ == "__main__":
    main()
