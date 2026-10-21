import asyncio
import json
import random

import pytest

from reltrade.agents.greedy import NEWTON_SPEEDS, REL_SPEEDS, GreedyMerchant
from reltrade.engine import ActionError, DecisionContext, Engine
from reltrade.events import reaches
from reltrade.physics import INSTANT, LIGHT, Newtonian, Relativistic
from reltrade.world import Rules, make_world

pytestmark = pytest.mark.integration


class Scripted:
    """按 (商人第几次决策) 执行预设动作的测试商人。"""

    def __init__(self, script: dict[int, object]) -> None:
        self.script = script
        self.obs = []
        self.n = 0

    async def decide(self, ctx: DecisionContext) -> None:
        self.n += 1
        self.obs.append(ctx.observe())
        step = self.script.get(self.n)
        if callable(step):
            step(ctx)
        else:
            ctx.wait(5.0, "idle")


def who_at(station: str, seed: int = 7) -> str:
    return next(m for m, s in make_world(seed).starts.items() if s == station)


def engine_with(
    script_for: dict[str, dict[int, object]],
    *,
    c_info: float = LIGHT,
    t_end: float = 40.0,
    seed: int = 7,
) -> tuple[Engine, dict[str, Scripted]]:
    w = make_world(seed, Rules(t_end=t_end))
    agents = {n: Scripted(script_for.get(n, {})) for n in w.merchant_names}
    return Engine(w, agents, c_info=c_info, kin=Relativistic()), agents


def test_reaches_is_inclusive_light_cone() -> None:
    assert reaches(5.0, LIGHT, 1.0, 6.0)
    assert not reaches(5.0, LIGHT, 1.0, 5.999)
    assert reaches(5.0, INSTANT, 1.0, 1.0)
    assert not reaches(0.0, LIGHT, 2.0, 1.0)


def test_departure_is_seen_only_after_light_arrives() -> None:
    # Wolf 359 的商人出发去 Sol；Ross 128（3.90 ly 外）的商人每 0.5 年看一次
    a, b = who_at("Wolf 359"), who_at("Ross 128")
    d = make_world(7).distance("Wolf 359", "Ross 128")

    def go(ctx: DecisionContext) -> None:
        ctx.depart("Sol", 0.9, "test")

    def poll(ctx: DecisionContext) -> None:
        ctx.wait(0.5, "poll")

    eng, agents = engine_with({a: {1: go}, b: {k: poll for k in range(1, 30)}})
    asyncio.run(eng.run())
    seen_at = [
        o.t for o in agents[b].obs if any(e.kind == "depart" and e.actor == a for e in o.events)
    ]
    assert seen_at, "the departure should eventually be seen"
    assert seen_at[0] >= d and seen_at[0] - d < 0.5 + 1e-9


def test_remote_board_is_the_retarded_snapshot() -> None:
    def buy_lots(ctx: DecisionContext) -> None:
        ctx.buy("ore", 120)
        ctx.wait(10.0, "hold")

    a, b = who_at("Wolf 359"), who_at("Ross 128")
    eng, agents = engine_with({a: {1: buy_lots}})  # Wolf 359 的商人大量买矿
    asyncio.run(eng.run())
    d = eng.world.distance("Wolf 359", "Ross 128")
    for o in agents[b].obs:
        view = next(v for v in o.remote if v.station == "Wolf 359")
        assert view.age == pytest.approx(d)
        assert view.board.quotes == eng.markets["Wolf 359"].board_at(o.t - d).quotes


def test_action_validation() -> None:
    errors: list[str] = []

    def bad(ctx: DecisionContext) -> None:
        for f in (
            lambda: ctx.buy("ore", 1e6),
            lambda: ctx.buy("gold", 1),
            lambda: ctx.buy("tech", -3),
            lambda: ctx.sell("med", 1),
            lambda: ctx.depart("Nowhere", 0.5),
            lambda: ctx.depart(ctx.station, 0.5),
            lambda: ctx.depart("Sol", 1.0),
            lambda: ctx.wait(100),
            lambda: ctx.send_message("Zed", "hi"),
            lambda: ctx.send_message(ctx.m.name, "me"),
        ):
            try:
                f()
            except ActionError as e:
                errors.append(str(e))
        ctx.wait(1.0, "done")
        try:
            ctx.buy("ore", 1)
        except ActionError as e:
            errors.append(str(e))

    eng, _ = engine_with({who_at("Sol"): {1: bad}}, t_end=5.0)
    asyncio.run(eng.run())
    assert len(errors) == 11
    assert "turn already ended" in errors[-1]


