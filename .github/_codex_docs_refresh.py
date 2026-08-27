from pathlib import Path

files = {
"README.md": r'''# firmquant

firmquant 是面向单一 A 股现金账户的实盘执行系统，不是新的策略研究项目。锁定的 **uquant 是唯一策略决策内核**：
firmquant 负责券商事实接入、订单执行、前置安全门、对账、恢复和审计，不复制或改写策略、组合分配与策略风险状态机。

项目只支持 A 股 AI 产业链、现金多头、无杠杆、禁止做空。系统通过用户的券商授权 API 连接账户，不是个人直接连接
交易所；它能自动处理委托与成交事实，但不能保证成交。

## 安全状态

- 默认 PAPER，实盘功能默认关闭，默认配置固定为 `live_trading_enabled = false`。
- REPLAY、PAPER、SHADOW 不具有真实券商写权限；CI 和示例配置也不具有实盘写权限。
- CANARY/LIVE 需要明确配置、短时 arm lease、部署身份绑定、券商 API 权限和程序化交易合规确认、启动对账、最新
  券商/行情事实、完整逐单风控及无 UNKNOWN 状态；任一条件缺失即失败关闭。
- uquant Risk Sentinel 仍为 `FREEZE_ONLY`：只能冻结新增风险，不直接卖出、不改 gross cap，也不创建第二风险账户。
- firmquant 的安全层只能阻止、缩小、延迟或取消订单以及进入 HALT，绝不扩大 uquant 的目标或订单意图。
- 紧急状态默认阻止新订单、取消系统拥有的未成交订单并 HALT，不自动清仓，更不会在状态不确定时发无保护市价单。
- 真实交易前必须完成券商 API 授权和程序化交易合规确认；任何测试或自动化验收都禁止发送真实订单。

XtQuant 适配器、contract fake 和只读组合边界已实现。当前构建环境没有官方 SDK，也没有合法账户环境，因此未完成
MiniQMT 真实账户只读 smoke，不能声明实盘适配器已经接通。安装版运行组合会对未验证的 XtQuant 前置条件失败关闭。

## 快速开始（仅 PAPER）

要求 Python 3.12 和 [uv](https://docs.astral.sh/uv/)。以下流程不会连接真实券商：

```bash
cp config/firmquant.example.toml config/firmquant.local.toml
uv sync --frozen --extra dev
uv run firmquant init
uv run firmquant doctor
uv run firmquant run --mode paper
uv run firmquant status
```

`doctor` 失败项必须先修复；不能通过修改摘要、删除状态或跳过检查强行继续。SHADOW 只可在部署机安装合法官方 SDK、完成
只读 schema/import 检查并使用单独的本地配置后启用，仓库不提供可直接复制的实盘启动命令。

## 账户权威与运行模型

真实账户首次接入必须先执行一次 `bootstrap-account`，建立券商账户、严格 uquant AccountState 和持久 account binding 的
唯一对应关系。系统必须处于 DISARMED、无有效 arm lease、无既有策略决策/系统订单/成交且无未完成账户事务；空持仓账户
可以由券商可用现金创建 `AccountState.empty`，非空账户必须提供严格校验通过的已复核 `--account-state` seed。系统不会从
券商持仓猜测 tranche、lifecycle、attribution 或策略来源。

正常运行时，券商事实不能直接改写 uquant AccountState。固定顺序是：读取完整 broker snapshot 和持久 binding → 对当前
AccountState、operational ledger 与 broker facts 做 preflight → 仅在深拷贝上 prepare 已知系统成交 → 对 prepared state 做
完整 reconciliation → 全部通过后才以 expected-before CAS 提交 AccountState、account operation receipt 和 reconciliation
receipt。人工交易、异常现金、外部订单、未解释持仓变化或身份漂移会在提交前失败关闭，不会被“同步”吸收。

日频经济路径固定为：盘后更新并验证 uquant 数据合同，完成上述账户对账与合法事实采纳后，通过
`ProductionEngine.decide()` 生成不可变决策；下一交易日只执行该冻结决策。盘中持续运行仅处理订单生命周期、成交、
断线、quote freshness、风险阻断和对账，不重新选股或优化组合。

运行状态不是布尔值，而是：`DISARMED`、`STARTING`、`RECONCILING`、`READY`、`EXECUTING`、`DEGRADED`、
`HALTED`、`STOPPING`。订单状态持久化为：`PLANNED`、`VALIDATED`、`ARMED`、`SUBMITTING`、
`ACKNOWLEDGED`、`PARTIALLY_FILLED`、`FILLED`、`CANCEL_REQUESTED`、`CANCELLED`、`REJECTED`、`EXPIRED`、
`UNKNOWN`。

## 本机 CLI

所有命令均支持 `--help` 和 `--json`；配置路径通过顶层 `--config` 指定。命令只暴露本机控制面，不默认开启 HTTP
管理端口。

| 命令 | 当前职责 |
|---|---|
| `firmquant init` | 创建安全默认配置、状态目录和 SQLite 账本 |
| `firmquant doctor` | 检查依赖、身份、数据、账本、锁、时钟、SDK、只读连接、合规和实盘锁定 |
| `firmquant run` | 运行与配置一致的 session；模式不一致即拒绝 |
| `firmquant status` | 输出模式、运行状态、arm、身份、券商、对账、订单、现金、敞口与阻断原因 |
| `firmquant arm-live` | 交互式创建短时、认证且绑定部署身份的 lease |
| `firmquant disarm` | 撤销活动 lease |
| `firmquant halt` | 触发 kill switch 并阻止新增订单 |
| `firmquant resume` | 经显式复核后请求恢复，不清除未解决差异 |
| `firmquant reconcile` | 对账券商、uquant AccountState 与 operational ledger |
| `firmquant bootstrap-account` | 一次性建立真实券商账户、uquant AccountState 与持久 binding；非空账户必须提供已复核 seed |
| `firmquant decisions` | 查询不可变 DecisionSnapshot |
| `firmquant orders` | 查询经济意图、提交尝试和券商订单生命周期 |
| `firmquant fills` | 查询已规范化成交与费用事实 |
| `firmquant report` | 生成/读取 session 的 Markdown 与 JSON 报告 |
| `firmquant replay` | 确定性重放冻结的券商事件 |
| `firmquant backup` | 创建原子、一致性状态备份 |
| `firmquant verify-backup` | 在隔离目录验证可恢复性、schema 与审计链 |
| `firmquant cancel-system-orders` | 请求取消 firmquant 拥有的未完成订单；仍需完整写门禁 |

## 文档

- [架构与权威边界](docs/ARCHITECTURE.md)
- [uquant 集成与等价性](docs/STRATEGY_INTEGRATION.md)
- [BrokerGateway 与 XtQuant 状态](docs/BROKER_ADAPTER.md)
- [订单执行合同](docs/EXECUTION.md)
- [风险与实盘安全](docs/RISK_AND_SAFETY.md)
- [运行与本机运维](docs/OPERATIONS.md)
- [故障恢复](docs/RECOVERY.md)
- [合规边界](docs/COMPLIANCE.md)
- [配置合同](docs/CONFIGURATION.md)
- [Windows 部署](docs/DEPLOYMENT_WINDOWS.md)
- [开发指南](docs/DEVELOPMENT.md)
- [质量与验证](docs/QUALITY.md)
- [uquant 源码基线](docs/SOURCE_BASELINE.md)
- [已复现的上游接口缺口](docs/UPSTREAM_GAPS.md)

## 许可证与责任

源码按 [MIT License](LICENSE) 提供。使用者必须自行确认券商协议、账户权限、程序化交易报告、交易所/监管要求和所在
地区法律。报告和日志是运行证据，不构成收益承诺或成交保证。
''',
"docs/ARCHITECTURE.md": r'''# 架构与权威边界

firmquant 采用模块化单体和端口适配器：一个进程、一个 SQLite operational ledger、一个账户 writer lease。该结构适合
单账户、日频、Windows 本地券商终端场景，也使订单状态和审计事务保持在同一一致性边界内。

## 数据流

```mermaid
flowchart LR
    B[券商与行情事实] --> N[严格规范化]
    N --> P[Binding + Preflight]
    P --> A[内存 Prepared Account Sync]
    A --> C[Final Reconciliation]
    C --> K[CAS Account / Receipt Finalization]
    K --> U[ProductionEngine.decide]
    U --> D[不可变 DecisionSnapshot]
    D --> R[ExecutionRiskGate]
    R --> O[Durable Order State Machine]
    O --> G[BrokerGateway]
    G --> E[委托与成交事件队列]
    E --> W[单 writer 事务推进]
    W --> P
```

盘后决策和次日执行是两个独立、可恢复的 workflow step。盘中行情只供执行与安全检查，不能进入日频策略输入。broker
facts 只有经过持久 account binding、preflight、内存 prepare 和 final reconciliation 后，才允许进入 uquant AccountState。

## 三类权威

| 权威 | 独占职责 | 明确不拥有 |
|---|---|---|
| 券商 | 可用现金、总资产、真实/可卖持仓、broker order/fill id、委托成交、费用、实时证券状态 | 策略目标、策略 lifecycle |
| uquant | 机会、风险、Sentinel、目标组合、策略持仓 lifecycle、经济 order id、策略配置与数据身份 | 在线连接、broker id、重试和告警 |
| firmquant | broker 映射、提交尝试、回调、UNKNOWN、账户 binding、对账、arm lease、kill switch、运行健康和审计 | 第二策略账户、目标组合、策略参数 |

首次真实账户接入通过一次性 bootstrap 建立券商账户与 uquant AccountState 的持久 binding；之后生产对账只信任该 binding，
不会把“上一份 broker snapshot”当成账户绑定依据。券商事实不能反向猜出 uquant lifecycle，firmquant operational ledger 也
不能成为第二个经济账户。任何权威间差异都先保留证据并 HALT，由操作员依据明确事实处理。

## 账户提交边界

账户同步分为 prepare 与 commit。prepare 只在深拷贝上调用锁定 uquant 的 account-sync 语义，不修改生产文件，也不创建
account operation 或 reconciliation receipt。preflight 只允许由 firmquant 已知、映射一致且已落 operational ledger 的系统
成交解释账户变化；人工交易、异常现金、外部订单、身份漂移和未解释持仓变化在生产 AccountState 改写前阻断。

final reconciliation 对 prepared AccountState 再做三方完整比较。通过后，expected-before CAS 才允许原子替换 AccountState
文件；account operation payload 同时封存 reconciliation finalization evidence，随后在一个 SQLite transaction 内完成 account
operation receipt、audit 与 reconciliation receipt。崩溃发生在文件替换与 SQLite finalization 之间时，恢复逻辑以同一
finalization evidence 收敛，而不是重新猜测或重新执行经济行为。

`ReviewedAccountAdjustment` 只是精确、append-only 的人工复核证据，绑定账户、symbol/session、类型、broker snapshot 和具体
difference hash。精确现金差异可被授权；持仓/可卖数量差异即使已复核仍要求显式、完整的 reviewed AccountState，firmquant
不会据此合成策略 tranche、公司行动或人工卖出生命周期。

## 模块边界

- `application`：本机用例、session coordinator、启动/停止和运维命令编排。
- `domain`：值对象、运行/订单状态机、领域事件与不变量。
- `strategy`：锁定身份、uquant anti-corruption adapter、账户 prepare/commit 和 DecisionSnapshot。
- `broker`：稳定 BrokerGateway、Fake/Paper/Replay/XtQuant 适配器和不可信输入规范化。
- `market_data`：权威交易日历、日频 manifest/append-only 验证、执行行情端口。
- `execution`：冻结决策到订单计划、SELL/BUY 执行顺序、提交/撤单与期限政策。
- `risk`：只收缩的逐单风控、arm lease、kill switch 与 broker-write capability。
- `reconciliation`：binding、preflight、三类权威之间的资金、持仓、委托、成交和身份对账。
- `persistence`：SQLite migration、事务 repository、单 writer、账户权威证据、恢复、备份和 hash-chain audit。
- `scheduling`：Asia/Shanghai session、时钟校验和可恢复 workflow receipt。
- `observability` / `security`：结构化日志、报告、告警、secret provider、脱敏与扫描。

## 进程与并发模型

Broker callback 线程只验证并投递到有界队列；一个 writer 线程串行规范化事件并推进订单和数据库事务。SQLite writer
lease 同时使用 OS 文件锁和带 generation/到期时间的数据库租约。第二实例无法获得写权限。

当前不启用 SDK bridge。只有部署机实际证明官方 SDK 与 Python 3.12 不兼容时，才允许增加经过认证、版本化、仅
loopback 的本机 bridge；bridge 不能包含策略、组合或风控决策。

## 安全失败方向

连接异常、事实缺失、时间/数据漂移、SQLite 异常、提交结果不确定、外部订单或账户差异都会减少权限：阻止/缩小/
延迟订单、撤销系统未成交单或进入 HALT。系统没有“为了继续运行”而覆盖 manifest、自动接纳人工交易或自动清仓的
路径。

相关记录：[订单执行](EXECUTION.md)、[风险与实盘安全](RISK_AND_SAFETY.md)、
[SQLite 单 writer ADR](decisions/0002-sqlite-single-writer.md)。
''',
"docs/OPERATIONS.md": r'''# 运行与本机运维

firmquant 的控制面是本机 CLI。操作命令返回稳定 reason code，`--json` 可供本机脚本读取；命令输出和审计 payload 均
经过统一脱敏。仓库不默认开放 HTTP 端口。

## 一次性账户初始化

真实券商账户在首次生产运行前必须完成一次 `bootstrap-account`。该命令只读取券商账户事实，不提交或撤销订单，并要求：
运行状态为 DISARMED、无活动 arm lease、无既有策略决策/系统订单/成交、无未完成账户事务且尚未存在 account binding。

券商为空持仓时，系统以可用现金创建严格 uquant `AccountState.empty`，并写入当前锁定 code/data identity；券商已有持仓时
必须显式提供 `--account-state`，且 seed 必须通过当前 uquant schema、code/data identity、现金、持仓、可卖数量和经济摘要
逐项严格校验。任何不一致都在生产 AccountState 写入前失败；系统不从券商事实猜测 lifecycle、tranche、attribution 或策略来源。

bootstrap 使用持久 PREPARED operation 作为写前证据。若进程在 AccountState 保存前退出，下一次同一 bootstrap 会复用同一
operation；若文件已经原子保存但 SQLite finalization 尚未完成，重启先验证文件 hash，再在单一 SQLite transaction 内完成
account binding、binding audit、operation final state 和 bootstrap audit，不重新覆盖账户文件。已完整绑定的账户再次 bootstrap
会拒绝覆盖。

## Session 生命周期

启动获取单实例锁，验证 Asia/Shanghai 时区和时钟，连接 broker，查询完整账户/持仓/委托/成交，并运行启动恢复与对账。
只有持久 account binding 存在，且不存在 UNKNOWN、外部活动、账户差异和身份漂移时才能进入 READY。

每次生产账户采纳都固定执行：完整 broker snapshot → 加载持久 binding 与当前 uquant AccountState → preflight 检查账户身份、
外部/未知订单成交和未解释经济差异 → 仅在深拷贝上 prepare 已知系统事实 → 对 prepared AccountState 做完整 reconciliation →
全部通过后以 expected-before CAS 提交 AccountState、account operation receipt 和 reconciliation receipt。任一检查失败都不会
先修改生产 AccountState，也不会把人工交易或异常现金“同步”为策略事实。

盘后 workflow 验证权威交易日历、市场已收盘、append-only 数据 manifest 和完整券商快照，完成上述账户对账和合法事实采纳
后再调用唯一决策入口。次日 workflow 加载冻结决策，验证交易状态和执行事实后提交有限窗口订单。盘中只处理订单、成交、
断线、风险与对账；EOD 再取完整快照，按同一 prepare-validate-commit 顺序确认合法成交、生成报告并备份。

`SessionCoordinator` 和 workflow receipts 提供上述可恢复步骤。当前安装版 `run` composition 对 PAPER/REPLAY 完成启动
对账并返回 READY 证据；官方 SDK 未验证时 XtQuant 模式明确失败关闭。部署方不能把该失败改成跳过对账。

## 日常 PAPER 操作

1. 使用示例配置创建未跟踪的本地配置；
2. 执行 `init`，确认目录、SQLite 和审计链初始化成功；
3. 执行 `doctor`，所有检查通过后才执行 `run`；
4. 使用 `status` 观察 blocker，使用 `decisions`、`orders`、`fills` 查看持久化证据；
5. 收盘后使用 `reconcile`、`report` 和 `backup`，再定期用 `verify-backup` 验证恢复。

快速命令仅在 [README](../README.md) 给出，那里只包含 PAPER，不提供可直接复制的实盘启动链路。

## 状态与阻断处理

`status` 输出 mode、runtime state、arm 到期时间、firmquant/uquant commit、strategy session、broker connection、最近
quote/对账、unresolved orders、现金、实际/目标 gross、kill switch 和所有 blocker。

- `DEGRADED`：读取或 freshness 下降，系统减少权限；先恢复事实源并对账。
- `HALTED`：新增订单被拒绝；保留数据库、日志、事件文件和账户文件，不手工删除现场。
- `UNKNOWN` / `SUBMITTING_UNRESOLVED`：查询券商委托与成交，禁止重发同一经济意图。
- 外部人工订单或人工账户变化：停止新增订单，导出报告，由操作员明确调查；系统不自动采纳。
- 公司行动/持仓差异：没有精确 reviewed evidence 时阻断；即使有持仓类 reviewed receipt，也必须提供显式 reviewed AccountState，不能把 receipt 当作“忽略差异”。
- 身份/数据漂移：恢复锁定源码、配置或 append-only 数据，不能直接改摘要。

`resume` 只请求状态恢复，仍需交互确认、重新对账和全部 blocker 消失。`disarm` 不等同于解决订单不确定性。

## 报告、日志与告警

结构化 JSON 和 console 事件包含 timestamp、session、correlation/decision/execution/uquant/broker order id、symbol 和
severity。Notifier 支持 console、file 和可选 HTTPS webhook；webhook 失败只生成安全告警，不回滚订单事务。

日报同时保存 JSON 和 Markdown，包含资金持仓、目标/实际差异、完整订单 lifecycle、成交/费用/滑点、未成交/拒单、风险、
对账和健康状态。缺失下一开盘价等参考事实会明确显示为空，不伪造指标。报告 receipt 写入 hash-chain audit。

## 停止与事故

正常停止进入 STOPPING，断开 broker 后保留 READY/停止证据。紧急情况使用 kill switch，优先取消系统未成交 BUY 并
HALT；不要在券商状态未知时手工重复提交或自动市价清仓。恢复流程见 [RECOVERY.md](RECOVERY.md)。
''',
"docs/RECOVERY.md": r'''# 故障恢复

恢复原则是先证明事实，再恢复权限。券商调用返回异常、进程退出或 callback 缺失都不能证明券商没有接单；任何无法安全
分类的状态进入 UNKNOWN/HALT，禁止盲目重发。

## 启动恢复顺序

1. 获取 OS 文件锁与 SQLite writer lease；
2. 打开数据库，验证 quick/integrity check、foreign keys、schema 和审计 hash chain；
3. 验证 uquant AccountState 文件、account binding、bootstrap operation 和未完成 account operation；
4. 连接 broker 并查询完整委托和成交，而不是等待 callback；
5. 将 SUBMITTING、CANCEL_REQUESTED 和 UNKNOWN 与 broker order/fill 逐一匹配；
6. 应用合法迟到事实，运行资金、持仓、委托、成交和身份对账；
7. 只有所有矛盾解决后进入 READY。

恢复服务从不调用 submit/cancel。找不到订单且不能证明未接单时保持 UNKNOWN；找到唯一且身份一致的订单时应用券商事实；
多重匹配、字段不一致或本地终态与券商活动态矛盾都要求调查。

## 订单崩溃边界

| 崩溃点 | 持久证据 | 重启行为 |
|---|---|---|
| 意图保存前 | 无经济订单 | 不创建/重发；等待 workflow 明确重建 |
| SUBMITTING 后、broker 调用前 | intent + command + attempt | 查询 broker；未证明前 UNKNOWN |
| broker 接受后、broker id 落库前 | 本地 SUBMITTING | 以 uquant client order id 和固定字段匹配 |
| 部分成交 callback 事务前 | broker 有 fill，本地可能缺失 | query fills 幂等补齐 |
| 撤单请求后、确认前 | CANCEL_REQUESTED | 查询委托与成交，不重复撤单 |
| CANCELLED 后迟到成交 | 终态 + 新 fill | 保留 fill，标记异常调查，不静默覆盖 |

重复 callback、整段事件重放和进程重启不会增加经济订单或重复 fill。事件身份碰撞、填单总量超限或状态非法回退失败关闭。

## AccountState prepare/commit 协议

broker sync 的 prepare 阶段只在深拷贝上执行 uquant account-sync，不创建账户事务、不写生产文件、不写 reconciliation receipt。
只有 preflight 与 prepared-state final reconciliation 全部通过后，才创建带 expected-before/expected-after、broker evidence 和
reconciliation finalization payload 的 account operation，再以 CAS 原子替换 uquant AccountState 文件。

文件写入成功后，account operation receipt、account-operation audit 和 reconciliation receipt 在同一个 SQLite transaction 内
finalize。若进程在文件已经替换、SQLite finalization 尚未提交之间退出，重启根据 operation 中封存的 finalization payload
验证 expected-after 文件并补齐整组 SQLite receipt；若文件仍等于 before 且 operation 仍 PREPARED，则保持 retry-required，
不会把未应用操作伪装成已提交。文件 hash 既不是 before 也不是 expected-after、payload 身份冲突或证据损坏都会进入
CONTRADICTION/HALT。

相同 broker snapshot / prepared identity 的重试复用同一 operation；已经完成的提交是幂等的，identity 相同但 payload 不同
失败关闭。系统不会为了“恢复”而覆盖未知账户文件。

## Bootstrap 恢复

一次性 `bootstrap-account` 也使用持久 PREPARED operation。崩溃发生在 AccountState 保存前时，重跑重新构造并严格验证候选
AccountState，只有其 account hash、账户身份及 code/data identity 与原 operation 完全一致才继续；不会插入第二个 operation。

崩溃发生在 AccountState 已经保存但 binding 尚未 finalize 时，重跑首先验证现有文件 hash 必须等于 operation 中的
expected account hash，然后在单一 SQLite transaction 内写 account binding、binding audit、BINDING_COMMITTED 和 bootstrap
audit。该恢复路径不再次保存或覆盖 AccountState。任何不匹配都失败关闭；完整 binding 已建立后再次 bootstrap 也拒绝覆盖。

## SQLite 异常

- 临时锁：事务失败，不调用 broker write；释放锁后从持久证据重新恢复。
- 损坏：`DatabaseCorrupt`，原文件原地保留，不自动重建空库。
- 第二实例：OS lock 或未到期 database lease 拒绝新 writer。
- migration：按 schema version 顺序、事务化且可重复；未知/未来 schema 拒绝打开。
- audit mismatch：停止运行，不重写 hash chain。

## Broker/客户端重启

断线超过安全阈值 HALT；重连后先执行完整查询和 reconciliation。新的 event watermark、迟到成交、外部订单和持仓变化
必须得到解释。客户端重启不会让本地假设 broker 已清空。

## 备份与验证

备份使用 SQLite online backup 生成一致性数据库，连同 manifest 和可选 uquant AccountState 写入临时 bundle；全部摘要、
schema、审计链和账户文件验证后原子发布。`verify-backup` 在隔离临时目录恢复，不覆盖生产文件。备份失败或验证失败不删除
源数据，也不能作为继续交易的理由。

恢复后使用 [运行指南](OPERATIONS.md) 的 `status`、`reconcile` 和报告证据复核；实盘恢复还需重新 arm。
''',
"docs/STRATEGY_INTEGRATION.md": r'''# uquant 策略集成

firmquant 将 uquant 视为锁定、不可改写的生产内核。依赖 commit、tree、依赖锁、生产源码面、默认配置 fingerprint、
canonical universe seal 和确定性 wheel 摘要记录在 [SOURCE_BASELINE.md](SOURCE_BASELINE.md)，机器可读副本位于
`src/firmquant/resources/source_identity.json`。

## 唯一决策路径

策略决策只允许调用一次 `ProductionEngine.decide()`。`StrategyAdapter` 的职责仅是：

1. 验证 uquant checkout/安装包、代码、配置、数据和 universe 身份；
2. 使用已经通过账户权威校验并提交的 uquant AccountState 与完整 broker snapshot；
3. 调用传入的唯一 ProductionEngine；
4. 原样捕获结果和账户经济状态，构造不可变 DecisionSnapshot；
5. 对相同 session 与相同输入生成稳定 decision id，拒绝覆盖输入变化后的旧快照。

adapter 不实现第二套 ProductionEngine、PortfolioAllocator、Risk、Sentinel 或策略状态机，也不对经济结果做近似转换。

## AccountState 边界

uquant AccountState 是策略经济状态和持仓 lifecycle 的唯一权威。首次真实账户接入通过 `bootstrap-account` 建立持久 account
binding；空持仓账户可以从券商可用现金创建 `AccountState.empty`，非空账户必须提供严格当前 schema 的已复核 seed。seed
必须与锁定 code/data identity、券商现金、持仓、可卖数量和经济摘要一致，firmquant 不推断 tranche、attribution 或 lifecycle。

之后的 broker sync 是显式 prepare-validate-commit 协议。生产路径先加载当前 AccountState 和持久 binding，对 broker snapshot、
operational ledger 和当前策略账户做 preflight；只有已知、映射一致且已入账的系统订单/成交可以解释差异。prepare 只对深拷贝
调用锁定 uquant account-sync，生产文件和 account-operation ledger 在该阶段保持不变。prepared AccountState 还必须再次通过完整
reconciliation；全部通过后才以 expected-before CAS 提交。

AccountState 文件替换与 SQLite 不是伪装成单一物理事务，而是 crash-consistent finalization：account operation 封存 before/after、
broker evidence 与 reconciliation finalization payload；文件原子保存后，在一个 SQLite transaction 中完成 account operation
receipt、audit 和 reconciliation receipt。重启可由同一 evidence 收敛，不重新执行经济行为。

券商持仓差异不能被自动改造成新的策略 lifecycle。`ReviewedAccountAdjustment` 只允许对精确账户、symbol/session、broker
snapshot 和 difference hash 留下 append-only 人工复核证据。精确现金差异可被授权；持仓或可卖数量差异即使有 reviewed
receipt，仍要求显式 reviewed AccountState，receipt 本身不能成为忽略差异或生成新 tranche 的开关。

Operational Ledger 只记录 broker id、订单尝试、事件、成交、对账和运行控制，不保存另一份策略目标或策略参数。

## DecisionSnapshot

快照绑定 strategy session、decision id、firmquant/uquant commit、uquant code/config fingerprint、data/universe
manifest、broker snapshot、账户前后摘要、opportunity、risk、sentinel、targets、pending orders 和 reason codes。
payload 使用 canonical JSON 和 SHA-256 封存；repository 只追加，不允许 UPDATE/DELETE。

次日执行必须加载前一交易日的冻结快照，不在盘前重新运行策略。若账户、身份或关键事实与快照前提发生实质变化，执行
进入 HALT 或等待下一次合法盘后决策，不能静默重算。

## Universe 与策略参数

canonical AI universe 和点时成员由 uquant manifest 独占。部署 allowlist 只能是它的子集；不在 canonical universe
内的证券永远不能获得 submit authority。

组合上限、策略风险上限、持仓数量、行业约束、流动性参与率和战略保留语义均从锁定的 uquant 配置或决策结果读取。
firmquant 文档与配置不维护第二份策略数值默认值。CANARY caps 是更严格的部署安全参数，不是策略优化参数。

## 运行形态与上游缺口

锁定 wheel 可复现，但缺少 uquant 生产源码 fingerprint registry 与 reference registry。为避免复制策略资源，
StrategyAdapter 只允许在精确验证、干净且锁定 commit 的 uquant source checkout 中执行策略，并验证 engine 的模块来源。
缺口、复现和希望上游提供的接口详见 [UPSTREAM_GAPS.md](UPSTREAM_GAPS.md)。

## 等价性证据

parity 测试在同一 commit、配置、数据、universe、账户、broker snapshot 与 as-of 下，对 adapter 和直接调用 uquant
比较完整 opportunity、risk、sentinel、targets、pending orders、reason codes、账户经济状态和 fingerprints。比较为
严格相等，不使用容差掩盖经济差异。
''',
}

for name, content in files.items():
    Path(name).write_text(content, encoding="utf-8")
