from __future__ import annotations

from pathlib import Path

path = Path("tests/unit/application/test_production_services_acceptance.py")
text = path.read_text(encoding="utf-8")

replacements = (
    (
        'promotion_config_sha256="p" * 64,',
        'promotion_config_sha256="c" * 64,',
        "promotion digest",
    ),
    (
        'assert ps._fee_schedule(safety_manifest()).minimum_commission == Money(Decimal("5"))',
        'assert ps._fee_schedule(safety_manifest()).minimum_commission == Decimal("5")',
        "fee assertion",
    ),
    (
        '''def ready(hooks: ProductionServiceHooks) -> None:
    hooks._status = RuntimeStatus(
        state=RuntimeState.READY,
        revision=3,
        reason="ready for test",
        blockers=(),
    )
''',
        '''def ready(hooks: ProductionServiceHooks) -> None:
    hooks._transition(RuntimeState.STARTING, reason="test startup")
    hooks._transition(RuntimeState.RECONCILING, reason="test reconciliation")
    hooks._transition(RuntimeState.READY, reason="test ready")
''',
        "ready helper",
    ),
    (
        '''        bad = replace(
            execution_snapshot(),
            market_status=MarketSessionStatus.CLOSED,
        )
''',
        '''        base = execution_snapshot()
        bad = replace(
            base,
            quotes=tuple(
                replace(quote, market_status=MarketSessionStatus.CLOSED)
                for quote in base.quotes
            ),
            market_status=MarketSessionStatus.CLOSED,
        )
''',
        "closed execution facts",
    ),
    (
        '''        monkeypatch.setattr(
            ps.StrategyIdentity,
            "locked",
            lambda: SimpleNamespace(
                uquant_commit="1" * 40,
                verify=lambda: None,
            ),
        )
''',
        '''        monkeypatch.setattr(
            ps.StrategyIdentity,
            "locked",
            lambda: SimpleNamespace(
                uquant_commit="1" * 40,
                canonical_universe_sha256="0" * 64,
                config_fingerprint="0" * 64,
                economic_code_fingerprint="0" * 64,
                verify=lambda: None,
            ),
        )
''',
        "builder identity",
    ),
)
for old, new, label in replacements:
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected one anchor, got {count}")
    text = text.replace(old, new, 1)

path.write_text(text, encoding="utf-8", newline="\n")
