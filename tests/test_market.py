import math

import pytest

from reltrade.market import (
    MarketError,
    StationMarket,
    buy_cost_from_quote,
    sell_revenue_from_quote,
)
from reltrade.world import ENERGY, EqSchedule, MarketSpec, Rules, StationSpec

pytestmark = pytest.mark.unit

FLAT = EqSchedule((0.0,), (1.0,))


def make_market(rules: Rules | None = None) -> StationMarket:
    spec = StationSpec(
        "Test",
        (0.0, 0.0, 0.0),
        {
            "ore": MarketSpec(10.0, 200.0),
            "med": MarketSpec(30.0, 100.0),
            ENERGY: MarketSpec(5.0, 1000.0),
        },
        "test station",
    )
    return StationMarket(spec, {g: FLAT for g in spec.markets}, rules or Rules())


def test_prices_follow_stock() -> None:
    m = make_market()
    q = m.board_at(0.0).quotes["ore"]
    assert (q.bid + q.ask) / 2 == pytest.approx(10.0)
    assert q.ask / q.bid == pytest.approx(1.02 / 0.98)
    m.buy(0.0, "ore", 100.0)  # 库存 200 → 100，价格翻倍
    q2 = m.board_at(0.0).quotes["ore"]
    assert (q2.bid + q2.ask) / 2 == pytest.approx(20.0)
    assert m.board_at(0.0).quotes[ENERGY].bid is None


def test_trades_are_path_independent() -> None:
    a, b = make_market(), make_market()
    whole = a.buy(1.0, "ore", 120.0)
    split = b.buy(1.0, "ore", 50.0) + b.buy(1.0, "ore", 70.0)
    assert whole == pytest.approx(split)
    assert a.sell(1.0, "ore", 30.0) == pytest.approx(
        b.sell(1.0, "ore", 10.0) + b.sell(1.0, "ore", 20.0)
    )


def test_round_trip_loses_the_spread() -> None:
    m = make_market()
    cost = m.buy(0.0, "ore", 50.0)
    revenue = m.sell(0.0, "ore", 50.0)
    assert revenue < cost
    assert revenue / cost == pytest.approx(0.98 / 1.02)


def test_buy_cap_and_energy_cannot_be_sold() -> None:
    m = make_market()
    with pytest.raises(MarketError):
        m.quote_buy(0.0, "ore", 0.95 * 200.0)
    with pytest.raises(MarketError):
        m.sell(0.0, ENERGY, 1.0)


def test_stock_relaxes_toward_normal() -> None:
    rules = Rules()
    m = make_market(rules)
    m.buy(0.0, "ore", 100.0)  # 200 → 100
    s = m.inventory_at(rules.relax_time)["ore"]
    assert s == pytest.approx(200.0 - 100.0 / math.e)
    assert m.inventory_at(50.0)["ore"] == pytest.approx(200.0, abs=1e-6)


def test_history_is_immutable_for_retarded_queries() -> None:
    m = make_market()
    before = m.board_at(3.0)
    m.buy(5.0, "med", 40.0)
    assert m.board_at(3.0) == before  # 远方观察者按推迟时间看到的仍是旧状态
    assert m.board_at(5.0).quotes["med"].stock == pytest.approx(60.0)
    assert m.board_at(-4.0).quotes == m.board_at(0.0).quotes  # 开局前视为初始状态
    with pytest.raises(RuntimeError):
        m.buy(4.0, "ore", 1.0)


def test_public_quote_formulas_match_execution() -> None:
    m = make_market()
    m.sell(2.0, "ore", 30.0)
    q = m.board_at(2.5).quotes["ore"]
    assert buy_cost_from_quote(q, 40.0) == pytest.approx(m.quote_buy(2.5, "ore", 40.0))
    assert sell_revenue_from_quote(q, 40.0) == pytest.approx(m.quote_sell(2.5, "ore", 40.0))
    e = m.board_at(2.5).quotes[ENERGY]
    assert buy_cost_from_quote(e, 25.0) == pytest.approx(m.quote_buy(2.5, ENERGY, 25.0))


def test_trades_at_time_zero_are_invisible_to_negative_retarded_times() -> None:
    m = make_market()
    initial = m.board_at(-1.0).quotes
    m.buy(0.0, "ore", 50.0)
    assert m.board_at(-3.0).quotes == initial  # 远方在 t=0 看到的是开局前的状态
    assert m.board_at(0.0).quotes["ore"].stock == pytest.approx(150.0)


def test_market_rebuilt_from_snapshots_quotes_the_same() -> None:
    m = make_market()
    m.buy(1.0, "ore", 50.0)
    m.sell(3.5, "med", 20.0)
    m.buy(3.5, "ore", 10.0)
    snaps = [(t, dict(inv)) for t, inv in m.snapshots()]
    r = StationMarket.from_snapshots(m.spec, m._sched, m.rules, snaps)
    for t in (-1.0, 0.0, 0.99, 1.0, 2.0, 3.5, 7.0):
        assert r.board_at(t) == m.board_at(t)
    with pytest.raises(ValueError, match="no snapshots"):
        StationMarket.from_snapshots(m.spec, m._sched, m.rules, [])
