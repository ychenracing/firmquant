"""Checksummed, repeatable SQLite schema migrations."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final

from .database import Database, PersistenceError


class MigrationError(PersistenceError):
    """Raised when schema history or a migration cannot be proven safe."""


_MIGRATION_NAME: Final = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_CREATE_MIGRATIONS_TABLE: Final = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY CHECK (version > 0),
    name TEXT NOT NULL UNIQUE,
    checksum TEXT NOT NULL CHECK (length(checksum) = 64),
    applied_at TEXT NOT NULL
) STRICT
"""


@dataclass(frozen=True, slots=True)
class Migration:
    version: int
    name: str
    statements: tuple[str, ...]
    checksum: str

    def __post_init__(self) -> None:
        if isinstance(self.version, bool) or not isinstance(self.version, int) or self.version <= 0:
            raise MigrationError("migration version must be a positive integer")
        if not isinstance(self.name, str) or _MIGRATION_NAME.fullmatch(self.name) is None:
            raise MigrationError("migration name is not canonical")
        if not isinstance(self.statements, tuple) or not self.statements:
            raise MigrationError("migration must contain statements")
        if any(not isinstance(statement, str) or not statement.strip() for statement in self.statements):
            raise MigrationError("migration statement must be non-empty SQL")
        if self.checksum != self.calculate_checksum(self.version, self.name, self.statements):
            raise MigrationError("migration checksum does not match its statements")

    @staticmethod
    def calculate_checksum(version: int, name: str, statements: tuple[str, ...]) -> str:
        encoded = json.dumps(
            {"version": version, "name": name, "statements": statements},
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @classmethod
    def create(cls, *, version: int, name: str, statements: tuple[str, ...]) -> Migration:
        return cls(
            version=version,
            name=name,
            statements=statements,
            checksum=cls.calculate_checksum(version, name, statements),
        )


_CORE_SCHEMA: Final = (
    """
    CREATE TABLE runtime_state (
        singleton_id INTEGER PRIMARY KEY CHECK (singleton_id = 1),
        mode TEXT NOT NULL CHECK (mode IN ('REPLAY','PAPER','SHADOW','CANARY','LIVE')),
        state TEXT NOT NULL CHECK (state IN (
            'DISARMED','STARTING','RECONCILING','READY','EXECUTING','DEGRADED','HALTED','STOPPING'
        )),
        revision INTEGER NOT NULL CHECK (revision >= 0),
        reason TEXT NOT NULL,
        blockers_json TEXT NOT NULL CHECK (json_valid(blockers_json)),
        updated_at TEXT NOT NULL
    ) STRICT
    """,
    """
    CREATE TABLE arm_leases (
        lease_id TEXT PRIMARY KEY,
        mode TEXT NOT NULL CHECK (mode IN ('CANARY','LIVE')),
        host_hash TEXT NOT NULL CHECK (length(host_hash) = 64),
        account_hash TEXT NOT NULL CHECK (length(account_hash) = 64),
        firmquant_commit TEXT NOT NULL CHECK (length(firmquant_commit) = 40),
        uquant_commit TEXT NOT NULL CHECK (length(uquant_commit) = 40),
        config_sha256 TEXT NOT NULL CHECK (length(config_sha256) = 64),
        identity_payload_sha256 TEXT NOT NULL CHECK (length(identity_payload_sha256) = 64),
        issued_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        revoked_at TEXT,
        revoke_reason TEXT,
        lease_mac TEXT NOT NULL CHECK (length(lease_mac) = 64)
    ) STRICT
    """,
    """
    CREATE TABLE decision_snapshots (
        decision_id TEXT PRIMARY KEY,
        strategy_session TEXT NOT NULL,
        input_fingerprint TEXT NOT NULL CHECK (length(input_fingerprint) = 64),
        firmquant_commit TEXT NOT NULL CHECK (length(firmquant_commit) = 40),
        uquant_commit TEXT NOT NULL CHECK (length(uquant_commit) = 40),
        uquant_code_fingerprint TEXT NOT NULL CHECK (length(uquant_code_fingerprint) = 64),
        uquant_config_fingerprint TEXT NOT NULL CHECK (length(uquant_config_fingerprint) = 64),
        data_manifest_sha256 TEXT NOT NULL CHECK (length(data_manifest_sha256) = 64),
        universe_manifest_sha256 TEXT NOT NULL CHECK (length(universe_manifest_sha256) = 64),
        broker_snapshot_sha256 TEXT NOT NULL CHECK (length(broker_snapshot_sha256) = 64),
        account_before_sha256 TEXT NOT NULL CHECK (length(account_before_sha256) = 64),
        account_after_sha256 TEXT NOT NULL CHECK (length(account_after_sha256) = 64),
        payload_json TEXT NOT NULL CHECK (json_valid(payload_json)),
        payload_sha256 TEXT NOT NULL CHECK (length(payload_sha256) = 64),
        created_at TEXT NOT NULL,
        supersedes_decision_id TEXT REFERENCES decision_snapshots(decision_id),
        UNIQUE (strategy_session, input_fingerprint)
    ) STRICT
    """,
    """
    CREATE TABLE execution_intents (
        execution_id TEXT PRIMARY KEY,
        decision_id TEXT NOT NULL REFERENCES decision_snapshots(decision_id),
        idempotency_key TEXT NOT NULL UNIQUE CHECK (length(idempotency_key) = 64),
        uquant_order_id TEXT NOT NULL,
        symbol TEXT NOT NULL,
        side TEXT NOT NULL CHECK (side IN ('BUY','SELL')),
        requested_shares INTEGER NOT NULL CHECK (requested_shares > 0),
        filled_shares INTEGER NOT NULL DEFAULT 0 CHECK (
            filled_shares >= 0 AND filled_shares <= requested_shares
        ),
        state TEXT NOT NULL CHECK (state IN (
            'PLANNED','VALIDATED','ARMED','SUBMITTING','ACKNOWLEDGED','PARTIALLY_FILLED',
            'FILLED','CANCEL_REQUESTED','CANCELLED','REJECTED','EXPIRED','UNKNOWN'
        )),
        strategy_session TEXT NOT NULL,
        uquant_source_sha TEXT NOT NULL CHECK (length(uquant_source_sha) = 40),
        aggregate_json TEXT NOT NULL CHECK (json_valid(aggregate_json)),
        version INTEGER NOT NULL CHECK (version >= 0),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE (decision_id, uquant_order_id)
    ) STRICT
    """,
    """
    CREATE TABLE broker_orders (
        broker_order_id TEXT PRIMARY KEY,
        execution_id TEXT REFERENCES execution_intents(execution_id),
        ownership TEXT NOT NULL CHECK (ownership IN ('SYSTEM','EXTERNAL','UNKNOWN')),
        client_order_id TEXT UNIQUE,
        symbol TEXT NOT NULL,
        side TEXT NOT NULL CHECK (side IN ('BUY','SELL')),
        status TEXT NOT NULL,
        requested_shares INTEGER NOT NULL CHECK (requested_shares > 0),
        filled_shares INTEGER NOT NULL CHECK (
            filled_shares >= 0 AND filled_shares <= requested_shares
        ),
        limit_price TEXT,
        session_date TEXT NOT NULL,
        last_event_sequence INTEGER CHECK (last_event_sequence IS NULL OR last_event_sequence >= 0),
        event_time TEXT NOT NULL,
        received_at TEXT NOT NULL,
        raw_payload_sha256 TEXT NOT NULL CHECK (length(raw_payload_sha256) = 64)
    ) STRICT
    """,
    """
    CREATE TABLE broker_order_attempts (
        attempt_id TEXT PRIMARY KEY,
        execution_id TEXT NOT NULL REFERENCES execution_intents(execution_id),
        attempt_number INTEGER NOT NULL CHECK (attempt_number > 0),
        state TEXT NOT NULL CHECK (state IN ('SUBMITTING','RETURNED','UNKNOWN','FAILED_LOCAL')),
        started_at TEXT NOT NULL,
        completed_at TEXT,
        broker_order_id TEXT REFERENCES broker_orders(broker_order_id),
        UNIQUE (execution_id, attempt_number)
    ) STRICT
    """,
    """
    CREATE TABLE order_commands (
        command_id TEXT PRIMARY KEY,
        attempt_id TEXT NOT NULL UNIQUE REFERENCES broker_order_attempts(attempt_id),
        command_kind TEXT NOT NULL CHECK (command_kind IN ('SUBMIT','CANCEL')),
        payload_json TEXT NOT NULL CHECK (json_valid(payload_json)),
        payload_sha256 TEXT NOT NULL CHECK (length(payload_sha256) = 64),
        created_at TEXT NOT NULL
    ) STRICT
    """,
    """
    CREATE TABLE broker_responses (
        response_id TEXT PRIMARY KEY,
        attempt_id TEXT NOT NULL REFERENCES broker_order_attempts(attempt_id),
        response_kind TEXT NOT NULL,
        payload_json TEXT NOT NULL CHECK (json_valid(payload_json)),
        payload_sha256 TEXT NOT NULL CHECK (length(payload_sha256) = 64),
        received_at TEXT NOT NULL
    ) STRICT
    """,
    """
    CREATE TABLE broker_events (
        broker_event_id TEXT PRIMARY KEY,
        event_type TEXT NOT NULL,
        broker_sequence INTEGER CHECK (broker_sequence IS NULL OR broker_sequence >= 0),
        session_date TEXT NOT NULL,
        event_time TEXT NOT NULL,
        received_at TEXT NOT NULL,
        safe_payload_json TEXT NOT NULL CHECK (json_valid(safe_payload_json)),
        safe_payload_sha256 TEXT NOT NULL CHECK (length(safe_payload_sha256) = 64),
        raw_payload_sha256 TEXT NOT NULL CHECK (length(raw_payload_sha256) = 64),
        recorded_at TEXT NOT NULL
    ) STRICT
    """,
    """
    CREATE TABLE domain_events (
        domain_event_id TEXT PRIMARY KEY,
        broker_event_id TEXT REFERENCES broker_events(broker_event_id),
        aggregate_type TEXT NOT NULL,
        aggregate_id TEXT NOT NULL,
        event_type TEXT NOT NULL,
        payload_json TEXT NOT NULL CHECK (json_valid(payload_json)),
        payload_sha256 TEXT NOT NULL CHECK (length(payload_sha256) = 64),
        occurred_at TEXT NOT NULL,
        recorded_at TEXT NOT NULL
    ) STRICT
    """,
    """
    CREATE TABLE fills (
        broker_fill_id TEXT PRIMARY KEY,
        identity_kind TEXT NOT NULL CHECK (identity_kind IN ('BROKER','COMPOSITE')),
        broker_order_id TEXT NOT NULL,
        execution_id TEXT REFERENCES execution_intents(execution_id),
        broker_event_id TEXT REFERENCES broker_events(broker_event_id),
        symbol TEXT NOT NULL,
        side TEXT NOT NULL CHECK (side IN ('BUY','SELL')),
        shares INTEGER NOT NULL CHECK (shares > 0),
        price TEXT NOT NULL,
        commission TEXT NOT NULL,
        stamp_duty TEXT NOT NULL,
        transfer_fee TEXT NOT NULL,
        session_date TEXT NOT NULL,
        event_time TEXT NOT NULL,
        raw_payload_sha256 TEXT NOT NULL CHECK (length(raw_payload_sha256) = 64)
    ) STRICT
    """,
    """
    CREATE TABLE broker_snapshots (
        snapshot_id TEXT PRIMARY KEY,
        account_id_hash TEXT NOT NULL CHECK (length(account_id_hash) = 64),
        account_type TEXT NOT NULL,
        session_date TEXT NOT NULL,
        captured_at TEXT NOT NULL,
        broker_event_watermark INTEGER NOT NULL CHECK (broker_event_watermark >= 0),
        raw_payload_sha256 TEXT NOT NULL CHECK (length(raw_payload_sha256) = 64),
        complete INTEGER NOT NULL CHECK (complete = 1)
    ) STRICT
    """,
    """
    CREATE TABLE position_snapshots (
        snapshot_id TEXT NOT NULL REFERENCES broker_snapshots(snapshot_id),
        symbol TEXT NOT NULL,
        total_shares INTEGER NOT NULL CHECK (total_shares >= 0),
        sellable_shares INTEGER NOT NULL CHECK (
            sellable_shares >= 0 AND sellable_shares <= total_shares
        ),
        average_cost TEXT,
        market_value TEXT NOT NULL,
        PRIMARY KEY (snapshot_id, symbol)
    ) WITHOUT ROWID, STRICT
    """,
    """
    CREATE TABLE cash_snapshots (
        snapshot_id TEXT PRIMARY KEY REFERENCES broker_snapshots(snapshot_id),
        available_cash TEXT NOT NULL,
        total_assets TEXT NOT NULL
    ) WITHOUT ROWID, STRICT
    """,
    """
    CREATE TABLE reconciliation_runs (
        reconciliation_id TEXT PRIMARY KEY,
        kind TEXT NOT NULL CHECK (kind IN ('STARTUP','INTRADAY','EOD','MANUAL','RECOVERY')),
        strategy_session TEXT,
        started_at TEXT NOT NULL,
        completed_at TEXT,
        passed INTEGER CHECK (passed IS NULL OR passed IN (0,1)),
        blockers_json TEXT NOT NULL CHECK (json_valid(blockers_json)),
        details_json TEXT NOT NULL CHECK (json_valid(details_json)),
        details_sha256 TEXT NOT NULL CHECK (length(details_sha256) = 64)
    ) STRICT
    """,
    """
    CREATE TABLE risk_events (
        risk_event_id TEXT PRIMARY KEY,
        severity TEXT NOT NULL CHECK (severity IN ('INFO','WARNING','CRITICAL')),
        code TEXT NOT NULL,
        execution_id TEXT REFERENCES execution_intents(execution_id),
        symbol TEXT,
        payload_json TEXT NOT NULL CHECK (json_valid(payload_json)),
        payload_sha256 TEXT NOT NULL CHECK (length(payload_sha256) = 64),
        created_at TEXT NOT NULL
    ) STRICT
    """,
    """
    CREATE TABLE alerts (
        alert_id TEXT PRIMARY KEY,
        severity TEXT NOT NULL CHECK (severity IN ('INFO','WARNING','CRITICAL')),
        code TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('OPEN','ACKNOWLEDGED','RESOLVED')),
        payload_json TEXT NOT NULL CHECK (json_valid(payload_json)),
        payload_sha256 TEXT NOT NULL CHECK (length(payload_sha256) = 64),
        created_at TEXT NOT NULL,
        acknowledged_at TEXT,
        resolved_at TEXT
    ) STRICT
    """,
    """
    CREATE TABLE audit_events (
        sequence INTEGER PRIMARY KEY AUTOINCREMENT,
        audit_event_id TEXT NOT NULL UNIQUE,
        category TEXT NOT NULL,
        actor TEXT NOT NULL,
        payload_json TEXT NOT NULL CHECK (json_valid(payload_json)),
        payload_sha256 TEXT NOT NULL CHECK (length(payload_sha256) = 64),
        previous_hash TEXT NOT NULL CHECK (length(previous_hash) = 64),
        chain_hash TEXT NOT NULL UNIQUE CHECK (length(chain_hash) = 64),
        created_at TEXT NOT NULL
    ) STRICT
    """,
    """
    CREATE TABLE writer_leases (
        singleton_id INTEGER PRIMARY KEY CHECK (singleton_id = 1),
        owner_id TEXT NOT NULL,
        host_hash TEXT NOT NULL CHECK (length(host_hash) = 64),
        process_id INTEGER NOT NULL CHECK (process_id > 0),
        acquired_at TEXT NOT NULL,
        renewed_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        generation INTEGER NOT NULL CHECK (generation > 0)
    ) STRICT
    """,
    """
    CREATE TABLE backup_receipts (
        backup_id TEXT PRIMARY KEY,
        database_sha256 TEXT NOT NULL CHECK (length(database_sha256) = 64),
        account_state_sha256 TEXT CHECK (
            account_state_sha256 IS NULL OR length(account_state_sha256) = 64
        ),
        manifest_json TEXT NOT NULL CHECK (json_valid(manifest_json)),
        manifest_sha256 TEXT NOT NULL CHECK (length(manifest_sha256) = 64),
        created_at TEXT NOT NULL,
        verified_at TEXT,
        verification_status TEXT NOT NULL CHECK (
            verification_status IN ('PENDING','VERIFIED','FAILED')
        )
    ) STRICT
    """,
    """
    CREATE TABLE account_operations (
        operation_id TEXT PRIMARY KEY,
        operation_kind TEXT NOT NULL,
        stage TEXT NOT NULL CHECK (stage IN (
            'PREPARED','FILE_COMMITTED','RECEIPT_COMMITTED','CONTRADICTION'
        )),
        account_before_sha256 TEXT NOT NULL CHECK (length(account_before_sha256) = 64),
        expected_account_after_sha256 TEXT NOT NULL CHECK (
            length(expected_account_after_sha256) = 64
        ),
        actual_account_after_sha256 TEXT CHECK (
            actual_account_after_sha256 IS NULL OR length(actual_account_after_sha256) = 64
        ),
        payload_json TEXT NOT NULL CHECK (json_valid(payload_json)),
        payload_sha256 TEXT NOT NULL CHECK (length(payload_sha256) = 64),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    ) STRICT
    """,
    "CREATE INDEX execution_intents_state_idx ON execution_intents(state, strategy_session)",
    "CREATE INDEX broker_orders_execution_idx ON broker_orders(execution_id, status)",
    "CREATE INDEX broker_events_received_idx ON broker_events(received_at, broker_event_id)",
    "CREATE INDEX fills_order_idx ON fills(broker_order_id, event_time)",
    "CREATE INDEX reconciliation_completed_idx ON reconciliation_runs(completed_at, passed)",
)

_APPEND_ONLY_TABLES: Final = (
    "schema_migrations",
    "decision_snapshots",
    "broker_events",
    "domain_events",
    "fills",
    "broker_snapshots",
    "position_snapshots",
    "cash_snapshots",
    "risk_events",
    "audit_events",
    "backup_receipts",
)

_APPEND_ONLY_TRIGGERS: Final = tuple(
    statement
    for table in _APPEND_ONLY_TABLES
    for statement in (
        f"""
        CREATE TRIGGER {table}_reject_update
        BEFORE UPDATE ON {table}
        BEGIN
            SELECT RAISE(ABORT, '{table} is append-only');
        END
        """,
        f"""
        CREATE TRIGGER {table}_reject_delete
        BEFORE DELETE ON {table}
        BEGIN
            SELECT RAISE(ABORT, '{table} is append-only');
        END
        """,
    )
)

_ACCOUNT_AUTHORITY_SCHEMA: Final = (
    """
    CREATE TABLE account_bindings (
        binding_id TEXT PRIMARY KEY,
        singleton_id INTEGER NOT NULL UNIQUE CHECK (singleton_id = 1),
        account_id_hash TEXT NOT NULL CHECK (length(account_id_hash) = 64),
        account_type TEXT NOT NULL CHECK (account_type = 'CASH'),
        broker_snapshot_sha256 TEXT NOT NULL CHECK (length(broker_snapshot_sha256) = 64),
        account_state_sha256 TEXT NOT NULL CHECK (length(account_state_sha256) = 64),
        uquant_commit TEXT NOT NULL CHECK (length(uquant_commit) = 40),
        uquant_code_fingerprint TEXT NOT NULL CHECK (length(uquant_code_fingerprint) = 64),
        data_hash TEXT NOT NULL CHECK (length(data_hash) = 64),
        data_as_of TEXT NOT NULL,
        data_symbols_json TEXT NOT NULL CHECK (json_valid(data_symbols_json)),
        payload_json TEXT NOT NULL CHECK (json_valid(payload_json)),
        payload_sha256 TEXT NOT NULL CHECK (length(payload_sha256) = 64),
        created_at TEXT NOT NULL
    ) STRICT
    """,
    """
    CREATE TABLE account_bootstrap_operations (
        operation_id TEXT PRIMARY KEY,
        stage TEXT NOT NULL CHECK (stage IN (
            'PREPARED','FILE_COMMITTED','BINDING_COMMITTED','CONTRADICTION'
        )),
        account_state_sha256 TEXT NOT NULL CHECK (length(account_state_sha256) = 64),
        broker_snapshot_sha256 TEXT NOT NULL CHECK (length(broker_snapshot_sha256) = 64),
        binding_payload_json TEXT NOT NULL CHECK (json_valid(binding_payload_json)),
        binding_payload_sha256 TEXT NOT NULL CHECK (length(binding_payload_sha256) = 64),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    ) STRICT
    """,
    """
    CREATE TABLE reviewed_account_adjustments (
        adjustment_id TEXT PRIMARY KEY,
        account_id_hash TEXT NOT NULL CHECK (length(account_id_hash) = 64),
        symbol TEXT NOT NULL,
        session_date TEXT NOT NULL,
        adjustment_type TEXT NOT NULL,
        coverage_kind TEXT NOT NULL,
        broker_snapshot_sha256 TEXT NOT NULL CHECK (length(broker_snapshot_sha256) = 64),
        difference_sha256 TEXT NOT NULL CHECK (length(difference_sha256) = 64),
        audit_summary_sha256 TEXT NOT NULL CHECK (length(audit_summary_sha256) = 64),
        payload_json TEXT NOT NULL CHECK (json_valid(payload_json)),
        payload_sha256 TEXT NOT NULL CHECK (length(payload_sha256) = 64),
        created_at TEXT NOT NULL
    ) STRICT
    """,
    """
    CREATE INDEX reviewed_account_adjustments_lookup_idx
    ON reviewed_account_adjustments(
        account_id_hash, symbol, session_date, coverage_kind,
        broker_snapshot_sha256, difference_sha256
    )
    """,
    """
    CREATE TRIGGER account_bindings_reject_update
    BEFORE UPDATE ON account_bindings
    BEGIN
        SELECT RAISE(ABORT, 'account_bindings is append-only');
    END
    """,
    """
    CREATE TRIGGER account_bindings_reject_delete
    BEFORE DELETE ON account_bindings
    BEGIN
        SELECT RAISE(ABORT, 'account_bindings is append-only');
    END
    """,
    """
    CREATE TRIGGER reviewed_account_adjustments_reject_update
    BEFORE UPDATE ON reviewed_account_adjustments
    BEGIN
        SELECT RAISE(ABORT, 'reviewed_account_adjustments is append-only');
    END
    """,
    """
    CREATE TRIGGER reviewed_account_adjustments_reject_delete
    BEFORE DELETE ON reviewed_account_adjustments
    BEGIN
        SELECT RAISE(ABORT, 'reviewed_account_adjustments is append-only');
    END
    """,
)

_OPERATIONAL_AUTHORITY_SCHEMA: Final = (
    "ALTER TABLE broker_snapshots ADD COLUMN started_at TEXT",
    "ALTER TABLE broker_snapshots ADD COLUMN completed_at TEXT",
    """
    ALTER TABLE broker_snapshots ADD COLUMN duration_ms INTEGER CHECK (
        (started_at IS NULL AND completed_at IS NULL AND duration_ms IS NULL)
        OR
        (started_at IS NOT NULL AND completed_at IS NOT NULL AND duration_ms >= 0)
    )
    """,
    "ALTER TABLE backup_receipts ADD COLUMN bundle_schema_version INTEGER CHECK (bundle_schema_version > 0)",
    """
    ALTER TABLE backup_receipts ADD COLUMN operational_schema_version INTEGER
    CHECK (operational_schema_version > 0)
    """,
    """
    ALTER TABLE backup_receipts ADD COLUMN reason TEXT
    CHECK (reason IN ('SESSION_CLOSE','MODE_TRANSITION','ACCOUNT_REBASELINE'))
    """,
    """
    ALTER TABLE backup_receipts ADD COLUMN deployment_identity_sha256 TEXT
    CHECK (deployment_identity_sha256 IS NULL OR length(deployment_identity_sha256) = 64)
    """,
    """
    ALTER TABLE backup_receipts ADD COLUMN operational_evidence_identity_sha256 TEXT
    CHECK (
        operational_evidence_identity_sha256 IS NULL
        OR length(operational_evidence_identity_sha256) = 64
    )
    """,
    """
    ALTER TABLE backup_receipts ADD COLUMN account_authority_epoch INTEGER
    CHECK (account_authority_epoch IS NULL OR account_authority_epoch > 0)
    """,
    """
    ALTER TABLE backup_receipts ADD COLUMN mode_epoch INTEGER
    CHECK (mode_epoch IS NULL OR mode_epoch > 0)
    """,
    "ALTER TABLE backup_receipts ADD COLUMN broker_snapshot_id TEXT",
    """
    ALTER TABLE backup_receipts ADD COLUMN broker_snapshot_sha256 TEXT
    CHECK (broker_snapshot_sha256 IS NULL OR length(broker_snapshot_sha256) = 64)
    """,
    """
    CREATE TABLE account_authority_epochs (
        epoch INTEGER PRIMARY KEY CHECK (epoch > 0),
        account_id_hash TEXT NOT NULL CHECK (length(account_id_hash) = 64),
        account_state_sha256 TEXT NOT NULL CHECK (length(account_state_sha256) = 64),
        deployment_identity_sha256 TEXT CHECK (
            deployment_identity_sha256 IS NULL OR length(deployment_identity_sha256) = 64
        ),
        source_binding_id TEXT REFERENCES account_bindings(binding_id),
        payload_json TEXT NOT NULL CHECK (json_valid(payload_json)),
        payload_sha256 TEXT NOT NULL UNIQUE CHECK (length(payload_sha256) = 64),
        created_at TEXT NOT NULL
    ) STRICT
    """,
    """
    CREATE TABLE account_authority_active (
        singleton_id INTEGER PRIMARY KEY CHECK (singleton_id = 1),
        epoch INTEGER NOT NULL REFERENCES account_authority_epochs(epoch)
    ) STRICT
    """,
    """
    CREATE TABLE mode_epochs (
        epoch INTEGER PRIMARY KEY CHECK (epoch > 0),
        mode TEXT NOT NULL CHECK (mode IN ('REPLAY','PAPER','SHADOW','CANARY','LIVE')),
        deployment_identity_sha256 TEXT CHECK (
            deployment_identity_sha256 IS NULL OR length(deployment_identity_sha256) = 64
        ),
        caps_sha256 TEXT CHECK (caps_sha256 IS NULL OR length(caps_sha256) = 64),
        payload_json TEXT NOT NULL CHECK (json_valid(payload_json)),
        payload_sha256 TEXT NOT NULL UNIQUE CHECK (length(payload_sha256) = 64),
        created_at TEXT NOT NULL
    ) STRICT
    """,
    """
    CREATE TABLE mode_epoch_active (
        singleton_id INTEGER PRIMARY KEY CHECK (singleton_id = 1),
        epoch INTEGER NOT NULL REFERENCES mode_epochs(epoch)
    ) STRICT
    """,
    """
    CREATE TABLE deployment_identities (
        deployment_identity_sha256 TEXT PRIMARY KEY CHECK (length(deployment_identity_sha256) = 64),
        account_id_hash TEXT NOT NULL CHECK (length(account_id_hash) = 64),
        account_authority_epoch INTEGER NOT NULL REFERENCES account_authority_epochs(epoch),
        mode_epoch INTEGER NOT NULL REFERENCES mode_epochs(epoch),
        mode TEXT NOT NULL CHECK (mode IN ('REPLAY','PAPER','SHADOW','CANARY','LIVE')),
        payload_json TEXT NOT NULL CHECK (json_valid(payload_json)),
        payload_sha256 TEXT NOT NULL UNIQUE CHECK (length(payload_sha256) = 64),
        created_at TEXT NOT NULL
    ) STRICT
    """,
    """
    CREATE TABLE operational_evidence_receipts (
        receipt_id TEXT PRIMARY KEY,
        operational_evidence_identity_sha256 TEXT NOT NULL UNIQUE CHECK (
            length(operational_evidence_identity_sha256) = 64
        ),
        deployment_identity_sha256 TEXT NOT NULL REFERENCES deployment_identities(
            deployment_identity_sha256
        ),
        account_authority_epoch INTEGER NOT NULL REFERENCES account_authority_epochs(epoch),
        mode_epoch INTEGER NOT NULL REFERENCES mode_epochs(epoch),
        account_state_sha256 TEXT NOT NULL CHECK (length(account_state_sha256) = 64),
        broker_snapshot_id TEXT NOT NULL REFERENCES broker_snapshots(snapshot_id),
        strategy_session TEXT NOT NULL,
        phase TEXT NOT NULL,
        kind TEXT NOT NULL,
        payload_json TEXT NOT NULL CHECK (json_valid(payload_json)),
        payload_sha256 TEXT NOT NULL UNIQUE CHECK (length(payload_sha256) = 64),
        created_at TEXT NOT NULL
    ) STRICT
    """,
    """
    CREATE INDEX operational_evidence_current_lookup_idx
    ON operational_evidence_receipts(
        deployment_identity_sha256,account_authority_epoch,mode_epoch,phase,kind
    )
    """,
    """
    CREATE TABLE account_rebaseline_operations (
        operation_id TEXT PRIMARY KEY,
        stage TEXT NOT NULL CHECK (stage IN (
            'PREPARED','FILE_COMMITTED','RECEIPT_COMMITTED','CONTRADICTION'
        )),
        source_epoch INTEGER NOT NULL CHECK (source_epoch > 0),
        target_epoch INTEGER NOT NULL CHECK (target_epoch = source_epoch + 1),
        account_id_hash TEXT NOT NULL CHECK (length(account_id_hash) = 64),
        account_before_sha256 TEXT NOT NULL CHECK (length(account_before_sha256) = 64),
        candidate_account_state_sha256 TEXT NOT NULL CHECK (
            length(candidate_account_state_sha256) = 64
        ),
        deployment_identity_sha256 TEXT NOT NULL CHECK (length(deployment_identity_sha256) = 64),
        broker_snapshot_id TEXT NOT NULL,
        broker_snapshot_sha256 TEXT NOT NULL CHECK (length(broker_snapshot_sha256) = 64),
        backup_id TEXT NOT NULL,
        reviewed_evidence_sha256 TEXT NOT NULL CHECK (length(reviewed_evidence_sha256) = 64),
        account_path_sha256 TEXT NOT NULL CHECK (length(account_path_sha256) = 64),
        actual_account_after_sha256 TEXT CHECK (
            actual_account_after_sha256 IS NULL OR length(actual_account_after_sha256) = 64
        ),
        reason TEXT NOT NULL,
        payload_json TEXT NOT NULL CHECK (json_valid(payload_json)),
        payload_sha256 TEXT NOT NULL CHECK (length(payload_sha256) = 64),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    ) STRICT
    """,
    """
    CREATE TABLE account_rebaseline_receipts (
        receipt_id TEXT PRIMARY KEY,
        operation_id TEXT NOT NULL UNIQUE REFERENCES account_rebaseline_operations(operation_id),
        account_authority_epoch INTEGER NOT NULL UNIQUE REFERENCES account_authority_epochs(epoch),
        deployment_identity_sha256 TEXT NOT NULL CHECK (length(deployment_identity_sha256) = 64),
        backup_id TEXT NOT NULL REFERENCES backup_receipts(backup_id),
        payload_json TEXT NOT NULL CHECK (json_valid(payload_json)),
        payload_sha256 TEXT NOT NULL UNIQUE CHECK (length(payload_sha256) = 64),
        created_at TEXT NOT NULL
    ) STRICT
    """,
    """
    CREATE TABLE mode_transition_operations (
        operation_id TEXT PRIMARY KEY,
        stage TEXT NOT NULL CHECK (stage IN (
            'PREPARED','EPOCH_COMMITTED','RECEIPT_COMMITTED','CONTRADICTION'
        )),
        source_epoch INTEGER NOT NULL CHECK (source_epoch > 0),
        target_epoch INTEGER NOT NULL CHECK (target_epoch = source_epoch + 1),
        source_mode TEXT NOT NULL CHECK (source_mode IN ('PAPER','SHADOW','CANARY','LIVE')),
        target_mode TEXT NOT NULL CHECK (target_mode IN ('PAPER','SHADOW','CANARY','LIVE')),
        deployment_identity_sha256 TEXT NOT NULL CHECK (length(deployment_identity_sha256) = 64),
        backup_id TEXT NOT NULL,
        evidence_sha256 TEXT NOT NULL CHECK (length(evidence_sha256) = 64),
        payload_json TEXT NOT NULL CHECK (json_valid(payload_json)),
        payload_sha256 TEXT NOT NULL CHECK (length(payload_sha256) = 64),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    ) STRICT
    """,
    """
    CREATE TABLE mode_transition_receipts (
        receipt_id TEXT PRIMARY KEY,
        operation_id TEXT NOT NULL UNIQUE REFERENCES mode_transition_operations(operation_id),
        mode_epoch INTEGER NOT NULL UNIQUE REFERENCES mode_epochs(epoch),
        deployment_identity_sha256 TEXT NOT NULL CHECK (length(deployment_identity_sha256) = 64),
        backup_id TEXT NOT NULL REFERENCES backup_receipts(backup_id),
        payload_json TEXT NOT NULL CHECK (json_valid(payload_json)),
        payload_sha256 TEXT NOT NULL UNIQUE CHECK (length(payload_sha256) = 64),
        created_at TEXT NOT NULL
    ) STRICT
    """,
    """
    CREATE TABLE backup_publication_operations (
        operation_id TEXT PRIMARY KEY,
        backup_id TEXT NOT NULL UNIQUE,
        stage TEXT NOT NULL CHECK (stage IN (
            'PREPARED','PUBLISHED','RECEIPT_COMMITTED','CONTRADICTION'
        )),
        reason TEXT NOT NULL CHECK (
            reason IN ('SESSION_CLOSE','MODE_TRANSITION','ACCOUNT_REBASELINE')
        ),
        manifest_sha256 TEXT NOT NULL CHECK (length(manifest_sha256) = 64),
        database_sha256 TEXT NOT NULL CHECK (length(database_sha256) = 64),
        account_state_sha256 TEXT NOT NULL CHECK (length(account_state_sha256) = 64),
        deployment_identity_sha256 TEXT NOT NULL CHECK (length(deployment_identity_sha256) = 64),
        operational_evidence_identity_sha256 TEXT NOT NULL CHECK (
            length(operational_evidence_identity_sha256) = 64
        ),
        account_authority_epoch INTEGER NOT NULL REFERENCES account_authority_epochs(epoch),
        mode_epoch INTEGER NOT NULL REFERENCES mode_epochs(epoch),
        bundle_name TEXT,
        payload_json TEXT NOT NULL CHECK (json_valid(payload_json)),
        payload_sha256 TEXT NOT NULL UNIQUE CHECK (length(payload_sha256) = 64),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    ) STRICT
    """,
    """
    CREATE TABLE restore_operations (
        operation_id TEXT PRIMARY KEY,
        restore_id TEXT NOT NULL UNIQUE,
        backup_id TEXT NOT NULL,
        stage TEXT NOT NULL CHECK (stage IN (
            'PREPARED','STAGED','PUBLISHED','RECEIPT_COMMITTED','CONTRADICTION'
        )),
        source_manifest_sha256 TEXT NOT NULL CHECK (length(source_manifest_sha256) = 64),
        source_database_sha256 TEXT NOT NULL CHECK (length(source_database_sha256) = 64),
        destination_identity_sha256 TEXT NOT NULL CHECK (length(destination_identity_sha256) = 64),
        sanitized_state_sha256 TEXT CHECK (
            sanitized_state_sha256 IS NULL OR length(sanitized_state_sha256) = 64
        ),
        final_directory_name TEXT,
        deployment_identity_sha256 TEXT NOT NULL CHECK (length(deployment_identity_sha256) = 64),
        operational_evidence_identity_sha256 TEXT NOT NULL CHECK (
            length(operational_evidence_identity_sha256) = 64
        ),
        account_authority_epoch INTEGER NOT NULL CHECK (account_authority_epoch > 0),
        mode_epoch INTEGER NOT NULL CHECK (mode_epoch > 0),
        payload_json TEXT NOT NULL CHECK (json_valid(payload_json)),
        payload_sha256 TEXT NOT NULL UNIQUE CHECK (length(payload_sha256) = 64),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    ) STRICT
    """,
    """
    CREATE TABLE restore_receipts (
        restore_id TEXT PRIMARY KEY,
        source_backup_id TEXT NOT NULL,
        source_manifest_sha256 TEXT NOT NULL CHECK (length(source_manifest_sha256) = 64),
        source_reason TEXT NOT NULL CHECK (
            source_reason IN ('SESSION_CLOSE','MODE_TRANSITION','ACCOUNT_REBASELINE')
        ),
        deployment_identity_sha256 TEXT NOT NULL CHECK (length(deployment_identity_sha256) = 64),
        operational_evidence_identity_sha256 TEXT NOT NULL CHECK (
            length(operational_evidence_identity_sha256) = 64
        ),
        account_authority_epoch INTEGER NOT NULL CHECK (account_authority_epoch > 0),
        mode_epoch INTEGER NOT NULL CHECK (mode_epoch > 0),
        source_database_sha256 TEXT NOT NULL CHECK (length(source_database_sha256) = 64),
        sanitized_state_sha256 TEXT NOT NULL CHECK (length(sanitized_state_sha256) = 64),
        original_audit_count INTEGER NOT NULL CHECK (original_audit_count >= 0),
        original_audit_head TEXT NOT NULL CHECK (length(original_audit_head) = 64),
        restored_audit_count INTEGER NOT NULL CHECK (restored_audit_count >= 0),
        restored_audit_head TEXT NOT NULL CHECK (length(restored_audit_head) = 64),
        restored_at TEXT NOT NULL,
        requires_fresh_snapshot INTEGER NOT NULL CHECK (requires_fresh_snapshot = 1),
        requires_reconciliation INTEGER NOT NULL CHECK (requires_reconciliation = 1),
        payload_json TEXT NOT NULL CHECK (json_valid(payload_json)),
        payload_sha256 TEXT NOT NULL UNIQUE CHECK (length(payload_sha256) = 64)
    ) STRICT
    """,
    """
    CREATE TABLE replay_acceptance_receipts (
        acceptance_identity_sha256 TEXT PRIMARY KEY CHECK (
            length(acceptance_identity_sha256) = 64
        ),
        deployment_identity_sha256 TEXT NOT NULL CHECK (length(deployment_identity_sha256) = 64),
        uquant_commit TEXT NOT NULL CHECK (length(uquant_commit) = 40),
        semantic_config_sha256 TEXT NOT NULL CHECK (length(semantic_config_sha256) = 64),
        policy_sha256 TEXT NOT NULL CHECK (length(policy_sha256) = 64),
        universe_sha256 TEXT NOT NULL CHECK (length(universe_sha256) = 64),
        frozen_data_manifest_sha256 TEXT NOT NULL CHECK (
            length(frozen_data_manifest_sha256) = 64
        ),
        normal_summary_sha256 TEXT NOT NULL CHECK (length(normal_summary_sha256) = 64),
        restart_summary_sha256 TEXT NOT NULL CHECK (length(restart_summary_sha256) = 64),
        passed INTEGER NOT NULL CHECK (passed IN (0,1)),
        payload_json TEXT NOT NULL CHECK (json_valid(payload_json)),
        payload_sha256 TEXT NOT NULL UNIQUE CHECK (length(payload_sha256) = 64),
        generated_at TEXT NOT NULL,
        UNIQUE (
            deployment_identity_sha256,uquant_commit,semantic_config_sha256,
            policy_sha256,universe_sha256,frozen_data_manifest_sha256
        )
    ) STRICT
    """,
    """
    INSERT INTO account_authority_epochs(
        epoch,account_id_hash,account_state_sha256,deployment_identity_sha256,
        source_binding_id,payload_json,payload_sha256,created_at
    )
    SELECT 1,account_id_hash,account_state_sha256,NULL,binding_id,
           payload_json,payload_sha256,created_at
    FROM account_bindings WHERE singleton_id = 1
    """,
    """
    INSERT INTO account_authority_active(singleton_id,epoch)
    SELECT 1,1 WHERE EXISTS(SELECT 1 FROM account_authority_epochs WHERE epoch = 1)
    """,
    """
    INSERT INTO mode_epochs(
        epoch,mode,deployment_identity_sha256,caps_sha256,payload_json,payload_sha256,created_at
    )
    SELECT 1,mode,NULL,NULL,
        CASE mode
            WHEN 'REPLAY' THEN '{"mode":"REPLAY","schema":"firmquant.mode-epoch.v1"}'
            WHEN 'PAPER' THEN '{"mode":"PAPER","schema":"firmquant.mode-epoch.v1"}'
            WHEN 'SHADOW' THEN '{"mode":"SHADOW","schema":"firmquant.mode-epoch.v1"}'
            WHEN 'CANARY' THEN '{"mode":"CANARY","schema":"firmquant.mode-epoch.v1"}'
            WHEN 'LIVE' THEN '{"mode":"LIVE","schema":"firmquant.mode-epoch.v1"}'
        END,
        CASE mode
            WHEN 'REPLAY' THEN '9791a290dd65bea78ea34730670dc5db24183a34a86aaf776b6db61fcf92d10d'
            WHEN 'PAPER' THEN '8ff81bc9f03b57d84babd9bcf797c74979099d2d7e8116d05362faebee7c6fdd'
            WHEN 'SHADOW' THEN '9932d157483b8741cf82707e1e3196f6c20014c5764cc272a886902a9c99c785'
            WHEN 'CANARY' THEN 'cfc5947d2acd397b4b3e896c5bd9128ef1deeb489c85b7a0f6d2e8d3ad1bc313'
            WHEN 'LIVE' THEN '300ae4f4ed06bc7be32236746e0aa1bbaf8518657ec9764ee823cfcfd73150a0'
        END,
        updated_at
    FROM runtime_state WHERE singleton_id = 1
    """,
    """
    INSERT INTO mode_epoch_active(singleton_id,epoch)
    SELECT 1,1 WHERE EXISTS(SELECT 1 FROM mode_epochs WHERE epoch = 1)
    """,
    """
    CREATE TRIGGER account_bindings_create_authority_epoch
    AFTER INSERT ON account_bindings
    BEGIN
        INSERT INTO account_authority_epochs(
            epoch,account_id_hash,account_state_sha256,deployment_identity_sha256,
            source_binding_id,payload_json,payload_sha256,created_at
        ) VALUES(
            1,NEW.account_id_hash,NEW.account_state_sha256,NULL,
            NEW.binding_id,NEW.payload_json,NEW.payload_sha256,NEW.created_at
        );
        INSERT INTO account_authority_active(singleton_id,epoch) VALUES(1,1);
    END
    """,
    """
    CREATE TRIGGER runtime_state_create_mode_epoch
    AFTER INSERT ON runtime_state
    BEGIN
        INSERT INTO mode_epochs(
            epoch,mode,deployment_identity_sha256,caps_sha256,
            payload_json,payload_sha256,created_at
        ) VALUES(
            1,NEW.mode,NULL,NULL,
            CASE NEW.mode
                WHEN 'REPLAY' THEN '{"mode":"REPLAY","schema":"firmquant.mode-epoch.v1"}'
                WHEN 'PAPER' THEN '{"mode":"PAPER","schema":"firmquant.mode-epoch.v1"}'
                WHEN 'SHADOW' THEN '{"mode":"SHADOW","schema":"firmquant.mode-epoch.v1"}'
                WHEN 'CANARY' THEN '{"mode":"CANARY","schema":"firmquant.mode-epoch.v1"}'
                WHEN 'LIVE' THEN '{"mode":"LIVE","schema":"firmquant.mode-epoch.v1"}'
            END,
            CASE NEW.mode
                WHEN 'REPLAY' THEN '9791a290dd65bea78ea34730670dc5db24183a34a86aaf776b6db61fcf92d10d'
                WHEN 'PAPER' THEN '8ff81bc9f03b57d84babd9bcf797c74979099d2d7e8116d05362faebee7c6fdd'
                WHEN 'SHADOW' THEN '9932d157483b8741cf82707e1e3196f6c20014c5764cc272a886902a9c99c785'
                WHEN 'CANARY' THEN 'cfc5947d2acd397b4b3e896c5bd9128ef1deeb489c85b7a0f6d2e8d3ad1bc313'
                WHEN 'LIVE' THEN '300ae4f4ed06bc7be32236746e0aa1bbaf8518657ec9764ee823cfcfd73150a0'
            END,
            NEW.updated_at
        );
        INSERT INTO mode_epoch_active(singleton_id,epoch) VALUES(1,1);
    END
    """,
)

_V5_IMMUTABLE_TABLES: Final = (
    "account_authority_epochs",
    "mode_epochs",
    "deployment_identities",
    "operational_evidence_receipts",
    "account_rebaseline_receipts",
    "mode_transition_receipts",
    "restore_receipts",
    "replay_acceptance_receipts",
)

_V5_IMMUTABLE_TRIGGERS: Final = tuple(
    statement
    for table in _V5_IMMUTABLE_TABLES
    for statement in (
        f"""
        CREATE TRIGGER {table}_reject_update
        BEFORE UPDATE ON {table}
        BEGIN
            SELECT RAISE(ABORT, '{table} is append-only');
        END
        """,
        f"""
        CREATE TRIGGER {table}_reject_delete
        BEFORE DELETE ON {table}
        BEGIN
            SELECT RAISE(ABORT, '{table} is append-only');
        END
        """,
    )
)

_V5_POINTER_TRIGGERS: Final = tuple(
    statement
    for table in ("account_authority_active", "mode_epoch_active")
    for statement in (
        f"""
        CREATE TRIGGER {table}_reject_initial_jump
        BEFORE INSERT ON {table} WHEN NEW.epoch != 1
        BEGIN
            SELECT RAISE(ABORT, '{table} must start at epoch one');
        END
        """,
        f"""
        CREATE TRIGGER {table}_reject_rollback
        BEFORE UPDATE ON {table} WHEN NEW.epoch <= OLD.epoch
        BEGIN
            SELECT RAISE(ABORT, '{table} rejects epoch rollback');
        END
        """,
        f"""
        CREATE TRIGGER {table}_reject_jump
        BEFORE UPDATE ON {table} WHEN NEW.epoch > OLD.epoch + 1
        BEGIN
            SELECT RAISE(ABORT, '{table} rejects epoch jump');
        END
        """,
        f"""
        CREATE TRIGGER {table}_reject_delete
        BEFORE DELETE ON {table}
        BEGIN
            SELECT RAISE(ABORT, '{table} rejects delete');
        END
        """,
    )
)

_V5_STAGED_OPERATION_TRIGGERS: Final = (
    """
    CREATE TRIGGER account_rebaseline_operations_forward_only
    BEFORE UPDATE ON account_rebaseline_operations
    BEGIN
        SELECT CASE WHEN
            NEW.operation_id IS NOT OLD.operation_id
            OR NEW.source_epoch IS NOT OLD.source_epoch
            OR NEW.target_epoch IS NOT OLD.target_epoch
            OR NEW.account_id_hash IS NOT OLD.account_id_hash
            OR NEW.account_before_sha256 IS NOT OLD.account_before_sha256
            OR NEW.candidate_account_state_sha256 IS NOT OLD.candidate_account_state_sha256
            OR NEW.deployment_identity_sha256 IS NOT OLD.deployment_identity_sha256
            OR NEW.broker_snapshot_id IS NOT OLD.broker_snapshot_id
            OR NEW.broker_snapshot_sha256 IS NOT OLD.broker_snapshot_sha256
            OR NEW.backup_id IS NOT OLD.backup_id
            OR NEW.reviewed_evidence_sha256 IS NOT OLD.reviewed_evidence_sha256
            OR NEW.account_path_sha256 IS NOT OLD.account_path_sha256
            OR (
                NEW.actual_account_after_sha256 IS NOT OLD.actual_account_after_sha256
                AND NOT (
                    OLD.stage='PREPARED'
                    AND NEW.stage='FILE_COMMITTED'
                    AND OLD.actual_account_after_sha256 IS NULL
                    AND NEW.actual_account_after_sha256 IS NOT NULL
                )
            )
            OR NEW.reason IS NOT OLD.reason
            OR NEW.payload_json IS NOT OLD.payload_json
            OR NEW.payload_sha256 IS NOT OLD.payload_sha256
            OR NEW.created_at IS NOT OLD.created_at
            THEN RAISE(ABORT, 'account_rebaseline_operations immutable payload') END;
        SELECT CASE WHEN NOT (
            (OLD.stage='PREPARED' AND NEW.stage IN ('FILE_COMMITTED','CONTRADICTION'))
            OR (OLD.stage='FILE_COMMITTED' AND NEW.stage IN ('RECEIPT_COMMITTED','CONTRADICTION'))
        ) THEN RAISE(ABORT, 'account_rebaseline_operations is forward-only') END;
    END
    """,
    """
    CREATE TRIGGER mode_transition_operations_forward_only
    BEFORE UPDATE ON mode_transition_operations
    BEGIN
        SELECT CASE WHEN
            NEW.operation_id IS NOT OLD.operation_id
            OR NEW.source_epoch IS NOT OLD.source_epoch
            OR NEW.target_epoch IS NOT OLD.target_epoch
            OR NEW.source_mode IS NOT OLD.source_mode
            OR NEW.target_mode IS NOT OLD.target_mode
            OR NEW.deployment_identity_sha256 IS NOT OLD.deployment_identity_sha256
            OR NEW.backup_id IS NOT OLD.backup_id
            OR NEW.evidence_sha256 IS NOT OLD.evidence_sha256
            OR NEW.payload_json IS NOT OLD.payload_json
            OR NEW.payload_sha256 IS NOT OLD.payload_sha256
            OR NEW.created_at IS NOT OLD.created_at
            THEN RAISE(ABORT, 'mode_transition_operations immutable payload') END;
        SELECT CASE WHEN NOT (
            (OLD.stage='PREPARED' AND NEW.stage IN ('EPOCH_COMMITTED','CONTRADICTION'))
            OR (OLD.stage='EPOCH_COMMITTED' AND NEW.stage IN ('RECEIPT_COMMITTED','CONTRADICTION'))
        ) THEN RAISE(ABORT, 'mode_transition_operations is forward-only') END;
    END
    """,
    """
    CREATE TRIGGER backup_publication_operations_forward_only
    BEFORE UPDATE ON backup_publication_operations
    BEGIN
        SELECT CASE WHEN
            NEW.operation_id IS NOT OLD.operation_id
            OR NEW.backup_id IS NOT OLD.backup_id
            OR NEW.reason IS NOT OLD.reason
            OR NEW.manifest_sha256 IS NOT OLD.manifest_sha256
            OR NEW.database_sha256 IS NOT OLD.database_sha256
            OR NEW.account_state_sha256 IS NOT OLD.account_state_sha256
            OR NEW.deployment_identity_sha256 IS NOT OLD.deployment_identity_sha256
            OR NEW.operational_evidence_identity_sha256 IS NOT OLD.operational_evidence_identity_sha256
            OR NEW.account_authority_epoch IS NOT OLD.account_authority_epoch
            OR NEW.mode_epoch IS NOT OLD.mode_epoch
            OR NEW.payload_json IS NOT OLD.payload_json
            OR NEW.payload_sha256 IS NOT OLD.payload_sha256
            OR NEW.created_at IS NOT OLD.created_at
            OR (OLD.stage != 'PREPARED' AND NEW.bundle_name IS NOT OLD.bundle_name)
            THEN RAISE(ABORT, 'backup_publication_operations immutable payload') END;
        SELECT CASE WHEN NOT (
            (OLD.stage='PREPARED' AND NEW.stage IN ('PUBLISHED','CONTRADICTION'))
            OR (OLD.stage='PUBLISHED' AND NEW.stage IN ('RECEIPT_COMMITTED','CONTRADICTION'))
        ) THEN RAISE(ABORT, 'backup_publication_operations is forward-only') END;
    END
    """,
    """
    CREATE TRIGGER restore_operations_forward_only
    BEFORE UPDATE ON restore_operations
    BEGIN
        SELECT CASE WHEN
            NEW.operation_id IS NOT OLD.operation_id
            OR NEW.restore_id IS NOT OLD.restore_id
            OR NEW.backup_id IS NOT OLD.backup_id
            OR NEW.source_manifest_sha256 IS NOT OLD.source_manifest_sha256
            OR NEW.source_database_sha256 IS NOT OLD.source_database_sha256
            OR NEW.destination_identity_sha256 IS NOT OLD.destination_identity_sha256
            OR NEW.deployment_identity_sha256 IS NOT OLD.deployment_identity_sha256
            OR NEW.operational_evidence_identity_sha256 IS NOT OLD.operational_evidence_identity_sha256
            OR NEW.account_authority_epoch IS NOT OLD.account_authority_epoch
            OR NEW.mode_epoch IS NOT OLD.mode_epoch
            OR NEW.payload_json IS NOT OLD.payload_json
            OR NEW.payload_sha256 IS NOT OLD.payload_sha256
            OR NEW.created_at IS NOT OLD.created_at
            OR (OLD.stage NOT IN ('PREPARED','STAGED')
                AND NEW.sanitized_state_sha256 IS NOT OLD.sanitized_state_sha256)
            OR (OLD.stage != 'STAGED' AND NEW.final_directory_name IS NOT OLD.final_directory_name)
            THEN RAISE(ABORT, 'restore_operations immutable payload') END;
        SELECT CASE WHEN NOT (
            (OLD.stage='PREPARED' AND NEW.stage IN ('STAGED','CONTRADICTION'))
            OR (OLD.stage='STAGED' AND NEW.stage IN ('PUBLISHED','CONTRADICTION'))
            OR (OLD.stage='PUBLISHED' AND NEW.stage IN ('RECEIPT_COMMITTED','CONTRADICTION'))
        ) THEN RAISE(ABORT, 'restore_operations is forward-only') END;
    END
    """,
)

_V5_STAGED_DELETE_TRIGGERS: Final = tuple(
    f"""
    CREATE TRIGGER {table}_reject_delete
    BEFORE DELETE ON {table}
    BEGIN
        SELECT RAISE(ABORT, '{table} rejects delete');
    END
    """
    for table in (
        "account_rebaseline_operations",
        "mode_transition_operations",
        "backup_publication_operations",
        "restore_operations",
    )
)

MIGRATIONS: Final = (
    Migration.create(
        version=1,
        name="operational_ledger",
        statements=(*_CORE_SCHEMA, *_APPEND_ONLY_TRIGGERS),
    ),
    Migration.create(
        version=2,
        name="immutable_reconciliation_receipts",
        statements=(
            """
            CREATE TRIGGER reconciliation_runs_reject_update
            BEFORE UPDATE ON reconciliation_runs
            BEGIN
                SELECT RAISE(ABORT, 'reconciliation_runs is append-only');
            END
            """,
            """
            CREATE TRIGGER reconciliation_runs_reject_delete
            BEFORE DELETE ON reconciliation_runs
            BEGIN
                SELECT RAISE(ABORT, 'reconciliation_runs is append-only');
            END
            """,
        ),
    ),
    Migration.create(
        version=3,
        name="account_authority",
        statements=_ACCOUNT_AUTHORITY_SCHEMA,
    ),
    Migration.create(
        version=4,
        name="production_heartbeat",
        statements=(
            """
            CREATE TABLE production_heartbeat (
                singleton_id INTEGER PRIMARY KEY CHECK (singleton_id = 1),
                mode TEXT NOT NULL CHECK (mode IN ('SHADOW','CANARY','LIVE')),
                runtime_state TEXT NOT NULL CHECK (runtime_state IN (
                    'DISARMED','STARTING','RECONCILING','READY','EXECUTING','DEGRADED','HALTED','STOPPING'
                )),
                observed_at TEXT NOT NULL,
                host_hash TEXT NOT NULL CHECK (length(host_hash) = 64),
                process_id INTEGER NOT NULL CHECK (process_id > 0),
                writer_generation INTEGER NOT NULL CHECK (writer_generation > 0),
                broker_connected INTEGER NOT NULL CHECK (broker_connected IN (0, 1)),
                broker_read_healthy INTEGER NOT NULL CHECK (broker_read_healthy IN (0, 1)),
                broker_write_healthy INTEGER NOT NULL CHECK (broker_write_healthy IN (0, 1)),
                pending_events INTEGER NOT NULL CHECK (pending_events >= 0),
                last_broker_event TEXT,
                last_quote TEXT,
                last_reconciliation TEXT,
                last_decision TEXT,
                last_execution TEXT,
                control_request_state TEXT NOT NULL,
                processed_events INTEGER NOT NULL CHECK (processed_events >= 0),
                decisions INTEGER NOT NULL CHECK (decisions >= 0),
                executions INTEGER NOT NULL CHECK (executions >= 0),
                eod INTEGER NOT NULL CHECK (eod >= 0)
            ) STRICT
            """,
        ),
    ),
    Migration.create(
        version=5,
        name="operational_authority_epochs",
        statements=(
            *_OPERATIONAL_AUTHORITY_SCHEMA,
            *_V5_IMMUTABLE_TRIGGERS,
            *_V5_POINTER_TRIGGERS,
            *_V5_STAGED_OPERATION_TRIGGERS,
            *_V5_STAGED_DELETE_TRIGGERS,
        ),
    ),
)
CURRENT_SCHEMA_VERSION: Final = MIGRATIONS[-1].version


def _validate_plan(migrations: tuple[Migration, ...]) -> None:
    if not migrations:
        raise MigrationError("migration plan is empty")
    versions = tuple(migration.version for migration in migrations)
    if versions != tuple(range(1, len(migrations) + 1)):
        raise MigrationError("migration versions must be contiguous from one")
    if len({migration.name for migration in migrations}) != len(migrations):
        raise MigrationError("migration names must be unique")


def apply_migrations(
    database: Database,
    *,
    migrations: tuple[Migration, ...] = MIGRATIONS,
) -> None:
    """Verify existing receipts and atomically apply every missing migration."""

    _validate_plan(migrations)
    active_name = "schema_migrations bootstrap"
    try:
        with database.transaction("EXCLUSIVE"):
            database.write(_CREATE_MIGRATIONS_TABLE)
            rows = database.query_all(
                "SELECT version, name, checksum FROM schema_migrations ORDER BY version"
            )
            expected = {migration.version: migration for migration in migrations}
            for row in rows:
                version = int(row["version"])
                migration = expected.get(version)
                if migration is None:
                    raise MigrationError(f"database schema version {version} is newer than this executable")
                if row["name"] != migration.name or row["checksum"] != migration.checksum:
                    raise MigrationError(f"migration receipt mismatch at version {version}")
            applied = {int(row["version"]) for row in rows}
            for migration in migrations:
                if migration.version in applied:
                    continue
                active_name = migration.name
                for statement in migration.statements:
                    database.write(statement)
                database.write(
                    """
                    INSERT INTO schema_migrations(version, name, checksum, applied_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        migration.version,
                        migration.name,
                        migration.checksum,
                        datetime.now(UTC).isoformat(),
                    ),
                )
    except MigrationError:
        raise
    except sqlite3.DatabaseError as exc:
        raise MigrationError(f"migration {active_name} failed") from exc


__all__ = (
    "CURRENT_SCHEMA_VERSION",
    "MIGRATIONS",
    "Migration",
    "MigrationError",
    "apply_migrations",
)
