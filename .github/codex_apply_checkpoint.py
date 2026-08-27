from __future__ import annotations

from pathlib import Path


def replace_once(path: str, old: str, new: str) -> None:
    target = Path(path)
    text = target.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"expected one match in {path}, found {count}: {old[:80]!r}")
    target.write_text(text.replace(old, new, 1), encoding="utf-8")


def replace_all(path: str, old: str, new: str, *, minimum: int = 1) -> None:
    target = Path(path)
    text = target.read_text(encoding="utf-8")
    count = text.count(old)
    if count < minimum:
        raise RuntimeError(f"expected at least {minimum} matches in {path}, found {count}")
    target.write_text(text.replace(old, new), encoding="utf-8")


# Backup: separate uquant account-authority hash from raw member-byte hash.
replace_once(
    "src/firmquant/persistence/backup.py",
    "from .repositories import canonical_json\nfrom .schema import CURRENT_SCHEMA_VERSION\n",
    "from .recovery import UquantAccountStateStore\nfrom .repositories import canonical_json\nfrom .schema import CURRENT_SCHEMA_VERSION\n",
)
replace_once(
    "src/firmquant/persistence/backup.py",
    '''    account_sha256 = _text(deployment, "account_sha256", label="deployment")\n    if account_sha256 != member_hashes["account_state.json"]:\n        raise BackupVerificationError("complete backup account identity is inconsistent")\n''',
    '''    account_sha256 = _text(deployment, "account_sha256", label="deployment")\n    try:\n        authority_hash = UquantAccountStateStore().hash_file(bundle / "account_state.json")\n    except Exception as exc:\n        raise BackupVerificationError("complete backup account authority is invalid") from exc\n    if account_sha256 != authority_hash:\n        raise BackupVerificationError("complete backup account identity is inconsistent")\n''',
)
replace_once(
    "src/firmquant/persistence/backup.py",
    '''    if member_hashes["account_state.json"] != inputs.account_sha256:\n        raise BackupError("account state changed before complete backup capture")\n''',
    "",
)

# Close checkpoint: discover and resume the latest incomplete durable close.
replace_once(
    "src/firmquant/application/close_checkpoint.py",
    '''    def latest_completed_session(self) -> date | None:\n''',
    '''    def latest_incomplete_session(self) -> date | None:\n        rows = self._database.query_all(\n            "SELECT payload_json FROM audit_events WHERE category = 'CLOSE_SESSION' ORDER BY sequence"\n        )\n        observed: set[date] = set()\n        completed: set[date] = set()\n        for row in rows:\n            payload = _decode(row["payload_json"])\n            try:\n                session = date.fromisoformat(str(payload["session"]))\n                step = CloseStep(str(payload["step"]))\n            except (KeyError, ValueError) as error:\n                raise CloseCheckpointError("stored close-session identity is invalid") from error\n            observed.add(session)\n            if step is CloseStep.COMPLETED:\n                completed.add(session)\n        incomplete = observed - completed\n        return max(incomplete) if incomplete else None\n\n    def latest_completed_session(self) -> date | None:\n''',
)

