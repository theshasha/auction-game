"""本场阵容：按座位排列的玩家名字、人设和赛前预测。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PlayerProfile:
    """predicted_win_rate 是 players.yaml 里虚构的公开预测，不是模型的真实表现统计。"""

    name: str
    persona_title: str
    persona_text: str
    predicted_win_rate: int | float


@dataclass(frozen=True)
class Lineup:
    profiles: tuple[PlayerProfile, ...]

    @property
    def names(self) -> list[str]:
        return [profile.name for profile in self.profiles]

    def others(self, name: str) -> tuple[str, ...]:
        """除 name 以外的玩家，按座位顺序。"""
        return tuple(other for other in self.names if other != name)
