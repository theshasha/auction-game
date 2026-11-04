"""本场阵容：按座位排列的玩家名字、人设、赛前预测和团队赛的阵营。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PlayerProfile:
    """predicted_win_rate 是 players.yaml 里虚构的公开预测，不是模型的真实表现统计；团队赛没有预测，为 None。

    team 是团队赛的阵营名，个人赛为 None。
    """

    name: str
    persona_title: str
    persona_text: str
    predicted_win_rate: int | float | None
    team: str | None = None


@dataclass(frozen=True)
class Lineup:
    profiles: tuple[PlayerProfile, ...]

    @property
    def names(self) -> list[str]:
        return [profile.name for profile in self.profiles]

    def others(self, name: str) -> tuple[str, ...]:
        """除 name 以外的玩家，按座位顺序。"""
        return tuple(other for other in self.names if other != name)

    @property
    def teams(self) -> dict[str, tuple[str, ...]]:
        """阵营名到队员，阵营和队员都按座位顺序；个人赛为空。"""
        teams: dict[str, list[str]] = {}
        for profile in self.profiles:
            if profile.team is not None:
                teams.setdefault(profile.team, []).append(profile.name)
        return {team: tuple(members) for team, members in teams.items()}

    @property
    def team_of(self) -> dict[str, str] | None:
        """玩家到阵营，交给结算和排名；个人赛为 None，按每人自成一个阵营处理。"""
        team_of = {profile.name: profile.team for profile in self.profiles if profile.team is not None}
        return team_of or None
