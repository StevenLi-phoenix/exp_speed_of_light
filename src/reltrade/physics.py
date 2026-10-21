"""运动学与信息传播。

单位：光年（ly）、年（yr），c = 1 ly/yr；质量以船体干重 M 为单位，能量以 M·c² 为单位。
所有星站静止在同一惯性系里，共用坐标时 t；只有船有固有时 τ。
飞船瞬时加速到巡航速度、匀速滑行、到站瞬时减速，世界线是一条匀速直线段。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Protocol

LIGHT = 1.0  # 信息以光速传播
INSTANT = math.inf  # c→∞ 对照组：信息瞬时到达


@dataclass(frozen=True)
class Leg:
    """一段航程。"""

    distance: float
    speed: float
    dt: float  # 坐标时（星站钟）
    dtau: float  # 固有时（船钟）
    energy_per_mass: float  # 加速 + 减速的总能耗，每单位静质量


class Kinematics(Protocol):
    name: str
    min_speed: float
    max_speed: float
    speed_unit: str

    def leg(self, distance: float, speed: float) -> Leg: ...


def _check_speed(speed: float, lo: float, hi: float, what: str) -> None:
    if not (math.isfinite(speed) and lo <= speed <= hi):
        raise ValueError(f"{what} must be in [{lo}, {hi}], got {speed}")


@dataclass(frozen=True)
class Relativistic:
    """speed = β（光速的分数）。E = 2(γ-1)m，dτ/dt = √(1-β²)。"""

    name: str = "relativistic"
    min_speed: float = 0.001
    max_speed: float = 0.99999
    speed_unit: str = "c"

    def leg(self, distance: float, speed: float) -> Leg:
        _check_speed(speed, self.min_speed, self.max_speed, "beta")
        # 1-β² 写成 (1-β)(1+β)，避免 β→1 时的抵消误差
        inv_gamma = math.sqrt((1.0 - speed) * (1.0 + speed))
        # γ-1 = β² / (√(1-β²)(1+√(1-β²)))，低速时也不丢精度
        gamma_minus_1 = speed * speed / (inv_gamma * (1.0 + inv_gamma))
        dt = distance / speed
        return Leg(distance, speed, dt, dt * inv_gamma, 2.0 * gamma_minus_1)


@dataclass(frozen=True)
class Newtonian:
    """c→∞ 极限：speed = v（ly/yr），无时间膨胀，E = 2 × ½mv² = mv²。"""

    name: str = "newtonian"
    min_speed: float = 0.001
    max_speed: float = 20.0
    speed_unit: str = "ly/yr"

    def leg(self, distance: float, speed: float) -> Leg:
        _check_speed(speed, self.min_speed, self.max_speed, "speed")
        dt = distance / speed
        return Leg(distance, speed, dt, dt, speed * speed)


def kinematics_by_name(name: str) -> Kinematics:
    if name == "relativistic":
        return Relativistic()
    if name == "newtonian":
        return Newtonian()
    raise ValueError(f"unknown kinematics {name!r}")


def light_lag(distance: float, c_info: float) -> float:
    """信息跨越 distance 需要的坐标时；c_info = ∞ 时为 0。"""
    if distance == 0.0 or math.isinf(c_info):
        return 0.0
    return distance / c_info
