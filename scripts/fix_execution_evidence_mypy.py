from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "src/firmquant/application/execution_evidence_runtime.py"
REPLAY = ROOT / "src/firmquant/execution/replay_runner.py"
READINESS = ROOT / "src/firmquant/application/live_readiness_runtime.py"
OPERATIONS = ROOT / "src/firmquant/application/operations.py"


def replace_once(path: Path, old: str, new: str, *, label: str) -> None:
    text = path.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected exactly one match, found {count}")
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


runtime = RUNTIME.read_text(encoding="utf-8")
authorized_count = runtime.count(".authorized_shares")
if authorized_count != 7:
    raise RuntimeError(f"runtime authorized shares: expected 7 matches, found {authorized_count}")
RUNTIME.write_text(runtime.replace(".authorized_shares", ".uquant_authorized_shares"), encoding="utf-8")

replace_once(
    RUNTIME,
    '''    symbols = {item.symbol for item in facts.broker_snapshot.positions}\n    for raw in decision.uquant_payload.get("targets", []):\n        if isinstance(raw, dict) and isinstance(raw.get("symbol"), str):\n            symbols.add(Symbol.parse(str(raw["symbol"])))\n''',
    '''    symbols = {item.symbol for item in facts.broker_snapshot.positions}\n    raw_targets = decision.uquant_payload.get("targets", [])\n    if not isinstance(raw_targets, list):\n        raise RuntimeEvidenceError("decision target evidence is malformed")\n    for raw in raw_targets:\n        if isinstance(raw, dict) and isinstance(raw.get("symbol"), str):\n            symbols.add(Symbol.parse(str(raw["symbol"])))\n''',
    label="runtime target narrowing",
)
replace_once(
    RUNTIME,
    '''        planned_shares = int(planned["planned_shares"])\n        blocker = None\n''',
    '''        raw_planned_shares = planned.get("planned_shares")\n        if isinstance(raw_planned_shares, bool) or not isinstance(raw_planned_shares, int):\n            raise RuntimeEvidenceError("CANARY planned shares are malformed")\n        planned_shares = raw_planned_shares\n        blocker = None\n''',
    label="runtime planned shares narrowing",
)

replace_once(
    REPLAY,
    "import pandas as pd\n",
    "import pandas as pd  # type: ignore[import-untyped]  # uquant owns the runtime pandas dependency\n",
    label="pandas typing boundary",
)
replace_once(
    REPLAY,
    "from firmquant.strategy.account_sync import sync_account\n",
    "from firmquant.strategy.account_sync import AccountStateContract, sync_account\n",
    label="account protocol import",
)
replace_once(
    REPLAY,
    '''def _account_state(initial_cash: float) -> object:\n''',
    '''def _account_state(initial_cash: float) -> AccountStateContract:\n''',
    label="account state return type",
)
replace_once(
    REPLAY,
    '''    return empty(initial_cash)\n\n\ndef _account_sha256(account: object) -> str:\n''',
    '''    return cast(AccountStateContract, empty(initial_cash))\n\n\ndef _account_sha256(account: object) -> str:\n''',
    label="account state cast",
)
replay = REPLAY.read_text(encoding="utf-8")
authorized_count = replay.count(".authorized_shares")
if authorized_count != 3:
    raise RuntimeError(f"replay authorized shares: expected 3 matches, found {authorized_count}")
