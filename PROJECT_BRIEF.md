# firmquant 项目简报

> 本文件只保存长期稳定的项目事实与边界。当前任务、分支、SHA、测试结果和临时风险统一记录在 `TASK_STATE.md`；当前任务验收合同统一记录在 `ACCEPTANCE.md`。

## 项目名称和最终目标

项目名称：`firmquant`。

最终目标：为一个用户、一个 A 股现金账户和一台 Windows 交易电脑提供可长期、轻量、安全运行的日频实盘执行系统，在不改写 uquant 策略经济行为的前提下，支持 PAPER → SHADOW → CANARY → LIVE 的受控晋级、运行中授权、异常恢复、账户重新基线、审计和后续锁定式 uquant 升级。

## 系统定位

- firmquant 是券商事实接入、订单执行、前置安全门、对账、恢复和审计系统，不是新的策略研究项目、第二策略、高频系统或收益承诺工具。
- 系统只服务单一 A 股 AI 产业链现金账户，现金多头、无杠杆、禁止做空；不覆盖多账户、多策略、融资融券、期货、期权或其他衍生品。
- 日频经济路径固定为盘后决策、下一可交易日执行。盘中只处理订单生命周期、成交、断线、行情 freshness、风险阻断和对账，不重新选股或优化组合。
- 系统通过用户授权的券商 API 连接账户，能够自动处理委托和成交事实，但不能保证成交。
- 默认模式始终为 PAPER，默认及示例配置必须保持 `live_trading_enabled = false`。

## 关键架构和模块边界

架构采用模块化单体和端口适配器：一个进程、一个 SQLite operational ledger、一个账户 writer lease。Broker callback 只验证并进入有界队列，单 writer 串行推进订单、账户和审计事务。

| 模块 | 独占职责 |
|---|---|
| `application` | 本机用例、session coordinator、启动/停止和运维命令编排 |
| `domain` | 值对象、运行/订单状态机、领域事件与不变量 |
| `strategy` | 锁定身份、uquant anti-corruption adapter、账户 prepare/commit、不可变 DecisionSnapshot |
| `broker` | BrokerGateway、Fake/Paper/Replay/XtQuant 适配器、不可信输入规范化 |
| `market_data` | 权威交易日历、日频 manifest、append-only 验证和执行行情端口 |
| `execution` | 冻结决策到订单计划、SELL/BUY 顺序、提交/撤单与期限政策 |
| `risk` | 只收缩逐单风控、arm lease、kill switch 和 broker-write capability |
| `reconciliation` | binding、preflight、券商/uquant/firmquant 三方资金、持仓、订单、成交和身份对账 |
| `persistence` | SQLite migration、事务 repository、单 writer、账户证据、恢复、备份和 hash-chain audit |
| `scheduling` | Asia/Shanghai session、时钟校验和可恢复 workflow receipt |
| `observability` / `security` | 结构化日志、报告、告警、secret provider、脱敏和扫描 |

## 不可改变的业务约束

- 锁定的 uquant 是唯一策略决策内核；策略决策只允许调用一次 `ProductionEngine.decide()`。
- PortfolioAllocator、Base Risk、FREEZE_ONLY Risk Sentinel、策略参数、目标组合和策略 AccountState 的经济职责由 uquant 独占。firmquant 不实现第二套对应逻辑。
- canonical AI universe 和点时成员由 uquant manifest 独占；部署 allowlist 只能取其子集，不能扩展成员。
- firmquant 安全层只能阻止、缩小、延迟、取消订单或进入 HALT，绝不能扩大 uquant 的目标、总仓、单票权重或买入数量。
- REPLAY、PAPER、SHADOW 和 CI 在结构上不得触达真实 `submit_order` 或 `cancel_order`。
- CANARY/LIVE 每次券商写操作都必须重新经过短时 arm lease、合规确认、身份绑定、启动对账、数据和 quote freshness、session、kill switch、UNKNOWN order、现金/持仓以及逐单 ExecutionRiskGate 等全部适用门禁；缺一即失败关闭。
- CANARY 不自动升级 LIVE。`live-readiness` 只读汇总机器门槛，不 arm、不下单、不代替人工批准。
- 紧急状态阻止新订单、仅取消 firmquant 明确拥有的未成交订单并 HALT；不自动清仓，不在状态不确定时发无保护市价单。
- uquant AccountState 是策略经济状态和持仓 lifecycle 的唯一权威；operational ledger 不得成为第二经济账户。
- 券商事实进入 AccountState 必须遵守 binding → preflight → 内存 prepare → final reconciliation → expected-before CAS commit；人工交易、异常现金、外部订单、身份漂移或未解释差异在提交前阻断。
- 金额、价格和费用使用 Decimal 或整数最小单位；股数使用整数值对象；外部 payload 一律视为不可信输入。
- 运行模式为 REPLAY、PAPER、SHADOW、CANARY、LIVE；运行状态为 DISARMED、STARTING、RECONCILING、READY、EXECUTING、DEGRADED、HALTED、STOPPING。
- 订单状态为 PLANNED、VALIDATED、ARMED、SUBMITTING、ACKNOWLEDGED、PARTIALLY_FILLED、FILLED、CANCEL_REQUESTED、CANCELLED、REJECTED、EXPIRED、UNKNOWN。
- 当前 XtQuant/MiniQMT 真实环境是否接通必须由合法部署机上的真实只读 smoke 证明；fake、contract、CI、skip 或未运行不得表述为真实账户已验证。

