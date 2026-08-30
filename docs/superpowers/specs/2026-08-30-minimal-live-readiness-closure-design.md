# Personal LIVE Readiness Closure Design

## Status and authority

This design records the approved implementation contract for PR #11. The
user-supplied task prompt remains the ultimate task authority; `AGENTS.md`
and `.github/CHATGPT_PROJECT_BRIEF.md` remain the repository authority. If
this document conflicts with either, the stricter fail-closed requirement
wins.

## Goal

Close the remaining correctness, recoverability, and evidence gaps required
for one user, one A-share cash account, one Windows trading host, one daemon,
one SQLite ledger, and one writer to progress manually through:

`PAPER -> SHADOW -> CANARY -> LIVE`

`uquant` remains the sole strategy and economic-state owner. `firmquant`
continues to own broker facts, execution, reconciliation, local operational
state, safety gates, recovery, audit, and authority reduction only.

## Frozen upstream and validation boundary

- `firmquant` baseline: `eb9c5b76a0e5fd0d413ed41cc9bb627431427e05`.
- `UQUANT_TARGET_SHA`: `a17322f6330953a27c77f70d463a713c9a48ebc9`.
- Production uquant inputs must come from a clean detached checkout at that
  exact 40-character SHA.
- Two independent wheel builds must be byte-identical and must agree with
  the source checkout, installed package manifest, public contract, adapter
  result, code/config fingerprints, and canonical universe.
- An old AccountState that cannot be loaded strictly by the target public
  codec is not guessed or automatically migrated. It requires the reviewed
  `rebaseline-account` path.
- Real Windows, MiniQMT, broker-account, SHADOW-duration, CANARY, restore
  drill, Scheduler-installation, and notifier-delivery evidence cannot be
  manufactured in CI. Missing receipts keep CANARY/LIVE closed but do not
  prevent safe repository code from merging.

## Architecture

The existing modular monolith and ports/adapters boundaries remain. Five
small responsibilities are added or made canonical:

1. `production_identity`: one stable `DeploymentIdentity` plus one mutable
   `OperationalEvidenceIdentity`.
2. `operational_authority`: append-only account and mode epochs with explicit
   active pointers and immutable receipts.
3. `backup`: schema-v3 bundles, durable publication state, strict verification,
   and restore into an empty destination.
4. `replay_acceptance`: causal execution replay, two-mode equality, fixed
   policy evaluation, and immutable receipt storage.
5. `control_channel`: authenticated ARM requests that only the online daemon
   may execute while holding the existing WriterLease.

No second daemon, writer, database, strategy, account state, queue, HTTP
service, or remote control surface is introduced.

## Canonical identities

### DeploymentIdentity

The stable deployment identity is canonical JSON with schema version and at
least:

- firmquant commit;
- uquant commit, tree, package manifest, code fingerprint, and configuration
  fingerprint;
- semantic configuration SHA-256;
- raw configuration-file SHA-256;
- XtQuant safety-manifest SHA-256;
- account-id SHA-256;
- account-authority epoch;
- mode epoch, mode, and deployment caps;
- production-safety-policy SHA-256.

It is stable across ordinary session AccountState changes. Long-running
SHADOW/CANARY aggregation keys use this stable identity rather than a daily
AccountState hash.

### OperationalEvidenceIdentity

The mutable evidence identity contains the deployment identity SHA-256 plus:

- current AccountState SHA-256;
- broker snapshot id, payload SHA-256, event watermark, started/completed
  times, and duration;
- calendar SHA-256;
- active data-generation and strategy-data-manifest SHA-256 values;
- strategy session and optional decision id;
- evidence phase and kind.

Both identities reject duplicate JSON keys, non-standard constants, NaN,
binary floats in economic fields, unknown fields, and non-canonical text.
Receipts store the complete canonical payload or enough exact fields to
reconstruct and verify it.

## Non-relaxable production policy

The code-owned `ProductionSafetyPolicy` validates Settings and contributes to
the semantic identity. Configuration may be stricter, never looser, than:

- `max_quote_age_seconds <= 5`
- `max_clock_drift_seconds <= 2`
- `max_disconnect_seconds <= 30`
- `max_price_deviation_bps <= 200`
- `max_equity_change_fraction <= 0.10`
- `max_intraday_loss_fraction <= 0.08`
- `max_capital_drawdown_fraction <= 0.25`
- `min_shadow_sessions >= 20`
- `min_shadow_orders >= 50`
- `max_target_tracking_error <= 0.05`
- `min_canary_sessions >= 3`
- `min_canary_orders >= 3`
- `min_canary_fills >= 1`
- `max_canary_target_tracking_error <= 0.05`
- ARM TTL `<= 900` seconds.

