# firmquant — ChatGPT project brief

This document is the stable, minimal context entry point for AI-assisted work. It summarizes durable boundaries and points to authoritative documents instead of duplicating them. Task state belongs in the linked GitHub Issue; review and merge evidence belongs in the Pull Request.

## Project goal and system position

`firmquant` is a lightweight, safety-first daily execution system for one user, one China A-share cash account, and one Windows trading host. It turns the locked `uquant` strategy output into controlled PAPER, SHADOW, CANARY, or LIVE execution while preserving recovery, reconciliation, auditability, and fail-closed behavior.

It is an execution, broker-fact integration, operational-risk, reconciliation, and audit system. It is not a second strategy, a research platform, a high-frequency system, a multi-account/multi-strategy platform, or a return guarantee.

## Architecture and module boundaries

The architecture is a modular monolith with ports and adapters: one process, one SQLite operational ledger, and one account writer lease. Broker callbacks are treated as untrusted input and enter a bounded queue; a single writer advances order, account, and audit transactions.

| Area | Boundary |
|---|---|
| `application` | Local use cases, session coordination, lifecycle, and operations command orchestration |
| `domain` | Value objects, runtime/order state machines, domain events, and invariants |
| `strategy` | Locked identity, uquant anti-corruption adapter, account prepare/commit, immutable decision snapshots |
| `broker` | Gateway ports, Fake/Paper/Replay/XtQuant adapters, normalization of untrusted broker input |
| `market_data` | Authoritative calendar, daily manifests, append-only validation, execution quote ports |
| `execution` | Frozen decision-to-order planning, SELL/BUY sequencing, submission and cancellation policy |
| `risk` | Shrink-only per-order gates, arm lease, kill switch, and broker-write capability |
| `reconciliation` | Account binding, preflight, and broker/uquant/firmquant reconciliation |
| `persistence` | SQLite migrations, transactional repositories, single writer, recovery, backup, hash-chain audit |
| `scheduling` | Asia/Shanghai sessions, clock checks, recoverable workflow receipts |
| `observability` / `security` | Structured logs, reports, alerts, secret providers, redaction, scanning |

See [`docs/ARCHITECTURE.md`](../docs/ARCHITECTURE.md) for the authoritative architecture narrative.

## State and data owners

| Owner | Authoritative for | Not authoritative for |
|---|---|---|
| Broker | Available cash, total assets, real/sellable positions, broker IDs, orders, fills, fees, live security status | Strategy targets or strategy lifecycle |
| uquant | Opportunities, risk, Sentinel, target portfolio, strategy position lifecycle, economic order IDs, strategy configuration, data identity, canonical universe | Online connectivity, broker IDs, retries, alerts |
| firmquant | Broker mappings, submission attempts, callbacks, UNKNOWN orders, account binding, reconciliation, arm lease, kill switch, runtime health, audit | A second economic account, target portfolio, strategy parameters |

Machine-readable source identity is owned by `src/firmquant/resources/source_identity.json`; the human-readable source baseline is owned by `docs/SOURCE_BASELINE.md`. Do not copy changing source hashes into this brief.

## Non-negotiable business and safety constraints

- The locked uquant implementation is the only strategy decision kernel. Strategy decisions use the single `ProductionEngine.decide()` path.
- uquant exclusively owns PortfolioAllocator, Base Risk, FREEZE_ONLY Risk Sentinel, strategy parameters, target portfolio, AccountState lifecycle, and the canonical AI universe. A deployment allowlist may only be a subset of that universe.
- firmquant may block, shrink, delay, cancel, or HALT; it must never expand uquant targets, gross exposure, per-symbol weights, buy quantities, universe membership, or risk authorization.
- The supported economic scope is one A-share AI-chain cash-long account: no leverage, no shorting, no derivatives, no multi-account or multi-strategy expansion.
- Daily economics are post-close decision and next-trading-day execution. Intraday logic manages lifecycle, fills, disconnects, freshness, risk blocks, and reconciliation; it does not reselect securities or re-optimize the portfolio.
- PAPER is the default and example configurations keep `live_trading_enabled = false`. REPLAY, PAPER, SHADOW, and CI must be structurally unable to reach real broker submit/cancel writes.
- Every CANARY/LIVE broker write must re-pass all applicable short-lived authorization, compliance, identity, reconciliation, freshness, session, kill-switch, UNKNOWN-order, cash/position, and per-order execution gates. Missing or uncertain evidence fails closed.
- CANARY never promotes itself to LIVE. Readiness reporting is read-only and never arms or submits orders.
- Emergency handling blocks new orders, cancels only clearly firmquant-owned open orders, and HALTs. It does not auto-liquidate or send unprotected market orders under uncertainty.
- uquant AccountState is the only strategy-economic state. Broker facts may enter it only through binding, preflight, in-memory prepare, final reconciliation, and expected-before CAS commit. Unexplained manual trades, external orders, cash changes, or identity drift block commit.
- Money, prices, and fees use Decimal or integer minor units; share quantities use integer value objects. All external payloads are untrusted.
- Fake, contract, CI, skipped, or unrun checks are not evidence of a real MiniQMT account. Real adapter claims require a legal deployment host and a real read-only smoke; tests must never send real orders.

