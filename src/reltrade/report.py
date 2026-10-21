"""把分析结果画成图、写成中文 markdown 报告（数据部分；结论由人读完数据后另写）。

配色取 dataviz 参考调色板：LLM 三组用分类色前三槽（蓝/橙/青，三槽两两可分），
贪心组统一用灰色作基线——在条带图里组别由 x 轴位置区分，颜色只是辅助。
"""

from __future__ import annotations

import csv
import logging
import math
import statistics
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402

# 中文字体回退链里每找不到一个字重就会打一条 WARNING，淹没真正的日志
logging.getLogger("matplotlib.font_manager").setLevel(logging.ERROR)

from reltrade.analysis import (  # noqa: E402
    CONCEPT_LABEL,
    GOODS,
    LABEL,
    ORDER,
    RESPONSE_WINDOW,
    WAVE_WINDOW,
    Run,
    audit_precision,
    concepts,
    excerpt,
)

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
    fig, ax = plt.subplots(figsize=(1.6 * len(conds) + 1.5, 4.4))
    labels: list[str] = []
    for i, c in enumerate(conds):
        ms = [m for m in per_run if m["condition"] == c]
        vals = [v for m in ms for v in m["net"].values()]
        alts = [m["net_alt"][n] for m in ms for n in m["net"]]
        xs = i + _jitter(len(vals), seed=i)
        ax.scatter(xs, vals, s=22, **_dot_kw(c, 0.75), zorder=3)
        for x, v, a in zip(xs, vals, alts, strict=True):  # 跨终局航程：竖线拉到留在港口的值
            if abs(v - a) > 1:
                ax.plot([x, x], [v, a], color=COLOR[c], lw=1, alpha=0.8, zorder=2)
                ax.plot([x - 0.05, x + 0.05], [a, a], color=COLOR[c], lw=1.4, zorder=2)
        mean, alt = statistics.mean(vals), statistics.mean(alts)
        ax.plot(
            [i - 0.32, i + 0.32], [mean, mean], color=INK, lw=2, solid_capstyle="round", zorder=4
        )
        label = f"{LABEL[c]}\n{mean / 1000:.1f}k"
        if abs(mean - alt) > 0.005 * mean:
            ax.plot([i - 0.32, i + 0.32], [alt, alt], color=INK, lw=1, ls=(0, (2, 2)), zorder=4)
            label += f"（{alt / 1000:.1f}k）"
        labels.append(label)
    ax.axhline(2000, color=BASE, lw=1, zorder=1)
    ax.annotate("起始 2,000¢", (-0.45, 2000), va="bottom", ha="left", fontsize=8.5, color=MUTED)
    ax.set_xticks(range(len(conds)), labels)
    ax.set_ylabel("终局净值（¢）")
    ax.yaxis.set_major_formatter(lambda v, _: f"{v / 1000:.0f}k")
    ax.set_title(
        "每个商人的终局净值（点）与组均值（实线）。竖线和虚线：去掉跨终局的最后一程后的净值"
        "与组均值（括号）",
        loc="left",
    )
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
    a1.set_ylabel("到港时可得收入 ÷ 出发时预期")
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


SHORT = {
    "Alpha Centauri": "α Cen",
    "Wolf 359": "Wolf",
    "Lalande 21185": "Lalande",
    "Ross 128": "Ross",
}


def _plane(world: dict[str, Any], iters: int = 300) -> dict[str, tuple[float, float]]:
    """星图的二维布局：从 PCA 投影出发做 SMACOF（stress MDS）迭代，
    把三维距离的误差摊到所有星对上，而不是把第三维整个压扁（Wolf 和 Lalande 会叠在一起）。"""
    names = list(world["stations"])
    xyz = np.array([world["stations"][s]["pos"] for s in names], dtype=float)
    xyz -= xyz.mean(axis=0)
    _, _, vt = np.linalg.svd(xyz, full_matrices=False)
    xy = xyz @ vt[:2].T
    target = np.linalg.norm(xyz[:, None] - xyz[None], axis=-1)
    n = len(names)
    for _ in range(iters):
        d = np.linalg.norm(xy[:, None] - xy[None], axis=-1)
        b = -target / np.where(d > 1e-12, d, 1e-12)
        np.fill_diagonal(b, 0.0)
        np.fill_diagonal(b, -b.sum(axis=1))
        xy = b @ xy / n
    return {s: (float(x), float(y)) for s, (x, y) in zip(names, xy, strict=True)}


