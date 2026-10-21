import asyncio
import math
from pathlib import Path

import pytest

from reltrade.analysis import load_runs, run_metrics, summarize
from reltrade.experiment import run_one
from reltrade.report import write_report

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def runs_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("runs")
    for cond in ("greedy-light", "greedy-ansible"):
        asyncio.run(run_one(cond, 3, 0, out_root=root, client=None, t_end=60.0))
    return root


def test_trips_link_departures_arrivals_and_sales(runs_dir: Path) -> None:
    runs = load_runs(runs_dir)
    assert {r.condition for r in runs} == {"greedy-light", "greedy-ansible"}
    for run in runs:
        departs = [r for r in run.records if r["type"] == "depart"]
        assert len(run.trips) == len(departs)
        arrived = [t for t in run.trips if t.t_arr is not None]
        assert arrived
        for t in arrived:
            assert t.t_arr == pytest.approx(t.t_dep + t.distance / t.speed)
            for g, q in t.arrived.items():
                assert q <= t.cargo.get(g, 0.0) + 1e-9  # 只会腐败，不会变多
        # 贪心到港即卖光主货：意外度可算，且与公式一致
        t = next(t for t in arrived if t.main != "empty" and t.main in t.sold)
        q, revenue = t.sold[t.main]
        p = t.perceived[t.main]
        expected = p["bid"] * p["stock"] * math.log1p(q / p["stock"])
        assert t.surprise(t.main) == pytest.approx(revenue / expected)


def test_shock_responses_respect_light_cone(runs_dir: Path) -> None:
    runs = {r.condition: r for r in load_runs(runs_dir)}
    m = run_metrics(runs["greedy-light"])
    assert m["shock_pairs"] > 0
    for resp in m["shock_responses"]:
        assert resp["rel_info"] == pytest.approx(resp["rel_shock"] - resp["distance"])
    inst = run_metrics(runs["greedy-ansible"])
    for resp in inst["shock_responses"]:
        assert resp["rel_info"] == pytest.approx(resp["rel_shock"])


def test_light_warning_is_d_times_inverse_beta_minus_one(runs_dir: Path) -> None:
    run = next(r for r in load_runs(runs_dir) if r.condition == "greedy-light")
    m = run_metrics(run)
    trips = [t for t in run.trips if t.t_arr is not None]
    for w, t in zip(m["warnings"], trips, strict=True):
        assert w["warning"] == pytest.approx(t.distance * (1 / t.speed - 1))


def test_report_renders(runs_dir: Path, tmp_path: Path) -> None:
    runs = load_runs(runs_dir)
    per_run = [run_metrics(r) for r in runs]
    write_report(tmp_path, runs, per_run, summarize(per_run))
    text = (tmp_path / "report.md").read_text()
    assert "## 总览" in text and "贪心·光速" in text
    for fig in ("fig_networth.png", "fig_speed.png", "fig_ports.png", "fig_surprise.png",
                "fig_shock.png", "fig_warning.png"):
        assert (tmp_path / fig).stat().st_size > 10_000
