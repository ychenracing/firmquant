# 订单执行合同

执行层消费冻结 DecisionSnapshot，不拥有目标组合。它只能把 uquant 经济订单意图转换为数量不更大的、具有价格保护和明确截止时间的券商命令。

## 计划与顺序

次日执行前重新查询账户、可卖数量、未完成订单、instrument、quote 和 market status，并验证这些事实与决策前提。默认不使用集合竞价，不使用无价格保护市价单；订单类型必须由券商明确支持。

执行顺序固定为风险缩减 SELL 优先。BUY 只使用当时真实可用现金，不预支尚未成交的 SELL 收益；SELL 实际成交后才按确定性规则重新计算可买股数。缩量原因写入 outcome 和报告，不修改目标权重。

新增 BUY 只允许在有限执行窗口内，风险缩减 SELL 使用独立的有限窗口。达到截止时间后按 policy 请求撤单或保留当前事实；不无限追价，不自动放宽价格，不循环重试。

## 时间围栏与 WriterLease

交易日、session 和市场时段使用 wall clock；持续时长、poll interval、单笔订单窗口和组合执行截止统一使用可注入 monotonic clock。wall clock 回拨不能延长一笔订单，也不能通过时间差制造额外 sleep；睡眠恢复和明显 wall/monotonic discontinuity 由生产 runtime 单独检测并转入 HALT/恢复流程。

每个组合执行都具有三个显式 monotonic fence：`latest_new_submit` 是最后允许创建新 broker 风险的时刻；`latest_cancel_initiation` 是最后允许开始正常撤单流程的时刻；`absolute_completion` 是本次组合执行必须结束并返回可恢复事实的硬截止。daemon 只有在剩余安全窗口足以覆盖一笔订单的最小生命周期、poll/cancel 余量时才会开始新单。程序晚启动或错过该交易日安全入场点时，不补执行已经错过的前一日信号。

LiveExecutionController 不拥有 WriterLease 算法，只接受 lease guard/keepalive port。每次 submit 前、每轮 poll、cancel 前和状态提交前都先检查 lease；达到续租间隔时由 guard 调用唯一 WriterLease 续租实现。旧 WriterLease 已到期、generation 被替换或 CAS 失败时 guard 立即失败，controller 不得继续新增 submit。

lease loss 发生在已有 SYSTEM 活动订单期间时，执行器停止新增风险，只允许在剩余安全截止时间内进行一次受控 cancel-only 尝试；无法证明撤单结果时保持 UNKNOWN，由重启恢复和权威 broker query 解析。失租后不能继续用旧 controller 对象写 durable 状态来“证明”自己仍是 writer。

## 券商状态合同

规范化后的券商委托状态完整保留为 `PENDING_NEW`、`ACKNOWLEDGED`、`PARTIALLY_FILLED`、`FILLED`、`PENDING_CANCEL`、`CANCELLED`、`REJECTED`、`EXPIRED`、`UNKNOWN`。这些是真实 broker facts，写入 operational ledger、审计证据和日报；`REJECTED`、`EXPIRED` 不会在 firmquant 内伪装为 `CANCELLED`。

firmquant aggregate 使用自己的 durable 执行状态表达写前、提交中和撤单请求，例如 `SUBMITTING` 与 `CANCEL_REQUESTED`。日报同时显示 aggregate `state` 和真实 `broker_status`，因此活动、终态和未知结果可以独立审计。

`UNKNOWN` 表示不能证明 submit/cancel 是否被券商接受。它不是取消、拒单或未接单，不能投影为已关闭，且会阻断同一经济订单重发和后续风险扩大。

## 经济身份与幂等

uquant `order_id` 标识经济意图；firmquant 为一次执行生成稳定 `execution_id` 与 `idempotency_key`。key 绑定 decision、uquant order、symbol、side、请求股数、strategy session 和 uquant source SHA。同一 decision/order 组合受数据库唯一约束保护，重启不会创建第二个经济订单。

confirmed fill 以 `broker_fill_id`、`broker_order_id`、session、execution sequence 和经济字段校验。相同 fill id 与完全相同经济内容/sequence 重复到达幂等；同一 fill id 复用于不同经济内容、旧 sequence、非法 sequence 或与累计成交量矛盾时失败关闭。原始 payload hash 保留为审计证据，但 payload 字节差异本身不会制造第二笔成交。

## Durable 状态与终态

合法主路径为计划、校验、armed、提交中、broker 确认、部分/全部成交；另有撤单请求、撤销、拒单、过期和 UNKNOWN。已确认终态不能非法回退，但 `CANCELLED`、`REJECTED`、`EXPIRED` 后到达的合法 confirmed fill 仍必须进入 ledger，累计经济结果不能被终态检查静默丢弃；该订单同时标记 late-fill 调查并保持真实 broker 终态。

`PARTIALLY_FILLED + CANCELLED/REJECTED/EXPIRED` 先保留已成交结果，再关闭未成交剩余量。对 uquant 的边界投影固定为：按稳定 execution sequence 导入 confirmed fills，然后对 `CANCELLED`、`REJECTED`、`EXPIRED` 使用 uquant 已有公共取消/关闭语义使 pending remainder 不再活动。该投影只表达“无活动剩余量”，不会改写 firmquant 保存的真实 broker 终态。

