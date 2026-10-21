import asyncio
import json
import math
from dataclasses import replace
from pathlib import Path

import pytest

from reltrade.analysis import (
    RESPONSE_WINDOW,
    WAVE_WINDOW,
    Dock,
    Run,
    arrival_surprise,
    audit_precision,
    audit_sample,
    concepts,
    crossing_gain,
    excerpt,
    load_docks,
    load_runs,
    rebuild_markets,
    run_metrics,
    summarize,
    tau_at_end,
    write_audit,
)
from reltrade.engine import DecisionContext
from reltrade.experiment import run_one
from reltrade.report import _kilo, _plane, fig_ghosts, write_report
from reltrade.world import make_world

pytestmark = pytest.mark.integration
NAMES = ("Asel", "Bram", "Chen", "Dara", "Emre", "Faye")


@pytest.mark.unit
def test_concepts_tag_reasoning_text() -> None:
    text = (
        "The Sol quote is 8.3 y old, so by the time I arrive the price may have recovered. "
        "At 0.99c gamma is 7.09, so ship time is short and med survives 80%. Chen goes there too. "
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
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # 时间膨胀：要「船时」和「γ / 银河时」同句出现，或明说 time dilation
        ("At 0.99c: 11.04 galactic y but only 1.56 ship y.", {"dilation"}),
        ("Ship time = d/(βγ) = 1.34 y.", {"dilation"}),
        ("Time dilation saves wages.", {"dilation"}),
        ("Energy cost = 2(γ−1) × mass; γ at 0.9 = 2.294.", set()),  # 能量公式里的 γ
        ("Energy 2(γ-1) × 1.1, ship time 1.3 y.", set()),
        ("Wages are 20¢ per ship-year.", set()),  # 牛顿组也会这么说
        ("Med spoils by ship time: half every 3 years.", {"spoil"}),
        ("Slower = MORE spoilage (longer ship time).", {"spoil"}),  # 牛顿组里也成立
        ("Faster: 4.3 galactic y, but just 1.3 ship y.", {"dilation"}),
        # 合作：要和对手或消息同句出现
        ("The tech route is far better if prices cooperate.", set()),
        ("Use the triangle Lalande-Wolf-Sol for med and ore, coordinating.", set()),
        ("Coordinating with rivals could help.", {"cooperate", "rivals"}),
        ("Maybe send a message to coordinate routes.", {"cooperate"}),
        # 冲击：港口动态不算新闻冲击
        ("From news, who is heading to Lalande?", set()),
        ("Epidemic at Sirius: med bid 120.", {"news"}),
    ],
)
def test_concept_rules_on_audited_false_positives(text: str, expected: set[str]) -> None:
    assert concepts(text, "Bram", NAMES) == expected


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
                "reasoning_content": "gamma is large, so ship time is short",
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


def test_cost_structure_adds_up_to_net_worth(runs_dir: Path) -> None:
    # 终局都停靠时：净值 = 起始现金 + 交易毛利 − 能源 − 工资 × 船时 + 终局货值，逐项相等
    for run in load_runs(runs_dir):
        m, rules, final = run_metrics(run), run.world["rules"], run.meta["final"]
        assert not any(f["in_flight"] for f in final.values())
        cargo = sum(f["cargo_value"] for f in final.values()) / len(final)
        wages = rules["wage"] * m["tau_mean"]
        expected = rules["start_cash"] + m["gross"] - m["energy"] - wages + cargo
        assert m["net_mean"] == pytest.approx(expected)
        assert 0 < m["trip_wages"] <= wages