def test_spoilage_and_wages_follow_ship_time() -> None:
    w = make_world(7)
    leg = Relativistic().leg(w.distance("Sol", "Alpha Centauri"), 0.99)
    state: dict[str, float] = {}

    def load(ctx: DecisionContext) -> None:
        ctx.buy("med", 30)
        state["cash_before"] = ctx.m.cash
        res = ctx.depart("Alpha Centauri", 0.99, "test")
        state["paid"] = res["energy_cost"] + res["wages"]
        state["wages"] = res["wages"]

    def check(ctx: DecisionContext) -> None:
        state["med_after"] = ctx.m.cargo["med"]
        state["tau"] = ctx.m.tau
        ctx.wait(2.0, "rest")

    def after_wait(ctx: DecisionContext) -> None:
        state["cash_after_wait"] = ctx.m.cash
        state["tau2"] = ctx.m.tau
        ctx.wait(5.0, "rest")

    eng, _ = engine_with({who_at("Sol"): {1: load, 2: check, 3: after_wait}})
    asyncio.run(eng.run())
    assert state["wages"] == pytest.approx(w.rules.wage * leg.dtau)
    assert state["med_after"] == pytest.approx(30 * 0.5 ** (leg.dtau / 3.0))
    assert state["tau"] == pytest.approx(leg.dtau)
    assert state["tau2"] == pytest.approx(leg.dtau + 2.0)  # 停靠时 dτ = dt
    cash_on_arrival = state["cash_before"] - state["paid"]
    assert state["cash_after_wait"] == pytest.approx(cash_on_arrival - 2.0 * w.rules.wage)


def test_final_valuation_counts_cargo_in_flight() -> None:
    def late(ctx: DecisionContext) -> None:
        ctx.buy("tech", 20)
        ctx.depart("Wolf 359", 0.5, "arrives after the end")

    eng, _ = engine_with({who_at("Sol"): {1: late}}, t_end=5.0)
    asyncio.run(eng.run())
    f = eng.final[who_at("Sol")]
    assert f["in_flight"] and f["where"] == "Wolf 359"
    assert f["cargo_value"] == pytest.approx(eng.markets["Wolf 359"].quote_sell(5.0, "tech", 20))
    assert f["net_worth"] == pytest.approx(f["cash"] + f["cargo_value"])


class JitteredGreedy(GreedyMerchant):
    def __init__(self, rng: random.Random, speeds: tuple[float, ...]) -> None:
        super().__init__(speeds)
        self.rng = rng

    async def decide(self, ctx: DecisionContext) -> None:
        await asyncio.sleep(self.rng.random() * 0.002)  # 打乱并发决策的完成顺序
        await super().decide(ctx)


def _run_greedy(jitter_seed: int | None, c_info: float, kin=None, speeds=REL_SPEEDS):
    w = make_world(3, Rules(t_end=60.0))
    rng = random.Random(jitter_seed)
    agents = {
        n: (JitteredGreedy(rng, speeds) if jitter_seed is not None else GreedyMerchant(speeds))
        for n in w.merchant_names
    }
    eng = Engine(w, agents, c_info=c_info, kin=kin or Relativistic())
    asyncio.run(eng.run())
    events = json.dumps([e.to_json() for e in eng.events.all()], sort_keys=True)
    return eng, events


def test_concurrent_schedule_is_order_independent() -> None:
    base, base_events = _run_greedy(None, LIGHT)
    assert base.max_parallel > 1, "spacelike-separated decisions should run concurrently"
    for seed in (1, 2, 3):
        eng, events = _run_greedy(seed, LIGHT)
        assert events == base_events
        assert eng.final == base.final


def test_instant_information_serializes_decisions() -> None:
    eng, _ = _run_greedy(5, INSTANT)
    assert eng.max_parallel == 1
    eng2, _ = _run_greedy(None, INSTANT, Newtonian(), NEWTON_SPEEDS)
    assert eng2.max_parallel == 1


def test_newtonian_requires_instant_information() -> None:
    w = make_world(7)
    with pytest.raises(ValueError):
        Engine(w, {}, c_info=LIGHT, kin=Newtonian())
