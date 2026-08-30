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

## 自然语言任务入口与上下文治理

本节约束任务入口、上下文加载、连续执行和交接，不削弱明确验收标准、验证要求、安全边界、数据完整性、业务与经济约束或本仓库专属规则。

- 接受用户直接用自然语言提出 GitHub 任务。能够从当前对话和 GitHub 现场解析的信息，不要求用户改写固定 Prompt、手工填写模板、提供分支名或 PR 编号，也不强制先建 Issue。
- 普通单 PR 任务以 PR 正文维护动态状态。只有确实跨多个 PR、长期分阶段、需要独立积压，或用户明确要求时才自动创建并填写 Issue。
- GitHub 当前现场是分支、SHA、commit、PR、review、checks 和合并状态的权威来源。旧聊天、记忆、计划、摘要和交接包只能作为线索。
- 新建工作前先搜索匹配的开放 PR、分支和 Issue；存在匹配项时原地继续，不得重复创建或重做。
- 先加载最小权威上下文：本文件、存在时的 `.github/CHATGPT_PROJECT_BRIEF.md`、匹配 PR 及 diff，再读取直接相关代码、测试、配置和 workflow。只有证据不足、互相矛盾或影响范围扩大时才扩读。
- 不默认加载完整仓库、完整聊天、全部 PR/Issue/Actions 或大段日志。禁止有损压缩否定条件、AND/OR 逻辑、阈值、日期、版本、路径、分支、SHA、准确结果、风险和 UNKNOWN。
- 当前环境没有本地 worktree 时，将本地路径和工作区字段标记为“不适用”，不得虚构。
- 可用时调用 `context-budget-router` 和 `conversation-continuity-guard`，但无论技能是否可用都必须遵守本文件。

## 连续执行

复杂、多步骤、长时间、GitHub、批量、研究、调试和多工具任务默认连续执行。

- 只要仍有安全、明确、可执行的下一步，就继续工作。
- 里程碑、checkpoint、commit、push、创建 PR、部分验证、进度更新和已准备交接都不等于任务完成。
- 不得仅因对话较长、文件/日志/工具较多、完成多个里程碑、下一阶段较大、可以准备交接或非 required CI 仍在运行而停止。
- 进度更新是非阻断的；发送后继续执行，不等待用户回复。下一步明确时不得要求用户回复“继续”。
- 平台未提供准确遥测时，不得声称知道剩余 token、消息数或上下文容量。

## 非阻断 Checkpoint 与上下文恢复

每完成一个有意义的里程碑：

1. 保存完整、可恢复的 checkpoint。
2. GitHub 任务刷新 PR 正文中的当前目标、已完成并验证事项、剩余事项、准确验证结果、风险、UNKNOWN 和下一步。
3. 适当时提交并推送可理解状态，再核验远端 head 和 PR。
4. 随后直接继续下一项。

普通 checkpoint 不得终止任务、把交接包作为最终输出、建议切换对话或要求确认。批量任务完成或安全保存一个对象后继续下一个；单个对象阻塞不终止仍有可执行项的批次。required checks 等待期间先完成其他可执行工作，长时间 non-required checks 不构成阻塞。

上下文可能过期时，重新读取权威仓库、PR、head/base SHA、commits、diff、reviews、checks 和剩余事项；通过只读核验消除冲突，丢弃过期叙事，刷新状态并继续。已经生成过交接包后，用户说“继续”“继续做完”或同义表达时，重新核验现场并恢复执行。

## 必须交接的真实条件

只有继续安全执行确实被以下至少一项阻断时，才停止并输出完整交接包：

1. 平台或必要工具明确达到硬限制或不可用；
2. 权限、分支保护、required approval 或外部授权阻断全部剩余工作；
3. 存在无法从既有要求安全推断的重大用户决策；
4. 现场存在只读核验无法解决的实质冲突；
5. 关键上下文已经真实丢失，且无法从权威来源恢复；
6. 继续会造成重大正确性、安全、隐私、数据完整性、经济或不可逆风险；
7. 用户明确要求停止或交接。

任务较长、里程碑/交互次数较多、文件/日志/工具较多、剩余阶段较大、已有交接包、non-required CI 等待、批次中单个仓库阻塞，或没有事实依据地担心未来达到限制，都不足以触发停止。

必须交接前，先完成最小安全原子操作，保存可恢复 checkpoint，刷新权威状态，说明真实阻塞原因，并输出独立完整的交接包；仓库、分支、SHA、worktree、测试、CI、commit、push、风险和下一步必须来自核验，不得猜测。

## 完成条件与 Git 安全

只有在目标及验收标准满足且完成必要最终验证、用户明确要求停止、真实阻塞阻断全部剩余安全工作、安全策略要求终止，或执行环境明确无法继续必要工具时才结束。`Remaining Work` 中仍有安全可执行项时必须继续。不得承诺后台异步完成。

未经明确授权，禁止 `reset`、`clean`、`rebase`、force push、改写共享历史、删除分支或 worktree、丢弃 tracked/staged/unstaged/untracked 工作、覆盖无关改动或重做已完成并验证的工作。

交接、合并或最终完成前，核验适用的当前分支、HEAD、远端功能分支 SHA、默认分支 SHA、merge-base、工作区、commits、push、reviews、checks 和准确测试结果。当前环境无法检查的字段标记为“待核验”或“不适用”，不得猜测。

## Remote Task Bootstrap

以下规则确保任务在实质修改前及重要里程碑时具备远端可恢复状态；它们补充而不替代仓库已有的业务、安全、量化、测试、CI、发布和 Git 安全规则，禁止删除或弱化这些专属规则。

- 完成最小只读核验后、任何实质修改前，先建立远端任务启动 checkpoint。新任务必须从已核验的远端默认分支 SHA 创建功能分支；先搜索匹配的 PR、分支或 Issue，若已存在则原地继续，先刷新 PR、push 当前可恢复状态并核验远端 head，再开始修改。
- 优先创建结构化空 bootstrap commit，完整记录：Objective、Acceptance criteria、Included and excluded scope、Non-negotiable constraints、Default branch and baseline SHA、Feature branch、Related PR/branch/Issue、Current verified state、Risks and unknowns、Next action。
- 将 bootstrap commit push 到远端功能分支并核验远端 head SHA；只有核验完成后才开始实质修改。环境不支持空 commit 时，可使用仅存在于功能分支的临时文件 `.github/task-bootstrap/<task-slug>.md`，但最终合并前必须删除。
- 每个正式 checkpoint 和重要里程碑都必须完成最小必要验证、提交一个完整原子状态、push、核验远端 SHA、更新 PR，然后继续。仅聊天、本地工作区、本地 commit 或临时容器状态不构成完整 checkpoint；不得为微小编辑制造提交。
- 不得推送秘密、无关改动或未完成的原子修改；未经明确授权不得直接 push 默认分支或 force push。无法 push 时必须准确报告阻塞，不得声称 checkpoint 已完成。
