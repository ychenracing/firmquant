from __future__ import annotations

from pathlib import Path


def replace_once(path: str, old: str, new: str) -> None:
    target = Path(path)
    text = target.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"{path}: expected anchor once, found {count}: {old[:120]!r}")
    target.write_text(text.replace(old, new), encoding="utf-8")


# Reconciliation receipt canonical recovery evidence.
path = "src/firmquant/reconciliation/service.py"
replace_once(
    path,
    "from collections.abc import Callable\nfrom contextlib import nullcontext\nfrom datetime import datetime\n",
    "from collections.abc import Callable, Mapping\nfrom contextlib import nullcontext\nfrom datetime import datetime\nfrom decimal import Decimal\n",
)
anchor = '''def _evidence_hash(*values: object) -> str:\n    return hashlib.sha256(canonical_json(values).encode("utf-8")).hexdigest()\n\n\nclass ReconciliationService:\n'''
insert = '''def _evidence_hash(*values: object) -> str:\n    return hashlib.sha256(canonical_json(values).encode("utf-8")).hexdigest()\n\n\ndef _finalization_sha256(value: str, *, label: str) -> None:\n    if (\n        not isinstance(value, str)\n        or len(value) != 64\n        or any(character not in "0123456789abcdef" for character in value)\n    ):\n        raise DomainValidationError(f"{label} must be lowercase SHA-256")\n\n\ndef _receipt_identity(receipt: ReconciliationReceipt) -> str:\n    return "recon_" + hashlib.sha256(\n        canonical_json(\n            {\n                "kind": receipt.kind,\n                "details_sha256": receipt.details_sha256,\n                "started_at": receipt.started_at,\n                "completed_at": receipt.completed_at,\n            }\n        ).encode("utf-8")\n    ).hexdigest()\n\n\ndef _validate_receipt_integrity(receipt: ReconciliationReceipt) -> None:\n    details_sha256 = hashlib.sha256(receipt.details_json.encode("utf-8")).hexdigest()\n    if details_sha256 != receipt.details_sha256:\n        raise DomainValidationError("reconciliation finalization details hash mismatch")\n    if _receipt_identity(receipt) != receipt.reconciliation_id:\n        raise DomainValidationError("reconciliation finalization identity mismatch")\n\n\ndef reconciliation_finalization_payload(\n    receipt: ReconciliationReceipt,\n    *,\n    broker_snapshot_sha256: str,\n) -> dict[str, object]:\n    """Serialize one passed/blocked receipt as canonical crash-recovery evidence."""\n\n    if not isinstance(receipt, ReconciliationReceipt):\n        raise DomainTypeError("reconciliation finalization requires ReconciliationReceipt")\n    _finalization_sha256(\n        broker_snapshot_sha256,\n        label="reconciliation finalization broker snapshot hash",\n    )\n    _validate_receipt_integrity(receipt)\n    return {\n        "schema": "firmquant.reconciliation-finalization.v1",\n        "broker_snapshot_sha256": broker_snapshot_sha256,\n        "receipt": {\n            "reconciliation_id": receipt.reconciliation_id,\n            "kind": receipt.kind.value,\n            "snapshot_id": receipt.snapshot_id,\n            "started_at": receipt.started_at.isoformat(),\n            "completed_at": receipt.completed_at.isoformat(),\n            "passed": receipt.passed,\n            "blockers": list(receipt.blockers),\n            "operator_actions": list(receipt.operator_actions),\n            "details_json": receipt.details_json,\n            "details_sha256": receipt.details_sha256,\n        },\n    }\n\n\ndef _decode_reconciliation_finalization(\n    payload: object,\n) -> tuple[ReconciliationReceipt, str]:\n    if not isinstance(payload, Mapping):\n        raise DomainTypeError("reconciliation finalization payload must be a mapping")\n    if set(payload) != {"schema", "broker_snapshot_sha256", "receipt"}:\n        raise DomainValidationError("reconciliation finalization payload schema is unexpected")\n    if payload["schema"] != "firmquant.reconciliation-finalization.v1":\n        raise DomainValidationError("reconciliation finalization schema is unsupported")\n    broker_snapshot_sha256 = payload["broker_snapshot_sha256"]\n    if not isinstance(broker_snapshot_sha256, str):\n        raise DomainTypeError("reconciliation finalization broker hash must be text")\n    _finalization_sha256(\n        broker_snapshot_sha256,\n        label="reconciliation finalization broker snapshot hash",\n    )\n    raw = payload["receipt"]\n    if not isinstance(raw, Mapping):\n        raise DomainTypeError("reconciliation finalization receipt must be a mapping")\n    expected_keys = {\n        "reconciliation_id",\n        "kind",\n        "snapshot_id",\n        "started_at",\n        "completed_at",\n        "passed",\n        "blockers",\n        "operator_actions",\n        "details_json",\n        "details_sha256",\n    }\n    if set(raw) != expected_keys:\n        raise DomainValidationError("reconciliation finalization receipt schema is unexpected")\n    blockers = raw["blockers"]\n    operator_actions = raw["operator_actions"]\n    if not isinstance(blockers, list) or not isinstance(operator_actions, list):\n        raise DomainTypeError("reconciliation finalization collections must be lists")\n    try:\n        kind = ReconciliationKind(str(raw["kind"]))\n        started_at = datetime.fromisoformat(str(raw["started_at"]))\n        completed_at = datetime.fromisoformat(str(raw["completed_at"]))\n    except (TypeError, ValueError) as error:\n        raise DomainValidationError("reconciliation finalization typed fields are invalid") from error\n    receipt = ReconciliationReceipt(\n        reconciliation_id=str(raw["reconciliation_id"]),\n        kind=kind,\n        snapshot_id=str(raw["snapshot_id"]),\n        started_at=started_at,\n        completed_at=completed_at,\n        passed=raw["passed"],\n        blockers=tuple(blockers),\n        operator_actions=tuple(operator_actions),\n        details_json=str(raw["details_json"]),\n        details_sha256=str(raw["details_sha256"]),\n    )\n    _validate_receipt_integrity(receipt)\n    return receipt, broker_snapshot_sha256\n\n\ndef commit_reconciliation_finalization(database: Database, payload: object) -> ReconciliationReceipt:\n    """Validate durable evidence and idempotently append its reconciliation receipt."""\n\n    if not isinstance(database, Database):\n        raise DomainTypeError("reconciliation finalization database must be Database")\n    receipt, broker_snapshot_sha256 = _decode_reconciliation_finalization(payload)\n    service = ReconciliationService(\n        database=database,\n        cash_tolerance=Money(Decimal("0")),\n        clock=lambda: receipt.completed_at,\n    )\n    service.commit(receipt, broker_snapshot_sha256=broker_snapshot_sha256)\n    return receipt\n\n\nclass ReconciliationService:\n'''
replace_once(path, anchor, insert)
replace_once(
    path,
    '__all__ = ("ReconciliationService",)\n',
    '__all__ = (\n    "ReconciliationService",\n    "commit_reconciliation_finalization",\n    "reconciliation_finalization_payload",\n)\n',
)

