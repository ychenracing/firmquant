# ADR 0002：SQLite 与单账户 writer lease

状态：采用。

## 背景

部署目标是单机 Windows、单账户、日频。写负载低，但提交前 write-ahead、callback、恢复和审计需要可靠事务；重复实例
必须不能形成双重下单。

## 决策

Operational Ledger 使用本机 SQLite，启用 WAL、foreign keys、FULL synchronous、显式 schema migration 和完整性检查。
所有状态推进由调用者显式事务包围。账户写权限同时要求 OS 文件锁和 SQLite 中带 generation/TTL 的 singleton lease。

原始 broker event、domain event、command、attempt、response、fill、snapshot、reconciliation、risk/alert 和 audit 分表
保存。audit 使用 hash chain；备份使用 SQLite online backup 并在原子发布前验证。

## 结果

- 进程崩溃后可从 durable attempt 和 broker 查询恢复，不盲目重发；
- 第二实例立即失去 writer authority；
- 数据库锁/损坏会失败关闭，事故文件保留；
- 当前不需要外部数据库、分布式锁或消息中间件。

只读运维命令使用 read-only connection；需要写 receipt 的命令也必须先取得 writer lease。
