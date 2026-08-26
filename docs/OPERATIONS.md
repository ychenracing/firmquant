# 运行与本机运维

firmquant 的控制面是本机 CLI。操作命令返回稳定 reason code，`--json` 可供本机脚本读取；命令输出和审计 payload 均
经过统一脱敏。仓库不默认开放 HTTP 端口。

## Session 生命周期

启动获取单实例锁，验证 Asia/Shanghai 时区和时钟，连接 broker，查询完整账户/持仓/委托/成交，并运行启动恢复与对账。
只有不存在 UNKNOWN、外部活动、账户差异和身份漂移时才能进入 READY。

盘后 workflow 验证权威交易日历、市场已收盘、append-only 数据 manifest 和完整券商快照，然后同步 uquant AccountState
并调用唯一决策入口。次日 workflow 加载冻结决策，验证交易状态和执行事实后提交有限窗口订单。盘中只处理订单、成交、
断线、风险与对账；EOD 再取完整快照、同步确认成交、生成报告并备份。

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
- 外部人工订单：停止新增订单，导出报告，由操作员明确调查；系统不自动采纳。
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
