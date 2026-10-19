"""单局规则引擎，只依赖 Player 接口。"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from typing import Callable, Optional

from auction_game.game.game_rules import GameSettings, validate_submission
from auction_game.game.models import (
    BidRecord,
    DayResult,
    MatchLog,
    Observation,
    StandingsEntry,
)
from auction_game.game.players import Player


DayEndCallback = Callable[[MatchLog, list[StandingsEntry], dict[str, int]], None]


def resolve_bids(bids: list[BidRecord]) -> tuple[Optional[BidRecord], set[int], set[int]]:
    """判定当天结果，返回获胜记录、撞车价位和超级命中价位；只有正常出价能获胜。"""
    normal_bids = [record for record in bids if not record.is_super]
    normal_counts = Counter(record.bid for record in normal_bids)
    sniped_values = {
        record.bid for record in bids if record.is_super and record.bid in normal_counts
    }
    burned_values = {
        value for value, count in normal_counts.items() if count >= 2 or value in sniped_values
    }
    winning_record = max(
        (record for record in normal_bids if record.bid not in burned_values),
        key=lambda record: record.bid,
        default=None,
    )
    return winning_record, burned_values, sniped_values


def calculate_day_scores(
    player_names: Iterable[str],
    *,
    winner: str | None,
    auction_score: int,
    super_uses: list[dict],
    game: GameSettings,
) -> dict[str, int]:
    """直接累加已判定的拍卖分与逐人命中分，返回全员单日总分。"""
    scores = {name: 0 for name in player_names}
    if winner is not None:
        scores[winner] += auction_score
    for use in super_uses:
        if use["hit"]:
            scores[use["player"]] += game.super_hit_score
    return scores


def compute_standings(
    auction_scores_history: dict[str, list[int]],
    *,
    scores_history: dict[str, list[int]],
    current_match_auction_scores: dict[str, int] | None = None,
    current_match_scores: dict[str, int] | None = None,
) -> list[StandingsEntry]:
    """按累计总分排名，同分并列；进行中的本局成绩只计入累计值。

    各局明细只包含已完成局，拍卖分单独保留，不用于破同分。
    """
    current_match_auction_scores = current_match_auction_scores or {}
    current_match_scores = current_match_scores or {}
    entries = [
        StandingsEntry(
            player=name,
            total_auction_score=sum(per_match) + current_match_auction_scores.get(name, 0),
            auction_scores_per_match=list(per_match),
            total_score=sum(scores_history[name]) + current_match_scores.get(name, 0),
            scores_per_match=list(scores_history[name]),
        )
        for name, per_match in auction_scores_history.items()
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
    auction_scores_history: dict[str, list[int]],
    past_logs: list[MatchLog],
    game: GameSettings,
    bid_collector=None,
    *,
    scores_history: dict[str, list[int]],
    on_day_end: DayEndCallback | None = None,
) -> MatchLog:
    """运行一局（正赛和加赛规则相同），返回局日志。

    auction_scores_history / scores_history：每名玩家已结束各局的拍卖分和总分。
    bid_collector：list[(player, obs)] -> list[(正常出价, 超级出价)]，顺序对齐；
    AI 模式用它并行收集，默认逐个调用 decide_bid。
    on_day_end：每天结算后收到局日志、累计排名和余额快照，只给观众看。
    出价不符合规则时抛出 ValueError：AI 的违规在代理层纠错，到这里就是程序错误。
    """
    coins = {player.name: game.initial_coins for player in players}
    match_auction_scores = {player.name: 0 for player in players}
    match_scores = {player.name: 0 for player in players}
    log = MatchLog(match_index=match_index, is_playoff=is_playoff, game=game)
    past_log_dicts = [past_log.to_dict() for past_log in past_logs]

    for day in range(1, game.max_days + 1):
        if day >= 2:
            for name in coins:
                coins[name] += game.daily_salary
        pool = game.daily_pool[day - 1]

        # 昨日本人的结算结果；扣款只含正常出价，超级出价的成本和悬赏另列。
        my_last_by_player: dict[str, dict] = {}
        if log.days:
            yesterday = log.days[-1]
            sniped_values = {use["value"] for use in yesterday.super_uses if use["hit"]}
            super_by_player = {use["player"]: use for use in yesterday.super_uses}
            for record in yesterday.bids:
                if record.is_super:
                    continue
                collided = record.bid in yesterday.collision_values
                paid = record.player == yesterday.winner or collided
                super_use = super_by_player.get(record.player)
                my_last_by_player[record.player] = {
                    "my_last_bid": record.bid,
                    "my_last_deduction": record.bid if paid else 0,
                    "my_last_collided": collided,
                    "my_last_sniped": record.bid in sniped_values,
                    "my_last_super_bid": super_use["value"] if super_use else None,
                    "my_last_super_hit": bool(super_use and super_use["hit"]),
                }

        # 全员余额第 2 天起公布，取发工资后的值；复制一份，当天结算不会改动它。
        all_coins_snapshot = dict(coins) if day >= 2 else None
        standings_snapshot = compute_standings(
            auction_scores_history,
            scores_history=scores_history,
            current_match_auction_scores=match_auction_scores,
            current_match_scores=match_scores,
        )
        observations = []
        for player in players:
            observations.append(
                (
                    player,
                    Observation(
                        my_name=player.name,
                        my_coins=coins[player.name],
                        my_match_auction_score=match_auction_scores[player.name],
                        my_total_auction_score=sum(auction_scores_history[player.name]) + match_auction_scores[player.name],
                        my_match_score=match_scores[player.name],
                        my_total_score=sum(scores_history[player.name]) + match_scores[player.name],
                        day=day,
                        today_pool=pool,
                        today_history=[day_result.public_view() for day_result in log.days],
                        standings=standings_snapshot,
                        past_match_logs=past_log_dicts,
                        game=game,
                        all_coins=all_coins_snapshot,
                        **my_last_by_player.get(player.name, {}),
                    ),
                )
            )

        if bid_collector is None:
            submissions = [player.decide_bid(obs) for player, obs in observations]
        else:
            submissions = bid_collector(observations)

        bids: list[BidRecord] = []
        super_uses: list[dict] = []
        for (player, _), (bid, super_bid) in zip(observations, submissions):
            reason = validate_submission(bid, super_bid, coins[player.name], game)
            if reason is not None:
                raise ValueError(f"{player.name} 的出价不符合规则：{reason}")
            bids.append(BidRecord(player=player.name, bid=bid))
            if super_bid is not None:
                bids.append(BidRecord(player=player.name, bid=super_bid, is_super=True))
                super_uses.append({"player": player.name, "value": super_bid, "hit": False})

        winning_record, burned_normal_values, sniped_values = resolve_bids(bids)
        for use in super_uses:
            use["hit"] = use["value"] in sniped_values

        winner = winning_record.player if winning_record else None
        winning_bid = winning_record.bid if winning_record else None
        auction_score = pool if winning_record else 0
        if winner is not None:
            match_auction_scores[winner] += auction_score

        # 获胜和撞车的正常出价全额扣除，其余退还；超级出价每次扣固定成本，命中各领一份悬赏。
        for record in bids:
            if record.is_super:
                continue
            if record is winning_record or record.bid in burned_normal_values:
                coins[record.player] -= record.bid
        for use in super_uses:
            coins[use["player"]] -= game.super_cost
            if use["hit"]:
                coins[use["player"]] += game.super_bounty

        day_scores = calculate_day_scores(
            match_scores,
            winner=winner,
            auction_score=auction_score,
            super_uses=super_uses,
            game=game,
        )
        for name, score in day_scores.items():
            match_scores[name] += score

        log.days.append(
            DayResult(
                day=day,
                bids=bids,
                pool=pool,
                winner=winner,
                winning_bid=winning_bid,
                auction_score=auction_score,
                super_uses=super_uses,
                collision_values=sorted(burned_normal_values),
                is_void=winner is None,
            )
        )
        log.auction_scores = dict(match_auction_scores)
        log.scores = dict(match_scores)
        log.remaining_coins = dict(coins)
        if on_day_end is not None:
            standings = compute_standings(
                auction_scores_history,
                scores_history=scores_history,
                current_match_auction_scores=match_auction_scores,
                current_match_scores=match_scores,
            )
            on_day_end(log, standings, dict(coins))

    return log
