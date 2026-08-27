# 故障恢复

恢复原则是先证明事实，再恢复权限。券商调用返回异常、进程退出、callback 缺失或普通查询未命中都不能证明券商没有接单；任何无法安全分类的状态进入 UNKNOWN/HALT，禁止盲目重发。

## 启动恢复顺序

1. 获取 OS 文件锁与 SQLite writer lease；
2. 打开数据库，验证 quick/integrity check、foreign keys、schema 和审计 hash chain；
3. 连接 broker 后，先消费 `${state_directory}/control/inbox` 中仍未过期且没有 durable receipt 的本机控制请求；HALT 优先于其他请求，并在任何策略 submit 前生效；
4. 验证 uquant AccountState 文件、account binding、bootstrap operation 和未完成 account operation；
5. 查询完整委托和成交，而不是等待 callback；
6. 按 durable client order identity、已知 broker order id、订单经济字段和 broker snapshot 将 `SUBMITTING`、`CANCEL_REQUESTED`、`UNKNOWN` 与 broker order/fill 匹配；
7. 按稳定 execution sequence 幂等应用 confirmed fills，再应用真实 broker 终态；
8. 运行资金、持仓、委托、成交、费用和身份对账；只有所有矛盾解决后进入 READY。

控制 inbox 是恢复输入的一部分，不是第二数据库。请求文件必须通过 host binding、时效、canonical JSON、大小、权限和非 symlink 校验；处理结果写到 `control/receipts`。已有 receipt 的同 request id 精确重复不会再次执行。过期、畸形、路径穿越或身份不匹配请求被拒绝并留下 reject receipt，不进入运行状态推进。

恢复服务从不调用 submit/cancel。普通查询找不到订单时仍保持 UNKNOWN；只有 broker 提供针对同一 durable command、同一 session 的明确 `NOT_ACCEPTED` 证明，才允许把 submit UNKNOWN 解析为未接单。多重匹配、身份/经济字段不一致、fill id 冲突、sequence 回退、累计成交量矛盾或证据损坏都失败关闭。

唯一例外是操作员显式发出的 `CANCEL_SYSTEM_ORDERS` 风险缩减控制：它不是恢复服务，也不尝试解析 UNKNOWN。cancel-only capability 只从 durable ledger 选择已确认仍活动的 SYSTEM 订单；每次 broker cancel 前先写 durable cancel attempt。调用结果 UNKNOWN 后该订单立即退出 cancel-only 候选，随后仍由上述只读 UNKNOWN 恢复流程处理，不能重复 cancel。

## 订单与控制崩溃边界

| 崩溃点 | 持久证据 | 重启行为 |
|---|---|---|
| control request 已 atomic rename、receipt 尚未写 | `control/inbox/<request>.json` | 验证请求仍未过期后在任何策略工作前重新处理；HALT 不丢失 |
| control receipt 已写、重复 request 文件再次出现 | `control/receipts/<request>.json` | 验证 request hash 一致后幂等删除重复 inbox 文件，不重复执行 |
| durable SUBMITTING 已写、broker 调用前 | intent + command + attempt | 查询 broker；没有明确 NOT_ACCEPTED 证明前保持 UNKNOWN |
| broker 已接受、broker order id 尚未落库 | 本地 SUBMITTING + client identity | 用 client order identity、订单经济字段和 broker snapshot 定位；绝不自动再次 submit |
| ACK 已落库、fill 尚未落库 | broker order + aggregate | 查询 broker order/fills，confirmed fills 幂等补齐 |
| 部分成交后崩溃 | 已有部分 fill + broker cumulative | 按 fill identity/sequence 补齐缺失成交，不重复已成交 |
| cancel request 已落库、broker 响应前 | CANCEL_REQUESTED + cancel attempt | 查询委托与成交；未证明撤单结果时 UNKNOWN，不再次 cancel |
| cancel-only 调用跨过 broker 边界后进程退出 | durable CANCEL_REQUESTED/attempt，response 可能缺失 | 按 UNKNOWN 恢复查询；不因新的控制请求再次 cancel |
| terminal broker fact 已到、AccountState 提交前 | ledger/snapshot/account operation | broker facts 保留；下一次 account sync 重新 prepare/reconcile 后提交，不重复经济订单 |
| AccountState 文件已提交、receipt 尚未提交 | expected-after 文件 + FILE_COMMITTED/PREPARED receipt evidence | 验证文件 hash 后补齐 SQLite receipt/reconciliation finalization，不重复写经济状态 |
| receipt 已提交后重复启动 | RECEIPT_COMMITTED + audit | 幂等读取与验证，结果保持一致 |
| CANCELLED/REJECTED/EXPIRED 后迟到 fill | terminal fact + 新 confirmed fill | 保留 fill 与真实终态，标记 late-fill 调查并 HALT，不能静默丢弃 |

