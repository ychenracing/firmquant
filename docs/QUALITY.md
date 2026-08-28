# 质量与验证

验证目标是获得新的证据，不是重复运行昂贵矩阵。测试永远不能连接真实券商写接口或读取开发机 secret。

## 渐进式阶梯

| 层级 | 触发条件 | 典型证据 |
|---|---|---|
| L1 | 局部代码/文档变化 | 直接单元节点、Ruff、相关 strict mypy、最小复现 |
| L2 | 模块 milestone | 模块测试、Broker contract、SQLite migration/transaction、小型 E2E |
| L3 | 跨模块行为稳定 | PAPER/REPLAY/SHADOW contract session、恢复、故障注入、Windows smoke |
| L4 | 最终交付候选 | 全工程、安全、经济等价、覆盖率、构建和双平台 CI |

L4 后只有行为/依赖/schema/配置/数据/runner/共享构建发生实质变化，或证据表明范围不足，才在新候选稳定后重跑完整
L4。失败先定位并运行失败节点与直接相邻检查。

## 测试证据

- unit：值对象、Decimal、symbol、运行/订单状态、arm、kill switch、risk gate、配置、日志和报告。
- contract：所有 BrokerGateway 的共同读取/写禁止语义，XtQuant 官方签名 fake。
- integration：SQLite、audit、backup、reconciliation、capability、notifier 隔离、startup recovery。
- properties：成交不超量、现金/持仓非负、终态不回退、事件幂等、执行不扩张、非实盘不写、replay 确定。
- fault：callback、submit/cancel 不确定性、断线、stale quote、交易状态、人工活动、身份漂移、锁/损坏和关键崩溃点。
- e2e：盘后决策、次日执行、SELL 后 BUY、部分成交、迟到成交、EOD、日报、restart/replay 等价。
- parity：StrategyAdapter 与直接 uquant 决策完整严格相等。
- execution evidence：SHADOW/CANARY observation 必须按 session 不可变、identity-bound，聚合只能从 observation 派生；LIVE 不能把旧 READY audit 当成替代证据。
- execution-aware replay：理论端和执行端必须使用同一锁定 uquant、canonical universe 和 frozen data；完整验收区间固定采用 uquant `continuous_ai_era`（2023-01-03 至 2026-08-05），输出累计收益、回撤、换手、费用、slippage、未成交损失和目标跟踪误差，不通过调参修饰差异。

## 最终工程门

稳定候选依次验证 frozen sync、Ruff check/format、strict mypy、pytest branch coverage、compileall、Bandit、pip-audit、
secret scan、确定性 wheel、source baseline、文档链接/CLI help、Broker contract、parity、PAPER/REPLAY E2E、restart
recovery 和 Windows smoke。分支覆盖率门为 85%。

常用命令：

```bash
uv sync --frozen --extra dev
uv run ruff check .
uv run ruff format --check .
uv run mypy src scripts
uv run pytest --cov=firmquant --cov-branch --cov-fail-under=85
uv run python -m compileall -q src tests
uv run bandit -c pyproject.toml -r src
uv run pip-audit
uv run python scripts/secret_scan.py
uv run python scripts/build_reproducible_wheels.py --verify-twice
uv run python scripts/verify_source_baseline.py
uv run python scripts/check_docs.py
```

XtQuant 专有 SDK 不进入通用 CI。SDK 相关验证分为无 SDK contract、存在时 import/schema、本机只读账户 smoke；任何
自动化层都禁止 submit/cancel。

## CI 与证据解释

Linux CI 验证核心工程与确定性构建；Windows CI 验证 Python/路径/zoneinfo/SQLite 锁/备份和 CLI 的 PAPER 安全。
workflow 成功不代表 MiniQMT 账户接通。真实 SDK 是否存在、只读 smoke 是否执行、跳过原因和未验证事项必须在交付报告
中逐项陈述，不能把 timeout、skip 或未运行写成通过。

## 停止条件

验收标准、覆盖率、安全门和文档一致性全部满足且无重大正确性/数据损失/经济等价问题后停止。不为代码行数、抽象数量
或治理信号继续低收益拆分。