## Standard commands

Python is `>=3.12,<3.13`; `.python-version` is `3.12`. Dependencies are owned by `uv.lock` and installed with frozen resolution.

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

Minimal documentation-contract checks:

```bash
uv run pytest tests/unit/test_documented_defaults.py -q
uv run python scripts/check_docs.py
uv run python scripts/secret_scan.py
```

PAPER-only local startup:

```bash
cp config/firmquant.example.toml config/firmquant.local.toml
uv sync --frozen --extra dev
uv run firmquant init
uv run firmquant doctor
uv run firmquant run --mode paper
uv run firmquant status
```

## Important paths and authority index

| Path | Authority |
|---|---|
| `README.md` | Project entry point, safety posture, runtime model, CLI overview |
| `AGENTS.md` | Repository-wide agent instructions, engineering method, verification ladder, Git safety |
| `pyproject.toml` / `uv.lock` | Python, dependency, build, test, lint, type, coverage configuration |
| `config/firmquant.example.toml` | Safe example configuration; live writes disabled by default |
| `src/firmquant/` | Production implementation |
| `tests/` | Unit, contract, integration, property, fault, end-to-end, and parity evidence |
| `scripts/` | Source identity, docs, secret, reproducible-build, and deployment checks |
| `.github/workflows/` | CI, security, Windows deployment-safety entry points |
| `docs/STRATEGY_INTEGRATION.md` | Unique uquant decision path, AccountState, universe, parity |
| `docs/RISK_AND_SAFETY.md` | Shrink-only risk, modes, leases, write capability |
| `docs/OPERATIONS.md` / `docs/RECOVERY.md` | Operations, supervision, incidents, recovery |
| `docs/QUALITY.md` / `docs/DEVELOPMENT.md` | Verification ladder, final gates, development rules |
| `.github/ISSUE_TEMPLATE/ai-task.md` | Latest verified task state, acceptance logic, risks, decisions, next action |
| `.github/pull_request_template.md` | Scope, change, verification, CI, risk, and merge evidence |

## GitHub Actions and acceptance entry points

- `CI` (`.github/workflows/ci.yml`): Linux and Windows source identity, Ruff, formatting, mypy, secret scan, branch coverage threshold, compile, docs, parity, and deterministic wheel checks.
- `Security` (`.github/workflows/security.yml`): Bandit, pip-audit, secret scan, and gitleaks.
- `Windows deployment safety` (`.github/workflows/windows.yml`): locked identity, targeted persistence/doctor/adapter tests, PAPER-only smoke, and CLI checks.
- A green workflow is repository evidence only; it does not prove real MiniQMT connectivity or authorize CANARY/LIVE.
- Acceptance criteria and exact AND/OR logic belong in the linked Issue. Commands, exact results, environment, unrun checks, CI links, and merge readiness belong in the PR.

## Prohibited actions

- Do not copy, approximate, or create alternatives to uquant strategy logic, PortfolioAllocator, Base Risk, FREEZE_ONLY Sentinel, target construction, parameters, or AccountState lifecycle.
- Do not widen the universe, targets, exposure, weights, quantities, or authorization; bypass gates or state machines; blindly resend UNKNOWN orders; auto-absorb manual activity; guess attribution/lifecycle; or fabricate evidence.
- Do not change production configuration semantics, strategy parameters, dependencies, or workflows merely to make checks pass.
- Do not commit account numbers, passwords, tokens, webhook secrets, sensitive MiniQMT userdata, real account snapshots, unredacted fills, SDKs, wheels, databases, logs, or incident payloads.
- Do not use fake/CI/contract/skip results as real-account validation, and never send real broker writes from tests, CI, or smoke checks.
- Do not build production uquant inputs from a dirty tree, temporary branch, floating main, or floating tag.
- Do not use reset, clean, rebase, force push, destructive checkout/restore, overwrite uncommitted work, or rewrite history without explicit authorization.
- Never report planned, skipped, inferred, or stale validation as passed.

## Authoritative references

- [`README.md`](../README.md)
- [`AGENTS.md`](../AGENTS.md)
- [`docs/ARCHITECTURE.md`](../docs/ARCHITECTURE.md)
- [`docs/STRATEGY_INTEGRATION.md`](../docs/STRATEGY_INTEGRATION.md)
- [`docs/RISK_AND_SAFETY.md`](../docs/RISK_AND_SAFETY.md)
- [`docs/OPERATIONS.md`](../docs/OPERATIONS.md)
- [`docs/RECOVERY.md`](../docs/RECOVERY.md)
- [`docs/QUALITY.md`](../docs/QUALITY.md)
- [`docs/DEVELOPMENT.md`](../docs/DEVELOPMENT.md)
- [`docs/SOURCE_BASELINE.md`](../docs/SOURCE_BASELINE.md)
- [`docs/UPSTREAM_GAPS.md`](../docs/UPSTREAM_GAPS.md)
