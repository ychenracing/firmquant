from __future__ import annotations

from pathlib import Path


def replace_once(path: str, old: str, new: str) -> None:
    target = Path(path)
    text = target.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"{path}: expected anchor once, found {count}: {old[:80]!r}")
    target.write_text(text.replace(old, new), encoding="utf-8")


# Reconciliation evaluation is pure; commit can join an existing SQLite transaction.
path = "src/firmquant/reconciliation/service.py"
replace_once(
    path,
    "import hashlib\nfrom collections.abc import Callable\nfrom datetime import datetime\n",
    "import hashlib\nfrom collections.abc import Callable\nfrom contextlib import nullcontext\nfrom datetime import datetime\n",
)
replace_once(
    path,
    "    def run(\n        self,\n        kind: ReconciliationKind,\n        facts: ReconciliationFacts,\n    ) -> ReconciliationReceipt:\n",
    "    def evaluate(\n        self,\n        kind: ReconciliationKind,\n        facts: ReconciliationFacts,\n    ) -> ReconciliationReceipt:\n",
)
replace_once(
    path,
    "        self._append(receipt, broker_snapshot_sha256=facts.broker_snapshot.raw_payload_sha256)\n        return receipt\n\n    def _compare_identity(\n",
    "        return receipt\n\n    def commit(\n        self,\n        receipt: ReconciliationReceipt,\n        *,\n        broker_snapshot_sha256: str,\n    ) -> None:\n        if not isinstance(receipt, ReconciliationReceipt):\n            raise DomainTypeError(\"reconciliation commit requires ReconciliationReceipt\")\n        if (\n            not isinstance(broker_snapshot_sha256, str)\n            or len(broker_snapshot_sha256) != 64\n            or any(character not in \"0123456789abcdef\" for character in broker_snapshot_sha256)\n        ):\n            raise DomainValidationError(\"reconciliation broker snapshot hash must be SHA-256\")\n        self._append(receipt, broker_snapshot_sha256=broker_snapshot_sha256)\n\n    def run(\n        self,\n        kind: ReconciliationKind,\n        facts: ReconciliationFacts,\n    ) -> ReconciliationReceipt:\n        receipt = self.evaluate(kind, facts)\n        self.commit(\n            receipt,\n            broker_snapshot_sha256=facts.broker_snapshot.raw_payload_sha256,\n        )\n        return receipt\n\n    def _compare_identity(\n",
)
replace_once(
    path,
    "        with self._database.transaction():\n            existing = self._database.query_one(\n",
    "        transaction = nullcontext() if self._database.in_transaction else self._database.transaction()\n        with transaction:\n            existing = self._database.query_one(\n",
)

