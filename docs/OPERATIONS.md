# 运行与本机运维

firmquant 的控制面是本机 CLI。操作命令返回稳定 reason code，`--json` 可供本机脚本读取；命令输出和审计 payload 均经过统一脱敏。仓库不默认开放 HTTP 端口。

## 一次性账户初始化

真实券商账户在首次生产运行前必须完成一次 `bootstrap-account`。该命令只读取券商账户事实，不提交或撤销订单，并要求：运行状态为 DISARMED、无活动 arm lease、无既有策略决策/系统订单/成交、无未完成账户事务且尚未存在 account binding。

券商为空持仓时，系统以可用现金创建严格 uquant `AccountState.empty`，并写入当前锁定 code/data identity；券商已有持仓时必须显式提供 `--account-state`，且 seed 必须通过当前 uquant schema、code/data identity、现金、持仓、可卖数量和经济摘要逐项严格校验。任何不一致都在生产 AccountState 写入前失败；系统不从券商事实猜测 lifecycle、tranche、attribution 或策略来源。

bootstrap 使用持久 PREPARED operation 作为写前证据。若进程在 AccountState 保存前退出，下一次同一 bootstrap 会复用同一 operation；若文件已经原子保存但 SQLite finalization 尚未完成，重启先验证文件 hash，再在单一 SQLite transaction 内完成 account binding、binding audit、operation final state 和 bootstrap audit，不重新覆盖账户文件。已完整绑定的账户再次 bootstrap 会拒绝覆盖。

## Session 生命周期

启动获取单实例锁，验证 Asia/Shanghai 时区和时钟，连接 broker，查询完整账户/持仓/委托/成交，并运行启动恢复与对账。只有持久 account binding 存在，且不存在 UNKNOWN、外部活动、账户差异和身份漂移时才能进入 READY。

每次生产账户采纳都固定执行：完整 broker snapshot → 加载持久 binding 与当前 uquant AccountState → preflight 检查账户身份、外部/未知订单成交和未解释经济差异 → 仅在深拷贝上 prepare 已知系统事实 → 对 prepared AccountState 做完整 reconciliation → 全部通过后以 expected-before CAS 提交 AccountState、account operation receipt 和 reconciliation receipt。任一检查失败都不会先修改生产 AccountState，也不会把人工交易或异常现金“同步”为策略事实。

broker snapshot 中的 confirmed fills 按稳定 execution sequence 先导入；`CANCELLED`、`REJECTED`、`EXPIRED` 只在 uquant 边界投影为“关闭未成交剩余量”。firmquant operational ledger、reconciliation、audit 和日报继续保存真实 broker 终态。UNKNOWN 不进入关闭投影。

盘后 workflow 验证权威交易日历、市场已收盘、append-only 数据 manifest 和完整券商快照，完成上述账户对账和合法事实采纳后再调用唯一决策入口。次日 workflow 加载冻结决策，验证交易状态和执行事实后提交有限窗口订单。盘中只处理订单、成交、断线、风险与对账；EOD 再取完整快照，按同一 prepare-validate-commit 顺序确认合法成交、生成报告并备份。

`SessionCoordinator` 和 workflow receipts 提供上述可恢复步骤。当前安装版 `run` composition 对 PAPER/REPLAY 完成启动对账并返回 READY 证据；官方 SDK 未验证时 XtQuant 模式明确失败关闭。部署方不能把该失败改成跳过对账。

## 日常 PAPER 操作

1. 使用示例配置创建未跟踪的本地配置；
2. 执行 `init`，确认目录、SQLite 和审计链初始化成功；
3. 执行 `doctor`，所有检查通过后才执行 `run`；
4. 使用 `status` 观察 blocker，使用 `decisions`、`orders`、`fills` 查看持久化证据；
5. 收盘后使用 `reconcile`、`report` 和 `backup`，再定期用 `verify-backup` 验证恢复。

快速命令仅在 [README](../README.md) 给出，那里只包含 PAPER，不提供可直接复制的实盘启动链路。

