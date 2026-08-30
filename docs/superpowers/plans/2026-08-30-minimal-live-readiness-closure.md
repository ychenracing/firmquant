# Personal LIVE Readiness Closure Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the remaining single-account production correctness, recovery, causal Replay, evidence, local supervision, and LIVE-readiness gaps without adding platform complexity.

**Architecture:** Extend the existing modular monolith with canonical identity values, append-only authority/mode epochs, schema-v3 backup/restore, a fixed causal Replay-acceptance service, and authenticated daemon-owned ARM requests. Reuse the current single SQLite writer, account bootstrap stages, control inbox, reconciliation, and uquant anti-corruption adapter; historical evidence remains append-only but is never upgraded into current authority by default.

**Tech Stack:** Python 3.12, Pydantic 2, SQLite STRICT tables and checksummed migrations, Decimal/integer economics, pytest/Hypothesis, Ruff, mypy, PowerShell/Task Scheduler, GitHub Actions.

**Spec:** `docs/superpowers/specs/2026-08-30-minimal-live-readiness-closure-design.md`

## Global Constraints

- `UQUANT_TARGET_SHA` is exactly `a17322f6330953a27c77f70d463a713c9a48ebc9`; do not re-resolve `main`.
- Single user, one A-share cash account, one Windows host, one daemon, one SQLite ledger, one WriterLease, one writer.
- uquant is the sole strategy/economic owner; firmquant can only block, shrink, delay, cancel, reconcile, recover, audit, or HALT.
- PAPER remains default; REPLAY/PAPER/SHADOW/CI cannot reach real submit/cancel.
- No automatic promotion, ARM, rebaseline, liquidation, catch-up execution, or absorption of external activity.
- UNKNOWN, identity drift, stale/missing evidence, deadline expiry, or payload conflict fail closed.
- Do not modify `ychenracing/uquant`, duplicate its strategy/risk/allocator/AccountState lifecycle, or add web/API/queue/multi-account infrastructure.
- No reset, clean, rebase, force push, history rewrite, direct main push, destructive migration, or loss of incident/unknown work.
- Production safety limits are ceilings/floors: quote `<=5s`, clock `<=2s`, disconnect `<=30s`, deviation `<=200bps`, equity change `<=0.10`, intraday loss `<=0.08`, drawdown `<=0.25`, SHADOW sessions/orders `>=20/50`, tracking error `<=0.05`, CANARY sessions/orders/fills `>=3/3/1`, CANARY tracking error `<=0.05`, ARM TTL `<=900s`.
- Fixed Replay acceptance is `continuous_ai_era`, `2023-01-03` through `2026-08-05`, with normal and `restart_each_session` exact equality and every threshold in the design spec.
- Real MiniQMT/Windows/account/CANARY/LIVE validation remains explicitly unverified when unavailable and must continue to block actual authorization.

---

### Task 1: Lock and validate the target uquant public contract

**Files:**
- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Modify: `src/firmquant/resources/source_identity.json`
- Modify: `src/firmquant/build_identity.py`
- Modify: `src/firmquant/strategy/adapter.py`
- Modify: `scripts/build_reproducible_wheels.py`
- Modify: `scripts/verify_source_baseline.py`
- Modify: `.github/workflows/ci.yml`
- Modify: `.github/workflows/windows.yml`
- Modify: `docs/SOURCE_BASELINE.md`
- Modify: `docs/STRATEGY_INTEGRATION.md`
- Test: `tests/unit/test_build_identity.py`
- Test: `tests/unit/test_build_identity_fail_closed.py`
- Test: `tests/unit/strategy/test_identity.py`
- Test: `tests/integration/test_uquant_parity.py`
- Test: `tests/integration/test_uquant_account_sync.py`

**Interfaces:**
- Consumes: clean detached checkout at `a17322f6330953a27c77f70d463a713c9a48ebc9`.
- Produces: an updated `SourceIdentity`; `verify_uquant_source_checkout(identity, root)` that rejects an attached branch; adapter/public-contract/source/wheel parity; old schema-5 AccountState remains fail-closed and explicitly requires rebaseline.

- [ ] **Step 1: Write failing detached-checkout and private-seam tests**

```python
def test_source_checkout_must_be_detached(target_checkout: Path, identity: SourceIdentity) -> None:
    subprocess.run(["git", "switch", "-c", "attached"], cwd=target_checkout, check=True)
    with pytest.raises(SourceIdentityError, match="detached"):
        verify_uquant_source_checkout(identity, target_checkout)


def test_adapter_operates_through_public_engine_surface(adapter_factory, real_engine, request) -> None:
    class PublicEngineFacade:
        __module__ = "uquant.engine"

        def __init__(self, engine) -> None:
            self.cfg = engine.cfg
            self.data = engine.data
            self._engine = engine

        def __getattribute__(self, name: str):
            if name == "_code_hash":
                raise AssertionError("private engine state was accessed")
            return object.__getattribute__(self, name)

        def decide(self, *, symbols, as_of, account):
            return self._engine.decide(symbols=symbols, as_of=as_of, account=account)

    result = adapter_factory(PublicEngineFacade(real_engine)).decide_once(request)
    assert result.uquant_decision_digest
```

- [ ] **Step 2: Run the focused tests and observe the expected failures**

Run: `uv run pytest tests/unit/test_build_identity.py tests/unit/test_build_identity_fail_closed.py tests/integration/test_uquant_parity.py -q`

Expected: the attached checkout is accepted and the public facade raises because the adapter touches `_code_hash`.

- [ ] **Step 3: Enforce detached source and adapt only through target public APIs**

```python
symbolic_head = _git(repository_root, "symbolic-ref", "-q", "HEAD")
if symbolic_head:
    raise SourceIdentityError("uquant source checkout must use detached HEAD")
```

Remove private `_code_hash` mutation. Build the target engine through its reviewed public constructor/codec only; if the public contract cannot express the required identity, fail with a stable upstream-contract blocker rather than reproducing upstream code.

