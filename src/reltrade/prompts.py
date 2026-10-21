"""LLM 商人看到的一切文字：系统提示、工具定义、停靠报告、工具结果。

提示只描述规则和物理，不暗示策略；停靠报告只含光锥内的信息，远方数据都标注年龄。
"""

from __future__ import annotations

import math
from typing import Any

from reltrade.engine import Observation
from reltrade.events import Event
from reltrade.physics import Kinematics, Relativistic
from reltrade.world import ENERGY, World

_SHORT = {
    "Sol": "Sol",
    "Alpha Centauri": "aCen",
    "Wolf 359": "Wolf",
    "Lalande 21185": "Lal",
    "Ross 128": "Ross",
    "Sirius": "Sir",
    "Procyon": "Proc",
}


def _money(x: float) -> str:
    return f"{x:,.0f}¢" if abs(x) >= 100 else f"{x:.1f}¢"


def _price(x: float | None) -> str:
    if x is None:
        return "–"
    return f"{x:.1f}" if x >= 10 else f"{x:.2f}"


def _speed(v: float, kin: Kinematics) -> str:
    return f"{v:g}c" if kin.name == "relativistic" else f"{v:g} ly/yr"


def _physics_block(kin: Kinematics, c_info: float) -> str:
    instant = math.isinf(c_info)
    if kin.name == "relativistic":
        rel = Relativistic()
        ex = {b: rel.leg(10.0, b) for b in (0.5, 0.9, 0.99, 0.999)}
        lines = [
            "- All stations are at rest in one frame and share Galactic time t (years).",
            "- Your ship jumps instantly to any cruise speed β (a fraction of light speed, "
            "0 < β < 1), coasts, and stops instantly at the destination.",
            "- Galactic flight time = d/β. Ship time (your clock and your crew's) = d/(βγ), "
            "γ = 1/√(1−β²). For a 10 ly trip: "
            + "; ".join(
                f"β={b:g} → {leg.dt:.2f} Galactic y, {leg.dtau:.2f} ship y" for b, leg in ex.items()
            )
            + ".",
            "- Energy per trip = 2(γ−1) × (hull + cargo mass), paid in full at departure at the "
            "departure station's energy price; it covers both acceleration and braking. 2(γ−1) = "
            + ", ".join(f"{leg.energy_per_mass:.2f} at β={b:g}" for b, leg in ex.items())
            + ".",
        ]
        if instant:
            lines.append(
                "- Information is instantaneous (an ansible network): prices, news, port "
                "traffic and messages from every station are live, even though no ship "
                "can reach light speed."
            )
        else:
            lines.append(
                "- Information travels at light speed. What you know about another "
                "station is as old as its distance in light-years: data from a station "
                "8.6 ly away is 8.6 years old when it reaches you. News, port traffic and "
                "messages also travel at light speed."
            )
    else:
        lines = [
            "- All stations share one Galactic time t (years). There is no light-speed barrier and no "
            "time dilation: ship time equals Galactic time.",
            f"- Your ship jumps instantly to any cruise speed v (light-years per year, "
            f"0 < v ≤ {kin.max_speed:g}), coasts, and stops instantly at the destination. "
            "Flight time = d/v.",
            "- Energy per trip = v² × (hull + cargo mass) (½mv² to accelerate plus the same to "
            "brake), paid in full at departure at the departure station's energy price.",
            "- Information is instantaneous (an ansible network): prices, news, port traffic and "
            "messages from every station are live.",
        ]
    lines.append(
        "- Buying energy moves the station's energy price like any other purchase. "
        "You only receive information while docked."
    )
    return "\n".join(lines)


