# 风险与实盘安全

firmquant 风控是执行安全层，不是策略风险模型。Base Risk 派生 target gross cap，PortfolioAllocator 形成目标组合，
Risk Sentinel 保持 FREEZE_ONLY；这些经济职责全部属于 uquant。

## 只收缩原则

ExecutionRiskGate 的结果只有 ALLOW、SHRINK、DELAY、BLOCK、HALT。授权股数同时不超过 uquant order intent、目标缺口、
真实可卖/持仓、可用现金、交易单位、流动性和部署上限。任何输入缺失或矛盾都不能产生更大的订单。

## 逐单事实

门禁覆盖以下类别：

- canonical AI universe 与部署 allowlist；
- 现金账户、禁止融资融券/做空、负现金和负持仓；
- uquant 目标、gross cap、freeze-new-risk 与实际持仓；
- 可卖数量、T+1、交易单位、证券风险状态、停牌；
- broker instrument/quote 给出的价格 tick、上下限、市场阶段和 freshness；
- 单笔/日累计/单票/总敞口、未完成订单、拒单、断线、生命周期、撤改和频率；
- uquant 成交量参与率、账户权益异常、日内损失与回撤警戒；
- 启动/盘中对账、外部人工订单、未解释持仓、疑似公司行动；
- 时钟、数据/配置/代码身份、UNKNOWN、kill switch。

系统不硬编码交易所涨跌停比例。instrument、upper/lower limit 或 market status 缺失时停止交易，不根据板块历史经验
猜测。所有金额、价格和费用使用 Decimal；与 uquant float 边界只接受有限值并执行精度/容差校验。

## 模式与写权限

| 模式 | 真实读取 | 真实 submit/cancel | 安全用途 |
|---|---:|---:|---|
| REPLAY | 否 | 永不 | 事故复盘与确定性回归 |
| PAPER | 否 | 永不 | 模拟执行与完整 session |
| SHADOW | 是（部署前提满足时） | 永不 | 真实环境只读观察和订单计划 |
| CANARY | 是 | 多重门禁后有限 | 使用无默认值部署 caps 的受限验证 |
| LIVE | 是 | 多重门禁后有限 | 执行完整 uquant 意图，仍只收缩 |

CANARY 不自动升级 LIVE；模式必须与配置一致。当前 XtQuant 真实环境未验证，相关模式在安装组合中失败关闭。

## Arm lease 与写 capability

`arm-live` 只能在交互终端执行，在 CI 中拒绝。确认短语通过 getpass 临时读取，不写日志；lease 默认短时且最长受 CLI
限制。lease 通过 MAC 认证并绑定 host、账户、mode、firmquant commit、uquant source SHA 和配置摘要。配置或代码
变化后旧 lease 失效，不能用环境变量永久解锁。

每次真实 submit/cancel 都重新构造最新 WriteAuthorizationContext，并同时检查 mode、live flag、lease、合规、broker
健康、启动对账、snapshot/quote freshness、session/market status、fingerprints、kill switch、UNKNOWN、外部活动、
逐单 gate、现金/持仓和频率。capability 不是启动时一次性通行证。

## HALT 与恢复

HALT 保留现场并阻止新增风险。`resume` 需要交互确认和重新对账，不能清除 UNKNOWN、外部订单、身份漂移或账户差异。
数据库损坏、writer lease 冲突和审计链失败直接阻止运行。紧急默认不是自动清仓。

合规前置条件见 [COMPLIANCE.md](COMPLIANCE.md)，操作流程见 [OPERATIONS.md](OPERATIONS.md)。
