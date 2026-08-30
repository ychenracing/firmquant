from __future__ import annotations

import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_quality_secret_scan_runs_before_nested_uquant_checkout() -> None:
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    quality_job = workflow.split("  uquant-parity:", maxsplit=1)[0]

    assert quality_job.index("Repository secret scan") < quality_job.index(
        "Checkout locked uquant source for Linux coverage"
    )


def test_linux_coverage_pythonpath_includes_repository_root_first() -> None:
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")

    assert (
        "PYTHONPATH: ${{ github.workspace }}:${{ github.workspace }}/.uquant-source:"
        "${{ github.workspace }}/src"
    ) in workflow


def test_gitleaks_allowlist_is_rule_path_and_line_scoped() -> None:
    config = tomllib.loads((ROOT / ".gitleaks.toml").read_text(encoding="utf-8"))

    assert config["extend"] == {"useDefault": True}
    assert set(config) == {"extend", "allowlists"}
    assert len(config["allowlists"]) == 1
    allowlist = config["allowlists"][0]
    assert allowlist["targetRules"] == ["generic-api-key"]
    assert allowlist["condition"] == "AND"
    assert allowlist["regexTarget"] == "line"
    assert allowlist["paths"] == [
        r"^src/firmquant/resources/source_identity\.json$",
        r"^tests/(integration/test_uquant_parity|unit/test_build_identity|unit/strategy/test_identity)\.py$",
    ]
    assert allowlist["regexes"] == [
        r'^\s*"public_api_contract_sha256"\s*:\s*"[a-f0-9]{64}",?\s*$',
        r'^\s*EXPECTED_PUBLIC_(API_)?CONTRACT_SHA256\s*=\s*"[a-f0-9]{64}"\s*$',
    ]
    assert "commits" not in allowlist
