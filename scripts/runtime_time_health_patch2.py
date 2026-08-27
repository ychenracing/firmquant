from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def write(path: str, text: str) -> None:
    (ROOT / path).write_text(text, encoding="utf-8")


def once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count == 0 and new in text:
        return text
    if count != 1:
        raise RuntimeError(f"{label}: expected one fragment, found {count}")
    return text.replace(old, new, 1)


def patch_capability() -> None:
    path = "src/firmquant/risk/capability.py"
    text = read(path)
    text = once(
        text,
        "from firmquant.domain.values import Symbol\n",
        "from firmquant.domain.values import Symbol\nfrom firmquant.scheduling.clock import ClockReceipt\n",
        "capability clock import",
    )
    text = once(
        text,
        "    frequency_within_limits: bool\n\n    def __post_init__(self) -> None:\n",
        "    frequency_within_limits: bool\n    clock_receipt: ClockReceipt | None = None\n\n    def __post_init__(self) -> None:\n",
        "capability clock field",
    )
    anchor = "        if self.gate_decision is not None and not isinstance(self.gate_decision, GateDecision):\n            raise DomainTypeError(\"write context gate decision must be GateDecision or null\")\n"
    insert = anchor + "        if self.clock_receipt is not None and not isinstance(self.clock_receipt, ClockReceipt):\n            raise DomainTypeError(\"write context clock receipt must be ClockReceipt or null\")\n"
    text = once(text, anchor, insert, "capability receipt validation")
    anchor = "        if not context.startup_reconciliation_passed:\n            reasons.append(\"STARTUP_RECONCILIATION_REQUIRED\")\n"
    insert = anchor + '''        if operation in {WriteOperation.SUBMIT, WriteOperation.CANCEL}:
            receipt = context.clock_receipt
            if receipt is None:
                reasons.append("CLOCK_DRIFT_UNVERIFIED")
            else:
                maximum_drift_ms = context.settings.execution.max_clock_drift_seconds * 1000
                if receipt.drift_milliseconds > maximum_drift_ms:
                    reasons.append("CLOCK_DRIFT_LIMIT")
                if receipt.system_time > context.now:
                    reasons.append("CLOCK_RECEIPT_TIME_IN_FUTURE")
                elif context.now - receipt.system_time > context.max_quote_age:
                    reasons.append("CLOCK_RECEIPT_STALE")
'''
    text = once(text, anchor, insert, "capability clock authorization")
    write(path, text)


