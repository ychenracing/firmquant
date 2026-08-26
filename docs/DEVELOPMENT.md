# 开发指南

## 环境

项目只支持 Python 3.12，依赖由 `uv.lock` 冻结。开发机在仓库根目录执行：

```bash
uv sync --frozen --extra dev
uv run firmquant --help
```

不要降低 Python 版本迁就券商 SDK。只有部署机实际证明不兼容时才按
[架构文档](ARCHITECTURE.md) 的本机 bridge 边界隔离 SDK。

## 源码结构

业务代码位于 `src/firmquant`，测试按 `unit`、`contract`、`integration`、`properties`、`fault`、`e2e` 分组。配置示例、
机器可读 uquant 身份、构建/扫描脚本和 GitHub workflows 分别位于 `config`、`src/firmquant/resources`、`scripts` 和
`.github/workflows`。

模块依赖应指向领域值对象和显式 Protocol，避免适配器反向污染策略/执行核心。跨权威转换集中在 anti-corruption
adapter；不要在 broker、risk 或 persistence 中复制策略逻辑。

## 测试驱动

新增功能或修复先写能证明行为的失败测试，确认失败原因后做最小实现。故障场景不能用 xfail、删除断言、近似经济比较
或放宽安全门处理。外部 payload、状态推进、幂等、事务和恢复都应有负向测试。

小改只运行直接受影响节点；模块 milestone 运行相关 L2/L3；完整 L4 只在最终稳定候选运行。失败先跑单项并定位根因，
不因每个小修反复启动全量矩阵。完整阶梯见 [QUALITY.md](QUALITY.md)。

## 代码规则

- Python 标识符使用清晰英文，运维/用户文档使用中文；时间为带 offset ISO-8601，session 使用 Asia/Shanghai。
- broker/ledger 边界不用无约束 float；金额、价格、费用使用 Decimal，股数使用整数值对象。
- SQLite 写入必须位于调用者拥有的显式事务中；禁止嵌套事务和 callback 并发写账户。
- 配置模型必须 strict/frozen/extra-forbid 并提供 `safe_repr`；新日志字段必须经过统一 redaction。
- 所有真实 broker write 必须依赖不可伪造的 capability，不能在 adapter 增加旁路。
- 不提交 wheel、SDK、secret、本机路径、真实快照、日志、数据库或事故 payload。

## Broker 开发

Fake、Paper、Replay 和 XtQuant 必须通过共享 BrokerGateway contract。开发 XtQuant 先使用 contract fake；SDK 存在时也
只执行 import/schema 和只读 smoke。禁止从网络旧样例猜 API，禁止用真实订单验证 mapping。

未来 adapter 需要新增证券元数据 provider 时，应保持事实来源明确、缺失即阻断。不要同时扩展多券商、多账户或交易品种。

## uquant 开发边界

firmquant 不修改上游仓库。若公共接口不足，先在 firmquant 建窄 adapter；确实无法安全适配才更新
[UPSTREAM_GAPS.md](UPSTREAM_GAPS.md)。策略 parity 数据必须固定 commit、config、data、universe、account、broker
snapshot 和 as-of。

## 文档与提交

更新 CLI、配置、模式或安全行为时同步修改 canonical 文档和 `test_documented_defaults.py`。运行：

```bash
uv run pytest tests/unit/test_documented_defaults.py -q
uv run python scripts/check_docs.py
```

checkpoint commit 应聚焦一个可理解 milestone，提交前检查 diff、测试范围和 secret scan；推送工作分支，不 force push。
