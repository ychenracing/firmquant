from pathlib import Path

production = Path("src/firmquant/application/production_services.py")
text = production.read_text(encoding="utf-8")

old = "from firmquant.persistence.audit import AuditLedger\nfrom firmquant.persistence.backup import backup_state\n"
new = "from firmquant.persistence.account_authority import AccountBindingRepository\nfrom firmquant.persistence.audit import AuditLedger\nfrom firmquant.persistence.backup import backup_state\n"
if old not in text:
    raise SystemExit("production persistence import block drifted")
text = text.replace(old, new, 1)

old = "from firmquant.persistence.production_repository import MonotonicExecutionLedgerRepository\nfrom firmquant.persistence.repositories import DecisionSnapshotRepository, canonical_json, canonical_sha256\n"
new = "from firmquant.persistence.production_repository import MonotonicExecutionLedgerRepository\nfrom firmquant.persistence.recovery import RecoveryContradiction\nfrom firmquant.persistence.repositories import DecisionSnapshotRepository, canonical_json, canonical_sha256\n"
if old not in text:
    raise SystemExit("production recovery import block drifted")
text = text.replace(old, new, 1)

old = "from firmquant.reconciliation.live_view import build_operational_ledger_view\nfrom firmquant.reconciliation.models import (\n"
new = "from firmquant.reconciliation.account_coordinator import (\n    AccountReconciliationBlocked,\n    AccountReconciliationCoordinator,\n)\nfrom firmquant.reconciliation.live_view import build_operational_ledger_view\nfrom firmquant.reconciliation.models import (\n"
if old not in text:
    raise SystemExit("production reconciliation import block drifted")
text = text.replace(old, new, 1)

old = "from firmquant.strategy.adapter import DecisionRequest, ProductionEngineContract, StrategyAdapter\nfrom firmquant.strategy.identity import StrategyIdentity\n"
new = "from firmquant.strategy.account_sync import AccountStateContract\nfrom firmquant.strategy.adapter import DecisionRequest, ProductionEngineContract, StrategyAdapter\nfrom firmquant.strategy.identity import StrategyIdentity\n"
if old not in text:
    raise SystemExit("production strategy import block drifted")
text = text.replace(old, new, 1)

old = '''    def _reconcile(self, kind: ReconciliationKind) -> tuple[ReconciliationReceipt, BrokerSnapshot, object]:
        snapshot = ProductionSnapshotCollector(
            broker=self._broker,
            clock=self._clock,
            max_attempts=3,
        ).capture()
        expected_id, expected_type = self._snapshots.previous_account_identity(snapshot)
        self._snapshots.persist(snapshot)
        account, _ = self._accounts.sync_broker_snapshot(snapshot)
        identity = StrategyIdentity.locked()
        payload = _account_payload(account)
        facts = ReconciliationFacts(
            broker_snapshot=snapshot,
            strategy_account=_strategy_view(account, snapshot.positions, self._accounts),
            operational_ledger=build_operational_ledger_view(
                self._database,
                broker_session=snapshot.session_date,
                expected_account_id_hash=expected_id,
                expected_account_type=expected_type,
            ),
            company_action_suspected_symbols=frozenset(),
            uquant_code_identity_matches=(
                payload.get("code_hash") in {"", identity.economic_code_fingerprint}
            ),
            data_identity_matches=_data_identity_matches(account, self._settings.paths.data_directory),
            config_identity_matches=(configuration_sha256(self._config_path) == self._identity.config_sha256),
        )
        receipt = self._reconciler.run(kind, facts)
        if not receipt.passed:
            raise ProductionServicesUnavailable("RECONCILIATION_FAILED:" + ",".join(receipt.blockers))
        return receipt, snapshot, account
'''
new = '''    def _reconcile(self, kind: ReconciliationKind) -> tuple[ReconciliationReceipt, BrokerSnapshot, object]:
        snapshot = ProductionSnapshotCollector(
            broker=self._broker,
            clock=self._clock,
            max_attempts=3,
        ).capture()
        self._snapshots.persist(snapshot)
        binding = AccountBindingRepository(self._database).load()
        if binding is None:
            raise ProductionServicesUnavailable("ACCOUNT_BINDING_REQUIRED")
        operational = build_operational_ledger_view(
            self._database,
            broker_session=snapshot.session_date,
            expected_account_id_hash=binding.account_id_hash,
            expected_account_type=binding.account_type,
        )
        coordinator = AccountReconciliationCoordinator(
            account_repository=self._accounts,
            reconciler=self._reconciler,
            cash_tolerance=Decimal("0.01"),
        )

        def final_facts(account: AccountStateContract) -> ReconciliationFacts:
            identity = StrategyIdentity.locked()
            payload = _account_payload(account)
            return ReconciliationFacts(
                broker_snapshot=snapshot,
                strategy_account=_strategy_view(account, snapshot.positions, self._accounts),
                operational_ledger=operational,
                company_action_suspected_symbols=frozenset(),
                uquant_code_identity_matches=(
                    payload.get("code_hash") in {"", identity.economic_code_fingerprint}
                ),
                data_identity_matches=_data_identity_matches(
                    account,
                    self._settings.paths.data_directory,
                ),
                config_identity_matches=(
                    configuration_sha256(self._config_path) == self._identity.config_sha256
                ),
            )

        try:
            result = coordinator.reconcile(
                kind=kind,
                snapshot=snapshot,
                operational_ledger=operational,
                binding=binding,
                final_facts=final_facts,
            )
        except AccountReconciliationBlocked as error:
            raise ProductionServicesUnavailable(
                "RECONCILIATION_FAILED:" + ",".join(error.blockers)
            ) from error
        except RecoveryContradiction as error:
            raise ProductionServicesUnavailable("ACCOUNT_COMMIT_CONTRADICTION") from error
        return cast(ReconciliationReceipt, result.receipt), snapshot, result.account
'''
if old not in text:
    raise SystemExit("production _reconcile block drifted")
