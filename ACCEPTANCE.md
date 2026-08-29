# 最小上下文项目状态系统验收合同

> 本文件保存当前治理任务的稳定验收标准，不记录“已经通过”的运行状态。实际进度、命令和结果只写入 `TASK_STATE.md`。

## 必须满足的功能条件

1. 根目录存在 `PROJECT_BRIEF.md`、`TASK_STATE.md`、`ACCEPTANCE.md`。
2. `PROJECT_BRIEF.md` 只包含长期稳定的项目目标、定位、架构/模块边界、不可变业务约束、权威 owner、标准命令、索引和禁止事项；不得包含当前分支、临时 SHA、当前测试结果、本轮进度、过程流水账或完整日志。
3. `TASK_STATE.md` 只包含一份当前有效状态，并包含目标、验收范围、分支/HEAD/origin/main/远端功能分支/merge-base、git status、已完成项、剩余顺序、阻塞/风险/UNKNOWN、准确验证结果、下一动作和最后更新时间。
4. 无法从当前现场核验的字段必须逐项写为“待核验”，并给出核验方法；不得根据旧聊天、旧摘要或旧交接猜测。
5. `ACCEPTANCE.md` 必须明确功能条件、AND/OR 逻辑、非回归、安全/数据完整性、必须验证、可选检查、失败条件和 Definition of Done。
6. 同名文件存在时必须先读后语义合并；不存在时才新增。任何更新都替换过期值，不并列保存互相冲突的状态快照。
7. 原始日志和大型证据保存到 `evidence/` 或项目现有证据目录，只在 `TASK_STATE.md` 保留结论、准确结果和证据位置。
8. README、AGENTS、当前任务 Prompt、当前有效交接包（若存在）以及构建/测试/验收直接相关配置中的每条硬约束，都必须在下方语义覆盖表中有明确归属。
9. 本任务只允许新增或更新治理 Markdown；不得改变业务代码、策略逻辑、配置语义、依赖锁、workflow 语义或生产行为。

## 组合逻辑

本任务验收为以下条件的严格 AND：

```text
ThreeFilesPresent
AND ProjectBriefStableOnly
AND TaskStateSingleAndCurrent
AND AcceptanceExplicit
AND UnknownsNeverGuessed
AND SemanticCoverageComplete
AND NoBusinessOrProductionChange
AND RequiredValidationRecordedAccurately
AND CrossFileConsistency
```

任一条件失败即整体失败。不存在“多数满足”、以 CI 代替真实环境、以计划代替结果或以旧聊天代替当前现场的 OR 捷径。

## 非回归要求

- `src/`、`tests/`、`config/`、`pyproject.toml`、`uv.lock`、`.github/workflows/`、README、AGENTS 和现有 `docs/` 的内容与语义保持不变。
- uquant 唯一策略 owner、`ProductionEngine.decide()` 唯一路径、PortfolioAllocator/Base Risk/FREEZE_ONLY Sentinel/AccountState owner、canonical universe 子集约束和 firmquant 只收缩原则保持不变。
- PAPER 默认、`live_trading_enabled = false`、非实盘模式无真实写权限、CANARY/LIVE 全门禁、UNKNOWN 失败关闭和禁止真实测试下单保持不变。
- 不新增第二状态机、第二经济账户、策略参数副本、生产写旁路或任何运行时依赖。

## 安全和数据完整性要求

- 不读取、写入或暴露账户号、密码、token、webhook secret、MiniQMT 敏感 userdata、真实账户快照或未脱敏成交。
- 不发送真实 broker submit/cancel，不 arm，不改变运行状态，不触发生产恢复或账户 bootstrap。
- 不通过 reset、clean、rebase、force push、破坏性 checkout/restore 或覆盖未提交工作获得“干净现场”。
- Git 字段、测试结果、CI、SDK 和真实账户验证必须区分“已验证”“待核验”“未运行”“skip”和“失败”；不得伪造或外推。
- 三文件的 canonical 职责必须保持：长期事实只在 Brief，瞬时状态只在 Task State，稳定验收只在 Acceptance。必要的交叉引用不构成第二份 owner。

## 必须运行的验证

在可用 checkout 或该功能分支 CI 上运行并把准确结果写入 `TASK_STATE.md`：

```bash
git diff --check
git status --short
git diff --name-only <merge-base>...HEAD
uv run pytest tests/unit/test_documented_defaults.py -q
uv run python scripts/check_docs.py
uv run python scripts/secret_scan.py
```

还必须执行一次三文件治理检查，至少证明：

- `PROJECT_BRIEF.md` 不含当前功能分支、当前 firmquant HEAD/merge-base、当前测试通过数或本轮进度；
- `TASK_STATE.md` 所有要求字段均存在，所有不可核验值均明确为“待核验”；
- `ACCEPTANCE.md` 的严格 AND、失败条件和 DoD 完整；
- 提交的变更文件集合只包含这三个治理文件；
- 三文件之间没有矛盾的 owner、SHA、状态或“计划即通过”表述。

## 可以跳过的可选检查

因为本任务不改变业务行为、依赖、schema、配置、数据、runner 或共享构建基础，以下检查可以跳过，但必须记录为“未运行/不适用”，不得写成通过：

- 完整 pytest 与分支覆盖率 L4；
- uquant parity 和可复现 wheel；
- PAPER/REPLAY/SHADOW 完整 session；
- execution-aware Replay；
- restart recovery、故障注入和 Windows deployment smoke；
- XtQuant SDK import/schema 和真实部署机只读 smoke；
- 任何 CANARY/LIVE 或真实 broker 写验证。

CI / Security / Windows workflow 可以作为附加非回归证据，但 workflow 绿色不等于 MiniQMT 或真实账户已验证。