# Production close orchestration imports and runtime data generation wiring.
replace_once(
    "src/firmquant/application/production_services.py",
    "from firmquant.application.event_pump import DomainEventPump\n",
    "from firmquant.application.close_checkpoint import CloseCheckpointStore, CloseStep\nfrom firmquant.application.event_pump import DomainEventPump\n",
)
replace_once(
    "src/firmquant/application/production_services.py",
    "from firmquant.market_data.calendar_manifest import load_trading_calendar_manifest\n",
    "from firmquant.market_data.calendar_manifest import load_trading_calendar_manifest\nfrom firmquant.market_data.generations import DataGenerationStore\n",
)
replace_once(
    "src/firmquant/application/production_services.py",
    "from firmquant.persistence.backup import backup_state\n",
    "from firmquant.persistence.backup import BackupBundleInputs, backup_state\n",
)
replace_once(
    "src/firmquant/application/production_services.py",
    '''        clock: Callable[[], datetime],\n        monotonic_clock: Callable[[], float] = time_module.monotonic,\n    ) -> None:\n''',
    '''        clock: Callable[[], datetime],\n        monotonic_clock: Callable[[], float] = time_module.monotonic,\n        data_generation_store: DataGenerationStore | None = None,\n        data_root: Path | None = None,\n        data_reloader: Callable[[Path], None] | None = None,\n    ) -> None:\n''',
)
replace_once(
    "src/firmquant/application/production_services.py",
    '''        self._clock = clock\n        self._monotonic_clock = monotonic_clock\n        self._last_monotonic: float | None = None\n''',
    '''        self._clock = clock\n        self._monotonic_clock = monotonic_clock\n        self._data_generations = data_generation_store\n        self._data_root = Path(data_root) if data_root is not None else settings.paths.data_directory\n        self._data_reloader = data_reloader\n        self._close = CloseCheckpointStore(self._database)\n        self._last_monotonic: float | None = None\n''',
)
replace_once(
    "src/firmquant/application/production_services.py",
    '''        and getattr(manifest, "end", None) == as_of\n        and tuple(getattr(manifest, "symbols", ())) == tuple(symbols)\n''',
    '''        and tuple(getattr(manifest, "symbols", ())) == tuple(symbols)\n''',
)
replace_all(
    "src/firmquant/application/production_services.py",
    "_data_identity_matches(\n                    account,\n                    self._settings.paths.data_directory,\n                )",
    "_data_identity_matches(account, self._data_root)",
)
replace_once(
    "src/firmquant/application/production_services.py",
    "data_identity_matches=_data_identity_matches(account_state, self._settings.paths.data_directory),",
    "data_identity_matches=_data_identity_matches(account_state, self._data_root),",
)

