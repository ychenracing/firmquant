from __future__ import annotations

from pathlib import Path


def replace_once(path: Path, old: str, new: str, label: str) -> None:
    text = path.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected one anchor, got {count}")
    path.write_text(text.replace(old, new, 1), encoding="utf-8", newline="\n")


adapter = Path("src/firmquant/strategy/adapter.py")
anchor = '''    def decide_once(self, request: DecisionRequest) -> DecisionSnapshot:
'''
method = '''    def recover_existing_decision(
        self,
        request: DecisionRequest,
        snapshot: DecisionSnapshot,
    ) -> DecisionSnapshot:
        """Recompute a durable decision after-state and apply it only if every identity is exact."""

        if not isinstance(snapshot, DecisionSnapshot):
            raise StrategyAdapterError("decision recovery requires DecisionSnapshot")
        identity = self._verified_identity()
        symbols = _normalized_symbols(
            request.symbols,
            session=request.strategy_session,
            policy=self._universe_policy,
        )
        account_before_sha256 = _account_sha256(request.account)
        if account_before_sha256 != snapshot.account_before_sha256:
            raise DecisionRecoveryRequired("decision recovery requires the immutable before-state")
        request_fingerprint, input_fingerprint = self._fingerprints(
            request,
            symbols=symbols,
            identity=identity,
            account_before_sha256=account_before_sha256,
        )
        if (
            request_fingerprint != snapshot.request_fingerprint
            or input_fingerprint != snapshot.input_fingerprint
        ):
            raise DecisionRecoveryRequired("decision recovery input identity differs from durable snapshot")
        current_code_hash = self._engine._code_hash
        if current_code_hash not in {None, identity.economic_code_fingerprint}:
            raise StrategyAdapterError("ProductionEngine instance has an unexpected code hash")
        self._engine._code_hash = identity.economic_code_fingerprint
        working = copy.deepcopy(request.account)
        try:
            decision = self._engine.decide(
                symbols=symbols,
                as_of=request.strategy_session.isoformat(),
                account=working,
            )
        except (RuntimeError, TypeError, ValueError) as exc:
            raise DecisionRecoveryRequired("uquant decision recovery recomputation failed") from exc
        uquant_payload = decision.canonical_payload(
            effective_config_sha256=identity.config_fingerprint
        )
        if decision.decision_digest != _canonical_sha256(uquant_payload):
            raise DecisionRecoveryRequired("recomputed uquant decision digest is not canonical")
        account_after_sha256 = _account_sha256(working)
        if getattr(working, "code_hash", None) != identity.economic_code_fingerprint:
            raise DecisionRecoveryRequired("recomputed account code identity differs")
        if getattr(working, "data_hash", None) != request.data_manifest_sha256:
            raise DecisionRecoveryRequired("recomputed account data identity differs")
        if getattr(working, "data_hash_as_of", None) != request.strategy_session.isoformat():
            raise DecisionRecoveryRequired("recomputed account data session differs")
        candidate = DecisionSnapshot.create(
            strategy_session=request.strategy_session,
            request_fingerprint=request_fingerprint,
            input_fingerprint=input_fingerprint,
            firmquant_commit=request.firmquant_commit,
            identity=identity,
            data_manifest_sha256=request.data_manifest_sha256,
            broker_snapshot_sha256=request.broker_snapshot_sha256,
            account_before_sha256=account_before_sha256,
            account_after_sha256=account_after_sha256,
            uquant_payload=uquant_payload,
            uquant_decision_digest=decision.decision_digest,
            risk_summary=decision.risk_summary,
            created_at=snapshot.created_at,
            supersedes_decision_id=snapshot.supersedes_decision_id,
        )
        if candidate != snapshot:
            raise DecisionRecoveryRequired(
                "recomputed decision differs from immutable durable snapshot"
            )
        try:
            commit_prepared_account(
                request.account,
                working,
                expected_sha256=snapshot.account_after_sha256,
            )
        except StrategySyncError as exc:
            raise DecisionRecoveryRequired("recomputed AccountState could not be applied") from exc
        return snapshot

'''
replace_once(adapter, anchor, method + anchor, "adapter recovery method")

services = Path("src/firmquant/application/production_services.py")
old = '''    def _post_close_decision(self, session: date) -> int:
        if self._decisions.for_session(session):
            return 0
        symbols = tuple(sorted(set(self._universe.deployment_symbols) | set(_REFERENCE_SYMBOLS)))
'''
new = '''    def _post_close_decision(self, session: date) -> int:
        existing = self._decisions.for_session(session)
        if existing:
            if len(existing) != 1:
                raise ProductionServicesUnavailable("MULTIPLE_FROZEN_DECISIONS")
            decision = existing[0]
            account = self._accounts.load()
            actual = self._accounts.store.hash_state(account)
            if actual == decision.account_after_sha256:
                return 0
            if actual != decision.account_before_sha256:
                raise ProductionServicesUnavailable("DECISION_ACCOUNT_RECOVERY_CONTRADICTION")
            recovered = self._strategy.recover_existing_decision(
                DecisionRequest(
                    strategy_session=session,
                    symbols=self._universe.deployment_symbols,
                    account=account,
                    firmquant_commit=decision.firmquant_commit,
                    data_manifest_sha256=decision.data_manifest_sha256,
                    broker_snapshot_sha256=decision.broker_snapshot_sha256,
                    created_at=decision.created_at,
                ),
                decision,
            )
            persisted = self._accounts.persist_prepared(
                account,
                expected_before_sha256=decision.account_before_sha256,
                operation_kind="DECISION_RECOVERY",
                evidence_sha256=decision.payload_sha256,
            )
            if recovered.decision_id != decision.decision_id or persisted != decision.account_after_sha256:
                raise ProductionServicesUnavailable("DECISION_ACCOUNT_RECOVERY_MISMATCH")
            self._audit(
                "production-decision-recovery:" + decision.decision_id,
                "PRODUCTION_DECISION_RECOVERY",
                {
                    "schema": "firmquant.production-decision-recovery.v1",
                    "decision_id": decision.decision_id,
                    "strategy_session": session,
                    "account_after_sha256": decision.account_after_sha256,
                },
            )
            return 0
        symbols = tuple(sorted(set(self._universe.deployment_symbols) | set(_REFERENCE_SYMBOLS)))
'''
replace_once(services, old, new, "production decision recovery")

tests = Path("tests/unit/application/test_production_services_acceptance.py")
old = '''    def decide_once(self, request):
        self.requests.append(request)
        return self.decision
'''
new = '''    def decide_once(self, request):
        self.requests.append(request)
        return self.decision

    def recover_existing_decision(self, request, snapshot):
        self.requests.append(request)
        return snapshot
'''
replace_once(tests, old, new, "strategy recovery mock")

old = '''        hooks._decisions = SimpleNamespace(for_session=lambda _session: (decision,))
        assert hooks._post_close_decision(STRATEGY_SESSION) == 0

        hooks._decisions = SimpleNamespace(for_session=lambda _session: ())
'''
new = '''        hooks._decisions = SimpleNamespace(for_session=lambda _session: (decision,))
        assert hooks._post_close_decision(STRATEGY_SESSION) == 0
        assert accounts.persisted[-1][1] == "DECISION_RECOVERY"
        assert hooks._audited("production-decision-recovery:" + decision.decision_id)

        accounts.store.hash_state = lambda _account: decision.account_after_sha256
        assert hooks._post_close_decision(STRATEGY_SESSION) == 0

        hooks._decisions = SimpleNamespace(for_session=lambda _session: ())
'''
replace_once(tests, old, new, "acceptance decision recovery assertion")
