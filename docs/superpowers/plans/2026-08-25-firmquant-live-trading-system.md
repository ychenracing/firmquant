# firmquant 实盘自动交易系统 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 构建一个以 uquant 为唯一策略内核、默认绝不触发真实交易、可恢复且可审计的单账户 A 股 AI 日频自动执行系统。

**Architecture:** Python 3.12 模块化单体以 SQLite 单 writer 保存在线事实，通过严格端口连接 uquant、行情和券商。真实券商写操作必须持有短时 arm lease 派生的能力对象；REPLAY、PAPER、SHADOW 与 CI 无法构造该能力。

**Tech Stack:** Python 3.12、uv、Pydantic 2、SQLite、标准库 logging/Decimal/HMAC/zoneinfo、pytest、Hypothesis、Ruff、strict mypy、Bandit、pip-audit、GitHub Actions。

**Spec:** `docs/superpowers/specs/2026-08-25-firmquant-live-trading-system-design.md`

## Global Constraints

- 开始执行时重新读取 `ychenracing/uquant` 最新 `origin/main`；当前核验 commit 为 `105695aacd3d1c7e62705f64188da88d202db4cd`，tree 为 `e3e2832eb1321e6d45f103cab538aeb9c95852d3`。
- uquant 必须通过精确 Git commit 与 `uv.lock` 锁定；不修改上游、不复制其生产内核。
- 唯一支持 Python `>=3.12,<3.13`；默认模式 PAPER，仓库内所有配置均设置 `live_trading_enabled=false`。
- 仅支持单一 A 股现金多头账户、AI canonical universe 的部署子集、日频盘后决策和次日执行。
- Sentinel 保持 `FREEZE_ONLY`；firmquant 只能阻止、缩量、延迟、取消或 HALT，不能扩大 uquant 目标。
- 订单、资金、价格与费用边界使用 Decimal 或整数最小单位；外部 payload 一律视为不可信输入。
- callback 只持久化并入队；单 writer 串行推进订单状态、账户同步和 SQLite 事务。
- XtQuant 只根据合法本机 SDK、官方文档和真实只读返回结构实现；自动化验收禁止真实 submit/cancel。
- 小改动运行 L1，checkpoint 运行相关 L2/L3；稳定最终候选只运行一次 L4。
- 每项任务通过后提交；每个 checkpoint 完成后推送 `codex/firmquant-live-bootstrap`，不 force push。

## Planned File Structure

```text
src/firmquant/
  application/{runtime.py,sessions.py,workflows.py,event_pump.py}
  domain/{errors.py,values.py,states.py,orders.py,broker_facts.py,events.py}
  strategy/{identity.py,universe.py,account_sync.py,snapshots.py,adapter.py}
  broker/{gateway.py,normalization.py,fake.py,paper.py,replay.py,xtquant.py}
  market_data/{gateway.py,validation.py,calendar.py}
  execution/{policy.py,planner.py,controller.py}
  risk/{gate.py,arm.py,capability.py,kill_switch.py}
  reconciliation/{models.py,service.py}
  persistence/{database.py,schema.py,repositories.py,audit.py,writer_lease.py,backup.py,recovery.py}
  scheduling/{clock.py,sessions.py}
  observability/{logging.py,health.py,notifiers.py,reports.py}
  security/{secrets.py,redaction.py}
  config.py
  build_identity.py
  cli.py
  __init__.py
  __main__.py
tests/
  unit/
  contract/
  integration/
  e2e/
  fault/
  properties/
  fixtures/
config/firmquant.example.toml
docs/ and docs/decisions/
scripts/{verify_source_baseline.py,build_reproducible_wheels.py,check_docs.py,secret_scan.py}
```

---

## Checkpoint 1 — 工程、基线和默认安全配置

### Task 1: 初始化 Python 工程与冻结依赖

**Files:**
- Create: `pyproject.toml`
- Create: `src/firmquant/__init__.py`
- Create: `src/firmquant/__main__.py`
- Create: `.python-version`
- Create: `.gitignore`
- Test: `tests/unit/test_package_metadata.py`

**Interfaces:**
- Produces: `firmquant.__version__: str`、`python -m firmquant` 入口、精确 uquant Git dependency。

- [ ] **Step 1: 写失败的包元数据测试**

```python
def test_supported_runtime_and_safe_version() -> None:
    import firmquant
    assert firmquant.__version__ == "0.1.0"
```

- [ ] **Step 2: 验证测试因包不存在而失败**

Run: `python -m pytest tests/unit/test_package_metadata.py -q`
Expected: FAIL with `ModuleNotFoundError: firmquant`.

- [ ] **Step 3: 创建 src-layout、构建元数据和依赖声明**

```toml
[project]
name = "firmquant"
version = "0.1.0"
requires-python = ">=3.12,<3.13"
dependencies = [
  "pydantic>=2.11,<3",
  "tzdata>=2025.2",
  "uquant @ git+https://github.com/ychenracing/uquant.git@105695aacd3d1c7e62705f64188da88d202db4cd",
]
[project.scripts]
firmquant = "firmquant.cli:main"
```

- [ ] **Step 4: 生成锁并运行 L1**

Run: `uv lock && uv sync --frozen --extra dev && uv run pytest tests/unit/test_package_metadata.py -q`
Expected: PASS and `uv.lock` records the exact uquant commit.

- [ ] **Step 5: 提交**

```bash
git add pyproject.toml uv.lock .python-version .gitignore src tests/unit/test_package_metadata.py
git commit -m "build: initialize locked Python project"
```

### Task 2: 固化 uquant 源码、wheel 与 universe 身份

**Files:**
- Create: `src/firmquant/build_identity.py`
- Create: `scripts/build_reproducible_wheels.py`
- Create: `scripts/verify_source_baseline.py`
- Create: `docs/SOURCE_BASELINE.md`
- Test: `tests/unit/test_build_identity.py`

**Interfaces:**
- Produces: `SourceIdentity`, `installed_uquant_identity() -> SourceIdentity`、`verify_uquant_identity(expected) -> None`。

- [ ] **Step 1: 写 commit/tree/wheel/config/universe 摘要校验测试**

```python
def test_source_identity_rejects_wrong_commit(locked_identity: SourceIdentity) -> None:
    bad = replace(locked_identity, uquant_commit="0" * 40)
    with pytest.raises(SourceIdentityError, match="uquant commit"):
        verify_uquant_identity(bad)
```

- [ ] **Step 2: 运行失败测试**

Run: `uv run pytest tests/unit/test_build_identity.py -q`
Expected: FAIL because `firmquant.build_identity` does not exist.

- [ ] **Step 3: 实现不可变身份与确定性 wheel 构建脚本**

```python
@dataclass(frozen=True, slots=True)
class SourceIdentity:
    uquant_commit: str
    uquant_tree: str
    wheel_sha256: str
    code_fingerprint: str
    config_fingerprint: str
    universe_sha256: str
    uv_lock_sha256: str
```