def test_arrival_surprise_equals_realized_when_sold_on_arrival(runs_dir: Path) -> None:
    """贪心到港即卖光：只要同一时刻没有别人在该站成交，按到港报价算的意外度就等于实际意外度。"""
    run = next(r for r in load_runs(runs_dir) if r.condition == "greedy-light")
    markets = rebuild_markets(run)
    traders: dict[tuple[str, float], set[str]] = {}
    for r in run.records:
        if r["type"] == "trade":
            traders.setdefault((r["station"], r["t"]), set()).add(r["merchant"])
    checked = 0
    for t in run.trips:
        if t.t_arr is None or t.main == "empty" or t.main not in t.sold:
            continue
        sold_q, _ = t.sold[t.main]
        if abs(sold_q - t.arrived[t.main]) > 1e-9 or traders[(t.dest, t.t_arr)] != {t.merchant}:
            continue
        assert arrival_surprise(t, markets) == pytest.approx(t.surprise(t.main), rel=1e-9)
        checked += 1
    assert checked > 20
    m = run_metrics(run)
    assert m["unsold_share"] == 0.0  # 贪心总是卖光


def test_rebuild_refuses_a_changed_world(runs_dir: Path) -> None:
    run = load_runs(runs_dir)[0]
    run.world["shocks"] = run.world["shocks"][1:]
    with pytest.raises(ValueError, match="world generator changed"):
        rebuild_markets(run)


def test_shock_responses_respect_light_cone(runs_dir: Path) -> None:
    runs = {r.condition: r for r in load_runs(runs_dir)}
    m = run_metrics(runs["greedy-light"])
    assert m["shock_pairs"] > 0
    for resp in m["shock_responses"]:
        assert resp["rel_info"] == pytest.approx(resp["rel_shock"] - resp["distance"])
    inst = run_metrics(runs["greedy-ansible"])
    for resp in inst["shock_responses"]:
        assert resp["rel_info"] == pytest.approx(resp["rel_shock"])
    # 追冲击：收到消息后 RESPONSE_WINDOW 年内出发的那部分
    shortages = [sh for sh in runs["greedy-light"].world["shocks"] if sh["kind"] == "shortage"]
    chasers = [r for r in m["shock_responses"] if 0 <= r["rel_info"] <= RESPONSE_WINDOW]
    assert m["chase_per_shock"] == pytest.approx(len(chasers) / len(shortages))
    if chasers:
        assert m["chase_in_time"] == pytest.approx(
            sum(r["in_time"] for r in chasers) / len(chasers)
        )
    # 光速下，追击航程从冲击发生到出发至少隔了消息传过来的 d/c
    for r in chasers:
        assert r["rel_shock"] >= r["distance"] - 1e-9


def test_wave_counts_purchases_around_the_news_front(runs_dir: Path) -> None:
    runs = list(load_runs(runs_dir))
    per_run = [run_metrics(r) for r in runs]
    for run, m in zip(runs, per_run, strict=True):
        assert m["wave"]
        buys = [r for r in run.records if r["type"] == "trade" and r["side"] == "buy"]
        for w in m["wave"]:  # 消息到达该站的时刻：光速 t0 + d/c，瞬时信息 t0
            assert w["front"] == pytest.approx(
                w["t_shock"] + run.lag(w["shock_station"], w["station"])
            )
            q = [
                (r["t"], r["qty"])
                for r in buys
                if r["station"] == w["station"] and r["good"] == w["good"]
            ]
            f = w["front"]
            assert w["pre"] == pytest.approx(sum(x for t, x in q if f - WAVE_WINDOW <= t < f))
            assert w["post"] == pytest.approx(sum(x for t, x in q if f <= t < f + WAVE_WINDOW))
            assert 0 <= w["to_shock"] <= w["post"] + 1e-9
    s = summarize(per_run)
    for c in s:
        diffs = [w["post"] - w["pre"] for m in per_run if m["condition"] == c for w in m["wave"]]
        assert s[c]["wave_extra"]["n"] == len(diffs)
        assert s[c]["wave_extra"]["mean"] == pytest.approx(sum(diffs) / len(diffs))


