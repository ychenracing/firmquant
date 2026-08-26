from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import replace
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from firmquant.domain.broker_facts import Side
from firmquant.observability.reports import (
    DailyReportRenderer,
    DatabaseDailyReportBuilder,
    ReportConflict,
    ReportError,
    TargetActualDifference,
    _json_array,
    _json_object,
    _json_string_array,
    _number,
    _parse_json_object,
    _shares,
    _stored_decimal,
    _stored_time,
    _text,
    _validate_codes,
)
from firmquant.persistence.database import Database
from tests.unit.observability.test_reports import NOW, report


@pytest.mark.parametrize(
    ("factory", "exception"),
    [
        (lambda: _text("", label="value"), ReportError),
        (lambda: _text(" bad", label="value"), ReportError),
        (lambda: _text("x" * 257, label="value"), ReportError),
        (lambda: _text("bad\n", label="value"), ReportError),
        (lambda: _number(1, label="value"), ReportError),
        (lambda: _number(Decimal("NaN"), label="value"), ReportError),
        (lambda: _number(Decimal("-1"), label="value"), ReportError),
        (lambda: _number(Decimal("1E+999999"), label="value"), ReportError),
        (lambda: _shares(True, label="shares"), ReportError),
        (lambda: _shares("1", label="shares"), ReportError),
        (lambda: _shares(-1, label="shares"), ReportError),
        (lambda: _shares(0, label="shares", positive=True), ReportError),
        (lambda: _validate_codes(["A"], label="codes"), ReportError),
        (lambda: _validate_codes(("B", "A"), label="codes"), ReportError),
        (lambda: _validate_codes((" bad",), label="codes"), ReportError),
        (lambda: _json_object([], label="value"), ReportError),
        (lambda: _json_object({1: "value"}, label="value"), ReportError),
        (lambda: _json_array({}, label="value"), ReportError),
        (lambda: _json_string_array([1], label="value"), ReportError),
        (lambda: _parse_json_object(1, label="value"), ReportError),
        (lambda: _parse_json_object("{", label="value"), ReportError),
        (lambda: _parse_json_object("[]", label="value"), ReportError),
        (lambda: _parse_json_object('{"value":NaN}', label="value"), ReportError),
        (lambda: _stored_decimal(True, label="value"), ReportError),
        (lambda: _stored_decimal(object(), label="value"), ReportError),
        (lambda: _stored_decimal("bad", label="value"), ReportError),
        (lambda: _stored_decimal(-1, label="value"), ReportError),
        (lambda: _stored_decimal(0, label="value", positive=True), ReportError),
        (lambda: _stored_time(1, label="value"), ReportError),
        (lambda: _stored_time("bad", label="value"), ReportError),
        (lambda: _stored_time("2026-08-25T01:00:00", label="value"), ReportError),
    ],
)
def test_report_primitive_validation_rejects_untrusted_evidence(
    factory: Callable[[], object], exception: type[Exception]
) -> None:
    with pytest.raises(exception):
        factory()


def test_report_primitive_normalization_preserves_exact_decimal_values() -> None:
    assert _text(None, label="optional") is None
    assert _text("canonical", label="value") == "canonical"
    assert _number(Decimal("1.23456"), label="value") == Decimal("1.2346")
    assert _number(Decimal("-1"), label="value", nonnegative=False) == Decimal("-1.0000")
    assert _json_object({"value": 1}, label="value") == {"value": 1}
    assert _json_string_array(["A"], label="value") == ["A"]
    assert _stored_decimal("1.25", label="value") == Decimal("1.2500")
    assert _stored_time(NOW.isoformat(), label="value") == NOW


@pytest.mark.parametrize(
    "change",
    [
        {"symbol": ""},
        {"target_weight": Decimal("-0.1")},
        {"actual_weight": Decimal("NaN")},
        {"target_weight": Decimal("1.1")},
        {"actual_weight": Decimal("1.1")},
    ],
)
def test_target_actual_difference_rejects_invalid_weights(change: dict[str, object]) -> None:
    valid = TargetActualDifference("600519.SH", Decimal("0.5"), Decimal("0.4"))
    with pytest.raises(ReportError):
        replace(valid, **change)


@pytest.mark.parametrize(
    ("change", "exception"),
    [
        ({"uquant_order_id": ""}, ReportError),
        ({"execution_id": " bad"}, ReportError),
        ({"broker_order_id": "bad\n"}, ReportError),
        ({"symbol": ""}, ReportError),
        ({"side": "BUY"}, TypeError),
        ({"requested_shares": 0}, ReportError),
        ({"requested_shares": True}, ReportError),
        ({"filled_shares": -1}, ReportError),
        ({"filled_shares": 1001}, ReportError),
        ({"state": ""}, ReportError),
        ({"reason_code": ""}, ReportError),
    ],
)
def test_order_lifecycle_rejects_inconsistent_evidence(
    change: dict[str, object], exception: type[Exception]
) -> None:
    with pytest.raises(exception):
        replace(report().orders[0], **change)


@pytest.mark.parametrize(
    ("change", "exception"),
    [
        ({"broker_fill_id": ""}, ReportError),
        ({"broker_order_id": " bad"}, ReportError),
        ({"symbol": ""}, ReportError),
        ({"side": "BUY"}, TypeError),
        ({"shares": 0}, ReportError),
        ({"price": Decimal(0)}, ReportError),
        ({"price": Decimal("NaN")}, ReportError),
        ({"commission": Decimal("-1")}, ReportError),
        ({"stamp_duty": 0}, ReportError),
        ({"planned_price": Decimal(0)}, ReportError),
        ({"next_open_price": Decimal("-1")}, ReportError),
    ],
)
def test_execution_fill_rejects_invalid_money_shares_or_references(
    change: dict[str, object], exception: type[Exception]
) -> None:
    with pytest.raises(exception):
        replace(report().fills[0], **change)


