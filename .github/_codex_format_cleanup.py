from pathlib import Path

for raw in (
    "src/firmquant/persistence/account_authority.py",
    "src/firmquant/strategy/account_bootstrap.py",
):
    path = Path(raw)
    text = path.read_text(encoding="utf-8")
    if "\n\n\n\n" not in text:
        raise SystemExit(f"expected formatter-only blank-line marker in {raw}")
    path.write_text(text.replace("\n\n\n\n", "\n\n\n"), encoding="utf-8")
