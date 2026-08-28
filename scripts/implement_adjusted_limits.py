from pathlib import Path


path = Path("src/firmquant/execution/replay_runner.py")
text = path.read_text(encoding="utf-8")
import_needle = "from decimal import ROUND_FLOOR, ROUND_HALF_UP, Decimal"
import_replacement = "from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, Decimal"
if text.count(import_needle) != 1:
    raise SystemExit(f"expected exactly one decimal import target, found {text.count(import_needle)}")
text = text.replace(import_needle, import_replacement, 1)
needle = '''    else:\n        upper = _tick_price(previous * (_ONE + limit_fraction))\n        lower = _tick_price(previous * (_ONE - limit_fraction))\n    return DailyBar(\n        session=session,\n        symbol=symbol,\n        open=Decimal(str(row["open"])),\n        high=Decimal(str(row["high"])),\n        low=Decimal(str(row["low"])),\n        close=Decimal(str(row["close"])),\n'''
replacement = '''    else:\n        upper = _tick_price(previous * (_ONE + limit_fraction))\n        lower = _tick_price(previous * (_ONE - limit_fraction))\n\n    # The locked frozen series is forward-adjusted and does not retain the raw\n    # exchange reference price or adjustment factor needed to reproduce an\n    # official cent-denominated price limit exactly in adjusted coordinates.\n    # Keep the nominal rule whenever it is consistent with observed trading;\n    # otherwise expand outward only to the smallest cent-denominated band that\n    # can contain the authoritative OHLC. This preserves the real A-share\n    # two-decimal instrument precision, never narrows a legal band, and changes\n    # neither uquant decisions nor the strategy's economic behavior.\n    observed_open = Decimal(str(row["open"]))\n    observed_high = Decimal(str(row["high"]))\n    observed_low = Decimal(str(row["low"]))\n    observed_close = Decimal(str(row["close"]))\n    observed_upper = observed_high.quantize(Decimal("0.01"), rounding=ROUND_CEILING)\n    observed_lower = observed_low.quantize(Decimal("0.01"), rounding=ROUND_FLOOR)\n    upper = max(upper, observed_upper)\n    lower = min(lower, observed_lower)\n    return DailyBar(\n        session=session,\n        symbol=symbol,\n        open=observed_open,\n        high=observed_high,\n        low=observed_low,\n        close=observed_close,\n'''
if text.count(needle) != 1:
    raise SystemExit(f"expected exactly one daily-bar limit target, found {text.count(needle)}")
path.write_text(text.replace(needle, replacement, 1), encoding="utf-8")