## 状态与阻断处理

`status` 输出 mode、runtime state、arm 到期时间、firmquant/uquant commit、strategy session、broker connection、最近 quote/对账、unresolved orders、现金、实际/目标 gross、kill switch 和所有 blocker。

- `DEGRADED`：读取或 freshness 下降，系统减少权限；先恢复事实源并对账。
- `HALTED`：新增订单被拒绝；保留数据库、日志、事件文件和账户文件，不手工删除现场。
- `UNKNOWN` / `SUBMITTING_UNRESOLVED`：查询券商委托与成交，禁止重发同一经济意图，禁止继续新的 BUY。
- submit/cancel 超时、网络中断、SDK 异常或返回不完整：按 UNKNOWN 处理；不要把调用异常当作 NOT_ACCEPTED。
- 普通 broker query 未命中：仍不足以证明未接单；只有明确、权威且绑定同一 durable command 的 NOT_ACCEPTED 证明才能解除 submit UNKNOWN。
- `CANCELLED` / `REJECTED` / `EXPIRED` 后出现迟到 confirmed fill：保留现场并 HALT 调查；不能删除 fill、改 broker 终态或手工伪造 uquant 订单。
- 同 fill id 不同经济内容、sequence 回退、累计成交矛盾：视为持久化/券商证据冲突，HALT。
- 外部人工订单或人工账户变化：停止新增订单，导出报告，由操作员明确调查；系统不自动采纳。
- 公司行动/持仓差异：没有精确 reviewed evidence 时阻断；即使有持仓类 reviewed receipt，也必须提供显式 reviewed AccountState，不能把 receipt 当作“忽略差异”。
- 身份/数据漂移：恢复锁定源码、配置或 append-only 数据，不能直接改摘要。

`resume` 只请求状态恢复，仍需重新查询 broker、重新对账、交互确认并证明所有 blocker 消失。`disarm` 不等同于解决订单不确定性。系统不通过 resume 自动重发、追价或改成市价单。

## 报告、日志与告警

结构化 JSON 和 console 事件包含 timestamp、session、correlation/decision/execution/uquant/broker order id、symbol 和 severity。Notifier 支持 console、file 和可选 HTTPS webhook；webhook 失败只生成安全告警，不回滚订单事务。

日报同时保存 JSON 和 Markdown，包含资金持仓、目标/实际差异、完整订单 lifecycle、成交/费用/滑点、未成交/拒单、风险、对账和健康状态。每个系统订单同时显示 firmquant aggregate `state` 与真实 `broker_status`；因此 `PENDING_NEW`、`PENDING_CANCEL`、`REJECTED`、`EXPIRED`、`UNKNOWN` 均可直接审计，拒单/过期不会显示成 broker CANCELLED。缺失下一开盘价等参考事实会明确显示为空，不伪造指标。报告 receipt 写入 hash-chain audit。

## 恢复检查

事故或重启后先执行 recovery/status/reconcile，再决定是否恢复权限。至少确认：没有重复 economic order；没有重复 fill；confirmed fills 未丢失；UNKNOWN 已由权威 broker facts 或明确 NOT_ACCEPTED 证明解析；AccountState、operational ledger、broker snapshot 的现金、持仓、费用和已确认终态一致；重复运行 recovery 不产生新的 broker write 或经济变化。

PAPER、REPLAY、SHADOW 验证环境不允许真实 broker write。REPLAY 的重启等价和 SHADOW 的无写端口用于验证恢复/只读组合；不能把这些模式的通过结果解释成真实 XtQuant 账户已完成 smoke。

## 停止与事故

正常停止进入 STOPPING，断开 broker 后保留 READY/停止证据。紧急情况使用 kill switch，优先取消系统未成交 BUY 并 HALT；不要在券商状态未知时手工重复提交、自动追价或自动市价清仓。恢复流程见 [RECOVERY.md](RECOVERY.md)。