CANARY/LIVE notional caps remain explicit operator configuration. Their
positivity, ordering, mode binding, and identity binding are validated; no
account-size value is hard-coded.

## Persistent state and migrations

One additive, checksummed, contiguous migration group introduces:

- append-only `deployment_identities` and operational evidence receipts;
- append-only account-authority epochs plus a singleton active pointer;
- append-only mode epochs plus a singleton active pointer;
- rebaseline and mode-transition operations with crash stages;
- snapshot started/completed/duration metadata nullable for historical rows;
- replay-acceptance receipts;
- backup publication/restore receipts where required.

Existing account binding becomes authority epoch 1. Existing persisted mode
becomes mode epoch 1. Historical rows remain readable and auditable but do
not become current evidence merely because new identity fields are absent.
Unknown/future schema versions still fail closed. No existing operational or
incident rows are deleted.

## Account rebaseline

`firmquant rebaseline-account` is the only subsequent authority-epoch entry.
It keeps the same `account_id_hash`, increments the epoch, retains all prior
evidence, and accepts no unexplained discrepancy.

Before PREPARED it requires DISARMED runtime, no active arm, no active or
unresolved SYSTEM/EXTERNAL/MANUAL/UNKNOWN order or account operation, a fresh
stable complete broker snapshot, a canonical reason, a reviewed evidence
digest, strict current/candidate AccountState validation, complete
reconciliation, and a verified schema-v3 `ACCOUNT_REBASELINE` backup.

Non-empty accounts require a human-reviewed AccountState file. Broker facts
must never be used to invent lifecycle, tranche, attribution, strategic owner,
or strategy origin. PREPARED -> FILE_COMMITTED -> RECEIPT_COMMITTED is
recoverable and idempotent; same identity with different payload conflicts.

## Mode transition

`firmquant transition-mode --to SHADOW|CANARY|LIVE` compares `--to` with the already
prepared local target config. The database mode is the source mode. Promotion
edges are only PAPER -> SHADOW -> CANARY -> LIVE. Explicit risk reduction may
move to lower modes but still requires DISARMED, no active orders, and full
reconciliation; it never resolves UNKNOWN or account mismatch.

Every transition verifies the corresponding evidence gates, creates and
verifies a `MODE_TRANSITION` backup, commits a new mode epoch and receipt
atomically, revokes old arm/readiness authority, and ends DISARMED. It never
edits config, auto-promotes, auto-arms, or submits.

## Backup v3 and restore

Schema-v3 reasons are exactly `SESSION_CLOSE`, `MODE_TRANSITION`, and
`ACCOUNT_REBASELINE`. A complete bundle contains the online SQLite backup,
strict AccountState, production config, XtQuant safety manifest, calendar,
data manifests, both canonical identities, both epochs, snapshot identity and
watermark, strategy session/decision id, audit count/head, schema, reason,
and an independent SHA-256 for every member. Secrets, userdata, raw sensitive
payloads, leases, heartbeat authority, and pending controls are not portable
authority.

`restore-backup` accepts only a nonexistent or strictly empty destination,
never overwrites production or deletes an incident site, verifies every
member and cross-identity before publish, fsyncs files/directories, and
publishes atomically. The restored SQLite state is audited, DISARMED, has all
arms revoked, has no writer/heartbeat authority, sends no order, and requires
a new broker connection, snapshot, and reconciliation on first start.
Legacy v1/v2 bundles remain verifiable through non-mutating compatibility
validation; they are not silently treated as current v3 authority.

## Snapshot and readiness

Snapshot collection records UTC start/completion, monotonic duration, per-call
bounded timeout/deadline, total deadline, and event watermark. Account identity
or lifecycle-signature changes across the double read fail closed. Readiness
uses completion age and total duration, not just a final timestamp.

LIVE readiness is read-only and returns every blocker. It verifies the active
authority/mode/deployment/evidence identity, current AccountState, current
snapshot, current data/calendar/decision, trusted ClockGuard/quote receipt,
fresh boot/client-bound smoke, matching backup, matching Replay acceptance,
unresolved facts in the current authority epoch, kill switch, and control/
heartbeat health.

Reconciliation is phase-aware:

- startup/pre-market: current STARTUP;
- intraday: current STARTUP and current INTRADAY;
- post-close: current EOD/close checkpoint.

Different broker fill IDs with identical economics are legal. The same fill
ID with the same payload is idempotent; the same ID with different content is
a conflict. Duplicate economic orders use the existing stable execution/
uquant order identity, not a lossy field grouping. `armed` is output state,
not a software-readiness prerequisite.

## Causal execution replay

The execution decision for session N+1 can see only open-time facts: account
cash/positions/sellable, instrument/listing facts, open price, previous close,
previous-session volume, frozen protection limit, and pre-known price-limit
facts. It never uses N+1 high, low, close, or full-day volume to authorize or
fill. If a protected order cannot fill at the open model, that finite window
is a no-fill.