构建脚本在临时目录检出精确 commit，两次以固定 `SOURCE_DATE_EPOCH` 构建 wheel，要求两个
SHA-256 相同；验证脚本从安装包读取 `code_fingerprint()`、`config_fingerprint()` 与
`default_ai_universe().sha256`。

- [ ] **Step 4: 生成并核验 SOURCE_BASELINE**

Run: `uv run python scripts/build_reproducible_wheels.py --verify-twice && uv run python scripts/verify_source_baseline.py`
Expected: two wheel hashes equal and baseline verification exits 0.

- [ ] **Step 5: 提交**

```bash
git add src/firmquant/build_identity.py scripts docs/SOURCE_BASELINE.md tests/unit/test_build_identity.py
git commit -m "build: bind exact uquant source identity"
```

### Task 3: 严格配置、CLI 骨架、AGENTS 与 CI

**Files:**
- Create: `src/firmquant/config.py`
- Create: `src/firmquant/cli.py`
- Create: `config/firmquant.example.toml`
- Create: `AGENTS.md`
- Create: `.github/workflows/ci.yml`
- Create: `.github/workflows/security.yml`
- Test: `tests/unit/test_config.py`
- Test: `tests/unit/test_cli_help.py`

**Interfaces:**
- Produces: `Mode`, `Settings`, `load_settings(path: Path) -> Settings`、`main(argv: Sequence[str] | None = None) -> int`。

- [ ] **Step 1: 写 PAPER 默认与 CANARY 无上限即拒绝的测试**

```python
def test_defaults_cannot_trade_live() -> None:
    settings = Settings()
    assert settings.mode is Mode.PAPER
    assert settings.live_trading_enabled is False

def test_canary_requires_all_deployment_caps() -> None:
    with pytest.raises(ValidationError):
        Settings(mode="CANARY", live_trading_enabled=True)
```

- [ ] **Step 2: 运行配置和 CLI 失败测试**

Run: `uv run pytest tests/unit/test_config.py tests/unit/test_cli_help.py -q`
Expected: FAIL because config and CLI modules do not exist.

- [ ] **Step 3: 实现 frozen Pydantic 配置和 argparse 命令注册**

```python
class Mode(StrEnum):
    REPLAY = "REPLAY"
    PAPER = "PAPER"
    SHADOW = "SHADOW"
    CANARY = "CANARY"
    LIVE = "LIVE"

class Settings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    mode: Mode = Mode.PAPER
    live_trading_enabled: bool = False
```

`safe_repr()` 必须隐藏账户、路径和 secret；示例配置与 CI 仅启用 PAPER/Replay。

- [ ] **Step 4: 运行 Checkpoint 1 验证**

Run: `uv run ruff check . && uv run mypy src && uv run pytest tests/unit -q && uv run python -m firmquant --help`
Expected: all commands exit 0.

- [ ] **Step 5: 提交并推送 Checkpoint 1**

```bash
git add AGENTS.md .github config src/firmquant/config.py src/firmquant/cli.py tests
git commit -m "feat: establish safe configuration and CI baseline"
git push -u origin codex/firmquant-live-bootstrap
```

---

## Checkpoint 2 — 领域状态、SQLite 与审计账本

### Task 4: Decimal 值对象、symbol 与券商事实

**Files:**
- Create: `src/firmquant/domain/errors.py`
- Create: `src/firmquant/domain/values.py`
- Create: `src/firmquant/domain/broker_facts.py`
- Test: `tests/unit/domain/test_values.py`
- Test: `tests/properties/test_decimal_values.py`

**Interfaces:**
- Produces: `Symbol.parse(str)`, `Money`, `Price`, `Shares`, `BrokerSnapshot`, `InstrumentFact`, `QuoteFact`。

- [ ] **Step 1: 写精度、有限值、symbol 规范化和负数拒绝测试**

```python
@pytest.mark.parametrize(("raw", "expected"), [("300308.SZ", "sz300308"), ("SH600000", "sh600000")])
def test_symbol_normalization(raw: str, expected: str) -> None:
    assert Symbol.parse(raw).canonical == expected

def test_price_rejects_float() -> None:
    with pytest.raises(TypeError):
        Price(10.1)  # type: ignore[arg-type]
```

- [ ] **Step 2: 运行失败测试**

Run: `uv run pytest tests/unit/domain/test_values.py tests/properties/test_decimal_values.py -q`
Expected: FAIL because value objects are absent.

- [ ] **Step 3: 实现 Decimal-only frozen dataclass 与不可变事实快照**

```python
@dataclass(frozen=True, slots=True)
class Price:
    value: Decimal
    def __post_init__(self) -> None:
        if not self.value.is_finite() or self.value <= 0:
            raise ValueError("price must be finite and positive")
```

- [ ] **Step 4: 运行 L1 与性质测试**

Run: `uv run pytest tests/unit/domain/test_values.py tests/properties/test_decimal_values.py -q`
Expected: PASS for generated finite/non-finite Decimal cases.

- [ ] **Step 5: 提交**

```bash
git add src/firmquant/domain tests/unit/domain tests/properties/test_decimal_values.py
git commit -m "feat: add strict trading value objects"
```

### Task 5: 运行状态机与耐久订单状态机

**Files:**
- Create: `src/firmquant/domain/states.py`
- Create: `src/firmquant/domain/orders.py`
- Create: `src/firmquant/domain/events.py`
- Test: `tests/unit/domain/test_runtime_state.py`
- Test: `tests/unit/domain/test_order_state.py`
- Test: `tests/properties/test_order_transitions.py`

**Interfaces:**
- Produces: `RuntimeState`, `OrderState`, `ExecutionIntent`, `OrderAggregate.apply(event) -> OrderAggregate`。

- [ ] **Step 1: 写合法迁移、终态回退、迟到成交和重复事件测试**

```python
def test_submit_exception_becomes_unknown_not_rejected(order: OrderAggregate) -> None:
    changed = order.apply(SubmitOutcomeUnknown(event_id="evt-1"))
    assert changed.state is OrderState.UNKNOWN
```

- [ ] **Step 2: 验证测试失败**

Run: `uv run pytest tests/unit/domain tests/properties/test_order_transitions.py -q`
Expected: FAIL because state enums and aggregate are absent.

- [ ] **Step 3: 实现显式迁移表与累计成交不变量**

```python
ORDER_TRANSITIONS: Final = {
    OrderState.PLANNED: frozenset({OrderState.VALIDATED, OrderState.EXPIRED}),
    OrderState.SUBMITTING: frozenset({OrderState.ACKNOWLEDGED, OrderState.UNKNOWN}),
}
```

事件 identity 已应用时返回相同 aggregate；累计成交不得超过 requested shares；终态新成交产生
`LateFillInvestigationRequired`，不丢弃成交事实。

- [ ] **Step 4: 运行 L1 与性质测试**

Run: `uv run pytest tests/unit/domain tests/properties/test_order_transitions.py -q`
Expected: PASS for arbitrary legal and illegal event sequences.