REPLAY.write_text(replay.replace(".authorized_shares", ".uquant_authorized_shares"), encoding="utf-8")
replace_once(
    REPLAY,
    '''    if row is None:\n        # Missing row on an authoritative index trading session is a suspension, not a fabricated K-line.\n        fraction = _limit_fraction(parsed, panel, session) or _ONE\n        return DailyBar(\n            session=session,\n            symbol=symbol,\n            open=previous,\n            high=previous,\n            low=previous,\n            close=previous,\n            previous_close=previous,\n            volume=0,\n            suspended=True,\n            limit_up=_tick_price(previous * (_ONE + fraction)),\n            limit_down=_tick_price(previous * max(_ONE - fraction, Decimal("0.01"))),\n        )\n    fraction = _limit_fraction(parsed, panel, session)\n    if fraction is None:\n        upper = _tick_price(previous * Decimal("10"))\n        lower = _tick_price(previous * Decimal("0.10"))\n    else:\n        upper = _tick_price(previous * (_ONE + fraction))\n        lower = _tick_price(previous * (_ONE - fraction))\n''',
    '''    if row is None:\n        # Missing row on an authoritative index trading session is a suspension, not a fabricated K-line.\n        suspension_fraction = _limit_fraction(parsed, panel, session) or _ONE\n        return DailyBar(\n            session=session,\n            symbol=symbol,\n            open=previous,\n            high=previous,\n            low=previous,\n            close=previous,\n            previous_close=previous,\n            volume=0,\n            suspended=True,\n            limit_up=_tick_price(previous * (_ONE + suspension_fraction)),\n            limit_down=_tick_price(previous * max(_ONE - suspension_fraction, Decimal("0.01"))),\n        )\n    limit_fraction = _limit_fraction(parsed, panel, session)\n    if limit_fraction is None:\n        upper = _tick_price(previous * Decimal("10"))\n        lower = _tick_price(previous * Decimal("0.10"))\n    else:\n        upper = _tick_price(previous * (_ONE + limit_fraction))\n        lower = _tick_price(previous * (_ONE - limit_fraction))\n''',
    label="limit fraction narrowing",
)
replace_once(
    REPLAY,
    '''    for symbol in symbols:\n        panel = panels.get(symbol)\n        if panel is None:\n            raise ExecutionReplayError(f"frozen panel is unavailable for {symbol}")\n        bars[symbol] = _daily_bar(symbol, panel, session)\n''',
    '''    for symbol_text in symbols:\n        panel = panels.get(symbol_text)\n        if panel is None:\n            raise ExecutionReplayError(f"frozen panel is unavailable for {symbol_text}")\n        bars[symbol_text] = _daily_bar(symbol_text, panel, session)\n''',
    label="execution facts string symbol",
)
replace_once(
    REPLAY,
    '''    for symbol_text in symbols:\n        bar = bars[symbol_text]\n        symbol = Symbol.parse(symbol_text)\n        status = SecurityStatus.SUSPENDED if bar.suspended else SecurityStatus.TRADING\n        instrument = InstrumentFact(\n            symbol=symbol,\n''',
    '''    for symbol_text in symbols:\n        bar = bars[symbol_text]\n        parsed_symbol = Symbol.parse(symbol_text)\n        status = SecurityStatus.SUSPENDED if bar.suspended else SecurityStatus.TRADING\n        instrument = InstrumentFact(\n            symbol=parsed_symbol,\n''',
    label="execution facts typed symbol",
)
replace_once(
    REPLAY,
    '''        quote = QuoteFact(\n            symbol=symbol,\n''',
    '''        quote = QuoteFact(\n            symbol=parsed_symbol,\n''',
    label="quote typed symbol",
)

replace_once(
    READINESS,
    "        calendar_coverage = calendar.coverage_start <= session <= calendar.coverage_end\n",
    "        calendar_coverage = calendar.covered_from <= session <= calendar.covered_through\n",
    label="calendar coverage fields",
)

replace_once(
    OPERATIONS,
    '''        for value in (self.start_session, self.end_session):\n            if value is not None and type(value) is not date:\n                raise TypeError("operator replay range must contain dates")\n''',
    '''        for session_value in (self.start_session, self.end_session):\n            if session_value is not None and type(session_value) is not date:\n                raise TypeError("operator replay range must contain dates")\n''',
    label="operator date loop",
)
replace_once(
    OPERATIONS,
    '''        for value in (self.events_path, self.bundle_path, self.account_state_path):\n            if value is not None and not isinstance(value, Path):\n                raise TypeError("operator path values must be pathlib.Path")\n''',
    '''        for path_value in (self.events_path, self.bundle_path, self.account_state_path):\n            if path_value is not None and not isinstance(path_value, Path):\n                raise TypeError("operator path values must be pathlib.Path")\n''',
    label="operator path loop",
)