# Account operation accepts optional canonical durable finalization evidence.
path = "src/firmquant/persistence/recovery.py"
replace_once(path, "from collections.abc import Callable\n", "from collections.abc import Callable, Mapping\n")
replace_once(
    path,
    "        evidence_sha256: str,\n        now: datetime,\n        operation_id: str | None = None,\n",
    "        evidence_sha256: str,\n        now: datetime,\n        operation_id: str | None = None,\n        finalization_payload: Mapping[str, object] | None = None,\n",
)
replace_once(
    path,
    '''        payload = {\n            "schema": "firmquant.account-operation.v1",\n            "operation_kind": operation_kind,\n            "account_path_sha256": path_digest,\n            "evidence_sha256": evidence_sha256,\n        }\n        payload_json = canonical_json(payload)\n''',
    '''        payload: dict[str, object] = {\n            "schema": "firmquant.account-operation.v1",\n            "operation_kind": operation_kind,\n            "account_path_sha256": path_digest,\n            "evidence_sha256": evidence_sha256,\n        }\n        if finalization_payload is not None:\n            if not isinstance(finalization_payload, Mapping):\n                raise DomainTypeError("account operation finalization payload must be a mapping")\n            canonical_finalization = canonical_json(finalization_payload)\n            decoded_finalization = json.loads(canonical_finalization)\n            if not isinstance(decoded_finalization, dict):\n                raise DomainValidationError("account operation finalization payload must be an object")\n            payload["finalization"] = decoded_finalization\n        payload_json = canonical_json(payload)\n''',
)
# Recovery helper methods inserted before _recover_accounts.
anchor = '''    def _recover_accounts(self, now: datetime) -> tuple[tuple[AccountRecoveryReceipt, ...], tuple[str, ...]]:\n'''
insert = '''    @staticmethod\n    def _operation_payload(payload_json: str) -> dict[str, object] | None:\n        try:\n            payload: object = json.loads(payload_json)\n        except (TypeError, ValueError, json.JSONDecodeError):\n            return None\n        return payload if isinstance(payload, dict) else None\n\n    def _recover_broker_sync_finalization(\n        self,\n        *,\n        row: sqlite3.Row,\n        operation_id: str,\n        actual: str,\n        now: datetime,\n    ) -> bool:\n        payload = self._operation_payload(str(row["payload_json"]))\n        if payload is None or "finalization" not in payload:\n            return False\n        finalization = payload["finalization"]\n        evidence_sha256 = payload.get("evidence_sha256")\n        if not isinstance(evidence_sha256, str):\n            return False\n        try:\n            from firmquant.reconciliation.service import commit_reconciliation_finalization\n\n            with self._database.transaction():\n                self._database.write(\n                    "UPDATE account_operations SET stage = 'RECEIPT_COMMITTED', "\n                    "actual_account_after_sha256 = ?, updated_at = ? "\n                    "WHERE operation_id = ? AND stage IN ('PREPARED','FILE_COMMITTED')",\n                    (actual, now.isoformat(), operation_id),\n                )\n                AuditLedger(self._database).append(\n                    audit_event_id="account-operation."\n                    + hashlib.sha256(operation_id.encode("utf-8")).hexdigest(),\n                    category="account.operation.committed",\n                    actor="firmquant",\n                    payload={\n                        "operation_id": operation_id,\n                        "operation_kind": "BROKER_SYNC",\n                        "account_path_sha256": payload.get("account_path_sha256"),\n                        "evidence_sha256": evidence_sha256,\n                        "account_before_sha256": str(row["account_before_sha256"]),\n                        "account_after_sha256": str(row["expected_account_after_sha256"]),\n                    },\n                    created_at=now,\n                )\n                commit_reconciliation_finalization(self._database, finalization)\n                self._append_account_recovery_audit(\n                    operation_id=operation_id,\n                    classification=AccountRecoveryClassification.FILE_APPLIED_RECEIPT_MISSING,\n                    actual=actual,\n                    now=now,\n                )\n            return True\n        except (\n            DomainTypeError,\n            DomainValidationError,\n            PersistenceConflict,\n            sqlite3.DatabaseError,\n            ValueError,\n        ):\n            return False\n\n    def _recover_accounts(self, now: datetime) -> tuple[tuple[AccountRecoveryReceipt, ...], tuple[str, ...]]:\n'''
replace_once(path, anchor, insert)
old_branch = '''                elif (\n                    classification is AccountRecoveryClassification.FILE_APPLIED_RECEIPT_MISSING\n                    and original_stage in {"PREPARED", "FILE_COMMITTED"}\n                ):\n                    target_stage = "FILE_COMMITTED"\n                    blockers.add("ACCOUNT_FINALIZATION_REQUIRED")\n'''
new_branch = '''                elif (\n                    classification is AccountRecoveryClassification.FILE_APPLIED_RECEIPT_MISSING\n                    and original_stage in {"PREPARED", "FILE_COMMITTED"}\n                ):\n                    if actual is not None and self._recover_broker_sync_finalization(\n                        row=row,\n                        operation_id=operation_id,\n                        actual=actual,\n                        now=now,\n                    ):\n                        receipts.append(\n                            AccountRecoveryReceipt(\n                                operation_id=operation_id,\n                                classification=classification,\n                                actual_account_sha256=actual,\n                            )\n                        )\n                        continue\n                    target_stage = "FILE_COMMITTED"\n                    blockers.add("ACCOUNT_FINALIZATION_REQUIRED")\n'''
replace_once(path, old_branch, new_branch)