- [ ] **Step 5: 提交**

```bash
git add src/firmquant/domain tests/unit/domain tests/properties/test_order_transitions.py
git commit -m "feat: implement durable runtime and order states"
```

### Task 6: SQLite schema、迁移与事务 repository

**Files:**
- Create: `src/firmquant/persistence/database.py`
- Create: `src/firmquant/persistence/schema.py`
- Create: `src/firmquant/persistence/repositories.py`
- Test: `tests/unit/persistence/test_database.py`
- Test: `tests/integration/test_migrations.py`

**Interfaces:**
- Produces: `Database.open(path)`, `Database.transaction()`, `Repositories`；schema 包含规格列出的全部表。

- [ ] **Step 1: 写 PRAGMA、重复迁移和回滚测试**

```python
def test_database_enables_safety_pragmas(db: Database) -> None:
    assert db.scalar("PRAGMA journal_mode") == "wal"
    assert db.scalar("PRAGMA foreign_keys") == 1
    assert db.scalar("PRAGMA synchronous") == 2
```

- [ ] **Step 2: 运行失败测试**

Run: `uv run pytest tests/unit/persistence/test_database.py tests/integration/test_migrations.py -q`
Expected: FAIL because persistence modules are absent.

- [ ] **Step 3: 实现 schema version、幂等迁移和显式事务**

每个 JSON 列写入 canonical JSON；raw broker event、order command、attempt、response 与 domain event
分别存储；唯一约束覆盖 idempotency key、broker event id 和 fill id。

- [ ] **Step 4: 运行 SQLite L2**

Run: `uv run pytest tests/unit/persistence tests/integration/test_migrations.py -q`
Expected: PASS including rollback after injected exception.

- [ ] **Step 5: 提交**

```bash
git add src/firmquant/persistence tests/unit/persistence tests/integration/test_migrations.py
git commit -m "feat: add transactional operational ledger"
```

### Task 7: Audit hash chain、writer lease 与备份恢复

**Files:**
- Create: `src/firmquant/persistence/audit.py`
- Create: `src/firmquant/persistence/writer_lease.py`
- Create: `src/firmquant/persistence/backup.py`
- Test: `tests/unit/persistence/test_audit.py`
- Test: `tests/integration/test_writer_lease.py`
- Test: `tests/integration/test_backup.py`

**Interfaces:**
- Produces: `AuditLedger.append()`, `AuditLedger.verify()`, `WriterLease.acquire()`, `backup_state()`, `verify_backup()`。

- [ ] **Step 1: 写篡改检测、第二实例和损坏备份测试**

```python
def test_second_writer_is_rejected(db_path: Path) -> None:
    first = WriterLease.acquire(db_path, owner="one")
    with pytest.raises(WriterLeaseBusy):
        WriterLease.acquire(db_path, owner="two")
    first.release()
```

- [ ] **Step 2: 运行失败测试**

Run: `uv run pytest tests/unit/persistence/test_audit.py tests/integration/test_writer_lease.py tests/integration/test_backup.py -q`
Expected: FAIL because audit, lease and backup APIs are absent.

- [ ] **Step 3: 实现 canonical hash chain、续租和 SQLite online backup**

备份写入临时文件，执行 `integrity_check` 与 audit verification 后原子替换；账户文件与 manifest
使用同一 backup receipt 记录 SHA-256。

- [ ] **Step 4: 运行 Checkpoint 2 验证**

Run: `uv run pytest tests/unit/domain tests/unit/persistence tests/integration/test_migrations.py tests/integration/test_writer_lease.py tests/integration/test_backup.py tests/properties/test_order_transitions.py -q`
Expected: PASS.

- [ ] **Step 5: 提交并推送 Checkpoint 2**

```bash
git add src/firmquant/persistence tests
git commit -m "feat: secure ledger ownership and recovery backups"
git push origin codex/firmquant-live-bootstrap
```

---

## Checkpoint 3 — uquant 精确接入与经济等价

### Task 8: uquant 身份与 canonical universe 适配

**Files:**
- Create: `src/firmquant/strategy/identity.py`
- Create: `src/firmquant/strategy/universe.py`
- Test: `tests/unit/strategy/test_identity.py`
- Test: `tests/unit/strategy/test_universe.py`

**Interfaces:**
- Produces: `StrategyIdentity.verify()`, `UniversePolicy.allowed(symbol, as_of)`, `UniversePolicy.manifest_sha256`。

- [ ] **Step 1: 写非 AI、失效成员和 allowlist 扩张拒绝测试**

```python
def test_deployment_allowlist_cannot_expand_canonical_universe() -> None:
    with pytest.raises(UniverseViolation):
        UniversePolicy.from_uquant(("sh600519",), as_of=date(2026, 8, 25))
```

- [ ] **Step 2: 运行失败测试**

Run: `uv run pytest tests/unit/strategy/test_identity.py tests/unit/strategy/test_universe.py -q`
Expected: FAIL because strategy adapters are absent.

- [ ] **Step 3: 仅调用 uquant 公共 contract 实现身份和点时成员检查**

```python
universe = default_ai_universe()
active = frozenset(universe.symbols_as_of(as_of))
if not set(configured_symbols) <= active:
    raise UniverseViolation("deployment universe exceeds canonical AI universe")
```

- [ ] **Step 4: 运行 L1**

Run: `uv run pytest tests/unit/strategy/test_identity.py tests/unit/strategy/test_universe.py -q`
Expected: PASS and manifest equals locked uquant contract.

- [ ] **Step 5: 提交**

```bash
git add src/firmquant/strategy tests/unit/strategy
git commit -m "feat: enforce locked uquant universe identity"
```

### Task 9: BrokerSnapshot 到 uquant account-sync 的 anti-corruption adapter

**Files:**
- Create: `src/firmquant/strategy/account_sync.py`
- Create: `tests/fixtures/broker_snapshots.py`
- Test: `tests/unit/strategy/test_account_sync.py`
- Test: `tests/integration/test_uquant_account_sync.py`

**Interfaces:**
- Consumes: `BrokerSnapshot`, `uquant.types.AccountState`。
- Produces: `to_uquant_broker_payload(snapshot) -> dict[str, object]`, `sync_account(account, snapshot) -> AccountSyncReceipt`。

- [ ] **Step 1: 写现金、持仓、可卖、成交、重复 fill 与未知订单测试**

```python
def test_sync_is_idempotent(account: AccountState, snapshot: BrokerSnapshot) -> None:
    first = sync_account(account, snapshot)
    second = sync_account(account, snapshot)
    assert first.account_after_sha256 == second.account_after_sha256
```

- [ ] **Step 2: 运行失败测试**

Run: `uv run pytest tests/unit/strategy/test_account_sync.py tests/integration/test_uquant_account_sync.py -q`
Expected: FAIL because account sync adapter is absent.

- [ ] **Step 3: 实现 Decimal 到有限 float 的显式转换并调用 `uquant.broker.sync_broker_snapshot`**