def fig_network(out: Path, runs: list[Run], per_run: list[dict[str, Any]]) -> str:
    """小多图：行 = 商人（贪心 / LLM），列 = 物理条件；线宽按全图统一刻度，可跨面板比较。"""
    conds = _conds(per_run)
    phys = [p for p in PHYS_COLOR if any(c.endswith(p) for c in conds)]
    agents = [a for a in ("greedy", "llm") if any(c.startswith(a) for c in conds)]
    pos = _plane(runs[0].world)
    pairs, calls, nruns = {}, {}, {}
    for c in conds:
        rs = [r for r in runs if r.condition == c]
        nruns[c] = len(rs)
        pc: dict[tuple[str, str], int] = defaultdict(int)
        cc: dict[str, int] = defaultdict(int)
        for r in rs:
            for t in r.trips:
                pc[tuple(sorted((t.origin, t.dest)))] += 1
                cc[t.dest] += 1
        pairs[c], calls[c] = pc, cc
    top = max((n / nruns[c] for c in conds for n in pairs[c].values()), default=1.0)
    fig, axes = plt.subplots(
        len(agents), len(phys), figsize=(3.7 * len(phys), 3.6 * len(agents)), squeeze=False
    )
    for i, a in enumerate(agents):
        for j, p in enumerate(phys):
            ax = axes[i][j]
            ax.set_aspect("equal")
            ax.axis("off")
            c = f"{a}-{p}"
            if c not in pairs:
                continue
            for (s1, s2), n in sorted(pairs[c].items(), key=lambda kv: kv[1]):
                w = n / nruns[c] / top
                ax.plot(
                    [pos[s1][0], pos[s2][0]],
                    [pos[s1][1], pos[s2][1]],
                    color=COLOR[c],
                    lw=0.4 + 8 * w,
                    alpha=0.35 + 0.45 * w,
                    solid_capstyle="round",
                    zorder=2,
                )
            total = sum(calls[c].values()) or 1
            for s, (x, y) in pos.items():
                size = 25 + 1100 * calls[c].get(s, 0) / total
                ax.scatter([x], [y], s=size, color=SURFACE, edgecolors=INK2, lw=1.1, zorder=3)
                ax.annotate(
                    SHORT.get(s, s),
                    (x, y),
                    xytext=(0, -(size**0.5) / 2 - 2),
                    textcoords="offset points",
                    ha="center",
                    va="top",
                    fontsize=7.5,
                    color=INK2,
                    zorder=4,
                )
            per = sum(pairs[c].values()) / nruns[c]
            ax.set_title(f"{LABEL[c]} · 每局 {per:.0f} 程", loc="left", fontsize=9.5)
    fig.suptitle(
        "贸易网络：线宽 = 每局两站之间的航程数（各面板同一刻度），圆面积 = 到港占比；"
        "星图按三维距离做 MDS 摊平（有少量变形）",
        x=0.01,
        ha="left",
        fontsize=10.5,
        fontweight="semibold",
        color=INK,
    )
    fig.tight_layout()
    fig.savefig(out / "fig_network.png", dpi=180)
    plt.close(fig)
    return "fig_network.png"


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