production.write_text(text.replace(old, new, 1), encoding="utf-8")

acceptance = Path("tests/unit/application/test_production_services_acceptance.py")
text = acceptance.read_text(encoding="utf-8")
old = '''class Account:
    def __init__(self) -> None:
        self.payload: dict[str, object] = {
'''
new = '''class AccountPosition:
    def __init__(self, shares: int) -> None:
        self.shares = shares

    def sellable_shares(self, _date: str) -> int:
        return self.shares


class Account:
    def __init__(self) -> None:
        self.payload: dict[str, object] = {
'''
if old not in text:
    raise SystemExit("acceptance Account class block drifted")
text = text.replace(old, new, 1)

old = '''    def to_dict(self) -> dict[str, object]:
        return self.payload


class AccountStore:
'''
new = '''    @property
    def cash(self) -> float:
        return float(self.payload["cash"])

    @property
    def positions(self) -> dict[str, AccountPosition]:
        raw = self.payload["positions"]
        assert isinstance(raw, dict)
        return {
            str(symbol): AccountPosition(int(position["shares"]))
            for symbol, position in raw.items()
            if isinstance(position, dict)
        }

    @property
    def order_ledger(self) -> list[object]:
        return []

    @property
    def fills(self) -> list[object]:
        return []

    def to_dict(self) -> dict[str, object]:
        return self.payload


class AccountStore:
'''
if old not in text:
    raise SystemExit("acceptance Account properties insertion drifted")
text = text.replace(old, new, 1)

old = '''    def sync_broker_snapshot(self, snapshot):
        return self.account, SimpleNamespace(
            account_before_sha256="c" * 64,
            account_after_sha256="c" * 64,
            snapshot_id=snapshot.snapshot_id,
        )

    def persist_prepared(
'''
new = '''    def sync_broker_snapshot(self, snapshot):
        return self.account, SimpleNamespace(
            account_before_sha256="c" * 64,
            account_after_sha256="c" * 64,
            snapshot_id=snapshot.snapshot_id,
        )

    def prepare_broker_snapshot(self, snapshot):
        return SimpleNamespace(
            prepared_account=self.account,
            receipt=SimpleNamespace(snapshot_id=snapshot.snapshot_id),
            account_before_sha256="c" * 64,
            account_after_sha256="c" * 64,
            evidence_sha256=snapshot.raw_payload_sha256,
        )

    def commit_broker_snapshot(self, prepared) -> str:
        return str(prepared.account_after_sha256)

    def persist_prepared(
'''
if old not in text:
    raise SystemExit("acceptance Accounts methods block drifted")
text = text.replace(old, new, 1)

marker = '''def test_hook_reconciliation_builds_session_scoped_authority_view(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with hook_case(tmp_path) as (hooks, _writer, _broker, accounts):
        reconciler = PassingReconciler()
'''
replacement = '''def test_hook_reconciliation_builds_session_scoped_authority_view(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with hook_case(tmp_path) as (hooks, writer, _broker, accounts):
        from firmquant.persistence.account_authority import AccountBinding, AccountBindingRepository
        from firmquant.strategy.identity import StrategyIdentity

        broker_snapshot = execution_snapshot().broker_snapshot
        identity = StrategyIdentity.locked()
        AccountBindingRepository(writer.database).bind(
            AccountBinding.create(
                account_id_hash=broker_snapshot.account.account_id_hash,
                account_type=broker_snapshot.account.account_type,
                broker_snapshot_sha256="a" * 64,
                account_state_sha256="c" * 64,
                uquant_commit=identity.uquant_commit,
                uquant_code_fingerprint=identity.economic_code_fingerprint,
                data_hash="d" * 64,
                data_as_of="2026-08-24",
                data_symbols=("sz300308",),
                created_at=NOW,
            )
        )
        reconciler = PassingReconciler()
'''
if marker not in text:
    raise SystemExit("acceptance reconciliation test marker drifted")
acceptance.write_text(text.replace(marker, replacement, 1), encoding="utf-8")