def test_light_warning_is_d_times_inverse_beta_minus_one(runs_dir: Path) -> None:
    run = next(r for r in load_runs(runs_dir) if r.condition == "greedy-light")
    m = run_metrics(run)
    trips = [t for t in run.trips if t.t_arr is not None]
    for w, t in zip(m["warnings"], trips, strict=True):
        assert w["warning"] == pytest.approx(t.distance * (1 / t.speed - 1))


class _Endgame:
    """脚本商人：开局买 ore；偶数号直接去最远的站，奇数号先去最近的站、到港不卖再去最远的站。
    两种都在终局之后才到，分别覆盖「本站买进」和「带过来的货」两条清算路径。"""

    def __init__(self, *_: object) -> None:
        self.docks = 0

    async def decide(self, ctx: DecisionContext) -> None:
        self.docks += 1
        w = ctx.world
        others = sorted(
            (w.distance(ctx.station, s), s) for s in w.station_names if s != ctx.station
        )
        if self.docks == 1:
            ctx.buy("ore", min(40.0, 0.2 * ctx.local_board().quotes["ore"].stock))
        if self.docks == 1 and w.merchant_names.index(ctx.m.name) % 2:
            ctx.depart(others[0][1], 0.9)
        else:
            ctx.depart(others[-1][1], 0.5)


@pytest.fixture(scope="module")
def endgame_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("endgame")
    mp = pytest.MonkeyPatch()
    mp.setattr("reltrade.experiment.GreedyMerchant", _Endgame)
    try:
        asyncio.run(run_one("greedy-light", 3, 0, out_root=root, client=None, t_end=12.0))
    finally:
        mp.undo()
    return root


def test_crossing_gain_restores_the_stay_in_port_counterfactual(endgame_dir: Path) -> None:
    (run,) = load_runs(endgame_dir)
    markets = rebuild_markets(run)
    gain = crossing_gain(run, markets)
    rules, t_end = run.world["rules"], run.meta["t_end"]
    expected = []
    for i, (n, f) in enumerate(run.meta["final"].items()):
        assert f["in_flight"]
        recs = [r for r in run.records if r["merchant"] == n]
        deps = [r for r in recs if r["type"] == "depart"]
        if i % 2 == 0:  # 开局买货直接出发：反事实就是拿着起始现金在港口等到终局
            alt = rules["start_cash"] - rules["wage"] * t_end
        else:  # 第二站带着没卖的 ore 出发：反事实是在那里卖掉，在港口等到终局
            (buy,) = [r for r in recs if r["type"] == "trade"]
            first, last = deps
            liq = markets[last["station"]].quote_sell(last["t"], "ore", last["cargo"]["ore"])
            alt = (
                rules["start_cash"]
                - buy["value"]
                - first["energy_cost"]
                - first["wages"]
                + liq
                - rules["wage"] * (t_end - last["t"])
            )
        assert f["net_worth"] - gain[n] == pytest.approx(alt)
        expected.append(alt)
    m = run_metrics(run)
    assert m["in_flight_share"] == 1.0
    assert m["net_alt_mean"] == pytest.approx(sum(expected) / len(expected))


def test_tau_at_end_interpolates_ships_still_in_flight(endgame_dir: Path) -> None:
    # 引擎给终局仍在飞的商人记到港时的船时（货按到港状态清算）；双生子比的是 t_end 那一刻
    (run,) = load_runs(endgame_dir)
    tau, t_end = tau_at_end(run), run.meta["t_end"]
    for i, (n, f) in enumerate(run.meta["final"].items()):
        assert tau[n] < f["tau"]
        if i % 2 == 0:  # t=0 起以 0.5c 一直飞过终局
            assert tau[n] == pytest.approx(t_end * math.sqrt(1 - 0.5**2))
    m = run_metrics(run)
    assert m["tau"] == tau and m["tau_mean"] == pytest.approx(sum(tau.values()) / len(tau))


def test_crossing_gain_is_zero_when_everyone_is_docked(runs_dir: Path) -> None:
    for run in load_runs(runs_dir):  # 贪心只选终局前能到的航程
        assert set(crossing_gain(run, rebuild_markets(run)).values()) == {0.0}
        m = run_metrics(run)
        assert m["in_flight_share"] == 0.0 and m["net_alt_mean"] == m["net_mean"]


