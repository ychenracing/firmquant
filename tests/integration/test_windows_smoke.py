from __future__ import annotations

from scripts.windows_smoke import run_smoke


def test_cross_platform_windows_smoke_initializes_state_before_read_only_doctor() -> None:
    receipt = run_smoke()

    assert receipt["backup_restore_verified"] is True
    assert receipt["broker_adapter"] == "PAPER"
    assert receipt["doctor_checks"] == 15
    assert receipt["python"] == "3.12"
    assert receipt["real_order_calls"] == 0
    assert receipt["timezone"] == "Asia/Shanghai"
    assert receipt["xtquant_readonly_smoke_completed"] is False
