# 当前任务状态

> 本文件只保存一份最新有效状态。更新时必须替换过期字段，不得追加互相矛盾的 SHA、测试结果或进度历史。原始日志和大型输出保存到 `evidence/` 或现有证据目录。

## 当前任务目标

为 `ychenracing/firmquant` 建立最小上下文项目状态系统：新增并校验 `PROJECT_BRIEF.md`、`TASK_STATE.md`、`ACCEPTANCE.md`，在不修改业务代码、策略逻辑、配置语义或生产行为的前提下，将长期事实、当前状态和稳定验收合同分离为单一权威文件。

## 当前验收范围

- 创建或安全合并上述三个根目录 Markdown 文件。
- 对 README、AGENTS、当前治理 Prompt、项目架构/质量文档和构建/CI 配置做最小必要读取。
- 建立旧指令到三个新文件的语义覆盖表。
- 核查三文件之间的冲突、重复、过期状态和禁止内容。
- 只验证文档治理变化；不执行业务改造，不改变生产经济行为。

权威组合验收见 `ACCEPTANCE.md`。

## Git 现场

核验时间：`2026-08-29T08:11:42Z`。

| 字段 | 当前有效值 | 核验说明 |
|---|---|---|
| 仓库 | `ychenracing/firmquant` | GitHub 仓库元数据已核验 |
| 工作树绝对路径 | 待核验 | 当前执行环境未挂载本地 Git checkout，scratch 目录为空 |
| 当前分支 | `codex/minimal-context-project-state` | 本任务远端功能分支 |
| 本地 `HEAD` | 待核验 | 无本地 checkout；在检出功能分支后运行 `git rev-parse HEAD` |
| `origin/main` | `1d2770a5cc2298f37e9bb928bb572394c9625e3c` | 2026-08-29 通过 GitHub `main` ref 核验 |
| 远端功能分支 | `codex/minimal-context-project-state` | 分支提交 SHA 待提交后核验 |
| 远端功能分支 SHA | 待核验 | 本文件位于待创建提交内，不能在提交前猜测其 SHA |
| `merge-base(HEAD, origin/main)` | `1d2770a5cc2298f37e9bb928bb572394c9625e3c` | 功能分支从该精确 main SHA 创建；本地检出后用 `git merge-base HEAD origin/main` 复核 |
| `git status --short` | 待核验 | 远端 API 没有本地工作树状态；检出后运行 `git status --short` |
| staged / unstaged / untracked | 待核验 / 待核验 / 待核验 | 同上 |
| push 状态 | 待核验 | 提交和更新远端 ref 后替换本行 |

## 已完成并验证事项

- 已核验仓库根目录、README、AGENTS、`pyproject.toml`、`.python-version`、相关架构/集成/风险/开发/质量/源码基线文档，以及三个 GitHub Actions workflow。
- 已确认 `main` 根目录不存在 `PROJECT_BRIEF.md`、`TASK_STATE.md`、`ACCEPTANCE.md`，无需合并同名旧文件。
- 已通过目标检索确认仓库内没有可用的 `HANDOFF_PROMPT`/交接文件；当前治理 Prompt 是本任务唯一当前合同。旧聊天摘要未作为现场事实使用。
- 已完成三文件的职责拆分：长期稳定事实 → `PROJECT_BRIEF.md`；单一最新状态 → `TASK_STATE.md`；当前任务稳定验收与语义覆盖 → `ACCEPTANCE.md`。
- 已将业务边界保持为引用和归类，不修改 `src/`、`tests/`、`config/`、`pyproject.toml`、`uv.lock`、`README.md`、`AGENTS.md` 或 `.github/workflows/`。

## 剩余事项及执行顺序

1. 在远端创建 `codex/minimal-context-project-state`，一次性提交三个 Markdown 文件并核验远端 ref。
2. 在可用 checkout 或 CI 中运行 `ACCEPTANCE.md` 列出的文档 L1 必须验证；将准确命令和结果替换到“最近验证”中。
3. 复查提交 diff 仅包含三个治理文件，并复核三文件无冲突、无过期现场值、无语义遗漏。
4. 由后续集成流程决定是否创建 PR/合入 `main`；本任务未授权直接合并 `main`。

## 当前阻塞、风险和 UNKNOWN

- `UNKNOWN`：本地工作树路径、`HEAD`、`git status` 和 staged/unstaged/untracked 状态无法在当前无 checkout 环境中核验。
- `UNKNOWN`：功能分支最终提交 SHA、push 后 ref 和 CI 结果在提交前不可得，禁止预写为通过。
- 当前有效交接包未在仓库或持久化文件区检索到；如果另有未提供的外部交接包，其语义尚未纳入，必须在发现后按当前现场重新比对，不能直接覆盖本文件。
- `docs/SOURCE_BASELINE.md` 记录的是当前锁定 uquant 基线；升级 uquant 时必须更新其机器/人类权威文件，不应把旧 SHA固化到 `PROJECT_BRIEF.md`。
- 本任务只改文档，完整 L4 不产生新增业务证据；不得据此声称 MiniQMT、真实账户或 LIVE 已验证。

## 最近验证

| 命令或检查 | 准确结果 |
|---|---|
| GitHub 根目录内容读取 | PASS：`main` 无三个同名治理文件 |
| 仓库目标检索：`HANDOFF HANDOFF_PROMPT 交接` | PASS：0 个结果；仅证明仓库检索未发现交接文件，不证明外部文件不存在 |
| 仓库目标检索：`PROJECT_BRIEF TASK_STATE ACCEPTANCE` | PASS：0 个结果 |
| `uv run pytest tests/unit/test_documented_defaults.py -q` | 待运行 |
| `uv run python scripts/check_docs.py` | 待运行 |
| `uv run python scripts/secret_scan.py` | 待运行 |
| 三文件结构、归属和冲突检查 | 待提交后运行 |
| GitHub Actions：CI / Security / Windows deployment safety | 待运行；不得表述为通过 |

## 下一步具体动作

完成远端提交后，立即读取功能分支 ref 和提交树，替换远端 SHA/push 状态；随后执行文档 L1 验证并用准确结果更新本文件。若只能依赖 CI，则记录 workflow 名称、run id、commit SHA、状态和未运行/skip 项，不能把 workflow 成功外推为真实 MiniQMT 验证。

## 最后更新时间

`2026-08-29T08:11:42Z`
