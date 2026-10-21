"""模拟引擎：商人状态、决策上下文（唯一的规则校验处）、光锥保守并发调度、终局结算。

时间记账：
- 船上的一切按船时 τ：船员工资、药品腐败、船员年龄。停靠时 dτ = dt，飞行时 dτ = dt/γ。
- 船外的一切按坐标时 t：星站库存、价格、冲击、信息传播。
- 飞行工资在出发时按该航程的 dτ 预付；停靠工资在下次行动时结算。

调度：每个商人任一时刻至多有一个待决事件（下一次停靠决策的时间和地点）。
决策 A=(x, t) 可以开始，当且仅当其他每个商人的待决/进行中事件 B=(x', t') 的影响都到不了 A：
t − t' < d(x', x)/c。飞船慢于光，B 之后派生的事件也在 B 的未来光锥里，
由三角不等式，它们同样到不了 A。所以类空间隔的决策可以并发，结果与完成顺序无关。
"""

from __future__ import annotations

import asyncio
import logging
import math
from dataclasses import dataclass, field
from typing import Any, Protocol

from reltrade.events import Event, EventLog
from reltrade.market import Board, MarketError, StationMarket
from reltrade.physics import Kinematics, Leg, light_lag
from reltrade.world import ENERGY, World

log = logging.getLogger("reltrade.engine")


class ActionError(ValueError):
    """商人提交了不合法的动作；错误信息会原样返回给商人。"""


@dataclass(frozen=True)
class Transit:
    origin: str
    destination: str
    t_dep: float
    t_arr: float
    speed: float
    dtau: float


@dataclass
class MerchantState:
    name: str
    station: str | None
    cash: float
    cargo: dict[str, float]
    tau: float = 0.0
    clock_t: float = 0.0  # τ、工资、腐败已结算到的坐标时
    notebook: str = ""
    transit: Transit | None = None
    next_t: float = 0.0
    next_station: str = ""
    seen: set[str] = field(default_factory=set)
    notices: list[str] = field(default_factory=list)
    decisions: int = 0


@dataclass(frozen=True)
class RemoteView:
    station: str
    distance: float
    asof: float
    age: float
    board: Board


@dataclass(frozen=True)
class Observation:
    merchant: str
    t: float
    tau: float
    t_end: float
    station: str
    cash: float
    cargo: dict[str, float]
    hold_used: float
    hold_capacity: float
    mass: float
    local: Board
    remote: list[RemoteView]
    events: list[Event]
    notebook: str
    notices: list[str]
    first: bool


class Agent(Protocol):
    async def decide(self, ctx: DecisionContext) -> None: ...


def _fmt(x: float) -> str:
    return f"{x:,.0f}" if abs(x) >= 100 else f"{x:.2f}"