任何 submit/cancel 前，repository 先在事务中写入命令、attempt 与 `SUBMITTING`/`CANCEL_REQUESTED`。只有明确的本地或券商证据证明 `NOT_ACCEPTED` 时才允许回到安全活动态；网络中断、超时、SDK 异常、返回不完整或无法证明的查询结果都进入 UNKNOWN。

## Callback 与成交

券商 callback 按 at-least-once 处理：callback 线程仅验证、规范化并放入有界队列，单 writer 在事务中写原始 broker event、domain event、订单状态和 fill。重复事件不改变经济结果，乱序事件不能倒退已证明事实，fill 数量不能超过委托数量。

callback 丢失、队列溢出或 writer 失败都会保留失败 envelope、触发 HALT 并要求查询券商恢复。恢复依赖 authoritative order/fill query，而不是 callback 完整性假设；迟到成交与重复启动使用同一幂等合同。

## 每 session 执行证据

SHADOW 与 CANARY 都以“一交易日一条不可变 execution observation”为事实来源，不保存可继续自增的累计计数。稳定 identity 绑定 stage、execution session、firmquant commit、uquant commit、promotion config hash 和 account hash；data/calendar identity 属于不可变内容。同一 identity 与完全相同内容重跑幂等，同一 identity 出现任何不同内容时失败关闭。

observation 记录 decision id、plan id、计划订单、planning blockers、目标股数/目标权重、成交股数/价格、未成交股数、费用、slippage、实际/假设期末持仓以及 UNKNOWN、外部活动和重复经济事实。聚合器只从这些明细计算 observed sessions、orders、fills、unresolved、external、duplicates、tracking error 和未成交 notional，不把上一条累计值“再加一”。

逐 symbol tracking error 定义为 `abs(target_weight - ending_market_value / portfolio_equity)`；ending market value 使用该 observation 的 reference price 和执行后股数。SHADOW 使用同一 execution policy 的假设期末持仓，CANARY 使用 EOD broker positions。组合报告同时给出 symbol error 的 max、算术 mean 和以 `max(target_notional, actual_notional)` 为权重的 notional-weighted mean。blocker 仅说明执行原因，不能把 tracking error 固定成 1；已满足目标、不可交易、价格限制、volume 限制、quote stale、现金不足、sell 未完成、UNKNOWN 与外部活动分别记录。

SHADOW 的零写模拟器使用实际 broker snapshot/instrument/quote 作为当日执行前事实，在隔离 PaperBroker 中复用生产手续费合同、锁定 uquant volume participation 与 slippage 假设；因此可出现部分成交和未成交。CANARY 则只使用真实持久 submit/cancel、broker order/fill 和 EOD broker positions，SHADOW observation 或 SHADOW READY audit 均不能替代 CANARY evidence。

## Execution-aware Replay

`execution-replay` 是确定性的日频执行模型，不是逐 tick 撮合器。经济决策唯一来自锁定 uquant `ProductionEngine`：交易日 N 收盘生成决策，最早在下一权威交易日 N+1 执行；执行后的现金、持仓、订单和成交以 synthetic broker facts 通过现有 uquant account-sync 公共合同回灌同一个 AccountState，下一次决策因此真实承受执行摩擦。Replay 不实现第二套策略、第二套持仓状态机，也不修改 uquant 参数。

订单 authorization 时只允许看到执行 session 的账户/T+1 可卖状态、instrument 合同和开盘价等当时已知事实，绝不读取当日未来 high/low。授权完成后，已结束交易日的 high/low 只用于事后判断该保护限价是否曾具备成交条件；成交价从 open、保护限价与固定 slippage 规则确定，不能使用未来极值择优成交。volume cap 使用锁定 uquant `max_volume_participation`，BUY 按 100 股交易单位，价格按 0.01 tick；A 股现金多头、禁止杠杆和做空，sell-before-buy，未完成 sell 会阻断依赖卖出资金的 BUY。

普通主板按 10%、创业板/科创板按 20%、北交所按 30% 日涨跌幅边界建模，上市最初五个交易 session 不套用普通日限幅。权威指数开市但单股没有该 session K 线时视为停牌/不可交易，不伪造可成交 K 线；前收只用于估值和边界参考。模型计入佣金、最低佣金、卖出印花税、过户费、slippage、部分成交与未成交，并输出 price-limit/suspension/incomplete-sell blockers。

`unfilled_notional` 定义为未成交股数乘 execution-session open；`unfilled_loss` 是执行事后机会损失：未成交 BUY 取 `max(close-open, 0) × unfilled_shares`，未成交 SELL 取 `max(open-close, 0) × unfilled_shares`。该指标只用于评估执行拖累，不参与 authorization、成交判断或策略决策，因此不会把未来数据反馈进交易路径。

## 撤单与紧急行为

撤单也是可能产生 UNKNOWN 的真实写操作，必须经过相同 capability、arm、身份、健康、频率和风险门禁。cancel 返回活动态或返回信息不足时，不能推断撤单成功；系统保持 UNKNOWN，查询 broker 后再恢复。

kill switch 优先阻止新订单并请求取消系统拥有的未成交 BUY；是否请求取消其他系统订单由安全 policy 决定。默认不自动平仓，不实现激进自动重试、追价或替换循环。产品当前不支持 replacement/reprice 功能，因此配置和风险事实中不存在无效的 replacement count/limit 占位字段。

故障语义详见 [恢复](RECOVERY.md)，逐单门禁详见 [风险与实盘安全](RISK_AND_SAFETY.md)。
