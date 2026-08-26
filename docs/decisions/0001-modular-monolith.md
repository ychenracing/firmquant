# ADR 0001：模块化单体与端口适配器

状态：采用。

## 背景

系统当前服务一个 A 股现金账户，策略日频，在线负载来自订单管理、回调、风险监控和对账。经济状态、订单事务和审计
证据需要强一致，Windows 本地券商终端也不适合引入分布式部署负担。

## 决策

使用单 Python 进程的模块化单体。领域、application、strategy、broker、market data、execution、risk、reconciliation、
persistence、scheduling、observability 和 security 保持显式边界；外部系统通过 Protocol/adapter 接入。

callback 只入队，单 writer 串行推进状态。策略内核作为精确锁定依赖运行，不嵌入第二套策略实现。

## 结果

- 订单状态、broker 事件、对账和审计可在单 SQLite 事务边界中保持一致；
- PAPER/REPLAY/SHADOW 与真实模式复用同一领域路径；
- 新 broker adapter 可独立增加而不污染策略；
- 当前不承担微服务发现、分布式事务、消息代理和跨节点 writer 选举的复杂度。

若未来真实需求超出单账户/单机范围，应重新作架构决策，不能在当前核心中静默扩展多账户或多策略。