`PARTIALLY_FILLED + CANCELLED/REJECTED/EXPIRED` 的恢复顺序固定为 confirmed fills first、terminal fact second。相同 fill id 与完全相同经济内容/sequence 重放幂等；相同 id 对应不同经济内容、旧 sequence 或与 broker cumulative 不一致时进入恢复矛盾。相同 broker response 在同 attempt 上重复启动时幂等；同 attempt 出现不同 response 内容失败关闭。

UNKNOWN 恢复为 `ACKNOWLEDGED`、`FILLED`、`CANCELLED`、`REJECTED` 或 `EXPIRED` 时，必须来自权威 broker facts；如果只能证明订单仍活动，cancel-UNKNOWN 继续保持 UNKNOWN/HALT。系统不因为“没查到”“仍 ACK”或 SDK 调用结束而猜测写操作结果。

## AccountState prepare/commit 协议

broker sync 的 prepare 阶段只在深拷贝上执行 uquant account-sync，不创建账户事务、不写生产文件、不写 reconciliation receipt。firmquant 首先保留真实 broker 状态；进入 uquant 边界时，confirmed fills 按稳定 execution sequence 导入，然后仅把 `CANCELLED`、`REJECTED`、`EXPIRED` 的未成交剩余量投影为 uquant 公共取消/关闭语义。`UNKNOWN` 不进入该投影。

只有 preflight 与 prepared-state final reconciliation 全部通过后，才创建带 expected-before/expected-after、broker evidence 和 reconciliation finalization payload 的 account operation，再以 CAS 原子替换 uquant AccountState 文件。

文件写入成功后，account operation receipt、account-operation audit 和 reconciliation receipt 在同一个 SQLite transaction 内 finalize。若进程在文件已经替换、SQLite finalization 尚未提交之间退出，重启根据 operation 中封存的 finalization payload 验证 expected-after 文件并补齐整组 SQLite receipt；若文件仍等于 before 且 operation 仍 PREPARED，则保持 retry-required，不会把未应用操作伪装成已提交。文件 hash 既不是 before 也不是 expected-after、payload 身份冲突或证据损坏都会进入 CONTRADICTION/HALT。

相同 broker snapshot / prepared identity 的重试复用同一 operation；已经完成的提交是幂等的，identity 相同但 payload 不同失败关闭。系统不会为了“恢复”而覆盖未知账户文件。

## Bootstrap 恢复

一次性 `bootstrap-account` 也使用持久 PREPARED operation。崩溃发生在 AccountState 保存前时，重跑重新构造并严格验证候选 AccountState，只有其 account hash、账户身份及 code/data identity 与原 operation 完全一致才继续；不会插入第二个 operation。

崩溃发生在 AccountState 已经保存但 binding 尚未 finalize 时，重跑首先验证现有文件 hash 必须等于 operation 中的 expected account hash，并重新确认当前只读 broker snapshot 仍属于同一账户、没有新的委托/成交，同时严格核对已保存 AccountState 的 code/data identity、现金、持仓和可卖数量。全部一致后，才在单一 SQLite transaction 内写 account binding、binding audit、BINDING_COMMITTED 和 bootstrap audit。该恢复路径不再次保存或覆盖 AccountState。任何身份或经济状态不匹配都保持 PREPARED 并失败关闭；完整 binding 已建立后再次 bootstrap 也拒绝覆盖。

## SQLite 异常

