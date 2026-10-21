import asyncio
import json
import math
from dataclasses import replace
from pathlib import Path

import pytest

from reltrade.analysis import (
    Dock,
    concepts,
    excerpt,
    load_docks,
    load_runs,
    run_metrics,
    summarize,
)
from reltrade.experiment import run_one
from reltrade.report import write_report

pytestmark = pytest.mark.integration
NAMES = ("Asel", "Bram", "Chen", "Dara", "Emre", "Faye")


@pytest.mark.unit
def test_concepts_tag_reasoning_text() -> None:
    text = (
        "The Sol quote is 8.3 y old, so by the time I arrive the price may have recovered. "
        "At 0.99c gamma is 7.09 and med survives 80%. Chen is heading there too. "
        "Energy shortage at Sirius in the news. I could bluff in a broadcast."
    )
    assert concepts(text, "Bram", NAMES) == {
        "stale",
        "forecast",
        "dilation",
        "spoil",
        "rivals",
        "news",
        "deceive",
    }
    assert concepts("Buy ore here, sell it at Sirius.", "Bram", NAMES) == set()
    assert concepts("I, Bram, sell ore.", "Bram", NAMES) == set()  # 提到自己不算对手
    assert concepts("Propose a cartel: let's coordinate prices.", "Bram", NAMES) == {"cooperate"}
    # 常见误报：形容计算"误导人"、把星站叫作贸易"伙伴"
    assert concepts("The naive calc is misleading because of the curve.", "Bram", NAMES) == set()
    assert concepts("Lalande is the right partner for Wolf.", "Bram", NAMES) == set()
    assert concepts("I could send misleading messages.", "Bram", NAMES) == {"deceive"}


@pytest.mark.unit
def test_excerpt_returns_the_matching_sentence() -> None:
    text = "Sell first. The Wolf data is 4.1 years old, so it may be stale now! Then buy."
    assert excerpt(text, "stale") == "The Wolf data is 4.1 years old, so it may be stale now!"
    assert excerpt(text, "dilation") is None


@pytest.mark.unit
def test_load_docks_collects_assistant_text_only(tmp_path: Path) -> None:
    (tmp_path / "transcripts").mkdir()
    dock = {
        "report": 1,
        "t": 2.5,
        "station": "Sol",
        "messages": [
            {"role": "user", "content": "REPORT #1 — data 8.3 y old"},
            {
                "role": "assistant",
                "content": "Going fast.",
                "reasoning_content": "gamma is large",
                "tool_calls": [
                    {"function": {"name": "write_notebook", "arguments": '{"text": "a.\\nb"}'}},
                    {"function": {"name": "depart", "arguments": '{"plan": "beat Chen"}'}},
                ],
            },
            {"role": "tool", "content": "Departed. 3 y old news"},
        ],
    }
    (tmp_path / "transcripts" / "Bram.jsonl").write_text(json.dumps(dock) + "\n")
    [d] = load_docks(tmp_path)
    assert (d.merchant, d.t, d.station) == ("Bram", 2.5, "Sol")
    assert "gamma is large" in d.text and "beat Chen" in d.text and "Going fast." in d.text
    assert "a.\nb" in d.text  # 工具参数按 JSON 解码，换行是真换行
    assert "REPORT" not in d.text and "Departed" not in d.text
    assert concepts(d.text, d.merchant, NAMES) == {"dilation", "rivals"}


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
    assert "## 总览" in text and "贪心·光速" in text and "## 行为" in text
    assert "## 推理里提到了什么" not in text  # 贪心组没有 transcript
    for fig in (
        "fig_networth.png",
        "fig_speed.png",
        "fig_ports.png",
        "fig_surprise.png",
        "fig_shock.png",
        "fig_warning.png",
    ):
        assert (tmp_path / fig).stat().st_size > 10_000


def test_report_renders_reasoning_section(runs_dir: Path, tmp_path: Path) -> None:
    base = next(r for r in load_runs(runs_dir) if r.condition == "greedy-light")
    said = "Sirius is 8.6 ly away, so its quote is 8.6 years old."
    fake = replace(
        base,
        meta={**base.meta, "condition": "llm-light"},
        docks=[Dock("Asel", 1.0, "Sol", f"Hmm. {said} Gamma is 7 at 0.99c.")],
    )
    per_run = [run_metrics(r) for r in (base, fake)]
    assert per_run[1]["concepts"]["stale"] == 1.0 and per_run[1]["concepts"]["spoil"] == 0.0
    write_report(tmp_path, [base, fake], per_run, summarize(per_run))
    text = (tmp_path / "report.md").read_text()
    assert "## 推理里提到了什么" in text
    assert f"**信息过时 / 光速延迟** · Asel y1.0 @ Sol：{said}" in text