# Account receipt transaction owns reconciliation finalization.
path = "src/firmquant/persistence/recovery.py"
replace_once(
    path,
    "    def commit_receipt(self, *, now: datetime) -> None:\n        _aware(now, label=\"account receipt commit time\")\n",
    "    def commit_receipt(\n        self,\n        *,\n        now: datetime,\n        finalize: Callable[[], None] | None = None,\n    ) -> None:\n        _aware(now, label=\"account receipt commit time\")\n        if finalize is not None and not callable(finalize):\n            raise DomainTypeError(\"account receipt finalizer must be callable or None\")\n",
)
replace_once(
    path,
    "                created_at=now,\n            )\n\n\nclass RecoveryService:\n",
    "                created_at=now,\n            )\n            if finalize is not None:\n                finalize()\n\n\nclass RecoveryService:\n",
)
old_recovery = '''            target_stage = (\n                "CONTRADICTION"\n                if classification is AccountRecoveryClassification.CONTRADICTION\n                else "RECEIPT_COMMITTED"\n            )\n            with self._database.transaction():\n                self._database.write(\n                    "UPDATE account_operations SET stage = ?, "\n                    "actual_account_after_sha256 = ?, updated_at = ? "\n                    "WHERE operation_id = ?",\n                    (target_stage, actual, now.isoformat(), operation_id),\n                )\n                self._append_account_recovery_audit(\n                    operation_id=operation_id,\n                    classification=classification,\n                    actual=actual,\n                    now=now,\n                )\n            receipts.append(\n                AccountRecoveryReceipt(\n                    operation_id=operation_id,\n                    classification=classification,\n                    actual_account_sha256=actual,\n                )\n            )\n            if classification is AccountRecoveryClassification.CONTRADICTION:\n                blockers.add("ACCOUNT_OPERATION_CONTRADICTION")\n'''
new_recovery = '''            operation_kind = str(row["operation_kind"])\n            original_stage = str(row["stage"])\n            persisted_actual = actual\n            if classification is AccountRecoveryClassification.CONTRADICTION:\n                target_stage = "CONTRADICTION"\n            elif operation_kind == "BROKER_SYNC":\n                if (\n                    classification is AccountRecoveryClassification.NOT_APPLIED\n                    and original_stage == "PREPARED"\n                ):\n                    target_stage = "PREPARED"\n                    persisted_actual = None\n                    blockers.add("ACCOUNT_COMMIT_RETRY_REQUIRED")\n                elif (\n                    classification is AccountRecoveryClassification.FILE_APPLIED_RECEIPT_MISSING\n                    and original_stage in {"PREPARED", "FILE_COMMITTED"}\n                ):\n                    target_stage = "FILE_COMMITTED"\n                    blockers.add("ACCOUNT_FINALIZATION_REQUIRED")\n                else:\n                    target_stage = "CONTRADICTION"\n                    classification = AccountRecoveryClassification.CONTRADICTION\n            else:\n                target_stage = "RECEIPT_COMMITTED"\n            with self._database.transaction():\n                self._database.write(\n                    "UPDATE account_operations SET stage = ?, "\n                    "actual_account_after_sha256 = ?, updated_at = ? "\n                    "WHERE operation_id = ?",\n                    (target_stage, persisted_actual, now.isoformat(), operation_id),\n                )\n                self._append_account_recovery_audit(\n                    operation_id=operation_id,\n                    classification=classification,\n                    actual=actual,\n                    now=now,\n                )\n            receipts.append(\n                AccountRecoveryReceipt(\n                    operation_id=operation_id,\n                    classification=classification,\n                    actual_account_sha256=actual,\n                )\n            )\n            if classification is AccountRecoveryClassification.CONTRADICTION:\n                blockers.add("ACCOUNT_OPERATION_CONTRADICTION")\n'''
replace_once(path, old_recovery, new_recovery)

# Runtime broker commit accepts a finalizer and supplies it to the account receipt transaction.
path = "src/firmquant/strategy/runtime_account.py"
replace_once(
    path,
    "    def commit_broker_snapshot(self, prepared: PreparedAccountSync) -> str:\n        \"\"\"CAS-commit one reviewed preparation, resuming the same durable identity idempotently.\"\"\"\n\n        if not isinstance(prepared, PreparedAccountSync):\n            raise TypeError(\"broker account commit requires PreparedAccountSync\")\n",
    "    def commit_broker_snapshot(\n        self,\n        prepared: PreparedAccountSync,\n        *,\n        finalize: Callable[[], None] | None = None,\n    ) -> str:\n        \"\"\"CAS-commit one reviewed preparation and its SQLite finalization atomically.\"\"\"\n\n        if not isinstance(prepared, PreparedAccountSync):\n            raise TypeError(\"broker account commit requires PreparedAccountSync\")\n        if finalize is not None and not callable(finalize):\n            raise TypeError(\"broker account finalizer must be callable or None\")\n",
)
replace_once(
    path,
    "        operation.commit_file(now=now)\n        operation.commit_receipt(now=now)\n        if self._store.hash_file(self._path) != prepared.account_after_sha256:\n",
    "        operation.commit_file(now=now)\n        operation.commit_receipt(now=now, finalize=finalize)\n        if self._store.hash_file(self._path) != prepared.account_after_sha256:\n",
)