## 明确失败条件

- 缺少任一治理文件，或同名旧文件被直接覆盖而未语义合并。
- `PROJECT_BRIEF.md` 混入当前 SHA、当前测试结果、任务进度、历史流水账或完整日志。
- `TASK_STATE.md` 同时保留新旧冲突 SHA/结果/进度，或把无法核验的值写成确定事实。
- `ACCEPTANCE.md` 把“待运行”“计划”“workflow 可能覆盖”写成已通过。
- 旧硬约束在覆盖表中无 owner，或在压缩中丢失否定词、例外、数字、阈值、路径、SHA、AND/OR、风险或禁止事项。
- 变更涉及业务代码、测试行为、配置语义、依赖、workflow 或生产运行时。
- 通过降低门禁、删除约束、减少必要验证、模糊 UNKNOWN 或伪造证据完成任务。
- 泄露敏感数据，发送真实订单，或执行破坏性 Git 操作。

## 旧指令到新文件的语义覆盖表

| 旧权威来源 / 硬约束主题 | 新 owner | 覆盖方式 |
|---|---|---|
| README：项目最终目标、单账户执行系统定位 | `PROJECT_BRIEF.md` | “项目名称和最终目标”“系统定位” |
| README / AGENTS：A 股 AI 产业链、现金多头、无杠杆、禁止做空、非多账户/多策略 | `PROJECT_BRIEF.md` | “系统定位”“不可改变的业务约束” |
| README / 架构文档：模块化单体、SQLite、单 writer、模块边界 | `PROJECT_BRIEF.md` | “关键架构和模块边界” |
| 架构/集成文档：券商、uquant、firmquant 三类权威 owner | `PROJECT_BRIEF.md` | “权威数据源和状态 owner” |
| README / AGENTS / 集成文档：锁定 uquant、唯一 `ProductionEngine.decide()`、PortfolioAllocator/Base Risk/FREEZE_ONLY/AccountState owner | `PROJECT_BRIEF.md` | “不可改变的业务约束”“明确禁止事项” |
| 集成文档：canonical universe 只能缩小、不能扩展 | `PROJECT_BRIEF.md` | “不可改变的业务约束” |
| README / 风险文档：firmquant 只能阻止/缩小/延迟/取消/HALT | `PROJECT_BRIEF.md` | “不可改变的业务约束” |
| README / AGENTS / workflow：PAPER 默认、`live_trading_enabled = false`、非实盘/CI 无真实写权限 | `PROJECT_BRIEF.md` | “系统定位”“不可改变的业务约束” |
| 风险文档：CANARY/LIVE 多重门禁、UNKNOWN 失败关闭、无自动清仓 | `PROJECT_BRIEF.md` | “不可改变的业务约束”“明确禁止事项” |
| README / 集成文档：AccountState binding/preflight/prepare/reconcile/CAS | `PROJECT_BRIEF.md` | “不可改变的业务约束” |
| README：运行模式、运行状态、订单状态 | `PROJECT_BRIEF.md` | “不可改变的业务约束” |
| AGENTS：Decimal、外部输入、secret、真实订单、Git 安全禁止项 | `PROJECT_BRIEF.md` | “不可改变的业务约束”“明确禁止事项” |
| pyproject / QUALITY / workflows：Python 3.12、frozen sync、Ruff、strict mypy、85% branch coverage、security/build/docs 命令 | `PROJECT_BRIEF.md` | “标准构建、测试、lint 和运行命令” |
| README / docs / 仓库结构：重要文件与目录 | `PROJECT_BRIEF.md` | “重要目录和文件索引” |
| 当前治理 Prompt：目标、范围、当前分支/SHA/status、完成/剩余/风险/验证/下一步/时间 | `TASK_STATE.md` | 对应当前状态各章节；不可核验值为“待核验” |
| 当前治理 Prompt：三文件内容规则、严格组合、非回归、安全、必须/可选验证、失败、DoD | `ACCEPTANCE.md` | 本文件各验收章节 |
| 当前治理 Prompt：原始日志不进入 TASK_STATE | `PROJECT_BRIEF.md` + `TASK_STATE.md` | 索引和文件头明确 evidence 路由 |
| 当前治理 Prompt：同名文件先读后合并、状态替换过期值 | `ACCEPTANCE.md` + `TASK_STATE.md` | 功能条件与单一最新状态规则 |
| 当前有效交接包 | `TASK_STATE.md` | 当前未检索到，明确列为 UNKNOWN；发现后必须重新核验并替换状态 |
| 历史过程、完整日志、旧交接和旧聊天摘要 | `evidence/` 或 Git 历史，不进入三文件正文 | 只在需要时精确回查，不作为当前现场事实 |

覆盖审计必须保留否定词、数字、阈值、路径、SHA、AND/OR、风险和禁止事项。若后续发现新的当前权威指令，先确定其属于长期事实、瞬时状态还是稳定验收，再只更新对应 owner；必要的引用不得演变成重复权威副本。

## Definition of Done

仅当以下全部成立时完成：

1. 三个文件已在功能分支提交，diff 只包含这三个治理文件；
2. 所有功能条件、严格 AND、非回归、安全和数据完整性要求均满足；
3. 必须验证已运行，准确命令和结果已写入 `TASK_STATE.md`；无法运行的项目仍明确为“待核验/未运行”，且不会被误报为完成；
4. 语义覆盖表无硬约束遗漏；
5. 三文件无冲突、重复 owner、过期状态或计划即通过表述；
6. 未修改业务代码、策略、配置、依赖、workflow 或生产行为；
7. 所有已知阻塞、风险和 UNKNOWN 已明确保留，并给出下一步核验动作。
