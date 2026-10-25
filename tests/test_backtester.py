import os
import sys
from datetime import datetime, timedelta

import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import crypto_backtester as cb  # noqa: E402


def make_df(rows):
    """rows: list of (high, low, close); one row per day"""
    start = datetime(2024, 1, 1)
    return pd.DataFrame([
        {"date": start + timedelta(days=i), "open": c, "high": h,
         "low": lo, "close": c, "volume": 1.0}
        for i, (h, lo, c) in enumerate(rows)
    ])


class FakeProvider:
    def __init__(self, df):
        self.df = df

    def get_historical_data(self, ticker, start, end):
        return self.df


def build(tps=None, zones=None, deposit=1000.0):
    zones = zones or [cb.Zone(100.0, 60.0)]
    tps = tps or [cb.TakeProfit("percent", 100.0)]
    portfolio = cb.Portfolio("t", deposit, 1)
    portfolio.add_asset(cb.Asset("BTC", zones, tps))
    return portfolio


def run(portfolio, df):
    engine = cb.BacktestEngine(portfolio, FakeProvider(df))
    engine.run_backtest()
    return engine


def test_zone_levels_five_equal_steps():
    z = cb.Zone(100.0, 60.0)
    assert z.levels == [100.0, 90.0, 80.0, 70.0, 60.0]


def test_allocation_split_across_zones():
    p = cb.Portfolio("t", 1000.0, 2)
    a = cb.Asset("BTC", [cb.Zone(100, 60), cb.Zone(50, 10)], [])
    assert p.add_asset(a)
    assert a.allocation_per_zone == 250.0
    assert p.free_cash == 500.0


def test_asset_limit_enforced():
    p = cb.Portfolio("t", 1000.0, 1)
    assert p.add_asset(cb.Asset("A", [cb.Zone(10, 5)], []))
    assert not p.add_asset(cb.Asset("B", [cb.Zone(10, 5)], []))


def test_buys_each_touched_level_once_at_20_percent():
    p = build()
    # day1 touches 100 and 90; day2 touches 80 again; level 90 must not rebuy
    df = make_df([(105, 89, 95), (95, 79, 85)])
    engine = run(p, df)
    buys = [t for t in engine.trade_logger.trades if t.action == "buy"]
    assert [t.price for t in buys] == [100.0, 90.0, 80.0]
    assert all(t.amount_usd == pytest.approx(1000.0 * 0.2) for t in buys)


def test_price_above_zone_buys_nothing():
    engine = run(build(), make_df([(120, 110, 115)] * 3))
    assert engine.trade_logger.trades == []


def test_percent_take_profit_sells_20_percent_at_target():
    p = build(zones=[cb.Zone(100.0, 100.0)])
    # all 5 levels equal 100 -> avg price 100, TP +100% = 200
    df = make_df([(101, 99, 100), (210, 150, 200)])
    engine = run(p, df)
    sells = [t for t in engine.trade_logger.trades if t.action == "sell"]
    assert len(sells) == 1
    assert sells[0].price == pytest.approx(200.0)
    bought = engine.positions["BTC"].total_quantity
    assert sells[0].quantity == pytest.approx(bought * 0.2)
    assert sells[0].level == 1


def test_take_profit_triggers_only_once():
    p = build(zones=[cb.Zone(100.0, 100.0)])
    df = make_df([(101, 99, 100), (210, 150, 200), (220, 150, 210)])
    engine = run(p, df)
    sells = [t for t in engine.trade_logger.trades if t.action == "sell"]
    assert len(sells) == 1


def test_stop_loss_breakeven_after_third_tp_closes_position():
    tps = [cb.TakeProfit("percent", v) for v in (10, 20, 30)]
    p = build(zones=[cb.Zone(100.0, 100.0)], tps=tps)
    df = make_df([
        (101, 99, 100),
        (140, 100, 135),   # TP1 110, TP2 120, TP3 130 all hit
        (120, 90, 95),     # close <= breakeven (100) -> stop out
    ])
    engine = run(p, df)
    trades = engine.trade_logger.trades
    sells = [t for t in trades if t.action == "sell"]
    assert [t.level for t in sells] == [1, 2, 3, 0]
    pos = engine.positions["BTC"]
    assert pos.stop_loss_active
    assert pos.get_current_quantity() == pytest.approx(0.0, abs=1e-9)


def test_trailing_tp_never_fires_fixed_price():
    e = cb.BacktestEngine(build(), FakeProvider(make_df([(1, 1, 1)])))
    assert e._calculate_tp_price(
        cb.TakeProfit("trailing"), 100.0, 500.0
    ) == float("inf")


def test_ath_tp_uses_at_least_400_percent():
    e = cb.BacktestEngine(build(), FakeProvider(make_df([(1, 1, 1)])))
    tp = cb.TakeProfit("ath")
    assert e._calculate_tp_price(tp, 100.0, 300.0) == 400.0
    assert e._calculate_tp_price(tp, 100.0, 900.0) == 900.0


def test_percent_tp_without_value_is_ignored():
    e = cb.BacktestEngine(build(), FakeProvider(make_df([(1, 1, 1)])))
    assert e._calculate_tp_price(cb.TakeProfit("percent"), 100.0, 1.0) == 0.0


def test_empty_data_raises():
    p = build()
    with pytest.raises(cb.DataUnavailableError):
        run(p, pd.DataFrame())


def test_main_returns_nonzero_when_data_unavailable(
    tmp_path, monkeypatch, capsys
):
    cfg = tmp_path / "cfg.json"
    cfg.write_text(
        '{"name": "x", "deposit": 100, "max_assets": 1, "assets": [{'
        '"ticker": "BTC", "zones": [{"price_from": 10, "price_to": 5}],'
        '"take_profits": [{"tp_type": "percent", "value": 10}]}]}',
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["bt", "--config", str(cfg)])
    monkeypatch.setattr(
        cb.BinanceAPI, "get_historical_data",
        lambda self, t, s, e: pd.DataFrame(),
    )
    assert cb.main() == 1
    assert not list((tmp_path / "results").glob("*.csv"))


def test_main_success_writes_csv_in_results(tmp_path, monkeypatch):
    cfg = tmp_path / "cfg.json"
    cfg.write_text(
        '{"name": "../evil", "deposit": 100, "max_assets": 1, "assets": [{'
        '"ticker": "BTC", "zones": [{"price_from": 100, "price_to": 100}],'
        '"take_profits": [{"tp_type": "percent", "value": 10}]}]}',
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["bt", "--config", str(cfg)])
    df = make_df([(101, 99, 100), (130, 100, 120)])
    monkeypatch.setattr(
        cb.BinanceAPI, "get_historical_data", lambda self, t, s, e: df
    )
    assert cb.main() == 0
    files = list((tmp_path / "results").glob("*.csv"))
    assert len(files) == 1
    assert ".." not in files[0].name and "/" not in files[0].name
    assert not (tmp_path / "evil").exists()


@pytest.mark.parametrize("name,expected", [
    ("demo_portfolio", "demo_portfolio"),
    ("../x", "_x"),
    ("a/b\c:d", "a_b_c_d"),
    ("...", "portfolio"),
    ("", "portfolio"),
])
def test_safe_filename(name, expected):
    assert cb.safe_filename(name) == expected


def test_config_missing_field_rejected():
    with pytest.raises(ValueError):
        cb.PortfolioFactory.from_config({"name": "x"})
