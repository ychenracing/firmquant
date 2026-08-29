# ChatGPT Project Brief

> 本文件只保存长期稳定、仓库级的信息。当前任务、临时分支、SHA、测试状态和执行进度应保存在当前 Pull Request 正文中。

## 1. Project

- 项目名称：firmquant
- GitHub 仓库：`ychenracing/firmquant`
- 默认分支：`main`
- 系统定位：面向单一 A 股现金账户的安全优先日频实盘执行系统，锁定的 uquant 是唯一策略决策内核。
- 项目最终目标：把 uquant 的冻结决策安全地转换为 PAPER、SHADOW、CANARY 或 LIVE 执行，同时保证对账、恢复、审计和失败关闭。

## 2. Purpose and Non-Goals

firmquant 负责券商事实接入、订单执行、前置安全门、账户绑定、对账、恢复、运行健康和审计。它支持单用户、单账户、单 Windows 交易主机上的 A 股 AI 产业链现金多头执行。

长期非目标：

- 第二套策略、PortfolioAllocator、Base Risk、Risk Sentinel 或 AccountState；
- 研究平台、高频/日内策略、多账户、多策略或分布式交易平台；
- 自动扩大 uquant 的股票池、目标、仓位、风险或授权；
- 把 CI、Fake、contract、SHADOW 或历史回放当作真实 MiniQMT 连接或真钱许可；
- 收益保证、自动清仓或不确定状态下的无保护市价单。

## 3. Architecture and Module Boundaries

系统采用模块化单体、ports and adapters：单进程、单 SQLite operational ledger、单 account writer lease。Broker callback 作为不可信输入进入有界队列，由单 writer 推进订单、账户和审计事务。

- `application`：用例、session 编排、生命周期和 operator 命令。
- `domain`：值对象、runtime/order 状态机、领域事件和不变量。
- `strategy`：锁定身份、uquant anti-corruption adapter、AccountState prepare/commit、不可变 DecisionSnapshot。
- `broker`：Gateway ports、Fake/Paper/Replay/XtQuant adapter 和输入规范化。
- `market_data`：权威交易日历、日线 manifest、append-only 校验和 execution quote ports。
- `execution`：冻结决策到订单计划、SELL/BUY 顺序、提交和撤单政策。
- `risk`：只缩小的逐单 gate、arm lease、kill switch 和 broker-write capability。
- `reconciliation`：账户绑定、preflight 以及 broker/uquant/firmquant 对账。
- `persistence`：SQLite migration、事务 repository、single writer、恢复、备份和 hash-chain audit。
- `scheduling`：Asia/Shanghai session、时钟检查和可恢复 workflow receipt。
- `observability` / `security`：结构化日志、报告、告警、secret provider、脱敏和扫描。

权威 Owner：

- Broker：可用现金、总资产、真实/可卖持仓、broker ID、订单、成交、费用和实时证券状态。
- uquant：机会、风险、Sentinel、目标组合、策略持仓生命周期、经济订单 ID、策略配置、数据身份和 canonical universe。
- firmquant：broker 映射、提交尝试、callback、UNKNOWN、账户绑定、对账、arm lease、kill switch、运行健康和审计。
- 源码身份：`src/firmquant/resources/source_identity.json`；人类可读基线：`docs/SOURCE_BASELINE.md`。

不得建立与上述 Owner 竞争的第二经济账户、第二目标组合、第二策略参数源或第二 broker truth。

## 4. Non-Negotiable Constraints

