# ADR 0003：XtQuant 作为首个实盘适配器目标

状态：采用；真实环境待部署验收。

## 背景

目标用户在 Windows 使用 A 股券商本地终端。需要选择一个实盘 adapter 目标验证 BrokerGateway，同时避免一次并行实现
多个券商、扩大范围或依赖来源不明 SDK。

## 决策

选择用户合法获得的官方 MiniQMT/XtQuant SDK 作为首个目标。adapter lazy import，不提交专有 wheel、客户端文件、
userdata 或账户资料。通用 CI 只运行官方签名 contract fake；SDK 存在时先做 import/schema，再做严格只读账户 smoke。

adapter 不包含策略、组合或风险决策。SHADOW 永久禁止写；CANARY/LIVE 的 gateway 必须由 BrokerWriteCapability 包装。
当前不实现 PTrade、XTP 或多券商路由。

## 结果

- broker-agnostic 核心、Paper 和 Replay 不依赖专有 SDK；
- SDK 缺失或映射未验证时给出明确诊断并失败关闭；
- 当前环境没有官方 SDK和合法账户，真实连接/只读 smoke 未完成，不能声明实盘已接通；
- 只有实际确认 Python 3.12 不兼容时，才另行设计认证、版本化、仅 loopback 的本机 bridge。

部署与验收边界见 [Windows 本地部署](../DEPLOYMENT_WINDOWS.md)。