- 临时锁：事务失败，不调用 broker write；释放锁后从持久证据重新恢复。
- 损坏：`DatabaseCorrupt`，原文件原地保留，不自动重建空库。
- 第二实例：OS lock 或未到期 database lease 拒绝新 writer；风险缩减 CLI 命令只能写入 control inbox，不能启动第二个交易 writer。
- migration：按 schema version 顺序、事务化且可重复；未知/未来 schema 拒绝打开。
- audit mismatch：停止运行，不重写 hash chain。

## Broker/客户端重启

断线超过安全阈值 HALT；重连后先处理 pending control，再执行完整查询和 reconciliation。新的 event watermark、迟到成交、外部订单和持仓变化必须得到解释。客户端重启不会让本地假设 broker 已清空，也不会重新提交 UNKNOWN 经济订单。

恢复完成后，本地 aggregate、operational ledger、broker snapshot 和 uquant AccountState 必须对已确认终态、现金、持仓和费用严格一致；`REJECTED`、`EXPIRED` 与 `CANCELLED` 在 firmquant 对账中保持不同终态。

## 备份与验证

备份使用 SQLite online backup 生成一致性数据库，连同 manifest 和可选 uquant AccountState 写入临时 bundle；全部摘要、schema、审计链和账户文件验证后原子发布。`verify-backup` 在隔离临时目录恢复，不覆盖生产文件。备份失败或验证失败不删除源数据，也不能作为继续交易的理由。

恢复后使用 [运行指南](OPERATIONS.md) 的 `status`、`reconcile`、控制 receipt 和报告证据复核；实盘恢复还需重新 arm。

## 收盘 checkpoint 与最终恢复 bundle

收盘闭环使用单一有序 checkpoint 链：`EOD_RECONCILED → DATA_VALIDATED → DECISION_COMMITTED → REPORT_PUBLISHED → BACKUP_VERIFIED → COMPLETED`。每一步都写入 append-only `CLOSE_SESSION` audit receipt，要求所有前驱已经存在；相同 evidence 重放幂等，不同 evidence 冲突失败关闭。`COMPLETED` 是唯一“当日收盘完整”的权威标记，旧的 EOD/reconciliation/report 单项证据都不能代替它。

进程可在任意边界崩溃。重启时先查找最近 incomplete close session，并从第一项缺失 checkpoint 继续：已经持久化的 EOD reconciliation、数据 manifest、冻结 DecisionSnapshot、报告或 backup 不重复产生；已经完成的 session 再次进入直接幂等返回。尤其是 `DECISION_COMMITTED` 之后崩溃时，恢复必须复用同一个 immutable DecisionSnapshot/AccountState identity，不能再次运行新的经济决策；`REPORT_PUBLISHED` 或 `BACKUP_VERIFIED` 之后崩溃也只完成后续步骤。任一步异常都不会写伪造 `COMPLETED`。

历史数据 rewrite promotion 另有 `pending-promotion.json` crash marker。崩溃发生在新 generation 建立后、active pointer 切换前或切换后但 source receipt 尚未发布时，重启只允许验证同一 candidate/old/new digest 后完成该 promotion；active pointer 指向第三个 generation、candidate 被改写或 digest 不一致都停止。旧 generation 永远保留，便于恢复和取证。

最终 schema-v2 backup bundle 必须同时包含 SQLite、严格 uquant AccountState、已验证 production config、XtQuant safety manifest、权威 trading calendar、active data generation manifest、当日 strategy-data manifest 和 deployment record。deployment record 绑定 firmquant/uquant commit、config hash、AccountState economic hash、calendar/data identity、strategy session 与当天 decision id；bundle manifest 对每个成员单独 SHA-256，并封存 operational schema 与 audit head。验证过程在隔离目录打开数据库、执行 integrity/schema/audit 校验，加载 AccountState/config/calendar/safety manifest，并确认数据库确实包含同一 frozen decision。

backup 先在临时目录完整构造和验证，再 atomic rename 发布；进程在构造/验证阶段退出不会留下可被识别为正式 backup 的半成品。bundle 不保存 `ARM_MAC_KEY`、密码、token、webhook token 或 MiniQMT userdata。恢复时若任一成员 hash、账户 economic hash、deployment identity、calendar/data identity、audit head 或 decision id 不一致，整个 bundle 视为不可恢复证据，不能用部分成员覆盖生产状态。
