from __future__ import annotations

from datetime import date

from firmquant.market_data.xtquant_history import OfficialXtQuantDailyHistoryProvider


class Frame:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.rows = rows

    def reset_index(self):
        return self

    def to_dict(self, orient: str):
        assert orient == "records"
        return self.rows


class XtData:
    def __init__(self) -> None:
        self.downloads: list[tuple[str, str, str]] = []
        self.requests: list[dict[str, object]] = []

    def download_history_data(
        self,
        stock_code: str,
        period: str,
        start_time: str,
        end_time: str,
        incrementally: bool,
    ) -> None:
        assert period == "1d"
        assert incrementally is True
        self.downloads.append((stock_code, start_time, end_time))

    def get_market_data_ex(self, **kwargs):
        self.requests.append(kwargs)
        code = kwargs["stock_list"][0]
        return {
            code: Frame(
                [
                    {
                        "time": "20260824",
                        "open": 10,
                        "high": 11,
                        "low": 9,
                        "close": 10.5,
                        "volume": 100,
                        "amount": 105000,
                    },
                    {
                        "time": "20260825",
                        "open": 10.5,
                        "high": 12,
                        "low": 10,
                        "close": 11,
                        "volume": 120,
                        "amount": 132000,
                    },
                ]
            )
        }


def test_official_provider_uses_reviewed_volume_multiplier_and_front_adjustment() -> None:
    xtdata = XtData()
    provider = OfficialXtQuantDailyHistoryProvider(
        xtdata=xtdata,
        volume_multipliers={"SH": 100, "SZ": 100, "BJ": 100},
    )

    result = provider.fetch(("sh600519",), through=date(2026, 8, 25))

    bars = result["sh600519"]
    assert bars[-1].volume == 12_000
    assert bars[-1].session == date(2026, 8, 25)
    assert xtdata.downloads == [("600519.SH", "", "20260825")]
    assert xtdata.requests[0]["dividend_type"] == "front"
    assert xtdata.requests[0]["period"] == "1d"


def test_official_provider_uses_raw_adjustment_for_strategy_indices() -> None:
    xtdata = XtData()
    provider = OfficialXtQuantDailyHistoryProvider(
        xtdata=xtdata,
        volume_multipliers={"SH": 1, "SZ": 1, "BJ": 1},
    )

    provider.fetch(("sh000300",), through=date(2026, 8, 25))

    assert xtdata.requests[0]["dividend_type"] == "none"