- 锁定的 uquant 是唯一策略决策内核，策略决定只走 `ProductionEngine.decide()`。
- uquant 独占 PortfolioAllocator、Base Risk、FREEZE_ONLY Risk Sentinel、策略参数、目标组合、AccountState 生命周期和 canonical AI universe；部署 allowlist 只能是其子集。
- firmquant 只能阻止、缩小、延迟、取消或 HALT，绝不能扩大目标、gross exposure、单票权重、买入数量、股票池或风险授权。
- 经济范围固定为单一 A 股 AI 产业链现金多头账户：无杠杆、禁止做空、无衍生品、不扩展多账户/多策略。
- 日频经济路径固定为盘后决策、下一交易日执行；盘中只处理订单生命周期、成交、断线、freshness、风险阻断和对账。
- PAPER 是默认模式，示例配置固定 `live_trading_enabled = false`；REPLAY、PAPER、SHADOW 和 CI 在结构上不可到达真实 submit/cancel。
- CANARY/LIVE 每次 broker write 都必须重新通过短时授权、合规、身份、对账、freshness、session、kill switch、UNKNOWN、现金/持仓和逐单 gate；证据缺失或不确定即失败关闭。
- CANARY 不自动晋级 LIVE；readiness 只读，不创建 arm，不发送订单。
- 紧急处理阻止新订单，只撤销可证明由 firmquant 拥有的活动订单并 HALT；不自动清仓。
- uquant AccountState 是唯一策略经济状态；broker facts 只通过绑定、preflight、内存 prepare、最终 reconciliation 和 expected-before CAS commit 进入。
- 人工交易、外部订单、异常现金、身份漂移、UNKNOWN 或证据冲突会阻断提交，不能自动吸收或猜测。
- Money、price 和 fee 使用 Decimal 或整数最小单位；share quantity 使用整数值对象；所有外部 payload 都是不可信输入。
- Fake、contract、CI、skip 或未运行检查不能证明真实 MiniQMT 账户；测试禁止发送真实订单。

## 5. Authoritative Sources

- 项目入口和安全姿态：`README.md`
- 工程方法、验证和 Git 安全：`AGENTS.md`
- 架构：`docs/ARCHITECTURE.md`
- uquant 唯一路径、AccountState、universe 和 parity：`docs/STRATEGY_INTEGRATION.md`
- 风险、mode、lease 和 write capability：`docs/RISK_AND_SAFETY.md`
- 运维、事故和恢复：`docs/OPERATIONS.md`、`docs/RECOVERY.md`
- 质量和开发：`docs/QUALITY.md`、`docs/DEVELOPMENT.md`
- 源码基线：`src/firmquant/resources/source_identity.json`、`docs/SOURCE_BASELINE.md`
- Python、依赖、构建和工具：`pyproject.toml`、`uv.lock`
- 安全示例配置：`config/firmquant.example.toml`
- 生产实现：`src/firmquant/`
- CI、security 和 Windows 部署安全：`.github/workflows/`
- 版本和发布：Git 标签、GitHub Releases 和 `pyproject.toml` 版本

## 6. Standard Commands

Python 要求为 `>=3.12,<3.13`，依赖由 `uv.lock` 管理。

```bash
uv sync --frozen --extra dev
uv run ruff check .
uv run ruff format --check .
uv run mypy src scripts
uv run pytest --cov=firmquant --cov-branch --cov-fail-under=85
uv run python -m compileall -q src scripts tests
uv run bandit -q -r src scripts
uv run pip-audit --cache-dir .pytest_cache/pip-audit
uv run python scripts/secret_scan.py
uv run python scripts/verify_source_baseline.py
uv run python scripts/build_reproducible_wheels.py --verify-twice
uv run python scripts/check_docs.py
```

PAPER-only 本地启动：

```bash
cp config/firmquant.example.toml config/firmquant.local.toml
uv sync --frozen --extra dev
uv run firmquant init
uv run firmquant doctor
uv run firmquant run --mode paper
uv run firmquant status
```

仓库不提供可直接复制的实盘启动链路；SHADOW/CANARY/LIVE 必须按运维与安全文档在合法部署机执行。

## 7. Important Paths

