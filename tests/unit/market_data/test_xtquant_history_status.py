from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from firmquant.domain.broker_facts import InstrumentFact, SecurityStatus, SecurityType
from firmquant.domain.values import Price, Shares, Symbol
from firmquant.market_data.xtquant_daily import InstrumentSessionState
from firmquant.market_data.xtquant_history import OfficialXtQuantDailyHistoryProvider


class Frame:
    def reset_index(self):
        return self

    def to_dict(self, orient: str):
        assert orient == "records"
        return [
            {
                "time": "20260824",
                "open": 10,
                "high": 10,
                "low": 10,
                "close": 10,
                "volume": 100,
                "amount": 1000,
            }
        ]


class XtData:
    def download_history_data(self, **_kwargs: object) -> None:
        return None

    def get_market_data_ex(self, **kwargs: object):
        stock_list = kwargs["stock_list"]
        assert isinstance(stock_list, list)
        return {stock_list[0]: Frame()}


def suspended(symbol: Symbol) -> InstrumentFact:
    return InstrumentFact(
        symbol=symbol,
        security_type=SecurityType.EQUITY,
        status=SecurityStatus.SUSPENDED,
        trading_unit=Shares(100),
        price_tick=Price(Decimal("0.01")),
        price_precision=2,
        lower_limit=None,
        upper_limit=None,
        session_date=date(2026, 8, 25),
        observed_at=datetime(2026, 8, 25, 7, 10, tzinfo=UTC),
    )


def test_official_provider_keeps_real_last_bar_and_exposes_authoritative_suspension() -> None:
    provider = OfficialXtQuantDailyHistoryProvider(
        xtdata=XtData(),
        volume_multipliers={"SH": 1, "SZ": 1, "BJ": 1},
        instrument_lookup=suspended,
    )

    bars = provider.fetch(("sz300308",), through=date(2026, 8, 25))["sz300308"]
    status = provider.fetch_status(("sz300308",), session=date(2026, 8, 25))["sz300308"]

    assert bars[-1].session == date(2026, 8, 24)
    assert status.state is InstrumentSessionState.SUSPENDED
    assert len(status.evidence_sha256) == 64
