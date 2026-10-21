"""事件日志与光锥过滤。

观察者在 (x_S, t) 只知道满足 |x_e − x_S| ≤ c·(t − t_e) 的事件 e。
同一个谓词 `reaches` 同时用于可见性判断和并发调度，两处语义严格一致，
这样并发执行的结果与完成顺序无关。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from reltrade.physics import light_lag


def reaches(distance: float, c_info: float, t_src: float, t_obs: float) -> bool:
    """t_src 时刻从 distance 外发出的信号，到 t_obs 时是否已到达（含边界）。"""
    return t_obs - t_src >= light_lag(distance, c_info)


@dataclass(frozen=True)
class Event:
    id: str
    t: float
    station: str
    kind: str  # news | news_end | arrive | depart | trade | message
    actor: str | None = None  # 商人名；世界事件为 None
    audience: str | None = None  # None = 公开；否则只有该商人可见（私信）
    data: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {"id": self.id, "t": self.t, "station": self.station, "kind": self.kind,
                "actor": self.actor, "audience": self.audience, "data": self.data}


class EventLog:
    def __init__(self, distance: Callable[[str, str], float], c_info: float) -> None:
        self._distance = distance
        self._c = c_info
        self._events: list[Event] = []
        self._ids: set[str] = set()

    def add(self, event: Event) -> None:
        if event.id in self._ids:
            raise ValueError(f"duplicate event id {event.id}")
        self._ids.add(event.id)
        self._events.append(event)

    def visible(self, station: str, t: float, viewer: str | None = None) -> list[Event]:
        """(station, t) 处光锥内、viewer 有权看的全部事件，按 (t, id) 排序。"""
        out = [
            e for e in self._events
            if (e.audience is None or e.audience == viewer)
            and reaches(self._distance(e.station, station), self._c, e.t, t)
        ]
        out.sort(key=lambda e: (e.t, e.id))
        return out

    def all(self) -> list[Event]:
        return sorted(self._events, key=lambda e: (e.t, e.id))