old_post_close = '''    def _post_close_decision(self, session: date) -> int:\n        existing = self._decisions.for_session(session)\n        if existing:\n            if len(existing) != 1:\n                raise ProductionServicesUnavailable("MULTIPLE_FROZEN_DECISIONS")\n            decision = existing[0]\n            account = self._accounts.load()\n            actual = self._accounts.store.hash_state(account)\n            if actual == decision.account_after_sha256:\n                return 0\n            if actual != decision.account_before_sha256:\n                raise ProductionServicesUnavailable("DECISION_ACCOUNT_RECOVERY_CONTRADICTION")\n            recovered = self._strategy.recover_existing_decision(\n                DecisionRequest(\n                    strategy_session=session,\n                    symbols=self._universe.deployment_symbols,\n                    account=account,\n                    firmquant_commit=decision.firmquant_commit,\n                    data_manifest_sha256=decision.data_manifest_sha256,\n                    broker_snapshot_sha256=decision.broker_snapshot_sha256,\n                    created_at=decision.created_at,\n                ),\n                decision,\n            )\n            persisted = self._accounts.persist_prepared(\n                account,\n                expected_before_sha256=decision.account_before_sha256,\n                operation_kind="DECISION_RECOVERY",\n                evidence_sha256=decision.payload_sha256,\n            )\n            if recovered.decision_id != decision.decision_id or persisted != decision.account_after_sha256:\n                raise ProductionServicesUnavailable("DECISION_ACCOUNT_RECOVERY_MISMATCH")\n            self._audit(\n                "production-decision-recovery:" + decision.decision_id,\n                "PRODUCTION_DECISION_RECOVERY",\n                {\n                    "schema": "firmquant.production-decision-recovery.v1",\n                    "decision_id": decision.decision_id,\n                    "strategy_session": session,\n                    "account_after_sha256": decision.account_after_sha256,\n                },\n            )\n            return 0\n        symbols = tuple(sorted(set(self._universe.deployment_symbols) | set(_REFERENCE_SYMBOLS)))\n        update = self._data_updater.update(symbols, through=session)\n        snapshot = self._capture()\n        account = self._accounts.load()\n        reconciliation_id = self._latest_passed_reconciliation(ReconciliationKind.EOD)\n        decision = self._strategy.decide_once(\n            DecisionRequest(\n                strategy_session=session,\n                symbols=self._universe.deployment_symbols,\n                account=account,\n                firmquant_commit=self._identity.firmquant_commit,\n                data_manifest_sha256=update.manifest_sha256,\n                broker_snapshot_sha256=snapshot.raw_payload_sha256,\n                created_at=self._now(),\n            )\n        )\n        persisted = self._accounts.persist_prepared(\n            account,\n            expected_before_sha256=decision.account_before_sha256,\n            operation_kind="DECISION_COMMIT",\n            evidence_sha256=decision.payload_sha256,\n        )\n        if persisted != decision.account_after_sha256:\n            raise ProductionServicesUnavailable("DECISION_ACCOUNT_COMMIT_MISMATCH")\n        self._audit(\n            "production-decision:" + decision.decision_id,\n            "PRODUCTION_DECISION",\n            {\n                "schema": "firmquant.production-decision.v1",\n                "decision_id": decision.decision_id,\n                "session": session,\n                "data_manifest_sha256": update.manifest_sha256,\n                "reconciliation_id": reconciliation_id,\n            },\n        )\n        return 1\n\n'''
new_post_close = '''    def _post_close_decision(\n        self,\n        session: date,\n        *,\n        data_manifest_sha256: str | None = None,\n        broker_snapshot_sha256: str | None = None,\n        reconciliation_id: str | None = None,\n    ) -> int:\n        existing = self._decisions.for_session(session)\n        if existing:\n            if len(existing) != 1:\n                raise ProductionServicesUnavailable("MULTIPLE_FROZEN_DECISIONS")\n            decision = existing[0]\n            account = self._accounts.load()\n            actual = self._accounts.store.hash_state(account)\n            if actual == decision.account_after_sha256:\n                return 0\n            if actual != decision.account_before_sha256:\n                raise ProductionServicesUnavailable("DECISION_ACCOUNT_RECOVERY_CONTRADICTION")\n            recovered = self._strategy.recover_existing_decision(\n                DecisionRequest(\n                    strategy_session=session,\n                    symbols=self._universe.deployment_symbols,\n                    account=account,\n                    firmquant_commit=decision.firmquant_commit,\n                    data_manifest_sha256=decision.data_manifest_sha256,\n                    broker_snapshot_sha256=decision.broker_snapshot_sha256,\n                    created_at=decision.created_at,\n                ),\n                decision,\n            )\n            persisted = self._accounts.persist_prepared(\n                account,\n                expected_before_sha256=decision.account_before_sha256,\n                operation_kind="DECISION_RECOVERY",\n                evidence_sha256=decision.payload_sha256,\n            )\n            if recovered.decision_id != decision.decision_id or persisted != decision.account_after_sha256:\n                raise ProductionServicesUnavailable("DECISION_ACCOUNT_RECOVERY_MISMATCH")\n            self._audit(\n                "production-decision-recovery:" + decision.decision_id,\n                "PRODUCTION_DECISION_RECOVERY",\n                {\n                    "schema": "firmquant.production-decision-recovery.v1",\n                    "decision_id": decision.decision_id,\n                    "strategy_session": session,\n                    "account_after_sha256": decision.account_after_sha256,\n                },\n            )\n            return 0\n        if data_manifest_sha256 is None:\n            symbols = tuple(sorted(set(self._universe.deployment_symbols) | set(_REFERENCE_SYMBOLS)))\n            update = self._data_updater.update(symbols, through=session)\n            data_manifest_sha256 = update.manifest_sha256\n            if self._data_reloader is not None:\n                self._data_reloader(self._data_root)\n        if broker_snapshot_sha256 is None:\n            broker_snapshot_sha256 = self._capture().raw_payload_sha256\n        if reconciliation_id is None:\n            reconciliation_id = self._latest_passed_reconciliation(ReconciliationKind.EOD)\n        account = self._accounts.load()\n        decision = self._strategy.decide_once(\n            DecisionRequest(\n                strategy_session=session,\n                symbols=self._universe.deployment_symbols,\n                account=account,\n                firmquant_commit=self._identity.firmquant_commit,\n                data_manifest_sha256=data_manifest_sha256,\n                broker_snapshot_sha256=broker_snapshot_sha256,\n                created_at=self._now(),\n            )\n        )\n        persisted = self._accounts.persist_prepared(\n            account,\n            expected_before_sha256=decision.account_before_sha256,\n            operation_kind="DECISION_COMMIT",\n            evidence_sha256=decision.payload_sha256,\n        )\n        if persisted != decision.account_after_sha256:\n            raise ProductionServicesUnavailable("DECISION_ACCOUNT_COMMIT_MISMATCH")\n        self._audit(\n            "production-decision:" + decision.decision_id,\n            "PRODUCTION_DECISION",\n            {\n                "schema": "firmquant.production-decision.v2",\n                "decision_id": decision.decision_id,\n                "session": session,\n                "data_manifest_sha256": data_manifest_sha256,\n                "broker_snapshot_sha256": broker_snapshot_sha256,\n                "reconciliation_id": reconciliation_id,\n            },\n        )\n        return 1\n\n'''
replace_once("src/firmquant/application/production_services.py", old_post_close, new_post_close)