def system_prompt(world: World, name: str, kin: Kinematics, c_info: float) -> str:
    r, g = world.rules, world.goods
    rivals = ", ".join(n for n in world.merchant_names if n != name)
    names = world.station_names
    header = " " * 16 + "".join(f"{_SHORT.get(s, s[:5]):>7}" for s in names)
    rows = [header] + [
        f"{s:16}"
        + "".join(f"{world.distance(s, o):7.2f}" if s != o else f"{'–':>7}" for o in names)
        for s in names
    ]
    stations = "\n".join(f"- {s}: {spec.blurb}" for s, spec in world.stations.items())
    signal = "instantly" if math.isinf(c_info) else "at light speed"
    return f"""You are {name}, an independent merchant captain trading between seven star systems. Five rival captains trade in the same network: {rivals}. Each runs their own ship and makes their own decisions.

## Goal
Maximize your net worth at Galactic year {r.t_end:g}: cash plus the liquidation value of your cargo at the station where you are docked then (if you are still in flight, at your destination's prices, after spoilage). Everyone starts at year 0 with {r.start_cash:,.0f}¢ and an empty hold, at different stations.

## Physics (units: light-years, years; c = 1 ly/yr)
{_physics_block(kin, c_info)}

## Your ship
- Hull mass {r.hull_mass:g} M; the hold carries up to {r.hold_capacity:g} M of cargo. Mass per unit: ore {g['ore'].mass:g} M, tech {g['tech'].mass:g} M, med {g['med'].mass:g} M.
- Crew wages: {r.wage:g}¢ per year of SHIP time, docked or flying. Flight wages are prepaid at departure; docked wages are charged when you next act.
- Medicine spoils by SHIP time: half of it is lost every {g['med'].half_life:g} ship-years. Ore and tech never spoil. Station stocks never spoil.

## Markets
- Every station buys and sells ore, tech and med, and sells energy (used only for departures). Bid/ask spread {r.spread:.0%}.
- For each good at each station, price × stock is constant: buying raises the price, selling lowers it, and a large order fills along the curve. Selling q units into stock S at quoted bid b yields b·S·ln(1+q/S); buying q at ask a costs a·S·ln(S/(S−q)). You cannot buy more than {r.max_trade_fraction:.0%} of a station's stock at once.
- Stocks drift back toward each station's normal level with a ~{r.relax_time:g}-year time constant (local production and consumption).
- Shocks (shortages, gluts, energy crises) hit stations from time to time and are reported in the news; they last years, then fade.

## Star map
{stations}
Distances (light-years):
{chr(10).join(rows)}

## How turns work
You act only while docked. Each time you dock you get a report: live local prices, the latest data that has reached this station from the others, and the news, port traffic and messages that arrived since your last report.
Tools: buy, sell, quote_trip, depart, wait, send_message, write_notebook. You may call several tools in one response; they run in order. Your turn ends when you depart or wait.
- quote_trip returns exact flight time, ship time, energy cost, wages and spoilage for the speeds you list, given your current cargo. Use it instead of computing by hand; buy and sell first if your cargo will change.
- send_message reaches one rival or "all", travelling {signal} from your current station; a rival reads it when docked at a station the signal has reached. Rivals may lie, and so may you.
- write_notebook stores private notes that are shown to you in every report (it replaces the previous notes; max {r.max_notebook_chars} characters)."""


