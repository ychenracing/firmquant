# 故障恢复

恢复原则是先证明事实，再恢复权限。券商调用返回异常、进程退出或 callback 缺失都不能证明券商没有接单；任何无法安全
分类的状态进入 UNKNOWN/HALT，禁止盲目重发。

## 启动恢复顺序

1. 获取 OS 文件锁与 SQLite writer lease；
2. 打开数据库，验证 quick/integrity check、foreign keys、schema 和审计 hash chain；
3. 验证 uquant AccountState 文件及未完成 account operation；
4. 连接 broker 并查询完整委托和成交，而不是等待 callback；
5. 将 SUBMITTING、CANCEL_REQUESTED 和 UNKNOWN 与 broker order/fill 逐一匹配；
6. 应用合法迟到事实，运行资金、持仓、委托、成交和身份对账；
7. 只有所有矛盾解决后进入 READY。

恢复服务从不调用 submit/cancel。找不到订单且不能证明未接单时保持 UNKNOWN；找到唯一且身份一致的订单时应用券商事实；
多重匹配、字段不一致或本地终态与券商活动态矛盾都要求调查。

## 订单崩溃边界

| 崩溃点 | 持久证据 | 重启行为 |
|---|---|---|
| 意图保存前 | 无经济订单 | 不创建/重发；等待 workflow 明确重建 |
| SUBMITTING 后、broker 调用前 | intent + command + attempt | 查询 broker；未证明前 UNKNOWN |
| broker 接受后、broker id 落库前 | 本地 SUBMITTING | 以 uquant client order id 和固定字段匹配 |
| 部分成交 callback 事务前 | broker 有 fill，本地可能缺失 | query fills 幂等补齐 |
| 撤单请求后、确认前 | CANCEL_REQUESTED | 查询委托与成交，不重复撤单 |
| CANCELLED 后迟到成交 | 终态 + 新 fill | 保留 fill，标记异常调查，不静默覆盖 |

重复 callback、整段事件重放和进程重启不会增加经济订单或重复 fill。事件身份碰撞、填单总量超限或状态非法回退失败关闭。

## AccountState 原子协议

firmquant 先在 SQLite 记录 prepared account operation，再调用 uquant 原子保存账户文件，最后写 receipt。恢复时文件若与
before hash 一致则分类为未应用；与 expected-after 一致则补写 receipt；两者均不匹配或文件损坏则 CONTRADICTION/HALT。
系统不会覆盖中断文件或删除事故证据。

## SQLite 异常

- 临时锁：事务失败，不调用 broker write；释放锁后从持久证据重新恢复。
- 损坏：`DatabaseCorrupt`，原文件原地保留，不自动重建空库。
- 第二实例：OS lock 或未到期 database lease 拒绝新 writer。
- migration：按 schema version 顺序、事务化且可重复；未知/未来 schema 拒绝打开。
- audit mismatch：停止运行，不重写 hash chain。

## Broker/客户端重启

断线超过安全阈值 HALT；重连后先执行完整查询和 reconciliation。新的 event watermark、迟到成交、外部订单和持仓变化
必须得到解释。客户端重启不会让本地假设 broker 已清空。

## 备份与验证

备份使用 SQLite online backup 生成一致性数据库，连同 manifest 和可选 uquant AccountState 写入临时 bundle；全部摘要、
schema、审计链和账户文件验证后原子发布。`verify-backup` 在隔离临时目录恢复，不覆盖生产文件。备份失败或验证失败不删除
源数据，也不能作为继续交易的理由。

恢复后使用 [运行指南](OPERATIONS.md) 的 `status`、`reconcile` 和报告证据复核；实盘恢复还需重新 arm。
