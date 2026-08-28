from __future__ import annotations

import os
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from firmquant.execution.replay_runner import run_execution_replay


def _locked_source_checkout() -> Path:
    raw = os.environ.get("FIRMQUANT_UQUANT_SOURCE_CHECKOUT")
    if not raw:
        pytest.skip("locked uquant source checkout is required for restart replay integration")
    path = Path(raw).resolve()
    if not path.is_dir():
        pytest.fail("locked uquant source checkout does not exist")
    return path


def test_execution_replay_restart_each_session_matches_continuous_summary() -> None:
    source_checkout = _locked_source_checkout()
    data_root = source_checkout / "data" / "frozen"
    kwargs = {
        "source_checkout": source_checkout,
        "data_root": data_root,
        "start": date(2026, 7, 1),
        "end": date(2026, 7, 10),
        "max_price_deviation_bps": Decimal("100"),
    }

    continuous = run_execution_replay(**kwargs, restart_each_session=False)
    restarted = run_execution_replay(**kwargs, restart_each_session=True)

    assert restarted.canonical_json() == continuous.canonical_json()
