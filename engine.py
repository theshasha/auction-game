"""单局规则引擎（裁判）。与玩家实现完全解耦，只依赖 Player 接口。"""

from __future__ import annotations

from collections import Counter

from models import (
    DAILY_POOL,
    DAILY_SALARY,
    INITIAL_COINS,
    MAX_BID,
    MAX_DAYS,
    BidRecord,
    DayResult,
    MatchLog,
    Observation,
    StandingsEntry,
)
from players import Player


def validate_bid(raw_bid: object, coins: int) -> tuple[int, str]:
    """校验出价。非法出价（超余额、超上限、非整数、负数）一律记 0。"""
    if type(raw_bid) is not int:  # bool 是 int 子类，同样视为非法
        return 0, "illegal"
    if raw_bid < 0 or raw_bid > MAX_BID or raw_bid > coins:
        return 0, "illegal"
    return raw_bid, "ok"


def compute_standings(
    wins_history: dict[str, list[int]],
    current_match_wins: dict[str, int] | None = None,
) -> list[StandingsEntry]:
    """根据各局胜场分布计算累计排名（并列同名次，取最高名次）。

    current_match_wins：进行中的本局已决出胜场，仅影响总胜场，
    不进各局明细（明细在局末写入 wins_history）。
    """
    current_match_wins = current_match_wins or {}
    entries = [
        StandingsEntry(
            player=name,
            total_wins=sum(per_match) + current_match_wins.get(name, 0),
            wins_per_match=list(per_match),
        )
        for name, per_match in wins_history.items()
    ]
    entries.sort(key=lambda entry: (-entry.total_wins, entry.player))
    for index, entry in enumerate(entries):
        if index > 0 and entry.total_wins == entries[index - 1].total_wins:
            entry.rank = entries[index - 1].rank
        else:
            entry.rank = index + 1
    return entries


def run_match(
    players: list[Player],
    match_index: int,
    is_playoff: bool,
    wins_history: dict[str, list[int]],
    past_logs: list[MatchLog],
    bid_collector=None,
) -> MatchLog:
    """运行一局（正赛局或加赛局规则相同），固定 5 天，返回完整局日志。

    wins_history: 每名玩家在已结束各局的胜场分布，用于构造 Observation 中的排名。
    past_logs: 已结束各局的完整日志，局末公开后玩家可见。
    bid_collector: 可选，签名为 list[(player, obs)] -> list[原始出价]（顺序对齐），
    AI 模式注入并行收集；默认逐个顺序调用 decide_bid。暂停/失败的重跑由
    run_parallel_round 内部处理，对本函数透明。
    """
    coins = {player.name: INITIAL_COINS for player in players}
    match_wins = {player.name: 0 for player in players}
    rollover = 0  # 流局并入次日奖池的胜场数
    log = MatchLog(match_index=match_index, is_playoff=is_playoff)
    past_log_dicts = [past_log.to_dict() for past_log in past_logs]

    for day in range(1, MAX_DAYS + 1):
        if day >= 2:
            for name in coins:
                coins[name] += DAILY_SALARY
        pool = DAILY_POOL[day - 1] + rollover

        # 同一天内 6 人的排名快照完全相同，循环外算一次
        standings_snapshot = compute_standings(wins_history, match_wins)
        observations = [
            (
                player,
                Observation(
                    my_name=player.name,
                    my_coins=coins[player.name],
                    my_match_wins=match_wins[player.name],
                    my_total_wins=sum(wins_history[player.name]) + match_wins[player.name],
                    day=day,
                    today_pool=pool,
                    today_history=[day_result.public_view() for day_result in log.days],
                    standings=standings_snapshot,
                    past_match_logs=past_log_dicts,
                ),
            )
            for player in players
        ]

        if bid_collector is None:
            raw_bids = [player.decide_bid(obs) for player, obs in observations]
        else:
            raw_bids = bid_collector(observations)

        bids: list[BidRecord] = []
        for (player, _), raw_bid in zip(observations, raw_bids):
            bid, status = validate_bid(raw_bid, coins[player.name])
            bids.append(BidRecord(player=player.name, raw_bid=raw_bid, bid=bid, status=status))

        price_counts = Counter(record.bid for record in bids)
        collided_players = {record.player for record in bids if price_counts[record.bid] >= 2}
        unique_prices_desc = sorted(
            (price for price, count in price_counts.items() if count == 1),
            reverse=True,
        )

        if unique_prices_desc:
            winning_bid = unique_prices_desc[0]
            winner = next(record.player for record in bids if record.bid == winning_bid)
            transfer_recipient = None
            transfer_wins = 0
            if winning_bid == MAX_BID:
                # 冲顶税与转判：获胜者得奖池一半（向上取整），另一半（向下取整）
                # 转判给次高唯一出价者；转判部分为 0（奖池 1）或不存在次高唯一出价者时作废
                wins_taken = -(-pool // 2)  # ceil(pool / 2)
                transfer_wins = pool // 2
                if transfer_wins > 0 and len(unique_prices_desc) >= 2:
                    runner_up_bid = unique_prices_desc[1]
                    transfer_recipient = next(
                        record.player for record in bids if record.bid == runner_up_bid
                    )
            else:
                wins_taken = pool  # 以 0~5 获胜拿全部奖池，无冲顶税、无转判
            match_wins[winner] += wins_taken
            if transfer_recipient is not None:
                match_wins[transfer_recipient] += transfer_wins
            rollover = 0
            is_void = False
        else:
            winner = None
            winning_bid = None
            wins_taken = 0
            transfer_recipient = None
            transfer_wins = 0
            rollover = pool  # 流局：奖池整体并入次日（第 5 天流局则随循环结束作废）
            is_void = True

        # 金币结算：获胜者、转判获得者与撞车者扣除各自出价（流局日撞车者同样扣除），
        # 其余玩家全额退还；扣除的金币由系统没收。
        # 流局日不存在唯一出价，全员必然撞车，即全员扣除。
        for record in bids:
            if (
                record.player == winner
                or record.player == transfer_recipient
                or record.player in collided_players
            ):
                coins[record.player] -= record.bid

        log.days.append(
            DayResult(
                day=day,
                bids=bids,
                pool=pool,
                winner=winner,
                winning_bid=winning_bid,
                wins_taken=wins_taken,
                transfer_recipient=transfer_recipient,
                transfer_wins=transfer_wins,
                collision_price_count=sum(1 for count in price_counts.values() if count >= 2),
                is_void=is_void,
            )
        )

    log.wins = match_wins
    return log