# Coordinator evaluates first; only a passed result is committed, inside account finalization for economic changes.
path = "src/firmquant/reconciliation/account_coordinator.py"
replace_once(
    path,
    "    def commit_broker_snapshot(self, prepared: PreparedAccountSync) -> str: ...\n\n\nclass _Reconciler(Protocol):\n    def run(\n        self,\n        kind: ReconciliationKind,\n        facts: ReconciliationFacts,\n    ) -> ReconciliationReceipt: ...\n",
    "    def commit_broker_snapshot(\n        self,\n        prepared: PreparedAccountSync,\n        *,\n        finalize: Callable[[], None] | None = None,\n    ) -> str: ...\n\n\nclass _Reconciler(Protocol):\n    def evaluate(\n        self,\n        kind: ReconciliationKind,\n        facts: ReconciliationFacts,\n    ) -> ReconciliationReceipt: ...\n\n    def commit(\n        self,\n        receipt: ReconciliationReceipt,\n        *,\n        broker_snapshot_sha256: str,\n    ) -> None: ...\n",
)
replace_once(
    path,
    "        receipt = self._reconciler.run(kind, facts)\n        if not receipt.passed:\n            raise AccountReconciliationBlocked(tuple(receipt.blockers))\n\n        if prepared.account_after_sha256 == prepared.account_before_sha256:\n            return AccountReconciliationResult(\n",
    "        receipt = self._reconciler.evaluate(kind, facts)\n        if not receipt.passed:\n            raise AccountReconciliationBlocked(tuple(receipt.blockers))\n\n        if prepared.account_after_sha256 == prepared.account_before_sha256:\n            self._reconciler.commit(\n                receipt,\n                broker_snapshot_sha256=snapshot.raw_payload_sha256,\n            )\n            return AccountReconciliationResult(\n",
)
replace_once(
    path,
    "        committed = self._accounts.commit_broker_snapshot(prepared)\n        if committed != prepared.account_after_sha256:\n",
    "        committed = self._accounts.commit_broker_snapshot(\n            prepared,\n            finalize=lambda: self._reconciler.commit(\n                receipt,\n                broker_snapshot_sha256=snapshot.raw_payload_sha256,\n            ),\n        )\n        if committed != prepared.account_after_sha256:\n",
)

