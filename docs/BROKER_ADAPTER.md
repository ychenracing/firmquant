# BrokerGateway 与适配器

BrokerGateway 是策略无关、严格类型化的券商端口，覆盖连接、健康、账户、持仓、委托、成交、证券元数据、行情、市场状态、提交、撤单和 callback 订阅。所有适配器返回相同领域事实；券商真实委托状态不会被适配器压缩成较少的“成功/失败”类别。

## 不可信输入边界

规范化层验证证券代码与市场、账户类型、方向、价格类型、委托/成交状态、broker id、事件序号、session date、带时区事件时间、价格精度、股数、可卖数量、费用和税费。金额与价格使用 Decimal 值对象，数量使用非负整数股数。

canonical broker order status 为 `PENDING_NEW`、`ACKNOWLEDGED`、`PARTIALLY_FILLED`、`FILLED`、`PENDING_CANCEL`、`CANCELLED`、`REJECTED`、`EXPIRED`、`UNKNOWN`。适配器只能在券商原始事实足以证明时映射到具体状态；无法安全解释的值必须成为 UNKNOWN 或拒绝规范化，不能把拒单/过期伪装成撤单。

同一 broker event id 只能对应同一类型与 payload 摘要；精确重复幂等，身份碰撞失败关闭。confirmed fill 还必须具有稳定 `broker_fill_id`、broker order identity、session、execution sequence 和完整经济字段。同 fill id/经济内容/sequence 重复到达幂等；id 被复用于不同经济内容、sequence 回退或累计成交量矛盾时 HALT。原始 payload hash 单独保存为审计证据。

## 已实现适配器

| 适配器 | 连接面 | 写能力 | 当前验证状态 |
|---|---|---|---|
| FakeBroker | 测试内存事实 | 可脚本化 | 拒单、超时、断线、重复、乱序、终态与恢复 contract tests |
| PaperBroker | 冻结/模拟行情 | 仅模拟 | 部分成交、滑点、费用、停牌、价格边界、流动性、T+1 与 durable write-ahead tests |
| RecordedReplayBroker | JSONL 冻结证据 | 永久只读 | 排序、重复、身份碰撞、确定性与 crash/restart 等价 tests |
| XtQuantBroker | 官方 MiniQMT/XtQuant SDK | 由 capability 包装 | raw status/fact anti-corruption contract tests；官方 SDK/真实账户 smoke 未完成 |

REPLAY、PAPER 和 SHADOW 的真实 broker write 在结构上被拒绝。真实 submit/cancel 只能通过 `BrokerWriteCapability` 包装真实 gateway；直接构造写 scope 不属于公共接口。恢复服务只使用 health/query 等只读能力，不调用 submit/cancel。

## PaperBroker 语义

PaperBroker 使用 instrument/quote 的交易单位、价格边界、停牌和市场状态；FillModel 控制参与率与确定性滑点，FeeSchedule 计算佣金、印花税和过户费。部分成交、撤单与迟到回调经过与实盘相同的 durable state machine。回调投递失败后 broker 阻止新写，直到事实被重新查询/重放。

## RecordedReplayBroker 文件

录制为一行一个 canonical JSON object：首行 STATE，后续为 EVENT。解析拒绝重复 JSON key、binary float、未知字段、过大文件、事件身份碰撞和不带时区的时间。事件按 event time、broker sequence、event id 稳定排序；重复行保留并由下游幂等处理。适配器的 submit/cancel 永远抛出 write-forbidden。

## XtQuant 状态

XtQuant 使用 lazy import，仓库不包含专有 SDK、来源不明 wheel、客户端文件或 userdata。raw order status 在 anti-corruption layer 中映射为 firmquant canonical status；无法识别的 raw status 保持 UNKNOWN。券商原始 order/trade 时间、id 和 payload 摘要保留用于审计，firmquant 不把“报单时间”误解释为券商生命周期状态本身。

XtQuant adapter mapping 依据测试用官方签名 contract，而不是网络旧样例；缺少 SDK、账户绑定、instrument/quote 安全事实或只读查询能力时返回明确诊断。当前运行环境没有官方 SDK，未执行 MiniQMT import/schema 的本机实测，也未完成资金、持仓、委托、成交的真实账户只读 smoke。安装版 composition 因此前置条件不足而拒绝 XtQuant runtime，不能声称 SHADOW 或实盘已接通。部署步骤见 [Windows 本地部署](DEPLOYMENT_WINDOWS.md)。任何 smoke 都不得发送小额真实订单。

## 写结果未知与非接单证明

submit/cancel 调用异常不能由 adapter 翻译成“未接单”。网络中断、超时、SDK 异常和不完整返回都由执行层标记 UNKNOWN。普通 `query_orders()` 未找到订单也不是 NOT_ACCEPTED 证明。

如果某 broker adapter 能提供明确、权威、绑定同一 durable command 的“未接受订单”证明，可通过专门的只读 proof contract 提供；否则恢复保持 UNKNOWN/HALT。该能力不是自动重试接口，也不能用于追价或重新提交。

## 新适配器约束

未来增加适配器必须通过共享 BrokerGateway contract tests、完整状态/成交身份测试、callback at-least-once 测试和 crash/restart 查询恢复，并证明 read-only 模式绝不触达写 API。当前范围不实现 PTrade、XTP、多券商路由、智能路由或自动追价。
