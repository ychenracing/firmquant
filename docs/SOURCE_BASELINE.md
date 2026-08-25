# uquant 权威源码基线

本文档是 firmquant 使用的 uquant 生产依赖身份记录。运行时机器可读的同一基线位于
`src/firmquant/resources/source_identity.json`。firmquant 不修改 uquant，也不从本地脏工作树、临时分支或浮动
引用构建策略内核。

## 获取结论

2026-08-25T17:07:02Z 获取并检查 `https://github.com/ychenracing/uquant.git`：

- 已知基线：`105695aacd3d1c7e62705f64188da88d202db4cd`
- 获取时 `origin/main`：`105695aacd3d1c7e62705f64188da88d202db4cd`
- 二者关系：完全相同（`identical`），不存在需要吸收的更新
- commit tree：`e3e2832eb1321e6d45f103cab538aeb9c95852d3`
- commit subject：`docs: repair source identity and reproducible build governance (#24)`
- 检查来源：干净、detached、精确 commit 的 Git checkout

## 依赖与源码摘要

| 身份项 | SHA-256 / 值 |
|---|---|
| uquant 版本 | `1.1.0` |
| uquant `pyproject.toml` | `36be3c7ac6c4ec5011552acf5456b3e44054ab20e036fff9007788c640c87920` |
| uquant `uv.lock` | `4accf16535b5ac95b831c9289e0ad2ff21282dc5dfae3f05dd0fb095089d6a61` |
| `economic_decision_v1` 源码面 | `2209a539bacbc01d90b29b9f0bb78ace4991016bee0d41f9e86f38ccf5af545e` |
| `execution_account_v1` 源码面 | `df2675ebe560f5dc9089a51825aed23f499a35d10e2827c3a96f0a1d40189e0c` |
| 默认生产配置 | `dae4d79fdd813832c6ab152611437c13be1d38227c7280691874d3a9267d93d5` |
| firmquant `pyproject.toml` | `5f70d9f842e3c61bf935ca9847fb74c65c26cfd95dae0e43de292bcdf6004d3e` |
| firmquant `uv.lock` | `78cbd90dcfdf2bc031963e5f3273320689d43865c0cf5c9bc952eedf9df7250f` |

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
| 文件大小 | 2,081,479 bytes |
| wheel SHA-256（两次相同） | `a5df13991b6696f22e8a1633b0dfb717d1a3647448462141318702844653137c` |
| 全部 215 个成员的 manifest | `332a0aa1d66df53984baa569aa8b436328f3f134a485ed2c684f27da6c63b3ac` |
| 209 个 `uquant/` 成员的 manifest | `35b3f686563647f462e6a5c0966c74e4cb4b2b5d15a9ea732cc177d65d0cc68f` |
| 非预期成员 | 无 |

复现命令（只构建，不连接券商）：

```bash
uv run python scripts/build_reproducible_wheels.py --verify-twice --output-dir dist/uquant
uv run python scripts/verify_source_baseline.py --wheel dist/uquant/uquant-1.1.0-py3-none-any.whl
```

部署流水线必须保留 wheel SHA-256；仓库不提交构建后的 wheel。运行时校验完整的已安装 `uquant/` 文件
manifest、版本、配置 fingerprint、universe seal，并在 VCS direct URL 存在时额外校验 commit 和仓库 URL。

## 已知上游打包边界

锁定 wheel 不包含源码 fingerprint registry 引用的若干仓库级文件，因此直接从 site-packages 调用
`uquant.engine.code_fingerprint()` 会失败关闭。firmquant 没有复制 ProductionEngine 或指纹算法，也没有修改
uquant；证据、影响与安全边界记录在 [UPSTREAM_GAPS.md](UPSTREAM_GAPS.md)。在策略适配器完成并通过 parity
验证前，生产决策执行不可用。