replace_once(
    "src/firmquant/application/production_services.py",
    '''        try:\n            strategy_session = self._calendar.previous_trading_session(session)\n        except CalendarCoverageError:\n            return 0\n        decisions = self._decisions.for_session(strategy_session)\n        if not decisions:\n            return 0\n''',
    '''        try:\n            strategy_session = self._calendar.previous_trading_session(session)\n        except CalendarCoverageError as error:\n            self.halt("CALENDAR_COVERAGE_DECISION_BLOCKER")\n            raise ProductionServicesUnavailable("CALENDAR_COVERAGE_DECISION_BLOCKER") from error\n        if self._close.completed(strategy_session) is None:\n            self.halt("MISSING_DECISION")\n            raise ProductionServicesUnavailable("MISSING_DECISION")\n        decisions = self._decisions.for_session(strategy_session)\n        if not decisions:\n            self.halt("MISSING_DECISION")\n            raise ProductionServicesUnavailable("MISSING_DECISION")\n''',
)

old_eod = '''    def _eod(self, session: date) -> int:\n        event_id = "production-eod:" + session.isoformat()\n        if self._audited(event_id):\n            return 0\n        receipt, _, _ = self._reconcile(ReconciliationKind.EOD)\n        report = DatabaseDailyReportBuilder(self._database, clock=self._clock).build(session)\n        rendered = DailyReportRenderer().write(report, self._settings.paths.report_directory)\n        backup = backup_state(\n            self._database,\n            self._settings.paths.backup_directory,\n            account_state_path=self._accounts.path,\n            created_at=self._now(),\n        )\n        self._audit(\n            event_id,\n            "PRODUCTION_EOD",\n            {\n                "schema": "firmquant.production-eod.v1",\n                "session": session,\n                "reconciliation_id": receipt.reconciliation_id,\n                "report_id": rendered.report_id,\n                "backup_id": backup.backup_id,\n                "backup_manifest_sha256": backup.manifest_sha256,\n            },\n        )\n        return 1\n\n'''
new_eod = '''    @staticmethod\n    def _checkpoint_text(evidence: Mapping[str, object], key: str) -> str:\n        value = evidence.get(key)\n        if not isinstance(value, str) or not value:\n            raise ProductionServicesUnavailable("CLOSE_SESSION_CHECKPOINT_INVALID")\n        return value\n\n    def _close_session(self, session: date) -> tuple[int, int]:\n        if self._close.completed(session) is not None:\n            return 0, 0\n\n        eod = self._close.load(session, CloseStep.EOD_RECONCILED)\n        if eod is None:\n            receipt, snapshot, _ = self._reconcile(ReconciliationKind.EOD)\n            eod = self._close.append(\n                session,\n                CloseStep.EOD_RECONCILED,\n                evidence={\n                    "reconciliation_id": receipt.reconciliation_id,\n                    "broker_snapshot_sha256": snapshot.raw_payload_sha256,\n                    "broker_snapshot_id": snapshot.snapshot_id,\n                },\n                created_at=self._now(),\n            )\n\n        data = self._close.load(session, CloseStep.DATA_VALIDATED)\n        if data is None:\n            symbols = tuple(sorted(set(self._universe.deployment_symbols) | set(_REFERENCE_SYMBOLS)))\n            update = self._data_updater.update(symbols, through=session)\n            if self._data_reloader is not None:\n                self._data_reloader(self._data_root)\n            data = self._close.append(\n                session,\n                CloseStep.DATA_VALIDATED,\n                evidence={\n                    "data_manifest_sha256": update.manifest_sha256,\n                    "governance_manifest_sha256": update.governance_manifest_sha256,\n                    "data_generation_id": update.data_generation_id,\n                    "fetch_attempts": update.fetch_attempts,\n                },\n                created_at=self._now(),\n            )\n\n        decision_count = 0\n        decision_cp = self._close.load(session, CloseStep.DECISION_COMMITTED)\n        if decision_cp is None:\n            decision_count = self._post_close_decision(\n                session,\n                data_manifest_sha256=self._checkpoint_text(data.evidence, "data_manifest_sha256"),\n                broker_snapshot_sha256=self._checkpoint_text(\n                    eod.evidence, "broker_snapshot_sha256"\n                ),\n                reconciliation_id=self._checkpoint_text(eod.evidence, "reconciliation_id"),\n            )\n            decisions = self._decisions.for_session(session)\n            if len(decisions) != 1:\n                raise ProductionServicesUnavailable("FROZEN_DECISION_NOT_UNIQUE")\n            decision = decisions[0]\n            decision_cp = self._close.append(\n                session,\n                CloseStep.DECISION_COMMITTED,\n                evidence={\n                    "decision_id": decision.decision_id,\n                    "decision_payload_sha256": decision.payload_sha256,\n                    "account_after_sha256": decision.account_after_sha256,\n                },\n                created_at=self._now(),\n            )\n        decision_id = self._checkpoint_text(decision_cp.evidence, "decision_id")\n\n        report_cp = self._close.load(session, CloseStep.REPORT_PUBLISHED)\n        if report_cp is None:\n            report = DatabaseDailyReportBuilder(self._database, clock=self._clock).build(session)\n            if report.decision_id != decision_id:\n                raise ProductionServicesUnavailable("REPORT_DECISION_IDENTITY_MISMATCH")\n            rendered = DailyReportRenderer().write(report, self._settings.paths.report_directory)\n            report_cp = self._close.append(\n                session,\n                CloseStep.REPORT_PUBLISHED,\n                evidence={\n                    "report_id": rendered.report_id,\n                    "json_sha256": rendered.json_sha256,\n                    "markdown_sha256": rendered.markdown_sha256,\n                    "decision_id": decision_id,\n                },\n                created_at=self._now(),\n            )\n\n        backup_cp = self._close.load(session, CloseStep.BACKUP_VERIFIED)\n        if backup_cp is None:\n            if self._data_generations is None:\n                raise ProductionServicesUnavailable("ACTIVE_DATA_GENERATION_UNAVAILABLE")\n            generation = self._data_generations.active()\n            safety_path = self._settings.broker.safety_manifest_path\n            if safety_path is None:\n                raise ProductionServicesUnavailable("XTQUANT_SAFETY_MANIFEST_MISSING")\n            strategy_manifest = self._data_root / ".firmquant-data-manifest.json"\n            backup = backup_state(\n                self._database,\n                self._settings.paths.backup_directory,\n                account_state_path=self._accounts.path,\n                created_at=self._now(),\n                complete_inputs=BackupBundleInputs(\n                    settings=self._settings,\n                    config_sha256=self._identity.config_sha256,\n                    safety_manifest_path=safety_path,\n                    calendar_manifest_path=self._settings.paths.data_directory / _CALENDAR_FILE,\n                    active_data_manifest_path=generation.path / "generation.json",\n                    strategy_data_manifest_path=strategy_manifest,\n                    firmquant_commit=self._identity.firmquant_commit,\n                    uquant_commit=self._identity.uquant_commit,\n                    account_sha256=self._accounts.store.hash_file(self._accounts.path),\n                    decision_id=decision_id,\n                    strategy_session=session,\n                ),\n            )\n            backup_cp = self._close.append(\n                session,\n                CloseStep.BACKUP_VERIFIED,\n                evidence={\n                    "backup_id": backup.backup_id,\n                    "backup_manifest_sha256": backup.manifest_sha256,\n                    "decision_id": decision_id,\n                },\n                created_at=self._now(),\n            )\n\n        self._close.append(\n            session,\n            CloseStep.COMPLETED,\n            evidence={\n                "decision_id": decision_id,\n                "report_id": self._checkpoint_text(report_cp.evidence, "report_id"),\n                "backup_id": self._checkpoint_text(backup_cp.evidence, "backup_id"),\n                "reconciliation_id": self._checkpoint_text(eod.evidence, "reconciliation_id"),\n                "data_manifest_sha256": self._checkpoint_text(\n                    data.evidence, "data_manifest_sha256"\n                ),\n            },\n            created_at=self._now(),\n        )\n        return decision_count, 1\n\n'''
replace_once("src/firmquant/application/production_services.py", old_eod, new_eod)