# Runtime repository persists and compares the recovery finalization evidence.
path = "src/firmquant/strategy/runtime_account.py"
replace_once(path, "from collections.abc import Callable\n", "from collections.abc import Callable, Mapping\n")
replace_once(
    path,
    "from firmquant.persistence.repositories import PersistenceConflict\n",
    "from firmquant.persistence.repositories import PersistenceConflict, canonical_json\n",
)
replace_once(
    path,
    "        operation_id: str,\n    ) -> AccountOperation | None:\n",
    "        operation_id: str,\n        finalization_payload: Mapping[str, object] | None = None,\n    ) -> AccountOperation | None:\n",
)
replace_once(
    path,
    '''        expected_payload = {\n            "schema": "firmquant.account-operation.v1",\n            "operation_kind": "BROKER_SYNC",\n            "account_path_sha256": path_sha256,\n            "evidence_sha256": prepared.broker_snapshot_sha256,\n        }\n        if payload != expected_payload:\n''',
    '''        expected_payload: dict[str, object] = {\n            "schema": "firmquant.account-operation.v1",\n            "operation_kind": "BROKER_SYNC",\n            "account_path_sha256": path_sha256,\n            "evidence_sha256": prepared.broker_snapshot_sha256,\n        }\n        if finalization_payload is not None:\n            decoded = json.loads(canonical_json(finalization_payload))\n            if not isinstance(decoded, dict):\n                raise PersistenceConflict("broker account finalization payload is invalid")\n            expected_payload["finalization"] = decoded\n        if payload != expected_payload:\n''',
)
replace_once(
    path,
    "        finalize: Callable[[], None] | None = None,\n    ) -> str:\n",
    "        finalize: Callable[[], None] | None = None,\n        finalization_payload: Mapping[str, object] | None = None,\n    ) -> str:\n",
)
replace_once(
    path,
    '''        operation = self._existing_broker_operation(prepared, operation_id=operation_id)\n''',
    '''        operation = self._existing_broker_operation(\n            prepared,\n            operation_id=operation_id,\n            finalization_payload=finalization_payload,\n        )\n''',
)
replace_once(
    path,
    '''                now=now,\n                operation_id=operation_id,\n            )\n''',
    '''                now=now,\n                operation_id=operation_id,\n                finalization_payload=finalization_payload,\n            )\n''',
)

