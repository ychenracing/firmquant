# 合规与责任边界

firmquant 通过券商授权 API 访问账户，不直接连接交易所。开源代码、回测或 PAPER 测试不代表账户已获得 API 权限，也
不替代券商、交易所或监管机构要求的程序化交易报告。

## 实盘前必须确认

- 账户为允许程序化交易的现金账户，券商已开通并书面/系统确认 API 权限；
- 适用的程序化交易报告、备案或告知已经完成；
- 用户理解系统不能保证下单成功、成交、价格或收益；
- 仅使用合法获得且与券商客户端匹配的官方 MiniQMT/XtQuant SDK；
- 操作员有权限处理账户、密钥、日志、备份和事故证据；
- SHADOW 观察、启动对账、时钟/数据身份、恢复演练和部署安全检查已经完成；
- CANARY/LIVE 配置、短时 arm lease 与部署 caps 经独立复核。

配置中的两个 compliance 字段是操作员声明，不是 firmquant 替用户完成的法律判断。字段为 false 时真实模式配置无效；
字段为 true 也不会绕过其他门禁。

## 禁止的验证方式

CI、contract test、SDK smoke、doctor、部署脚本和事故复盘不得通过发送“小额单”验证接入。只读 smoke 限于连接、健康、
资金、持仓、委托、成交、instrument、quote 和 market status。任何真实 submit/cancel 都必须发生在用户明确部署、合法
授权且完整 arm 的运行环境中。

## 人工交易

LIVE 期间发现人工活动订单、无法映射的成交或未解释持仓变化时，系统停止新增订单并保留现场。firmquant 不自动接纳
人工交易、不修改 uquant lifecycle 来“对平”，也不在状态不确定时自动清仓。操作员应先联系券商核实事实，再按恢复
流程显式处理。

## 数据与隐私

仓库禁止账户号、密码、通讯密码、token、私钥、webhook secret、userdata 内容、真实账户快照和未脱敏成交。secret
只从环境 secret provider 或本机凭据存储读取；TOML 不承载秘密。日志、CLI、告警和审计统一脱敏，完整敏感 payload
不进入 SQLite audit。

## 责任

用户负责核实所在地法律、监管规则、交易所规则、券商协议、税费和账户风险。firmquant 的风险门是工程安全约束，不是
投资建议、收益承诺或法律意见。部署清单见 [Windows 本地部署](DEPLOYMENT_WINDOWS.md)。
