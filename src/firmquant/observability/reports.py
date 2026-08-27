"""Immutable daily execution reports rendered consistently as JSON and Markdown."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation
from pathlib import Path
from typing import Never
from zoneinfo import ZoneInfo

from firmquant.domain.broker_facts import BrokerOrderStatus, Side
from firmquant.domain.values import Symbol
from firmquant.persistence.database import Database

_QUANTUM = Decimal("0.0001")
_SHANGHAI = ZoneInfo("Asia/Shanghai")


class ReportError(RuntimeError):
    """A report could not be validated or published safely."""


class ReportConflict(ReportError):
    """An immutable report path already contains different evidence."""


def _aware(value: datetime) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ReportError("report timestamp must be timezone-aware")


def _text(value: str | None, *, label: str) -> str | None:
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > 256
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ReportError(f"{label} must be canonical text")
    return value


def _number(value: Decimal, *, label: str, nonnegative: bool = True) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ReportError(f"{label} must be a finite Decimal")
    if nonnegative and value < 0:
        raise ReportError(f"{label} must be nonnegative")
    try:
        return value.quantize(_QUANTUM, rounding=ROUND_HALF_EVEN)
    except InvalidOperation as error:
        raise ReportError(f"{label} exceeds report precision bounds") from error


def _render_decimal(value: Decimal | None) -> str | None:
    return None if value is None else format(value, ".4f")


def _shares(value: int, *, label: str, positive: bool = False) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ReportError(f"{label} must be integer shares")
    if value < 0 or (positive and value == 0):
        raise ReportError(f"{label} is outside share bounds")


def _validate_codes(values: tuple[str, ...], *, label: str) -> None:
    if not isinstance(values, tuple) or tuple(sorted(set(values))) != values:
        raise ReportError(f"report {label} must be sorted and unique")
    for value in values:
        _text(value, label=f"report {label}")


def _reject_constant(value: str) -> Never:
    raise ReportError(f"report evidence contains non-standard JSON constant: {value}")


def _json_object(value: object, *, label: str) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ReportError(f"{label} must be a JSON object")
    return value


def _json_array(value: object, *, label: str) -> list[object]:
    if not isinstance(value, list):
        raise ReportError(f"{label} must be a JSON array")
    return value


def _json_string_array(value: object, *, label: str) -> list[str]:
    values = _json_array(value, label=label)
    if not all(isinstance(item, str) for item in values):
        raise ReportError(f"{label} must contain only text")
    return [item for item in values if isinstance(item, str)]


def _parse_json_object(value: object, *, label: str) -> dict[str, object]:
    if not isinstance(value, str):
        raise ReportError(f"{label} must be JSON text")
    try:
        parsed: object = json.loads(value, parse_constant=_reject_constant)
    except (json.JSONDecodeError, ValueError) as error:
        raise ReportError(f"{label} is invalid JSON") from error
    return _json_object(parsed, label=label)


def _stored_decimal(value: object, *, label: str, positive: bool = False) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ReportError(f"{label} is not decimal-compatible")
    try:
        observed = Decimal(str(value))
    except InvalidOperation as error:
        raise ReportError(f"{label} is not decimal-compatible") from error
    normalized = _number(observed, label=label)
    if positive and normalized <= 0:
        raise ReportError(f"{label} must be positive")
    return normalized


def _stored_time(value: object, *, label: str) -> datetime:
    if not isinstance(value, str):
        raise ReportError(f"{label} must be timestamp text")
    try:
        observed = datetime.fromisoformat(value)
    except ValueError as error:
        raise ReportError(f"{label} is not ISO-8601") from error
    _aware(observed)
    return observed


def _stored_broker_status(value: object) -> BrokerOrderStatus | None:
    if value is None:
        return None
    try:
        return BrokerOrderStatus(str(value))
    except ValueError as error:
        raise ReportError("stored broker order status is not canonical") from error


@dataclass(frozen=True, slots=True)
class TargetActualDifference:
    symbol: str
    target_weight: Decimal
    actual_weight: Decimal

    def __post_init__(self) -> None:
        _text(self.symbol, label="difference symbol")
        target = _number(self.target_weight, label="target weight")
        actual = _number(self.actual_weight, label="actual weight")
        if target > 1 or actual > 1:
            raise ReportError("report weights cannot exceed one")
        object.__setattr__(self, "target_weight", target)
        object.__setattr__(self, "actual_weight", actual)

    @property
    def difference_weight(self) -> Decimal:
        return _number(
            self.actual_weight - self.target_weight,
            label="difference weight",
            nonnegative=False,
        )

    def payload(self) -> dict[str, object]:
        return {
            "symbol": self.symbol,
            "target_weight": _render_decimal(self.target_weight),
            "actual_weight": _render_decimal(self.actual_weight),
            "difference_weight": _render_decimal(self.difference_weight),
        }


@dataclass(frozen=True, slots=True)
class OrderLifecycle:
    uquant_order_id: str
    execution_id: str | None
    broker_order_id: str | None
    symbol: str
    side: Side
    requested_shares: int
    filled_shares: int
    state: str
    reason_code: str
    broker_status: BrokerOrderStatus | None = None

    def __post_init__(self) -> None:
        for label, value in (
            ("uquant order id", self.uquant_order_id),
            ("execution id", self.execution_id),
            ("broker order id", self.broker_order_id),
            ("order symbol", self.symbol),
            ("order state", self.state),
            ("order reason code", self.reason_code),
        ):
            _text(value, label=label)
        if not isinstance(self.side, Side):
            raise TypeError("report order side must be typed")
        if self.broker_status is not None and not isinstance(self.broker_status, BrokerOrderStatus):
            raise TypeError("report broker order status must be typed or null")
        _shares(self.requested_shares, label="requested shares", positive=True)
        _shares(self.filled_shares, label="filled shares")
        if self.filled_shares > self.requested_shares:
            raise ReportError("report filled shares exceed requested shares")

    @property
    def rejected(self) -> bool:
        return self.state in {"REJECTED", "EXPIRED", "UNKNOWN"}

    def payload(self) -> dict[str, object]:
        return {
            "uquant_order_id": self.uquant_order_id,
            "execution_id": self.execution_id,
            "broker_order_id": self.broker_order_id,
            "symbol": self.symbol,
            "side": self.side.value,
            "requested_shares": self.requested_shares,
            "filled_shares": self.filled_shares,
            "unfilled_shares": self.requested_shares - self.filled_shares,
            "state": self.state,
            "broker_status": None if self.broker_status is None else self.broker_status.value,
            "reason_code": self.reason_code,
        }


@dataclass(frozen=True, slots=True)
class ExecutionFill:
    broker_fill_id: str
    broker_order_id: str
    symbol: str
    side: Side
    shares: int
    price: Decimal
    commission: Decimal
    stamp_duty: Decimal
    transfer_fee: Decimal
    planned_price: Decimal | None
    next_open_price: Decimal | None

    def __post_init__(self) -> None:
        for label, value in (
            ("broker fill id", self.broker_fill_id),
            ("broker order id", self.broker_order_id),
            ("fill symbol", self.symbol),
        ):
            _text(value, label=label)
        if not isinstance(self.side, Side):
            raise TypeError("report fill side must be typed")
        _shares(self.shares, label="fill shares", positive=True)
        for field in ("price", "commission", "stamp_duty", "transfer_fee"):
            object.__setattr__(self, field, _number(getattr(self, field), label=field))
        if self.price <= 0:
            raise ReportError("fill price must be positive")
        for field in ("planned_price", "next_open_price"):
            value = getattr(self, field)
            if value is not None:
                normalized = _number(value, label=field)
                if normalized <= 0:
                    raise ReportError(f"{field} must be positive")
                object.__setattr__(self, field, normalized)

    @property
    def total_fees(self) -> Decimal:
        return _number(
            self.commission + self.stamp_duty + self.transfer_fee,
            label="total fees",
        )

    def _slippage(self, reference: Decimal | None) -> Decimal | None:
        if reference is None:
            return None
        raw = (
            (self.price - reference) / reference
            if self.side is Side.BUY
            else (reference - self.price) / reference
        ) * Decimal("10000")
        return _number(raw, label="slippage bps", nonnegative=False)

    def payload(self) -> dict[str, object]:
        return {
            "broker_fill_id": self.broker_fill_id,
            "broker_order_id": self.broker_order_id,
            "symbol": self.symbol,
            "side": self.side.value,
            "shares": self.shares,
            "price": _render_decimal(self.price),
            "commission": _render_decimal(self.commission),
            "stamp_duty": _render_decimal(self.stamp_duty),
            "transfer_fee": _render_decimal(self.transfer_fee),
            "total_fees": _render_decimal(self.total_fees),
            "planned_price": _render_decimal(self.planned_price),
            "next_open_price": _render_decimal(self.next_open_price),
            "slippage_vs_plan_bps": _render_decimal(self._slippage(self.planned_price)),
            "slippage_vs_next_open_bps": _render_decimal(self._slippage(self.next_open_price)),
        }


@dataclass(frozen=True, slots=True)
class DailyReport:
    session: date
    strategy_session: date | None
    generated_at: datetime
    decision_id: str | None
    available_cash: Decimal
    total_assets: Decimal
    actual_gross: Decimal
    target_gross: Decimal | None
    target_actual_differences: tuple[TargetActualDifference, ...]
    orders: tuple[OrderLifecycle, ...]
    fills: tuple[ExecutionFill, ...]
    risk_events: tuple[str, ...]
    reconciliation_passed: bool
    reconciliation_blockers: tuple[str, ...]
    runtime_state: str
    health_blockers: tuple[str, ...]

    def __post_init__(self) -> None:
        if type(self.session) is not date:
            raise TypeError("report session must be a date")
        if self.strategy_session is not None and type(self.strategy_session) is not date:
            raise TypeError("report strategy session must be a date")
        _aware(self.generated_at)
        _text(self.decision_id, label="report decision id")
        for field in ("available_cash", "total_assets", "actual_gross"):
            object.__setattr__(self, field, _number(getattr(self, field), label=field))
        if self.target_gross is not None:
            object.__setattr__(
                self,
                "target_gross",
                _number(self.target_gross, label="target gross"),
            )
        if self.actual_gross > 1 or (self.target_gross is not None and self.target_gross > 1):
            raise ReportError("report gross weights cannot exceed one")
        typed_tuples = (
            (self.target_actual_differences, TargetActualDifference, "differences"),
            (self.orders, OrderLifecycle, "orders"),
            (self.fills, ExecutionFill, "fills"),
        )
        for values, expected, label in typed_tuples:
            if not isinstance(values, tuple) or any(not isinstance(item, expected) for item in values):
                raise TypeError(f"report {label} must be a typed tuple")
        _validate_codes(self.risk_events, label="risk events")
        _validate_codes(self.reconciliation_blockers, label="reconciliation blockers")
        _validate_codes(self.health_blockers, label="health blockers")
        if type(self.reconciliation_passed) is not bool:
            raise TypeError("report reconciliation result must be bool")
        if self.reconciliation_passed != (not self.reconciliation_blockers):
            raise ReportError("report reconciliation result contradicts blockers")
        _text(self.runtime_state, label="report runtime state")

    def _identity_payload(self) -> dict[str, object]:
        return {
            "schema": "firmquant.daily-report.v1",
            "session": self.session.isoformat(),
            "strategy_session": (
                None if self.strategy_session is None else self.strategy_session.isoformat()
            ),
            "generated_at": self.generated_at.isoformat(),
            "decision_id": self.decision_id,
            "funds": {
                "available_cash": _render_decimal(self.available_cash),
                "total_assets": _render_decimal(self.total_assets),
                "actual_gross": _render_decimal(self.actual_gross),
                "target_gross": _render_decimal(self.target_gross),
            },
            "target_actual_differences": [
                item.payload()
                for item in sorted(self.target_actual_differences, key=lambda value: value.symbol)
            ],
            "orders": [item.payload() for item in self.orders],
            "rejected_orders": [item.payload() for item in self.orders if item.rejected],
            "unfilled_orders": [
                item.payload() for item in self.orders if item.filled_shares < item.requested_shares
            ],
            "fills": [item.payload() for item in self.fills],
            "risk_events": list(self.risk_events),
            "reconciliation": {
                "passed": self.reconciliation_passed,
                "blockers": list(self.reconciliation_blockers),
            },
            "health": {
                "runtime_state": self.runtime_state,
                "blockers": list(self.health_blockers),
            },
        }

    @property
    def report_id(self) -> str:
        encoded = json.dumps(
            self._identity_payload(),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        return "report_" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def payload(self) -> dict[str, object]:
        return {"report_id": self.report_id, **self._identity_payload()}


@dataclass(frozen=True, slots=True)
class ReportWriteReceipt:
    report_id: str
    session: date
    json_sha256: str
    markdown_sha256: str


class DailyReportRenderer:
    def render_json(self, report: DailyReport) -> str:
        if not isinstance(report, DailyReport):
            raise TypeError("daily report renderer requires DailyReport")
        return (
            json.dumps(
                report.payload(),
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
        )

    def render_markdown(self, report: DailyReport) -> str:
        if not isinstance(report, DailyReport):
            raise TypeError("daily report renderer requires DailyReport")
        lines = [
            f"# firmquant 日报 — {report.session.isoformat()}",
            "",
            f"- 报告 ID: `{report.report_id}`",
            f"- 策略 session: `{report.strategy_session}`",
            f"- 决策 ID: `{report.decision_id}`",
            f"- 运行状态: `{report.runtime_state}`",
            "",
            "## 资金与总仓",
            "",
            f"- 可用现金: {_render_decimal(report.available_cash)}",
            f"- 总资产: {_render_decimal(report.total_assets)}",
            f"- 实际总仓: {_render_decimal(report.actual_gross)}",
            f"- 目标总仓: {_render_decimal(report.target_gross)}",
            "",
            "## 目标与实际差异",
            "",
            "| 证券 | 目标权重 | 实际权重 | 差异 |",
            "|---|---:|---:|---:|",
        ]
        lines.extend(
            f"| {item.symbol} | {_render_decimal(item.target_weight)} | "
            f"{_render_decimal(item.actual_weight)} | {_render_decimal(item.difference_weight)} |"
            for item in report.target_actual_differences
        )
        lines.extend(["", "## 委托生命周期", ""])
        if not report.orders:
            lines.append("无委托。")
        else:
            lines.extend(
                f"- `{item.uquant_order_id}` {item.side.value} {item.symbol}: "
                f"{item.filled_shares}/{item.requested_shares}, {item.state}, "
                f"broker={item.broker_status.value if item.broker_status is not None else 'N/A'}, "
                f"{item.reason_code}"
                for item in report.orders
            )
        lines.extend(["", "## 成交、费用与滑点", ""])
        if not report.fills:
            lines.append("无成交。")
        for fill in report.fills:
            payload = fill.payload()
            lines.append(
                f"- `{fill.broker_fill_id}` {fill.side.value} {fill.symbol} {fill.shares} 股, "
                f"价格 {payload['price']}, 费用 {payload['total_fees']}, "
                f"相对计划价 {payload['slippage_vs_plan_bps']} bps。"
            )
            if fill.next_open_price is None:
                lines.append("  - 缺少下一开盘价参考; 未伪造该项滑点。")
            else:
                lines.append(f"  - 相对下一开盘价 {payload['slippage_vs_next_open_bps']} bps。")
        lines.extend(
            [
                "",
                "## 风险、对账与健康",
                "",
                "- 风险事件: " + (", ".join(report.risk_events) or "无"),
                "- 对账: " + ("通过" if report.reconciliation_passed else "失败"),
                "- 对账阻断: " + (", ".join(report.reconciliation_blockers) or "无"),
                "- 健康阻断: " + (", ".join(report.health_blockers) or "无"),
                "",
            ]
        )
        return "\n".join(lines)

    @staticmethod
    def _publish(path: Path, content: bytes, *, report_id: str) -> str:
        digest = hashlib.sha256(content).hexdigest()
        if path.is_symlink():
            raise ReportError("report target must not be a symbolic link")
        if path.exists():
            if not path.is_file() or path.read_bytes() != content:
                raise ReportConflict("immutable report path already differs")
            return digest
        temporary = path.with_name(f".{path.name}.{report_id}.tmp")
        if temporary.exists() or temporary.is_symlink():
            raise ReportConflict("report temporary path already exists")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(temporary, flags, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        except BaseException:
            if temporary.exists() and not temporary.is_symlink():
                temporary.unlink()
            raise
        return digest

    def write(self, report: DailyReport, directory: Path) -> ReportWriteReceipt:
        root = Path(directory)
        if root.is_symlink() or not root.is_dir():
            raise ReportError("report directory is unavailable")
        json_content = self.render_json(report).encode("utf-8")
        markdown_content = self.render_markdown(report).encode("utf-8")
        basename = report.session.isoformat()
        json_sha256 = self._publish(
            root / f"{basename}.json",
            json_content,
            report_id=report.report_id,
        )
        markdown_sha256 = self._publish(
            root / f"{basename}.md",
            markdown_content,
            report_id=report.report_id,
        )
        return ReportWriteReceipt(
            report_id=report.report_id,
            session=report.session,
            json_sha256=json_sha256,
            markdown_sha256=markdown_sha256,
        )


class DatabaseDailyReportBuilder:
    """Build one report exclusively from validated, durable operational evidence."""

    def __init__(self, database: Database, *, clock: Callable[[], datetime]) -> None:
        if not isinstance(database, Database):
            raise TypeError("daily report builder requires Database")
        if not callable(clock):
            raise TypeError("daily report builder clock must be callable")
        self._database = database
        self._clock = clock

    @staticmethod
    def _decision_targets(
        row: sqlite3.Row | None,
    ) -> tuple[
        str | None,
        date | None,
        dict[str, Decimal],
        Decimal | None,
        dict[str, str],
        datetime | None,
    ]:
        if row is None:
            return None, None, {}, None, {}, None
        payload = _parse_json_object(row["payload_json"], label="decision payload")
        upstream = _json_object(payload.get("uquant_payload"), label="uquant decision payload")
        raw_targets = _json_array(payload.get("targets"), label="decision targets")
        targets: dict[str, Decimal] = {}
        for item in raw_targets:
            target = _json_object(item, label="decision target")
            raw_symbol = target.get("symbol")
            if not isinstance(raw_symbol, str):
                raise ReportError("decision target symbol is invalid")
            symbol = Symbol.parse(raw_symbol).canonical
            weight = _stored_decimal(target.get("weight"), label="decision target weight")
            if weight > 1 or symbol in targets:
                raise ReportError("decision targets are invalid")
            targets[symbol] = weight
        target_gross = _stored_decimal(
            upstream.get("target_gross"),
            label="decision target gross",
        )
        if target_gross > 1:
            raise ReportError("decision target gross exceeds one")
        pending = _json_array(payload.get("pending_orders"), label="decision pending orders")
        reasons: dict[str, str] = {}
        for item in pending:
            order = _json_object(item, label="decision pending order")
            order_id = order.get("order_id")
            reason = order.get("reason_code")
            if isinstance(order_id, str) and isinstance(reason, str):
                reasons[order_id] = reason
        strategy_session = date.fromisoformat(str(row["strategy_session"]))
        return (
            str(row["decision_id"]),
            strategy_session,
            targets,
            target_gross,
            reasons,
            _stored_time(row["created_at"], label="decision created_at"),
        )

    def _orders(
        self,
        *,
        decision_id: str | None,
        upstream_reasons: dict[str, str],
    ) -> tuple[OrderLifecycle, ...]:
        if decision_id is None:
            return ()
        rows = self._database.query_all(
            """
            SELECT i.execution_id, i.uquant_order_id, i.symbol, i.side,
                   i.requested_shares, i.filled_shares, i.state,
                   b.broker_order_id, b.status AS broker_status
            FROM execution_intents i
            LEFT JOIN broker_orders b ON b.execution_id = i.execution_id
            WHERE i.decision_id = ? ORDER BY i.created_at, i.execution_id
            """,
            (decision_id,),
        )
        event_rows = self._database.query_all(
            """
            SELECT aggregate_id, event_type, payload_json, recorded_at
            FROM domain_events
            WHERE aggregate_type = 'ORDER'
            ORDER BY recorded_at, domain_event_id
            """
        )
        event_reasons: dict[str, str] = {}
        for event in event_rows:
            payload = _parse_json_object(event["payload_json"], label="order domain event")
            fields = _json_object(payload.get("fields"), label="order domain event fields")
            raw_reason = fields.get("reason_code", fields.get("diagnostic_code"))
            if isinstance(raw_reason, str):
                event_reasons[str(event["aggregate_id"])] = raw_reason
        return tuple(
            OrderLifecycle(
                uquant_order_id=str(row["uquant_order_id"]),
                execution_id=str(row["execution_id"]),
                broker_order_id=(None if row["broker_order_id"] is None else str(row["broker_order_id"])),
                symbol=Symbol.parse(str(row["symbol"])).xtquant,
                side=Side(str(row["side"])),
                requested_shares=int(row["requested_shares"]),
                filled_shares=int(row["filled_shares"]),
                state=str(row["state"]),
                reason_code=event_reasons.get(
                    str(row["execution_id"]),
                    upstream_reasons.get(str(row["uquant_order_id"]), str(row["state"])),
                ),
                broker_status=_stored_broker_status(row["broker_status"]),
            )
            for row in rows
        )

    def _fills(self, *, decision_id: str | None, session: date) -> tuple[ExecutionFill, ...]:
        if decision_id is None:
            rows = self._database.query_all(
                """
                SELECT f.*, b.limit_price FROM fills f
                LEFT JOIN broker_orders b ON b.broker_order_id = f.broker_order_id
                WHERE f.session_date = ? ORDER BY f.event_time, f.broker_fill_id
                """,
                (session.isoformat(),),
            )
        else:
            rows = self._database.query_all(
                """
                SELECT f.*, b.limit_price FROM fills f
                LEFT JOIN broker_orders b ON b.broker_order_id = f.broker_order_id
                JOIN execution_intents i ON i.execution_id = f.execution_id
                WHERE i.decision_id = ? ORDER BY f.event_time, f.broker_fill_id
                """,
                (decision_id,),
            )
        return tuple(
            ExecutionFill(
                broker_fill_id=str(row["broker_fill_id"]),
                broker_order_id=str(row["broker_order_id"]),
                symbol=Symbol.parse(str(row["symbol"])).xtquant,
                side=Side(str(row["side"])),
                shares=int(row["shares"]),
                price=_stored_decimal(row["price"], label="fill price", positive=True),
                commission=_stored_decimal(row["commission"], label="fill commission"),
                stamp_duty=_stored_decimal(row["stamp_duty"], label="fill stamp duty"),
                transfer_fee=_stored_decimal(row["transfer_fee"], label="fill transfer fee"),
                planned_price=(
                    None
                    if row["limit_price"] is None
                    else _stored_decimal(
                        row["limit_price"],
                        label="fill planned price",
                        positive=True,
                    )
                ),
                next_open_price=None,
            )
            for row in rows
        )

    def _risk_codes(self, session: date) -> tuple[str, ...]:
        rows = self._database.query_all(
            "SELECT code, created_at FROM risk_events ORDER BY created_at, risk_event_id"
        )
        return tuple(
            sorted(
                {
                    str(row["code"])
                    for row in rows
                    if _stored_time(row["created_at"], label="risk event created_at")
                    .astimezone(_SHANGHAI)
                    .date()
                    == session
                }
            )
        )

    def build(self, session: date) -> DailyReport:
        if type(session) is not date:
            raise TypeError("daily report session must be a date")
        decision_row = self._database.query_one(
            """
            SELECT decision_id, strategy_session, payload_json, created_at
            FROM decision_snapshots WHERE strategy_session <= ?
            ORDER BY strategy_session DESC, created_at DESC, decision_id DESC LIMIT 1
            """,
            (session.isoformat(),),
        )
        (
            decision_id,
            strategy_session,
            targets,
            target_gross,
            upstream_reasons,
            decision_created_at,
        ) = self._decision_targets(decision_row)
        snapshot = self._database.query_one(
            """
            SELECT b.snapshot_id, b.captured_at, c.available_cash, c.total_assets
            FROM broker_snapshots b
            JOIN cash_snapshots c ON c.snapshot_id = b.snapshot_id
            WHERE b.session_date = ?
            ORDER BY b.captured_at DESC, b.snapshot_id DESC LIMIT 1
            """,
            (session.isoformat(),),
        )
        if snapshot is None:
            raise ReportError("complete broker cash snapshot is unavailable for report session")
        cash = _stored_decimal(snapshot["available_cash"], label="report available cash")
        assets = _stored_decimal(snapshot["total_assets"], label="report total assets")
        positions = self._database.query_all(
            "SELECT symbol, market_value FROM position_snapshots WHERE snapshot_id = ?",
            (snapshot["snapshot_id"],),
        )
        market_values = {
            Symbol.parse(str(row["symbol"])).canonical: _stored_decimal(
                row["market_value"],
                label="position market value",
            )
            for row in positions
        }
        if assets == 0 and any(value != 0 for value in market_values.values()):
            raise ReportError("positive positions contradict zero total assets")
        actual_weights = {
            symbol: (Decimal(0) if assets == 0 else _number(value / assets, label="actual weight"))
            for symbol, value in market_values.items()
        }
        differences = tuple(
            TargetActualDifference(
                symbol=Symbol.parse(symbol).xtquant,
                target_weight=targets.get(symbol, Decimal(0)),
                actual_weight=actual_weights.get(symbol, Decimal(0)),
            )
            for symbol in sorted(set(targets) | set(actual_weights))
        )
        actual_gross = (
            Decimal(0)
            if assets == 0
            else _number(
                sum(market_values.values(), Decimal(0)) / assets,
                label="actual gross",
            )
        )
        reconciliation = self._database.query_one(
            """
            SELECT passed, blockers_json, completed_at FROM reconciliation_runs
            WHERE strategy_session = ? AND kind = 'EOD'
            ORDER BY started_at DESC, reconciliation_id DESC LIMIT 1
            """,
            (session.isoformat(),),
        )
        reconciliation_blockers: tuple[str, ...]
        if reconciliation is None:
            reconciliation_passed = False
            reconciliation_blockers = ("RECONCILIATION_MISSING",)
            reconciliation_completed_at = None
        else:
            raw_blockers = json.loads(
                str(reconciliation["blockers_json"]),
                parse_constant=_reject_constant,
            )
            blockers = _json_string_array(raw_blockers, label="reconciliation blockers")
            observed_blockers = set(blockers)
            reconciliation_passed = reconciliation["passed"] == 1 and not observed_blockers
            if not reconciliation_passed and not observed_blockers:
                observed_blockers.add("RECONCILIATION_FAILED")
            if reconciliation["passed"] == 1 and observed_blockers:
                observed_blockers.add("RECONCILIATION_EVIDENCE_INVALID")
            reconciliation_blockers = tuple(sorted(observed_blockers))
            reconciliation_completed_at = (
                None
                if reconciliation["completed_at"] is None
                else _stored_time(
                    reconciliation["completed_at"],
                    label="reconciliation completed_at",
                )
            )
        runtime = self._database.query_one(
            "SELECT state, blockers_json, updated_at FROM runtime_state WHERE singleton_id = 1"
        )
        if runtime is None:
            runtime_state = "DISARMED"
            health_blockers: tuple[str, ...] = ()
            runtime_updated_at = None
        else:
            runtime_state = str(runtime["state"])
            raw_health = json.loads(
                str(runtime["blockers_json"]),
                parse_constant=_reject_constant,
            )
            health = _json_string_array(raw_health, label="runtime blockers")
            health_blockers = tuple(sorted(set(health)))
            runtime_updated_at = _stored_time(runtime["updated_at"], label="runtime updated_at")
        evidence_times = [
            _stored_time(snapshot["captured_at"], label="snapshot captured_at"),
            *(
                value
                for value in (
                    decision_created_at,
                    reconciliation_completed_at,
                    runtime_updated_at,
                )
                if value is not None
            ),
        ]
        generated_at = max(evidence_times)
        observed_clock = self._clock()
        _aware(observed_clock)
        if generated_at > observed_clock:
            raise ReportError("report evidence time is in the future")
        return DailyReport(
            session=session,
            strategy_session=strategy_session,
            generated_at=generated_at,
            decision_id=decision_id,
            available_cash=cash,
            total_assets=assets,
            actual_gross=actual_gross,
            target_gross=target_gross,
            target_actual_differences=differences,
            orders=self._orders(decision_id=decision_id, upstream_reasons=upstream_reasons),
            fills=self._fills(decision_id=decision_id, session=session),
            risk_events=self._risk_codes(session),
            reconciliation_passed=reconciliation_passed,
            reconciliation_blockers=reconciliation_blockers,
            runtime_state=runtime_state,
            health_blockers=health_blockers,
        )


__all__ = (
    "DailyReport",
    "DailyReportRenderer",
    "DatabaseDailyReportBuilder",
    "ExecutionFill",
    "OrderLifecycle",
    "ReportConflict",
    "ReportError",
    "ReportWriteReceipt",
    "TargetActualDifference",
)