class DecisionContext:
    """一次停靠决策。商人（LLM 或贪心）只能通过这里的方法读世界、改状态。"""

    def __init__(self, engine: Engine, merchant: MerchantState, t: float) -> None:
        self.engine = engine
        self.m = merchant
        self.t = t
        self.station: str = merchant.station or ""
        self.outcome: str | None = None  # depart | wait
        self.forced = False
        self.actions: list[dict[str, Any]] = []
        self._seq = 0
        self._messages = 0

    # ---------- 读 ----------
    @property
    def world(self) -> World:
        return self.engine.world

    @property
    def done(self) -> bool:
        return self.outcome is not None

    def hold_used(self) -> float:
        return sum(q * self.world.goods[g].mass for g, q in self.m.cargo.items())

    def mass(self) -> float:
        return self.world.rules.hull_mass + self.hold_used()

    def local_board(self) -> Board:
        return self.engine.markets[self.station].board_at(self.t)

    def remote_views(self) -> list[RemoteView]:
        views = []
        for s in self.world.station_names:
            if s == self.station:
                continue
            d = self.world.distance(self.station, s)
            age = light_lag(d, self.engine.c_info)
            asof = self.t - age
            views.append(RemoteView(s, d, asof, age, self.engine.markets[s].board_at(asof)))
        return views

    def observe(self) -> Observation:
        """本次停靠的完整观察；光锥内新到的事件只给一次（标记已读）。"""
        events = [
            e
            for e in self.engine.events.visible(self.station, self.t, viewer=self.m.name)
            if e.id not in self.m.seen and e.actor != self.m.name
        ]
        self.m.seen.update(e.id for e in events)
        notices, self.m.notices = self.m.notices, []
        r = self.world.rules
        return Observation(
            merchant=self.m.name,
            t=self.t,
            tau=self.m.tau,
            t_end=r.t_end,
            station=self.station,
            cash=self.m.cash,
            cargo={g: q for g, q in self.m.cargo.items() if q > 1e-9},
            hold_used=self.hold_used(),
            hold_capacity=r.hold_capacity,
            mass=self.mass(),
            local=self.local_board(),
            remote=self.remote_views(),
            events=events,
            notebook=self.m.notebook,
            notices=notices,
            first=self.m.decisions == 1,
        )

    def trip_costs(
        self, destination: str, speed: float, mass: float | None = None
    ) -> tuple[Leg, float, float | None, float]:
        """(航程, 所需能量, 能源总价或 None=本站供不起, 工资)；mass 缺省为当前质量。"""
        try:
            leg = self.engine.kin.leg(self.world.distance(self.station, destination), speed)
        except ValueError as e:
            raise ActionError(str(e)) from None
        units = leg.energy_per_mass * (self.mass() if mass is None else mass)
        try:
            cost: float | None = self.engine.markets[self.station].quote_buy(self.t, ENERGY, units)
        except MarketError:
            cost = None
        return leg, units, cost, self.world.rules.wage * leg.dtau

    # ---------- 写 ----------
    def _check_open(self) -> None:
        if self.done:
            raise ActionError(f"your turn already ended ({self.outcome}); this action was ignored")

    def _check_good(self, good: str) -> str:
        if good not in self.world.goods:
            raise ActionError(f"unknown good {good!r}; goods are {', '.join(self.world.goods)}")
        return good

    def _check_qty(self, qty: Any) -> float:
        try:
            q = float(qty)
        except (TypeError, ValueError):
            raise ActionError(f"quantity must be a number, got {qty!r}") from None
        if not (math.isfinite(q) and q > 0):
            raise ActionError(f"quantity must be positive, got {qty!r}")
        return q

    def _check_station(self, name: str) -> str:
        if name not in self.world.stations:
            raise ActionError(
                f"unknown station {name!r}; stations: " f"{', '.join(self.world.station_names)}"
            )
        if name == self.station:
            raise ActionError(f"you are already at {name}")
        return name

    def _event(self, kind: str, data: dict[str, Any], audience: str | None = None) -> None:
        self._seq += 1
        eid = f"{self.t:014.6f}|{self.m.name}|{self.m.decisions}|{self._seq}"
        self.engine.events.add(Event(eid, self.t, self.station, kind, self.m.name, audience, data))

    def _record(self, kind: str, **data: Any) -> None:
        rec = {"type": kind, "t": self.t, "merchant": self.m.name, "station": self.station, **data}
        self.actions.append(rec)
        self.engine.records.append(rec)

    def buy(self, good: str, qty: Any) -> dict[str, Any]:
        self._check_open()
        g, q = self._check_good(good), self._check_qty(qty)
        rules, market = self.world.rules, self.engine.markets[self.station]
        hold_after = self.hold_used() + q * self.world.goods[g].mass
        if hold_after > rules.hold_capacity + 1e-9:
            room = (rules.hold_capacity - self.hold_used()) / self.world.goods[g].mass
            raise ActionError(f"hold too small: room for {room:.1f} more {g}")
        try:
            cost = market.quote_buy(self.t, g, q)
        except MarketError as e:
            raise ActionError(str(e)) from None
        if cost > self.m.cash + 1e-9:
            raise ActionError(f"{q:g} {g} costs {_fmt(cost)}¢ but you have {_fmt(self.m.cash)}¢")
        market.buy(self.t, g, q)
        self.m.cash -= cost
        self.m.cargo[g] = self.m.cargo.get(g, 0.0) + q
        self._event("trade", {"side": "buy", "good": g, "qty": q, "value": cost})
        self._record("trade", side="buy", good=g, qty=q, value=cost)
        return {
            "good": g,
            "qty": q,
            "cost": cost,
            "avg": cost / q,
            "cash": self.m.cash,
            "hold_used": self.hold_used(),
            "ask_now": self.local_board().quotes[g].ask,
        }

    def sell(self, good: str, qty: Any) -> dict[str, Any]:
        self._check_open()
        g, q = self._check_good(good), self._check_qty(qty)
        held = self.m.cargo.get(g, 0.0)
        if q > held + 1e-6:
            raise ActionError(f"you hold only {held:.2f} {g}")
        q = min(q, held)
        revenue = self.engine.markets[self.station].sell(self.t, g, q)
        self.m.cash += revenue
        self.m.cargo[g] = held - q if held - q > 1e-9 else 0.0
        self._event("trade", {"side": "sell", "good": g, "qty": q, "value": revenue})
        self._record("trade", side="sell", good=g, qty=q, value=revenue)
        return {
            "good": g,
            "qty": q,
            "revenue": revenue,
            "avg": revenue / q,
            "cash": self.m.cash,
            "bid_now": self.local_board().quotes[g].bid,
        }

    def quote(self, destination: str, speeds: list[Any]) -> list[dict[str, Any]]:
        dest = self._check_station(destination)
        if not speeds:
            raise ActionError("give at least one speed")
        out = []
        for raw in list(speeds)[:10]:
            try:
                speed = float(raw)
                leg, units, cost, wages = self.trip_costs(dest, speed)
            except (TypeError, ValueError) as e:  # ActionError 是 ValueError 的子类
                out.append({"speed": raw, "error": str(e)})
                continue
            spoil = {
                g: 0.5 ** (leg.dtau / self.world.goods[g].half_life)
                for g, q in self.m.cargo.items()
                if q > 1e-9 and self.world.goods[g].half_life
            }
            total = None if cost is None else cost + wages
            out.append(
                {
                    "speed": speed,
                    "dt": leg.dt,
                    "dtau": leg.dtau,
                    "arrival": self.t + leg.dt,
                    "energy_units": units,
                    "energy_cost": cost,
                    "wages": wages,
                    "total": total,
                    "affordable": total is not None and total <= self.m.cash + 1e-9,
                    "survival": spoil,
                    "after_end": self.t + leg.dt > self.world.rules.t_end,
                }
            )
        return out

    def depart(self, destination: str, speed: Any, plan: str = "") -> dict[str, Any]:
        self._check_open()
        dest = self._check_station(destination)
        try:
            speed = float(speed)
        except (TypeError, ValueError):
            raise ActionError(f"speed must be a number, got {speed!r}") from None
        leg, units, cost, wages = self.trip_costs(dest, speed)
        if cost is None:
            raise ActionError(
                f"{self.station} cannot supply {units:.1f} energy units right now "
                f"(max {self.engine.markets[self.station].max_buy(self.t, ENERGY):.1f}); "
                "fly slower or lighter"
            )
        if cost + wages > self.m.cash + 1e-9:
            raise ActionError(
                f"this flight needs {_fmt(cost + wages)}¢ (energy {_fmt(cost)} + "
                f"wages {_fmt(wages)}) but you have {_fmt(self.m.cash)}¢"
            )
        perceived = {v.station: v for v in self.remote_views()}[dest]
        energy_ask = self.local_board().quotes[ENERGY].ask
        self.engine.markets[self.station].buy(self.t, ENERGY, units)
        self.m.cash -= cost + wages
        cargo = {g: q for g, q in self.m.cargo.items() if q > 1e-9}
        self.m.transit = Transit(self.station, dest, self.t, self.t + leg.dt, speed, leg.dtau)
        self.m.next_t, self.m.next_station = self.t + leg.dt, dest
        self.outcome = "depart"
        self._event("depart", {"to": dest, "speed": speed, "eta": self.t + leg.dt, "cargo": cargo})
        self._record(
            "depart",
            to=dest,
            speed=speed,
            distance=leg.distance,
            dt=leg.dt,
            dtau=leg.dtau,
            eta=self.t + leg.dt,
            mass=self.mass(),
            energy_units=units,
            energy_cost=cost,
            wages=wages,
            cargo=cargo,
            plan=plan,
            perceived_age=perceived.age,
            perceived={
                g: {"bid": q.bid, "stock": q.stock}
                for g, q in perceived.board.quotes.items()
                if g != ENERGY
            },
            energy_ask=energy_ask,
        )
        return {
            "destination": dest,
            "speed": speed,
            "arrival": self.t + leg.dt,
            "dt": leg.dt,
            "dtau": leg.dtau,
            "energy_units": units,
            "energy_cost": cost,
            "wages": wages,
            "cash": self.m.cash,
        }

    def wait(self, years: Any, reason: str = "", forced: bool = False) -> dict[str, Any]:
        self._check_open()
        r = self.world.rules
        try:
            y = float(years)
        except (TypeError, ValueError):
            raise ActionError(f"years must be a number, got {years!r}") from None
        if not (r.min_wait <= y <= r.max_wait):
            raise ActionError(f"wait between {r.min_wait} and {r.max_wait} years")
        self.m.next_t, self.m.next_station = self.t + y, self.station
        self.outcome, self.forced = "wait", forced
        self._record("wait", years=y, reason=reason, forced=forced)
        return {"until": self.t + y, "years": y}

    def send_message(self, to: str, text: str) -> dict[str, Any]:
        r = self.world.rules
        names = self.world.merchant_names
        if to != "all" and (to not in names or to == self.m.name):
            raise ActionError(
                f"'to' must be 'all' or one of {', '.join(n for n in names if n != self.m.name)}"
            )
        text = (text or "").strip()
        if not text:
            raise ActionError("empty message")
        if len(text) > r.max_message_chars:
            raise ActionError(f"message too long ({len(text)} > {r.max_message_chars} chars)")
        if self._messages >= r.max_messages_per_dock:
            raise ActionError(f"at most {r.max_messages_per_dock} messages per stop")
        self._messages += 1
        self._event("message", {"to": to, "text": text}, audience=None if to == "all" else to)
        self._record("message", to=to, text=text)
        return {"to": to, "chars": len(text)}

    def write_notebook(self, text: str) -> dict[str, Any]:
        limit = self.world.rules.max_notebook_chars
        text = text or ""
        if len(text) > limit:
            raise ActionError(f"notebook too long ({len(text)} > {limit} chars)")
        self.m.notebook = text
        self._record("notebook", chars=len(text))
        return {"chars": len(text)}


