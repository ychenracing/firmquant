from pathlib import Path

path = Path("src/firmquant/strategy/account_bootstrap.py")
text = path.read_text(encoding="utf-8")
old = '''        self._preconditions(\n            allow_existing_account_file=pending is not None and self._account_path.exists()\n        )\n'''
new = '''        self._preconditions(allow_existing_account_file=pending is not None and self._account_path.exists())\n'''
if text.count(old) != 1:
    raise SystemExit("expected exactly one formatter marker")
path.write_text(text.replace(old, new), encoding="utf-8")
