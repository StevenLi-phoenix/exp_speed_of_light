"""把分析结果画成图、写成中文 markdown 报告（数据部分；结论由人读完数据后另写）。

配色取 dataviz 参考调色板：LLM 三组用分类色前三槽（蓝/橙/青，三槽两两可分），
贪心组统一用灰色作基线——在条带图里组别由 x 轴位置区分，颜色只是辅助。
"""

from __future__ import annotations

import csv
import statistics
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402

from reltrade.analysis import GOODS, LABEL, ORDER, Run  # noqa: E402

SURFACE, INK, INK2, MUTED, GRID, BASE = (
    "#fcfcfb",
    "#0b0b0b",
    "#52514e",
    "#898781",
    "#e1e0d9",
    "#c3c2b7",
)
# 组合编码：颜色 = 物理条件（光速 / 瞬时信息 / 牛顿），线型与点型 = 商人（LLM 实线实心，贪心虚线空心）
PHYS_COLOR = {"light": "#2a78d6", "ansible": "#eb6834", "newton": "#1baf7a"}
COLOR = {c: PHYS_COLOR[c.split("-", 1)[1]] for c in ORDER}


def _dot_kw(c: str, alpha: float) -> dict[str, Any]:
    if c.startswith("llm"):
        return {"color": COLOR[c], "alpha": alpha, "edgecolors": "none"}
    return {
        "facecolors": "none",
        "edgecolors": COLOR[c],
        "alpha": min(alpha + 0.2, 1.0),
        "linewidths": 0.9,
    }


def _line_kw(c: str) -> dict[str, Any]:
    if c.startswith("llm"):
        return {"color": COLOR[c], "lw": 2.0, "ls": "-"}
    return {"color": COLOR[c], "lw": 1.6, "ls": (0, (4, 2))}


plt.rcParams.update(
    {
        "font.family": ["PingFang SC", "Hiragino Sans GB", "Arial Unicode MS", "DejaVu Sans"],
        "font.size": 10,
        "axes.titlesize": 11,
        "axes.titleweight": "semibold",
        "axes.titlecolor": INK,
        "axes.labelcolor": INK2,
        "axes.edgecolor": BASE,
        "axes.linewidth": 0.8,
        "axes.facecolor": SURFACE,
        "figure.facecolor": SURFACE,
        "savefig.facecolor": SURFACE,
        "xtick.color": MUTED,
        "ytick.color": MUTED,
        "xtick.labelcolor": INK2,
        "ytick.labelcolor": INK2,
        "grid.color": GRID,
        "grid.linewidth": 0.8,
        "axes.grid": True,
        "axes.grid.axis": "y",
        "axes.spines.top": False,
        "axes.spines.right": False,
        "legend.frameon": False,
        "axes.unicode_minus": False,
    }
)


def _conds(per_run: list[dict[str, Any]]) -> list[str]:
    present = {m["condition"] for m in per_run}
    return [c for c in ORDER if c in present]


def _jitter(n: int, width: float = 0.28, seed: int = 0) -> np.ndarray:
    return np.random.default_rng(seed).uniform(-width, width, n)


def _rapidity(beta: np.ndarray) -> np.ndarray:
    """把 β 映射到 −log10(1−β)，让 0.9 / 0.99 / 0.999 等距。"""
    return -np.log10(1 - np.clip(beta, 0, 0.999999))


