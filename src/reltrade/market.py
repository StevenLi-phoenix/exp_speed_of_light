"""星站市场：库存驱动价格，价格 × 库存 = 常数（K = p_eq × 正常库存）。

- 中间价 mid(S) = K / S；ask = mid·(1+h/2)，bid = mid·(1−h/2)，h 为买卖价差。
- 大单沿价格曲线成交，按积分计价：买 q 花 (1+h/2)·K·ln(S/(S−q))，卖 q 得 (1−h/2)·K·ln(1+q/S)。
  ln 可加，所以拆单和一次成交价格相同（与路径无关）。
- 交易之间，库存以 relax_time 为时间常数指数回归到"正常库存 × 外生倍率"（生产与消费的合效果）。
- 每次成交后追加一个快照，历史永不改写；远方观察者按推迟时间（t − d/c）查询快照。
"""

from __future__ import annotations

import math
from bisect import bisect_right
from dataclasses import dataclass

from reltrade.world import ENERGY, EqSchedule, Rules, StationSpec


class MarketError(ValueError):
    """不合法的市场操作（库存不足等）。"""


@dataclass(frozen=True)
class Quote:
    bid: float | None  # 能源只卖不收，bid 为 None
    ask: float
    stock: float
    normal: float


@dataclass(frozen=True)
class Board:
    asof: float
    quotes: dict[str, Quote]


def buy_cost(k: float, stock: float, q: float, spread: float) -> float:
    return (1 + spread / 2) * k * math.log(stock / (stock - q))


def sell_revenue(k: float, stock: float, q: float, spread: float) -> float:
    return (1 - spread / 2) * k * math.log1p(q / stock)


def buy_cost_from_quote(quote: Quote, q: float) -> float:
    """只用公开报价算买入成本：ask × S = (1+h/2)·K，所以 ask·S·ln(S/(S−q)) 是精确值。"""
    return quote.ask * quote.stock * math.log(quote.stock / (quote.stock - q))


def sell_revenue_from_quote(quote: Quote, q: float) -> float:
    """只用公开报价算卖出收入：bid·S·ln(1 + q/S)，同样精确。"""
    assert quote.bid is not None
    return quote.bid * quote.stock * math.log1p(q / quote.stock)


class StationMarket:
    def __init__(self, spec: StationSpec, schedules: dict[str, EqSchedule], rules: Rules) -> None:
        self.spec = spec
        self.rules = rules
        self._sched = schedules
        self._k = {g: m.p_eq * m.depth for g, m in spec.markets.items()}
        init = {g: m.depth * schedules[g].mult_at(0.0) for g, m in spec.markets.items()}
        self._times: list[float] = [0.0]
        self._invs: list[dict[str, float]] = [init]

    # ---- 状态查询 ----
    def inventory_at(self, t: float) -> dict[str, float]:
        if t < 0.0:
            # 开局前的历史视为初始状态；不能夹到 t=0，否则 t=0 的成交会瞬间被任意远处看到
            return dict(self._invs[0])
        i = bisect_right(self._times, t) - 1
        t0, inv0 = self._times[i], self._invs[i]
        return {g: self._evolve(g, s, t0, t) for g, s in inv0.items()}

    def _evolve(self, good: str, stock: float, t0: float, t1: float) -> float:
        depth = self.spec.markets[good].depth
        tau = self.rules.relax_time
        for a, b, mult in self._sched[good].segments(t0, t1):
            eq = depth * mult
            stock = eq + (stock - eq) * math.exp(-(b - a) / tau)
        return stock

    def board_at(self, t: float) -> Board:
        h = self.rules.spread
        inv = self.inventory_at(t)
        quotes = {}
        for g, s in inv.items():
            mid = self._k[g] / s
            bid = None if g == ENERGY else mid * (1 - h / 2)
            quotes[g] = Quote(bid, mid * (1 + h / 2), s, self.spec.markets[g].depth)
        return Board(t, quotes)

    def max_buy(self, t: float, good: str) -> float:
        return self.rules.max_trade_fraction * self.inventory_at(t)[good]

    def quote_buy(self, t: float, good: str, q: float) -> float:
        """买 q 单位的总价（不改变状态）；超过可买上限抛 MarketError。"""
        stock = self.inventory_at(t)[good]
        if q > self.rules.max_trade_fraction * stock + 1e-9:
            raise MarketError(
                f"{self.spec.name} can supply at most {self.rules.max_trade_fraction * stock:.1f} "
                f"{good} right now (stock {stock:.1f})")
        return buy_cost(self._k[good], stock, q, self.rules.spread)

    def quote_sell(self, t: float, good: str, q: float) -> float:
        stock = self.inventory_at(t)[good]
        return sell_revenue(self._k[good], stock, q, self.rules.spread)

    # ---- 成交（追加快照） ----
    def buy(self, t: float, good: str, q: float) -> float:
        cost = self.quote_buy(t, good, q)
        self._apply(t, good, -q)
        return cost

    def sell(self, t: float, good: str, q: float) -> float:
        if good == ENERGY:
            raise MarketError("stations do not buy energy")
        revenue = self.quote_sell(t, good, q)
        self._apply(t, good, +q)
        return revenue

    def _apply(self, t: float, good: str, delta: float) -> None:
        if t < self._times[-1] - 1e-9:
            raise RuntimeError(
                f"{self.spec.name}: trade at t={t} before last snapshot {self._times[-1]}")
        inv = self.inventory_at(t)
        inv[good] += delta
        self._times.append(t)
        self._invs.append(inv)

    def snapshots(self) -> list[tuple[float, dict[str, float]]]:
        return list(zip(self._times, self._invs, strict=True))