## 权威数据源和状态 owner

| 权威 | 独占拥有 | 明确不拥有 |
|---|---|---|
| 券商 | 可用现金、总资产、真实/可卖持仓、broker order/fill id、委托成交、费用、实时证券状态 | 策略目标、策略 lifecycle |
| uquant | 机会、风险、Sentinel、目标组合、策略持仓 lifecycle、经济 order id、策略配置、数据身份、canonical universe | 在线连接、broker id、重试和告警 |
| firmquant | broker 映射、提交尝试、回调、UNKNOWN、账户 binding、对账、arm lease、kill switch、运行健康和审计 | 第二策略账户、目标组合、策略参数 |

身份和版本的机器权威位于 `src/firmquant/resources/source_identity.json`，人类可读基线位于 `docs/SOURCE_BASELINE.md`。具体锁定 SHA、tree、manifest 和 wheel 摘要只在这些权威位置维护，不在本文件复制可能变化的快照。

## 标准构建、测试、lint 和运行命令

要求 Python `>=3.12,<3.13`，仓库 `.python-version` 为 `3.12`；依赖以 `uv.lock` 和 frozen sync 为准。

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
uv run python scripts/build_reproducible_wheels.py --verify-twice
uv run python scripts/verify_source_baseline.py
uv run python scripts/check_docs.py
```

文档或 CLI 合同变化的最小验证：

```bash
uv run pytest tests/unit/test_documented_defaults.py -q
uv run python scripts/check_docs.py
uv run python scripts/secret_scan.py
```

仅 PAPER 的本机启动流程：

```bash
cp config/firmquant.example.toml config/firmquant.local.toml
uv sync --frozen --extra dev
uv run firmquant init
uv run firmquant doctor
uv run firmquant run --mode paper
uv run firmquant status
```

## 重要目录和文件索引

| 路径 | 用途 |
|---|---|
| `README.md` | 项目入口、安全状态、运行模型和 CLI 概览 |
| `AGENTS.md` | 编码代理工作约定、工程方法、验证阶梯和 Git 安全规则 |
| `PROJECT_BRIEF.md` | 长期稳定的项目目标、边界、owner 和标准命令 |
| `TASK_STATE.md` | 单一最新任务状态；替换过期值，不保留冲突快照 |
| `ACCEPTANCE.md` | 当前任务稳定验收合同和组合逻辑 |
| `pyproject.toml` / `uv.lock` | Python、依赖、构建、测试、lint、类型和覆盖率配置 |
| `config/firmquant.example.toml` | 安全示例配置；实盘写权限默认关闭 |
| `src/firmquant/` | 生产实现 |
| `src/firmquant/resources/` | 机器可读的锁定身份和静态资源 |
| `tests/` | unit、contract、integration、property、fault、e2e 和 parity 证据 |
| `scripts/` | 文档、secret、基线、可复现构建和 Windows smoke 检查 |
| `.github/workflows/` | Linux/Windows、parity、安全和部署 smoke CI |
| `docs/ARCHITECTURE.md` | 架构、权威和账户提交边界 |
| `docs/STRATEGY_INTEGRATION.md` | uquant 唯一决策路径、AccountState、universe 和 parity |
| `docs/RISK_AND_SAFETY.md` | 只收缩风控、模式、lease 和 capability |
| `docs/OPERATIONS.md` / `docs/RECOVERY.md` | 运行、进程托管、故障和恢复 |
| `docs/QUALITY.md` / `docs/DEVELOPMENT.md` | 验证阶梯、最终门和开发规则 |
| `docs/SOURCE_BASELINE.md` | 当前 uquant 锁定源码、manifest 和构建身份 |
| `evidence/`（存在时） | 原始日志、大型测试输出和任务证据；不得复制进 `TASK_STATE.md` |

## 明确禁止事项

- 禁止修改、复制或近似重写 uquant 的策略、PortfolioAllocator、Base Risk、FREEZE_ONLY Sentinel、目标组合、参数或 AccountState lifecycle。
- 禁止扩大 canonical AI universe、策略目标、总仓、单票权重、买入数量或风险授权。
- 禁止用 fake/CI/contract/skip 代替真实 MiniQMT 只读验证，禁止在测试、CI、smoke 或实现过程中发送真实订单。
- 禁止从脏工作树、临时分支、浮动 main 或浮动 tag 构建生产 uquant 依赖。
- 禁止覆盖 manifest、自动吸收人工交易、猜测 tranche/lifecycle/attribution、盲目重发 UNKNOWN、在不确定状态自动清仓或伪造证据。
- 禁止提交或记录账户号、密码、token、webhook secret、MiniQMT 敏感 userdata、真实账户快照、未脱敏成交、SDK、wheel、数据库、日志或事故 payload。
- 禁止为使测试变绿而删除场景、放宽门禁、近似经济断言、绕过状态机或把未运行写成通过。
- 禁止 reset、clean、rebase、force push、破坏性 checkout/restore、覆盖未提交工作或无授权改写 Git 历史。
- 禁止把临时 SHA、当前分支、当前测试结果、本轮进度、大段历史或完整日志写入本文件。
