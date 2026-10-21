"""实验运行器：按对照组 × 世界 seed × 重复编号跑模拟，产物写进 runs/<run_id>/。

对照组（handoff 的三组 + 一组拆分）：
  greedy-light   贪心 + 正常 c（光速信息、相对论运动学）
  llm-light      LLM  + 正常 c
  llm-newton     LLM  + c→∞（瞬时信息、牛顿运动学：无光速上限、无时间膨胀、E = mv²）
  llm-ansible    LLM  + 瞬时信息、但运动学仍是相对论（把"信息延迟"和"运动学"两半拆开）
  greedy-ansible / greedy-newton  贪心对应组（便宜，可多 seed）

LLM 组需要在 `go.sh exec --` 下运行以注入 OPENAI_* / OPENCODE_GO_SESSION。
已完成的 run（有 result.json）默认跳过；中断的 run 重跑时，已缓存的 LLM 响应会被确定性重放。

用法：
  python -m reltrade.experiment --conditions greedy-light,greedy-newton --seeds 1-20
  go.sh exec -- python -m reltrade.experiment --conditions llm-light --seeds 7 --replicates 3
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import logging
import math
import shutil
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from reltrade import __version__
from reltrade.agents.greedy import NEWTON_SPEEDS, REL_SPEEDS, GreedyMerchant
from reltrade.agents.llm import LLMMerchant
from reltrade.engine import Agent, Engine
from reltrade.llm_client import AuthError, LLMClient, QuotaError
from reltrade.physics import INSTANT, LIGHT, kinematics_by_name
from reltrade.world import Rules, make_world

log = logging.getLogger("reltrade.experiment")
ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class Condition:
    agent: str  # greedy | llm
    kinematics: str  # relativistic | newtonian
    info: str  # light | instant

    @property
    def c_info(self) -> float:
        return LIGHT if self.info == "light" else INSTANT


CONDITIONS: dict[str, Condition] = {
    "greedy-light": Condition("greedy", "relativistic", "light"),
    "greedy-ansible": Condition("greedy", "relativistic", "instant"),
    "greedy-newton": Condition("greedy", "newtonian", "instant"),
    "llm-light": Condition("llm", "relativistic", "light"),
    "llm-ansible": Condition("llm", "relativistic", "instant"),
    "llm-newton": Condition("llm", "newtonian", "instant"),
}


def parse_range(spec: str) -> list[int]:
    out: list[int] = []
    for part in spec.split(","):
        if "-" in part:
            a, b = part.split("-")
            out += list(range(int(a), int(b) + 1))
        elif part:
            out.append(int(part))
    return out


def run_id(condition: str, seed: int, rep: int) -> str:
    return f"{condition}__w{seed}__r{rep}"


def _json_default(o: Any) -> Any:
    if isinstance(o, float) and math.isinf(o):
        return "inf"
    if isinstance(o, set):
        return sorted(o)
    raise TypeError(type(o))


def _dump(path: Path, obj: Any) -> None:
    path.write_text(json.dumps(obj, indent=1, ensure_ascii=False, default=_json_default))


def _write_outputs(
    run_dir: Path, engine: Engine, meta: dict[str, Any], agents: dict[str, Agent]
) -> None:
    w = engine.world
    _dump(
        run_dir / "world.json",
        {
            "seed": w.seed,
            "rules": asdict(w.rules),
            "starts": w.starts,
            "stations": {
                s: {
                    "pos": spec.pos,
                    "blurb": spec.blurb,
                    "markets": {g: asdict(m) for g, m in spec.markets.items()},
                }
                for s, spec in w.stations.items()
            },
            "distances": {a: {b: w.distance(a, b) for b in w.stations} for a in w.stations},
            "shocks": [asdict(sh) for sh in w.shocks],
        },
    )
    with (run_dir / "events.jsonl").open("w") as f:
        for e in engine.events.all():
            f.write(json.dumps(e.to_json(), ensure_ascii=False) + "\n")
    with (run_dir / "records.jsonl").open("w") as f:
        for r in engine.records:
            f.write(json.dumps(r, ensure_ascii=False, default=_json_default) + "\n")
    _dump(run_dir / "stations.json", {s: m.snapshots() for s, m in engine.markets.items()})
    # 等间隔采样的价格场，供分析直接用
    with (run_dir / "prices.csv").open("w", newline="") as f:
        out = csv.writer(f)
        out.writerow(["t", "station", "good", "bid", "ask", "stock", "normal"])
        steps = int(w.rules.t_end / 0.25)
        for k in range(steps + 1):
            t = k * 0.25
            for s, m in engine.markets.items():
                for g, q in m.board_at(t).quotes.items():
                    out.writerow(
                        [
                            f"{t:.2f}",
                            s,
                            g,
                            "" if q.bid is None else f"{q.bid:.4f}",
                            f"{q.ask:.4f}",
                            f"{q.stock:.3f}",
                            f"{q.normal:.1f}",
                        ]
                    )
    llm_stats = {n: a.stats for n, a in agents.items() if isinstance(a, LLMMerchant)}
    _dump(
        run_dir / "result.json",
        {**meta, "final": engine.final, "llm": llm_stats, "max_parallel": engine.max_parallel},
    )


async def run_one(
    condition: str,
    seed: int,
    rep: int,
    *,
    out_root: Path,
    client: LLMClient | None,
    t_end: float,
    force: bool = False,
) -> Path:
    cond = CONDITIONS[condition]
    rid = run_id(condition, seed, rep)
    run_dir = out_root / rid
    if (run_dir / "result.json").exists() and not force:
        log.info("skip %s (done)", rid)
        return run_dir
    if run_dir.exists():
        shutil.rmtree(run_dir)  # 从头重放（LLM 响应来自缓存）
    run_dir.mkdir(parents=True)
    run_log = logging.getLogger(f"reltrade.run.{rid}")
    handler = logging.FileHandler(run_dir / "run.log")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    run_log.addHandler(handler)

    world = make_world(seed, Rules(t_end=t_end))
    kin = kinematics_by_name(cond.kinematics)
    agents: dict[str, Agent] = {}
    for name in world.merchant_names:
        if cond.agent == "greedy":
            agents[name] = GreedyMerchant(
                REL_SPEEDS if kin.name == "relativistic" else NEWTON_SPEEDS
            )
        else:
            assert client is not None, "LLM conditions need an LLM client"
            agents[name] = LLMMerchant(
                name,
                client,
                world,
                kin,
                cond.c_info,
                tag=f"{rid}",
                transcript=run_dir / "transcripts" / f"{name}.jsonl",
                logger=run_log,
            )
    engine = Engine(world, agents, c_info=cond.c_info, kin=kin, label=rid, logger=run_log)
    meta: dict[str, Any] = {
        "run_id": rid,
        "condition": condition,
        **asdict(cond),
        "world_seed": seed,
        "replicate": rep,
        "t_end": t_end,
        "version": __version__,
        "model": client.model if client and cond.agent == "llm" else None,
        "temperature": client.temperature if client and cond.agent == "llm" else None,
        "started": datetime.now(UTC).isoformat(),
    }
    t0 = time.monotonic()
    try:
        await engine.run()
    finally:
        run_log.removeHandler(handler)
        handler.close()
    meta["wall_s"] = time.monotonic() - t0
    meta["finished"] = datetime.now(UTC).isoformat()
    _write_outputs(run_dir, engine, meta, agents)
    nets = [f["net_worth"] for f in engine.final.values()]
    cost = sum(a.stats["cost_usd"] for a in agents.values() if isinstance(a, LLMMerchant))
    log.info(
        "done %s in %.0fs: mean net worth %.0f (min %.0f, max %.0f); LLM cost $%.3f",
        rid,
        meta["wall_s"],
        sum(nets) / len(nets),
        min(nets),
        max(nets),
        cost,
    )
    return run_dir


async def main_async(args: argparse.Namespace) -> int:
    conditions = [c.strip() for c in args.conditions.split(",") if c.strip()]
    unknown = [c for c in conditions if c not in CONDITIONS]
    if unknown:
        raise SystemExit(f"unknown conditions {unknown}; choose from {list(CONDITIONS)}")
    needs_llm = any(CONDITIONS[c].agent == "llm" for c in conditions)
    client = None
    if needs_llm:
        client = LLMClient(
            args.model,
            Path(args.cache),
            temperature=args.temperature,
            max_tokens=args.max_tokens,
            concurrency=args.concurrency,
            offline=args.offline,
        )
    reps = parse_range(args.reps) if args.reps else list(range(args.replicates))
    jobs = [(c, s, r) for c in conditions for s in parse_range(args.seeds) for r in reps]
    sem = asyncio.Semaphore(args.parallel_runs)
    failures: list[str] = []

    async def guarded(c: str, s: int, r: int) -> None:
        async with sem:
            try:
                await run_one(
                    c,
                    s,
                    r,
                    out_root=Path(args.out),
                    client=client,
                    t_end=args.t_end,
                    force=args.force,
                )
            except (AuthError, QuotaError):
                raise
            except Exception:
                log.exception("run %s failed", run_id(c, s, r))
                failures.append(run_id(c, s, r))

    try:
        await asyncio.gather(*(guarded(c, s, r) for c, s, r in jobs))
    except AuthError as e:
        log.error("API key rejected (%s). Stop: ask the user for a new OPENCODE_GO_API_KEY.", e)
        return 3
    except QuotaError as e:
        log.error("quota exhausted: %s", e)
        return 4
    finally:
        if client is not None:
            log.info("LLM usage: %s; est. cost $%.3f", dict(client.usage), client.cost_usd)
            await client.aclose()
    if failures:
        log.error("%d runs failed: %s", len(failures), failures)
        return 1
    return 0


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--conditions", required=True)
    p.add_argument("--seeds", default="7")
    p.add_argument("--replicates", type=int, default=1)
    p.add_argument(
        "--reps", default="", help="explicit replicate indices, e.g. 1-2 (overrides --replicates)"
    )
    p.add_argument("--t-end", type=float, default=Rules().t_end)
    p.add_argument("--model", default="deepseek-v4.1-flash")
    p.add_argument("--temperature", type=float, default=1.0)
    p.add_argument("--max-tokens", type=int, default=32768)
    p.add_argument("--concurrency", type=int, default=12, help="max concurrent LLM requests")
    p.add_argument("--parallel-runs", type=int, default=8)
    p.add_argument("--out", default=str(ROOT / "runs"))
    p.add_argument("--cache", default=str(ROOT / "cache" / "llm"))
    p.add_argument("--force", action="store_true", help="rerun even if result.json exists")
    p.add_argument("--offline", action="store_true", help="replay from cache only")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    raise SystemExit(asyncio.run(main_async(args)))


if __name__ == "__main__":
    main()