adapter 先深拷贝 AccountState，调用上游同步，验证 economic hash 后才替换调用方状态；未知经济
order id、股数不一致或上游校验异常均转换为 fail-closed `StrategySyncError`。

- [ ] **Step 4: 运行 L2**

Run: `uv run pytest tests/unit/strategy/test_account_sync.py tests/integration/test_uquant_account_sync.py -q`
Expected: PASS with exact duplicate-fill idempotency.

- [ ] **Step 5: 提交**

```bash
git add src/firmquant/strategy/account_sync.py tests
git commit -m "feat: adapt broker facts to uquant account sync"
```

### Task 10: 不可变 DecisionSnapshot 与 parity

**Files:**
- Create: `src/firmquant/strategy/snapshots.py`
- Create: `src/firmquant/strategy/adapter.py`
- Create: `tests/fixtures/uquant_parity.py`
- Test: `tests/unit/strategy/test_decision_snapshot.py`
- Test: `tests/integration/test_uquant_parity.py`

**Interfaces:**
- Produces: `StrategyAdapter.decide_once(request) -> DecisionSnapshot`, `DecisionSnapshot.canonical_json()`。

- [ ] **Step 1: 写相同输入复用、输入冲突和直接 uquant 精确输出比较测试**

```python
def test_adapter_matches_direct_engine(parity_case: ParityCase) -> None:
    direct = parity_case.engine.decide(**parity_case.kwargs)
    adapted = parity_case.adapter.decide_once(parity_case.request)
    assert adapted.uquant_payload == direct.canonical_payload(
        effective_config_sha256=config_fingerprint(parity_case.engine.cfg)
    )
```

- [ ] **Step 2: 运行失败测试**

Run: `uv run pytest tests/unit/strategy/test_decision_snapshot.py tests/integration/test_uquant_parity.py -q`
Expected: FAIL because snapshot and adapter are absent.

- [ ] **Step 3: 实现 canonical input/output hash、一次调用事务和 append-only 冲突处理**

`DecisionSnapshot` 固化规格要求的全部字段；同 session 同 input hash 从 repository 返回旧快照；
同 session 不同 input hash 保存 conflict 并抛出 `DecisionConflict`，不再次调用引擎。

- [ ] **Step 4: 运行 Checkpoint 3 验证**

Run: `uv run pytest tests/unit/strategy tests/integration/test_uquant_account_sync.py tests/integration/test_uquant_parity.py -q`
Expected: PASS with exact targets, orders, reason codes, account hashes and fingerprints.

- [ ] **Step 5: 提交并推送 Checkpoint 3**

```bash
git add src/firmquant/strategy tests
git commit -m "feat: preserve exact uquant decision economics"
git push origin codex/firmquant-live-bootstrap
```

---

## Checkpoint 4 — Broker contracts、Paper、Replay 与执行闭环

### Task 11: BrokerGateway、规范化与 callback 队列

**Files:**
- Create: `src/firmquant/broker/gateway.py`
- Create: `src/firmquant/broker/normalization.py`
- Create: `src/firmquant/application/event_pump.py`
- Test: `tests/contract/test_broker_gateway_contract.py`
- Test: `tests/unit/broker/test_normalization.py`

**Interfaces:**
- Produces: `BrokerGateway` 全方法 Protocol、`BrokerEventSink`, `normalize_*()`、`DomainEventPump`。

- [ ] **Step 1: 写完整方法集、未知 enum、无时区时间和 callback 不直写 DB 测试**

```python
def test_gateway_protocol_surface() -> None:
    assert required_methods(BrokerGateway) == {
        "connect", "disconnect", "health", "query_account", "query_positions",
        "query_orders", "query_fills", "query_instrument", "query_quote",
        "query_market_status", "submit_order", "cancel_order", "subscribe",
    }
```

- [ ] **Step 2: 运行失败测试**

Run: `uv run pytest tests/contract/test_broker_gateway_contract.py tests/unit/broker/test_normalization.py -q`
Expected: FAIL because broker protocol is absent.

- [ ] **Step 3: 实现 typed Protocol、严格 normalizer 和 bounded queue**

callback sink 只生成 envelope、保存 raw digest 并 `put_nowait`；队列满时触发 CRITICAL/HALT 标志，
不丢弃事件后继续交易。

- [ ] **Step 4: 运行 contract L1**

Run: `uv run pytest tests/contract/test_broker_gateway_contract.py tests/unit/broker/test_normalization.py -q`
Expected: PASS.

- [ ] **Step 5: 提交**

```bash
git add src/firmquant/broker src/firmquant/application/event_pump.py tests
git commit -m "feat: define untrusted broker boundary"
```

### Task 12: FakeBroker 与 RecordedReplayBroker

**Files:**
- Create: `src/firmquant/broker/fake.py`
- Create: `src/firmquant/broker/replay.py`
- Test: `tests/contract/test_fake_broker.py`
- Test: `tests/contract/test_replay_broker.py`
- Test: `tests/properties/test_replay_determinism.py`

**Interfaces:**
- Produces: `FakeBroker.script(outcomes)`, `RecordedReplayBroker.from_jsonl(path)`。

- [ ] **Step 1: 写拒单、超时、断线、重复、乱序与两次 replay 同摘要测试**

```python
def test_replay_is_deterministic(recording: Path) -> None:
    assert run_replay(recording).state_sha256 == run_replay(recording).state_sha256
```

- [ ] **Step 2: 运行失败测试**

Run: `uv run pytest tests/contract/test_fake_broker.py tests/contract/test_replay_broker.py tests/properties/test_replay_determinism.py -q`
Expected: FAIL because brokers are absent.

- [ ] **Step 3: 实现可编程结果队列与 canonical JSONL replay**

Replay 按 `(event_time, sequence, event_id)` 排序，同 event id 不同 payload 立即失败；FakeBroker
记录每次 submit/cancel 以供安全断言。

- [ ] **Step 4: 运行 contract 与性质测试**

Run: `uv run pytest tests/contract/test_fake_broker.py tests/contract/test_replay_broker.py tests/properties/test_replay_determinism.py -q`
Expected: PASS.

- [ ] **Step 5: 提交**

```bash
git add src/firmquant/broker tests/contract tests/properties/test_replay_determinism.py
git commit -m "feat: add deterministic fake and replay brokers"
```

### Task 13: PaperBroker 撮合事实

**Files:**
- Create: `src/firmquant/broker/paper.py`
- Create: `src/firmquant/execution/policy.py`
- Test: `tests/contract/test_paper_broker.py`
- Test: `tests/unit/execution/test_policy.py`

**Interfaces:**
- Produces: `PaperBroker`, `ExecutionPolicy`, `FeeSchedule`, `FillModel`。

- [ ] **Step 1: 写部分成交、滑点、费用、停牌、价格边界、流动性和 T+1 测试**

```python
def test_paper_fill_respects_volume_participation(paper: PaperBroker) -> None:
    fill = paper.match(order(shares=10_000), quote(volume=100_000), max_participation=Decimal("0.005"))
    assert fill.shares == 500
```

