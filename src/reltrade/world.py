"""世界：星图、商品、星站产业、外生库存波动与冲击。

世界完全由 seed 决定，与商人行为无关；冲击和噪声预先生成，所以任何一局都能精确重放。
"""

from __future__ import annotations

import math
from bisect import bisect_right
from collections.abc import Iterator
from dataclasses import dataclass, field

import numpy as np

ENERGY = "energy"


@dataclass(frozen=True)
class Good:
    name: str
    mass: float  # M / 单位
    half_life: float | None  # 按船时计的半衰期（年）；None = 不腐败


GOODS: dict[str, Good] = {
    "ore": Good("ore", 0.008, None),
    "tech": Good("tech", 0.0015, None),
    "med": Good("med", 0.003, 3.0),
}


@dataclass(frozen=True)
class MarketSpec:
    p_eq: float  # 库存处于正常水平时的中间价（¢/单位；能源为 ¢/(M·c²)）
    depth: float  # 正常库存


@dataclass(frozen=True)
class StationSpec:
    name: str
    pos: tuple[float, float, float]  # ly，以太阳为原点的赤道直角坐标
    markets: dict[str, MarketSpec]  # 三种商品 + energy
    blurb: str


@dataclass(frozen=True)
class Rules:
    t_end: float = 100.0
    start_cash: float = 2000.0
    hull_mass: float = 1.0
    hold_capacity: float = 1.0
    wage: float = 20.0  # ¢ / 船时年
    spread: float = 0.04
    relax_time: float = 2.0  # 库存回归正常水平的时间常数（年）
    max_trade_fraction: float = 0.9  # 单笔最多买走现有库存的比例
    min_wait: float = 0.1
    max_wait: float = 20.0
    default_wait: float = 1.0  # 商人没做决定时的强制等待
    max_messages_per_dock: int = 3
    max_message_chars: int = 600
    max_notebook_chars: int = 2000
    # 外生波动
    noise_step: float = 0.5
    noise_sigma: float = 0.15
    noise_corr: float = 5.0
    energy_noise_sigma: float = 0.10
    shock_rate: float = 1 / 6  # 每年
    shock_window: tuple[float, float] = (3.0, 92.0)


# 赤经（时 分 秒）、赤纬（度 分 秒）、距离（ly），J2000；数据来自 RECONS / Wikipedia
_STAR_COORDS: dict[str, tuple[tuple[float, float, float], tuple[float, float, float], float]] = {
    "Alpha Centauri": ((14, 39, 36.5), (-60, 50, 2), 4.37),
    "Wolf 359": ((10, 56, 29.2), (7, 0, 53), 7.86),
    "Lalande 21185": ((11, 3, 20.2), (35, 58, 12), 8.31),
    "Sirius": ((6, 45, 8.9), (-16, 42, 58), 8.60),
    "Procyon": ((7, 39, 18.1), (5, 13, 30), 11.46),
    "Ross 128": ((11, 47, 44.4), (0, 48, 16), 11.01),
}


def _cartesian(
    ra: tuple[float, float, float], dec: tuple[float, float, float], dist: float
) -> tuple[float, float, float]:
    h, m, s = ra
    d, dm, ds = dec
    ra_rad = math.radians((h + m / 60 + s / 3600) * 15)
    sign = -1.0 if d < 0 else 1.0
    dec_rad = math.radians(sign * (abs(d) + dm / 60 + ds / 3600))
    return (
        dist * math.cos(dec_rad) * math.cos(ra_rad),
        dist * math.cos(dec_rad) * math.sin(ra_rad),
        dist * math.sin(dec_rad),
    )


def _pos(name: str) -> tuple[float, float, float]:
    if name == "Sol":
        return (0.0, 0.0, 0.0)
    ra, dec, dist = _STAR_COORDS[name]
    return _cartesian(ra, dec, dist)


def _m(ore: tuple[float, float], tech: tuple[float, float], med: tuple[float, float],
       energy: tuple[float, float]) -> dict[str, MarketSpec]:
    return {
        "ore": MarketSpec(*ore),
        "tech": MarketSpec(*tech),
        "med": MarketSpec(*med),
        ENERGY: MarketSpec(*energy),
    }