def tools(world: World, kin: Kinematics, name: str) -> list[dict[str, Any]]:
    goods = list(world.goods)
    stations = world.station_names
    others = [n for n in world.merchant_names if n != name]
    if kin.name == "relativistic":
        speed_desc = (
            f"cruise speed as a fraction of light speed, {kin.min_speed:g} to {kin.max_speed:g}"
        )
    else:
        speed_desc = f"cruise speed in light-years per year, {kin.min_speed:g} to {kin.max_speed:g}"

    def fn(tool: str, desc: str, props: dict[str, Any], required: list[str]) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": tool,
                "description": desc,
                "parameters": {"type": "object", "properties": props, "required": required},
            },
        }

    return [
        fn(
            "buy",
            "Buy goods at this station's market (fills along the price curve).",
            {
                "good": {"type": "string", "enum": goods},
                "quantity": {"type": "number", "description": "units to buy"},
            },
            ["good", "quantity"],
        ),
        fn(
            "sell",
            "Sell goods from your hold at this station.",
            {
                "good": {"type": "string", "enum": goods},
                "quantity": {"type": "number", "description": "units to sell"},
            },
            ["good", "quantity"],
        ),
        fn(
            "quote_trip",
            "Exact flight time, ship time, energy cost, wages and spoilage for "
            "candidate speeds to one destination, for your current cargo. Changes nothing.",
            {
                "destination": {"type": "string", "enum": stations},
                "speeds": {
                    "type": "array",
                    "items": {"type": "number"},
                    "description": f"up to 10 speeds; {speed_desc}",
                },
            },
            ["destination", "speeds"],
        ),
        fn(
            "depart",
            "Leave for another station. Pays energy and flight wages now. Ends your turn.",
            {
                "destination": {"type": "string", "enum": stations},
                "speed": {"type": "number", "description": speed_desc},
                "plan": {
                    "type": "string",
                    "description": "your intent for this trip, for your own records",
                },
            },
            ["destination", "speed", "plan"],
        ),
        fn(
            "wait",
            "Stay docked here for a while, then get a new report. Ends your turn.",
            {
                "years": {"type": "number", "description": "Galactic years, 0.1 to 20"},
                "reason": {"type": "string"},
            },
            ["years", "reason"],
        ),
        fn(
            "send_message",
            "Send a message to one rival captain or to all, from this station.",
            {"to": {"type": "string", "enum": ["all", *others]}, "text": {"type": "string"}},
            ["to", "text"],
        ),
        fn(
            "write_notebook",
            "Replace your private notebook (shown in every report).",
            {"text": {"type": "string"}},
            ["text"],
        ),
    ]


def _event_line(e: Event, kin: Kinematics) -> str:
    d = e.data
    head = f"y{e.t:.2f} {e.station}:"
    if e.kind in ("news", "news_end"):
        return f"{head} NEWS — {d['headline']}"
    if e.kind == "arrive":
        return f"{head} {e.actor} arrived from {d['from']}"
    if e.kind == "depart":
        cargo = ", ".join(f"{g} {q:.2f}" for g, q in d["cargo"].items()) or "nothing"
        return (
            f"{head} {e.actor} departed for {d['to']} at {_speed(d['speed'], kin)} "
            f"(ETA y{d['eta']:.2f}) carrying {cargo}"
        )
    if e.kind == "trade":
        verb = "bought" if d["side"] == "buy" else "sold"
        return f"{head} {e.actor} {verb} {d['qty']:.1f} {d['good']} for {_money(d['value'])}"
    if e.kind == "message":
        to = "all" if d["to"] == "all" else "you"
        return f"{head} MESSAGE from {e.actor} to {to}: \"{d['text']}\""
    return f"{head} {e.kind} {d}"


def _quotes_inline(quotes: dict[str, Any], goods: list[str]) -> str:
    parts = [
        f"{g} {_price(quotes[g].bid)}/{_price(quotes[g].ask)} "
        f"[{quotes[g].stock:,.0f}/{quotes[g].normal:,.0f}]"
        for g in goods
    ]
    parts.append(f"energy {_price(quotes[ENERGY].ask)}")
    return "  ".join(parts)