# Central migration owns all authority tables and append-only triggers.
path = "src/firmquant/persistence/schema.py"
account_authority_schema = '''\n_ACCOUNT_AUTHORITY_SCHEMA: Final = (\n    """\n    CREATE TABLE account_bindings (\n        binding_id TEXT PRIMARY KEY,\n        singleton_id INTEGER NOT NULL UNIQUE CHECK (singleton_id = 1),\n        account_id_hash TEXT NOT NULL CHECK (length(account_id_hash) = 64),\n        account_type TEXT NOT NULL CHECK (account_type = 'CASH'),\n        broker_snapshot_sha256 TEXT NOT NULL CHECK (length(broker_snapshot_sha256) = 64),\n        account_state_sha256 TEXT NOT NULL CHECK (length(account_state_sha256) = 64),\n        uquant_commit TEXT NOT NULL CHECK (length(uquant_commit) = 40),\n        uquant_code_fingerprint TEXT NOT NULL CHECK (length(uquant_code_fingerprint) = 64),\n        data_hash TEXT NOT NULL CHECK (length(data_hash) = 64),\n        data_as_of TEXT NOT NULL,\n        data_symbols_json TEXT NOT NULL CHECK (json_valid(data_symbols_json)),\n        payload_json TEXT NOT NULL CHECK (json_valid(payload_json)),\n        payload_sha256 TEXT NOT NULL CHECK (length(payload_sha256) = 64),\n        created_at TEXT NOT NULL\n    ) STRICT\n    """,\n    """\n    CREATE TABLE account_bootstrap_operations (\n        operation_id TEXT PRIMARY KEY,\n        stage TEXT NOT NULL CHECK (stage IN (\n            'PREPARED','FILE_COMMITTED','BINDING_COMMITTED','CONTRADICTION'\n        )),\n        account_state_sha256 TEXT NOT NULL CHECK (length(account_state_sha256) = 64),\n        broker_snapshot_sha256 TEXT NOT NULL CHECK (length(broker_snapshot_sha256) = 64),\n        binding_payload_json TEXT NOT NULL CHECK (json_valid(binding_payload_json)),\n        binding_payload_sha256 TEXT NOT NULL CHECK (length(binding_payload_sha256) = 64),\n        created_at TEXT NOT NULL,\n        updated_at TEXT NOT NULL\n    ) STRICT\n    """,\n    """\n    CREATE TABLE reviewed_account_adjustments (\n        adjustment_id TEXT PRIMARY KEY,\n        account_id_hash TEXT NOT NULL CHECK (length(account_id_hash) = 64),\n        symbol TEXT NOT NULL,\n        session_date TEXT NOT NULL,\n        adjustment_type TEXT NOT NULL,\n        coverage_kind TEXT NOT NULL,\n        broker_snapshot_sha256 TEXT NOT NULL CHECK (length(broker_snapshot_sha256) = 64),\n        difference_sha256 TEXT NOT NULL CHECK (length(difference_sha256) = 64),\n        audit_summary_sha256 TEXT NOT NULL CHECK (length(audit_summary_sha256) = 64),\n        payload_json TEXT NOT NULL CHECK (json_valid(payload_json)),\n        payload_sha256 TEXT NOT NULL CHECK (length(payload_sha256) = 64),\n        created_at TEXT NOT NULL\n    ) STRICT\n    """,\n    """\n    CREATE INDEX reviewed_account_adjustments_lookup_idx\n    ON reviewed_account_adjustments(\n        account_id_hash, symbol, session_date, coverage_kind,\n        broker_snapshot_sha256, difference_sha256\n    )\n    """,\n    """\n    CREATE TRIGGER account_bindings_reject_update\n    BEFORE UPDATE ON account_bindings\n    BEGIN\n        SELECT RAISE(ABORT, 'account_bindings is append-only');\n    END\n    """,\n    """\n    CREATE TRIGGER account_bindings_reject_delete\n    BEFORE DELETE ON account_bindings\n    BEGIN\n        SELECT RAISE(ABORT, 'account_bindings is append-only');\n    END\n    """,\n    """\n    CREATE TRIGGER reviewed_account_adjustments_reject_update\n    BEFORE UPDATE ON reviewed_account_adjustments\n    BEGIN\n        SELECT RAISE(ABORT, 'reviewed_account_adjustments is append-only');\n    END\n    """,\n    """\n    CREATE TRIGGER reviewed_account_adjustments_reject_delete\n    BEFORE DELETE ON reviewed_account_adjustments\n    BEGIN\n        SELECT RAISE(ABORT, 'reviewed_account_adjustments is append-only');\n    END\n    """,\n)\n\n'''
replace_once(path, "\nMIGRATIONS: Final = (\n", account_authority_schema + "MIGRATIONS: Final = (\n")
replace_once(
    path,
    "    Migration.create(\n        version=2,\n        name=\"immutable_reconciliation_receipts\",\n        statements=(\n            \"\"\"\n            CREATE TRIGGER reconciliation_runs_reject_update\n            BEFORE UPDATE ON reconciliation_runs\n            BEGIN\n                SELECT RAISE(ABORT, 'reconciliation_runs is append-only');\n            END\n            \"\"\",\n            \"\"\"\n            CREATE TRIGGER reconciliation_runs_reject_delete\n            BEFORE DELETE ON reconciliation_runs\n            BEGIN\n                SELECT RAISE(ABORT, 'reconciliation_runs is append-only');\n            END\n            \"\"\",\n        ),\n    ),\n)\n",
    "    Migration.create(\n        version=2,\n        name=\"immutable_reconciliation_receipts\",\n        statements=(\n            \"\"\"\n            CREATE TRIGGER reconciliation_runs_reject_update\n            BEFORE UPDATE ON reconciliation_runs\n            BEGIN\n                SELECT RAISE(ABORT, 'reconciliation_runs is append-only');\n            END\n            \"\"\",\n            \"\"\"\n            CREATE TRIGGER reconciliation_runs_reject_delete\n            BEFORE DELETE ON reconciliation_runs\n            BEGIN\n                SELECT RAISE(ABORT, 'reconciliation_runs is append-only');\n            END\n            \"\"\",\n        ),\n    ),\n    Migration.create(\n        version=3,\n        name=\"account_authority\",\n        statements=_ACCOUNT_AUTHORITY_SCHEMA,\n    ),\n)\n",
)