def patch_production_services() -> None:
    path = "src/firmquant/application/production_services.py"
    text = read(path)
    text = once(text, "from dataclasses import dataclass\n", "from dataclasses import dataclass, replace\n", "services replace import")
    text = once(
        text,
        "from firmquant.execution.live_controller import ExecutionWindowPolicy, LiveExecutionController\n",
        "from firmquant.execution.live_controller import (\n    ExecutionDeadlines,\n    ExecutionWindowPolicy,\n    LiveExecutionController,\n)\n",
        "services controller imports",
    )
    text = once(
        text,
        "from firmquant.persistence.writer_lease import WriterLease\n",
        "from firmquant.persistence.writer_lease import WriterLease, WriterLeaseGuard, WriterLeaseLost\n",
        "services lease imports",
    )
    text = once(
        text,
        "from firmquant.scheduling.sessions import WorkflowReceiptStore\n",
        "from firmquant.scheduling.clock import ClockGuard, ClockObservation, ClockReceipt\nfrom firmquant.scheduling.sessions import WorkflowReceiptStore\n",
        "services clock imports",
    )
    text = once(
        text,
        "    planned: Mapping[str, PlannedOrder]\n",
        "    planned: Mapping[str, PlannedOrder]\n    reconciliation: ReconciliationReceipt\n",
        "execution authority reconciliation",
    )
    text = once(
        text,
        "        safety_manifest: XtQuantSafetyManifest,\n        clock: Callable[[], datetime],\n    ) -> None:\n",
        "        safety_manifest: XtQuantSafetyManifest,\n        clock: Callable[[], datetime],\n        monotonic_clock: Callable[[], float] = time_module.monotonic,\n    ) -> None:\n",
        "services monotonic constructor",
    )
    text = once(
        text,
        "        self._clock = clock\n        self._snapshots = BrokerSnapshotStore(self._database)\n",
        "        self._clock = clock\n        self._monotonic_clock = monotonic_clock\n        self._last_monotonic: float | None = None\n        self._disconnect_started_monotonic: float | None = None\n        self._last_quote_at: datetime | None = None\n        self._snapshots = BrokerSnapshotStore(self._database)\n",
        "services runtime observations",
    )
    anchor = '''    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ProductionServicesUnavailable("PRODUCTION_CLOCK_INVALID")
        return value
'''
    insert = anchor + '''
    def _monotonic(self) -> float:
        value = self._monotonic_clock()
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ProductionServicesUnavailable("MONOTONIC_CLOCK_INVALID")
        observed = float(value)
        if observed < 0 or (self._last_monotonic is not None and observed < self._last_monotonic):
            raise ProductionServicesUnavailable("MONOTONIC_CLOCK_ROLLBACK")
        self._last_monotonic = observed
        return observed

    def _clock_receipt(self, symbol: Symbol) -> ClockReceipt:
        system_time = self._now()
        quote = self._broker.query_quote(symbol)
        self._last_quote_at = quote.received_at
        try:
            return ClockGuard(
                max_drift=timedelta(seconds=self._settings.execution.max_clock_drift_seconds)
            ).verify(
                ClockObservation(
                    system_time=system_time,
                    reference_time=quote.event_time,
                    local_timezone=self._settings.timezone,
                )
            )
        except Exception as error:
            raise ProductionServicesUnavailable("CLOCK_DRIFT_UNVERIFIED") from error

    def _disconnect_duration(self, *, connected: bool) -> timedelta:
        observed = self._monotonic()
        if connected:
            self._disconnect_started_monotonic = None
            return timedelta(seconds=observed - observed)
        if self._disconnect_started_monotonic is None:
            self._disconnect_started_monotonic = observed
        return timedelta(seconds=observed - self._disconnect_started_monotonic)

    def _existing_order_age(self, now: datetime) -> timedelta | None:
        row = self._database.query_one(
            "SELECT min(created_at) AS created_at FROM execution_intents WHERE state IN "
            "('SUBMITTING','ACKNOWLEDGED','PARTIALLY_FILLED','CANCEL_REQUESTED','UNKNOWN')"
        )
        if row is None or row["created_at"] is None:
            return None
        created_at = datetime.fromisoformat(str(row["created_at"]))
        if created_at.tzinfo is None or created_at.utcoffset() is None or created_at > now:
            raise ProductionServicesUnavailable("EXISTING_ORDER_TIME_INVALID")
        return now - created_at

    def _execution_deadlines(self, now: datetime) -> ExecutionDeadlines | None:
        shanghai = now.astimezone(_SHANGHAI)
        completion_wall = datetime.combine(
            shanghai.date(), time(14, 59, 50), tzinfo=_SHANGHAI
        )
        cancel_wall = completion_wall - timedelta(seconds=30)
        max_window = timedelta(
            seconds=max(
                self._settings.execution.sell_window_seconds,
                self._settings.execution.buy_window_seconds,
            )
        )
        submit_wall = cancel_wall - max_window
        if shanghai >= submit_wall:
            return None
        monotonic_now = self._monotonic()
        return ExecutionDeadlines(
            latest_new_submit=monotonic_now + (submit_wall - shanghai).total_seconds(),
            latest_cancel_initiation=monotonic_now + (cancel_wall - shanghai).total_seconds(),
            absolute_completion=monotonic_now + (completion_wall - shanghai).total_seconds(),
        )
'''
    text = once(text, anchor, insert, "services time helpers")

    old = '''        def final_facts(account: AccountStateContract) -> ReconciliationFacts:
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
'''
    new = '''        def final_facts(account: AccountStateContract) -> ReconciliationFacts:
            identity = StrategyIdentity.locked()
            payload = _account_payload(account)
            strategy_view = _strategy_view(account, snapshot.positions, self._accounts)
            broker_positions = {item.symbol: item for item in snapshot.positions}
            strategy_positions = {item.symbol: item for item in strategy_view.positions}
            suspected = frozenset(
                symbol
                for symbol in set(broker_positions) | set(strategy_positions)
                if (
                    (0 if broker_positions.get(symbol) is None else broker_positions[symbol].total_shares.value)
                    != (0 if strategy_positions.get(symbol) is None else strategy_positions[symbol].total_shares.value)
                    or (
                        0
                        if broker_positions.get(symbol) is None
                        else broker_positions[symbol].sellable_shares.value
                    )
                    != (
                        0
                        if strategy_positions.get(symbol) is None
                        else strategy_positions[symbol].sellable_shares.value
                    )
                )
            )
            return ReconciliationFacts(
                broker_snapshot=snapshot,
                strategy_account=strategy_view,
                operational_ledger=operational,
                company_action_suspected_symbols=suspected,
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
'''
    text = once(text, old, new, "services reconciliation suspicion")

    old = '''        health = self._broker.health()
        account_state = self._accounts.load()
        actual_gross = sum((item.market_value.value for item in positions), Decimal(0))
        symbol_notional = Decimal(0) if position is None else position.market_value.value
        return ExecutionRiskContext(
'''
    new = '''        health = self._broker.health()
        observed_now = self._now()
        clock_receipt = self._clock_receipt(command.symbol)
        existing_order_age = self._existing_order_age(observed_now)
        disconnect_duration = self._disconnect_duration(connected=health.connected)
        reconciliation = authorities.reconciliation
        account_state = self._accounts.load()
        actual_gross = sum((item.market_value.value for item in positions), Decimal(0))
        symbol_notional = Decimal(0) if position is None else position.market_value.value
        return ExecutionRiskContext(
'''
    text = once(text, old, new, "services risk derived facts")
    text = text.replace("            now=self._now(),\n", "            now=observed_now,\n", 1)
    text = once(
        text,
        '''            broker_connected=health.connected,
            disconnect_duration=timedelta(0)
            if health.connected
            else limits.max_disconnect_duration + timedelta(seconds=1),
            existing_order_age=None,
            replacement_count=0,
''',
        '''            broker_connected=health.connected,
            disconnect_duration=disconnect_duration,
            existing_order_age=existing_order_age,
''',
        "services disconnect and order age",
    )
    text = once(
        text,
        '''            reconciliation_healthy=self._latest_passed_reconciliation(ReconciliationKind.INTRADAY) != "",
            external_active_order_count=self._external_order_count(),
            unexplained_position_change=False,
            corporate_action_suspected=False,
            clock_drift=timedelta(0),
''',
        '''            reconciliation_healthy=reconciliation.passed,
            external_active_order_count=self._external_order_count(),
            unexplained_position_change=("UNEXPLAINED_POSITION_CHANGE" in reconciliation.blockers),
            corporate_action_suspected=("CORPORATE_ACTION_SUSPECTED" in reconciliation.blockers),
            clock_drift=timedelta(milliseconds=clock_receipt.drift_milliseconds),
''',
        "services reconciliation and clock facts",
    )

    text = once(
        text,
        '''            quote_time = snapshot.captured_at
            symbol_allowed = True
''',
        '''            quote_time = snapshot.captured_at
            clock_receipt: ClockReceipt | None = None
            symbol_allowed = True
''',
        "services auth clock variable",
    )
    text = once(
        text,
        '''                    quote_time = self._broker.query_quote(subject.symbol).received_at
                    symbol_allowed = self._universe.allowed(
''',
        '''                    quote = self._broker.query_quote(subject.symbol)
                    self._last_quote_at = quote.received_at
                    quote_time = quote.received_at
                    clock_receipt = self._clock_receipt(subject.symbol)
                    symbol_allowed = self._universe.allowed(
''',
        "services submit clock receipt",
    )
    text = once(
        text,
        '''                    quote_time = self._broker.query_quote(broker_order.symbol).received_at
            known = self._known_client_ids()
''',
        '''                    quote = self._broker.query_quote(broker_order.symbol)
                    self._last_quote_at = quote.received_at
                    quote_time = quote.received_at
                    clock_receipt = self._clock_receipt(broker_order.symbol)
            known = self._known_client_ids()
''',
        "services cancel clock receipt",
    )
    text = once(
        text,
        '''                reconciliation_mismatch=False,
                external_activity_detected=external,
''',
        '''                reconciliation_mismatch=not authorities.reconciliation.passed,
                external_activity_detected=external,
''',
        "services auth reconciliation fact",
    )
    text = once(
        text,
        '''                cash_and_positions_safe=True,
                frequency_within_limits=(
''',
        '''                cash_and_positions_safe=(
                    authorities.reconciliation.passed
                    and snapshot.account.available_cash.value >= 0
                    and all(item.sellable_shares.value <= item.total_shares.value for item in snapshot.positions)
                ),
                frequency_within_limits=(
''',
        "services cash position fact",
    )
    text = once(
        text,
        '''                    and attempts < self._settings.execution.max_cancel_count_window
                ),
            )
''',
        '''                    and attempts < self._settings.execution.max_cancel_count_window
                ),
                clock_receipt=clock_receipt,
            )
''',
        "services auth receipt field",
    )

    text = once(
        text,
        '''        authorities = _ExecutionAuthorities(
            plan=plan,
            facts=facts,
            decision=decision,
            planned={item.uquant_order_id: item for item in plan.orders},
        )
        controller = LiveExecutionController(
''',
        '''        authorities = _ExecutionAuthorities(
            plan=plan,
            facts=facts,
            decision=decision,
            planned={item.uquant_order_id: item for item in plan.orders},
            reconciliation=reconciliation,
        )
        deadlines = self._active_execution_deadlines
        if deadlines is None:
            raise ProductionServicesUnavailable("EXECUTION_DEADLINE_UNAVAILABLE")
        controller = LiveExecutionController(
''',
        "services authority deadline",
    )
    text = once(
        text,
        '''            window_policy=ExecutionWindowPolicy(
                sell_window=timedelta(seconds=self._settings.execution.sell_window_seconds),
                buy_window=timedelta(seconds=self._settings.execution.buy_window_seconds),
                minimum_order_lifetime=timedelta(seconds=self._settings.execution.min_order_lifetime_seconds),
                poll_interval=timedelta(seconds=self._settings.execution.poll_interval_seconds),
            ),
        )
''',
        '''            window_policy=ExecutionWindowPolicy(
                sell_window=timedelta(seconds=self._settings.execution.sell_window_seconds),
                buy_window=timedelta(seconds=self._settings.execution.buy_window_seconds),
                minimum_order_lifetime=timedelta(seconds=self._settings.execution.min_order_lifetime_seconds),
                poll_interval=timedelta(seconds=self._settings.execution.poll_interval_seconds),
            ),
            lease_guard=WriterLeaseGuard(
                self._writer,
                monotonic_clock=self._monotonic_clock,
                renew_interval=timedelta(seconds=10),
            ),
            monotonic_clock=self._monotonic_clock,
            execution_deadlines=deadlines,
            sleep=time_module.sleep,
        )
''',
        "services controller guard deadline",
    )
    text = once(
        text,
        "        self._real_order_calls = 0\n",
        "        self._real_order_calls = 0\n        self._active_execution_deadlines: ExecutionDeadlines | None = None\n",
        "services active deadline state",
    )

    old = '''        if market_status is MarketSessionStatus.OPEN:
            self._transition(RuntimeState.EXECUTING, reason="next-session execution")
            try:
                executions = self._execute(session)
            except Exception:
                self.halt("EXECUTION_STEP_FAILED")
                raise
            self._transition(RuntimeState.READY, reason="execution step completed")
'''
    new = '''        if market_status is MarketSessionStatus.OPEN:
            deadlines = self._execution_deadlines(shanghai)
            if deadlines is None:
                return ProductionCycleResult(0, 0, 0)
            self._active_execution_deadlines = deadlines
            self._transition(RuntimeState.EXECUTING, reason="next-session execution")
            try:
                executions = self._execute(session)
            except WriterLeaseLost:
                raise
            except Exception:
                self.halt("EXECUTION_STEP_FAILED")
                raise
            finally:
                self._active_execution_deadlines = None
            self._transition(RuntimeState.READY, reason="execution step completed")
'''
    text = once(text, old, new, "services safe execution window")

    old = '''    def heartbeat(self, heartbeat: ProductionHeartbeat) -> None:
        if not isinstance(heartbeat, ProductionHeartbeat):
            raise TypeError("production heartbeat must be typed")
'''
    new = '''    def heartbeat(self, heartbeat: ProductionHeartbeat) -> None:
        if not isinstance(heartbeat, ProductionHeartbeat):
            raise TypeError("production heartbeat must be typed")
        health = self._broker.health()
        last_broker_event = self._database.scalar("SELECT max(recorded_at) FROM broker_events")
        last_reconciliation = self._database.scalar(
            "SELECT max(completed_at) FROM reconciliation_runs WHERE completed_at IS NOT NULL"
        )
        last_decision = self._database.scalar("SELECT max(created_at) FROM decision_snapshots")
        last_execution = self._database.scalar(
            "SELECT max(created_at) FROM audit_events WHERE category = 'PRODUCTION_EXECUTION'"
        )
        enriched = replace(
            heartbeat,
            runtime_state=self._status.state,
            broker_connected=health.connected,
            broker_read_healthy=health.read_healthy,
            broker_write_healthy=health.write_healthy,
            last_broker_event=None if last_broker_event is None else datetime.fromisoformat(str(last_broker_event)),
            last_quote=self._last_quote_at,
            last_reconciliation=(
                None if last_reconciliation is None else datetime.fromisoformat(str(last_reconciliation))
            ),
            last_decision=None if last_decision is None else datetime.fromisoformat(str(last_decision)),
            last_execution=(
                None if last_execution is None else datetime.fromisoformat(str(last_execution))
            ),
        )
        with self._database.transaction():
            self._database.write(
                """
                INSERT INTO production_heartbeat(
                    singleton_id,mode,runtime_state,observed_at,host_hash,process_id,writer_generation,
                    broker_connected,broker_read_healthy,broker_write_healthy,pending_events,
                    last_broker_event,last_quote,last_reconciliation,last_decision,last_execution,
                    control_request_state,processed_events,decisions,executions,eod
                ) VALUES (1,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(singleton_id) DO UPDATE SET
                    mode=excluded.mode,runtime_state=excluded.runtime_state,observed_at=excluded.observed_at,
                    host_hash=excluded.host_hash,process_id=excluded.process_id,
                    writer_generation=excluded.writer_generation,broker_connected=excluded.broker_connected,
                    broker_read_healthy=excluded.broker_read_healthy,
                    broker_write_healthy=excluded.broker_write_healthy,pending_events=excluded.pending_events,
                    last_broker_event=excluded.last_broker_event,last_quote=excluded.last_quote,
                    last_reconciliation=excluded.last_reconciliation,last_decision=excluded.last_decision,
                    last_execution=excluded.last_execution,control_request_state=excluded.control_request_state,
                    processed_events=excluded.processed_events,decisions=excluded.decisions,
                    executions=excluded.executions,eod=excluded.eod
                """,
                (
                    enriched.mode.value,enriched.runtime_state.value,enriched.observed_at.isoformat(),
                    enriched.host_hash,enriched.process_id,enriched.writer_generation,
                    int(enriched.broker_connected),int(enriched.broker_read_healthy),
                    int(enriched.broker_write_healthy),enriched.pending_events,
                    None if enriched.last_broker_event is None else enriched.last_broker_event.isoformat(),
                    None if enriched.last_quote is None else enriched.last_quote.isoformat(),
                    None if enriched.last_reconciliation is None else enriched.last_reconciliation.isoformat(),
                    None if enriched.last_decision is None else enriched.last_decision.isoformat(),
                    None if enriched.last_execution is None else enriched.last_execution.isoformat(),
                    enriched.control_request_state,enriched.processed_events,enriched.decisions,
                    enriched.executions,enriched.eod,
                ),
            )
'''
    text = once(text, old, new, "services heartbeat persistence")

    text = once(
        text,
        '''    hooks = ProductionServiceHooks(
        config_path=config_path.resolve(),
''',
        '''    monotonic_clock = time_module.monotonic
    hooks = ProductionServiceHooks(
        config_path=config_path.resolve(),
''',
        "services shared monotonic",
    )
    text = once(
        text,
        '''        safety_manifest=safety,
        clock=clock,
    )
''',
        '''        safety_manifest=safety,
        clock=clock,
        monotonic_clock=monotonic_clock,
    )
''',
        "services hook monotonic argument",
    )
    text = once(
        text,
        '''        clock=clock,
        sleep=time_module.sleep,
''',
        '''        clock=clock,
        monotonic_clock=monotonic_clock,
        sleep=time_module.sleep,
''',
        "services daemon monotonic argument",
    )
    write(path, text)


def patch_daemon_threshold() -> None:
    path = "src/firmquant/application/production_daemon.py"
    text = read(path)
    text = text.replace("            max_observation_gap=timedelta(minutes=15),\n", "            max_observation_gap=timedelta(seconds=30),\n")
    write(path, text)


def main() -> None:
    patch_capability()
    patch_production_services()
    patch_daemon_threshold()


if __name__ == "__main__":
    main()
