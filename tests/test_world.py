import pytest

from reltrade.world import ENERGY, EqSchedule, make_world

pytestmark = pytest.mark.unit


def test_world_is_deterministic_per_seed() -> None:
    a, b = make_world(7), make_world(7)
    assert a.shocks == b.shocks
    assert a.schedules == b.schedules
    assert a.starts == b.starts
    assert make_world(8).shocks != a.shocks


def test_real_star_distances() -> None:
    w = make_world(7)
    assert w.distance("Sol", "Alpha Centauri") == pytest.approx(4.37, abs=0.01)
    assert w.distance("Wolf 359", "Lalande 21185") == pytest.approx(4.07, abs=0.02)
    assert w.distance("Sirius", "Procyon") == pytest.approx(5.26, abs=0.02)
    for a in w.station_names:
        assert w.distance(a, a) == 0.0
        for b in w.station_names:
            assert w.distance(a, b) == pytest.approx(w.distance(b, a))
            if a != b:
                assert 3.5 < w.distance(a, b) < 13.5


def test_merchants_start_at_distinct_stations() -> None:
    w = make_world(7)
    assert len(w.starts) == 6
    assert len(set(w.starts.values())) == 6


def test_schedules_start_normal_and_apply_shock_factor() -> None:
    w = make_world(7)
    assert w.shocks, "seed 7 should produce shocks"
    for (s, g), sched in w.schedules.items():
        assert sched.mult_at(0.0) == pytest.approx(1.0)
    for sh in w.shocks:
        sched = w.schedules[(sh.station, sh.good)]
        before = sched.mult_at(sh.t_start - 1e-9)
        after = sched.mult_at(sh.t_start)
        # 冲击起点不在噪声网格上时，两侧噪声相同，比值就是冲击倍率
        if abs(sh.t_start / w.rules.noise_step - round(sh.t_start / w.rules.noise_step)) > 1e-6:
            assert after / before == pytest.approx(sh.factor)
        assert sh.good == ENERGY or sh.kind != "crisis"


def test_shocks_do_not_overlap_on_same_market() -> None:
    w = make_world(7)
    for i, a in enumerate(w.shocks):
        for b in w.shocks[i + 1:]:
            if a.station == b.station and a.good == b.good:
                assert a.t_end <= b.t_start or b.t_end <= a.t_start


def test_segments_cover_interval_contiguously() -> None:
    sched = EqSchedule((0.0, 1.0, 2.5), (1.0, 2.0, 3.0))
    segs = list(sched.segments(0.5, 3.0))
    assert segs == [(0.5, 1.0, 1.0), (1.0, 2.5, 2.0), (2.5, 3.0, 3.0)]
    assert list(sched.segments(2.0, 2.0)) == []
    assert sched.mult_at(-5.0) == 1.0