# Repository schema helper delegates to the checksummed migration plan.
path = "src/firmquant/persistence/account_authority.py"
text = Path(path).read_text(encoding="utf-8")
start = text.index("def ensure_account_authority_schema(database: Database) -> None:\n")
end = text.index("\n\n@dataclass(frozen=True, slots=True)\nclass AccountBinding:", start)
replacement = '''def ensure_account_authority_schema(database: Database) -> None:\n    """Verify the centrally checksummed authority schema is applied."""\n\n    if not isinstance(database, Database):\n        raise TypeError("account authority schema requires Database")\n    from .schema import apply_migrations\n\n    apply_migrations(database)'''
Path(path).write_text(text[:start] + replacement + text[end:], encoding="utf-8")

# Existing schema-count assertions follow the additive migration.
path = "tests/unit/persistence/test_database.py"
replace_once(path, 'assert reader.scalar("SELECT count(*) FROM schema_migrations") == 2', 'assert reader.scalar("SELECT count(*) FROM schema_migrations") == 3')
replace_once(path, 'assert restored.scalar("SELECT max(version) FROM schema_migrations") == 2', 'assert restored.scalar("SELECT max(version) FROM schema_migrations") == 3')

# Coordinator test reconciler now models evaluate/commit separately.
path = "tests/unit/reconciliation/test_account_coordinator.py"
replace_once(
    path,
    "    def run(self, kind, facts):\n        self.calls.append((kind, facts))\n        if self.before_return is not None:\n            self.before_return()\n        return SimpleNamespace(\n            reconciliation_id=\"recon_\" + \"a\" * 64,\n            kind=kind,\n            passed=self.passed,\n            blockers=self.blockers,\n        )\n",
    "    def evaluate(self, kind, facts):\n        self.calls.append((kind, facts))\n        if self.before_return is not None:\n            self.before_return()\n        return SimpleNamespace(\n            reconciliation_id=\"recon_\" + \"a\" * 64,\n            kind=kind,\n            passed=self.passed,\n            blockers=self.blockers,\n        )\n\n    def commit(self, _receipt, *, broker_snapshot_sha256):\n        assert len(broker_snapshot_sha256) == 64\n",
)

# Coverage-only fakes implement the same new protocol without changing test intent.
path = "tests/unit/reconciliation/test_account_authority_additional_edges.py"
replace_once(
    path,
    "    def commit_broker_snapshot(self, _prepared):\n        return self.commit_result\n",
    "    def commit_broker_snapshot(self, _prepared, *, finalize=None):\n        if finalize is not None:\n            finalize()\n        return self.commit_result\n",
)
replace_once(
    path,
    "class _PassingReconciler:\n    def run(self, _kind, _facts):\n        return SimpleNamespace(passed=True, blockers=())\n",
    "class _PassingReconciler:\n    def evaluate(self, _kind, _facts):\n        return SimpleNamespace(passed=True, blockers=())\n\n    def commit(self, _receipt, *, broker_snapshot_sha256):\n        assert len(broker_snapshot_sha256) == 64\n",
)