- [ ] **Step 4: Update the exact dependency and regenerate machine values from the detached target**

Run:

```bash
git clone --no-checkout https://github.com/ychenracing/uquant.git .uquant-target
git -C .uquant-target checkout --detach a17322f6330953a27c77f70d463a713c9a48ebc9
git -C .uquant-target status --short
uv lock
uv sync --frozen --extra dev
uv run python scripts/build_reproducible_wheels.py --source-root .uquant-target --verify-twice --output-dir dist/uquant
uv run python scripts/verify_source_baseline.py --source-root .uquant-target --wheel dist/uquant/uquant-*.whl
```

Record only measured commit/tree/file/wheel/member/package/config/universe digests. Do not commit the checkout or wheel.

- [ ] **Step 5: Add source/wheel/public-contract/adapter parity and schema-upgrade boundary tests**

```python
def test_old_account_requires_reviewed_rebaseline(target_runtime: RuntimeAccount) -> None:
    with pytest.raises(AccountStateError, match="schema"):
        target_runtime.load(strict=True, allow_legacy_schema=False)
```

The parity test loads the target checked-in public API contract, executes the same public decision/fill/account trace from source and installed wheel, and compares canonical results exactly.

- [ ] **Step 6: Verify Task 1**

Run the focused identity/parity/account suites, `uv sync --frozen --extra dev`, source verification, and double wheel build. Then run Ruff and mypy on changed Python files.

- [ ] **Step 7: Commit the coherent upstream-lock checkpoint**

```bash
git add pyproject.toml uv.lock src/firmquant/resources/source_identity.json src/firmquant/build_identity.py src/firmquant/strategy/adapter.py scripts .github/workflows docs tests
git commit -m "build: lock target uquant public contract"
```

---

### Task 2: Canonical deployment identity and non-relaxable safety policy

**Files:**
- Modify: `src/firmquant/application/production_identity.py`
- Create: `src/firmquant/risk/production_policy.py`
- Modify: `src/firmquant/config.py`
- Modify: `src/firmquant/application/execution_evidence.py`
- Modify: `src/firmquant/application/composition.py`
- Test: `tests/unit/application/test_production_identity.py`
- Test: `tests/unit/application/test_production_identity_branches.py`
- Create: `tests/unit/risk/test_production_policy.py`
- Modify: `tests/unit/test_config.py`

**Interfaces:**
- Consumes: updated Task 1 `SourceIdentity`, validated `Settings`, current account/mode epoch values.
- Produces: `ProductionSafetyPolicy.from_settings(settings)`, `DeploymentIdentity`, `OperationalEvidenceIdentity`, canonical payloads and SHA-256 values used by every later receipt.

- [ ] **Step 1: Write failing policy-relaxation tests**

```python
@pytest.mark.parametrize(
    ("path", "value"),
    [
        ("execution.max_quote_age_seconds", 6),
        ("execution.max_clock_drift_seconds", 3),
        ("execution.max_disconnect_seconds", 31),
        ("execution.max_price_deviation_bps", "201"),
        ("promotion.min_shadow_sessions", 19),
        ("promotion.min_canary_fills", 0),
    ],
)
def test_production_policy_rejects_relaxation(settings_payload, path, value) -> None:
    assign(settings_payload, path, value)
    with pytest.raises(ConfigurationError, match="PRODUCTION_SAFETY_POLICY"):
        Settings.model_validate(settings_payload)
```

- [ ] **Step 2: Write failing identity distinction and canonicalization tests**

```python
def test_deployment_identity_is_stable_across_account_state_changes(base_identity) -> None:
    first = OperationalEvidenceIdentity(deployment=base_identity, account_state_sha256="1" * 64, **facts)
    second = replace(first, account_state_sha256="2" * 64)
    assert first.deployment_identity_sha256 == second.deployment_identity_sha256
    assert first.sha256 != second.sha256


def test_identity_rejects_noncanonical_json() -> None:
    with pytest.raises(IdentityError):
        parse_identity('{"schema":"x","schema":"y"}')
```

- [ ] **Step 3: Observe RED**

Run: `uv run pytest tests/unit/application/test_production_identity.py tests/unit/application/test_production_identity_branches.py tests/unit/risk/test_production_policy.py tests/unit/test_config.py -q`

- [ ] **Step 4: Implement the pure policy and canonical identities**

```python
@dataclass(frozen=True, slots=True)
class DeploymentIdentity:
    firmquant_commit: str
    uquant_commit: str
    uquant_tree: str
    uquant_package_manifest_sha256: str
    uquant_code_fingerprint: str
    uquant_config_fingerprint: str
    semantic_config_sha256: str
    raw_config_sha256: str
    xtquant_safety_manifest_sha256: str
    account_id_hash: str
    account_authority_epoch: int
    mode_epoch: int
    mode: Mode
    caps_sha256: str
    production_policy_sha256: str


@dataclass(frozen=True, slots=True)
class OperationalEvidenceIdentity:
    deployment_identity: DeploymentIdentity
    account_state_sha256: str
    broker_snapshot_id: str
    broker_snapshot_sha256: str
    broker_event_watermark: int
    snapshot_started_at: datetime
    snapshot_completed_at: datetime
    snapshot_duration_ms: int
    calendar_sha256: str
    active_data_generation_sha256: str
    strategy_data_manifest_sha256: str
    strategy_session: date
    decision_id: str | None
    phase: str
    kind: str
```

`semantic_config_sha256` hashes normalized non-secret semantic values plus the policy and caps. `raw_config_sha256` retains exact file identity. Replace `EvidenceIdentity.stable_payload` aggregation keys with stable deployment/authority/mode/stage identity while each observation stores the complete operational identity.

- [ ] **Step 5: Verify identities and policy**

Run focused tests plus `uv run ruff check` and `uv run mypy` on changed paths.

- [ ] **Step 6: Commit**

```bash
git add src/firmquant/application/production_identity.py src/firmquant/application/execution_evidence.py src/firmquant/application/composition.py src/firmquant/risk/production_policy.py src/firmquant/config.py tests
git commit -m "feat: canonicalize deployment safety identity"
```

