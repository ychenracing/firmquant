"""Deterministic causal next-open/OHLCV execution model for A-share replay."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_DOWN, ROUND_HALF_EVEN, Decimal
from enum import Enum

from firmquant.application.execution_evidence import BlockerCode

_ZERO = Decimal(0)
_ONE = Decimal(1)
_BPS = Decimal("10000")
_FEE_QUANTUM = Decimal("0.0001")
_LOT_SIZE = 100


class ReplaySide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


def _decimal(value: Decimal, *, label: str, positive: bool = False) -> None:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ValueError(f"{label} must be a finite Decimal")
    if value < 0 or (positive and value == 0):
        raise ValueError(f"{label} is outside its permitted bound")


def _shares(value: int, *, label: str, positive: bool = False) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0 or (positive and value == 0):
        raise ValueError(f"{label} must be a nonnegative integer share count")


def _money(value: Decimal) -> Decimal:
    return value.quantize(_FEE_QUANTUM, rounding=ROUND_HALF_EVEN)


def _text_decimal(value: Decimal) -> str:
    return format(value, "f")


@dataclass(frozen=True, slots=True)
class DailyBar:
    session: date
    symbol: str
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    previous_close: Decimal
    volume: int
    suspended: bool
    limit_up: Decimal
    limit_down: Decimal

    def __post_init__(self) -> None:
        if type(self.session) is not date:
            raise TypeError("bar session must be a calendar date")
        if not isinstance(self.symbol, str) or not self.symbol:
            raise ValueError("bar symbol must be non-empty text")
        for label, value in (
            ("open", self.open),
            ("high", self.high),
            ("low", self.low),
            ("close", self.close),
            ("previous close", self.previous_close),
            ("limit up", self.limit_up),
            ("limit down", self.limit_down),
        ):
            _decimal(value, label=label, positive=True)
        if self.low > self.high:
            raise ValueError("daily low cannot exceed daily high")
        if not self.low <= self.open <= self.high or not self.low <= self.close <= self.high:
            raise ValueError("open and close must lie inside the daily range")
        if not self.limit_down <= self.previous_close <= self.limit_up:
            raise ValueError("previous close must lie inside price limits")
        _shares(self.volume, label="daily volume")
        if not isinstance(self.suspended, bool):
            raise TypeError("suspended flag must be bool")
        if self.suspended and self.volume != 0:
            raise ValueError("suspended session must have zero volume")


@dataclass(frozen=True, slots=True)
class ReplayCosts:
    commission_rate: Decimal
    minimum_commission: Decimal
    sell_stamp_duty_rate: Decimal
    transfer_fee_rate: Decimal
    slippage_bps: Decimal
    max_price_deviation_bps: Decimal = Decimal("300")

    def __post_init__(self) -> None:
        for label, value in (
            ("commission rate", self.commission_rate),
            ("minimum commission", self.minimum_commission),
            ("sell stamp duty rate", self.sell_stamp_duty_rate),
            ("transfer fee rate", self.transfer_fee_rate),
            ("slippage bps", self.slippage_bps),
            ("maximum price deviation bps", self.max_price_deviation_bps),
        ):
            _decimal(value, label=label)
        if self.commission_rate > _ONE or self.sell_stamp_duty_rate > _ONE or self.transfer_fee_rate > _ONE:
            raise ValueError("fee rates must not exceed one")
        if self.slippage_bps > _BPS or self.max_price_deviation_bps > _BPS:
            raise ValueError("basis-point parameters exceed 100%")


@dataclass(frozen=True, slots=True)
class ReplayOrder:
    symbol: str
    side: ReplaySide
    shares: int
    limit_price: Decimal
    max_volume_participation: Decimal
    depends_on_sell_proceeds: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.symbol, str) or not self.symbol:
            raise ValueError("replay order symbol must be non-empty text")
        if not isinstance(self.side, ReplaySide):
            raise TypeError("replay order side must be typed")
        _shares(self.shares, label="replay order shares", positive=True)
        _decimal(self.limit_price, label="replay order limit price", positive=True)
        _decimal(self.max_volume_participation, label="volume participation", positive=True)
        if self.max_volume_participation > _ONE:
            raise ValueError("volume participation cannot exceed one")
        if not isinstance(self.depends_on_sell_proceeds, bool):
            raise TypeError("sell-proceeds dependency must be bool")
        if self.side is ReplaySide.SELL and self.depends_on_sell_proceeds:
            raise ValueError("SELL cannot depend on sell proceeds")


@dataclass(frozen=True, slots=True)
class ReplayAccount:
    cash: Decimal
    positions: dict[str, int]
    sellable: dict[str, int]

    def __post_init__(self) -> None:
        _decimal(self.cash, label="replay cash")
        if not isinstance(self.positions, dict) or not isinstance(self.sellable, dict):
            raise TypeError("replay positions and sellable shares must be dictionaries")
        for symbol, shares in self.positions.items():
            if not isinstance(symbol, str) or not symbol:
                raise ValueError("position symbol must be non-empty text")
            _shares(shares, label="position shares")
        for symbol, shares in self.sellable.items():
            _shares(shares, label="sellable shares")
            if shares > self.positions.get(symbol, 0):
                raise ValueError("sellable shares exceed total position")
        if any(shares == 0 for shares in self.positions.values()):
            raise ValueError("zero-share positions must be omitted")

    def roll_session(self) -> ReplayAccount:
        """Advance A-share T+1 eligibility without changing economics."""

        return ReplayAccount(
            cash=self.cash,
            positions=dict(self.positions),
            sellable=dict(self.positions),
        )


@dataclass(frozen=True, slots=True)
class ReplayOrderResult:
    symbol: str
    side: ReplaySide
    requested_shares: int
    authorized: bool
    filled_shares: int
    fill_price: Decimal | None
    blocker: BlockerCode | None
    commission: Decimal
    stamp_duty: Decimal
    transfer_fee: Decimal
    slippage_cost: Decimal
    unfilled_notional: Decimal
    unfilled_loss: Decimal

    def payload(self) -> dict[str, object]:
        return {
            "symbol": self.symbol,
            "side": self.side.value,
            "requested_shares": self.requested_shares,
            "authorized": self.authorized,
            "filled_shares": self.filled_shares,
            "unfilled_shares": self.requested_shares - self.filled_shares,
            "fill_price": None if self.fill_price is None else _text_decimal(self.fill_price),
            "blocker": None if self.blocker is None else self.blocker.value,
            "commission": _text_decimal(self.commission),
            "stamp_duty": _text_decimal(self.stamp_duty),
            "transfer_fee": _text_decimal(self.transfer_fee),
            "slippage_cost": _text_decimal(self.slippage_cost),
            "unfilled_notional": _text_decimal(self.unfilled_notional),
            "unfilled_loss": _text_decimal(self.unfilled_loss),
        }


@dataclass(frozen=True, slots=True)
class ReplaySessionResult:
    session: date
    orders: tuple[ReplayOrderResult, ...]
    ending_account: ReplayAccount
    commissions: Decimal
    stamp_duty: Decimal
    transfer_fees: Decimal
    slippage_cost: Decimal
    unfilled_notional: Decimal
    unfilled_loss: Decimal
    turnover_notional: Decimal
    partial_fill_count: int
    price_limit_blocks: int
    suspension_blocks: int
    incomplete_sell_blocked_buys: int

    def canonical_json(self) -> str:
        payload = {
            "schema": "firmquant.execution-replay-session.v1",
            "session": self.session.isoformat(),
            "orders": [item.payload() for item in self.orders],
            "ending_account": {
                "cash": _text_decimal(self.ending_account.cash),
                "positions": dict(sorted(self.ending_account.positions.items())),
                "sellable": dict(sorted(self.ending_account.sellable.items())),
            },
            "commissions": _text_decimal(self.commissions),
            "stamp_duty": _text_decimal(self.stamp_duty),
            "transfer_fees": _text_decimal(self.transfer_fees),
            "slippage_cost": _text_decimal(self.slippage_cost),
            "unfilled_notional": _text_decimal(self.unfilled_notional),
            "unfilled_loss": _text_decimal(self.unfilled_loss),
            "turnover_notional": _text_decimal(self.turnover_notional),
            "partial_fill_count": self.partial_fill_count,
            "price_limit_blocks": self.price_limit_blocks,
            "suspension_blocks": self.suspension_blocks,
            "incomplete_sell_blocked_buys": self.incomplete_sell_blocked_buys,
        }
        return json.dumps(
            payload,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )


def _volume_cap(bar: DailyBar, participation: Decimal, *, side: ReplaySide) -> int:
    raw = int((Decimal(bar.volume) * participation).to_integral_value(rounding=ROUND_DOWN))
    if side is ReplaySide.BUY:
        return raw - raw % _LOT_SIZE
    return raw


def _authorization_blocker(
    account: ReplayAccount,
    order: ReplayOrder,
    bar: DailyBar,
    costs: ReplayCosts,
) -> BlockerCode | None:
    """Use only information available at order authorization: state and session open."""

    if bar.suspended:
        return BlockerCode.NON_TRADABLE
    if order.side is ReplaySide.SELL and account.sellable.get(order.symbol, 0) <= 0:
        return BlockerCode.NON_TRADABLE
    if order.side is ReplaySide.BUY and bar.open >= bar.limit_up:
        return BlockerCode.PRICE_LIMIT
    if order.side is ReplaySide.SELL and bar.open <= bar.limit_down:
        return BlockerCode.PRICE_LIMIT
    deviation = abs(order.limit_price - bar.open) / bar.open * _BPS
    if deviation > costs.max_price_deviation_bps:
        return BlockerCode.STALE_QUOTE
    return None


def _candidate_fill_price(order: ReplayOrder, bar: DailyBar, costs: ReplayCosts) -> Decimal:
    slippage = costs.slippage_bps / _BPS
    if order.side is ReplaySide.BUY:
        nominal = bar.open if order.limit_price >= bar.open else order.limit_price
        return min(order.limit_price, nominal * (_ONE + slippage))
    nominal = bar.open if order.limit_price <= bar.open else order.limit_price
    return max(order.limit_price, nominal * (_ONE - slippage))


def _price_reached(order: ReplayOrder, bar: DailyBar, fill_price: Decimal) -> bool:
    """Use completed OHLCV only after causal authorization to decide whether the limit traded."""

    if order.side is ReplaySide.BUY:
        return bar.low <= fill_price <= bar.high and fill_price <= bar.limit_up
    return bar.low <= fill_price <= bar.high and fill_price >= bar.limit_down


def _fees(notional: Decimal, side: ReplaySide, costs: ReplayCosts) -> tuple[Decimal, Decimal, Decimal]:
    if notional <= 0:
        return _ZERO, _ZERO, _ZERO
    commission = _money(max(costs.minimum_commission, notional * costs.commission_rate))
    stamp = _money(notional * costs.sell_stamp_duty_rate) if side is ReplaySide.SELL else _ZERO
    transfer = _money(notional * costs.transfer_fee_rate)
    return commission, stamp, transfer


def _affordable_buy_shares(
    cash: Decimal,
    desired: int,
    fill_price: Decimal,
    costs: ReplayCosts,
) -> int:
    candidate = desired - desired % _LOT_SIZE
    while candidate > 0:
        notional = fill_price * Decimal(candidate)
        commission, _, transfer = _fees(notional, ReplaySide.BUY, costs)
        if notional + commission + transfer <= cash:
            return candidate
        candidate -= _LOT_SIZE
    return 0


def _unfilled_loss(order: ReplayOrder, bar: DailyBar, unfilled_shares: int) -> Decimal:
    if unfilled_shares <= 0:
        return _ZERO
    reference = bar.open
    if order.side is ReplaySide.BUY:
        opportunity = max(bar.close - reference, _ZERO)
    else:
        opportunity = max(reference - bar.close, _ZERO)
    return _money(opportunity * Decimal(unfilled_shares))


def _blocked_result(
    order: ReplayOrder,
    bar: DailyBar,
    *,
    blocker: BlockerCode,
    authorized: bool,
) -> ReplayOrderResult:
    return ReplayOrderResult(
        symbol=order.symbol,
        side=order.side,
        requested_shares=order.shares,
        authorized=authorized,
        filled_shares=0,
        fill_price=None,
        blocker=blocker,
        commission=_ZERO,
        stamp_duty=_ZERO,
        transfer_fee=_ZERO,
        slippage_cost=_ZERO,
        unfilled_notional=_money(bar.open * Decimal(order.shares)),
        unfilled_loss=_unfilled_loss(order, bar, order.shares),
    )


def execute_session(
    account: ReplayAccount,
    orders: tuple[ReplayOrder, ...],
    bars: dict[str, DailyBar],
    costs: ReplayCosts,
) -> ReplaySessionResult:
    """Execute sells before buys with no future information in authorization decisions."""

    if not isinstance(account, ReplayAccount):
        raise TypeError("execution replay requires ReplayAccount")
    if not isinstance(orders, tuple) or any(not isinstance(order, ReplayOrder) for order in orders):
        raise TypeError("execution replay requires a tuple of ReplayOrder")
    if not isinstance(bars, dict) or any(not isinstance(bar, DailyBar) for bar in bars.values()):
        raise TypeError("execution replay requires DailyBar mapping")
    if not isinstance(costs, ReplayCosts):
        raise TypeError("execution replay requires ReplayCosts")
    if not orders:
        raise ValueError("execution replay session requires at least one order")
    missing = {order.symbol for order in orders} - set(bars)
    if missing:
        raise ValueError("execution replay is missing daily bars")
    sessions = {bars[order.symbol].session for order in orders}
    if len(sessions) != 1:
        raise ValueError("all execution bars must belong to one session")
    session = next(iter(sessions))

    cash = account.cash
    positions = dict(account.positions)
    sellable = dict(account.sellable)
    ordered = tuple(sorted(orders, key=lambda item: 0 if item.side is ReplaySide.SELL else 1))
    results: list[ReplayOrderResult] = []
    incomplete_sell = False

    for order in ordered:
        bar = bars[order.symbol]
        if order.side is ReplaySide.BUY and order.depends_on_sell_proceeds and incomplete_sell:
            results.append(
                _blocked_result(
                    order,
                    bar,
                    blocker=BlockerCode.INCOMPLETE_SELL,
                    authorized=False,
                )
            )
            continue
        blocker = _authorization_blocker(
            ReplayAccount(cash=cash, positions=dict(positions), sellable=dict(sellable)),
            order,
            bar,
            costs,
        )
        if blocker is not None:
            results.append(_blocked_result(order, bar, blocker=blocker, authorized=False))
            if order.side is ReplaySide.SELL:
                incomplete_sell = True
            continue

        fill_price = _candidate_fill_price(order, bar, costs)
        if not _price_reached(order, bar, fill_price):
            result = _blocked_result(
                order,
                bar,
                blocker=BlockerCode.PRICE_LIMIT,
                authorized=True,
            )
            results.append(result)
            if order.side is ReplaySide.SELL:
                incomplete_sell = True
            continue

        cap = _volume_cap(bar, order.max_volume_participation, side=order.side)
        desired = min(order.shares, cap)
        blocker_after_fill: BlockerCode | None = None
        if order.side is ReplaySide.SELL:
            desired = min(desired, sellable.get(order.symbol, 0), positions.get(order.symbol, 0))
            if desired <= 0:
                results.append(
                    _blocked_result(
                        order,
                        bar,
                        blocker=BlockerCode.NON_TRADABLE,
                        authorized=True,
                    )
                )
                incomplete_sell = True
                continue
        else:
            affordable = _affordable_buy_shares(cash, desired, fill_price, costs)
            if affordable < desired:
                desired = affordable
                blocker_after_fill = BlockerCode.INSUFFICIENT_CASH

        if desired <= 0:
            blocker_zero = (
                BlockerCode.INSUFFICIENT_CASH
                if order.side is ReplaySide.BUY and cap > 0
                else BlockerCode.VOLUME_LIMIT
            )
            results.append(_blocked_result(order, bar, blocker=blocker_zero, authorized=True))
            if order.side is ReplaySide.SELL:
                incomplete_sell = True
            continue

        notional = fill_price * Decimal(desired)
        commission, stamp, transfer = _fees(notional, order.side, costs)
        if order.side is ReplaySide.SELL:
            proceeds = notional - commission - stamp - transfer
            cash += proceeds
            remaining = positions.get(order.symbol, 0) - desired
            remaining_sellable = sellable.get(order.symbol, 0) - desired
            if remaining > 0:
                positions[order.symbol] = remaining
                sellable[order.symbol] = max(remaining_sellable, 0)
            else:
                positions.pop(order.symbol, None)
                sellable.pop(order.symbol, None)
        else:
            cash -= notional + commission + transfer
            positions[order.symbol] = positions.get(order.symbol, 0) + desired
            # Newly purchased shares remain absent from sellable until roll_session().

        if desired < order.shares and blocker_after_fill is None:
            blocker_after_fill = BlockerCode.VOLUME_LIMIT
        if order.side is ReplaySide.SELL and desired < order.shares:
            incomplete_sell = True
        slippage_cost = _money(abs(fill_price - bar.open) * Decimal(desired))
        unfilled_shares = order.shares - desired
        results.append(
            ReplayOrderResult(
                symbol=order.symbol,
                side=order.side,
                requested_shares=order.shares,
                authorized=True,
                filled_shares=desired,
                fill_price=fill_price,
                blocker=blocker_after_fill,
                commission=commission,
                stamp_duty=stamp,
                transfer_fee=transfer,
                slippage_cost=slippage_cost,
                unfilled_notional=_money(bar.open * Decimal(unfilled_shares)),
                unfilled_loss=_unfilled_loss(order, bar, unfilled_shares),
            )
        )

    ending = ReplayAccount(cash=_money(cash), positions=positions, sellable=sellable)
    commissions = sum((item.commission for item in results), start=_ZERO)
    stamp_duty = sum((item.stamp_duty for item in results), start=_ZERO)
    transfer_fees = sum((item.transfer_fee for item in results), start=_ZERO)
    slippage_cost = sum((item.slippage_cost for item in results), start=_ZERO)
    unfilled_notional = sum((item.unfilled_notional for item in results), start=_ZERO)
    unfilled_loss = sum((item.unfilled_loss for item in results), start=_ZERO)
    turnover_notional = sum(
        (
            _ZERO if item.fill_price is None else item.fill_price * Decimal(item.filled_shares)
            for item in results
        ),
        start=_ZERO,
    )
    return ReplaySessionResult(
        session=session,
        orders=tuple(results),
        ending_account=ending,
        commissions=_money(commissions),
        stamp_duty=_money(stamp_duty),
        transfer_fees=_money(transfer_fees),
        slippage_cost=_money(slippage_cost),
        unfilled_notional=_money(unfilled_notional),
        unfilled_loss=_money(unfilled_loss),
        turnover_notional=_money(turnover_notional),
        partial_fill_count=sum(0 < item.filled_shares < item.requested_shares for item in results),
        price_limit_blocks=sum(item.blocker is BlockerCode.PRICE_LIMIT for item in results),
        suspension_blocks=sum(
            item.blocker is BlockerCode.NON_TRADABLE and bars[item.symbol].suspended for item in results
        ),
        incomplete_sell_blocked_buys=sum(item.blocker is BlockerCode.INCOMPLETE_SELL for item in results),
    )


__all__ = (
    "DailyBar",
    "ReplayAccount",
    "ReplayCosts",
    "ReplayOrder",
    "ReplayOrderResult",
    "ReplaySessionResult",
    "ReplaySide",
    "execute_session",
)
