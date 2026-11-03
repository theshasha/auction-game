"""单局规则：判定和结算一天的出价、累计排名，以及进行中一局的状态。"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from typing import Optional

from auction_game.game.settings import GameSettings, validate_submission
from auction_game.game.models import (
    DayResult,
    MatchLog,
    Observation,
    PlayerDayResult,
    StandingsEntry,
)


Submission = tuple[str, int, Optional[int]]  # (玩家, 正常出价, 超级出价或 None)


def resolve_bids(
    normal_bids: Iterable[tuple[str, int]], super_values: Iterable[int],
) -> tuple[Optional[str], set[int], set[int]]:
    """判定当天结果，返回获胜者、撞车价位和超级出价命中的价位；只有正常出价能获胜。"""
    normal_bids = list(normal_bids)
    normal_counts = Counter(bid for _, bid in normal_bids)
    sniped_values = {value for value in super_values if value in normal_counts}
    burned_values = {
        value for value, count in normal_counts.items() if count >= 2 or value in sniped_values
    }
    winner = max(
        ((player, bid) for player, bid in normal_bids if bid not in burned_values),
        key=lambda item: item[1],
        default=(None, None),
    )[0]
    return winner, burned_values, sniped_values


def settle_day(day: int, pool: int, submissions: list[Submission], game: GameSettings) -> DayResult:
    """按规则结算一天的出价，出价须已通过校验。

    获胜和撞车的正常出价全额扣除，其余退还；超级出价每次扣固定成本，命中各领一份悬赏和命中分。
    获胜者取得当天奖池的拍卖分，流局时奖池作废。
    """
    winner, burned_values, sniped_values = resolve_bids(
        ((player, bid) for player, bid, _ in submissions),
        (super_bid for _, _, super_bid in submissions if super_bid is not None),
    )
    results = []
    for player, bid, super_bid in submissions:
        if player == winner:
            outcome = "won"
        elif bid in burned_values:
            outcome = "collided"
        else:
            outcome = "refunded"
        super_hit = super_bid in sniped_values
        coins_delta = 0 if outcome == "refunded" else -bid
        score = pool if outcome == "won" else 0
        if super_bid is not None:
            coins_delta -= game.super_cost
        if super_hit:
            coins_delta += game.super_bounty
            score += game.super_hit_score
        results.append(PlayerDayResult(
            player=player, bid=bid, super_bid=super_bid, outcome=outcome, sniped=bid in sniped_values,
            super_hit=super_hit, coins_delta=coins_delta, score=score,
        ))
    return DayResult(day=day, pool=pool, players=tuple(results))


def compute_standings(names: list[str], logs: list[MatchLog], current: MatchLog | None = None) -> list[StandingsEntry]:
    """按累计总分排名，同分并列；logs 是已结束的局，进行中的 current 只计入累计值。

    各局明细只包含已结束的局，拍卖分单独保留，不用于破同分。
    """
    current_scores = current.scores if current is not None else {}
    current_auction_scores = current.auction_scores if current is not None else {}
    entries = [
        StandingsEntry(
            player=name,
            total_score=sum(log.scores[name] for log in logs) + current_scores.get(name, 0),
            total_auction_score=sum(log.auction_scores[name] for log in logs) + current_auction_scores.get(name, 0),
            scores_per_match=[log.scores[name] for log in logs],
        )
        for name in names
    ]
    entries.sort(key=lambda entry: (-entry.total_score, entry.player))
    for index, entry in enumerate(entries):
        if index > 0 and entry.total_score == entries[index - 1].total_score:
            entry.rank = entries[index - 1].rank
        else:
            entry.rank = index + 1
    return entries


class MatchState:
    """进行中的一局：每名玩家的金币和已结算的天，按 start_day、settle 交替推进。"""

    def __init__(self, names: list[str], match_index: int, is_playoff: bool,
                 finished_logs: list[MatchLog], game: GameSettings):
        """finished_logs 是已结束的各局，只用于累计排名。"""
        self.names = list(names)
        self.finished_logs = list(finished_logs)
        self.game = game
        self.coins = {name: game.initial_coins for name in names}
        self.log = MatchLog(match_index=match_index, is_playoff=is_playoff, game=game)

    def standings(self) -> list[StandingsEntry]:
        """含本局已结算部分的累计排名。"""
        return compute_standings(self.names, self.finished_logs, self.log)

    def start_day(self) -> dict[str, Observation]:
        """开始下一天，第 2 天起先发工资；返回每名玩家当天能看到的信息，按座位顺序。

        全员共用同一份排名、余额和本局记录。
        """
        day = len(self.log.days) + 1
        if day >= 2:
            for name in self.coins:
                self.coins[name] += self.game.daily_salary
        # 全员余额第 2 天起公布，取发工资后的值；复制一份，当天结算不会改动它。
        all_coins = dict(self.coins) if day >= 2 else None
        standings = self.standings()
        history = list(self.log.days)
        match_scores = self.log.scores
        return {name: Observation(
            my_name=name,
            my_coins=self.coins[name],
            my_match_score=match_scores.get(name, 0),
            match_index=self.log.match_index,
            day=day,
            today_pool=self.game.daily_pool[day - 1],
            today_history=history,
            standings=standings,
            game=self.game,
            all_coins=all_coins,
        ) for name in self.names}

    def settle(self, submissions: dict[str, tuple[int, Optional[int]]]) -> DayResult:
        """结算 start_day 开始的这一天：submissions 是玩家到 (正常出价, 超级出价)。

        出价不符合规则时抛出 ValueError：AI 的违规在代理层纠错，到这里就是程序错误。
        """
        day = len(self.log.days) + 1
        entries = [(name, *submissions[name]) for name in self.names]
        for name, bid, super_bid in entries:
            reason = validate_submission(bid, super_bid, self.coins[name], self.game)
            if reason is not None:
                raise ValueError(f"{name} 的出价不符合规则：{reason}")
        result = settle_day(day, self.game.daily_pool[day - 1], entries, self.game)
        for player_result in result.players:
            self.coins[player_result.player] += player_result.coins_delta
        self.log.days.append(result)
        return result
