# 风险与实盘安全

firmquant 风控是执行安全层，不是策略风险模型。Base Risk 派生 target gross cap，PortfolioAllocator 形成目标组合，Risk Sentinel 保持 FREEZE_ONLY；这些经济职责全部属于 uquant。

## 只收缩原则

ExecutionRiskGate 的结果只有 ALLOW、SHRINK、DELAY、BLOCK、HALT。授权股数同时不超过 uquant order intent、目标缺口、真实可卖/持仓、可用现金、交易单位、流动性和部署上限。任何输入缺失或矛盾都不能产生更大的订单。

本机生产控制面同样只允许风险缩减操作：HALT、DISARM、CANCEL_SYSTEM_ORDERS、STOP。控制请求不能指定 symbol、broker order id、目标权重，也不存在 submit、改单、追价或策略状态修改命令。

## 逐单事实

门禁覆盖以下类别：

- canonical AI universe 与部署 allowlist；
- 现金账户、禁止融资融券/做空、负现金和负持仓；
- uquant 目标、gross cap、freeze-new-risk 与实际持仓；
- 可卖数量、T+1、交易单位、证券风险状态、停牌；
- broker instrument/quote 给出的价格 tick、上下限、市场阶段和 freshness；
- 单笔/日累计/单票/总敞口、未完成订单、拒单、断线持续时间、已有订单年龄和 submit/cancel 频率；
- uquant 成交量参与率、账户权益异常、日内损失与回撤警戒；
- 启动/盘中对账、外部人工订单、未解释持仓、疑似公司行动；
- 时钟 receipt、数据/配置/代码身份、UNKNOWN、kill switch。

系统不硬编码交易所涨跌停比例。instrument、upper/lower limit 或 market status 缺失时停止交易，不根据板块历史经验猜测。所有金额、价格和费用使用 Decimal；与 uquant float 边界只接受有限值并执行精度/容差校验。

生产风险事实不允许用“有利占位值”填空。broker 断线持续时间、已有订单年龄、reconciliation mismatch、未解释持仓、公司行动和 clock drift 必须来自 broker、ledger、reconciliation 或 ClockGuard 的实际证据；事实缺失时失败关闭。当前产品没有 replacement/reprice 功能，因此相关无效配置与风险字段已删除，而不是保留 `replacement_count=0` 一类恒真默认值。

## 模式与写权限

| 模式 | 真实读取 | 真实 submit | cancel-only 真实撤单 | 安全用途 |
|---|---:|---:|---:|---|
| REPLAY | 否 | 永不 | 永不 | 事故复盘与确定性回归 |
| PAPER | 否 | 永不 | 永不 | 模拟执行与完整 session |
| SHADOW | 是（部署前提满足时） | 永不 | 永不 | 真实环境只读观察和订单计划 |
| CANARY | 是 | 多重门禁后有限 | 仅 SYSTEM 活动订单 | 使用无默认值部署 caps 的受限验证 |
| LIVE | 是 | 多重门禁后有限 | 仅 SYSTEM 活动订单 | 执行完整 uquant 意图，仍只收缩 |

CANARY 不自动升级 LIVE；模式必须与配置一致。当前 XtQuant 真实环境未验证，相关模式在安装组合中失败关闭。

## WriterLease 与 arm lease

WriterLease 是进程级唯一写权威，不是普通 TTL 缓存。续租必须在同一 EXCLUSIVE transaction 内验证 owner、host、pid、generation 和 stored `expires_at`；当前时间达到或超过 stored `expires_at`、generation 被其他 owner 取代或 CAS rowcount 不为 1 时立即失租。旧 lease 对象不能在系统暂停或睡眠恢复后复活。

长时间执行由 lease guard/keepalive port 复用同一 WriterLease 实现；controller 不复制 lease 算法。submit、poll、cancel 和状态提交前都重新检查 lease，达到续租间隔时续租。失租后所有新增 submit 被禁止；已有 SYSTEM 活动订单仅可按受控 cancel-only/UNKNOWN 语义缩减风险，随后进入 HALT 与恢复流程。

`arm-live` 只能在交互终端执行，在 CI 中拒绝。确认短语通过 getpass 临时读取，不写日志；lease 默认短时且最长受 CLI 限制。lease 通过 MAC 认证并绑定 host、账户、mode、firmquant commit、uquant source SHA 和配置摘要。配置或代码变化后旧 lease 失效，不能用环境变量永久解锁。