def test_report_renders(runs_dir: Path, tmp_path: Path) -> None:
    runs = load_runs(runs_dir)
    per_run = [run_metrics(r) for r in runs]
    write_report(tmp_path, runs, per_run, summarize(per_run))
    text = (tmp_path / "report.md").read_text()
    assert "## 总览" in text and "贪心·光速" in text and "## 行为" in text
    assert (
        "不计跨终局航程" in text and "终局在途" in text and "| 交易毛利 | 能源 | 航程工资 |" in text
    )
    assert "## 推理里提到了什么" not in text  # 贪心组没有 transcript
    for fig in (
        "fig_networth.png",
        "fig_speed.png",
        "fig_ports.png",
        "fig_surprise.png",
        "fig_shock.png",
        "fig_warning.png",
        "fig_network.png",
    ):
        assert (tmp_path / fig).stat().st_size > 10_000


def test_ghost_figure_stacks_runs_on_the_same_shock(runs_dir: Path, tmp_path: Path) -> None:
    runs = {r.condition: r for r in load_runs(runs_dir)}
    pair = [runs["greedy-light"], runs["greedy-ansible"]]
    name = fig_ghosts(tmp_path, pair)
    assert name == "fig_ghosts.png" and (tmp_path / name).stat().st_size > 50_000
    whole = fig_ghosts(tmp_path, [*pair, runs["greedy-light"]], name="whole.png", window=None)
    assert whole == "whole.png" and (tmp_path / whole).stat().st_size > 50_000


@pytest.mark.unit
def test_kilo_ticks_keep_half_thousands() -> None:
    # 刻度在 2.5k 时，旧格式 .0f 会标成「2k」，和起始 2,000¢ 的基线对不上
    assert [_kilo(v) for v in (0, 2500, 7500, 10000, -2500)] == [
        "0k",
        "2.5k",
        "7.5k",
        "10k",
        "-2.5k",
    ]


@pytest.mark.unit
def test_plane_projection_keeps_coplanar_distances() -> None:
    pts = {"A": [0, 0, 0], "B": [1, 1, 0], "C": [0, 1, 1], "D": [2, 1, -1]}  # 都在 x − y + z = 0 上
    xy = _plane({"stations": {s: {"pos": p} for s, p in pts.items()}})
    for a in pts:
        for b in pts:
            d3 = math.dist(pts[a], pts[b])
            assert math.dist(xy[a], xy[b]) == pytest.approx(d3, abs=1e-9)


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


def test_resumed_runs_show_no_wall_clock(runs_dir: Path, tmp_path: Path) -> None:
    # 中途失败、从缓存续跑的局，wall_s 只覆盖最后一次运行
    base = next(r for r in load_runs(runs_dir) if r.condition == "greedy-light")

    def llm_run(run_id: str, cache_hits: int) -> Run:
        stats = {"calls": 10, "cache_hits": cache_hits, "cost_usd": 0.5}
        meta = {**base.meta, "condition": "llm-light", "run_id": run_id}
        return replace(base, meta={**meta, "wall_s": 600.0, "llm": {"Asel": stats}})

    runs = [llm_run("fresh", 0), llm_run("resumed", 4)]
    per_run = [run_metrics(r) for r in runs]
    write_report(tmp_path, runs, per_run, summarize(per_run))
    rows = {
        line.split(" | ")[0][2:]: line.split(" | ")
        for line in (tmp_path / "report.md").read_text().splitlines()
        if line.startswith(("| fresh |", "| resumed |"))
    }
    assert rows["fresh"][8] == "10.0 min" and rows["resumed"][8] == "–（续跑）"
    assert rows["resumed"][10] == "$0.50 |"


