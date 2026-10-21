"""实验分析：读 runs/*/，按对照组汇总待观察现象，输出 metrics.json、图和中文报告。

把每次出发、随后的到港、到港后在目的地的卖出串成一条"航程"，在航程上计算：
- 速度分层：按主货（按目的地预期价值计）分组的巡航速度
- 枢纽：各星站到港次数、出发速度，与能源价格的关系
- 过时信息与扎堆：意外度 = 实际卖出收入 / 按出发时看到的目的地报价算出的预期收入；
  拥挤度 = 本船在途期间其他船在同一目的地卖同一种货的次数
- 价格/信息波：短缺冲击后，把该货运往冲击星站的出发，相对冲击时刻的滞后 vs 距离
- 突袭：预警时间 = 到港时刻 − 出发消息到达目的地的时刻 = d(1/β − 1)（光速信息下）
- 双生子：终局船时 τ

用法：python -m reltrade.analysis --runs runs --out reports/<name>
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

log = logging.getLogger("reltrade.analysis")

GOODS = ("ore", "tech", "med")
ORDER = [
    "greedy-light",
    "greedy-ansible",
    "greedy-newton",
    "llm-light",
    "llm-ansible",
    "llm-newton",
]
LABEL = {
    "greedy-light": "贪心·光速",
    "greedy-ansible": "贪心·瞬时信息",
    "greedy-newton": "贪心·牛顿",
    "llm-light": "LLM·光速",
    "llm-ansible": "LLM·瞬时信息",
    "llm-newton": "LLM·牛顿（c→∞）",
}


@dataclass
class Trip:
    run: str
    merchant: str
    origin: str
    dest: str
    t_dep: float
    t_arr: float | None
    speed: float
    distance: float
    dtau: float
    cargo: dict[str, float]
    arrived: dict[str, float]
    perceived: dict[str, dict[str, float]]
    perceived_age: float
    energy_cost: float
    energy_ask: float
    wages: float
    plan: str
    sold: dict[str, tuple[float, float]] = field(default_factory=dict)  # 货 → (数量, 收入)

    @property
    def main(self) -> str:
        """主货：按目的地预期价值（看到的 bid × 数量）最大者；空船为 empty。"""
        vals = {
            g: q * (self.perceived.get(g, {}).get("bid") or 0.0)
            for g, q in self.cargo.items()
            if q > 1e-6
        }
        return max(vals, key=lambda g: vals[g]) if vals else "empty"

    def surprise(self, good: str) -> float | None:
        """实际收入 / 以出发时看到的目的地报价、同样数量算出的预期收入。"""
        if good not in self.sold or good not in self.perceived:
            return None
        q, revenue = self.sold[good]
        p = self.perceived[good]
        if q <= 1e-6 or not p.get("bid") or not p.get("stock"):
            return None
        expected = p["bid"] * p["stock"] * math.log1p(q / p["stock"])
        return revenue / expected if expected > 0 else None


@dataclass
class Run:
    path: Path
    meta: dict[str, Any]
    world: dict[str, Any]
    records: list[dict[str, Any]]
    trips: list[Trip]

    @property
    def condition(self) -> str:
        return self.meta["condition"]

    @property
    def c_info(self) -> float:
        return 1.0 if self.meta["info"] == "light" else math.inf

    def dist(self, a: str, b: str) -> float:
        return self.world["distances"][a][b]

    def lag(self, a: str, b: str) -> float:
        return 0.0 if math.isinf(self.c_info) else self.dist(a, b) / self.c_info


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    with path.open() as f:
        return [json.loads(line) for line in f if line.strip()]


def _build_trips(rid: str, records: list[dict[str, Any]]) -> list[Trip]:
    by_m: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in records:  # 同一商人的记录按发生顺序追加
        by_m[r["merchant"]].append(r)
    trips: list[Trip] = []
    for m, recs in by_m.items():
        cur: Trip | None = None
        for r in recs:
            if r["type"] == "depart":
                cur = Trip(
                    rid,
                    m,
                    r["station"],
                    r["to"],
                    r["t"],
                    None,
                    r["speed"],
                    r["distance"],
                    r["dtau"],
                    dict(r["cargo"]),
                    {},
                    r["perceived"],
                    r["perceived_age"],
                    r["energy_cost"],
                    r["energy_ask"],
                    r["wages"],
                    r.get("plan", ""),
                )
                trips.append(cur)
            elif r["type"] == "arrive" and cur is not None:
                cur.t_arr = r["t"]
                cur.arrived = dict(r["cargo"])
            elif (
                r["type"] == "trade"
                and r["side"] == "sell"
                and cur is not None
                and cur.t_arr is not None
                and r["station"] == cur.dest
            ):
                q, v = cur.sold.get(r["good"], (0.0, 0.0))
                cur.sold[r["good"]] = (q + r["qty"], v + r["value"])
    return trips


def load_runs(root: Path) -> list[Run]:
    runs = []
    for d in sorted(root.glob("*__w*__r*")):
        if not (d / "result.json").exists():
            continue
        meta = json.loads((d / "result.json").read_text())
        world = json.loads((d / "world.json").read_text())
        records = _read_jsonl(d / "records.jsonl")
        runs.append(Run(d, meta, world, records, _build_trips(meta["run_id"], records)))
    return runs


# ---------------- 每局指标 ----------------


def _gini(xs: list[float]) -> float:
    xs = sorted(max(x, 0.0) for x in xs)
    n, total = len(xs), sum(xs)
    if n == 0 or total == 0:
        return 0.0
    return sum((2 * i - n - 1) * x for i, x in enumerate(xs, 1)) / (n * total)


def _spearman(x: list[float], y: list[float]) -> float | None:
    if len(x) < 5 or len(set(x)) < 2 or len(set(y)) < 2:
        return None
    rx = np.argsort(np.argsort(x)).astype(float)
    ry = np.argsort(np.argsort(y)).astype(float)
    return float(np.corrcoef(rx, ry)[0, 1])


def run_metrics(run: Run) -> dict[str, Any]:
    final = run.meta["final"]
    nets = [f["net_worth"] for f in final.values()]
    trips = [t for t in run.trips if t.t_arr is not None]
    out: dict[str, Any] = {
        "run_id": run.meta["run_id"],
        "condition": run.condition,
        "world_seed": run.meta["world_seed"],
        "replicate": run.meta["replicate"],
        "net_mean": statistics.mean(nets),
        "net_median": statistics.median(nets),
        "net_min": min(nets),
        "net_max": max(nets),
        "gini": _gini(nets),
        "bankrupt": sum(1 for r in run.records if r["type"] == "bankrupt"),
        "trips": len(run.trips),
        "empty_trips": sum(1 for t in run.trips if t.main == "empty"),
        "tau_mean": statistics.mean(f["tau"] for f in final.values()),
        "waits": sum(1 for r in run.records if r["type"] == "wait"),
        "forced_waits": sum(1 for r in run.records if r["type"] == "wait" and r["forced"]),
    }
    # 速度分层
    by_good: dict[str, list[float]] = defaultdict(list)
    for t in run.trips:
        by_good[t.main].append(t.speed)
    out["speed_by_good"] = {g: statistics.median(v) for g, v in by_good.items()}
    out["speed_n"] = {g: len(v) for g, v in by_good.items()}
    med_share = [
        t.cargo.get("med", 0)
        * (t.perceived.get("med", {}).get("bid") or 0)
        / max(sum(q * (t.perceived.get(g, {}).get("bid") or 0) for g, q in t.cargo.items()), 1e-9)
        for t in run.trips
        if t.cargo
    ]
    loaded = [t for t in run.trips if t.cargo]
    out["rho_speed_medshare"] = _spearman(med_share, [t.speed for t in loaded])
    out["rho_speed_energy"] = _spearman(
        [t.energy_ask for t in run.trips], [t.speed for t in run.trips]
    )
    # 枢纽
    arrivals = Counter(r["station"] for r in run.records if r["type"] == "arrive")
    out["port_calls"] = {s: arrivals.get(s, 0) for s in run.world["stations"]}
    out["departures"] = dict(Counter(t.origin for t in run.trips))
    # 意外度与拥挤
    sells = [r for r in run.records if r["type"] == "trade" and r["side"] == "sell"]
    surprises, crowd_pairs = [], []
    for t in trips:
        s = t.surprise(t.main) if t.main != "empty" else None
        if s is None:
            continue
        crowd = sum(
            1
            for r in sells
            if r["merchant"] != t.merchant
            and r["station"] == t.dest
            and r["good"] == t.main
            and t.t_dep < r["t"] <= (t.t_arr or 0)
        )
        surprises.append(s)
        crowd_pairs.append((crowd, s))
    out["surprise"] = surprises
    out["surprise_mean"] = statistics.mean(surprises) if surprises else None
    out["surprise_bad"] = (
        sum(1 for s in surprises if s < 0.8) / len(surprises) if surprises else None
    )
    out["crowd_pairs"] = crowd_pairs
    out["rho_crowd_surprise"] = _spearman([c for c, _ in crowd_pairs], [s for _, s in crowd_pairs])
    out["info_age_mean"] = statistics.mean(t.perceived_age for t in run.trips) if run.trips else 0
    # 冲击响应（事件研究）：短缺冲击 (A, g, t0) × 每个出发站 S，
    # 记录 S→A 且载有 g 的出发，相对冲击时刻 t0 和相对消息到达 S 的时刻 t0 + d/c 各差多少
    responses, pairs = [], 0
    for sh in run.world["shocks"]:
        if sh["kind"] != "shortage":
            continue
        a, g, t0 = sh["station"], sh["good"], sh["t_start"]
        for origin in run.world["stations"]:
            if origin == a:
                continue
            pairs += 1
            t_info = t0 + run.lag(origin, a)
            for t in run.trips:
                if t.origin == origin and t.dest == a and t.cargo.get(g, 0) > 1e-6:
                    responses.append(
                        {
                            "station": a,
                            "good": g,
                            "origin": origin,
                            "distance": run.dist(origin, a),
                            "rel_shock": t.t_dep - t0,
                            "rel_info": t.t_dep - t_info,
                        }
                    )
    out["shock_responses"] = responses
    out["shock_pairs"] = pairs
    # 突袭：出发消息到达目的地之后多久，船就到了
    warnings = [
        {
            "speed": t.speed,
            "warning": (t.t_arr or 0) - t.t_dep - run.lag(t.origin, t.dest),
            "surprise": t.surprise(t.main) if t.main != "empty" else None,
        }
        for t in trips
    ]
    out["warnings"] = warnings
    out["raid_share"] = (
        sum(1 for w in warnings if w["warning"] < 0.5) / len(warnings) if warnings else 0.0
    )
    # 通信
    msgs = [r for r in run.records if r["type"] == "message"]
    out["messages"] = len(msgs)
    out["broadcasts"] = sum(1 for m in msgs if m["to"] == "all")
    out["message_samples"] = [
        {"t": m["t"], "from": m["merchant"], "at": m["station"], "to": m["to"], "text": m["text"]}
        for m in msgs
    ]
    out["tau"] = {n: f["tau"] for n, f in final.items()}
    out["net"] = {n: f["net_worth"] for n, f in final.items()}
    llm = run.meta.get("llm") or {}
    if llm:
        out["llm"] = {
            k: sum(s.get(k, 0) for s in llm.values())
            for k in (
                "calls",
                "cache_hits",
                "cost_usd",
                "prompt_tokens",
                "cached_tokens",
                "completion_tokens",
                "reasoning_tokens",
                "tool_calls",
                "tool_errors",
                "nudges",
                "turns_exhausted",
            )
        }
    out["wall_s"] = run.meta.get("wall_s")
    out["max_parallel"] = run.meta.get("max_parallel")
    return out


# ---------------- 汇总 ----------------


def _ms(xs: list[float]) -> dict[str, float | None]:
    xs = [x for x in xs if x is not None]
    if not xs:
        return {"mean": None, "sd": None, "n": 0}
    return {
        "mean": statistics.mean(xs),
        "sd": statistics.stdev(xs) if len(xs) > 1 else 0.0,
        "n": len(xs),
    }


def summarize(per_run: list[dict[str, Any]]) -> dict[str, Any]:
    by_c: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for m in per_run:
        by_c[m["condition"]].append(m)
    out = {}
    for c, ms in by_c.items():
        pooled_speed: dict[str, list[float]] = defaultdict(list)
        for m in ms:
            for g, v in m["speed_by_good"].items():
                pooled_speed[g].append(v)
        port = Counter()
        for m in ms:
            port.update(m["port_calls"])
        total_calls = sum(port.values()) or 1
        out[c] = {
            "runs": len(ms),
            "worlds": sorted({m["world_seed"] for m in ms}),
            "net_mean": _ms([m["net_mean"] for m in ms]),
            "gini": _ms([m["gini"] for m in ms]),
            "trips": _ms([m["trips"] for m in ms]),
            "speed_by_good": {g: _ms(v) for g, v in pooled_speed.items()},
            "surprise_mean": _ms([m["surprise_mean"] for m in ms]),
            "surprise_bad": _ms([m["surprise_bad"] for m in ms]),
            "rho_crowd_surprise": _ms([m["rho_crowd_surprise"] for m in ms]),
            "rho_speed_energy": _ms([m["rho_speed_energy"] for m in ms]),
            "port_share": {s: n / total_calls for s, n in port.items()},
            "raid_share": _ms([m["raid_share"] for m in ms]),
            "messages": _ms([m["messages"] for m in ms]),
            "tau_mean": _ms([m["tau_mean"] for m in ms]),
            "bankrupt": sum(m["bankrupt"] for m in ms),
            "cost_usd": sum((m.get("llm") or {}).get("cost_usd", 0.0) for m in ms),
        }
    return out


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--runs", default="runs")
    p.add_argument("--out", required=True)
    p.add_argument("--world", type=int, default=None, help="only runs on this world seed")
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    runs = load_runs(Path(args.runs))
    if args.world is not None:
        runs = [r for r in runs if r.meta["world_seed"] == args.world]
    per_run = [run_metrics(r) for r in runs]
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    summary = summarize(per_run)
    (out / "metrics.json").write_text(
        json.dumps({"summary": summary, "runs": per_run}, indent=1, ensure_ascii=False)
    )
    from reltrade.report import write_report  # matplotlib 只在出报告时加载

    write_report(out, runs, per_run, summary)
    log.info("analyzed %d runs → %s", len(runs), out)


if __name__ == "__main__":
    main()
