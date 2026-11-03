"""赛程：正赛打完后第一名并列就加赛，到上限为止；以及整场的最终结果。"""

from __future__ import annotations

from dataclasses import dataclass

from auction_game.game.settings import GameSettings
from auction_game.game.models import MatchLog, StandingsEntry


def first_place_tied(standings: list[StandingsEntry]) -> bool:
    """第一名总分是否有并列。"""
    top_score = standings[0].total_score
    return sum(1 for entry in standings if entry.total_score == top_score) >= 2


def has_next_match(logs: list[MatchLog], standings: list[StandingsEntry], game: GameSettings) -> bool:
    """logs 是已结束的全部局，standings 是它们的累计排名。

    正赛没打完，或第一名并列且加赛没到上限，就还有下一局；正赛打完后的局都是加赛。
    """
    playoffs = sum(log.is_playoff for log in logs)
    return len(logs) < game.regular_matches or (first_place_tied(standings) and playoffs < game.max_playoffs)


@dataclass
class TournamentResult:
    match_logs: list[MatchLog]
    final_standings: list[StandingsEntry]

    @property
    def playoff_count(self) -> int:
        return sum(log.is_playoff for log in self.match_logs)

    @property
    def playoff_cap_hit(self) -> bool:
        """比赛结束时第一名仍并列，只会是加赛到了上限，保留并列冠军。"""
        return first_place_tied(self.final_standings)
