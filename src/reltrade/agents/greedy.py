"""贪心基线商人。

每次停靠：卖光货 → 用光锥内（可能过时的）报价，枚举 (商品, 目的地, 速度, 数量)，
取"利润 / 坐标时"最高的单一商品航程；没有正利润就空船换位到预期最好的星站，再不行原地等。
它把看到的远方报价当成到港时的真实价格：不外推、不考虑对手、不读新闻和消息。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from reltrade.engine import DecisionContext
from reltrade.market import Quote, buy_cost_from_quote, sell_revenue_from_quote
from reltrade.world import ENERGY

log = logging.getLogger("reltrade.greedy")

REL_SPEEDS = (0.3, 0.5, 0.7, 0.8, 0.9, 0.95, 0.98, 0.99, 0.995, 0.999)
NEWTON_SPEEDS = (0.3, 0.5, 0.7, 1.0, 1.5, 2.0, 3.0, 4.0, 6.0, 8.0)


@dataclass(frozen=True)
class Plan:
    good: str | None
    qty: float
    dest: str
    speed: float
    profit: float
    dt: float

    @property
    def rate(self) -> float:
        return self.profit / self.dt


class GreedyMerchant:
    def __init__(
        self,
        speeds: tuple[float, ...] = REL_SPEEDS,
        fractions: tuple[float, ...] = (0.25, 0.5, 0.75, 1.0),
    ) -> None:
        self.speeds = speeds
        self.fractions = fractions

    async def decide(self, ctx: DecisionContext) -> None:
        obs = ctx.observe()
        for g, q in obs.cargo.items():
            ctx.sell(g, q)
        local = ctx.local_board().quotes
        views = {v.station: v.board.quotes for v in ctx.remote_views()}
        best = self._best_from(ctx, ctx.station, local, views, ctx.m.cash, ctx.t)
        if best is not None:
            assert best.good is not None
            ctx.buy(best.good, best.qty)
            ctx.depart(
                best.dest,
                best.speed,
                plan=f"greedy: {best.qty:.1f} {best.good} to {best.dest}, "
                f"est. profit {best.profit:.0f} ({best.rate:.1f}/yr)",
            )
            return
        move = self._best_reposition(ctx, local, views)
        if move is not None:
            ctx.depart(move.dest, move.speed, plan=f"greedy: reposition to {move.dest}")
            return
        ctx.wait(2.0, "greedy: nothing profitable in sight")

    # ---- 规划 ----
    def _travel(
        self, ctx: DecisionContext, origin: str, dest: str, speed: float, mass: float, energy: Quote
    ) -> tuple[float, float, float] | None:
        """(坐标时, 船时, 能源+工资)；供不起能源返回 None。"""
        leg = ctx.engine.kin.leg(ctx.world.distance(origin, dest), speed)
        units = leg.energy_per_mass * mass
        if units > ctx.world.rules.max_trade_fraction * energy.stock:
            return None
        return (
            leg.dt,
            leg.dtau,
            buy_cost_from_quote(energy, units) + ctx.world.rules.wage * leg.dtau,
        )

    def _best_from(
        self,
        ctx: DecisionContext,
        origin: str,
        here: dict[str, Quote],
        views: dict[str, dict[str, Quote]],
        cash: float,
        t0: float,
    ) -> Plan | None:
        w, r = ctx.world, ctx.world.rules
        best: Plan | None = None
        for dest, there in views.items():
            if dest == origin:
                continue
            for g, good in w.goods.items():
                ask, bid = here[g], there[g]
                if bid.bid is None or bid.bid <= ask.ask:
                    continue
                q_cap = min(r.hold_capacity / good.mass, r.max_trade_fraction * ask.stock)
                for speed in self.speeds:
                    base = self._travel(ctx, origin, dest, speed, r.hull_mass, here[ENERGY])
                    if base is None or t0 + base[0] > r.t_end or base[2] >= cash:
                        continue
                    lo, hi = 0.0, q_cap  # 二分求预算内最大数量（总成本随数量单调增）
                    for _ in range(40):
                        mid = (lo + hi) / 2
                        tr = self._travel(
                            ctx, origin, dest, speed, r.hull_mass + mid * good.mass, here[ENERGY]
                        )
                        if tr is not None and buy_cost_from_quote(ask, mid) + tr[2] <= cash:
                            lo = mid
                        else:
                            hi = mid
                    for f in self.fractions:
                        q = f * lo
                        if q < 1e-3:
                            continue
                        tr = self._travel(
                            ctx, origin, dest, speed, r.hull_mass + q * good.mass, here[ENERGY]
                        )
                        if tr is None:
                            continue
                        dt, dtau, travel = tr
                        survive = 0.5 ** (dtau / good.half_life) if good.half_life else 1.0
                        profit = (
                            sell_revenue_from_quote(bid, q * survive)
                            - buy_cost_from_quote(ask, q)
                            - travel
                        )
                        if profit > 0 and (best is None or profit / dt > best.rate):
                            best = Plan(g, q, dest, speed, profit, dt)
        return best

    def _best_reposition(
        self, ctx: DecisionContext, here: dict[str, Quote], views: dict[str, dict[str, Quote]]
    ) -> Plan | None:
        """空船去 j，再从 j 做一笔（全用本站看到的报价估计）；按总利润 / 总时间排序。"""
        r = ctx.world.rules
        best: Plan | None = None
        for j, there in views.items():
            legs = [
                (s, self._travel(ctx, ctx.station, j, s, r.hull_mass, here[ENERGY]))
                for s in self.speeds
            ]
            legs = [(s, tr) for s, tr in legs if tr is not None and tr[2] < ctx.m.cash]
            if not legs:
                continue
            onward = {k: v for k, v in views.items() if k != j}
            onward[ctx.station] = here
            # 后续一笔与换位速度基本无关：用最贵换位后的余钱、最慢到达时间保守估计一次
            worst_cost = max(tr[2] for _, tr in legs)
            latest = max(tr[0] for _, tr in legs)
            nxt = self._best_from(ctx, j, there, onward, ctx.m.cash - worst_cost, ctx.t + latest)
            if nxt is None:
                continue
            for s, (dt, _, cost) in legs:
                total = Plan(None, 0.0, j, s, nxt.profit - cost, dt + nxt.dt)
                if total.profit > 0 and (best is None or total.rate > best.rate):
                    best = total
        return best
