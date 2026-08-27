# uquant 策略集成

firmquant 将 uquant 视为锁定、不可改写的生产内核。依赖 commit、tree、依赖锁、生产源码面、默认配置 fingerprint、
canonical universe seal 和确定性 wheel 摘要记录在 [SOURCE_BASELINE.md](SOURCE_BASELINE.md)，机器可读副本位于
`src/firmquant/resources/source_identity.json`。

## 唯一决策路径

策略决策只允许调用一次 `ProductionEngine.decide()`。进入该入口前，账户必须已经完成 firmquant 的权威 reconciliation 与
CAS finalization。`StrategyAdapter` 的职责仅是：

1. 验证 uquant checkout/安装包、代码、配置、数据和 universe 身份；
2. 接收已经通过 AccountBinding/preflight 的完整券商事实；公共 account-sync 语义只在 deep-copy AccountState 上形成候选，
   不在 reconciliation 前修改生产账户；
3. 在候选账户通过完整 reconciliation 并以 expected-before CAS 提交后，调用传入的唯一 ProductionEngine；
4. 原样捕获结果和账户经济状态，构造不可变 DecisionSnapshot；
5. 对相同 session 与相同输入生成稳定 decision id，拒绝覆盖输入变化后的旧快照。

adapter 不实现第二套 ProductionEngine、PortfolioAllocator、Risk、Sentinel 或策略状态机，也不对经济结果做近似转换。

## AccountState 边界

uquant AccountState 是策略经济状态和持仓 lifecycle 的唯一权威。真实券商账户先通过一次性、不可变 AccountBinding 绑定账户
id/type；firmquant 不从第一份 BrokerSnapshot 或历史快照隐式采用账户身份。

Broker sync 拆成 prepare 与 commit。prepare 加载严格 uquant AccountState 并在 deep copy 上调用公共 account-sync 合同，只允许
operational ledger 已证明归属且身份一致的系统订单/成交进入候选；prepare 不写账户文件、不创建 account operation/receipt。
人工订单、异常现金、未知成交或无法解释的持仓差异在生产 AccountState 变更前由 preflight 阻断。

候选账户必须再与同一 BrokerSnapshot 和同一 operational-ledger view 执行完整资金、持仓、委托、成交、代码/数据/配置
reconciliation。只有通过后才以 before economic hash 做 CAS；AccountState 原子保存之后，account-operation 与 reconciliation
receipt 在同一 SQLite finalization 边界提交。文件落盘后若进程崩溃，durable reconciliation evidence 允许恢复服务幂等补齐
finalization，而不是重放策略或创建第二份经济账户。

ReviewedAccountAdjustment 只是一条精确、只追加的操作员复核证据，不会改变 uquant 的语义所有权。与当前
account/symbol/session/type/broker snapshot/difference hash 完全一致的现金差异可以被显式授权；涉及持仓总数、可卖数量、公司
行动或人工卖出的变化仍不能由 firmquant 推导 lifecycle、tranche 或 attribution，必须提供已经复核并通过 uquant 严格 loader 的
AccountState。券商持仓差异不能被自动改造成新的策略 lifecycle。

Operational Ledger 只记录 broker id、订单尝试、事件、成交、对账和运行控制，不保存另一份策略目标或策略参数。

## DecisionSnapshot

快照绑定 strategy session、decision id、firmquant/uquant commit、uquant code/config fingerprint、data/universe
manifest、broker snapshot、账户前后摘要、opportunity、risk、sentinel、targets、pending orders 和 reason codes。
payload 使用 canonical JSON 和 SHA-256 封存；repository 只追加，不允许 UPDATE/DELETE。

次日执行必须加载前一交易日的冻结快照，不在盘前重新运行策略。若账户、身份或关键事实与快照前提发生实质变化，执行
进入 HALT 或等待下一次合法盘后决策，不能静默重算。

## Universe 与策略参数

canonical AI universe 和点时成员由 uquant manifest 独占。部署 allowlist 只能是它的子集；不在 canonical universe
内的证券永远不能获得 submit authority。

组合上限、策略风险上限、持仓数量、行业约束、流动性参与率和战略保留语义均从锁定的 uquant 配置或决策结果读取。
firmquant 文档与配置不维护第二份策略数值默认值。CANARY caps 是更严格的部署安全参数，不是策略优化参数。

## 运行形态与上游缺口

锁定 wheel 可复现，但缺少 uquant 生产源码 fingerprint registry 与 reference registry。为避免复制策略资源，
StrategyAdapter 只允许在精确验证、干净且锁定 commit 的 uquant source checkout 中执行策略，并验证 engine 的模块来源。
缺口、复现和希望上游提供的接口详见 [UPSTREAM_GAPS.md](UPSTREAM_GAPS.md)。

## 等价性证据

parity 测试在同一 commit、配置、数据、universe、账户、broker snapshot 与 as-of 下，对 adapter 和直接调用 uquant
比较完整 opportunity、risk、sentinel、targets、pending orders、reason codes、账户经济状态和 fingerprints。比较为
严格相等，不使用容差掩盖经济差异。