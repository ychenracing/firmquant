from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "src/firmquant/execution/replay_runner.py"
WORKFLOW = ROOT / ".github/workflows/replay-synthetic-fact-fix.yml"


def replace_once(text: str, old: str, new: str, *, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected one match, found {count}")
    return text.replace(old, new, 1)


def remove_between(text: str, start: str, end: str, *, label: str) -> str:
    start_index = text.find(start)
    if start_index < 0:
        raise RuntimeError(f"{label}: start marker missing")
    end_index = text.find(end, start_index)
    if end_index < 0:
        raise RuntimeError(f"{label}: end marker missing")
    return text[:start_index] + text[end_index:]


text = TARGET.read_text(encoding="utf-8")
text = replace_once(
    text,
    "from decimal import ROUND_HALF_UP, Decimal\n",
    "from decimal import ROUND_FLOOR, ROUND_HALF_UP, Decimal\n",
    label="decimal rounding import",
)
text = replace_once(
    text,
    "    ReplayOrder,\n    ReplaySide,\n",
    "    ReplayOrder,\n    ReplaySessionResult,\n    ReplaySide,\n",
    label="replay result import",
)
text = replace_once(
    text,
    "        raw_target_shares = int((target_equity * weight / bar.open).to_integral_value(rounding=\"ROUND_FLOOR\"))\n",
    "        raw_target_shares = int(\n            (target_equity * weight / bar.open).to_integral_value(rounding=ROUND_FLOOR)\n        )\n",
    label="tracking rounding",
)
text = replace_once(
    text,
    "    result: object,\n) -> dict[str, Decimal]:\n",
    "    result: ReplaySessionResult,\n) -> dict[str, Decimal]:\n",
    label="average cost result type",
)
text = replace_once(
    text,
    "    result: object | None,\n",
    "    result: ReplaySessionResult | None,\n",
    label="broker fact result type",
)
text = replace_once(
    text,
    "            execution_result: object | None = None\n",
    "            execution_result: ReplaySessionResult | None = None\n",
    label="runner result type",
)
text = remove_between(
    text,
    "    for blocker in plan.blockers:\n",
    "    return tuple(orders), tuple(fills)\n",
    label="unsubmitted blocker broker facts",
)
TARGET.write_text(text, encoding="utf-8")
Path(__file__).unlink()
WORKFLOW.unlink()
