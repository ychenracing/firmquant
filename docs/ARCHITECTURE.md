# 架构与权威边界

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
