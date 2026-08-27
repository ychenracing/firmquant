# 故障恢复

恢复原则是先证明事实，再恢复权限。券商调用返回异常、进程退出或 callback 缺失都不能证明券商没有接单；任何无法安全
分类的状态进入 UNKNOWN/HALT，禁止盲目重发。

## 启动恢复顺序

1. 获取 OS 文件锁与 SQLite writer lease；
2. 打开数据库，验证 quick/integrity check、foreign keys、schema 和审计 hash chain；
3. 验证 uquant AccountState 文件、持久 AccountBinding 及未完成 account operation；
4. 对 BROKER_SYNC 的未完成提交先按 before/expected-after 文件身份和 durable finalization evidence 分类；
5. 连接 broker 并查询完整委托和成交，而不是等待 callback；
6. 将 SUBMITTING、CANCEL_REQUESTED 和 UNKNOWN 与 broker order/fill 逐一匹配；
7. 应用合法迟到事实，运行账户身份、资金、持仓、委托、成交和代码/数据/配置 reconciliation；
8. 只有所有矛盾解决后进入 READY。

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

## AccountState prepare / commit / finalization

Broker account prepare 是纯内存步骤：加载当前严格 uquant AccountState，在 deep copy 上应用已证明归属的系统 broker facts，生成
稳定 preparation identity、before/after economic hash、broker snapshot hash 和 payload hash。prepare 不创建 account operation、
不写 SQLite receipt，也不修改生产账户文件，因此 reconciliation 阻断或进程在 prepare 后退出都不会污染 AccountState。

只有 preflight 和完整 reconciliation 都通过后才开始 commit。commit 先以 recorded before hash 做 CAS，然后记录 deterministic
`BROKER_SYNC` account operation，再调用 uquant 原子保存 AccountState。文件成功落盘后，account-operation receipt、
reconciliation receipt 和对应 audit 在同一 SQLite finalization 事务中提交。相同 preparation 重试使用同一 operation identity；
identity 被不同事实复用或 before-state 已变化时失败关闭。

为覆盖“AccountState 文件已经落盘、SQLite final receipt 尚未提交”这一崩溃窗口，account operation 只持久化 canonical
reconciliation finalization evidence，不保存第二份 AccountState。重启时：

- 文件仍为 before hash：BROKER_SYNC 保持 `PREPARED`，报告 `ACCOUNT_COMMIT_RETRY_REQUIRED`，不会伪造已完成 receipt；
- 文件为 expected-after 且 durable finalization evidence 完整、hash/identity 均通过：恢复服务在单一 SQLite 事务中幂等补齐
  account-operation receipt、reconciliation receipt 和 audit，并将 operation 置为 `RECEIPT_COMMITTED`；
- 文件为 expected-after 但 evidence 缺失或损坏：保持 `FILE_COMMITTED` 并报告 `ACCOUNT_FINALIZATION_REQUIRED`，等待明确修复；
- 文件既不是 before 也不是 expected-after，或路径/证据身份矛盾：标记 CONTRADICTION/HALT。

第二次 recovery 不会重复 reconciliation receipt 或 audit。系统不会为了恢复运行而覆盖事故文件、篡改 before hash 或猜测
reconciliation 已完成。

## bootstrap 崩溃边界

一次性 `bootstrap-account` 使用独立的 crash-consistent bootstrap operation：只有券商快照、seed/empty AccountState、锁定
code/data identity 和全部前置条件验证通过后才进入 PREPARED。AccountState 文件发布后再提交不可变 AccountBinding 与 audit；
重复 bootstrap、已有未绑定账户文件、已有 binding 或中途 contradiction 都拒绝覆盖。非空券商账户没有严格复核 seed 时不会写
任何生产账户状态。

## SQLite 异常

- 临时锁：事务失败，不调用 broker write；释放锁后从持久证据重新恢复。
- 损坏：`DatabaseCorrupt`，原文件原地保留，不自动重建空库。
- 第二实例：OS lock 或未到期 database lease 拒绝新 writer。
- migration：按 schema version 顺序、事务化且可重复；账户 binding、bootstrap operation 与 reviewed adjustment 也由中心
  checksummed migration 管理；未知/未来 schema 拒绝打开。
- audit mismatch：停止运行，不重写 hash chain。

## Broker/客户端重启

断线超过安全阈值 HALT；重连后先执行完整查询和 reconciliation。新的 event watermark、迟到成交、外部订单和持仓变化
必须得到解释。客户端重启不会让本地假设 broker 已清空，也不会把新快照自动当成新的账户 binding。

## 备份与验证

备份使用 SQLite online backup 生成一致性数据库，连同 manifest 和可选 uquant AccountState 写入临时 bundle；全部摘要、
schema、审计链和账户文件验证后原子发布。`verify-backup` 在隔离临时目录恢复，不覆盖生产文件。备份失败或验证失败不删除
源数据，也不能作为继续交易的理由。

恢复后使用 [运行指南](OPERATIONS.md) 的 `status`、`reconcile` 和报告证据复核；实盘恢复还需重新 arm。