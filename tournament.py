"""整场流程：5 局正赛 + 第一名并列时的加赛循环 + 最终排名结算。

发言编排通过可选的 director 钩子注入，规则逻辑本身不依赖任何 AI 代码。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional, Protocol

from engine import compute_standings, run_match
from models import MatchLog, StandingsEntry
from players import Player

REGULAR_MATCHES = 5
MAX_PLAYOFFS = 20  # 安全上限，防止极端情况下加赛死循环


class SpeechDirector(Protocol):
    """发言编排钩子（开局/局末/终局）。Bot 模式传 None，行为完全不变。"""

    def on_opening(self) -> None: ...
    def on_match_end(self, log: MatchLog, standings: list[StandingsEntry], will_continue: bool) -> None: ...
    def on_final(self, standings: list[StandingsEntry]) -> None: ...


@dataclass
class TournamentResult:
    match_logs: list[MatchLog] = field(default_factory=list)
    final_standings: list[StandingsEntry] = field(default_factory=list)
    playoff_count: int = 0
    playoff_cap_hit: bool = False  # 触发加赛上限仍为并列的异常情况


def first_place_tied(standings: list[StandingsEntry]) -> bool:
    """第一名是否有并列。"""
    if not standings:
        return False
    top_wins = standings[0].total_wins
    return sum(1 for entry in standings if entry.total_wins == top_wins) >= 2


def run_tournament(
    players: list[Player],
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
    result = TournamentResult()
    wins_history = {player.name: [] for player in players}
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
            wins_history=wins_history,
            past_logs=result.match_logs,
            bid_collector=bid_collector,
        )
        for name, wins in log.wins.items():
            wins_history[name].append(wins)
        result.match_logs.append(log)
        standings = compute_standings(wins_history)
        if on_match_end:
            on_match_end(log, standings)
        if director:
            will_continue = (
                match_index < REGULAR_MATCHES
                or first_place_tied(standings)  # 第 5 局或加赛后仍并列，才有下一局
            )
            director.on_match_end(log, standings, will_continue)

    for _ in range(REGULAR_MATCHES):
        play_one_match(is_playoff=False)

    while first_place_tied(compute_standings(wins_history)):
        if result.playoff_count >= MAX_PLAYOFFS:
            result.playoff_cap_hit = True
            break
        play_one_match(is_playoff=True)
        result.playoff_count += 1

    result.final_standings = compute_standings(wins_history)
    if director:
        director.on_final(result.final_standings)
    return result