# Coordinator supplies identical evidence to live callback and crash recovery.
path = "src/firmquant/reconciliation/account_coordinator.py"
replace_once(path, "from collections.abc import Callable\n", "from collections.abc import Callable, Mapping\n")
replace_once(
    path,
    "from .models import OperationalLedgerView, ReconciliationFacts, ReconciliationKind, ReconciliationReceipt\n",
    "from .models import OperationalLedgerView, ReconciliationFacts, ReconciliationKind, ReconciliationReceipt\nfrom .service import reconciliation_finalization_payload\n",
)
replace_once(
    path,
    "        finalize: Callable[[], None] | None = None,\n    ) -> str: ...\n",
    "        finalize: Callable[[], None] | None = None,\n        finalization_payload: Mapping[str, object] | None = None,\n    ) -> str: ...\n",
)
replace_once(
    path,
    '''        committed = self._accounts.commit_broker_snapshot(\n            prepared,\n            finalize=lambda: self._reconciler.commit(\n                receipt,\n                broker_snapshot_sha256=snapshot.raw_payload_sha256,\n            ),\n        )\n''',
    '''        finalization = reconciliation_finalization_payload(\n            receipt,\n            broker_snapshot_sha256=snapshot.raw_payload_sha256,\n        )\n        committed = self._accounts.commit_broker_snapshot(\n            prepared,\n            finalize=lambda: self._reconciler.commit(\n                receipt,\n                broker_snapshot_sha256=snapshot.raw_payload_sha256,\n            ),\n            finalization_payload=finalization,\n        )\n''',
)

# Test doubles and legacy recovery expectations follow the final protocol.
path = "tests/unit/application/test_account_reconciliation_integration.py"
replace_once(
    path,
    "            receipt = self._reconciler.run(kind, facts)\n            return SimpleNamespace(receipt=receipt, account=candidate)\n",
    "            receipt = self._reconciler.evaluate(kind, facts)\n            self._reconciler.commit(\n                receipt,\n                broker_snapshot_sha256=snapshot.raw_payload_sha256,\n            )\n            return SimpleNamespace(receipt=receipt, account=candidate)\n",
)
path = "tests/unit/application/test_production_services_acceptance.py"
replace_once(
    path,
    '''        hooks._reconciler = SimpleNamespace(\n            run=lambda _kind, _facts: SimpleNamespace(\n                passed=False,\n                blockers=("BROKER_MISMATCH",),\n            )\n        )\n''',
    '''        hooks._reconciler = SimpleNamespace(\n            evaluate=lambda _kind, _facts: SimpleNamespace(\n                passed=False,\n                blockers=("BROKER_MISMATCH",),\n            ),\n            commit=lambda _receipt, **_kwargs: None,\n        )\n''',
)
path = "tests/integration/test_restart_recovery.py"
replace_once(
    path,
    '''    assert report.account_receipts[0].classification is expected\n    assert report.halt_required is (expected is AccountRecoveryClassification.CONTRADICTION)\n    expected_stage = (\n        "CONTRADICTION" if expected is AccountRecoveryClassification.CONTRADICTION else "RECEIPT_COMMITTED"\n    )\n    assert database.scalar("SELECT stage FROM account_operations") == expected_stage\n''',
    '''    assert report.account_receipts[0].classification is expected\n    assert report.halt_required is True\n    expected_stage = {\n        AccountRecoveryClassification.NOT_APPLIED: "PREPARED",\n        AccountRecoveryClassification.FILE_APPLIED_RECEIPT_MISSING: "FILE_COMMITTED",\n        AccountRecoveryClassification.CONTRADICTION: "CONTRADICTION",\n    }[expected]\n    assert database.scalar("SELECT stage FROM account_operations") == expected_stage\n''',
)
