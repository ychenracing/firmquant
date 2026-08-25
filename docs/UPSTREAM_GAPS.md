# uquant 上游接口缺口

本文档只记录 firmquant 在锁定 uquant commit 上实际复现、且无法仅通过现有已打包公共接口消除的边界。

## UG-001：wheel 不能独立计算生产源码 fingerprint

状态：已复现；依赖身份校验已失败关闭；策略执行适配尚未启用。

### 复现事实

在 `uquant==1.1.0`、commit `105695aacd3d1c7e62705f64188da88d202db4cd` 的确定性 wheel 安装后，调用
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
- 运行时校验全部 209 个已安装 `uquant/` 文件的路径、大小和内容摘要；
- 校验 uquant 版本、VCS direct URL（存在时）、默认配置 fingerprint 与 canonical universe seal；
- 不调用、替换或复制第二套 ProductionEngine、PortfolioAllocator、Risk 或策略状态机。

由于 `ProductionEngine.decide()` 在没有预置 code hash 时会调用该不可用的 wheel 路径，firmquant 当前不会把
此 wheel 宣称为可直接执行生产决策。后续策略 anti-corruption adapter 只能使用从上述已验证 Git 源码面得到的
同一 fingerprint 注入单个 ProductionEngine 实例，并必须用 source-checkout 直接调用作严格 parity 证明；不得
全局 monkeypatch，不得自行计算经济输出。

### 希望上游提供的稳定接口

上游可以选择把完整 fingerprint registry 和所有成员作为只读 package resource 发布，或提供一个由确定性构建
流程写入、运行时可验证的 source identity 合同。该接口应保留当前源码面成员关系和摘要算法，不改变任何经济行为。
