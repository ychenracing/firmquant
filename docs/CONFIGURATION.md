# 配置合同

配置是 UTF-8 TOML，默认路径为 `config/firmquant.local.toml`。Pydantic 模型使用 strict、extra-forbid 和 frozen；未知
字段、binary float、模式/adapter 不匹配或跨字段安全条件缺失都会拒绝启动。配置文件不能通过环境变量提升实盘权限。

## 安全默认配置

仓库唯一示例是 [firmquant.example.toml](../config/firmquant.example.toml)，与 `Settings()` 默认值逐字段测试一致：

```toml
schema_version = 1
mode = "PAPER"
live_trading_enabled = false
timezone = "Asia/Shanghai"

[broker]
adapter = "PAPER"

[paths]
state_directory = "var/state"
data_directory = "var/data"
report_directory = "var/reports"
backup_directory = "var/backups"

[compliance]
program_trading_report_confirmed = false
broker_api_authorized = false
```

示例不包含账户、密码、token、本机 userdata 路径或真实 caps。相对路径相对于配置文件目录解析；状态目录必须是本地、
非 symlink 且具有受限权限的位置。

## 顶层字段

| 字段 | 合同 |
|---|---|
| `schema_version` | 当前只接受整数 1 |
| `mode` | REPLAY、PAPER、SHADOW、CANARY 或 LIVE；默认 PAPER |
| `live_trading_enabled` | 默认 false；只读模式禁止 true，真实模式要求 true |
| `timezone` | 固定 Asia/Shanghai |
| `broker` | adapter 和本地非 secret 引用 |
| `paths` | 状态、策略数据、报告和备份目录 |
| `compliance` | 两项显式合规声明，默认均 false |
| `canary_caps` | CANARY 必填且没有默认值；其他模式可省略 |

模式与 adapter 固定对应：REPLAY 使用 RecordedReplay，PAPER 使用 Paper，SHADOW/CANARY/LIVE 使用 XtQuant。当前官方
XtQuant 环境未验证，配置合法不等于运行前置条件已经满足。

## Broker 与 secret

`broker.account_alias` 和 `broker.xtquant_userdata_path` 会在 `safe_repr` 中脱敏。路径只允许写入未跟踪的本地配置；账号、
密码、token 和 arm MAC key 不属于 TOML schema。secret provider 缺失时需要 secret 的操作失败关闭。

## CANARY 部署 caps

CANARY 要求明确设置单笔、单日提交、单日成交、单票和总敞口名义金额上限。所有值必须是正 Decimal 文本并满足安全
排序关系；缺任一字段即配置无效。这些字段只能比 uquant 决策更严格，不是策略参数，也不能扩展 universe。

## 变更与 arm

系统对原始配置文件计算 SHA-256 并绑定到 arm lease。arm 后的任何字节变化都会触发配置身份不一致，必须 disarm、
重新 doctor/reconcile 并显式重新 arm。不要手工修改数据库里的摘要。

CLI 参数 `--mode` 必须与配置 mode 相同；它不能临时提升运行模式。完整写门禁见
[风险与实盘安全](RISK_AND_SAFETY.md)。