replace_once(
    "src/firmquant/application/production_services.py",
    '''        elif market_status is MarketSessionStatus.CLOSED and shanghai.time() >= _POST_CLOSE:\n            self._transition(RuntimeState.RECONCILING, reason="end-of-day reconciliation")\n            try:\n                eod = self._eod(session)\n            except Exception:\n                self.halt("EOD_RECONCILIATION_FAILED")\n                raise\n            self._transition(RuntimeState.READY, reason="end-of-day reconciliation completed")\n            self._transition(RuntimeState.EXECUTING, reason="post-close strategy decision")\n            try:\n                decisions = self._post_close_decision(session)\n            except Exception:\n                self.halt("POST_CLOSE_DECISION_FAILED")\n                raise\n            self._transition(RuntimeState.READY, reason="post-close strategy decision completed")\n''',
    '''        elif market_status is MarketSessionStatus.CLOSED and shanghai.time() >= _POST_CLOSE:\n            self._transition(RuntimeState.RECONCILING, reason="close-session checkpoint")\n            try:\n                decisions, eod = self._close_session(session)\n            except Exception:\n                self.halt("CLOSE_SESSION_FAILED")\n                raise\n            self._transition(RuntimeState.READY, reason="close-session checkpoint completed")\n''',
)

# Resume incomplete close before normal startup reconciliation.
replace_once(
    "src/firmquant/application/production_services.py",
    '''        account = self._broker.query_account()\n        run_readonly_production_smoke(\n''',
    '''        incomplete_close = self._close.latest_incomplete_session()\n        if incomplete_close is not None:\n            try:\n                self._close_session(incomplete_close)\n            except Exception as error:\n                self._transition(\n                    RuntimeState.HALTED,\n                    reason="incomplete close-session recovery failed",\n                    blockers=("CLOSE_SESSION_RECOVERY_FAILED",),\n                )\n                raise ProductionServicesUnavailable("CLOSE_SESSION_RECOVERY_FAILED") from error\n        account = self._broker.query_account()\n        run_readonly_production_smoke(\n''',
)