# 产业设定：(正常中间价, 正常库存)。三个星团——核心（Sol / α Cen）、
# 红矮星团（Wolf 359 / Lalande / Ross 128，彼此 3.9–6.6 ly）、亮星对（Sirius / Procyon，5.3 ly）。
# 能源最便宜的 Sirius 故意放在边缘，避免"几何中心"和"能源便宜"两个枢纽成因混在一起。
STATIONS: dict[str, StationSpec] = {
    s.name: s
    for s in [
        StationSpec("Sol", _pos("Sol"), _m((12, 300), (35, 250), (32, 300), (20, 1500)),
                    "old core world; makes cheap tech, hungry for ore"),
        StationSpec("Alpha Centauri", _pos("Alpha Centauri"),
                    _m((13, 250), (45, 150), (40, 250), (25, 800)),
                    "industrial colony; consumes ore, needs medicine"),
        StationSpec("Wolf 359", _pos("Wolf 359"), _m((4, 400), (75, 120), (45, 200), (35, 500)),
                    "mining outpost on a flare star; cheap ore, pricey energy, needs med and tech"),
        StationSpec("Lalande 21185", _pos("Lalande 21185"),
                    _m((9, 250), (75, 120), (14, 400), (28, 600)),
                    "bio-colony; cheap medicine, needs tech"),
        StationSpec("Ross 128", _pos("Ross 128"), _m((5, 400), (80, 120), (20, 350), (30, 600)),
                    "temperate frontier world; ore and some medicine, needs tech"),
        StationSpec("Sirius", _pos("Sirius"), _m((13, 300), (60, 150), (48, 250), (3, 5000)),
                    "solar-power giant; by far the cheapest energy, needs ore and medicine"),
        StationSpec("Procyon", _pos("Procyon"), _m((10, 250), (32, 250), (50, 250), (8, 2000)),
                    "tech foundries; cheap tech and cheap energy, needs medicine"),
    ]
}

MERCHANT_NAMES: tuple[str, ...] = ("Asel", "Bram", "Chen", "Dara", "Emre", "Faye")


@dataclass(frozen=True)
class EqSchedule:
    """某星站某商品"正常库存倍率"的分段常数时间表：[times[k], times[k+1]) 内为 mults[k]。"""

    times: tuple[float, ...]
    mults: tuple[float, ...]

    def mult_at(self, t: float) -> float:
        i = max(bisect_right(self.times, t) - 1, 0)
        return self.mults[i]

    def segments(self, t0: float, t1: float) -> Iterator[tuple[float, float, float]]:
        """按断点切分 [t0, t1]，逐段给出 (起, 止, 倍率)。"""
        if t1 <= t0:
            return
        i = max(bisect_right(self.times, t0) - 1, 0)
        a = t0
        while a < t1:
            nxt = self.times[i + 1] if i + 1 < len(self.times) else math.inf
            b = min(nxt, t1)
            yield a, b, self.mults[i]
            a = b
            i += 1


@dataclass(frozen=True)
class Shock:
    station: str
    good: str
    kind: str  # shortage | glut | crisis
    t_start: float
    t_end: float
    factor: float  # 乘在正常库存上：<1 短缺（涨价），>1 过剩（跌价）
    headline: str
    end_headline: str


_HEADLINES: dict[tuple[str, str], str] = {
    ("shortage", "ore"): "Ore shortage at {s}: the mines have stopped, ore stocks are collapsing",
    ("shortage", "tech"): "Tech shortage at {s}: fabrication plants offline",
    ("shortage", "med"): "Epidemic at {s}: medicine demand is surging",
    ("glut", "ore"): "Ore glut at {s}: rich new deposits are flooding the market",
    ("glut", "tech"): "Tech glut at {s}: overproduction, warehouses full",
    ("glut", "med"): "Bumper harvest at {s}: medicine stocks overflowing",
    ("crisis", ENERGY): "Energy crisis at {s}: main reactor failure, energy rationed",
}


