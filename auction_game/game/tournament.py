"""整场流程：正赛、第一名并列时的加赛和最终排名。AI 发言通过 director 钩子接入。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional, Protocol

from auction_game.game.engine import DayEndCallback, compute_standings, run_match
from auction_game.game.game_rules import GameSettings
from auction_game.game.models import MatchLog, StandingsEntry
from auction_game.game.players import Player


class SpeechDirector(Protocol):
    """开局、局末和终局的发言编排；on_match_end 只在还有下一局时调用。"""

    def on_opening(self) -> None: ...
    def on_match_end(self, log: MatchLog, standings: list[StandingsEntry]) -> None: ...
    def on_final(self, standings: list[StandingsEntry], log: MatchLog) -> None: ...


@dataclass
class TournamentResult:
    match_logs: list[MatchLog] = field(default_factory=list)
    final_standings: list[StandingsEntry] = field(default_factory=list)
    playoff_count: int = 0
    playoff_cap_hit: bool = False  # 达到加赛上限仍并列，保留并列冠军


def first_place_tied(standings: list[StandingsEntry]) -> bool:
    """第一名总分是否有并列。"""
    top_score = standings[0].total_score
    return sum(1 for entry in standings if entry.total_score == top_score) >= 2


def run_tournament(
    players: list[Player],
    game: GameSettings,
    on_match_end: Optional[Callable[[MatchLog, list[StandingsEntry]], None]] = None,
    director: Optional[SpeechDirector] = None,
    bid_collector=None,
    *,
    on_day_end: DayEndCallback | None = None,
) -> TournamentResult:
    """跑完整场比赛。

    on_match_end 在每局（含加赛局）结束后收到该局日志和累计排名，即规则要求局末公开的信息。
    bid_collector 和 on_day_end 转交给 engine.run_match。
    """
    result = TournamentResult()
    auction_scores_history = {player.name: [] for player in players}
    scores_history = {player.name: [] for player in players}
    match_index = 0

    if director:
        director.on_opening()

    def play_one_match(is_playoff: bool) -> None:
        nonlocal match_index
        match_index += 1
        log = run_match(
            players,
            match_index=match_index,
            is_playoff=is_playoff,
            auction_scores_history=auction_scores_history,
            scores_history=scores_history,
            past_logs=result.match_logs,
            game=game,
            bid_collector=bid_collector,
            on_day_end=on_day_end,
        )
        for name, auction_score in log.auction_scores.items():
            auction_scores_history[name].append(auction_score)
        for name, score in log.scores.items():
            scores_history[name].append(score)
        result.match_logs.append(log)
        standings = compute_standings(auction_scores_history, scores_history=scores_history)
        if on_match_end:
            on_match_end(log, standings)
        # 正赛打完后，第一名仍并列且没到加赛上限才有下一局；这时加赛局的 playoff_count 还没自增。
        has_next_match = match_index < game.regular_matches or (
            first_place_tied(standings)
            and result.playoff_count + (1 if is_playoff else 0) < game.max_playoffs
        )
        # 最后一局不做局末发言，它的出价日志随终局感言（on_final）发出。
        if director and has_next_match:
            director.on_match_end(log, standings)

    for _ in range(game.regular_matches):
        play_one_match(is_playoff=False)

    while first_place_tied(compute_standings(auction_scores_history, scores_history=scores_history)):
        if result.playoff_count >= game.max_playoffs:
            result.playoff_cap_hit = True
            break
        play_one_match(is_playoff=True)
        result.playoff_count += 1

    result.final_standings = compute_standings(auction_scores_history, scores_history=scores_history)
    if director:
        director.on_final(result.final_standings, result.match_logs[-1])
    return result