def fig_networth(out: Path, per_run: list[dict[str, Any]]) -> str:
    conds = _conds(per_run)
    fig, ax = plt.subplots(figsize=(8.5, 4.2))
    for i, c in enumerate(conds):
        vals = [v for m in per_run if m["condition"] == c for v in m["net"].values()]
        ax.scatter(
            i + _jitter(len(vals), seed=i),
            vals,
            s=22,
            **_dot_kw(c, 0.75),
            zorder=3,
        )
        mean = statistics.mean(vals)
        ax.plot(
            [i - 0.32, i + 0.32], [mean, mean], color=INK, lw=2, solid_capstyle="round", zorder=4
        )
        ax.annotate(f"{mean / 1000:.1f}k", (i + 0.36, mean), va="center", fontsize=9, color=INK)
    ax.axhline(2000, color=BASE, lw=1, zorder=1)
    ax.annotate("起始 2,000¢", (-0.45, 2000), va="bottom", ha="left", fontsize=8.5, color=MUTED)
    ax.set_xticks(range(len(conds)), [LABEL[c] for c in conds])
    ax.set_ylabel("终局净值（¢）")
    ax.yaxis.set_major_formatter(lambda v, _: f"{v / 1000:.0f}k")
    ax.set_title("每个商人的终局净值（点）与组均值（横线）", loc="left")
    fig.tight_layout()
    fig.savefig(out / "fig_networth.png", dpi=180)
    plt.close(fig)
    return "fig_networth.png"


def fig_speed(out: Path, runs: list[Run], per_run: list[dict[str, Any]]) -> str:
    conds = _conds(per_run)
    fig, axes = plt.subplots(1, len(conds), figsize=(2.3 * len(conds) + 1, 4.0), squeeze=False)
    cats = [*GOODS, "empty"]
    for ax, c in zip(axes[0], conds, strict=True):
        rel = not c.endswith("newton")
        for j, g in enumerate(cats):
            sp = np.array(
                [t.speed for r in runs if r.condition == c for t in r.trips if t.main == g]
            )
            if not len(sp):
                continue
            y = _rapidity(sp) if rel else sp
            ax.scatter(
                j + _jitter(len(y), 0.25, seed=j),
                y,
                s=12,
                **_dot_kw(c, 0.45),
                zorder=3,
            )
            med = float(np.median(y))
            ax.plot(
                [j - 0.3, j + 0.3], [med, med], color=INK, lw=2, solid_capstyle="round", zorder=4
            )
        ax.set_xticks(range(len(cats)), cats)
        ax.set_title(LABEL[c], loc="left", fontsize=10)
        if rel:
            ticks = [0.5, 0.8, 0.9, 0.95, 0.99, 0.995, 0.999]
            ax.set_yticks(_rapidity(np.array(ticks)), [f"{b:g}c" for b in ticks])
            ax.set_ylim(_rapidity(np.array([0.4]))[0], _rapidity(np.array([0.9995]))[0])
        else:
            ax.set_ylabel("v（ly/yr）")
    axes[0][0].set_ylabel("巡航速度 β（按 −log(1−β) 刻度）")
    fig.suptitle(
        "按主货分组的巡航速度（点 = 一次出发，横线 = 中位数）",
        x=0.01,
        ha="left",
        fontsize=11,
        fontweight="semibold",
        color=INK,
    )
    fig.tight_layout()
    fig.savefig(out / "fig_speed.png", dpi=180)
    plt.close(fig)
    return "fig_speed.png"


def fig_ports(out: Path, runs: list[Run], summary: dict[str, Any]) -> str:
    conds = [c for c in ORDER if c in summary]
    world = runs[0].world
    stations = sorted(
        world["stations"], key=lambda s: world["stations"][s]["markets"]["energy"]["p_eq"]
    )
    data = np.array([[summary[c]["port_share"].get(s, 0.0) * 100 for c in conds] for s in stations])
    fig, ax = plt.subplots(figsize=(1.35 * len(conds) + 2.8, 4.2))
    from matplotlib.colors import LinearSegmentedColormap

    cmap = LinearSegmentedColormap.from_list("blue", ["#f4f8fe", "#86b6ef", "#2a78d6", "#104281"])
    ax.imshow(data, cmap=cmap, aspect="auto", vmin=0, vmax=max(35.0, data.max()))
    ax.grid(False)
    for i in range(len(stations)):
        for j in range(len(conds)):
            v = data[i, j]
            ax.text(
                j,
                i,
                f"{v:.0f}%",
                ha="center",
                va="center",
                fontsize=9,
                color="white" if v > 22 else INK,
            )
    ax.set_xticks(range(len(conds)), [LABEL[c] for c in conds], rotation=20, ha="right")
    ax.set_yticks(
        range(len(stations)),
        [f"{s}（能源 {world['stations'][s]['markets']['energy']['p_eq']:g}¢）" for s in stations],
    )
    for sp in ax.spines.values():
        sp.set_visible(False)
    ax.set_title("各星站到港次数占比（按正常能源价从低到高排列）", loc="left")
    fig.tight_layout()
    fig.savefig(out / "fig_ports.png", dpi=180)
    plt.close(fig)
    return "fig_ports.png"