- [ ] **Step 2: 运行失败测试**

Run: `uv run pytest tests/contract/test_paper_broker.py tests/unit/execution/test_policy.py -q`
Expected: FAIL because PaperBroker is absent.

- [ ] **Step 3: 实现使用 Broker facts 的确定性 Paper 撮合**

不硬编码板块涨跌幅；InstrumentFact 缺 upper/lower limit 时拒绝撮合。费用均以 Decimal 计算，
event/fill id 从命令与累计撮合序号确定性派生。

- [ ] **Step 4: 运行 contract L2**

Run: `uv run pytest tests/contract/test_paper_broker.py tests/unit/execution/test_policy.py -q`
Expected: PASS.

- [ ] **Step 5: 提交**

```bash
git add src/firmquant/broker/paper.py src/firmquant/execution tests
git commit -m "feat: simulate constrained A-share execution"
```

### Task 14: ExecutionController 与 Paper 次日 E2E

**Files:**
- Create: `src/firmquant/execution/planner.py`
- Create: `src/firmquant/execution/controller.py`
- Create: `tests/fixtures/session_cases.py`
- Test: `tests/e2e/test_paper_session.py`

**Interfaces:**
- Produces: `ExecutionPlanner.plan(snapshot, broker_snapshot)`, `ExecutionController.execute(plan)`。

- [ ] **Step 1: 写卖出后按实际现金买入、部分成交、截止撤单和日报前对账场景**

```python
def test_buy_uses_realized_cash_not_expected_sale(session_case: SessionCase) -> None:
    result = session_case.run_with_partial_sell()
    assert result.buy_submitted_value <= result.cash_after_sell
    assert result.negative_cash is False
```

- [ ] **Step 2: 运行失败 E2E**

Run: `uv run pytest tests/e2e/test_paper_session.py -q`
Expected: FAIL because planner/controller are absent.

- [ ] **Step 3: 实现 SELL 优先、稳定 BUY 缩量、SUBMITTING 先持久化和有限撤换**

planner 从 uquant 目标与经济 order id 派生最大授权数量；controller 每次 broker write 前提交
`SUBMITTING`，调用异常转 UNKNOWN，绝不重发。

- [ ] **Step 4: 运行 Checkpoint 4 验证**

Run: `uv run pytest tests/contract tests/unit/broker tests/unit/execution tests/e2e/test_paper_session.py tests/properties/test_replay_determinism.py -q`
Expected: PASS.

- [ ] **Step 5: 提交并推送 Checkpoint 4**

```bash
git add src/firmquant/execution tests
git commit -m "feat: complete paper execution session"
git push origin codex/firmquant-live-bootstrap
```

---

## Checkpoint 5 — 实盘风控、解锁、对账与崩溃恢复

### Task 15: 只收缩 ExecutionRiskGate

**Files:**
- Create: `src/firmquant/risk/gate.py`
- Test: `tests/unit/risk/test_gate.py`
- Test: `tests/properties/test_risk_never_expands.py`

**Interfaces:**
- Produces: `ExecutionRiskGate.evaluate(command, context) -> GateDecision`，结果仅 `ALLOW`、`SHRINK`、`DELAY`、`BLOCK`、`HALT`。

- [ ] **Step 1: 为规格四十项风控建立参数化拒绝/缩量测试**

```python
@given(commands_and_uquant_caps())
def test_gate_never_expands(case: GateCase) -> None:
    decision = case.gate.evaluate(case.command, case.context)
    assert decision.authorized_shares <= case.command.uquant_authorized_shares
```

- [ ] **Step 2: 运行失败测试**

Run: `uv run pytest tests/unit/risk/test_gate.py tests/properties/test_risk_never_expands.py -q`
Expected: FAIL because gate is absent.

- [ ] **Step 3: 实现有序检查、reason code 与 fail-closed 缺失事实处理**

所有价格边界与交易单位取 Broker facts；freeze_new_risk 阻止新增 BUY；SELL 不超过真实持仓与可卖；
任何 SHRINK 使用交易单位向下取整。

- [ ] **Step 4: 运行 L1/性质测试**

Run: `uv run pytest tests/unit/risk/test_gate.py tests/properties/test_risk_never_expands.py -q`
Expected: PASS.

- [ ] **Step 5: 提交**

```bash
git add src/firmquant/risk/gate.py tests/unit/risk tests/properties/test_risk_never_expands.py
git commit -m "feat: enforce shrink-only execution risk"
```

### Task 16: Arm lease、BrokerWriteCapability 与 kill switch

**Files:**
- Create: `src/firmquant/risk/arm.py`
- Create: `src/firmquant/risk/capability.py`
- Create: `src/firmquant/risk/kill_switch.py`
- Create: `src/firmquant/security/secrets.py`
- Test: `tests/unit/risk/test_arm.py`
- Test: `tests/unit/risk/test_capability.py`
- Test: `tests/properties/test_non_live_never_writes.py`

**Interfaces:**
- Produces: `ArmLease`, `ArmService.issue()`, `WriteCapabilityFactory.create()`, `KillSwitch.trip()`。

- [ ] **Step 1: 写 TTL、主机/账户/commit/config 绑定、CI/非 TTY、PAPER/SHADOW 和 kill 测试**

```python
@given(mode=sampled_from([Mode.REPLAY, Mode.PAPER, Mode.SHADOW]))
def test_non_live_mode_cannot_construct_write_capability(mode: Mode) -> None:
    with pytest.raises(WriteCapabilityDenied):
        WriteCapabilityFactory.create(context_for(mode))
```

- [ ] **Step 2: 运行失败测试**

Run: `uv run pytest tests/unit/risk/test_arm.py tests/unit/risk/test_capability.py tests/properties/test_non_live_never_writes.py -q`
Expected: FAIL because arm and capability modules are absent.

- [ ] **Step 3: 实现 HMAC lease、短时过期、全部十六项真实写门禁与不可序列化 capability**

确认短语只从 TTY 读取；环境变量不能表示 armed；capability 不提供 pickle/JSON 表达，到期、配置
漂移、disarm、halt 或 kill 时每次调用均重新拒绝。

- [ ] **Step 4: 运行安全 L2**

Run: `uv run pytest tests/unit/risk tests/properties/test_non_live_never_writes.py -q`
Expected: PASS and fake real gateway submit/cancel counters remain zero.

- [ ] **Step 5: 提交**

```bash
git add src/firmquant/risk src/firmquant/security tests
git commit -m "feat: require expiring capability for broker writes"
```

### Task 17: 启动、盘中与 EOD 对账

**Files:**
- Create: `src/firmquant/reconciliation/models.py`
- Create: `src/firmquant/reconciliation/service.py`
- Test: `tests/unit/reconciliation/test_service.py`
- Test: `tests/integration/test_external_activity_halt.py`

**Interfaces:**
- Produces: `ReconciliationService.run(kind, facts) -> ReconciliationReceipt`。