---

### Task 3: Add append-only authority, mode, snapshot, and receipt schema

**Files:**
- Modify: `src/firmquant/persistence/schema.py`
- Create: `src/firmquant/persistence/operational_authority.py`
- Modify: `src/firmquant/persistence/broker_snapshot_store.py`
- Test: `tests/integration/test_migrations.py`
- Create: `tests/unit/persistence/test_operational_authority.py`
- Modify: `tests/unit/persistence/test_account_authority_schema_migration.py`
- Modify: `tests/unit/persistence/test_broker_snapshot_store.py`

**Interfaces:**
- Consumes: Task 2 identity payloads.
- Produces: epoch-1 migration of legacy binding/mode; `OperationalAuthorityStore`; nullable historical snapshot timing; append-only replay/identity/operation tables for later tasks.

- [ ] **Step 1: Write migration RED tests**

```python
def test_migration_seeds_legacy_binding_and_mode_as_epoch_one(v4_database: Database) -> None:
    apply_migrations(v4_database)
    assert row(v4_database, "SELECT epoch FROM account_authority_active") == (1,)
    assert row(v4_database, "SELECT epoch FROM mode_epoch_active") == (1,)
    assert scalar(v4_database, "SELECT COUNT(*) FROM account_authority_epochs") == 1
    assert scalar(v4_database, "SELECT COUNT(*) FROM mode_epochs") == 1


def test_legacy_snapshot_timing_is_not_current(v4_database: Database) -> None:
    apply_migrations(v4_database)
    snapshot = SnapshotStore(v4_database).latest()
    assert snapshot.started_at is None
    assert snapshot.completed_at is None
```

- [ ] **Step 2: Observe RED**

Run: `uv run pytest tests/integration/test_migrations.py tests/unit/persistence/test_account_authority_schema_migration.py tests/unit/persistence/test_broker_snapshot_store.py -q`

- [ ] **Step 3: Add one contiguous checksummed migration**

The migration creates STRICT append-only epoch/identity/receipt tables, singleton active-pointer tables, staged operation tables, and nullable snapshot timing columns. Representative keys are:

```sql
CREATE TABLE account_authority_epochs (
    epoch INTEGER PRIMARY KEY CHECK (epoch > 0),
    account_id_hash TEXT NOT NULL CHECK (length(account_id_hash) = 64),
    account_state_sha256 TEXT NOT NULL CHECK (length(account_state_sha256) = 64),
    deployment_identity_sha256 TEXT NOT NULL CHECK (length(deployment_identity_sha256) = 64),
    payload_json TEXT NOT NULL CHECK (json_valid(payload_json)),
    payload_sha256 TEXT NOT NULL UNIQUE CHECK (length(payload_sha256) = 64),
    created_at TEXT NOT NULL
) STRICT;

CREATE TABLE account_authority_active (
    singleton_id INTEGER PRIMARY KEY CHECK (singleton_id = 1),
    epoch INTEGER NOT NULL REFERENCES account_authority_epochs(epoch)
) STRICT;
```

Use equivalent mode epoch/history, identity, replay receipt, and operation tables. Extend append-only triggers. Seed epoch 1 transactionally without rewriting the legacy row.

- [ ] **Step 4: Implement typed repositories and collision rules**

Implement exact typed methods `active_account_epoch() -> AccountAuthorityEpoch`,
`active_mode_epoch() -> ModeEpoch`,
`prepare_rebaseline(operation: RebaselineOperation) -> RebaselineOperation`,
and `prepare_transition(operation: ModeTransitionOperation) -> ModeTransitionOperation`.

Same operation id/payload returns the stored row. Same id/different payload records or raises a contradiction without overwriting history.

- [ ] **Step 5: Verify migration and repository behavior**

Run migration from a current-main database twice, unknown/future/checksum negatives, epoch seed, append-only, collision, and snapshot compatibility tests.

- [ ] **Step 6: Commit**

```bash
git add src/firmquant/persistence/schema.py src/firmquant/persistence/operational_authority.py src/firmquant/persistence/broker_snapshot_store.py tests
git commit -m "feat: persist operational authority epochs"
```

---

### Task 4: Implement complete backup v3 and empty-directory restore

**Files:**
- Modify: `src/firmquant/persistence/backup.py`
- Modify: `src/firmquant/application/production_services.py`
- Modify: `src/firmquant/application/operations.py`
- Modify: `src/firmquant/cli.py`
- Modify: `src/firmquant/persistence/schema.py`
- Test: `tests/unit/persistence/test_backup_preflight_coverage.py`
- Test: `tests/integration/test_backup.py`
- Test: `tests/integration/test_complete_backup_bundle.py`
- Test: `tests/integration/test_cli_operations.py`
- Test: `tests/fault/test_storage_failures.py`

**Interfaces:**
- Consumes: Task 2 identities and Task 3 epochs/staged receipt storage.
- Produces: `BackupReason`, schema-v3 create/verify, v1/v2 compatibility verification, `restore_backup(bundle, destination) -> RestoreReceipt`, and durable publish recovery.

- [ ] **Step 1: Write v3 contract and legacy-compatibility RED tests**

```python
@pytest.mark.parametrize("reason", list(BackupReason))
def test_v3_bundle_cross_binds_reason_epochs_and_identities(reason, complete_inputs) -> None:
    receipt = backup_state(
        database,
        backup_root,
        account_state_path=account_state_path,
        complete_inputs=replace(complete_inputs, reason=reason),
    )
    verified = verify_backup(receipt.bundle_path)
    assert verified.schema_version == 3
    assert verified.reason is reason
    assert verified.deployment_identity_sha256 == complete_inputs.deployment_identity.sha256


def test_schema_v2_bundle_remains_verifiable_after_current_migration(v2_bundle: Path) -> None:
    assert verify_backup(v2_bundle).schema_version == 2
```

- [ ] **Step 2: Write restore safety RED tests**

