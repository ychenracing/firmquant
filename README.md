# firmquant

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

## 运行模型

日频经济路径固定为：盘后更新并验证 uquant 数据合同，读取完整券商快照，通过
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