- [ ] **Step 1: 写外部订单、未知成交、现金/股数/可卖差异、账户变化和公司行动疑似测试**

```python
def test_external_order_forces_halt(service: ReconciliationService) -> None:
    receipt = service.run(ReconciliationKind.STARTUP, facts_with_external_order())
    assert receipt.passed is False
    assert "EXTERNAL_ACTIVE_ORDER" in receipt.blockers
```

- [ ] **Step 2: 运行失败测试**

Run: `uv run pytest tests/unit/reconciliation tests/integration/test_external_activity_halt.py -q`
Expected: FAIL because reconciliation service is absent.

- [ ] **Step 3: 实现三方权威字段比较与 append-only receipt**

容差只允许配置的 Decimal 现金舍入差；股数和可卖差异零容忍。人工活动不自动吸收，公司行动疑似
生成 operator action 并 HALT。

- [ ] **Step 4: 运行 L2**

Run: `uv run pytest tests/unit/reconciliation tests/integration/test_external_activity_halt.py -q`
Expected: PASS.

- [ ] **Step 5: 提交**

```bash
git add src/firmquant/reconciliation tests
git commit -m "feat: halt on unexplained broker reality"
```

### Task 18: 双存储 write-ahead 协议与关键崩溃恢复

**Files:**
- Create: `src/firmquant/persistence/recovery.py`
- Test: `tests/fault/test_crash_points.py`
- Test: `tests/integration/test_restart_recovery.py`

**Interfaces:**
- Produces: `AccountOperation.begin()`, `AccountOperation.commit_file()`, `RecoveryService.recover()`。

- [ ] **Step 1: 为规格十个崩溃点编写参数化测试**

```python
@pytest.mark.parametrize("point", CRITICAL_CRASH_POINTS)
def test_restart_never_duplicates_order_or_fill(point: CrashPoint, harness: CrashHarness) -> None:
    recovered = harness.crash_and_restart(point)
    assert recovered.duplicate_orders == 0
    assert recovered.duplicate_fills == 0
```

- [ ] **Step 2: 运行失败故障测试**

Run: `uv run pytest tests/fault/test_crash_points.py tests/integration/test_restart_recovery.py -q`
Expected: FAIL because recovery protocol is absent.

- [ ] **Step 3: 实现 before/expected-after hash、文件原子写入、收据补全与矛盾 HALT**

恢复分类严格为 `NOT_APPLIED`、`FILE_APPLIED_RECEIPT_MISSING`、`CONTRADICTION`；UNKNOWN submit
先查询 broker，无法证明未接单时保持 UNKNOWN。

- [ ] **Step 4: 运行 Checkpoint 5 验证**

Run: `uv run pytest tests/unit/risk tests/unit/reconciliation tests/integration/test_external_activity_halt.py tests/integration/test_restart_recovery.py tests/fault/test_crash_points.py tests/properties/test_risk_never_expands.py tests/properties/test_non_live_never_writes.py -q`
Expected: PASS.

- [ ] **Step 5: 提交并推送 Checkpoint 5**

```bash
git add src/firmquant/persistence/recovery.py tests
git commit -m "feat: recover account and order operations safely"
git push origin codex/firmquant-live-bootstrap
```

---

## Checkpoint 6 — XtQuant、SHADOW 与 Windows 部署

### Task 19: XtQuant lazy adapter 与 SDK contract fake

**Files:**
- Create: `src/firmquant/broker/xtquant.py`
- Create: `tests/fixtures/xtquant_sdk_fake.py`
- Test: `tests/contract/test_xtquant_adapter.py`
- Test: `tests/integration/test_xtquant_import_smoke.py`

**Interfaces:**
- Produces: `XtQuantBroker`, `XtQuantSdkFacade`, `diagnose_xtquant_sdk()`。

- [ ] **Step 1: 写 SDK 缺失失败关闭、官方字段映射和禁写 smoke 测试**

```python
def test_missing_sdk_has_actionable_diagnostic() -> None:
    with pytest.raises(BrokerDependencyMissing, match="official MiniQMT/XtQuant SDK"):
        XtQuantBroker.load_sdk(importer=missing_importer)
```

- [ ] **Step 2: 运行失败 contract**

Run: `uv run pytest tests/contract/test_xtquant_adapter.py tests/integration/test_xtquant_import_smoke.py -q`
Expected: FAIL because XtQuant adapter is absent.

- [ ] **Step 3: 根据当前官方资料/本机签名实现 lazy facade 与只读查询**

SDK import 只发生在选择 XtQuant 时；通用 CI 注入 contract fake。若本机 SDK 不存在，真实映射调用
保留 fail-closed 诊断，报告明确标记未完成真实环境验收，不能用猜测 payload 补齐。

- [ ] **Step 4: 运行 adapter contract；SDK 存在时额外运行只读 smoke**

Run: `uv run pytest tests/contract/test_xtquant_adapter.py tests/integration/test_xtquant_import_smoke.py -q`
Expected: PASS without calling submit/cancel; optional local read-only smoke records evidence separately.

- [ ] **Step 5: 提交**

```bash
git add src/firmquant/broker/xtquant.py tests
git commit -m "feat: add fail-closed XtQuant boundary"
```

### Task 20: SHADOW 只读 session

**Files:**
- Create: `src/firmquant/application/runtime.py`
- Create: `tests/e2e/test_shadow_session.py`

**Interfaces:**
- Produces: `Runtime.start(mode)`, `Runtime.stop()`, `ReadOnlyBrokerSession`。

- [ ] **Step 1: 写真实查询可用但 submit/cancel 不可达测试**

```python
def test_shadow_has_no_write_port(shadow_runtime: Runtime) -> None:
    result = shadow_runtime.run_one_session()
    assert result.plan_created is True
    assert shadow_runtime.gateway.submit_calls == 0
    assert shadow_runtime.gateway.cancel_calls == 0
```

- [ ] **Step 2: 运行失败 E2E**

Run: `uv run pytest tests/e2e/test_shadow_session.py -q`
Expected: FAIL because runtime composition is absent.

- [ ] **Step 3: 实现 mode-specific composition root，SHADOW 不注入 BrokerWriteCapability**

SHADOW 完成连接、快照、对账、决策计划和报告，但执行阶段只保存 hypothetical plan；任何
cancel-system-orders 请求返回明确的 mode blocker。

- [ ] **Step 4: 运行 SHADOW L3**

Run: `uv run pytest tests/e2e/test_shadow_session.py -q`
Expected: PASS with zero writes.

- [ ] **Step 5: 提交**

```bash
git add src/firmquant/application/runtime.py tests/e2e/test_shadow_session.py
git commit -m "feat: run broker-connected shadow sessions read-only"
```

### Task 21: Doctor 与 Windows 部署 smoke

**Files:**
- Create: `src/firmquant/observability/health.py`
- Create: `scripts/windows_smoke.py`
- Create: `docs/DEPLOYMENT_WINDOWS.md`
- Create: `.github/workflows/windows.yml`
- Test: `tests/unit/observability/test_doctor.py`