```python
def test_restore_forces_disarmed_and_revokes_authority(v3_bundle, empty_destination) -> None:
    receipt = restore_backup(v3_bundle, empty_destination)
    restored = Database.open(empty_destination / "state.sqlite3")
    assert runtime_state(restored).state is RuntimeState.DISARMED
    assert scalar(restored, "SELECT COUNT(*) FROM arm_leases WHERE revoked_at IS NULL") == 0
    assert scalar(restored, "SELECT COUNT(*) FROM writer_leases") == 0
    assert receipt.requires_fresh_snapshot is True
    assert receipt.requires_reconciliation is True
```

Cover non-empty/symlink/current-production destinations, member corruption,
wrong identity, interrupted fsync/publish, repeated restore, stale writer,
active arm, and zero submit/cancel calls.

- [ ] **Step 3: Observe RED**

Run focused backup/CLI/fault suites and confirm schema 3/restore are absent.

- [ ] **Step 4: Implement v3 and non-migrating legacy verification**

```python
class BackupReason(StrEnum):
    SESSION_CLOSE = "SESSION_CLOSE"
    MODE_TRANSITION = "MODE_TRANSITION"
    ACCOUNT_REBASELINE = "ACCOUNT_REBASELINE"


def restore_backup(bundle: Path, destination: Path) -> RestoreReceipt:
    """Verify, stage, sanitize, fsync, and atomically publish a DISARMED restore."""
```

Validate v1/v2 database schema from `schema_migrations` without calling the
normal mutating `Database.open()` migration path. V3 publication has durable
PREPARED/PUBLISHED/RECEIPT_COMMITTED identity and recovers idempotently.

- [ ] **Step 5: Wire producers and CLI**

EOD uses `SESSION_CLOSE`. Add
`restore-backup --bundle C:\\FirmQuant\\Backups\\backup-id --destination C:\\FirmQuantRestored`
with stable JSON output and recovery instructions. Do not make generic backup
silently produce an incomplete production-v3 bundle.

- [ ] **Step 6: Verify Task 4**

Run backup/migration/CLI/fault suites, compile, Ruff, and mypy.

- [ ] **Step 7: Commit**

```bash
git add src/firmquant/persistence/backup.py src/firmquant/persistence/schema.py src/firmquant/application/production_services.py src/firmquant/application/operations.py src/firmquant/cli.py tests
git commit -m "feat: add recoverable backup v3 restore"
```

---

### Task 5: Add account rebaseline and transactional mode epochs

**Files:**
- Modify: `src/firmquant/persistence/operational_authority.py`
- Create: `src/firmquant/strategy/account_rebaseline.py`
- Create: `src/firmquant/application/mode_transition.py`
- Modify: `src/firmquant/application/operations.py`
- Modify: `src/firmquant/cli.py`
- Modify: `src/firmquant/application/composition.py`
- Test: `tests/unit/strategy/test_account_rebaseline.py`
- Test: `tests/fault/test_account_rebaseline_recovery.py`
- Test: `tests/unit/application/test_mode_transition.py`
- Test: `tests/fault/test_mode_transition_recovery.py`
- Modify: `tests/integration/test_cli_operations.py`

**Interfaces:**
- Consumes: Task 4 verified reason-bound backup; existing bootstrap prepare/file/commit pattern; Task 3 active epochs.
- Produces: `rebaseline-account`, `transition-mode`, immutable receipts, monotonic epochs, and old-evidence invalidation.

- [ ] **Step 1: Write rebaseline prerequisite and crash RED tests**

```python
def test_rebaseline_requires_verified_backup_before_prepare(service, reviewed_state) -> None:
    with pytest.raises(RebaselineDenied, match="ACCOUNT_REBASELINE_BACKUP_MISSING"):
        service.rebaseline(reviewed_state, reason=RebaselineReason.MANUAL_BROKER_ACTIVITY)


@pytest.mark.parametrize("fault", ["after_prepare", "after_file", "before_receipt"])
def test_rebaseline_recovers_idempotently(fault, harness) -> None:
    operation_id = harness.fail_once(fault)
    result = harness.retry(operation_id)
    assert result.epoch == 2
    assert harness.active_epoch_count() == 1
```

- [ ] **Step 2: Write mode-edge and crash RED tests**

```python
@pytest.mark.parametrize(
    ("source", "target", "allowed"),
    [
        (Mode.PAPER, Mode.SHADOW, True),
        (Mode.SHADOW, Mode.LIVE, False),
        (Mode.SHADOW, Mode.CANARY, True),
        (Mode.CANARY, Mode.LIVE, True),
    ],
)
def test_mode_edges(source, target, allowed, transition_service) -> None:
    assert transition_service.edge_allowed(source, target) is allowed
```

Also prove target config mismatch, armed runtime, active/unresolved orders,
UNKNOWN, missing promotion/Replay/backup evidence, and every crash boundary
fail closed. Every successful transition finishes DISARMED and revokes old
authority.

- [ ] **Step 3: Observe RED**

Run the new tests and CLI surface tests; confirm the commands do not exist.

- [ ] **Step 4: Implement rebaseline by reusing strict bootstrap seams**

Implement `AccountRebaselineService.execute()` with keyword-only
`account_state_path: Path`, `reviewed_evidence_path: Path`,
`reason: RebaselineReason`, and `operation_id: str`, returning the committed
`AccountAuthorityEpoch`.

Scan active broker and operation states before PREPARED; never infer uquant
state from the snapshot. Store only reviewed-evidence digest, not sensitive
source contents.

- [ ] **Step 5: Implement transactional mode transition**

```python
_PROMOTION_EDGES = {
    (Mode.PAPER, Mode.SHADOW),
    (Mode.SHADOW, Mode.CANARY),
    (Mode.CANARY, Mode.LIVE),
}
```

The operator prepares target config; `--to` must equal its mode. The service
does not edit config. It verifies `MODE_TRANSITION` backup, evidence gates,
and reconciliation, then commits epoch/pointer/receipt/audit atomically.

