# 订单执行合同

执行层消费冻结 DecisionSnapshot，不拥有目标组合。它只能把 uquant 经济订单意图转换为数量不更大的、具有价格保护和
明确截止时间的券商命令。

## 计划与顺序

次日执行前重新查询账户、可卖数量、未完成订单、instrument、quote 和 market status，并验证这些事实与决策前提。
默认不使用集合竞价，不使用无价格保护市价单；订单类型必须由券商明确支持。

执行顺序固定为风险缩减 SELL 优先。BUY 只使用当时真实可用现金，不预支尚未成交的 SELL 收益；SELL 实际成交后才按
确定性规则重新计算可买股数。缩量原因写入 outcome 和报告，不修改目标权重。

新增 BUY 只允许在有限执行窗口内，风险缩减 SELL 使用独立的有限窗口。达到截止时间后按 policy 取消、保留或过期，
提交/撤销/替换次数均有上限，不无限追价或循环重试。

## 经济身份与幂等

uquant `order_id` 标识经济意图；firmquant 为一次执行生成稳定 `execution_id` 与 `idempotency_key`。key 绑定 decision、
uquant order、symbol、side、请求股数、strategy session 和 uquant source SHA。同一 decision/order 组合在数据库受
唯一约束保护，重启不会创建第二个经济订单。

## Durable 状态机

合法主路径为计划、校验、armed、提交中、broker 确认、部分/全部成交；另有撤单请求、撤销、拒单、过期和 UNKNOWN。
终态不能非法回退，但终态后迟到成交仍作为新事实进入异常调查，不能被丢弃或静默覆盖。

任何 submit/cancel 前，repository 先在事务中写入命令、attempt 与 `SUBMITTING`/`CANCEL_REQUESTED`。本地调用异常只
说明结果未知，不说明券商未接单；aggregate 进入 UNKNOWN，阻止后续订单。

## Callback 与成交

券商 callback 按 at-least-once 处理：callback 线程仅验证、规范化并放入有界队列，单 writer 在事务中写原始 broker
event、domain event、订单状态和 fill。重复事件不改变经济结果，乱序事件不能倒退状态，fill 数量不能超过委托数量。

callback 丢失、队列溢出或 writer 失败都会保留失败 envelope、触发 HALT 并要求查询券商恢复。对账使用 broker order
和 fill 查询补齐事实，而不是依靠 callback 完整性假设。

## 撤单与紧急行为

撤单也是可能产生 UNKNOWN 的真实写操作，必须经过相同 capability、arm、身份、健康、频率和风险门禁。kill switch
优先阻止新订单并请求取消系统拥有的未成交 BUY；是否请求取消其他系统订单由安全 policy 决定。默认不自动平仓。

故障语义详见 [恢复](RECOVERY.md)，逐单门禁详见 [风险与实盘安全](RISK_AND_SAFETY.md)。