def fig_surprise(out: Path, per_run: list[dict[str, Any]]) -> str:
    conds = _conds(per_run)
    fig, (a1, a2) = plt.subplots(1, 2, figsize=(11, 4.2), gridspec_kw={"width_ratios": [3, 2]})
    for i, c in enumerate(conds):
        vals = [s for m in per_run if m["condition"] == c for s in m["surprise"]]
        if not vals:
            continue
        a1.scatter(
            i + _jitter(len(vals), seed=i),
            vals,
            s=10,
            **_dot_kw(c, 0.45),
            zorder=3,
        )
        med = statistics.median(vals)
        a1.plot([i - 0.32, i + 0.32], [med, med], color=INK, lw=2, solid_capstyle="round", zorder=4)
    a1.axhline(1.0, color=BASE, lw=1, zorder=1)
    a1.set_xticks(range(len(conds)), [LABEL[c] for c in conds], rotation=15, ha="right")
    a1.set_ylabel("实际收入 ÷ 出发时预期")
    a1.set_ylim(0, 2.2)
    a1.set_title("到港卖货的意外度（1 = 与出发时看到的报价一致）", loc="left")
    for c in conds:
        pairs = [p for m in per_run if m["condition"] == c for p in m["crowd_pairs"]]
        if not pairs:
            continue
        bins: dict[int, list[float]] = defaultdict(list)
        for crowd, s in pairs:
            bins[min(crowd, 3)].append(s)
        xs = sorted(bins)
        a2.plot(
            xs,
            [statistics.mean(bins[x]) for x in xs],
            **_line_kw(c),
            marker="o",
            ms=6,
            mec=SURFACE,
            mew=1.5,
            label=LABEL[c],
        )
    a2.axhline(1.0, color=BASE, lw=1, zorder=1)
    a2.set_xticks([0, 1, 2, 3], ["0", "1", "2", "3+"])
    a2.set_xlabel("在途期间对手在同一目的地卖同种货的次数")
    a2.set_ylabel("平均意外度")
    a2.set_title("扎堆的代价", loc="left")
    a2.legend(fontsize=8.5, loc="lower left")
    fig.tight_layout()
    fig.savefig(out / "fig_surprise.png", dpi=180)
    plt.close(fig)
    return "fig_surprise.png"


def fig_shock(out: Path, per_run: list[dict[str, Any]]) -> str:
    """事件研究：运往短缺星站的发货率，分别按冲击时刻和按消息到达出发站的时刻对齐。"""
    conds = [c for c in _conds(per_run) if c != "greedy-newton"]
    bins = np.arange(-10, 13, 1.0)
    fig, axes = plt.subplots(1, 2, figsize=(11, 4.2), sharey=True)
    for ax, key, title in (
        (axes[0], "rel_shock", "按冲击发生时刻对齐"),
        (axes[1], "rel_info", "按消息光速到达出发站的时刻对齐"),
    ):
        for c in conds:
            ms = [m for m in per_run if m["condition"] == c]
            pairs = sum(m["shock_pairs"] for m in ms)
            xs = [r[key] for m in ms for r in m["shock_responses"]]
            if not pairs or not xs:
                continue
            counts, _ = np.histogram(xs, bins=bins)
            rate = counts / pairs  # 每个 (冲击, 出发站) 组合每年的发货次数
            ax.step(bins[:-1], rate, where="post", label=LABEL[c], **_line_kw(c))
        ax.axvline(0, color=INK2, lw=1)
        ax.set_xlabel("出发时刻 − 对齐时刻（年）")
        ax.set_title(title, loc="left")
    axes[0].set_ylabel("运往短缺站的发货率（次 / 出发站 / 年）")
    axes[1].legend(fontsize=8.5, loc="upper left")
    fig.suptitle(
        "短缺冲击后，各站什么时候开始往冲击站运货",
        x=0.01,
        ha="left",
        fontsize=11,
        fontweight="semibold",
        color=INK,
    )
    fig.tight_layout()
    fig.savefig(out / "fig_shock.png", dpi=180)
    plt.close(fig)
    return "fig_shock.png"