@pytest.mark.unit
def test_star_layout_spreads_distortion() -> None:
    world = make_world(7)
    w = {"stations": {s: {"pos": list(spec.pos)} for s, spec in world.stations.items()}}

    def worst(xy: dict[str, tuple[float, float]]) -> float:
        return max(
            abs(math.dist(xy[a], xy[b]) / world.distance(a, b) - 1) for a in xy for b in xy if a < b
        )

    pca = worst(_plane(w, iters=0))  # 纯投影：Wolf 和 Lalande 几乎重合
    assert pca > 0.5 and worst(_plane(w)) < 0.35


def test_audit_sample_lists_hit_sentences_and_respects_exclusions(
    runs_dir: Path, tmp_path: Path
) -> None:
    base = next(r for r in load_runs(runs_dir) if r.condition == "greedy-light")
    docks = [
        Dock("Asel", 1.0, "Sol", "Buy ore. The Sirius quote is 8.6 years old."),
        Dock("Bram", 2.0, "Sol", "Wolf data is stale. Chen went there."),
        Dock("Chen", 3.0, "Sol", "Nothing here."),
    ]
    run = replace(base, meta={**base.meta, "condition": "llm-light"}, docks=docks)
    rows = audit_sample([run], n=5, seed=1)
    stale = [r for r in rows if r["concept"] == "stale"]
    assert {r["merchant"] for r in stale} == {"Asel", "Bram"}
    assert next(r for r in stale if r["merchant"] == "Asel")["sentences"] == (
        "The Sirius quote is 8.6 years old."
    )
    rivals = [r for r in rows if r["concept"] == "rivals"]
    assert [r["sentences"] for r in rivals] == ["Chen went there."]  # 只因名字命中
    assert audit_sample([run], n=5, seed=1) == rows  # 同一种子可复现
    key = f"{run.meta['run_id']}|Asel|1.00"
    assert all(r["merchant"] != "Asel" for r in audit_sample([run], 5, 1, exclude=[key]))
    path = tmp_path / "concept_audit.csv"
    write_audit(path, rows)
    with pytest.raises(FileExistsError):
        write_audit(path, rows)  # 不覆盖可能已标注的文件


def test_audit_precision_counts_labeled_rows(tmp_path: Path) -> None:
    path = tmp_path / "a.csv"
    path.write_text(
        "concept,run,merchant,t,station,sentences,label\n"
        "stale,r,A,1.00,Sol,x,TP\n"
        "stale,r,B,2.00,Sol,x,fp\n"
        "stale,r,C,3.00,Sol,x,\n"
        "dilation,r,A,1.00,Sol,x,TP\n"
    )
    assert audit_precision(path) == {"stale": (1, 2), "dilation": (1, 1)}


def test_report_adds_audit_column_when_labels_exist(runs_dir: Path, tmp_path: Path) -> None:
    base = next(r for r in load_runs(runs_dir) if r.condition == "greedy-light")
    fake = replace(
        base,
        meta={**base.meta, "condition": "llm-light"},
        docks=[Dock("Asel", 1.0, "Sol", "The Sirius quote is 8.6 years old.")],
    )
    (tmp_path / "concept_audit.csv").write_text(
        "concept,run,merchant,t,station,sentences,label\nstale,r,A,1.00,Sol,x,TP\n"
    )
    per_run = [run_metrics(r) for r in (base, fake)]
    write_report(tmp_path, [base, fake], per_run, summarize(per_run))
    text = (tmp_path / "report.md").read_text()
    assert "人工核对：真 / 抽查" in text and "| 1 / 1 |" in text
    assert "audit_exclude.json" not in text
    # 调关键词规则时看过的停靠不进抽样：报告要说明这是一批没看过的样本
    (tmp_path / "audit_exclude.json").write_text(json.dumps(["r|A|1.0000", "r|B|2.0000"]))
    write_report(tmp_path, [base, fake], per_run, summarize(per_run))
    assert "不含调关键词规则时已经看过的 2 次停靠" in (tmp_path / "report.md").read_text()