@dataclass(frozen=True)
class World:
    seed: int
    rules: Rules
    goods: dict[str, Good]
    stations: dict[str, StationSpec]
    shocks: tuple[Shock, ...]
    schedules: dict[tuple[str, str], EqSchedule]
    starts: dict[str, str]  # 商人 → 起始星站
    _dist: dict[tuple[str, str], float] = field(repr=False)

    def distance(self, a: str, b: str) -> float:
        return self._dist[(a, b)]

    @property
    def station_names(self) -> list[str]:
        return list(self.stations)

    @property
    def merchant_names(self) -> list[str]:
        return list(self.starts)


def _ou_log_mults(rng: np.random.Generator, n: int, step: float, sigma: float,
                  corr: float) -> np.ndarray:
    """平稳标准差 sigma、相关时间 corr 的 OU 过程（对数倍率），从 0 出发。"""
    a = math.exp(-step / corr)
    b = sigma * math.sqrt(1 - a * a)
    x = np.zeros(n)
    for k in range(1, n):
        x[k] = a * x[k - 1] + b * rng.standard_normal()
    return x


def _make_shocks(rng: np.random.Generator, rules: Rules, stations: list[str]) -> list[Shock]:
    shocks: list[Shock] = []
    lo, hi = rules.shock_window
    t = lo
    while True:
        t += rng.exponential(1 / rules.shock_rate)
        if t >= hi:
            break
        station = stations[rng.integers(len(stations))]
        r = rng.random()
        if r < 0.45:
            kind, good, factor = "shortage", list(GOODS)[rng.integers(3)], rng.uniform(0.2, 0.35)
        elif r < 0.75:
            kind, good, factor = "glut", list(GOODS)[rng.integers(3)], rng.uniform(2.5, 4.0)
        else:
            kind, good, factor = "crisis", ENERGY, rng.uniform(0.15, 0.3)
        duration = float(rng.uniform(3.0, 10.0))
        t_end = min(t + duration, rules.t_end)
        # 同一星站同一商品的冲击不重叠
        if any(s.station == station and s.good == good and t < s.t_end and s.t_start < t_end
               for s in shocks):
            continue
        headline = _HEADLINES[(kind, good)].format(s=station)
        headline += f" (expected to last ~{round(duration)} years)"
        shocks.append(Shock(station, good, kind, float(t), float(t_end), float(factor), headline,
                            f"{station}: the {good} market is returning to normal"))
    return shocks


def make_world(seed: int = 7, rules: Rules | None = None,
               merchants: tuple[str, ...] = MERCHANT_NAMES) -> World:
    rules = rules or Rules()
    names = list(STATIONS)
    # 每个组件用独立的随机流：起始位置、冲击、各市场噪声互不牵连，也不随 t_end 改变
    shocks = _make_shocks(np.random.default_rng([seed, 1]), rules, names)

    n = int(math.ceil(rules.t_end / rules.noise_step)) + 1
    grid = [k * rules.noise_step for k in range(n)]
    schedules: dict[tuple[str, str], EqSchedule] = {}
    for i, s in enumerate(names):
        for j, g in enumerate(STATIONS[s].markets):
            sigma = rules.energy_noise_sigma if g == ENERGY else rules.noise_sigma
            logm = _ou_log_mults(np.random.default_rng([seed, 2, i, j]), n, rules.noise_step,
                                 sigma, rules.noise_corr)
            mine = [sh for sh in shocks if sh.station == s and sh.good == g]
            cuts = sorted(set(grid) | {sh.t_start for sh in mine} | {sh.t_end for sh in mine})
            times, mults = [], []
            for c in cuts:
                k = min(int(c / rules.noise_step + 1e-9), n - 1)
                m = math.exp(logm[k])
                for sh in mine:
                    if sh.t_start <= c < sh.t_end:
                        m *= sh.factor
                times.append(float(c))
                mults.append(float(m))
            schedules[(s, g)] = EqSchedule(tuple(times), tuple(mults))

    # 起始星站：按 seed 打乱后依次分配，6 个商人占 7 个星站中的 6 个
    order = [names[i] for i in np.random.default_rng([seed, 0]).permutation(len(names))]
    starts = {m: order[i % len(order)] for i, m in enumerate(merchants)}

    dist = {(a, b): math.dist(STATIONS[a].pos, STATIONS[b].pos) for a in names for b in names}
    return World(seed, rules, GOODS, STATIONS, tuple(shocks), schedules, starts, dist)