def fig_warning(out: Path, per_run: list[dict[str, Any]]) -> str:
    fig, ax = plt.subplots(figsize=(7.5, 4.0))
    for c in _conds(per_run):
        if not c.endswith("light"):
            continue
        ws = [w for m in per_run if m["condition"] == c for w in m["warnings"]]
        ax.scatter(
            [w["speed"] for w in ws],
            [max(w["warning"], 1e-3) for w in ws],
            s=14,
            **_dot_kw(c, 0.55),
            label=LABEL[c],
            zorder=3,
        )
    beta = np.linspace(0.5, 0.9995, 200)
    for d in (4, 8, 12):
        ax.plot(beta, d * (1 / beta - 1), color=BASE, lw=1, zorder=1)
        ax.annotate(f"d = {d} ly", (0.52, d * (1 / 0.52 - 1)), fontsize=8, color=MUTED, va="bottom")
    ax.set_yscale("log")
    ax.set_xlabel("巡航速度 β")
    ax.set_ylabel("预警时间（年，对数）")
    ax.set_title("出发消息到港后多久船就到了：d(1/β − 1)", loc="left")
    ax.legend(fontsize=8.5)
    fig.tight_layout()
    fig.savefig(out / "fig_warning.png", dpi=180)
    plt.close(fig)
    return "fig_warning.png"


def fig_wave(out: Path, runs: list[Run], per_run: list[dict[str, Any]]) -> str | None:
    """挑一局 LLM·光速里响应最多的短缺冲击，画该货在各星站的价格随时间的变化。"""
    best = None
    for r, m in zip(runs, per_run, strict=True):
        if r.condition != "llm-light":
            continue
        counts: dict[tuple[str, str], int] = defaultdict(int)
        for resp in m["shock_responses"]:
            if 0 <= resp["rel_info"] <= 8:
                counts[(resp["station"], resp["good"])] += 1
        for (s, g), n in counts.items():
            if best is None or n > best[0]:
                best = (n, r, s, g)
    if best is None:
        return None
    _, run, station, good = best
    shock = next(
        sh
        for sh in run.world["shocks"]
        if sh["station"] == station and sh["good"] == good and sh["kind"] == "shortage"
    )
    t0 = shock["t_start"]
    series: dict[str, list[tuple[float, float]]] = defaultdict(list)
    with (run.path / "prices.csv").open() as f:
        for row in csv.DictReader(f):
            if row["good"] == good:
                t = float(row["t"])
                if t0 - 5 <= t <= shock["t_end"] + 12:
                    series[row["station"]].append((t, float(row["ask"])))
    stations = sorted(series, key=lambda s: run.dist(station, s))
    fig, axes = plt.subplots(len(stations), 1, figsize=(8.5, 1.05 * len(stations) + 1), sharex=True)
    for ax, s in zip(axes, stations, strict=True):
        ts, ps = zip(*series[s], strict=True)
        base = (
            statistics.median(p for t, p in series[s] if t < t0)
            if any(t < t0 for t, _ in series[s])
            else ps[0]
        )
        ax.plot(ts, np.array(ps) / base, color=COLOR["llm-light"], lw=1.6)
        ax.axhline(1.0, color=BASE, lw=0.8)
        arrive = t0 + run.dist(station, s)
        ax.axvline(arrive, color=INK2, lw=1)
        ax.set_ylabel(
            f"{s}\n{run.dist(station, s):.1f} ly", rotation=0, ha="right", va="center", fontsize=8.5
        )
        ax.grid(False)
        ax.tick_params(axis="y", labelsize=7.5)
    axes[0].set_title(
        f"{run.meta['run_id']}：y{t0:.1f} {station} {good} 短缺后，各站 {good} "
        "价格（相对冲击前中位数）；竖线 = 消息光速到达",
        loc="left",
        fontsize=9.5,
    )
    axes[-1].set_xlabel("银河年")
    fig.tight_layout()
    fig.savefig(out / "fig_wave.png", dpi=180)
    plt.close(fig)
    return "fig_wave.png"


