# uquant 上游接口缺口

本文档只记录 firmquant 在锁定 uquant commit 上实际复现、且无法仅通过现有已打包公共接口消除的边界。

## UG-001：wheel 不能独立计算生产源码 fingerprint

状态：已复现；依赖身份校验已失败关闭；策略适配仅在精确验证的干净源码 checkout 中启用。

### 复现事实

在 `uquant==1.1.0`、commit `a17322f6330953a27c77f70d463a713c9a48ebc9` 的确定性 wheel 安装后，调用
`uquant.engine.code_fingerprint()` 返回：

```text
ValueError: source surface registry is missing or unsafe: benchmarks/source_surface_registry.json
```

`economic_decision_v1` 的权威源码面包含 `benchmarks/*.json`、仓库根 `pyproject.toml`、`requirements.txt`、
`uv.lock` 以及 `uquant/` 源码。uquant wheel 的生产打包合同只包含 `uquant/` 与 dist-info；其可复现构建器也
明确确认仓库级文件不是 wheel 成员。因此，这不是 SDK 或本机环境缺失，而是 source-checkout fingerprint
合同与 wheel 运行形态之间的接口缺口。

### 当前安全边界

firmquant 当前执行以下校验，任一不一致即失败：

- 从干净 Git commit 计算完整 `economic_decision_v1` 和 `execution_account_v1` fingerprint；
- 两次构建 byte-identical wheel，并锁定 wheel SHA-256；
- 运行时校验全部 220 个已安装 `uquant/` 文件的路径、大小和内容摘要；
- 校验 uquant 版本、VCS direct URL（存在时）、默认配置 fingerprint 与 canonical universe seal；
- 校验目标 public API contract 原始文件和 canonical seal；
- 不调用、替换或复制第二套 ProductionEngine、PortfolioAllocator、Risk 或策略状态机。

由于 `ProductionEngine.decide()` 在没有预置 code hash 时会调用该不可用的 wheel 路径，firmquant 不把此 wheel
宣称为可独立执行生产决策。`StrategyAdapter` 只在以下条件同时满足时调用决策：源码 checkout 的 commit、tree、
工作树、锁文件、public API contract、universe resource 和两个生产源码面全部精确校验；传入的 `ProductionEngine`
确实从该 checkout 加载。adapter 不读取或写入 `_code_hash`，只调用公开 `ProductionEngine.decide()` 并验证公开账户
后状态中的 fingerprint。它不修改模块全局、不替换 `ProductionEngine.decide()`，也不自行计算任何策略经济输出。
直接 source-checkout 调用与 adapter 调用的完整 payload、账户经济状态、targets、orders、reason codes 和 fingerprint
已纳入严格 parity 测试；安装 wheel 的相同公开 trace 保留为 strict xfail，直到上游打包合同闭合。

### 希望上游提供的稳定接口

上游可以选择把完整 fingerprint registry 和所有成员作为只读 package resource 发布，或提供一个由确定性构建
流程写入、运行时可验证的 source identity 合同。该接口应保留当前源码面成员关系和摘要算法，不改变任何经济行为。

## UG-002：wheel 缺少生产决策所需的 reference registry

状态：已确认目标 wheel 仍不包含该 registry；公开决策先被 UG-001 失败关闭；使用同一精确验证源码 checkout 运行唯一
决策内核。

### 复现事实

已知基线 wheel 中的 `uquant.reference_registry.DEFAULT_REGISTRY_PATH` 指向 site-packages 同级的
`benchmarks/reference_registry.json`，但确定性 wheel 不包含 `benchmarks/`。即使先为单个 engine 实例预置正确的
code fingerprint，调用 `ProductionEngine.decide()` 仍精确失败为：

```text
FileNotFoundError: .../site-packages/benchmarks/reference_registry.json
```

目标 wheel 的成员 manifest 仍不包含该文件；公开路径在此之前已因 UG-001 停止，因此 firmquant 不通过私有状态绕过
UG-001 来制造目标运行结果。该 registry 决定点时 reference membership，firmquant 不能猜测、重建或维护第二份。
因此 firmquant 不复制该文件，不 monkeypatch `resolve_reference_symbols()`，也不把缺文件降级为固定股票池。

### 当前安全边界

生产决策运行形态要求一个锁定 commit 的干净 uquant checkout；`StrategyAdapter` 在每次决策前验证该 checkout，且
验证 engine 模块文件正是 checkout 中的 `uquant/engine.py`。uv 仍把同一 Git commit 锁为依赖，已安装 wheel 继续
提供确定性包字节和运行身份的第二重校验。CI 另外检出精确 commit 并运行 source parity；任何缺失、脏修改或身份
漂移都会在调用 `decide()` 前失败。

### 希望上游提供的稳定接口

将 `reference_registry.json` 作为只读 package resource 发布，并使 `resolve_reference_symbols()` 从该 resource 加载；
或者为 `ProductionEngine` 提供显式、严格校验且不会改变经济行为的 registry 注入接口。上游修复并重新锁定前，
firmquant 不声称 wheel 可独立运行生产决策。