@dataclass
class _Slot:
    t: float
    station: str
    running: bool = False


class Engine:
    def __init__(
        self,
        world: World,
        agents: dict[str, Agent],
        *,
        c_info: float,
        kin: Kinematics,
        label: str = "run",
        logger: logging.Logger | None = None,
    ) -> None:
        if kin.name == "newtonian" and not math.isinf(c_info):
            # 牛顿运动学下船可以超光速，光速信息的因果结构不再成立
            raise ValueError("newtonian kinematics requires instant information")
        self.world, self.agents, self.c_info, self.kin = world, agents, c_info, kin
        self.label = label
        self.log = logger or log
        r = world.rules
        self.markets = {
            s: StationMarket(spec, {g: world.schedules[(s, g)] for g in spec.markets}, r)
            for s, spec in world.stations.items()
        }
        self.events = EventLog(world.distance, c_info)
        for i, sh in enumerate(world.shocks):
            data = {
                "headline": sh.headline,
                "good": sh.good,
                "kind": sh.kind,
                "factor": sh.factor,
                "until": sh.t_end,
            }
            self.events.add(
                Event(f"{sh.t_start:014.6f}|world|{i}|0", sh.t_start, sh.station, "news", data=data)
            )
            if sh.t_end < r.t_end:
                self.events.add(
                    Event(
                        f"{sh.t_end:014.6f}|world|{i}|1",
                        sh.t_end,
                        sh.station,
                        "news_end",
                        data={"headline": sh.end_headline, "good": sh.good},
                    )
                )
        self.merchants = {
            name: MerchantState(
                name, start, r.start_cash, {g: 0.0 for g in world.goods}, next_station=start
            )
            for name, start in world.starts.items()
        }
        self.records: list[dict[str, Any]] = []
        self.final: dict[str, dict[str, Any]] = {}
        self.max_parallel = 0

    # ---------- 调度 ----------
    def _safe(self, name: str, slot: _Slot, slots: dict[str, _Slot]) -> bool:
        for other, s in slots.items():
            if other == name:
                continue
            lag = light_lag(self.world.distance(s.station, slot.station), self.c_info)
            if slot.t - s.t < lag:
                continue  # other 的影响到不了这里
            if not s.running and lag == 0.0 and slot.t == s.t and name < other:
                continue  # 同时同地（或瞬时信息下同时）：按名字先后
            return False
        return True

    async def run(self) -> dict[str, dict[str, Any]]:
        t_end = self.world.rules.t_end
        slots = {n: _Slot(0.0, m.station or "") for n, m in self.merchants.items()}
        running: dict[asyncio.Task[None], str] = {}
        try:
            while slots:
                for n in [n for n, s in slots.items() if not s.running and s.t >= t_end]:
                    del slots[n]  # 下一个决策在终局之后：退场
                pending = sorted((s.t, n) for n, s in slots.items() if not s.running)
                for _, n in pending:
                    if self._safe(n, slots[n], slots):
                        slots[n].running = True
                        running[asyncio.create_task(self._process(n), name=n)] = n
                self.max_parallel = max(self.max_parallel, len(running))
                if not running:
                    if slots:
                        raise RuntimeError(f"scheduler deadlock: {slots}")
                    break
                done, _ = await asyncio.wait(running, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    n = running.pop(task)
                    task.result()  # 传播异常
                    m = self.merchants[n]
                    slots[n] = _Slot(m.next_t, m.next_station)
        finally:
            for task in running:
                task.cancel()
            if running:
                await asyncio.gather(*running, return_exceptions=True)
        self._finalize()
        return self.final

    # ---------- 决策 ----------
    async def _process(self, name: str) -> None:
        m = self.merchants[name]
        t = m.next_t
        if m.transit is not None:
            self._arrive(m)
        else:
            self._settle_docked(m, t)
        if m.cash < 0 and not any(q > 1e-9 for q in m.cargo.values()):
            # 欠着船员工资、又没有货可卖：破产退场
            m.next_t = math.inf
            self.records.append(
                {"type": "bankrupt", "t": t, "merchant": name, "station": m.station, "cash": m.cash}
            )
            self.log.warning(
                "[%s] y%.2f %s is bankrupt at %s (cash %s)",
                self.label,
                t,
                name,
                m.station,
                _fmt(m.cash),
            )
            return
        m.decisions += 1
        ctx = DecisionContext(self, m, t)
        cash0 = m.cash
        await self.agents[name].decide(ctx)
        if not ctx.done:
            ctx.wait(self.world.rules.default_wait, "(automatic: no decision)", forced=True)
            m.notices.append(
                f"You did not end your last turn with depart or wait, so you stayed "
                f"docked for {self.world.rules.default_wait:g} year."
            )
        self.log.info(
            "[%s] y%.2f %s @ %s: %s cash %s→%s",
            self.label,
            t,
            name,
            ctx.station,
            _summarize(ctx.actions),
            _fmt(cash0),
            _fmt(m.cash),
        )

    def _spoil(self, m: MerchantState, dtau: float) -> list[str]:
        notes = []
        for g, q in m.cargo.items():
            h = self.world.goods[g].half_life
            if h and q > 1e-9 and dtau > 0:
                left = q * 0.5 ** (dtau / h)
                notes.append(f"{g} spoiled {q:.1f} → {left:.1f}")
                m.cargo[g] = left
        return notes

    def _arrive(self, m: MerchantState) -> None:
        tr = m.transit
        assert tr is not None
        m.tau += tr.dtau
        spoiled = self._spoil(m, tr.dtau)
        m.station, m.clock_t, m.transit = tr.destination, tr.t_arr, None
        msg = (
            f"Arrived from {tr.origin}: {self.world.distance(tr.origin, tr.destination):.2f} ly "
            f"at {tr.speed:g}{self.kin.speed_unit} took {tr.t_arr - tr.t_dep:.2f} Galactic years "
            f"({tr.dtau:.2f} ship years; wages were prepaid)."
        )
        if spoiled:
            msg += " " + "; ".join(spoiled) + "."
        m.notices.append(msg)
        eid = f"{tr.t_arr:014.6f}|{m.name}|{m.decisions + 1}|0"
        self.events.add(
            Event(
                eid,
                tr.t_arr,
                tr.destination,
                "arrive",
                m.name,
                data={"from": tr.origin, "speed": tr.speed},
            )
        )
        self.records.append(
            {
                "type": "arrive",
                "t": tr.t_arr,
                "merchant": m.name,
                "station": tr.destination,
                "origin": tr.origin,
                "speed": tr.speed,
                "t_dep": tr.t_dep,
                "dtau": tr.dtau,
                "cargo": {g: q for g, q in m.cargo.items() if q > 1e-9},
            }
        )

    def _settle_docked(self, m: MerchantState, t: float) -> None:
        dt = t - m.clock_t
        if dt <= 0:
            return
        wages = self.world.rules.wage * dt
        m.tau += dt
        m.cash -= wages
        spoiled = self._spoil(m, dt)
        m.clock_t = t
        note = f"Docked {dt:.2f} years: crew wages −{_fmt(wages)}¢."
        if spoiled:
            note += " " + "; ".join(spoiled) + "."
        m.notices.append(note)

    # ---------- 终局 ----------
    def _finalize(self) -> None:
        t_end = self.world.rules.t_end
        for m in self.merchants.values():
            if m.transit is not None:  # 终局时仍在飞：按目的地 t_end 时的价格清算
                self._spoil(m, m.transit.dtau)
                where, in_flight = m.transit.destination, True
                tau_end = m.tau + m.transit.dtau
            else:
                self._settle_docked(m, t_end)
                where, in_flight, tau_end = m.station or "", False, m.tau
            market = self.markets[where]
            value = sum(market.quote_sell(t_end, g, q) for g, q in m.cargo.items() if q > 1e-9)
            self.final[m.name] = {
                "net_worth": m.cash + value,
                "cash": m.cash,
                "cargo_value": value,
                "cargo": {g: q for g, q in m.cargo.items() if q > 1e-9},
                "where": where,
                "in_flight": in_flight,
                "tau": tau_end,
                "decisions": m.decisions,
                "notebook": m.notebook,
            }


def _summarize(actions: list[dict[str, Any]]) -> str:
    parts = []
    for a in actions:
        if a["type"] == "trade":
            sign = "+" if a["side"] == "sell" else "−"
            parts.append(f"{a['side']} {a['qty']:.1f} {a['good']} ({sign}{_fmt(a['value'])}¢)")
        elif a["type"] == "depart":
            parts.append(f"→ {a['to']} @{a['speed']:g} ETA y{a['eta']:.1f}")
        elif a["type"] == "wait":
            parts.append(f"wait {a['years']:g}y" + (" (forced)" if a["forced"] else ""))
        elif a["type"] == "message":
            parts.append(f"msg→{a['to']}")
    return "; ".join(parts) or "(nothing)"
