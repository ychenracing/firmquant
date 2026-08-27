# 订单执行合同

执行层消费冻结 DecisionSnapshot，不拥有目标组合。它只能把 uquant 经济订单意图转换为数量不更大的、具有价格保护和明确截止时间的券商命令。

## 计划与顺序

次日执行前重新查询账户、可卖数量、未完成订单、instrument、quote 和 market status，并验证这些事实与决策前提。默认不使用集合竞价，不使用无价格保护市价单；订单类型必须由券商明确支持。

执行顺序固定为风险缩减 SELL 优先。BUY 只使用当时真实可用现金，不预支尚未成交的 SELL 收益；SELL 实际成交后才按确定性规则重新计算可买股数。缩量原因写入 outcome 和报告，不修改目标权重。

新增 BUY 只允许在有限执行窗口内，风险缩减 SELL 使用独立的有限窗口。达到截止时间后按 policy 请求撤单或保留当前事实；不无限追价，不自动放宽价格，不循环重试。

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

## 撤单与紧急行为

撤单也是可能产生 UNKNOWN 的真实写操作，必须经过相同 capability、arm、身份、健康、频率和风险门禁。cancel 返回活动态或返回信息不足时，不能推断撤单成功；系统保持 UNKNOWN，查询 broker 后再恢复。

kill switch 优先阻止新订单并请求取消系统拥有的未成交 BUY；是否请求取消其他系统订单由安全 policy 决定。默认不自动平仓，不实现激进自动重试、追价或替换循环。

故障语义详见 [恢复](RECOVERY.md)，逐单门禁详见 [风险与实盘安全](RISK_AND_SAFETY.md)。
