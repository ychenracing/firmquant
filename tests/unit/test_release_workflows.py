from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_quality_secret_scan_runs_before_nested_uquant_checkout() -> None:
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    quality_job = workflow.split("  uquant-parity:", maxsplit=1)[0]

    assert quality_job.index("Repository secret scan") < quality_job.index(
        "Checkout locked uquant source for Linux coverage"
    )
