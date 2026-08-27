# uquant 策略集成

firmquant 将 uquant 视为锁定、不可改写的生产内核。依赖 commit、tree、依赖锁、生产源码面、默认配置 fingerprint、
canonical universe seal 和确定性 wheel 摘要记录在 [SOURCE_BASELINE.md](SOURCE_BASELINE.md)，机器可读副本位于
`src/firmquant/resources/source_identity.json`。

## 唯一决策路径

策略决策只允许调用一次 `ProductionEngine.decide()`。`StrategyAdapter` 的职责仅是：

1. 验证 uquant checkout/安装包、代码、配置、数据和 universe 身份；
2. 使用已经通过账户权威校验并提交的 uquant AccountState 与完整 broker snapshot；
3. 调用传入的唯一 ProductionEngine；
4. 原样捕获结果和账户经济状态，构造不可变 DecisionSnapshot；
5. 对相同 session 与相同输入生成稳定 decision id，拒绝覆盖输入变化后的旧快照。

adapter 不实现第二套 ProductionEngine、PortfolioAllocator、Risk、Sentinel 或策略状态机，也不对经济结果做近似转换。

## AccountState 边界

uquant AccountState 是策略经济状态和持仓 lifecycle 的唯一权威。首次真实账户接入通过 `bootstrap-account` 建立持久 account
binding；空持仓账户可以从券商可用现金创建 `AccountState.empty`，非空账户必须提供严格当前 schema 的已复核 seed。seed
必须与锁定 code/data identity、券商现金、持仓、可卖数量和经济摘要一致，firmquant 不推断 tranche、attribution 或 lifecycle。

之后的 broker sync 是显式 prepare-validate-commit 协议。生产路径先加载当前 AccountState 和持久 binding，对 broker snapshot、
operational ledger 和当前策略账户做 preflight；只有已知、映射一致且已入账的系统订单/成交可以解释差异。prepare 只对深拷贝
调用锁定 uquant account-sync，生产文件和 account-operation ledger 在该阶段保持不变。prepared AccountState 还必须再次通过完整
reconciliation；全部通过后才以 expected-before CAS 提交。

AccountState 文件替换与 SQLite 不是伪装成单一物理事务，而是 crash-consistent finalization：account operation 封存 before/after、
broker evidence 与 reconciliation finalization payload；文件原子保存后，在一个 SQLite transaction 中完成 account operation
receipt、audit 和 reconciliation receipt。重启可由同一 evidence 收敛，不重新执行经济行为。

券商持仓差异不能被自动改造成新的策略 lifecycle。`ReviewedAccountAdjustment` 只允许对精确账户、symbol/session、broker
snapshot 和 difference hash 留下 append-only 人工复核证据。精确现金差异可被授权；持仓或可卖数量差异即使有 reviewed
receipt，仍要求显式 reviewed AccountState，receipt 本身不能成为忽略差异或生成新 tranche 的开关。

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
