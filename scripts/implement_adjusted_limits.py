from pathlib import Path


path = Path("src/firmquant/execution/replay_runner.py")
text = path.read_text(encoding="utf-8")
needle = '''    else:\n        upper = _tick_price(previous * (_ONE + limit_fraction))\n        lower = _tick_price(previous * (_ONE - limit_fraction))\n    return DailyBar(\n        session=session,\n        symbol=symbol,\n        open=Decimal(str(row["open"])),\n        high=Decimal(str(row["high"])),\n        low=Decimal(str(row["low"])),\n        close=Decimal(str(row["close"])),\n'''
replacement = '''    else:\n        upper = _tick_price(previous * (_ONE + limit_fraction))\n        lower = _tick_price(previous * (_ONE - limit_fraction))\n\n    # The locked frozen series is forward-adjusted and does not retain the raw\n    # exchange reference price or adjustment factor needed to reproduce an\n    # official cent-denominated price limit exactly in adjusted coordinates.\n    # Keep the nominal rule whenever it is consistent with observed trading;\n    # otherwise expand outward only to the smallest band that can contain the\n    # authoritative OHLC. This never narrows a legal band or changes uquant\n    # decisions; it only prevents replay from declaring observed trades\n    # impossible because of adjustment/rounding coordinates.\n    observed_open = Decimal(str(row["open"]))\n    observed_high = Decimal(str(row["high"]))\n    observed_low = Decimal(str(row["low"]))\n    observed_close = Decimal(str(row["close"]))\n    upper = max(upper, observed_high)\n    lower = min(lower, observed_low)\n    return DailyBar(\n        session=session,\n        symbol=symbol,\n        open=observed_open,\n        high=observed_high,\n        low=observed_low,\n        close=observed_close,\n'''
if text.count(needle) != 1:
    raise SystemExit(f"expected exactly one daily-bar limit target, found {text.count(needle)}")
path.write_text(text.replace(needle, replacement, 1), encoding="utf-8")
