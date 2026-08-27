from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "src/firmquant/application/production_services.py"
WORKFLOW = ROOT / ".github/workflows/execution-evidence-wiring.yml"


def replace_once(text: str, old: str, new: str, *, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected exactly one match, found {count}")
    return text.replace(old, new, 1)


def replace_between(text: str, start: str, end: str, new: str, *, label: str) -> str:
    start_index = text.find(start)
    if start_index < 0:
        raise RuntimeError(f"{label}: start marker missing")
    end_index = text.find(end, start_index)
    if end_index < 0:
        raise RuntimeError(f"{label}: end marker missing")
    if text.find(start, start_index + 1) >= 0:
        raise RuntimeError(f"{label}: start marker is not unique")
    return text[:start_index] + new + text[end_index:]


text = TARGET.read_text(encoding="utf-8")
text = replace_once(
    text,
    "from firmquant.application.production_runtime import ProductionRuntime\n"
    "from firmquant.application.promotion import ShadowPromotionEvidence\n"
    "from firmquant.application.promotion_store import PromotionStore\n",
    "from firmquant.application.execution_evidence import EvidenceStage, ExecutionEvidenceStore\n"
    "from firmquant.application.execution_evidence_runtime import (\n"
    "    build_shadow_observation,\n"
    "    finalize_canary_observation,\n"
    "    record_canary_plan,\n"
    ")\n"
    "from firmquant.application.production_runtime import ProductionRuntime\n"
    "from firmquant.application.promotion_store import PromotionStore\n",
    label="imports",
)

promotion_start = "    def _require_promotion(self, account_hash: str) -> None:\n"
promotion_end = "\n    def startup(self) -> str:\n"
promotion = '''    def _require_promotion(self, account_hash: str) -> None:
        if self._settings.mode is Mode.SHADOW:
            return
        thresholds = self._settings.promotion
        store = PromotionStore(self._database)
        if not store.qualifies(
            stage=EvidenceStage.SHADOW,
            firmquant_commit=self._identity.firmquant_commit,
            uquant_commit=self._identity.uquant_commit,
            config_sha256=self._identity.promotion_config_sha256,
            account_hash=account_hash,
            min_sessions=thresholds.min_shadow_sessions,
            min_orders=thresholds.min_shadow_orders,
            max_tracking_error=thresholds.max_target_tracking_error,
        ):
            raise ProductionServicesUnavailable("SHADOW_PROMOTION_EVIDENCE_REQUIRED")
        if self._settings.mode is Mode.CANARY:
            return
        if not store.qualifies(
            stage=EvidenceStage.CANARY,
            firmquant_commit=self._identity.firmquant_commit,
            uquant_commit=self._identity.uquant_commit,
            config_sha256=self._identity.promotion_config_sha256,
            account_hash=account_hash,
            min_sessions=thresholds.min_canary_sessions,
            min_orders=thresholds.min_canary_orders,
            min_fills=thresholds.min_canary_fills,
            max_tracking_error=thresholds.max_canary_target_tracking_error,
        ):
            raise ProductionServicesUnavailable("CANARY_PROMOTION_EVIDENCE_REQUIRED")
'''
text = replace_between(text, promotion_start, promotion_end, promotion, label="promotion gate")

shadow_start = "    def _shadow_execute(self, plan: ExecutionPlan, decision: DecisionSnapshot) -> None:\n"
shadow_end = "\n    def _execute(self, session: date) -> int:\n"
shadow = '''    def _shadow_execute(
        self,
        plan: ExecutionPlan,
        decision: DecisionSnapshot,
        facts: ExecutionBrokerSnapshot,
    ) -> None:
        observation = build_shadow_observation(
            database=self._database,
            broker=self._broker,
            facts=facts,
            plan=plan,
            decision=decision,
            firmquant_commit=self._identity.firmquant_commit,
            uquant_commit=self._identity.uquant_commit,
            promotion_config_sha256=self._identity.promotion_config_sha256,
            calendar_sha256=self._calendar.sha256,
            safety_manifest=self._safety,
            created_at=self._now(),
        )
        ExecutionEvidenceStore(self._database).append(observation)
        self._audit(
            "shadow-execution:" + decision.decision_id + ":" + plan.execution_session.isoformat(),
            "SHADOW_EXECUTION",
            {
                "schema": "firmquant.shadow-execution.v1",
                "decision_id": decision.decision_id,
                "plan_id": plan.plan_id,
                "execution_session": plan.execution_session,
                "hypothetical_order_count": len(plan.orders),
                "blockers": [item.reason_code for item in plan.blockers],
                "observation_sha256": observation.content_sha256,
                "real_order_calls": 0,
            },
        )
'''
text = replace_between(text, shadow_start, shadow_end, shadow, label="shadow execution")
text = replace_once(
    text,
    "            self._shadow_execute(plan, decision)\n",
    "            self._shadow_execute(plan, decision, facts)\n",
    label="shadow call",
)
text = replace_once(
    text,
    "        self._require_promotion(facts.broker_snapshot.account.account_id_hash)\n"
    "        authorities = _ExecutionAuthorities(\n",
    "        self._require_promotion(facts.broker_snapshot.account.account_id_hash)\n"
    "        if self._settings.mode is Mode.CANARY:\n"
    "            record_canary_plan(\n"
    "                database=self._database,\n"
    "                broker=self._broker,\n"
    "                facts=facts,\n"
    "                plan=plan,\n"
    "                decision=decision,\n"
    "                firmquant_commit=self._identity.firmquant_commit,\n"
    "                uquant_commit=self._identity.uquant_commit,\n"
    "                promotion_config_sha256=self._identity.promotion_config_sha256,\n"
    "                calendar_sha256=self._calendar.sha256,\n"
    "                created_at=self._now(),\n"
    "            )\n"
    "        authorities = _ExecutionAuthorities(\n",
    label="canary plan wiring",
)

text = replace_once(
    text,
    "        eod = self._close.load(session, CloseStep.EOD_RECONCILED)\n"
    "        eod_created_now = eod is None\n"
    "        if eod is None:\n"
    "            receipt, snapshot, _ = self._reconcile(ReconciliationKind.EOD)\n",
    "        eod = self._close.load(session, CloseStep.EOD_RECONCILED)\n"
    "        eod_created_now = eod is None\n"
    "        eod_snapshot: BrokerSnapshot | None = None\n"
    "        if eod is None:\n"
    "            receipt, snapshot, _ = self._reconcile(ReconciliationKind.EOD)\n"
    "            eod_snapshot = snapshot\n",
    label="EOD snapshot capture",
)
text = replace_once(
    text,
    "                created_at=self._now(),\n"
    "            )\n\n"
    "        data = self._close.load(session, CloseStep.DATA_VALIDATED)\n",
    "                created_at=self._now(),\n"
    "            )\n\n"
    "        if self._settings.mode is Mode.CANARY:\n"
    "            if eod_snapshot is None:\n"
    "                eod_snapshot = self._capture()\n"
    "            if eod_snapshot.session_date != session:\n"
    "                raise ProductionServicesUnavailable(\"CANARY_EOD_SNAPSHOT_SESSION_MISMATCH\")\n"
    "            canary = finalize_canary_observation(\n"
    "                database=self._database,\n"
    "                eod_snapshot=eod_snapshot,\n"
    "                session=session,\n"
    "                created_at=self._now(),\n"
    "            )\n"
    "            if canary is not None:\n"
    "                ExecutionEvidenceStore(self._database).append(canary)\n\n"
    "        data = self._close.load(session, CloseStep.DATA_VALIDATED)\n",
    label="CANARY EOD observation",
)

heartbeat_start = "    def heartbeat(self, heartbeat: ProductionHeartbeat) -> None:\n"
heartbeat_end = "\n    def halt(self, reason_code: str) -> None:\n"
heartbeat = '''    def heartbeat(self, heartbeat: ProductionHeartbeat) -> None:
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
            last_broker_event=(
                None if last_broker_event is None else datetime.fromisoformat(str(last_broker_event))
            ),
            last_quote=self._last_quote_at,
            last_reconciliation=(
                None if last_reconciliation is None else datetime.fromisoformat(str(last_reconciliation))
            ),
            last_decision=(
                None if last_decision is None else datetime.fromisoformat(str(last_decision))
            ),
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
                    enriched.mode.value,
                    enriched.runtime_state.value,
                    enriched.observed_at.isoformat(),
                    enriched.host_hash,
                    enriched.process_id,
                    enriched.writer_generation,
                    int(enriched.broker_connected),
                    int(enriched.broker_read_healthy),
                    int(enriched.broker_write_healthy),
                    enriched.pending_events,
                    None if enriched.last_broker_event is None else enriched.last_broker_event.isoformat(),
                    None if enriched.last_quote is None else enriched.last_quote.isoformat(),
                    None
                    if enriched.last_reconciliation is None
                    else enriched.last_reconciliation.isoformat(),
                    None if enriched.last_decision is None else enriched.last_decision.isoformat(),
                    None if enriched.last_execution is None else enriched.last_execution.isoformat(),
                    enriched.control_request_state,
                    enriched.processed_events,
                    enriched.decisions,
                    enriched.executions,
                    enriched.eod,
                ),
            )
'''
text = replace_between(text, heartbeat_start, heartbeat_end, heartbeat, label="heartbeat deduplication")

TARGET.write_text(text, encoding="utf-8")
Path(__file__).unlink()
WORKFLOW.unlink()
