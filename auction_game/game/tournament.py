"""整场流程：按本场参数运行正赛、第一名并列时的加赛循环与最终排名结算。

发言编排通过可选的 director 钩子注入，规则逻辑本身不依赖任何 AI 代码。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional, Protocol

from auction_game.game.engine import compute_standings, run_match
from auction_game.game.game_rules import GameSettings
from auction_game.game.models import MatchLog, StandingsEntry
from auction_game.game.players import Player


class SpeechDirector(Protocol):
    """发言编排钩子（开局/局末/终局）。Bot 模式传 None，行为完全不变。"""

    def on_opening(self) -> None: ...
    def on_match_end(self, log: MatchLog, standings: list[StandingsEntry], will_continue: bool) -> None: ...
    def on_final(self, standings: list[StandingsEntry], log: MatchLog) -> None: ...


@dataclass
class TournamentResult:
    match_logs: list[MatchLog] = field(default_factory=list)
    final_standings: list[StandingsEntry] = field(default_factory=list)
    playoff_count: int = 0
    playoff_cap_hit: bool = False  # 达到加赛上限仍并列，保留并列冠军


def first_place_tied(standings: list[StandingsEntry]) -> bool:
    """第一名总分是否有并列。"""
    if not standings:
        return False
    top_score = standings[0].total_score
    return sum(1 for entry in standings if entry.total_score == top_score) >= 2


def run_tournament(
    players: list[Player],
    game: GameSettings,
    on_match_end: Optional[Callable[[MatchLog, list[StandingsEntry]], None]] = None,
    director: Optional[SpeechDirector] = None,
    bid_collector=None,
) -> TournamentResult:
    """跑完整场比赛。

    on_match_end: 每局（含加赛局）结束后的回调，收到该局完整日志与截至当前的
    累计排名——这两者即规则要求局末向全体玩家公开的信息。
    director: 发言编排（开局发言、局末发言+下局规划、终局感言+观众总结）。
    bid_collector: AI 模式的并行出价收集器，透传给 engine.run_match。
    """
    names = [player.name for player in players]
    if len(names) < 2:
        raise ValueError("比赛至少需要两名玩家")
    if any(not isinstance(name, str) or not name.strip() for name in names):
        raise ValueError("玩家名字不能为空")
    if len(set(names)) != len(names):
        raise ValueError("玩家名字必须唯一")
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
        )
        for name, auction_score in log.auction_scores.items():
            auction_scores_history[name].append(auction_score)
        for name, score in log.scores.items():
            scores_history[name].append(score)
        result.match_logs.append(log)
        standings = compute_standings(auction_scores_history, scores_history=scores_history)
        if on_match_end:
            on_match_end(log, standings)
        if director:
            # 正赛末局或加赛后仍并列才有下一局；加赛局还要预留上限余量
            # （正赛局结束后 playoff_count 未自增，只有加赛局需 +1）
            will_continue = match_index < game.regular_matches or (
                first_place_tied(standings)
                and result.playoff_count + (1 if is_playoff else 0) < game.max_playoffs
            )
            # 最后一局跳过局末发言：该局的完整出价日志并入终局感言（on_final）
            if will_continue:
                director.on_match_end(log, standings, will_continue)

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