# Build runtime from the active data generation and refresh uquant DataStore after each close update.
replace_once(
    "src/firmquant/application/production_services.py",
    '''    calendar = load_trading_calendar_manifest(settings.paths.data_directory / _CALENDAR_FILE)\n    broker = build_production_xtquant_gateway(\n        settings=settings,\n        database=writer.database,\n        clock=clock,\n    )\n    try:\n        xtdata = importlib.import_module("xtquant.xtdata")\n    except (ImportError, ModuleNotFoundError) as error:\n        raise ProductionServicesUnavailable("XTQUANT_HISTORY_API_UNAVAILABLE") from error\n    data_updater = XtQuantDailyDataUpdater(\n        root=settings.paths.data_directory,\n        provider=OfficialXtQuantDailyHistoryProvider(\n            xtdata=xtdata,\n            volume_multipliers=safety.volume_multipliers,\n        ),\n    )\n    observed_now = clock()\n    if observed_now.tzinfo is None or observed_now.utcoffset() is None:\n        raise ProductionServicesUnavailable("PRODUCTION_CLOCK_INVALID")\n    universe = UniversePolicy.from_uquant(\n        configured_symbols=None,\n        as_of=observed_now.astimezone(_SHANGHAI).date(),\n    )\n    account_repository = RuntimeAccountRepository(\n        database=writer.database,\n        path=settings.paths.state_directory / _ACCOUNT_FILE,\n        clock=clock,\n    )\n    strategy = StrategyAdapter(\n        engine=_load_engine(source_checkout, settings.paths.data_directory),\n        database=writer.database,\n        source_checkout=source_checkout,\n        universe_policy=universe,\n    )\n''',
    '''    calendar = load_trading_calendar_manifest(settings.paths.data_directory / _CALENDAR_FILE)\n    broker = build_production_xtquant_gateway(\n        settings=settings,\n        database=writer.database,\n        clock=clock,\n    )\n    observed_now = clock()\n    if observed_now.tzinfo is None or observed_now.utcoffset() is None:\n        raise ProductionServicesUnavailable("PRODUCTION_CLOCK_INVALID")\n    generations = DataGenerationStore(settings.paths.state_directory)\n    active_data = generations.ensure_active(\n        settings.paths.data_directory,\n        source="configured-production-data",\n        created_at=observed_now,\n    )\n    try:\n        xtdata = importlib.import_module("xtquant.xtdata")\n    except (ImportError, ModuleNotFoundError) as error:\n        raise ProductionServicesUnavailable("XTQUANT_HISTORY_API_UNAVAILABLE") from error\n    data_updater = XtQuantDailyDataUpdater(\n        root=active_data.path,\n        state_root=settings.paths.state_directory / "market-data",\n        provider=OfficialXtQuantDailyHistoryProvider(\n            xtdata=xtdata,\n            volume_multipliers=safety.volume_multipliers,\n            instrument_lookup=broker.query_instrument,\n        ),\n        clock=clock,\n        required_complete_symbols=frozenset(_REFERENCE_SYMBOLS),\n        generation_store=generations,\n    )\n    universe = UniversePolicy.from_uquant(\n        configured_symbols=None,\n        as_of=observed_now.astimezone(_SHANGHAI).date(),\n    )\n    account_repository = RuntimeAccountRepository(\n        database=writer.database,\n        path=settings.paths.state_directory / _ACCOUNT_FILE,\n        clock=clock,\n    )\n    engine = _load_engine(source_checkout, active_data.path)\n    strategy = StrategyAdapter(\n        engine=engine,\n        database=writer.database,\n        source_checkout=source_checkout,\n        universe_policy=universe,\n    )\n\n    def reload_data(path: Path) -> None:\n        try:\n            module = importlib.import_module("uquant.data")\n            factory = vars(module).get("DataStore")\n            if not callable(factory):\n                raise TypeError\n            replacement = factory(path)\n            setattr(engine, "data", replacement)\n        except Exception as error:\n            raise ProductionServicesUnavailable("UQUANT_DATA_RELOAD_FAILED") from error\n''',
)
replace_once(
    "src/firmquant/application/production_services.py",
    '''        safety_manifest=safety,\n        clock=clock,\n        monotonic_clock=monotonic_clock,\n    )\n''',
    '''        safety_manifest=safety,\n        clock=clock,\n        monotonic_clock=monotonic_clock,\n        data_generation_store=generations,\n        data_root=active_data.path,\n        data_reloader=reload_data,\n    )\n''',
)
replace_once(
    "src/firmquant/application/production_services.py",
    '''    monotonic_clock = time_module.monotonic\n    monotonic_clock = time_module.monotonic\n    monotonic_clock = time_module.monotonic\n''',
    '''    monotonic_clock = time_module.monotonic\n''',
)