- `src/firmquant/`：生产实现。
- `src/firmquant/resources/source_identity.json`：机器可读 uquant/源码身份。
- `config/firmquant.example.toml`：默认禁用实盘写的安全示例。
- `tests/`：unit、contract、integration、property、fault、E2E 和 parity 证据。
- `scripts/`：source identity、docs、secret、reproducible build 和部署检查。
- `docs/ARCHITECTURE.md`：模块与 Owner 边界。
- `docs/STRATEGY_INTEGRATION.md`：uquant 集成契约。
- `docs/RISK_AND_SAFETY.md`：风险和授权边界。
- `docs/OPERATIONS.md`、`docs/RECOVERY.md`：运维和恢复。
- `pyproject.toml`、`uv.lock`：语言、依赖和工具链。
- `.github/workflows/`：CI、安全和 Windows 部署安全门。

## 8. CI and Acceptance Entry Points

- `.github/workflows/ci.yml`：Linux/Windows source identity、Ruff、format、mypy、secret scan、branch coverage、compile、docs、parity 和 deterministic wheel。
- `.github/workflows/security.yml`：Bandit、pip-audit、secret scan 和 gitleaks。
- `.github/workflows/windows.yml`：锁定身份、持久化/doctor/adapter 定向测试、PAPER-only smoke 和 CLI。
- 绿色 CI 只证明仓库自动化，不证明真实 MiniQMT 连接或授权 CANARY/LIVE。
- 本地验证按 `AGENTS.md` 的影响范围和 L1–L4 层级执行；生产/实盘声明还需要目标机只读 smoke 和相应人工/机器门。
- Definition of Done：验收逐项满足；required checks 通过；没有阻断 review；运行/未运行证据准确；不扩大业务、风险或授权边界。

## 9. Prohibited Actions

- 不得复制、近似或另建 uquant 策略、PortfolioAllocator、Base Risk、FREEZE_ONLY Sentinel、target construction、参数或 AccountState 生命周期。
- 不得扩大 universe、target、exposure、weight、quantity 或 authorization；不得绕过 gate/state machine、盲重发 UNKNOWN 或自动吸收人工活动。
- 不得为了通过检查而改变生产配置语义、策略参数、依赖或 workflow。
- 不得提交账号、密码、token、webhook secret、MiniQMT userdata、真实账户快照、未脱敏成交、SDK、wheel、数据库、日志或事故 payload。
- 不得把 fake/CI/contract/skip 当作真实账户验证，也不得从测试、CI 或 smoke 发送真实 broker write。
- 不得从 dirty tree、临时分支、floating main 或 floating tag 构建生产 uquant 输入。
- 不得擅自改写 Git 历史或 force push。
- 不得丢弃未知或未提交工作，也不得覆盖无关改动。
- 不得把计划执行写成已验证完成。
- 不得根据旧聊天猜测当前分支、SHA、PR 或 CI 状态。

## 10. Context Loading Protocol

1. 新开发任务可以直接使用自然语言提出，不要求预先填写固定 Prompt。
2. 开始任务时先读取本文件。
3. 搜索与任务相关的开放 PR、分支和 Issue。
4. 如果存在匹配工作，从现有现场原地继续。
5. 当前动态任务状态默认维护在 Pull Request 正文。
6. 不强制普通单 PR 任务创建 Issue。
7. 优先读取目标代码、直接调用者、相关测试和直接相关配置。
8. 只有证据不足、状态冲突或影响范围扩大时才扩大读取。
9. 不默认加载完整仓库、完整聊天、完整日志或全部 GitHub Actions 历史。
10. 长对话交接使用 `conversation-continuity-guard`，但 GitHub 当前现场仍是状态权威来源。

## 11. References

- `README.md`
- `AGENTS.md`
- `docs/ARCHITECTURE.md`
- `docs/STRATEGY_INTEGRATION.md`
- `docs/RISK_AND_SAFETY.md`
- `docs/OPERATIONS.md`
- `docs/RECOVERY.md`
- `docs/QUALITY.md`
- `docs/DEVELOPMENT.md`
- `docs/SOURCE_BASELINE.md`
- `pyproject.toml`
- `uv.lock`
- `.github/workflows/`