Capacity uses previous-session volume. SELL proceeds are available to BUY only
after an actual simulated SELL fill. BUY is always a 100-share lot. SELL is a
100-share lot except a one-time full liquidation of the entire remaining odd
lot. T+1, suspension, limits, capacity, cash, fees, partial fills, no short,
no negative cash, no duplicate order/fill, no same-day sale of newly bought
shares, and no intent expansion are explicit invariants.

QFQ limits are never widened from current-day OHLC. Missing reliable
pre-execution limit facts yields `LIMIT_FACT_UNAVAILABLE` and no fill. Listing
session comes from an authoritative manifest; missing metadata fails closed or
marks the symbol-session unverifiable.

Directional adverse slippage cost is:

- BUY: `max(fill - benchmark, 0) * shares`
- SELL: `max(benchmark - fill, 0) * shares`

Favourable execution is reported separately as signed improvement. If
`slippage_bps > 0` and at least one fill occurs, adverse slippage cost must be
positive.

## Replay acceptance and receipt

Acceptance is fixed to uquant `continuous_ai_era`, `2023-01-03` through
`2026-08-05`, and runs both normal and `restart_each_session`. Their economic
metrics, order/fill identities, session digests, final cash/positions,
AccountState hash, and final identity must match exactly.

All gates must pass:

- execution-aware TWR / theoretical TWR `>= 0.75`;
- execution MDD minus theoretical MDD `<= 0.05`;
- maximum target tracking error `<= 0.05`;
- `unfilled_loss / turnover_notional <= 0.10`;
- `slippage_bps > 0`;
- fills imply adverse `slippage_cost > 0`;
- no negative cash, short, same-day sale of new buys, duplicate economic
  order, duplicate fill, UNKNOWN, future-data access, or enlarged intent;
- exact two-mode equality.

The append-only receipt binds firmquant/uquant identities, semantic
config/policy, universe, frozen data, date range, fee/slippage/capacity policy,
thresholds, both summaries, every actual gate value/result, generation time,
schema, and payload SHA-256. Same identity/same result is idempotent; same
identity/different result conflicts. Any relevant code/config/uquant/data/
universe/policy change invalidates it. CANARY and LIVE require a current PASS
receipt.

## Daemon-owned ARM

The existing local atomic control inbox gains ARM. CLI use is interactive,
local, and disabled in CI. The request binds canonical id, created/expiry,
host, requested mode and TTL, current deployment identity, nonce, and MAC from
the existing `ARM_MAC_KEY`; it stores no key or confirmation phrase. An online
daemon response is `QUEUED`, not “armed”. An offline daemon cannot create an
arm.

The daemon processes ARM before any submit, validates MAC/nonce/host/time/
mode/identity/idempotency, recollects readiness, requires READY with no kill
switch/UNKNOWN/external/unresolved operation, and writes the lease and audit
while holding the existing WriterLease. Effective TTL is the minimum of the
request, 900 seconds, remaining safe session window, and next revalidation
boundary. ARM never resumes, promotes, or submits.

## Windows supervision

Repository-owned PowerShell provides least-privilege Task Scheduler install,
remove/query and an external watchdog. It runs as a dedicated non-admin user,
parses `status --json`, alerts on not-running/stale status, and may restart only
the daemon. It never invokes resume, transition, arm, rebaseline, catch-up, or
orders. Restart remains DISARMED. Scripts contain no account, secret, userdata,
or live config. CI performs only static and PAPER-safe smoke; actual Scheduler
installation and restart drills remain target-machine work.

## Error handling and audit

External payloads are untrusted and strict. Unknown identity, stale evidence,
deadline expiry, payload collision, crash ambiguity, or failed cross-binding
adds a stable blocker, retains evidence, and closes authority. Retrying the
same operation id and same payload is idempotent; a different payload is a
conflict. No failure path guesses “not submitted”, resends UNKNOWN, clears
incident evidence, or restores write authority.

## Verification and completion

Every behavior change follows red -> observed expected failure -> minimal
green -> affected suite. Coherent milestones receive direct/L2/L3 validation,
remote commit, remote SHA verification, and PR-state refresh. The stable final
candidate runs the repository L4 commands plus exact source/wheel/public-
contract parity, migration/rebaseline/transition/restore faults, PAPER/REPLAY/
SHADOW E2E, restart recovery, fixed Replay both modes, receipt verification,
Windows-safe smoke, and GitHub CI/Security/Windows workflows.

After one focused independent review and at most one focused final fix/review
wave, required checks and protections govern squash merge. Main protection is
configured from actual successful check contexts only: PR-only, no force push,
no deletion, no mandatory extra reviewer, and an optional administrator
emergency bypass when GitHub supports it.
