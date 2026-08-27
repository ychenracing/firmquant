# 架构与权威边界

firmquant 采用模块化单体和端口适配器：一个进程、一个 SQLite operational ledger、一个账户 writer lease。该结构适合
单账户、日频、Windows 本地券商终端场景，也使订单状态和审计事务保持在同一一致性边界内。

## 数据流

```mermaid
flowchart LR
    B[券商与行情事实] --> N[严格规范化]
    N --> S[完整 BrokerSnapshot 证据]
    S --> P[持久 AccountBinding + Preflight]
    P --> A[deep-copy uquant AccountState candidate]
    A --> C[完整 Reconciliation]
    C --> K[expected-before CAS + account/reconciliation receipts]
    K --> U[ProductionEngine.decide]
    U --> D[不可变 DecisionSnapshot]
    D --> R[ExecutionRiskGate]
    R --> O[Durable Order State Machine]
    O --> G[BrokerGateway]
    G --> E[委托与成交事件队列]
    E --> W[单 writer 事务推进]
    W --> L[Operational Ledger / 报告 / 审计]
```

盘后决策和次日执行是两个独立、可恢复的 workflow step。盘中行情只供执行与安全检查，不能进入日频策略输入。盘中或
EOD 新取得的完整券商快照重新进入同一 binding/preflight/reconciliation 边界，不能绕过该边界直接修改 AccountState。

## 三类权威

| 权威 | 独占职责 | 明确不拥有 |
|---|---|---|
| 券商 | 可用现金、总资产、真实/可卖持仓、broker order/fill id、委托成交、费用、实时证券状态 | 策略目标、策略 lifecycle |
| uquant | 机会、风险、Sentinel、目标组合、策略持仓 lifecycle、经济 order id、策略配置与数据身份 | 在线连接、broker id、重试和告警 |
| firmquant | broker 映射、提交尝试、回调、UNKNOWN、对账、arm lease、kill switch、运行健康和审计 | 第二策略账户、目标组合、策略参数 |

券商事实不能反向“猜出”uquant lifecycle，firmquant operational ledger 也不能成为第二个经济账户。任何权威间差异都先
保留证据并 HALT，由操作员依据明确事实处理。

## 账户权威与提交边界

真实账户身份来自一次性、不可变的 AccountBinding，而不是历史 BrokerSnapshot。生产流程不存在“没有历史快照就采用当前
账户”的隐式绑定；未绑定、账户 id/type 改变或 binding 与快照不一致都会在 AccountState 变更前失败关闭。

账户同步拆为 prepare 与 commit：prepare 只在 uquant AccountState 的 deep copy 上调用公共 account-sync 语义，不创建
account operation、不写 SQLite receipt，也不修改生产账户文件。preflight 只允许已由 operational ledger 证明归属且身份
一致的系统订单/成交解释差异；人工订单、未知成交、异常现金和无法解释的持仓变化都会阻断。

候选 AccountState 形成后还必须执行完整 reconciliation。只有 reconciliation 通过，才使用 recorded before hash 做 CAS
提交。账户文件原子保存后，account-operation receipt 与 reconciliation receipt 在同一 SQLite finalization 事务中提交；若
进程恰在文件落盘后退出，durable canonical reconciliation evidence 允许 recovery 幂等补齐该 finalization。证据缺失、损坏或
identity 冲突时保持 FILE_COMMITTED/HALT，不猜测完成状态。

ReviewedAccountAdjustment 是显式、只追加的操作员复核证据，绑定 account、symbol、session、adjustment type、broker
snapshot 和精确 difference hash。它不是通用 ignore 开关：精确现金差异可以被授权；涉及持仓总数或可卖数量的变化仍不能由
firmquant 生成 lifecycle/tranche/attribution，必须提供已复核且满足 uquant 严格合同的 AccountState。

## 模块边界

- `application`：本机用例、session coordinator、启动/停止和运维命令编排。
- `domain`：值对象、运行/订单状态机、领域事件与不变量。
- `strategy`：锁定身份、uquant anti-corruption adapter、账户 prepare/commit 和 DecisionSnapshot。
- `broker`：稳定 BrokerGateway、Fake/Paper/Replay/XtQuant 适配器和不可信输入规范化。
- `market_data`：权威交易日历、日频 manifest/append-only 验证、执行行情端口。
- `execution`：冻结决策到订单计划、SELL/BUY 执行顺序、提交/撤单与期限政策。
- `risk`：只收缩的逐单风控、arm lease、kill switch 与 broker-write capability。
- `reconciliation`：三类权威之间的身份、资金、持仓、委托、成交 preflight、完整对账及 finalization evidence。
- `persistence`：SQLite migration、账户 binding/复核 receipt、事务 repository、单 writer、恢复、备份和 hash-chain audit。
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