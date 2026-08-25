# Windows 本地部署

firmquant 的 Windows 部署目标是单账户、日频、单机运行，并通过用户合法安装的 MiniQMT/XtQuant
客户端访问券商授权 API。系统不直接连接交易所，也不包含或分发券商专有 SDK、客户端文件、账户资料和
`userdata` 内容。

## 安全边界

- 核心进程固定使用 Python 3.12，默认模式为 `PAPER`，实盘写能力默认不存在。
- `PAPER` 使用 `PaperBroker`；`SHADOW` 只能获得只读 broker surface，不能调用提交或撤单。
- XtQuant 采用 lazy import。SDK 缺失或签名不符合已验证合同会失败关闭，不会切换到猜测映射。
- 公共 XtQuant 接口没有权威提供的交易单位、完整费用、市场阶段或成交量股数换算等安全事实，必须由本机
  已验证 provider 提供；任何缺失均阻止交易。
- 当前没有启用 broker bridge。尚未在合法安装环境中发现 Python 3.12 兼容性问题，因此不预先引入第二
  进程；若部署机证实不兼容，应保持核心 Python 3.12，并通过经过认证、版本化、仅 loopback 的 bridge
  隔离 SDK。bridge 不得包含策略、组合或风控决策。
- 紧急状态默认阻止新订单、请求取消系统拥有的未成交订单并进入 `HALTED`，不自动市价清仓。

## 目录与权限

使用专用的非管理员 Windows 用户运行 firmquant 和 MiniQMT。状态、数据、报告和备份目录应位于仅该用户
可读写的本地磁盘，不要使用同步盘、共享盘、临时目录或符号链接。示例配置中的路径和账户别名仅是占位符；
本机绝对路径、账号和 secret 不得提交到 Git。

SQLite 账本在启动时验证以下设置：

- WAL；
- `foreign_keys=ON`；
- `synchronous=FULL`；
- 当前 schema migration；
- 完整性检查；
- Windows `msvcrt` 文件锁与 SQLite 到期 writer lease 的双重单实例排他。

备份通过 SQLite online backup 写入临时 bundle，验证数据库、schema、审计 hash chain 和可选 uquant
AccountState 后再原子发布。恢复验证在隔离临时目录中完成，不覆盖生产状态，也不删除事故现场。

## MiniQMT/XtQuant 前置条件

实机只读接入前，操作员必须完成以下事项：

1. 从已授权券商或官方渠道安装与账户匹配的 MiniQMT/XtQuant SDK；
2. 确认券商 API 权限和程序化交易报告要求；
3. 在本地 secret provider 中保存必要凭据，不把值写入 TOML、日志或命令行；
4. 将 `userdata` 路径只写入未跟踪的本地配置，并限制文件 ACL；
5. 先完成 import/schema smoke，再在 MiniQMT 已登录环境进行仅查询资金、持仓、委托和成交的 smoke；
6. 只读结果必须通过规范化和启动对账，且不存在外部活动订单、未知成交或账户差异。

仓库中的 contract fake 只验证字段映射和 SDK 调用签名，不证明真实账户已经接通。真实环境只读 smoke
必须由部署机完成；自动化验收绝不允许发送一笔“小额测试单”。

## 安全部署 smoke

在仓库根目录执行：

```powershell
uv sync --frozen --extra dev
uv run python scripts/verify_source_baseline.py
uv run python scripts/windows_smoke.py
```

`windows_smoke.py` 只创建临时目录，使用 `PaperBroker` 和内存 fake secret provider，检查 Python 3.12、
Asia/Shanghai zoneinfo、Windows 路径、SQLite durability、单实例锁、备份恢复、CLI 失败关闭和全部十五项
doctor 检查。脚本仅执行无连接的 SDK import/schema 诊断，不实例化 XtQuant 交易对象、不查询真实账户、
不调用 submit/cancel；成功输出必须包含：

```json
{"broker_adapter":"PAPER","doctor_checks":15,"real_order_calls":0}
```

GitHub Actions 的 `Windows deployment safety` workflow 同样固定 `PAPER` 和
`live_trading_enabled=false`，不注入券商 secret，也不安装专有 SDK。它验证 Windows 运行时兼容性，
不构成真实 MiniQMT 账户验收。

## 当前真实环境验证状态

本仓库包含 XtQuant adapter、lazy SDK 诊断、官方签名 contract fake 和只读 SHADOW 组合边界。当前构建
环境没有检测到官方 XtQuant SDK，因此没有完成真实 MiniQMT 连接或账户只读 smoke，也不能声明实盘
适配器已经接通。实现与测试期间不得、也没有通过上述 smoke 提交真实订单。

在只读 smoke、完整 SHADOW 观察、启动对账、多重实盘门禁和操作员合规确认全部通过前，部署必须保持
`PAPER` 或 `SHADOW`，不得生成任何 broker write capability。