**Interfaces:**
- Produces: `Doctor.run() -> tuple[CheckResult, ...]`，覆盖需求列出的十五项检查。

- [ ] **Step 1: 写每项 doctor check 与 live 锁死测试**

```python
def test_doctor_proves_live_is_locked(doctor: Doctor) -> None:
    result = doctor.run_named("live-mode-lock")
    assert result.passed and result.details["write_capability"] is False
```

- [ ] **Step 2: 运行失败测试**

Run: `uv run pytest tests/unit/observability/test_doctor.py -q`
Expected: FAIL because Doctor is absent.

- [ ] **Step 3: 实现健康检查和不接触真实券商的 Windows smoke**

Windows smoke 使用临时目录、PaperBroker 和 fake secret provider，检查 zoneinfo、SQLite WAL、CLI、
路径、文件锁与备份恢复；CI 不查真实账户。

- [ ] **Step 4: 运行 Checkpoint 6 验证**

Run: `uv run pytest tests/contract/test_xtquant_adapter.py tests/integration/test_xtquant_import_smoke.py tests/e2e/test_shadow_session.py tests/unit/observability/test_doctor.py -q && uv run python scripts/windows_smoke.py`
Expected: PASS; real SDK/account status reported honestly.

- [ ] **Step 5: 提交并推送 Checkpoint 6**

```bash
git add src/firmquant/observability .github/workflows/windows.yml scripts/windows_smoke.py docs/DEPLOYMENT_WINDOWS.md tests
git commit -m "ops: verify shadow and Windows deployment safety"
git push origin codex/firmquant-live-bootstrap
```

---

## Checkpoint 7 — Session 编排、运维、报告、文档与最终验收

### Task 22: 交易时钟、市场日历与每日 workflow

**Files:**
- Create: `src/firmquant/market_data/gateway.py`
- Create: `src/firmquant/market_data/validation.py`
- Create: `src/firmquant/market_data/calendar.py`
- Create: `src/firmquant/scheduling/clock.py`
- Create: `src/firmquant/scheduling/sessions.py`
- Create: `src/firmquant/application/workflows.py`
- Create: `src/firmquant/application/sessions.py`
- Test: `tests/unit/scheduling/test_clock.py`
- Test: `tests/e2e/test_daily_workflow.py`

**Interfaces:**
- Produces: `SessionCoordinator.startup()`, `post_close_decision()`, `next_day_execute()`, `intraday()`, `eod()`。

- [ ] **Step 1: 写 holiday、clock skew、stale data/quote、历史前缀漂移和盘前禁重算测试**

```python
def test_next_day_execution_uses_frozen_decision(workflow: WorkflowHarness) -> None:
    workflow.execute_next_day()
    assert workflow.production_engine_decide_calls == 0
```

- [ ] **Step 2: 运行失败 E2E**

Run: `uv run pytest tests/unit/scheduling/test_clock.py tests/e2e/test_daily_workflow.py -q`
Expected: FAIL because workflow modules are absent.

- [ ] **Step 3: 实现 Asia/Shanghai session、broker market status 授权和可恢复步骤收据**

数据验证复用 uquant manifest 和 append-only 语义；weekday 只显示不授权。每个步骤以 session/idempotency
key 保存开始与完成，重启从最后已确认步骤继续。

- [ ] **Step 4: 运行 L3**

Run: `uv run pytest tests/unit/scheduling tests/e2e/test_daily_workflow.py -q`
Expected: PASS across startup, post-close, next-day, intraday and EOD.

- [ ] **Step 5: 提交**

```bash
git add src/firmquant/market_data src/firmquant/scheduling src/firmquant/application tests
git commit -m "feat: orchestrate recoverable trading sessions"
```

### Task 23: 完整运维 CLI

**Files:**
- Modify: `src/firmquant/cli.py`
- Test: `tests/unit/test_cli_commands.py`
- Test: `tests/integration/test_cli_operations.py`

**Interfaces:**
- Produces: 规格列出的 init、doctor、run、status、arm-live、disarm、halt、resume、reconcile、decisions、orders、fills、report、replay、backup、verify-backup、cancel-system-orders。

- [ ] **Step 1: 写命令全集、status 字段全集和真实命令门禁测试**

```python
def test_status_contains_required_fields(cli: CliRunner) -> None:
    payload = json.loads(cli.invoke(["status", "--json"]).stdout)
    assert REQUIRED_STATUS_FIELDS <= payload.keys()
```

- [ ] **Step 2: 运行失败测试**

Run: `uv run pytest tests/unit/test_cli_commands.py tests/integration/test_cli_operations.py -q`
Expected: FAIL because command handlers are incomplete.

- [ ] **Step 3: 实现薄 CLI handler，所有业务动作委托 application service**

`arm-live` 拒绝非 TTY/CI，真实账户标识永不回显；`resume` 强制重新对账；`cancel-system-orders`
只取消 firmquant 映射订单并要求 write capability。

- [ ] **Step 4: 运行 CLI L2**

Run: `uv run pytest tests/unit/test_cli_commands.py tests/integration/test_cli_operations.py -q && uv run firmquant --help`
Expected: PASS and help includes every command.

- [ ] **Step 5: 提交**

```bash
git add src/firmquant/cli.py tests/unit/test_cli_commands.py tests/integration/test_cli_operations.py
git commit -m "feat: expose audited local operations CLI"
```

### Task 24: 日志脱敏、通知、日报和安全扫描

**Files:**
- Create: `src/firmquant/security/redaction.py`
- Create: `src/firmquant/observability/logging.py`
- Create: `src/firmquant/observability/notifiers.py`
- Create: `src/firmquant/observability/reports.py`
- Create: `scripts/secret_scan.py`
- Test: `tests/unit/security/test_redaction.py`
- Test: `tests/unit/observability/test_reports.py`
- Test: `tests/integration/test_notifier_failure_isolation.py`

**Interfaces:**
- Produces: `redact(value)`, `configure_logging()`, `Notifier`, `DailyReportRenderer.render()`。

- [ ] **Step 1: 写 secret/account/path 脱敏、通知失败隔离和 Markdown/JSON 一致测试**

```python
def test_report_keeps_failures_and_differences(report: DailyReport) -> None:
    payload = json.loads(renderer.render_json(report))
    assert payload["rejected_orders"]
    assert payload["target_actual_differences"]
```

- [ ] **Step 2: 运行失败测试**

Run: `uv run pytest tests/unit/security tests/unit/observability/test_reports.py tests/integration/test_notifier_failure_isolation.py -q`
Expected: FAIL because observability/security modules are absent.

- [ ] **Step 3: 实现统一 redaction、JSON/console formatter、console/file/webhook notifier 和报告**

webhook secret 从 provider 读取且不进入 repr；webhook 异常只生成 alert，不回滚 broker/订单事务；
secret scanner 拒绝已知 token 模式、真实账户快照和 MiniQMT userdata 内容。

- [ ] **Step 4: 运行安全 L2**