- [ ] **Step 6: Verify Task 5**

Run focused unit, fault, migration, CLI, account bootstrap, reconciliation,
and runtime-state suites.

- [ ] **Step 7: Commit**

```bash
git add src/firmquant/persistence/operational_authority.py src/firmquant/strategy/account_rebaseline.py src/firmquant/application/mode_transition.py src/firmquant/application/operations.py src/firmquant/application/composition.py src/firmquant/cli.py tests
git commit -m "feat: add authority rebaseline and mode epochs"
```

---

### Task 6: Add bounded snapshot timing and trustworthy evidence

**Files:**
- Modify: `src/firmquant/domain/broker_facts.py`
- Modify: `src/firmquant/broker/gateway.py`
- Modify: `src/firmquant/broker/production_snapshot.py`
- Modify: `src/firmquant/broker/xtquant_production.py`
- Modify: `src/firmquant/persistence/broker_snapshot_store.py`
- Modify: `src/firmquant/application/production_services.py`
- Test: `tests/unit/broker/test_production_snapshot.py`
- Test: `tests/unit/persistence/test_broker_snapshot_store.py`
- Test: `tests/contract/test_xtquant_adapter.py`

**Interfaces:**
- Consumes: Task 3 nullable persisted timing fields.
- Produces: complete `BrokerSnapshot` timing/watermark identity with query and total deadlines, while legacy snapshots remain loadable but never current-ready.

- [ ] **Step 1: Write timing/deadline RED tests**

```python
def test_slow_first_query_is_stale_even_when_completion_is_recent(collector, clock) -> None:
    clock.advance_monotonic(seconds=31)
    snapshot = collector.collect(total_deadline_seconds=30)
    with pytest.raises(SnapshotCollectionError, match="SNAPSHOT_DEADLINE_EXCEEDED"):
        snapshot


def test_snapshot_persists_started_completed_duration_and_watermark(store, snapshot) -> None:
    store.append(snapshot)
    loaded = store.load(snapshot.snapshot_id)
    assert loaded.started_at == snapshot.started_at
    assert loaded.completed_at == snapshot.completed_at
    assert loaded.duration_ms == snapshot.duration_ms
```

- [ ] **Step 2: Observe RED**

Run the snapshot/store/adapter contract tests.

- [ ] **Step 3: Extend the snapshot contract and collector**

```python
@dataclass(frozen=True, slots=True)
class BrokerSnapshot:
    # existing fields
    started_at: datetime
    completed_at: datetime
    duration_ms: int
```

Each broker query receives a bounded deadline/timeout through the existing
synchronous port. Use monotonic time for duration and aware UTC for receipts.
Reject total deadline, changed account identity, or changed lifecycle
signature across the two reads. Do not add threads or asyncio.

- [ ] **Step 4: Verify Task 6**

Run unit, persistence, XtQuant contract, snapshot reconciliation, Ruff, and
mypy tests.

- [ ] **Step 5: Commit**

```bash
git add src/firmquant/domain/broker_facts.py src/firmquant/broker src/firmquant/persistence/broker_snapshot_store.py src/firmquant/application/production_services.py tests
git commit -m "feat: bound broker snapshot freshness"
```

---

### Task 7: Make execution Replay strictly causal and economically complete

**Files:**
- Modify: `src/firmquant/execution/execution_replay.py`
- Modify: `src/firmquant/execution/replay_runner.py`
- Modify: `src/firmquant/execution/replay_metrics.py`
- Modify: `src/firmquant/market_data/generations.py`
- Modify: `src/firmquant/market_data/validation.py`
- Modify: `tests/unit/execution/test_execution_replay.py`
- Modify: `tests/unit/execution/test_execution_replay_branches.py`
- Modify: `tests/unit/execution/test_adjusted_price_limits.py`
- Modify: `tests/unit/execution/test_replay_metrics.py`
- Modify: `tests/integration/test_execution_replay_restart.py`
- Modify: `tests/properties/test_replay_determinism.py`

**Interfaces:**
- Consumes: locked Task 1 public uquant account sync; frozen data/listing manifest.
- Produces: `OpenExecutionFacts`, identity-preserving orders/fills, causal capacity/limit/listing behavior, lot/T+1/cash/fee/slippage invariants, and complete deterministic summaries.

- [ ] **Step 1: Write future-data mutation RED tests**

```python
@given(high=prices(), low=prices(), close=prices(), current_volume=share_counts())
def test_future_bar_fields_cannot_change_open_fill(
    order: ReplayOrder, open_facts: OpenExecutionFacts, high, low, close, current_volume
) -> None:
    first = execute_open(order, open_facts)
    second = execute_open(order, replace(open_facts, diagnostic_eod=(high, low, close, current_volume)))
    assert first == second
```

Write direct cases proving an open-unreachable limit is no-fill even if later
high/low crosses it, and capacity uses previous volume.

- [ ] **Step 2: Write listing/limit/lot/cash/slippage RED tests**

```python
def test_qfq_without_authoritative_limit_fact_blocks_fill() -> None:
    assert execute_open(order, facts(limit_fact=None)).blocker is BlockerCode.LIMIT_FACT_UNAVAILABLE


def test_sell_odd_lot_only_when_liquidating_entire_remainder() -> None:
    assert execute_open(sell(50), account(position=150, sellable=150)).filled_shares == 0
    assert execute_open(sell(150), account(position=150, sellable=150)).filled_shares == 150


def test_directional_adverse_slippage() -> None:
    assert adverse_slippage(ReplaySide.BUY, Decimal("10.02"), Decimal("10"), 100) == Decimal("2")
    assert adverse_slippage(ReplaySide.SELL, Decimal("9.98"), Decimal("10"), 100) == Decimal("2")
```

- [ ] **Step 3: Observe RED**

Run unit/property/restart suites and verify existing high/low, current-volume,
OHLC-expanded limit, row-derived listing, sell-lot, and identity behavior fails.

