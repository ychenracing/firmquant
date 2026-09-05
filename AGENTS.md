# firmquant 工作约定

## 任务、上下文与方法

在平台权限内执行当前任务的明确范围和验收标准，保留项目业务、安全、数据、经济、CI 和发布合同。编辑目录前读取适用的嵌套 `AGENTS.md`。历史计划只提供上下文；只读分析、先审批后修改的要求，在获授权前保持只读。

以 GitHub 当前分支、SHA、PR、review、checks 为状态依据；有本地工作树时检查未知改动。已有匹配任务原地恢复，不创建替代 PR、不重做已验证工作。先读存在时的 `.github/CHATGPT_PROJECT_BRIEF.md`、相关 README/主题文档、PR/diff 和直接相关代码、测试、配置、workflow，按影响扩读，不默认加载全部技能、日志或历史。准确保留约束和证据身份。

技能是按需方法，不是额外授权来源。对于已明确授权、范围清晰的任务，不重复索要设计批准、执行方式选择，不强制 full 模式或仪式性技能播报；平台强制技能规则和真实审批门仍有效。可用工具足够时，缺少可选技能不构成阻塞。优先最小充分实现和已有依赖；计划只记录重要决策、依赖和验收，不再写一遍实现。批量处理相关修改；只并行独立工作，共享文件、runtime 和证据身份保持单 writer。

## 系统边界

- firmquant 是单账户、A 股 AI 产业链、现金多头、日频实盘执行系统，不是策略研究、高频系统或第二策略。
- 锁定的 uquant 是唯一策略决策内核，只通过 `ProductionEngine.decide()` 决策；PortfolioAllocator、Base Risk、FREEZE_ONLY Risk Sentinel 和策略 AccountState 的经济职责由 uquant 独占。
- 执行与安全层只能阻止、缩小、延迟、取消订单或进入 HALT，不能扩大 uquant 的目标、总仓、单票权重或买入数量。
- 不修改或复制 uquant 策略，不从未提交工作树、临时分支、浮动 main/tag 构建生产依赖。
- 不扩展到多账户、多策略、Web 管理后台、云端多租户、融资融券、做空、期货、期权或其他衍生品。

## 实盘与数据安全

- 默认模式为 PAPER；`live_trading_enabled` 默认及示例始终为 `false`。REPLAY、PAPER、SHADOW 和 CI 在结构上不得触达真实 `submit_order` 或 `cancel_order`。
- CANARY/LIVE 券商写操作必须通过短时效 arm lease、合规确认、身份绑定、启动对账、数据与 quote freshness、session、kill switch、UNKNOWN order 和逐单 ExecutionRiskGate 的全部门禁；缺一即拒绝。
- 券商返回未知、提交超时、断线、重复/乱序事件、数据库异常或账户差异时失败关闭。调用异常不代表未接单；不得盲目重发，不得在状态不确定时自动市价清仓。
- 金额、价格、数量和费用边界使用 Decimal 或整数最小单位；外部 payload 是不可信输入。
- 测试、CI、smoke 和实现过程禁止真实订单。XtQuant SDK 不进入通用 CI；缺少合法本机环境必须标记未验证。
- 不提交或记录账户号、密码、token、webhook secret、MiniQMT 敏感 userdata、真实账户快照或未脱敏成交。

## 工程与证据

- 功能和缺陷修复先用失败测试证明所需行为并确认失败原因，再写最小实现。外部事实、状态推进、幂等键、订单事件、对账和审计写入使用严格类型与事务边界；callback 只验证入队，由单 writer 串行改变状态。
- 优先模块化单体与端口适配器；只有确实确认 Python 3.12 与官方券商 SDK 不兼容时才引入认证的 localhost bridge。
- 不通过删除场景、放宽门禁、近似经济断言或绕过状态机修绿。保留真实 broker/domain event、DecisionSnapshot、reconciliation、风险事件、backup verification 和 hash-chain audit evidence；不删除事故现场、不伪造结果。

## 渐进式验证

L1：直接相关单元/性质测试、最小复现、Ruff、strict mypy、schema 或 compile。L2：受影响模块、Broker contract、SQLite migration/transaction、小型 E2E 和恢复点。L3：完整非实盘 PAPER/REPLAY/SHADOW session、崩溃恢复、故障注入、Windows smoke 和对应 parity 数据集。L4：稳定候选完整工程、安全、经济等价、故障注入、构建、Linux/Windows 和文档验收。

从能证明变化的最低层开始，跨策略、风险、订单、数据库、安全或构建边界时扩大。失败先定位根因，运行失败项及直接受影响检查；相关修复批量完成后再扩验，不逐补丁重跑全矩阵。稳定候选完整验收一次；后续行为、依赖、schema、配置、数据、runner、构建实质变化或发现原证据不足，先局部复验，再对新稳定候选完成适用验收。换消息、代理或交接本身不使证据失效。

经济结果只有在 uquant commit、配置、数据 manifest、universe、账户、broker snapshot、as-of、runtime lock 完全一致时可复用；parity 禁止近似断言掩盖差异。新 HEAD 仍须通过适用精确 SHA 检查，不能沿用旧 CI 状态冒充新 HEAD 通过。

工程最终候选至少满足 Python 3.12、`uv sync --frozen`、Ruff、strict mypy、pytest、分支覆盖率 85%、compileall、Bandit、pip-audit、确定性 wheel、secret scan、Broker contract、parity、PAPER/REPLAY E2E 和 restart recovery。纯文档变更检查相关链接、命令与文档合同；指令变更另检查触发、授权和完成边界，不因改指令重算未变的经济结果，但不豁免明确要求的检查。

## 授权、Git 与恢复

安全、明确、已授权的下一步直接继续；可读取解决的事实不重复询问。涉及花钱、实盘、凭据/权限、不可逆操作或无关外部写入，不从沉默推断授权。一个目标阻塞时继续其他独立已授权项。

已有分支原地继续；新任务使用功能分支。首个完整成果和重要里程碑检查 diff、秘密和必要验证后提交、推送、核验远端 SHA；不要求空 bootstrap commit、临时 bootstrap 文件或普通任务另建 Issue。动态状态放当前 PR，不堆进永久指令。仅在适用最终验收、完整 diff 审查、required checks 和工作树要求满足后通过 PR 合入 main；原工程 L4 合同保持有效，不 force push、不创建无关 tag/release。

未经明确授权，不执行 `reset`、`clean`、`rebase`，不改写历史、删除分支/worktree、丢弃 tracked/staged/unstaged/untracked 工作或覆盖无关改动。远端保存失败如实报告，不冒充 checkpoint 完成。

## 文档与完成

README、`docs/` 和 ADR 描述当前系统；历史由 Git 保存，不积累流水账、临时审查报告、无效 TODO 或重复参数表。默认审查完整任务 diff 一次，重大修复或风险变化后做针对性复审。

明确验收通过且无已知重大正确性、安全、数据损失或经济等价问题后停止，不继续低收益拆分扩展。checkpoint、PR、部分测试通过或交接包不是完成。无安全已授权操作可执行时，保存可恢复状态并分别报告已完成、阻塞和未验证事项；不承诺后台完成。交付/合并前核验适用远端 SHA、完整 diff、reviews、required checks 和证据；不存在的本地字段标记不适用。
