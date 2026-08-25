# Agent Working Agreement

本文件适用于在 firmquant 仓库工作的 Codex、ChatGPT Work 和其他编码代理。用户当次任务的明确要求与验收
标准优先；设计记录和历史计划不能覆盖当前合同。

## 系统边界

- firmquant 是单账户、A 股 AI 产业链、现金多头、日频实盘执行系统，不是策略研究项目、高频系统或第二策略。
- 锁定的 uquant 是唯一策略决策内核。只通过 `ProductionEngine.decide()` 决策；PortfolioAllocator、Base Risk、
  FREEZE_ONLY Risk Sentinel 及策略 AccountState 的经济职责仍由 uquant 独占。
- firmquant 的执行与安全层只能阻止、缩小、延迟或取消订单以及进入 HALT，绝不能扩大 uquant 的目标、总仓、
  单票权重或买入数量。
- 不修改 uquant，不复制其策略实现，不从未提交工作树、临时分支、浮动 main 或浮动 tag 构建生产依赖。
- 当前范围不扩展到多账户、多策略、Web 管理后台、云端多租户、融资融券、做空、期货、期权或其他衍生品。

## 最高优先级安全规则

- 默认模式始终为 PAPER，`live_trading_enabled` 默认且示例中始终为 `false`。
- REPLAY、PAPER、SHADOW 和 CI 在结构上不得触达真实 `submit_order` 或 `cancel_order`。
- CANARY/LIVE 的任何券商写操作必须经过短时效 arm lease、合规确认、身份绑定、启动对账、数据与 quote freshness、
  session、kill switch、UNKNOWN order 和逐单 ExecutionRiskGate 的全部门禁；缺一即拒绝。
- 券商返回未知、提交超时、断线、重复/乱序事件、数据库异常或账户差异时失败关闭。不得把调用异常解释为未接单，
  不得盲目重发，不得在状态不确定时自动市价清仓。
- 金额、价格、数量和费用边界使用 Decimal 或整数最小单位；外部 payload 一律视为不可信输入。
- 测试、CI、smoke 和实现过程禁止提交真实订单。XtQuant SDK 不进入通用 CI，缺少合法本机环境时只陈述未验证事实。
- 不提交或记录账户号、密码、token、webhook secret、MiniQMT 敏感 userdata、真实账户快照或未脱敏成交。

## 工程方法

- 功能和缺陷修复采用测试驱动：先写能证明所需行为的失败测试，确认失败原因正确，再写最小实现并保持测试绿色。
- 外部事实、状态推进、幂等键、订单事件、对账和审计写入必须有严格类型和事务边界；callback 只验证入队，单 writer
  串行改变状态。
- 优先模块化单体与端口适配器。只有实际确认 Python 3.12 与官方券商 SDK 不兼容时，才引入认证的 localhost bridge。
- 修改前阅读附近代码、测试和当前文档；维护用户未提交的无关改动。文件编辑使用可审查补丁，不做破坏性 Git 操作，
  不 force push。
- 失败时先定位根因并复现最小失败项；不要通过删除场景、放宽门禁、近似经济断言或绕过状态机使测试变绿。

## 渐进式验证阶梯

验证以影响范围和新增证据为驱动。成功的更宽验证在其覆盖的行为、配置、数据和运行输入未变化时仍然有效，不重复运行。

### L1 — 直接影响验证

日常小改动使用：直接相关单元/性质测试、最小复现、Ruff、strict mypy、schema 或 compile 检查。

### L2 — 模块与小型集成

有意义模块 milestone 使用：受影响模块、Broker contract、SQLite migration/transaction、相关小型 E2E 和恢复点。

### L3 — 完整非实盘 session

跨模块行为使用：完整 PAPER/REPLAY/SHADOW session、崩溃恢复、故障注入、Windows smoke 与相应 parity 数据集。

### L4 — 最终候选验收

只在拟交付的稳定候选上运行一次完整工程、安全、经济等价、故障注入、构建、Linux/Windows 和文档验收。L4 后只有
行为、依赖、schema、配置、数据、runner 或共享构建基础发生实质变化才先局部复验，并在新候选稳定后重跑 L4。

## 验证与失败处理

1. 从能证明当前变化的最低层开始；跨共享策略、风险、订单、数据库、安全和构建边界时再升级。
2. 测试失败先确认预期与实际、调用路径、状态和输入来源，运行失败项及直接相邻检查。
3. milestone 后运行对应 L2/L3；不要每个小补丁都启动全量矩阵。
4. 经济结果只有在 uquant commit、配置、数据 manifest、universe、账户、broker snapshot、as-of、runtime lock 完全
   一致时才能复用；parity 禁止近似断言掩盖差异。
5. 最终候选至少通过 Python 3.12、`uv sync --frozen`、Ruff、strict mypy、pytest、分支覆盖率 85%、compileall、
   Bandit、pip-audit、确定性 wheel、secret scan、Broker contract、parity、PAPER/REPLAY E2E 和 restart recovery。

## Git、证据与文档

- 每个 checkpoint 应是可理解、可继续且验证范围明确的 commit；检查 diff 和 secret 后推送工作分支。
- 只在最终 L4 通过、完整 diff 审查和工作树干净后通过 PR 合入 main；禁止 force push 和无关 tag/release。
- README、`docs/` 当前主题文档和 ADR 只描述当前系统。不要堆积版本号叙事、阶段流水账、临时审查报告、无效 TODO
  或重复参数表。Git 历史承担变更历史职责。
- 原始 broker event、domain event、DecisionSnapshot、reconciliation、风险事件、backup verification 和 hash-chain audit
  evidence 必须保留真实失败与差异；不得自动删除事故现场或伪造 SDK、账户、成交、CI 或测试结果。

## 停止条件

显式验收标准通过且不存在已知正确性、安全、数据损失或重大经济等价问题后停止。不要为了代码行数、抽象数量或所谓
工业级继续低收益拆分和扩展。
