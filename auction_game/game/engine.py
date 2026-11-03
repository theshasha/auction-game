"""单局规则引擎，只依赖 Player 接口。"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from typing import Callable, Optional

from auction_game.game.settings import GameSettings, validate_submission
from auction_game.game.models import (
    DayResult,
    MatchLog,
    Observation,
    PlayerDayResult,
    StandingsEntry,
)
from auction_game.game.players import Player


DayEndCallback = Callable[[MatchLog, list[StandingsEntry], dict[str, int]], None]
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


def run_match(
    players: list[Player],
    match_index: int,
    is_playoff: bool,
    finished_logs: list[MatchLog],
    game: GameSettings,
    bid_collector=None,
    *,
    on_day_end: DayEndCallback | None = None,
) -> MatchLog:
    """运行一局（正赛和加赛规则相同），返回局日志。

    finished_logs：已结束的各局，用于累计排名。
    bid_collector：list[(player, obs)] -> list[(正常出价, 超级出价)]，顺序对齐；
    AI 模式用它并行收集，默认逐个调用 decide_bid。
    on_day_end：每天结算后收到局日志、累计排名和余额快照，只给观众看。
    出价不符合规则时抛出 ValueError：AI 的违规在代理层纠错，到这里就是程序错误。
    """
    names = [player.name for player in players]
    coins = {name: game.initial_coins for name in names}
    log = MatchLog(match_index=match_index, is_playoff=is_playoff, game=game)

    def observe(day: int, pool: int) -> list[tuple[Player, Observation]]:
        """当天开始时每名玩家能看到的信息，全员共用同一份排名、余额和本局记录。"""
        # 全员余额第 2 天起公布，取发工资后的值；复制一份，当天结算不会改动它。
        all_coins = dict(coins) if day >= 2 else None
        standings = compute_standings(names, finished_logs, log)
        history = list(log.days)
        match_scores = log.scores
        return [(player, Observation(
            my_name=player.name,
            my_coins=coins[player.name],
            my_match_score=match_scores.get(player.name, 0),
            match_index=match_index,
            day=day,
            today_pool=pool,
            today_history=history,
            standings=standings,
            game=game,
            all_coins=all_coins,
        )) for player in players]

    for day in range(1, game.max_days + 1):
        if day >= 2:
            for name in coins:
                coins[name] += game.daily_salary
        pool = game.daily_pool[day - 1]
        observations = observe(day, pool)

        if bid_collector is None:
            submissions = [player.decide_bid(obs) for player, obs in observations]
        else:
            submissions = bid_collector(observations)
        entries = [(player.name, bid, super_bid)
                   for (player, _), (bid, super_bid) in zip(observations, submissions)]
        for name, bid, super_bid in entries:
            reason = validate_submission(bid, super_bid, coins[name], game)
            if reason is not None:
                raise ValueError(f"{name} 的出价不符合规则：{reason}")

        result = settle_day(day, pool, entries, game)
        for player_result in result.players:
            coins[player_result.player] += player_result.coins_delta
        log.days.append(result)
        if on_day_end is not None:
            on_day_end(log, compute_standings(names, finished_logs, log), dict(coins))

    return log