def _responders(run: Run, sh: dict[str, Any], window: float = RESPONSE_WINDOW) -> list[Any]:
    """收到某次短缺冲击的消息之后 window 年内，载着该货出发前往冲击站的航程（同 analysis 的定义）。"""
    a, g, t0 = sh["station"], sh["good"], sh["t_start"]
    return [
        t
        for t in run.trips
        if t.dest == a
        and t.cargo.get(g, 0) > 1e-6
        and 0 <= t.t_dep - (t0 + run.lag(t.origin, a)) <= window
    ]


def _draw_spacetime(ax: Any, run: Run, front: dict[str, Any] | None) -> None:
    """在 ax 上画一局的"世界线"：每段航程从 (出发时刻, 出发站) 连到 (到港时刻, 目的地)，按商人着色；
    front 是一次短缺冲击：标出它的消息到达各站的时刻，并加粗收到消息后运货赶去的航程。"""
    rows = [s for s in ROW_ORDER if s in run.world["stations"]]
    y = {s: i for i, s in enumerate(rows)}
    label_end: dict[str, float] = {}
    for sh in sorted(run.world["shocks"], key=lambda sh: sh["t_start"]):
        st = sh["station"]
        if st not in y:
            continue
        hot_band = front is not None and sh == front
        ax.fill_between(
            [sh["t_start"], sh["t_end"]],
            y[st] - 0.32,
            y[st] + 0.32,
            color="#cfcdc4" if hot_band else "#e1e0d9",
            lw=0,
            zorder=1,
        )
        text = f"{sh['good']} {sh['kind']}"
        below = sh["t_start"] < label_end.get(st, -1.0)  # 和同一行上一条标签重叠就放到灰带下方
        ax.annotate(
            text,
            (sh["t_start"] + 0.2, y[st] + (-0.36 if below else 0.36)),
            fontsize=6.5,
            color=INK2 if hot_band else MUTED,
            va="top" if below else "bottom",
        )
        if not below:
            label_end[st] = sh["t_start"] + 0.45 * len(text) + 0.5
    bold = {id(t) for t in _responders(run, front)} if front else set()
    if front:
        a, t0 = front["station"], front["t_start"]
        for st in rows:
            x = t0 + run.lag(a, st)
            if x <= run.meta["t_end"]:
                ax.plot([x, x], [y[st] - 0.42, y[st] + 0.42], color=INK, lw=1.4, zorder=5)
        how = "瞬时到达各站" if math.isinf(run.c_info) else "以光速到达各站的时刻"
        ax.plot(
            [],
            [],
            color=INK,
            marker="|",
            ls="none",
            ms=11,
            mew=1.6,
            label=f"y{t0:.1f} {a} {front['good']} 短缺的消息{how}",
        )
    for k, m in enumerate(run.meta["final"]):
        color = MERCHANT_COLORS[k % len(MERCHANT_COLORS)]
        trips = [t for t in run.trips if t.merchant == m]
        for t in trips:
            t_arr = (
                t.t_arr
                if t.t_arr is not None
                else min(t.t_dep + t.distance / t.speed, run.meta["t_end"])
            )
            hot = id(t) in bold
            ax.plot(
                [t.t_dep, t_arr],
                [y[t.origin], y[t.dest]],
                color=color,
                lw=3.0 if hot else 1.2,
                alpha=1.0 if hot else 0.75,
                solid_capstyle="round",
                zorder=4 if hot else 3,
            )
        for a_, b_ in zip(trips, trips[1:], strict=False):  # 停靠等待
            if a_.t_arr is not None and b_.t_dep - a_.t_arr > 0.05:
                ax.plot(
                    [a_.t_arr, b_.t_dep],
                    [y[a_.dest]] * 2,
                    color=color,
                    lw=3,
                    alpha=0.5,
                    solid_capstyle="butt",
                    zorder=2,
                )
        ax.plot([], [], color=color, lw=2, label=m)
    ax.set_yticks(range(len(rows)), rows)
    ax.set_xlim(0, run.meta["t_end"])
    ax.set_ylim(-0.75, len(rows) - 0.2)
    ax.grid(axis="y", color=GRID, lw=0.8)
    ax.grid(axis="x", visible=False)