MERCHANT_COLORS = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#6250d6"]
ROW_ORDER = [
    "Procyon",
    "Sirius",
    "Sol",
    "Alpha Centauri",
    "Wolf 359",
    "Ross 128",
    "Lalande 21185",
]  # 相邻行在空间上也相邻


def fig_spacetime(out: Path, run: Run, name: str = "fig_spacetime.png") -> str:
    """一局的"世界线"：每段航程从 (出发时刻, 出发站) 连到 (到港时刻, 目的地)，按商人着色。"""
    rows = [s for s in ROW_ORDER if s in run.world["stations"]]
    y = {s: i for i, s in enumerate(rows)}
    fig, ax = plt.subplots(figsize=(12, 4.6))
    for sh in run.world["shocks"]:
        if sh["station"] in y:
            ax.fill_between(
                [sh["t_start"], sh["t_end"]],
                y[sh["station"]] - 0.32,
                y[sh["station"]] + 0.32,
                color="#e1e0d9",
                lw=0,
                zorder=1,
            )
            ax.annotate(
                f"{sh['good']} {sh['kind']}",
                (sh["t_start"] + 0.2, y[sh["station"]] + 0.36),
                fontsize=6.5,
                color=MUTED,
                va="bottom",
            )
    merchants = list(run.meta["final"])
    for k, m in enumerate(merchants):
        color = MERCHANT_COLORS[k % len(MERCHANT_COLORS)]
        trips = [t for t in run.trips if t.merchant == m]
        for t in trips:
            t_arr = (
                t.t_arr
                if t.t_arr is not None
                else min(t.t_dep + t.distance / t.speed, run.meta["t_end"])
            )
            ax.plot(
                [t.t_dep, t_arr],
                [y[t.origin], y[t.dest]],
                color=color,
                lw=1.3,
                alpha=0.85,
                solid_capstyle="round",
                zorder=3,
            )
        for a, b in zip(trips, trips[1:], strict=False):  # 停靠等待
            if a.t_arr is not None and b.t_dep - a.t_arr > 0.05:
                ax.plot(
                    [a.t_arr, b.t_dep],
                    [y[a.dest]] * 2,
                    color=color,
                    lw=3,
                    alpha=0.5,
                    solid_capstyle="butt",
                    zorder=2,
                )
        ax.plot([], [], color=color, lw=2, label=m)
    ax.set_yticks(range(len(rows)), rows)
    ax.set_xlim(0, run.meta["t_end"])
    ax.set_ylim(-0.6, len(rows) - 0.3)
    ax.grid(axis="y", color=GRID, lw=0.8)
    ax.grid(axis="x", visible=False)
    ax.set_xlabel("银河年（坐标时 t）")
    ax.set_title(
        f"{LABEL.get(run.condition, run.condition)} · 世界 {run.meta['world_seed']} · "
        f"重复 {run.meta['replicate']}：6 个商人 {run.meta['t_end']:g} 年的航程"
        "（斜线 = 飞行，粗横线 = 停靠等待，灰带 = 冲击）",
        loc="left",
    )
    ax.legend(ncol=6, fontsize=8.5, loc="upper left", bbox_to_anchor=(0, -0.13))
    fig.tight_layout()
    fig.savefig(out / name, dpi=180)
    plt.close(fig)
    return name


