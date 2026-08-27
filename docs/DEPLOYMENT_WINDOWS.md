# Windows 本地部署

firmquant 的 Windows 部署目标是单账户、日频、单机运行，并通过用户合法安装的 MiniQMT/XtQuant 客户端访问券商授权 API。系统不直接连接交易所，也不包含或分发券商专有 SDK、客户端文件、账户资料和 `userdata` 内容。

## 安全边界

- 核心进程固定使用 Python 3.12，默认模式为 `PAPER`，实盘写能力默认不存在。
- `PAPER` 使用 `PaperBroker`；`SHADOW` 只能获得只读 broker surface，不能调用真实提交或撤单。
- XtQuant 采用 lazy import。SDK 缺失或签名不符合已验证合同会失败关闭，不会切换到猜测映射。
- 公共 XtQuant 接口没有权威提供的交易单位、完整费用、市场阶段或成交量股数换算等安全事实，必须由本机已验证 provider 提供；任何缺失均阻止交易。
- 当前架构不启用 broker bridge、第二交易进程、Web 服务、HTTP 管理 API、数据库外消息队列或微服务。若未来部署机证实 SDK 与核心运行时不兼容，需要单独安全评审和架构变更，不能在现有生产控制通道旁新增隐藏写进程。
- HALT 默认阻止新订单、撤销 arm 并保留现场，不自动市价清仓；需要撤销已有系统委托时显式执行 `cancel-system-orders`。

## 目录、ACL 与本机控制 inbox

使用专用的非管理员 Windows 用户运行 firmquant 和 MiniQMT。状态、数据、报告和备份目录应位于仅该用户可读写的本地磁盘，不要使用同步盘、共享盘、临时目录或符号链接。示例配置中的路径和账户别名仅是占位符；本机绝对路径、账号和 secret 不得提交到 Git。

生产控制请求固定存放在 `${state_directory}\control\inbox`，处理结果存放在 `${state_directory}\control\receipts`。这两个目录不是网络 API：只依赖本机 NTFS ACL、固定非 symlink 路径和 request 中的 host binding。运行账户之外的用户不应具有写权限。不要把 `control` 目录放到 OneDrive、网络盘、共享目录或任何会改变 atomic rename/ACL 语义的位置。

CLI 通过同目录临时文件写入 canonical JSON，flush/fsync 后 atomic rename 成正式请求。daemon 持有 WriterLease 时，`halt`、`disarm`、`cancel-system-orders`、`stop` 只排队，不打开第二个生产 broker writer；`status --request-id <id> --json` 可读取 durable receipt。`QUEUED` 仅表示排队成功，不能当成“已撤单”。

SQLite 账本在启动时验证以下设置：

- WAL；
- `foreign_keys=ON`；
- `synchronous=FULL`；
- 当前 schema migration；
- 完整性检查；
- Windows `msvcrt` 文件锁与 SQLite 到期 writer lease 的双重单实例排他。

备份通过 SQLite online backup 写入临时 bundle，验证数据库、schema、审计 hash chain 和可选 uquant AccountState 后再原子发布。恢复验证在隔离临时目录中完成，不覆盖生产状态，也不删除事故现场。

锁定的 uquant package manifest 按确定性 wheel 的 LF 原始字节计算。Windows 上必须在执行 `uv sync` 前禁用 Git 的 CRLF checkout 转换，否则安装内容不再等于审查过的 wheel，身份校验会失败关闭：

```powershell
git config --global core.autocrlf false
```

该设置只影响文本 checkout，不放宽任何 fingerprint。若组织策略不允许修改用户级 Git 配置，应由受控构建流程安装并验证 `SOURCE_BASELINE.md` 记录 SHA-256 的确定性 wheel，而不是跳过 package manifest 校验。

## MiniQMT/XtQuant 前置条件

实机只读接入前，操作员必须完成以下事项：

1. 从已授权券商或官方渠道安装与账户匹配的 MiniQMT/XtQuant SDK；
2. 确认券商 API 权限和程序化交易报告要求；
3. 在本地 secret provider 中保存必要凭据，不把值写入 TOML、日志或命令行；
4. 将 `userdata` 路径只写入未跟踪的本地配置，并限制文件 ACL；
5. 先完成 import/schema smoke，再在 MiniQMT 已登录环境进行仅查询资金、持仓、委托和成交的 smoke；
6. 只读结果必须通过规范化和启动对账，且不存在外部活动订单、未知成交或账户差异。