def _front(run: Run) -> dict[str, Any] | None:
    """响应最多的那次短缺冲击。"""
    shortages = [sh for sh in run.world["shocks"] if sh["kind"] == "shortage"]
    return max(shortages, key=lambda sh: len(_responders(run, sh)), default=None)


def fig_spacetime(out: Path, run: Run, name: str = "fig_spacetime.png") -> str:
    fig, ax = plt.subplots(figsize=(12, 4.8))
    _draw_spacetime(ax, run, _front(run))
    ax.set_xlabel("银河年（坐标时 t）")
    ax.set_title(
        f"{LABEL.get(run.condition, run.condition)} · 世界 {run.meta['world_seed']} · "
        f"重复 {run.meta['replicate']}：6 个商人 {run.meta['t_end']:g} 年的航程"
        "（斜线 = 飞行，粗横线 = 停靠等待，灰带 = 冲击，加粗 = 收到短缺消息后运货赶去）",
        loc="left",
        fontsize=10,
    )
    ax.legend(ncol=7, fontsize=8.5, loc="upper left", bbox_to_anchor=(0, -0.13))
    fig.tight_layout()
    fig.savefig(out / name, dpi=180)
    plt.close(fig)
    return name


def fig_ghosts(
    out: Path,
    runs: list[Run],
    name: str = "fig_ghosts.png",
    window: tuple[float, float] | None = (8.0, 30.0),
) -> str | None:
    """同一个世界的几局上下对照，标同一次冲击（取第一局响应最多的短缺）。
    window = 显示冲击开始前、后各多少年；None 显示整局。"""
    front = _front(runs[0])
    if front is None:
        return None
    t0 = front["t_start"]
    fig, axes = plt.subplots(
        len(runs), 1, figsize=(12, 0.9 + 3.6 * len(runs)), sharex=True, squeeze=False
    )
    for ax, run in zip(axes[:, 0], runs, strict=True):
        _draw_spacetime(ax, run, front)
        resp = _responders(run, front)
        on_time = sum(1 for t in resp if t.t_arr is not None and t.t_arr <= front["t_end"])
        title = (
            f"{LABEL.get(run.condition, run.condition)} · 重复 {run.meta['replicate']}："
            f"收到消息后运货赶去 {len(resp)} 程（加粗）"
        )
        if resp:
            title += f"，{on_time} 程在冲击结束前到达"
        if math.isinf(run.c_info):
            title += f"；消息瞬时到达各站（竖线都在 y{t0:.1f}）"
        ax.set_title(title, loc="left", fontsize=10)
        if window is not None:
            ax.set_xlim(max(t0 - window[0], 0.0), min(t0 + window[1], run.meta["t_end"]))
    axes[-1, 0].set_xlabel("银河年（坐标时 t）")
    handles, labels = axes[0, 0].get_legend_handles_labels()
    axes[-1, 0].legend(
        handles, labels, ncol=7, fontsize=8.5, loc="upper left", bbox_to_anchor=(0, -0.16)
    )
    fig.suptitle(
        f"同一个世界、同一场 {front['station']} {front['good']} 短缺（y{t0:.0f}–"
        f"y{front['t_end']:.0f}，深灰带）：光速下消息走到各站要 d/c 年，船再飞 d/β 年",
        x=0.01,
        ha="left",
        fontsize=11.5,
        fontweight="semibold",
        color=INK,
    )
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


def _pooled_fmt(p: dict[str, Any], signed: bool = True) -> str:
    if p.get("mean") is None:
        return "–"
    return f"{p['mean']:{'+' if signed else ''}.1f} ± {p['se']:.1f}"