def _fmt(ms: dict[str, Any], digits: int = 0, pct: bool = False) -> str:
    if ms.get("mean") is None:
        return "–"
    k = 100 if pct else 1
    unit = "%" if pct else ""
    if ms.get("n", 0) > 1:
        return f"{ms['mean'] * k:,.{digits}f}{unit} ± {ms['sd'] * k:,.{digits}f}"
    return f"{ms['mean'] * k:,.{digits}f}{unit}"


def write_report(
    out: Path, runs: list[Run], per_run: list[dict[str, Any]], summary: dict[str, Any]
) -> None:
    figs = {
        "networth": fig_networth(out, per_run),
        "speed": fig_speed(out, runs, per_run),
        "ports": fig_ports(out, runs, summary),
        "surprise": fig_surprise(out, per_run),
        "shock": fig_shock(out, per_run),
        "warning": fig_warning(out, per_run),
        "wave": fig_wave(out, runs, per_run),
    }
    showcase = next(
        (r for r in runs if r.condition == "llm-light"),
        next((r for r in runs if r.condition == "greedy-light"), None),
    )
    figs["spacetime"] = fig_spacetime(out, showcase) if showcase is not None else None
    conds = [c for c in ORDER if c in summary]
    L: list[str] = []
    L.append("# 相对论星际贸易：实验数据报告")
    L.append("")
    models = sorted({r.meta.get("model") for r in runs if r.meta.get("model")})
    L.append(
        f"生成于 {datetime.now():%Y-%m-%d %H:%M} · {len(runs)} 局 · LLM：{', '.join(models) or '—'}"
        " · 结论另见 README「发现」"
    )
    L.append("")
    L.append("## 总览")
    L.append("")
    L.append(
        "| 对照组 | 局数（世界） | 每局商人均净值 | 基尼 | 每局航程 | ore / tech / med 中位速度 |"
        " 意外度均值 | 意外度<0.8 | 预警<0.5 年 | 每局消息 | 平均船时 τ | 破产 | LLM 花费 |"
    )
    L.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for c in conds:
        s = summary[c]
        sp = s["speed_by_good"]
        unit = "" if c.endswith("newton") else "c"
        speeds = " / ".join(
            f"{sp[g]['mean']:.3g}{unit}" if g in sp and sp[g]["mean"] is not None else "–"
            for g in GOODS
        )
        L.append(
            f"| {LABEL[c]} | {s['runs']}（{','.join(map(str, s['worlds']))}） | "
            f"{_fmt(s['net_mean'])} | {_fmt(s['gini'], 2)} | {_fmt(s['trips'], 1)} | "
            f"{speeds} | {_fmt(s['surprise_mean'], 2)} | {_fmt(s['surprise_bad'], 0, True)} | "
            f"{_fmt(s['raid_share'], 0, True)} | {_fmt(s['messages'], 1)} | "
            f"{_fmt(s['tau_mean'], 1)} | {s['bankrupt']} | ${s['cost_usd']:.2f} |"
        )
    L.append("")
    L.append(
        "「±」是跨局标准差。贪心组多世界 seed，用来估计世界之间的差异；LLM 组同一世界多次重复，"
        "差异来自采样随机性。"
    )
    L.append("")
    sections = [
        (
            "一局的世界线",
            "spacetime",
            "示例局：每条斜线是一段航程，斜率越陡飞得越快；可以看到航线、" "扎堆和冲击期间的绕行。",
        ),
        ("终局净值", "networth", "推理（LLM vs 贪心）和物理（光速 vs 瞬时 vs 牛顿）各自的效果。"),
        (
            "速度分层",
            "speed",
            "相对论组的纵轴按 −log10(1−β) 刻度，0.9c / 0.99c / 0.999c 等距；"
            "牛顿组单位是 ly/yr。",
        ),
        ("能源与枢纽", "ports", "能源便宜的星站会不会成为枢纽。"),
        (
            "过时报价与扎堆",
            "surprise",
            "意外度 < 1：到港时价格比出发时看到的差（报价过时、对手先到、冲击结束）。",
        ),
        (
            "信息波：冲击响应",
            "shock",
            "左图按冲击时刻对齐：光速组的响应被各站距离抹开；右图按消息到达"
            "出发站的时刻对齐：响应应当在 0 点之后才出现、且突然出现。0 点之前的发货是常规航线的背景。",
        ),
        (
            "突袭：快船与自己的出发消息几乎同时到达",
            "warning",
            "预警时间 = 到港时刻 − 出发消息光速到达目的地的时刻 = d(1/β − 1)。",
        ),
        (
            "一次冲击的价格场",
            "wave",
            "竖线是冲击消息以光速到达各站的时刻；各站价格的扰动只能出现在竖线之后。",
        ),
    ]
    for title, key, note in sections:
        if figs.get(key):
            L.append(f"## {title}")
            L.append("")
            L.append(note)
            L.append("")
            L.append(f"![{title}]({figs[key]})")
            L.append("")
    # 枢纽表
    L.append("### 到港占比（%）")
    L.append("")
    world = runs[0].world
    stations = sorted(
        world["stations"], key=lambda s: world["stations"][s]["markets"]["energy"]["p_eq"]
    )
    L.append("| 星站 | 正常能源价 | " + " | ".join(LABEL[c] for c in conds) + " |")
    L.append("|---|---|" + "---|" * len(conds))
    for st in stations:
        cells = " | ".join(f"{summary[c]['port_share'].get(st, 0) * 100:.0f}" for c in conds)
        L.append(f"| {st} | {world['stations'][st]['markets']['energy']['p_eq']:g}¢ | {cells} |")
    L.append("")
    # 每局明细
    L.append("## 每局明细")
    L.append("")
    L.append(
        "| run | 均净值 | 最低 / 最高 | 航程 | 空船 | 意外度 | 消息 | 最大并发 | 墙钟 | LLM 调用 | 花费 |"
    )
    L.append("|---|---|---|---|---|---|---|---|---|---|---|")
    for m in sorted(
        per_run, key=lambda m: (ORDER.index(m["condition"]), m["world_seed"], m["replicate"])
    ):
        llm = m.get("llm") or {}
        sm = f"{m['surprise_mean']:.2f}" if m["surprise_mean"] is not None else "–"
        L.append(
            f"| {m['run_id']} | {m['net_mean']:,.0f} | {m['net_min']:,.0f} / "
            f"{m['net_max']:,.0f} | {m['trips']} | {m['empty_trips']} | {sm} | "
            f"{m['messages']} | {m['max_parallel']} | {(m['wall_s'] or 0) / 60:.1f} min | "
            f"{llm.get('calls', '–')} | ${llm.get('cost_usd', 0):.2f} |"
        )
    L.append("")
    # 消息与笔记本摘录
    llm_runs = [(r, m) for r, m in zip(runs, per_run, strict=True) if r.condition.startswith("llm")]
    if llm_runs:
        L.append("## 商人之间的消息（摘录）")
        L.append("")
        for r, m in llm_runs:
            if not m["message_samples"]:
                continue
            L.append(f"**{r.meta['run_id']}**")
            L.append("")
            for msg in m["message_samples"][:12]:
                text = msg["text"].replace("\n", " ").replace("|", "/")
                L.append(
                    f"- y{msg['t']:.1f} {msg['from']} @ {msg['at']} → {msg['to']}：" f"{text[:300]}"
                )
            L.append("")
        L.append("## 终局笔记本（摘录）")
        L.append("")
        for r, _ in llm_runs[:3]:
            L.append(f"**{r.meta['run_id']}**")
            L.append("")
            for n, f in r.meta["final"].items():
                note = (f.get("notebook") or "").strip().replace("\n", " ")
                if note:
                    L.append(
                        f"- {n}（净值 {f['net_worth']:,.0f}¢，船时 {f['tau']:.1f} 年）："
                        f"{note[:400]}"
                    )
            L.append("")
    (out / "report.md").write_text("\n".join(L) + "\n")