- [ ] **Step 4: Separate open facts from EOD marks**

```python
@dataclass(frozen=True, slots=True)
class OpenExecutionFacts:
    session: date
    symbol: str
    open: Decimal
    previous_close: Decimal
    previous_volume: int
    suspended: bool
    limit_up: Decimal | None
    limit_down: Decimal | None
    listing_session_number: int | None
```

`execute_session` receives only open facts for fills. EOD close exists only in
mark-to-market metrics after execution. Do not retain an execution path that
can access current high/low/close/volume.

- [ ] **Step 5: Preserve order/fill identity and complete economics**

Add `execution_id` and `uquant_order_id` to `ReplayOrder` and result payloads;
map results by identity, never `(symbol, side)`. Record no-fill/partial/limit-
unavailable/suspension/volume/cash/incomplete-sell counters, signed improvement,
theoretical MDD, session economic digest, final cash/positions/AccountState
hash, and order/fill identity digests.

- [ ] **Step 6: Verify no second AccountState**

After every session, continue to call the public account-sync contract and
strictly load the resulting uquant AccountState. Add a test that restart and
normal use the same serialized public state rather than reconstructing local
strategy fields.

- [ ] **Step 7: Verify Task 7**

Run all execution Replay unit/property/restart/account-sync suites and the
future-data negative tests. Then Ruff/mypy changed paths.

- [ ] **Step 8: Commit**

```bash
git add src/firmquant/execution src/firmquant/market_data tests/unit/execution tests/integration/test_execution_replay_restart.py tests/properties/test_replay_determinism.py
git commit -m "fix: make execution replay strictly causal"
```

---

### Task 8: Add fixed Replay acceptance and immutable receipt

**Files:**
- Create: `src/firmquant/execution/replay_acceptance.py`
- Create: `src/firmquant/persistence/replay_acceptance_store.py`
- Modify: `src/firmquant/application/operations.py`
- Modify: `src/firmquant/cli.py`
- Modify: `src/firmquant/application/composition.py`
- Modify: `src/firmquant/persistence/schema.py`
- Create: `tests/unit/execution/test_replay_acceptance.py`
- Create: `tests/unit/persistence/test_replay_acceptance_store.py`
- Create: `tests/integration/test_replay_acceptance.py`
- Modify: `tests/integration/test_cli_operations.py`

**Interfaces:**
- Consumes: Task 7 complete Replay summaries and Task 2 identity/policy.
- Produces: fixed `ReplayAcceptancePolicy`, two-mode runner, per-gate result, append-only tamper-evident receipt, and exact current-receipt validator.

- [ ] **Step 1: Write literal-policy RED tests**

```python
def test_policy_is_fixed_and_not_configurable() -> None:
    policy = ReplayAcceptancePolicy.production()
    assert policy.start == date(2023, 1, 3)
    assert policy.end == date(2026, 8, 5)
    assert policy.minimum_twr_ratio == Decimal("0.75")
    assert policy.maximum_mdd_degradation == Decimal("0.05")
    assert policy.maximum_tracking_error == Decimal("0.05")
    assert policy.maximum_unfilled_loss_ratio == Decimal("0.10")
```

- [ ] **Step 2: Write exact two-mode and receipt-collision RED tests**

```python
def test_normal_and_restart_must_match_every_economic_identity(harness) -> None:
    result = evaluate_production_replay(harness)
    assert result.normal.economic_sha256 == result.restart_each_session.economic_sha256


def test_same_identity_different_result_conflicts(store, passed_receipt) -> None:
    store.append(passed_receipt)
    with pytest.raises(ReplayAcceptanceConflict):
        store.append(replace(passed_receipt, normal_summary_sha256="f" * 64))
```

- [ ] **Step 3: Observe RED**

Run new acceptance/store/CLI tests; confirm no service or receipt exists.

- [ ] **Step 4: Implement policy evaluation and immutable payload**

```python
@dataclass(frozen=True, slots=True)
class ReplayAcceptanceReceipt:
    deployment_identity_sha256: str
    uquant_commit: str
    semantic_config_sha256: str
    policy_sha256: str
    universe_sha256: str
    frozen_data_manifest_sha256: str
    normal_summary_sha256: str
    restart_summary_sha256: str
    gates: Sequence[ReplayAcceptanceGate]
    generated_at: datetime
    payload_sha256: str
```

All gates use actual Decimal/string/integer values in canonical JSON. Same
identity/result is idempotent. Any content/hash mismatch or missing mode is a
hard failure. The CLI production acceptance command does not accept alternate
dates or looser thresholds.

- [ ] **Step 5: Run the fixed Replay both ways**

Use the detached target source and frozen production data. If a threshold
fails, retain exact failure evidence; do not adjust uquant, universe, data,
policy, or threshold.

- [ ] **Step 6: Verify Task 8**

Run unit/store/integration/CLI tests, normal and restart fixed-window runs,
receipt tamper/idempotency checks, Ruff, and mypy.

- [ ] **Step 7: Commit**

```bash
git add src/firmquant/execution/replay_acceptance.py src/firmquant/persistence/replay_acceptance_store.py src/firmquant/application/operations.py src/firmquant/application/composition.py src/firmquant/cli.py src/firmquant/persistence/schema.py tests
git commit -m "feat: gate production on replay acceptance"
```

---

### Task 9: Rebuild phase-aware, identity-bound LIVE readiness

**Files:**
- Modify: `src/firmquant/application/readiness.py`
- Modify: `src/firmquant/application/live_readiness_runtime.py`
- Modify: `src/firmquant/reconciliation/service.py`
- Modify: `src/firmquant/reconciliation/live_view.py`
- Modify: `src/firmquant/broker/production_smoke.py`
- Modify: `src/firmquant/scheduling/clock.py`
- Modify: `src/firmquant/market_data/calendar_manifest.py`
- Test: `tests/unit/application/test_live_readiness.py`
- Test: `tests/unit/application/test_readiness_and_canary_runtime.py`
- Test: `tests/unit/reconciliation/test_service.py`
- Test: `tests/unit/reconciliation/test_live_view.py`
- Test: `tests/unit/broker/test_production_smoke.py`

