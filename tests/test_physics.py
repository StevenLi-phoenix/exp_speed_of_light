import math

import pytest

from reltrade.physics import INSTANT, LIGHT, Newtonian, Relativistic, light_lag

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("beta", "dt", "dtau", "gamma_minus_1"),
    [
        # handoff 里已算好的 1 光年航程数值（c = 1）
        (0.9, 1.111, 0.484, 1.294),
        (0.99, 1.010, 0.142, 6.089),
        (0.999, 1.001, 0.045, 21.366),
    ],
)
def test_relativistic_leg_matches_handoff_numbers(
    beta: float, dt: float, dtau: float, gamma_minus_1: float
) -> None:
    leg = Relativistic().leg(1.0, beta)
    assert leg.dt == pytest.approx(dt, abs=1e-3)
    assert leg.dtau == pytest.approx(dtau, abs=1e-3)
    # 单程能耗 E = 2(γ-1)mc²：加速、减速各付一次
    assert leg.energy_per_mass == pytest.approx(2 * gamma_minus_1, abs=2e-3)


def test_relativistic_gamma_minus_one_is_accurate_near_c() -> None:
    beta = 0.9999
    exact = 1 / math.sqrt((1 - beta) * (1 + beta)) - 1
    assert Relativistic().leg(1.0, beta).energy_per_mass == pytest.approx(2 * exact, rel=1e-12)


def test_relativistic_low_speed_limit_is_newtonian() -> None:
    v = 1e-3
    rel = Relativistic().leg(5.0, v)
    newt = Newtonian().leg(5.0, v)
    assert rel.energy_per_mass == pytest.approx(newt.energy_per_mass, rel=1e-5)
    assert rel.dtau == pytest.approx(newt.dtau, rel=1e-5)


def test_newtonian_leg() -> None:
    leg = Newtonian().leg(6.0, 3.0)
    assert leg.dt == pytest.approx(2.0)
    assert leg.dtau == pytest.approx(2.0)  # 无时间膨胀
    assert leg.energy_per_mass == pytest.approx(9.0)  # 2 × ½v²


@pytest.mark.parametrize("bad", [0.0, -0.5, 1.0, 1.2, math.nan])
def test_relativistic_rejects_bad_speed(bad: float) -> None:
    with pytest.raises(ValueError):
        Relativistic().leg(1.0, bad)


def test_newtonian_rejects_bad_speed() -> None:
    k = Newtonian()
    with pytest.raises(ValueError):
        k.leg(1.0, 0.0)
    with pytest.raises(ValueError):
        k.leg(1.0, k.max_speed * 1.01)


def test_light_lag() -> None:
    assert light_lag(8.6, LIGHT) == pytest.approx(8.6)
    assert light_lag(8.6, INSTANT) == 0.0
    assert light_lag(0.0, LIGHT) == 0.0
