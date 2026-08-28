from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PATH = ROOT / "src/firmquant/application/live_readiness_runtime.py"
text = PATH.read_text(encoding="utf-8")

old_active = '''    try:\n        active = DataGenerationStore(_resolved(config_path, settings.paths.state_directory)).active()\n        active_manifest_sha256 = active.manifest_sha256\n    except Exception:\n        pass\n'''
new_active = '''    try:\n        active = DataGenerationStore(_resolved(config_path, settings.paths.state_directory)).active()\n        active_manifest_sha256 = active.manifest_sha256\n    except Exception:\n        active_manifest_sha256 = None\n'''
old_calendar = '''    try:\n        calendar = load_trading_calendar_manifest(calendar_path)\n        session = now.astimezone(_SHANGHAI).date()\n        calendar_coverage = calendar.covered_from <= session <= calendar.covered_through\n    except Exception:\n        pass\n'''
new_calendar = '''    try:\n        calendar = load_trading_calendar_manifest(calendar_path)\n        session = now.astimezone(_SHANGHAI).date()\n        calendar_coverage = calendar.covered_from <= session <= calendar.covered_through\n    except Exception:\n        calendar_coverage = False\n'''

for label, old, new in (
    ("active data generation", old_active, new_active),
    ("calendar coverage", old_calendar, new_calendar),
):
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected exactly one match, found {count}")
    text = text.replace(old, new, 1)

PATH.write_text(text, encoding="utf-8")