Run: `uv run pytest tests/unit/security tests/unit/observability tests/integration/test_notifier_failure_isolation.py -q && uv run python scripts/secret_scan.py`
Expected: PASS.

- [ ] **Step 5: 提交**

```bash
git add src/firmquant/security src/firmquant/observability scripts/secret_scan.py tests
git commit -m "feat: add redacted audit reporting and alerts"
```

### Task 25: 完整性质测试与故障注入矩阵

**Files:**
- Create: `tests/properties/test_economic_invariants.py`
- Create: `tests/fault/test_broker_failures.py`
- Create: `tests/fault/test_storage_failures.py`
- Create: `tests/fault/test_identity_failures.py`
- Create: `tests/e2e/test_replay_restart_equivalence.py`

**Interfaces:**
- Consumes: 已完成领域、broker、risk、persistence 和 application 公共接口。
- Produces: 用户要求的十项性质和二十八类故障场景的可执行证据。

- [ ] **Step 1: 写成交、持仓、现金、终态、去重、目标、universe、非 LIVE、replay 和重启性质**

```python
@given(event_streams())
def test_duplicate_events_do_not_change_economics(stream: list[BrokerEvent]) -> None:
    assert run(stream).economic_hash == run(stream + stream).economic_hash
```

- [ ] **Step 2: 运行新增测试并记录真实失败**

Run: `uv run pytest tests/properties/test_economic_invariants.py tests/fault tests/e2e/test_replay_restart_equivalence.py -q`
Expected: any failure identifies a concrete invariant gap; no xfail or test deletion is allowed.

- [ ] **Step 3: 对每个失败先运行单项并最小修复受影响模块**

Run: `uv run pytest <failing-node-id> -q`
Expected: each repaired node passes before rerunning this task's matrix.

- [ ] **Step 4: 运行故障/性质 L3**

Run: `uv run pytest tests/properties tests/fault tests/e2e/test_replay_restart_equivalence.py -q`
Expected: PASS for all required invariants and injected failures.

- [ ] **Step 5: 提交**

```bash
git add src tests/properties tests/fault tests/e2e/test_replay_restart_equivalence.py
git commit -m "test: prove execution and recovery invariants"
```

### Task 26: Canonical 中文文档与 ADR

**Files:**
- Create: `README.md`
- Create: `docs/ARCHITECTURE.md`
- Create: `docs/STRATEGY_INTEGRATION.md`
- Create: `docs/BROKER_ADAPTER.md`
- Create: `docs/EXECUTION.md`
- Create: `docs/RISK_AND_SAFETY.md`
- Create: `docs/OPERATIONS.md`
- Create: `docs/RECOVERY.md`
- Create: `docs/COMPLIANCE.md`
- Create: `docs/CONFIGURATION.md`
- Create: `docs/DEVELOPMENT.md`
- Create: `docs/QUALITY.md`
- Create: `docs/UPSTREAM_GAPS.md`
- Create: `docs/decisions/0001-modular-monolith.md`
- Create: `docs/decisions/0002-sqlite-single-writer.md`
- Create: `docs/decisions/0003-xtquant-first-adapter.md`
- Create: `scripts/check_docs.py`
- Test: `tests/unit/test_documented_defaults.py`

**Interfaces:**
- Produces: 仅描述当前系统、与 CLI/config/code 可机读核对的 canonical 文档。

- [ ] **Step 1: 写 README 安全声明、链接、命令和默认值一致性测试**

```python
def test_readme_never_contains_copyable_live_run_command() -> None:
    text = Path("README.md").read_text(encoding="utf-8")
    assert "firmquant run --mode live" not in text.lower()
    assert "默认 PAPER" in text
```

- [ ] **Step 2: 运行失败文档测试**

Run: `uv run pytest tests/unit/test_documented_defaults.py -q`
Expected: FAIL because canonical docs are absent.

- [ ] **Step 3: 根据实际代码编写中文文档和三项 ADR**

README 快速开始只允许 PAPER/SHADOW；参数表从 config schema 生成或校验，不复制 uquant 权威策略
默认值；UPSTREAM_GAPS 只记录实际无法通过 adapter 解决的公共接口缺口。

- [ ] **Step 4: 运行文档检查**

Run: `uv run pytest tests/unit/test_documented_defaults.py -q && uv run python scripts/check_docs.py && uv run firmquant --help`
Expected: PASS with no broken local links or stale commands.

- [ ] **Step 5: 提交**

```bash
git add README.md docs scripts/check_docs.py tests/unit/test_documented_defaults.py
git commit -m "docs: publish current production operating contract"
```

### Task 27: 最终 L4、清理、PR 与 main 核验

**Files:**
- Modify: `.github/workflows/ci.yml`
- Modify: `.github/workflows/security.yml`
- Delete: `docs/superpowers/specs/2026-08-25-firmquant-live-trading-system-design.md`
- Delete: `docs/superpowers/plans/2026-08-25-firmquant-live-trading-system.md`
- Verify: entire repository

**Interfaces:**
- Produces: clean final candidate、构建 wheel、L4 证据、PR 与最终 `origin/main` SHA。

- [ ] **Step 1: 在稳定候选上运行一次完整 L4**

```bash
uv sync --frozen --extra dev
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run pytest --cov=firmquant --cov-branch --cov-fail-under=85
uv run python -m compileall -q src tests
uv run bandit -c pyproject.toml -r src
uv run pip-audit
uv run python scripts/build_reproducible_wheels.py --verify-twice
uv run python scripts/verify_source_baseline.py
uv run python scripts/check_docs.py
uv run python scripts/secret_scan.py
uv run python scripts/windows_smoke.py
```

Expected: every command exits 0; no real submit/cancel call; coverage branch threshold is met.

- [ ] **Step 2: 对 L4 失败执行影响范围修复**

先运行失败 node/job，修复后运行其直接受影响测试；只有行为或共享基础设施变化使稳定候选改变时，
才重新执行完整 L4。不得删除失败场景、放宽安全门或降低覆盖率。

- [ ] **Step 3: 清理工作材料并运行行为中性检查**

删除本规格和计划工作稿、临时日志、构建目录和无长期价值报告；运行 `git diff --check`、文档链接、
CLI help 与 secret scan。canonical 文档与 ADR 已承接所有长期设计决策。

- [ ] **Step 4: 提交并推送最终候选**

```bash
git add -A
git commit -m "chore: finalize verified firmquant candidate"
git status --short
git push origin codex/firmquant-live-bootstrap
```

Expected: worktree clean and branch contains every checkpoint commit.

- [ ] **Step 5: 创建 PR、等待 Linux/Windows CI、合并并核验 main**

创建从 `codex/firmquant-live-bootstrap` 到 `main` 的 PR；仅在 required checks 全部成功后非强制合并。
随后读取 `origin/main` 精确 SHA、检查合并 tree 与候选 tree 一致，并输出最终报告要求的二十项证据，
其中“实施过程中提交过真实订单”必须为“没有”。