正常策略 submit 以及执行窗口内的普通 cancel 仍通过 `BrokerWriteCapability`。每次写操作重新构造最新 WriteAuthorizationContext，并检查 mode、live flag、lease、合规、broker 健康、启动对账、snapshot/quote freshness、session/market status、fingerprints、kill switch、UNKNOWN、外部活动、逐单 gate、现金/持仓、频率和最新 ClockReceipt。capability 不是启动时一次性通行证。

## 时钟事实与时间围栏

wall clock 负责交易日、session 和市场时间；monotonic clock 负责持续时长、poll interval、订单生命周期和组合 deadline。ClockGuard 只接受明确 `ClockReferenceProvider` 提供的可信参考时间。真实模式没有可信参考时间或券商 quote event time 证据时，doctor、write authorization 与 ExecutionRiskContext 必须进入 `CLOCK_DRIFT_UNVERIFIED`，不能硬编码零漂移。

runtime 维护 wall/monotonic observation。检测到 sleep/resume gap、wall clock 回拨或两者差异异常时，撤销 arm、阻止新增订单并要求重新启动恢复与对账。时间跳变不是可自动忽略的零漂移事件。

## cancel-only capability

紧急安全撤单使用独立 `BrokerCancelOnlyCapability`，类型上没有 `submit_order`。调用者不提供 broker order id、symbol 或订单集合；capability 只从 operational ledger 内部选择同时满足以下条件的候选：ownership=`SYSTEM`、execution/broker/client identity 已知、本地 aggregate 仍活动、broker ledger 状态仍活动。EXTERNAL、MANUAL、UNKNOWN ownership、无法映射或已经 terminal 的订单永远不进入真实 cancel 调用。

cancel-only 的目标是缩减已经存在的 broker 风险，因此与新增风险授权不同：HALTED、kill switch 已触发、arm 已过期或已撤销时仍允许安全撤单；它不要求新行情、quote freshness、目标权重、market status 或新的策略决策。它仍必须在每笔 cancel 前重新验证：broker 已连接且 read health 可用、当前账户与持久 account binding 完全一致、broker order id 唯一、client id/symbol/side/requested shares/limit price/session 没有身份漂移、累计成交不回退、订单未终结。

每笔 cancel 先在 SQLite 中 durable 写入 `CANCEL_REQUESTED` 和 broker attempt，再跨越 broker 写边界。返回后把规范化 broker fact、confirmed fills 和结果写回同一 monotonic order repository。网络异常、超时、SDK 异常、不完整响应或无法证明 cancel acceptance 时，attempt 进入 UNKNOWN；该 aggregate 随即不再是 cancel-only 候选，因此同一命令、重启或重复 request id 都不会盲目再次 cancel。之后只能沿用既有 UNKNOWN 查询恢复。

PAPER、REPLAY、SHADOW 的 cancel-only 在结构上返回 write-forbidden/零真实调用，不构造真实 broker write。测试、CI 和 Windows deployment smoke 继续固定 `real_order_calls=0`。

## HALT、DISARM 与 STOP

HALT 是最高优先级的本机控制：daemon 在 broker callback、策略执行和其他工作前处理它，立即阻止后续 submit，撤销 active arm lease，并持久化 HALTED 与 `KILL_SWITCH` blocker。HALT 本身不自动撤单，也不自动市价清仓；需要降低活动委托风险时显式调用 `cancel-system-orders`。

DISARM 只撤销新的实盘授权并推进安全运行状态，不伪造订单终态，不把 UNKNOWN 标记为 CANCELLED。STOP 停止新增工作、撤销 arm、持久化 STOPPING；只有 broker 断开完成后才进入 DISARMED。SIGINT/SIGTERM 只设置内存停止标志，实际 STOP 状态推进发生在单 writer daemon 循环内。

`resume` 需要交互确认和重新对账，不能清除 UNKNOWN、外部订单、身份漂移、账户差异、lease loss 或 clock discontinuity。数据库损坏、writer lease 冲突、控制请求身份错误和审计链失败直接阻止运行。紧急默认不是自动清仓。

合规前置条件见 [COMPLIANCE.md](COMPLIANCE.md)，操作流程见 [OPERATIONS.md](OPERATIONS.md)。
