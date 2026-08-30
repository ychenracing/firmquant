# uquant 权威源码基线

本文档是 firmquant 使用的 uquant 生产依赖身份记录。运行时机器可读的同一基线位于
`src/firmquant/resources/source_identity.json`。firmquant 不修改 uquant，也不从本地脏工作树、临时分支或浮动
引用构建策略内核。

## 获取结论

2026-08-30 获取并检查 `https://github.com/ychenracing/uquant.git`：

- 已知基线：`105695aacd3d1c7e62705f64188da88d202db4cd`
- 锁定目标：`a17322f6330953a27c77f70d463a713c9a48ebc9`
- 二者关系：目标是已知基线的后代（`descendant`）
- commit tree：`846566bb6317ddbdcff729aa9fff7950fa5baa58`
- commit subject：`docs: optimize AGENTS governance`
- 检查来源：干净、detached、精确 commit 的 Git checkout

## 依赖与源码摘要

| 身份项 | SHA-256 / 值 |
|---|---|
| uquant 版本 | `1.1.0` |
| uquant `pyproject.toml` | `36be3c7ac6c4ec5011552acf5456b3e44054ab20e036fff9007788c640c87920` |
| uquant `uv.lock` | `4accf16535b5ac95b831c9289e0ad2ff21282dc5dfae3f05dd0fb095089d6a61` |
| `economic_decision_v1` 源码面 | `d1ef7977ae482e46a920381e6af58791199ec8e1a02586dbe8df451e7d4696c9` |
| `execution_account_v1` 源码面 | `2c686d470ecb156801d0dfbde555fcec6de20c81804125847fcccb5f1f304daf` |
| 默认生产配置 | `dae4d79fdd813832c6ab152611437c13be1d38227c7280691874d3a9267d93d5` |
| public API contract 原始文件 | `fa0b34250fd4d34547841388a4869f7fce459a6b95b98556c2678dcae38fd89a` |
| public API contract canonical seal | `b485932a5eb10b0528c2d01008c6495f8f2e1e74ead04c737cafd9c665efa6b5` |
| firmquant `pyproject.toml` | `60d1d40b59a155a1afe32039ca304c098452e847309a5056bab5ae76c689bcf7` |
| firmquant `uv.lock` | `b8ce42ec34ab79029fcebc609dbcd0d0dfaa325e8434280c566d9c41fd04d37a` |

firmquant 的 `pyproject.toml` 和 `uv.lock` 均把 uquant 锁到上述 40 位 commit；没有使用浮动 `main`、tag
或版本范围。`uv.lock` 的解析结果也以同一 commit 作为 source fragment。

## Canonical AI universe

- manifest id：`phase1-ai-universe-v1`
- schema version：`1`
- 点时成员记录：34 个唯一证券
- 原始 manifest：7,320 bytes
- 原始文件 SHA-256：`849d8ec600d207fed7fa700f3f9e3c40ba1251b30a175a8935655b7645213219`
- uquant canonical seal：`03f42c5066fb8e1c7b2f8e1b7dd38d508d8053f548ebb5596317ce587d7cffd0`

该 manifest 由锁定的 uquant 合同加载和校验。firmquant 的部署 allowlist 只能进一步取其子集，不能新增成员。

## 确定性 wheel

使用 uquant commit 内置的 `scripts/build_reproducible_wheel.py`，从 `git archive` 导出的干净源码构建。
固定 `SOURCE_DATE_EPOCH=315532800`，并由 firmquant 包装脚本连续构建两次：

| 产物项 | 值 |
|---|---|
| 文件名 | `uquant-1.1.0-py3-none-any.whl` |
| 文件大小 | 2,345,276 bytes |
| wheel SHA-256（两次相同） | `13ef26d5d34a86d8ee45641ef63bb1c8a01d381156cff323fcdc582b599189d8` |
| 全部 226 个成员的 manifest | `455fc57f2027dd82a4e15cddc464d7be54df52ea1cbf6e8890cc21844e0a82ca` |
| 220 个 `uquant/` 成员的 manifest | `a1b754b9875a6572c4ecbd5fa996336b93ecfb29f7c23157e5051af645e5cfa5` |
| 非预期成员 | 无 |

复现命令（只构建，不连接券商）：

```bash
uv run python scripts/build_reproducible_wheels.py --source-root .uquant-target --verify-twice --output-dir dist/uquant
uv run python scripts/verify_source_baseline.py --source-root .uquant-target --wheel dist/uquant/uquant-1.1.0-py3-none-any.whl
```

部署流水线必须保留 wheel SHA-256；仓库不提交构建后的 wheel。运行时校验完整的已安装 `uquant/` 文件
manifest、版本、配置 fingerprint、universe seal，并在 VCS direct URL 存在时额外校验 commit 和仓库 URL。

## 已知上游打包边界

目标 wheel 不包含 public `code_fingerprint()` 必需的 `benchmarks/source_surface_registry.json`，因此直接从 site-packages 调用
`uquant.engine.code_fingerprint()` 会失败关闭。firmquant 没有复制 ProductionEngine 或指纹算法，也没有修改
uquant；证据、影响与安全边界记录在 [UPSTREAM_GAPS.md](UPSTREAM_GAPS.md)。当前 `StrategyAdapter` 仅通过目标公开
`ProductionEngine.decide()` 工作，在精确验证的干净 detached 源码 checkout 中通过严格 parity；wheel 单独运行仍按上述缺口
失败关闭，不能被当作无需源码身份验证的降级路径，也不能用于声称 source/wheel 决策 trace 等价。