**Interfaces:**
- Consumes: Tasks 2-8 current identities, epochs, snapshot timing, v3 backup, and Replay PASS receipt.
- Produces: read-only `collect_live_readiness()` with all blockers and phase selection; correct fill/order identity semantics; `armed` remains informational only.

- [ ] **Step 1: Write wrong-identity/epoch/snapshot/backup RED tests**

```python
@pytest.mark.parametrize(
    "mutation",
    ["authority_epoch", "mode_epoch", "deployment", "account_state", "snapshot", "calendar", "data"],
)
def test_readiness_rejects_stale_identity_component(runtime, mutation) -> None:
    facts = runtime.collect(mutate=mutation)
    assert facts.passed is False
    assert any("MISMATCH" in blocker or "STALE" in blocker for blocker in facts.blockers)


def test_backup_compares_account_state_not_account_id(runtime) -> None:
    assert runtime.with_matching_account_state_backup().collect().verified_backup is True
```

- [ ] **Step 2: Write phase/clock/calendar/smoke/fill RED tests**

```python
@pytest.mark.parametrize(
    ("phase", "required"),
    [("PREMARKET", {"STARTUP"}), ("INTRADAY", {"STARTUP", "INTRADAY"}), ("POST_CLOSE", {"EOD"})],
)
def test_reconciliation_is_phase_aware(phase, required, runtime) -> None:
    assert runtime.required_reconciliation_kinds(phase) == required


def test_distinct_fill_ids_with_same_economics_are_not_duplicates(runtime) -> None:
    runtime.add_fill("fill-a", economics)
    runtime.add_fill("fill-b", economics)
    assert runtime.collect().no_duplicate_fills is True
```

Also cover same fill id/different content, duplicate economic order identity,
trusted clock receipt expiry, smoke boot/client expiry, next-safe-session
calendar end, snapshot duration, Replay receipt mismatch/tamper, historical
external activity before rebaseline, and current unresolved activity.

- [ ] **Step 3: Observe RED**

Run readiness/reconciliation/smoke/clock/calendar suites.

- [ ] **Step 4: Implement one current evidence assembly**

Replace historical “latest PASS by kind” and global scans with queries bound to
the current operational identity and authority epoch. Reuse authoritative
`broker_fill_id` conflict handling and stable execution/uquant order identity.
Build a phase enum and return every blocker; do not create leases or mutate
state.

- [ ] **Step 5: Verify Task 9**

Run readiness, reconciliation, backup, Replay receipt, snapshot, clock,
calendar, external-activity epoch, and duplicate identity suites.

- [ ] **Step 6: Commit**

```bash
git add src/firmquant/application/readiness.py src/firmquant/application/live_readiness_runtime.py src/firmquant/reconciliation src/firmquant/broker/production_smoke.py src/firmquant/scheduling/clock.py src/firmquant/market_data/calendar_manifest.py tests
git commit -m "fix: bind readiness to current evidence"
```

---

### Task 10: Route authenticated ARM through the online daemon

**Files:**
- Modify: `src/firmquant/application/control_channel.py`
- Modify: `src/firmquant/application/production_daemon.py`
- Modify: `src/firmquant/application/operations.py`
- Modify: `src/firmquant/cli.py`
- Modify: `src/firmquant/risk/arm.py`
- Modify: `src/firmquant/application/runtime_control.py`
- Test: `tests/unit/application/test_runtime_control_channel.py`
- Test: `tests/unit/application/test_runtime_control_executor.py`
- Test: `tests/unit/application/test_production_daemon.py`
- Test: `tests/unit/risk/test_arm.py`
- Modify: `tests/integration/test_writer_lease.py`
- Modify: `tests/integration/test_cli_operations.py`

**Interfaces:**
- Consumes: Task 9 current readiness and Task 2 deployment identity.
- Produces: MAC-authenticated `ControlCommand.ARM`, request-status receipt, daemon-only lease creation under WriterLease, bounded TTL, no auto-resume/promotion/order.

- [ ] **Step 1: Write request authentication and queue RED tests**

```python
def test_online_arm_cli_returns_queued_not_armed(operator, online_daemon) -> None:
    result = operator.arm_live(ttl_seconds=900)
    assert result.code == "QUEUED"
    assert online_daemon.arm_store.active() is None


@pytest.mark.parametrize("mutation", ["mac", "nonce", "host", "mode", "identity", "expiry"])
def test_daemon_rejects_mutated_arm_request(mutation, daemon, arm_request) -> None:
    receipt = daemon.process_control(mutate(arm_request, mutation))
    assert receipt.status is ControlStatus.REJECTED
```

- [ ] **Step 2: Write daemon ownership and TTL RED tests**

```python
def test_only_daemon_writer_creates_arm(offline_operator) -> None:
    with pytest.raises(OperatorCommandDenied, match="ARM_DAEMON_NOT_READY"):
        offline_operator.arm_live()


def test_effective_ttl_is_minimum_of_all_boundaries(daemon, request) -> None:
    lease = daemon.execute_arm(request, session_remaining=120, revalidate_in=60)
    assert lease.expires_at - lease.issued_at == timedelta(seconds=60)
```

- [ ] **Step 3: Observe RED**

Run control channel/daemon/arm/writer/CLI suites; confirm current CLI competes
for WriterLease and no ARM control command exists.

- [ ] **Step 4: Extend the canonical request and MAC**

```python
@dataclass(frozen=True, slots=True)
class ArmControlPayload:
    request_id: str
    created_at: datetime
    expires_at: datetime
    host_hash: str
    requested_mode: Mode
    requested_ttl_seconds: int
    deployment_identity_sha256: str
    nonce: str
    mac: str
```

The MAC covers every canonical field. CLI is local interactive only, refuses
CI, atomically publishes, and never stores the secret/phrase. Request status
reads immutable terminal receipt.