仓库中的 contract fake 只验证字段映射和 SDK 调用签名，不证明真实账户已经接通。真实环境只读 smoke 必须由部署机完成；自动化验收绝不允许发送一笔“小额测试单”。

## 进程信号与干净停止

Windows 和 POSIX 使用同一 daemon 主循环推进生产状态。支持的平台上，SIGINT/SIGTERM handler 只设置一个内存 stop flag；handler 不打开数据库、不写 control 文件、不调用 XtQuant。daemon 下一次循环在 broker event 或策略工作之前读取该 flag，并在持有唯一 WriterLease 的上下文中转换为内部 STOP：撤销 arm、持久化 STOPPING、断开 broker，最后持久化 DISARMED。

Windows 不提供的 signal 常量或不允许安装 handler 的运行环境会被安全跳过，不会因为平台差异安装后台线程或额外控制服务。Windows 正常交互停止仍可使用 `firmquant stop`；daemon 在线时该命令进入本机 inbox，daemon 不在线且 WriterLease 可获取时直接完成同一 STOP 状态语义。

## cancel-only 安全撤单

`cancel-system-orders` 不接受 broker order id 或 symbol 参数。CANARY/LIVE 下，唯一 writer 重新连接/复用配置绑定的生产 broker 后，只从 SQLite operational ledger 内部选择 ownership=`SYSTEM` 且仍活动的已知 broker order；随后再次核对 broker read health、账户 binding、order identity、累计成交和非终态状态。arm 已过期、HALTED 或 kill switch 已触发不会阻止这种风险缩减撤单。

每笔 cancel 在调用 SDK 前先 durable 写 attempt；调用异常或返回无法证明结果时进入 UNKNOWN，后续不会重复 cancel。EXTERNAL/MANUAL/unmapped/terminal order 不会被撤销。PAPER、REPLAY、SHADOW 的同一 CLI 命令保持真实 broker 写调用为零。

## 安全部署 smoke

在仓库根目录执行：

```powershell
git config --global core.autocrlf false
uv sync --frozen --extra dev
uv run python scripts/verify_source_baseline.py
uv run python scripts/windows_smoke.py
```

`windows_smoke.py` 只创建临时目录，使用 `PaperBroker` 和内存 fake secret provider，检查 Python 3.12、Asia/Shanghai zoneinfo、Windows 路径、SQLite durability、单实例锁、备份恢复、CLI 失败关闭和全部 doctor 检查。脚本仅执行无连接的 SDK import/schema 诊断，不实例化 XtQuant 交易对象、不查询真实账户、不调用 submit/cancel；成功输出必须包含 `real_order_calls: 0`。

GitHub Actions 的 `Windows deployment safety` workflow 同样固定 `PAPER` 和 `live_trading_enabled=false`，不注入券商 secret，也不安装专有 SDK。它运行 Windows unit/persistence/CLI smoke，验证 control inbox、writer lease 和路径行为不会引入真实 broker write。该 workflow 只证明 Windows 运行时兼容性，不构成真实 MiniQMT 账户验收。

## 当前真实环境验证状态

本仓库包含 XtQuant adapter、lazy SDK 诊断、官方签名 contract fake、只读 SHADOW 组合边界、本机 control inbox 和 cancel-only capability。当前构建环境没有检测到官方 XtQuant SDK，因此没有完成真实 MiniQMT 连接或账户只读 smoke，也不能声明实盘适配器已经接通。实现与测试期间不得、也没有通过上述 smoke 提交真实订单。

在只读 smoke、完整 SHADOW 观察、启动对账、多重实盘门禁和操作员合规确认全部通过前，部署必须保持 `PAPER` 或 `SHADOW`，不得生成任何 submit capability。cancel-only 的代码存在不等于已经具备真实账户撤单资格；只有 CANARY/LIVE 配置、账户 binding 和真实 broker read identity 全部通过时才可跨越真实 cancel 边界。