# Reports distinguish legal zero intent from a missing frozen decision.
replace_once(
    "src/firmquant/observability/reports.py",
    '''    runtime_state: str\n    health_blockers: tuple[str, ...]\n''',
    '''    runtime_state: str\n    health_blockers: tuple[str, ...]\n    intent_state: str = "INTENT"\n''',
)
replace_once(
    "src/firmquant/observability/reports.py",
    '''        _text(self.runtime_state, label="report runtime state")\n''',
    '''        _text(self.runtime_state, label="report runtime state")\n        if self.intent_state not in {"INTENT", "NO_INTENT", "MISSING_DECISION"}:\n            raise ReportError("report intent state is invalid")\n''',
)
replace_once(
    "src/firmquant/observability/reports.py",
    '''            "decision_id": self.decision_id,\n            "funds": {\n''',
    '''            "decision_id": self.decision_id,\n            "intent_state": self.intent_state,\n            "funds": {\n''',
)
replace_once(
    "src/firmquant/observability/reports.py",
    '''            f"- 决策 ID: `{report.decision_id}`",\n            f"- 运行状态: `{report.runtime_state}`",\n''',
    '''            f"- 决策 ID: `{report.decision_id}`",\n            f"- 意图状态: `{report.intent_state}`",\n            f"- 运行状态: `{report.runtime_state}`",\n''',
)
replace_once(
    "src/firmquant/observability/reports.py",
    '''        ) = self._decision_targets(decision_row)\n        snapshot = self._database.query_one(\n''',
    '''        ) = self._decision_targets(decision_row)\n        if decision_row is None:\n            intent_state = "MISSING_DECISION"\n        else:\n            decision_payload = _parse_json_object(decision_row["payload_json"], label="decision payload")\n            upstream_payload = _json_object(\n                decision_payload.get("uquant_payload"), label="uquant decision payload"\n            )\n            raw_orders = _json_array(upstream_payload.get("orders"), label="uquant decision orders")\n            intent_state = "NO_INTENT" if not raw_orders else "INTENT"\n        snapshot = self._database.query_one(\n''',
)
replace_once(
    "src/firmquant/observability/reports.py",
    '''            runtime_state=runtime_state,\n            health_blockers=health_blockers,\n        )\n''',
    '''            runtime_state=runtime_state,\n            health_blockers=health_blockers,\n            intent_state=intent_state,\n        )\n''',
)

# Existing acceptance test must now model the unified close call, not the removed two-step ordering.
replace_once(
    "tests/unit/application/test_production_services_acceptance.py",
    '''        monkeypatch.setattr(hooks, "_eod", lambda _session: 1)\n        monkeypatch.setattr(hooks, "_post_close_decision", lambda _session: 1)\n        result = hooks.cycle(POST_CLOSE)\n''',
    '''        monkeypatch.setattr(hooks, "_close_session", lambda _session: (1, 1))\n        result = hooks.cycle(POST_CLOSE)\n''',
)