def test_sell_slippage_direction_is_symmetric_and_explicit() -> None:
    fill = replace(
        report().fills[0],
        side=Side.SELL,
        price=Decimal("9.90"),
        planned_price=Decimal("10"),
        next_open_price=Decimal("10.10"),
    )
    payload = fill.payload()
    assert payload["slippage_vs_plan_bps"] == "100.0000"
    assert payload["slippage_vs_next_open_bps"] == "198.0198"


@pytest.mark.parametrize(
    ("change", "exception"),
    [
        ({"session": datetime(2026, 8, 26)}, TypeError),
        ({"strategy_session": datetime(2026, 8, 25)}, TypeError),
        ({"generated_at": datetime(2026, 8, 26)}, ReportError),
        ({"decision_id": " bad"}, ReportError),
        ({"available_cash": Decimal("-1")}, ReportError),
        ({"total_assets": Decimal("NaN")}, ReportError),
        ({"actual_gross": Decimal("1.1")}, ReportError),
        ({"target_gross": Decimal("1.1")}, ReportError),
        ({"target_actual_differences": []}, TypeError),
        ({"orders": []}, TypeError),
        ({"fills": []}, TypeError),
        ({"risk_events": ["RISK"]}, ReportError),
        ({"risk_events": ("Z", "A")}, ReportError),
        ({"reconciliation_passed": 1}, TypeError),
        ({"reconciliation_passed": True}, ReportError),
        ({"runtime_state": ""}, ReportError),
        ({"health_blockers": ("Z", "A")}, ReportError),
    ],
)
def test_daily_report_rejects_inconsistent_or_untyped_evidence(
    change: dict[str, object], exception: type[Exception]
) -> None:
    with pytest.raises(exception):
        replace(report(), **change)


def test_renderer_rejects_untyped_report() -> None:
    renderer = DailyReportRenderer()
    with pytest.raises(TypeError):
        renderer.render_json(object())  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        renderer.render_markdown(object())  # type: ignore[arg-type]


def test_markdown_covers_empty_and_complete_optional_sections() -> None:
    renderer = DailyReportRenderer()
    empty = replace(
        report(),
        strategy_session=None,
        decision_id=None,
        target_gross=None,
        target_actual_differences=(),
        orders=(),
        fills=(),
        risk_events=(),
        reconciliation_passed=True,
        reconciliation_blockers=(),
        health_blockers=(),
    )
    markdown = renderer.render_markdown(empty)
    assert "无委托" in markdown
    assert "无成交" in markdown
    assert "- 风险事件: 无" in markdown
    assert "- 对账: 通过" in markdown

    with_reference = replace(
        report(),
        fills=(replace(report().fills[0], next_open_price=Decimal("10.10")),),
    )
    assert "相对下一开盘价" in renderer.render_markdown(with_reference)


def test_report_publish_is_idempotent_and_detects_conflicts(tmp_path: Path) -> None:
    renderer = DailyReportRenderer()
    receipt = renderer.write(report(), tmp_path)
    assert renderer.write(report(), tmp_path) == receipt

    (tmp_path / "2026-08-26.json").write_text("different", encoding="utf-8")
    with pytest.raises(ReportConflict, match="already differs"):
        renderer.write(report(), tmp_path)


def test_report_publish_rejects_symlink_temporary_collision_and_missing_directory(
    tmp_path: Path,
) -> None:
    renderer = DailyReportRenderer()
    missing = tmp_path / "missing"
    with pytest.raises(ReportError, match="directory"):
        renderer.write(report(), missing)

    target = tmp_path / "target"
    target.write_text("target", encoding="utf-8")
    linked = tmp_path / "2026-08-26.json"
    linked.symlink_to(target)
    with pytest.raises(ReportError, match="symbolic link"):
        renderer.write(report(), tmp_path)
    linked.unlink()

    temporary = tmp_path / f".2026-08-26.json.{report().report_id}.tmp"
    temporary.touch()
    with pytest.raises(ReportConflict, match="temporary path"):
        renderer.write(report(), tmp_path)


def test_report_builder_rejects_invalid_dependencies_session_and_missing_snapshot(
    tmp_path: Path,
) -> None:
    with pytest.raises(TypeError):
        DatabaseDailyReportBuilder(object(), clock=lambda: NOW)  # type: ignore[arg-type]
    database = Database.open(tmp_path / "firmquant.sqlite3")
    try:
        with pytest.raises(TypeError):
            DatabaseDailyReportBuilder(database, clock=object())  # type: ignore[arg-type]
        builder = DatabaseDailyReportBuilder(database, clock=lambda: NOW)
        with pytest.raises(TypeError):
            builder.build(datetime(2026, 8, 26))  # type: ignore[arg-type]
        with pytest.raises(ReportError, match="cash snapshot"):
            builder.build(date(2026, 8, 26))
    finally:
        database.close()


def test_report_evidence_clock_cannot_be_nonfinite_float() -> None:
    with pytest.raises(ReportError):
        _stored_decimal(math.inf, label="value")