def _behavior(L: list[str], conds: list[str], summary: dict[str, Any]) -> None:
    L.append("## 行为")
    L.append("")
    L.append(
        "| 对照组 | 平均航距（ly） | 出发时目的地报价的年龄（年） | 每局等待次数 |"
        " 主货占比 ore / tech / med / 空船 | 每次短缺的追击航程 | 到港时冲击仍在 | 追击航程的意外度 |"
        " 消息到达后各站多买走的货 | 其中运往冲击站 |"
    )
    L.append("|---|---|---|---|---|---|---|---|---|---|")
    for c in conds:
        s = summary[c]
        mix = " / ".join(f"{s['main_share'][g]['mean'] * 100:.0f}%" for g in (*GOODS, "empty"))
        L.append(
            f"| {LABEL[c]} | {_fmt(s['dist_mean'], 1)} | {_fmt(s['info_age_mean'], 1)} | "
            f"{_fmt(s['waits'], 1)} | {mix} | {_fmt(s['chase_per_shock'], 2)} | "
            f"{_fmt(s['chase_in_time'], 0, True)} | {_fmt(s['chase_surprise'], 2)} | "
            f"{_pooled_fmt(s['wave_extra'])} | {_pooled_fmt(s['wave_to_shock'], signed=False)} |"
        )
    L.append("")
    L.append(
        f"「追击航程」：收到短缺消息后 {RESPONSE_WINDOW:g} 年内，载着短缺的货出发前往冲击站的航程。"
        "光速下，远方的冲击从发生到船赶到至少要 d/c + d/β ≈ 2d 年。"
        f"「消息到达后各站多买走的货」：短缺消息到达冲击站以外的某站之后 {WAVE_WINDOW:g} 年，"
        f"同一种货在那里被买走的单位数，减去消息到达之前 {WAVE_WINDOW:g} 年的；"
        "所有（冲击，站）对合在一起，± 是标准误。远方的冲击只能通过这些买卖改变别处的价格，"
        "正常库存是 120–400 单位。「其中运往冲击站」是之后那段里同一次停靠就运往冲击站的量。"
    )
    L.append("")