def render_observation(
    obs: Observation, world: World, kin: Kinematics, c_info: float, report_no: int
) -> str:
    goods = list(world.goods)
    left = obs.t_end - obs.t
    lines = [
        f"REPORT #{report_no} — docked at {obs.station} — Galactic year {obs.t:.2f} — "
        f"ship clock {obs.tau:.2f} y — {left:.2f} Galactic years left"
    ]
    lines += [f"• {n}" for n in obs.notices]
    cargo = ", ".join(f"{g} {q:.2f}" for g, q in obs.cargo.items()) or "empty"
    lines.append(
        f"Cash {_money(obs.cash)} · Hold {obs.hold_used:.3f}/{obs.hold_capacity:g} M "
        f"({cargo}) · Ship mass {obs.mass:.3f} M"
    )
    lines.append("")
    lines.append("LOCAL MARKET (live)   bid / ask   [stock / normal]")
    for g in [*goods, ENERGY]:
        q = obs.local.quotes[g]
        lines.append(
            f"  {g:7} {_price(q.bid):>8} / {_price(q.ask):<8} [{q.stock:,.0f} / "
            f"{q.normal:,.0f}]"
        )
    lines.append("")
    if math.isinf(c_info):
        lines.append("OTHER STATIONS (live via ansible)   bid/ask [stock/normal]")
    else:
        lines.append(
            "OTHER STATIONS — light-delayed snapshots: each line shows that station as "
            "it was `age` years ago   bid/ask [stock/normal]"
        )
    for v in obs.remote:
        when = "live" if math.isinf(c_info) else f"as of y{v.asof:.2f} ({v.age:.2f} y old)"
        lines.append(
            f"  {v.station:15} {v.distance:5.2f} ly  {when}:  "
            f"{_quotes_inline(v.board.quotes, goods)}"
        )
    if obs.first and not math.isinf(c_info):
        lines.append(
            "  (Before year 0 every station was at its normal level, so this first "
            "snapshot shows normal prices.)"
        )
    lines.append("")
    reach = "arrived" if math.isinf(c_info) else "whose light reached"
    lines.append(
        f"NEWS, PORT TRAFFIC AND MESSAGES {reach} {obs.station} since your last report "
        "(oldest first)"
    )
    lines += [f"  {_event_line(e, kin)}" for e in obs.events] or ["  (nothing new)"]
    lines.append("")
    lines.append("YOUR NOTEBOOK")
    lines.append(obs.notebook.strip() or "(empty)")
    return "\n".join(lines)


def render_result(tool: str, res: Any, kin: Kinematics, station: str = "") -> str:
    if tool == "buy":
        return (
            f"Bought {res['qty']:.2f} {res['good']} for {_money(res['cost'])} "
            f"(avg {_price(res['avg'])}). Cash {_money(res['cash'])}. Hold "
            f"{res['hold_used']:.3f} M. {res['good']} ask is now {_price(res['ask_now'])}."
        )
    if tool == "sell":
        return (
            f"Sold {res['qty']:.2f} {res['good']} for {_money(res['revenue'])} "
            f"(avg {_price(res['avg'])}). Cash {_money(res['cash'])}. {res['good']} bid is "
            f"now {_price(res['bid_now'])}."
        )
    if tool == "quote_trip":
        out = [res["header"]]
        for q in res["quotes"]:
            if "error" in q:
                out.append(f"  {q['speed']}: ERROR {q['error']}")
                continue
            if q["energy_cost"] is None:
                energy = f"energy {q['energy_units']:.1f} units — MORE THAN THIS STATION CAN SUPPLY"
                total = ""
            else:
                energy = f"energy {q['energy_units']:.2f} units = {_money(q['energy_cost'])}"
                total = f"; total {_money(q['total'])}"
            flags = []
            if q["energy_cost"] is not None and not q["affordable"]:
                flags.append("CANNOT AFFORD")
            if q["after_end"]:
                flags.append("ARRIVES AFTER THE END")
            spoil = "".join(f"; {g} survives {s:.0%}" for g, s in q["survival"].items())
            out.append(
                f"  {_speed(q['speed'], kin)}: {q['dt']:.2f} Galactic y (arrive "
                f"y{q['arrival']:.2f}), {q['dtau']:.2f} ship y; {energy}; wages "
                f"{_money(q['wages'])}{total}{spoil}" + (f"  [{', '.join(flags)}]" if flags else "")
            )
        return "\n".join(out)
    if tool == "depart":
        return (
            f"Departed for {res['destination']} at {_speed(res['speed'], kin)}: paid energy "
            f"{_money(res['energy_cost'])} + wages {_money(res['wages'])}; cash "
            f"{_money(res['cash'])}. Arrival Galactic year {res['arrival']:.2f} (ship clock "
            f"+{res['dtau']:.2f} y). Your turn is over."
        )
    if tool == "wait":
        return f"Staying docked until Galactic year {res['until']:.2f}. Your turn is over."
    if tool == "send_message":
        return f"Message to {res['to']} sent from {station}."
    if tool == "write_notebook":
        return f"Notebook saved ({res['chars']} chars)."
    return str(res)
