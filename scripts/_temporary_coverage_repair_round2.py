from __future__ import annotations

from pathlib import Path

path = Path("tests/unit/application/test_production_services_branches.py")
text = path.read_text(encoding="utf-8")
old = "    with base.hook_case(tmp_path) as (hooks, _writer, broker, accounts):\n"
new = "    with base.hook_case(tmp_path) as (hooks, _writer, _broker, accounts):\n"
count = text.count(old)
if count != 1:
    raise RuntimeError(f"unused broker anchor: expected one, got {count}")
path.write_text(text.replace(old, new, 1), encoding="utf-8", newline="\n")
