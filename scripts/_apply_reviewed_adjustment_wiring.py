from pathlib import Path

path = Path("src/firmquant/persistence/account_authority.py")
text = path.read_text(encoding="utf-8")
old = '''    def covers(
        self,
        *,
        account_id_hash: str,
        symbol: Symbol,
        session: date,
        coverage: AdjustmentCoverage,
        broker_snapshot_sha256: str,
        difference_sha256: str,
    ) -> bool:
        _sha256(account_id_hash, label="adjustment lookup account identity")
        if not isinstance(symbol, Symbol) or type(session) is not date:
            raise TypeError("adjustment lookup requires Symbol and date")
        if not isinstance(coverage, AdjustmentCoverage):
            raise TypeError("adjustment lookup coverage must be typed")
        _sha256(broker_snapshot_sha256, label="adjustment lookup broker snapshot")
        _sha256(difference_sha256, label="adjustment lookup difference")
        return (
            self._database.query_one(
                """
                SELECT 1 FROM reviewed_account_adjustments
                WHERE account_id_hash = ? AND symbol = ? AND session_date = ?
                  AND coverage_kind = ? AND broker_snapshot_sha256 = ? AND difference_sha256 = ?
                LIMIT 1
                """,
                (
                    account_id_hash,
                    symbol.canonical,
                    session.isoformat(),
                    coverage.value,
                    broker_snapshot_sha256,
                    difference_sha256,
                ),
            )
            is not None
        )
'''
new = '''    def matching_ids(
        self,
        *,
        account_id_hash: str,
        symbol: Symbol | None,
        session: date,
        coverage: AdjustmentCoverage,
        broker_snapshot_sha256: str,
        difference_sha256: str,
    ) -> tuple[str, ...]:
        """Return exact reviewed evidence identities after re-verifying stored payload hashes."""

        _sha256(account_id_hash, label="adjustment lookup account identity")
        if symbol is not None and not isinstance(symbol, Symbol):
            raise TypeError("adjustment lookup symbol must be Symbol or None")
        if type(session) is not date:
            raise TypeError("adjustment lookup session must be date")
        if not isinstance(coverage, AdjustmentCoverage):
            raise TypeError("adjustment lookup coverage must be typed")
        _sha256(broker_snapshot_sha256, label="adjustment lookup broker snapshot")
        _sha256(difference_sha256, label="adjustment lookup difference")
        parameters: tuple[object, ...] = (
            account_id_hash,
            session.isoformat(),
            coverage.value,
            broker_snapshot_sha256,
            difference_sha256,
        )
        if symbol is None:
            rows = self._database.query_all(
                """
                SELECT adjustment_id, payload_json, payload_sha256
                FROM reviewed_account_adjustments
                WHERE account_id_hash = ? AND session_date = ?
                  AND coverage_kind = ? AND broker_snapshot_sha256 = ? AND difference_sha256 = ?
                ORDER BY adjustment_id
                """,
                parameters,
            )
        else:
            rows = self._database.query_all(
                """
                SELECT adjustment_id, payload_json, payload_sha256
                FROM reviewed_account_adjustments
                WHERE account_id_hash = ? AND session_date = ?
                  AND coverage_kind = ? AND broker_snapshot_sha256 = ? AND difference_sha256 = ?
                  AND symbol = ?
                ORDER BY adjustment_id
                """,
                (*parameters, symbol.canonical),
            )
        identities: list[str] = []
        for row in rows:
            payload_json = str(row["payload_json"])
            payload_sha256 = str(row["payload_sha256"])
            actual = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
            adjustment_id = str(row["adjustment_id"])
            if actual != payload_sha256 or adjustment_id != "acctadj_" + payload_sha256:
                raise PersistenceConflict("reviewed adjustment stored identity is corrupt")
            identities.append(adjustment_id)
        return tuple(identities)

    def covers(
        self,
        *,
        account_id_hash: str,
        symbol: Symbol,
        session: date,
        coverage: AdjustmentCoverage,
        broker_snapshot_sha256: str,
        difference_sha256: str,
    ) -> bool:
        return bool(
            self.matching_ids(
                account_id_hash=account_id_hash,
                symbol=symbol,
                session=session,
                coverage=coverage,
                broker_snapshot_sha256=broker_snapshot_sha256,
                difference_sha256=difference_sha256,
            )
        )
'''
if text.count(old) != 1:
    raise SystemExit("reviewed adjustment covers block drifted")
path.write_text(text.replace(old, new, 1), encoding="utf-8")

path = Path("src/firmquant/application/production_services.py")
text = path.read_text(encoding="utf-8")
old = "from firmquant.persistence.account_authority import AccountBindingRepository\n"
new = "from firmquant.persistence.account_authority import (\n    AccountBindingRepository,\n    ReviewedAccountAdjustmentRepository,\n)\n"
if text.count(old) != 1:
    raise SystemExit("production account authority import drifted")
text = text.replace(old, new, 1)
old = '''        coordinator = AccountReconciliationCoordinator(
            account_repository=self._accounts,
            reconciler=self._reconciler,
            cash_tolerance=Decimal("0.01"),
        )
'''
new = '''        coordinator = AccountReconciliationCoordinator(
            account_repository=self._accounts,
            reconciler=self._reconciler,
            cash_tolerance=Decimal("0.01"),
            reviewed_adjustments=ReviewedAccountAdjustmentRepository(self._database),
        )
'''
if text.count(old) != 1:
    raise SystemExit("production coordinator construction drifted")
path.write_text(text.replace(old, new, 1), encoding="utf-8")

path = Path("tests/unit/application/test_account_reconciliation_integration.py")
text = path.read_text(encoding="utf-8")
old = '''        def __init__(self, *, account_repository, reconciler, cash_tolerance: Decimal) -> None:
            calls.append(
                {
                    "account_repository": account_repository,
                    "reconciler": reconciler,
                    "cash_tolerance": cash_tolerance,
                }
            )
'''
new = '''        def __init__(
            self,
            *,
            account_repository,
            reconciler,
            cash_tolerance: Decimal,
            reviewed_adjustments=None,
        ) -> None:
            calls.append(
                {
                    "account_repository": account_repository,
                    "reconciler": reconciler,
                    "cash_tolerance": cash_tolerance,
                    "reviewed_adjustments": reviewed_adjustments,
                }
            )
'''
if text.count(old) != 1:
    raise SystemExit("recording coordinator constructor drifted")
path.write_text(text.replace(old, new, 1), encoding="utf-8")
