from __future__ import annotations

from pathlib import Path


path = Path("tests/unit/test_runtime_time_health_coverage.py")
text = path.read_text(encoding="utf-8")
text = text.replace(
    "            WriterLeaseGuard(lease, monotonic_clock=lambda: True)\n",
    "            WriterLeaseGuard(\n"
    "                lease,\n"
    "                monotonic_clock=lambda: True,\n"
    "                renew_interval=timedelta(seconds=1),\n"
    "            )\n",
    1,
)
text = text.replace(
    "        guard = WriterLeaseGuard(lease, monotonic_clock=lambda: observed[0])  # type: ignore[return-value]\n",
    "        guard = WriterLeaseGuard(\n"
    "            lease,\n"
    "            monotonic_clock=lambda: observed[0],  # type: ignore[return-value]\n"
    "            renew_interval=timedelta(seconds=1),\n"
    "        )\n",
    1,
)
text = text.replace(
    "        guard2 = WriterLeaseGuard(lease, monotonic_clock=lambda: rollback[0])\n",
    "        guard2 = WriterLeaseGuard(\n"
    "            lease,\n"
    "            monotonic_clock=lambda: rollback[0],\n"
    "            renew_interval=timedelta(seconds=1),\n"
    "        )\n",
    1,
)
path.write_text(text, encoding="utf-8")