- [ ] **Step 5: Execute ARM in the daemon before submit**

The daemon revalidates host/time/nonce/MAC/mode/identity/idempotency, recollects
readiness, requires READY/no kill/UNKNOWN/external/unresolved operation, and
creates the arm/audit atomically while holding the existing writer. A request
never resumes, promotes, or submits.

- [ ] **Step 6: Verify structural no-write guarantees**

Run PAPER/REPLAY/SHADOW/CI E2E and assert real submit/cancel call count remains
zero with ARM requests present.

- [ ] **Step 7: Commit**

```bash
git add src/firmquant/application/control_channel.py src/firmquant/application/production_daemon.py src/firmquant/application/operations.py src/firmquant/application/runtime_control.py src/firmquant/cli.py src/firmquant/risk/arm.py tests
git commit -m "feat: make live arm daemon owned"
```

---

### Task 11: Add Windows supervision, current documentation, and final integration gates

**Files:**
- Create: `scripts/windows/install_firmquant_task.ps1`
- Create: `scripts/windows/watch_firmquant.ps1`
- Create: `scripts/windows/remove_firmquant_task.ps1`
- Create: `tests/unit/test_windows_supervision_scripts.py`
- Modify: `scripts/windows_smoke.py`
- Modify: `tests/integration/test_windows_smoke.py`
- Modify: `.github/workflows/windows.yml`
- Modify: `README.md`
- Modify: `docs/DEPLOYMENT_WINDOWS.md`
- Modify: `docs/OPERATIONS.md`
- Modify: `docs/RECOVERY.md`
- Modify: `docs/RISK_AND_SAFETY.md`
- Modify: `docs/EXECUTION.md`
- Modify: `docs/STRATEGY_INTEGRATION.md`
- Modify: `docs/QUALITY.md`

**Interfaces:**
- Consumes: completed Tasks 1-10 and stable CLI/status JSON.
- Produces: least-privilege local Task Scheduler/runbook, watchdog exit codes and alerts, no forbidden automation, accurate current docs, and a stable final candidate.

- [ ] **Step 1: Write PowerShell static-contract RED tests**

```python
def test_watchdog_allows_only_status_and_run() -> None:
    script = Path("scripts/windows/watch_firmquant.ps1").read_text(encoding="utf-8")
    assert "status --json" in script
    for forbidden in ("arm-live", "resume", "transition-mode", "rebaseline-account", "submit"):
        assert forbidden not in script


def test_task_does_not_use_highest_privilege_or_store_secrets() -> None:
    script = Path("scripts/windows/install_firmquant_task.ps1").read_text(encoding="utf-8")
    assert "RunLevel Highest" not in script
    assert "ARM_MAC_KEY" not in script
```

- [ ] **Step 2: Observe RED**

Run Windows script/static/smoke tests and confirm scripts are absent.

- [ ] **Step 3: Implement least-privilege Scheduler and watchdog behavior**

The installer requires an explicit dedicated non-admin account and quoted
absolute executable/config/log paths, disables missed-run catch-up, uses a
stable restart policy, and stores no secret/account/userdata. The watchdog
parses status JSON; stale/not-running yields alert plus daemon restart only.
Malformed status fails closed. Restart never arms/resumes/promotes/catches up.

- [ ] **Step 4: Update current-state documentation**

Document exact operator sequences for bootstrap/rebaseline/transition/ARM/
status/backup/restore/Replay acceptance/Scheduler, every fail-closed boundary,
and the distinction between repository evidence and unverified target-machine
work. Remove obsolete v2/readiness/high-low execution claims instead of
appending a version history.

- [ ] **Step 5: Run affected L3 integration**

Run migration/rebaseline/transition/restore faults, PAPER/REPLAY/SHADOW E2E,
restart recovery, fixed Replay normal/restart, receipt validation, Windows-safe
smoke, and explicit real-write-zero tests.

- [ ] **Step 6: Commit the final feature checkpoint**

```bash
git add scripts/windows scripts/windows_smoke.py tests .github/workflows/windows.yml README.md docs
git commit -m "ops: close local Windows live operations"
```

- [ ] **Step 7: Run the stable-candidate L4 once**

```bash
uv sync --frozen --extra dev
uv run ruff check .
uv run ruff format --check .
uv run mypy src scripts
uv run pytest --cov=firmquant --cov-branch --cov-fail-under=85
uv run python -m compileall -q src scripts tests
uv run bandit -q -r src scripts
uv run pip-audit --cache-dir .pytest_cache/pip-audit
uv run python scripts/secret_scan.py
uv run python scripts/verify_source_baseline.py
uv run python scripts/build_reproducible_wheels.py --verify-twice
uv run python scripts/check_docs.py
```

Also run exact source/wheel/public-contract parity, broker contracts, all
account/mode/restore fault suites, fixed Replay both modes, receipt validation,
and Windows-safe tests. Record exact outcomes; skipped real environment work is
not PASS.

- [ ] **Step 8: Focused review, CI, squash merge, and post-merge protection**

Generate one whole-branch review package. Resolve Critical/Important findings
with at most one focused final fix/re-review wave. Refresh PR #11, verify the
remote head, required CI/Security/Windows checks and clean tree, then squash
merge. Verify the resulting `main` SHA and checks. Read actual successful check
contexts and configure the smallest supported PR-only/no-force-push/no-delete
main protection with no mandatory extra reviewer; re-read and verify. If the
API/plan/permission does not support this, record the exact blocker without
claiming protection.

---

## Plan self-review result

- Every approved Task 1-13 requirement maps to Tasks 1-11 above.
- Dependency order prevents readiness/ARM from consuming identities, epochs,
  backups, snapshots, or Replay receipts before they exist.
- No task introduces a second strategy, account state, writer, daemon,
  database, service, or remote-control plane.
- Every production behavior task starts with an observed failing test and ends
  with affected verification plus a coherent checkpoint.
- Exact SHA, dates, thresholds, modes, backup reasons, failure semantics, and
  target-machine limitations are preserved literally.
