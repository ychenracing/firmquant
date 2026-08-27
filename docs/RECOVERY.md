# 故障恢复

恢复原则是先证明事实，再恢复权限。券商调用返回异常、进程退出或 callback 缺失都不能证明券商没有接单；任何无法安全
分类的状态进入 UNKNOWN/HALT，禁止盲目重发。

## 启动恢复顺序

1. 获取 OS 文件锁与 SQLite writer lease；
2. 打开数据库，验证 quick/integrity check、foreign keys、schema 和审计 hash chain；
3. 验证 uquant AccountState 文件、account binding、bootstrap operation 和未完成 account operation；
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

## AccountState prepare/commit 协议

broker sync 的 prepare 阶段只在深拷贝上执行 uquant account-sync，不创建账户事务、不写生产文件、不写 reconciliation receipt。
只有 preflight 与 prepared-state final reconciliation 全部通过后，才创建带 expected-before/expected-after、broker evidence 和
reconciliation finalization payload 的 account operation，再以 CAS 原子替换 uquant AccountState 文件。

文件写入成功后，account operation receipt、account-operation audit 和 reconciliation receipt 在同一个 SQLite transaction 内
finalize。若进程在文件已经替换、SQLite finalization 尚未提交之间退出，重启根据 operation 中封存的 finalization payload
验证 expected-after 文件并补齐整组 SQLite receipt；若文件仍等于 before 且 operation 仍 PREPARED，则保持 retry-required，
不会把未应用操作伪装成已提交。文件 hash 既不是 before 也不是 expected-after、payload 身份冲突或证据损坏都会进入
CONTRADICTION/HALT。

相同 broker snapshot / prepared identity 的重试复用同一 operation；已经完成的提交是幂等的，identity 相同但 payload 不同
失败关闭。系统不会为了“恢复”而覆盖未知账户文件。

## Bootstrap 恢复

一次性 `bootstrap-account` 也使用持久 PREPARED operation。崩溃发生在 AccountState 保存前时，重跑重新构造并严格验证候选
AccountState，只有其 account hash、账户身份及 code/data identity 与原 operation 完全一致才继续；不会插入第二个 operation。

崩溃发生在 AccountState 已经保存但 binding 尚未 finalize 时，重跑首先验证现有文件 hash 必须等于 operation 中的
expected account hash，然后在单一 SQLite transaction 内写 account binding、binding audit、BINDING_COMMITTED 和 bootstrap
audit。该恢复路径不再次保存或覆盖 AccountState。任何不匹配都失败关闭；完整 binding 已建立后再次 bootstrap 也拒绝覆盖。

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