def _reasoning(
    L: list[str],
    conds: list[str],
    summary: dict[str, Any],
    showcase: Run | None,
    audit: dict[str, tuple[int, int]] | None = None,
) -> None:
    llm_conds = [c for c in conds if summary[c]["concepts"]]
    if not llm_conds:
        return
    L.append("## 推理里提到了什么")
    L.append("")
    L.append(
        "每次停靠，模型写下的全部文字（推理 + 正文 + 工具参数）里是否出现某个概念的关键词，"
        "按停靠次数计百分比。关键词是粗筛：瞬时信息组和牛顿组的「信息过时」、牛顿组的「时间膨胀」"
        "本应为 0，它们的实际值可以当作误报率的参照。"
    )
    L.append("")
    extra = " 人工核对：真 / 抽查 |" if audit else ""
    L.append("| 概念 | " + " | ".join(LABEL[c] for c in llm_conds) + " |" + extra)
    L.append("|---|" + "---|" * len(llm_conds) + ("---|" if audit else ""))
    for k, label in CONCEPT_LABEL.items():
        cells = " | ".join(_fmt(summary[c]["concepts"][k], 0, True) for c in llm_conds)
        if audit:
            tp, n = audit.get(k, (0, 0))
            cells += f" | {tp} / {n}" if n else " | –"
        L.append(f"| {label} | {cells} |")
    L.append("")
    if audit:
        L.append(
            "「人工核对」：每个概念从全部 LLM 局里随机抽若干次命中的停靠，人读命中的句子判断是否真在谈"
            "这个概念（明细见 concept_audit.csv）。这只衡量误报（精度），不衡量漏报：换个说法的提及"
            "关键词抓不到。精度接近 100% 的概念，表中占比可以看作「提到」的下限。"
        )
        L.append("")
    if showcase is None or not showcase.docks:
        return
    L.append(f"原文摘录（{showcase.meta['run_id']}，每个概念取最早提到它的两位商人）：")
    L.append("")
    names = list(showcase.meta["final"])
    for k, label in CONCEPT_LABEL.items():
        seen: set[str] = set()
        for d in sorted(showcase.docks, key=lambda d: (d.t, d.merchant)):
            if d.merchant in seen or k not in concepts(d.text, d.merchant, names):
                continue
            ex = excerpt(d.text, k)
            if ex is None:  # 只因提到对手的名字而命中，没有可摘的关键词句
                continue
            seen.add(d.merchant)
            L.append(f"- **{label}** · {d.merchant} y{d.t:.1f} @ {d.station}：{ex}")
            if len(seen) == 2:
                break
    L.append("")


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
        "network": fig_network(out, runs, per_run),
    }
    showcase = next(
        (r for r in runs if r.condition == "llm-light"),
        next((r for r in runs if r.condition == "greedy-light"), None),
    )
    figs["spacetime"] = fig_spacetime(out, showcase) if showcase is not None else None
    llm_light = [r for r in runs if r.condition == "llm-light"]
    greedy_pair = next(
        (
            r
            for r in runs
            if r.condition == "greedy-light"
            and llm_light
            and r.meta["world_seed"] == llm_light[0].meta["world_seed"]
        ),
        None,
    )
    newton = [
        r
        for r in runs
        if r.condition == "llm-newton"
        and greedy_pair
        and r.meta["world_seed"] == greedy_pair.meta["world_seed"]
    ]
    figs["ghosts"] = (
        fig_ghosts(out, [greedy_pair, llm_light[0], *newton[:1]]) if greedy_pair else None
    )
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
        "| 对照组 | 局数（世界） | 每局商人均净值 | 不计跨终局航程 | 终局在途 | 基尼 | 每局航程 |"
        " ore / tech / med 中位速度 | 意外度均值 | 意外度<0.8 | 到港没卖主货 | 预警<0.5 年 | 每局消息 |"
        " 平均船时 τ | 破产 | LLM 花费 |"
    )
    L.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
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
            f"{_fmt(s['net_mean'])} | {_fmt(s['net_alt_mean'])} | "
            f"{_fmt(s['in_flight_share'], 0, True)} | {_fmt(s['gini'], 2)} | {_fmt(s['trips'], 1)} | "
            f"{speeds} | {_fmt(s['surprise_mean'], 2)} | {_fmt(s['surprise_bad'], 0, True)} | "
            f"{_fmt(s['unsold_share'], 0, True)} | "
            f"{_fmt(s['raid_share'], 0, True)} | {_fmt(s['messages'], 1)} | "
            f"{_fmt(s['tau_mean'], 1)} | {s['bankrupt']} | ${s['cost_usd']:.2f} |"
        )
    L.append("")
    L.append(
        "「±」是跨局标准差。贪心组多世界 seed，用来估计世界之间的差异；LLM 组同一世界多次重复，"
        "差异来自采样随机性。意外度 = 到港那一刻按目的地真实报价卖掉主货能得的收入 ÷ 出发时按看到的"
        "报价算的预期，所有载货航程都算，与到港后卖不卖无关；「到港没卖主货」是到港后没在目的地卖主货"
        "（带走或囤着）的航程比例。「终局在途」是第 100 年还在飞的商人比例：规则让在途的货按目的地的"
        "终局报价清算（扣掉腐败），LLM 会专门在最后几年启程去一个终局之后才到的地方，贪心只选终局前能到的"
        "航程。「不计跨终局航程」把这最后一程换成「不出发、留在港口」：本站买进的货按买价退回，带过来的货"
        "按出发站当时的报价卖掉，退回能源和航程工资，改付停靠工资。比较 LLM 和贪心时以这一列为准。"
        "「平均船时 τ」是第 100 年那一刻的船时，仍在飞的商人按匀速插值。"
    )
    L.append("")
    _behavior(L, conds, summary)
    audit_path = out / "concept_audit.csv"
    audit = audit_precision(audit_path) if audit_path.exists() else None
    _reasoning(L, conds, summary, showcase, audit)
    sections = [
        (
            "一局的世界线",
            "spacetime",
            "示例局：每条斜线是一段航程，斜率越陡飞得越快；可以看到航线、" "扎堆和冲击期间的绕行。",
        ),
        (
            "追幽灵：同一场冲击，贪心 vs LLM",
            "ghosts",
            "几局都是同一个世界（冲击和噪声完全相同），只截取冲击前后。加粗的是收到短缺消息后载着短缺的货"
            "赶往冲击站的航程。光速下，远方的冲击从发生到船赶到至少 d/c + d/β ≈ 2d 年，往往比冲击本身还长；"
            "有 LLM·牛顿局时放在最下面作对照（消息瞬时到达，船速没有上限）。",
        ),
        ("终局净值", "networth", "推理（LLM vs 贪心）和物理（光速 vs 瞬时 vs 牛顿）各自的效果。"),
        (
            "速度分层",
            "speed",
            "相对论组的纵轴按 −log10(1−β) 刻度，0.9c / 0.99c / 0.999c 等距；"
            "牛顿组单位是 ly/yr。相对论下加速买到的主要是船时：7 光年从 0.888c 加速到 0.987c，"
            "银河时只省 10%，船时省 69%（工资和药品腐败按船时算），能量要 4.4 倍。",
        ),
        ("能源与枢纽", "ports", "能源便宜的星站会不会成为枢纽。"),
        (
            "贸易网络",
            "network",
            "谁和谁做生意。光速延迟下远方的报价更旧，贸易是否收缩到彼此近的星团里，"
            "对比同一行的光速列和瞬时信息列。",
        ),
        (
            "过时报价与扎堆",
            "surprise",
            "意外度 < 1：到港时目的地的真实报价比出发时看到的差（报价过时、对手先到、冲击结束）。"
            "按到港时刻的真实报价算，与到港后卖不卖无关。",
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
        wall = "–（续跑）" if llm.get("cache_hits") else f"{(m['wall_s'] or 0) / 60:.1f} min"
        L.append(
            f"| {m['run_id']} | {m['net_mean']:,.0f} | {m['net_min']:,.0f} / "
            f"{m['net_max']:,.0f} | {m['trips']} | {m['empty_trips']} | {sm} | "
            f"{m['messages']} | {m['max_parallel']} | {wall} | "
            f"{llm.get('calls', '–')} | ${llm.get('cost_usd', 0):.2f} |"
        )
    L.append("")
    if any((m.get("llm") or {}).get("cache_hits") for m in per_run):
        L.append(
            "「续跑」：这一局中途因上游错误中止，之后从缓存逐字节重放到中止处再继续，墙钟只覆盖最后一次"
            "运行。花费按每个响应当初请求时的价格计，与是否重放无关。"
        )
        L.append("")
    # 消息与笔记本摘录
    llm_runs = [(r, m) for r, m in zip(runs, per_run, strict=True) if r.condition.startswith("llm")]
    if llm_runs:
        L.append("## 商人之间的消息（摘录）")
        L.append("")
        if not any(m["message_samples"] for _, m in llm_runs):
            L.append("没有任何 LLM 商人发过消息（send_message 一直可用，可以私信、广播、撒谎）。")
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
        for r, m in llm_runs[:3]:
            L.append(f"**{r.meta['run_id']}**")
            L.append("")
            for n, f in r.meta["final"].items():
                note = (f.get("notebook") or "").strip().replace("\n", " ")
                if note:
                    L.append(
                        f"- {n}（净值 {f['net_worth']:,.0f}¢，船时 {m['tau'][n]:.1f} 年）："
                        f"{note[:400]}"
                    )
            L.append("")
    (out / "report.md").write_text("\n".join(L) + "\n")
