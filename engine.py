"""单局规则引擎（裁判）。与玩家实现完全解耦，只依赖 Player 接口。"""

from __future__ import annotations

from collections import Counter

from models import (
    BID_LIMIT,
    INITIAL_COINS,
    MAX_DAYS,
    WINS_PER_MATCH,
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
    if raw_bid < 0 or raw_bid > BID_LIMIT or raw_bid > coins:
        return 0, "illegal"
    return raw_bid, "ok"


def compute_standings(
    wins_history: dict[str, list[int]],
    current_match_wins: dict[str, int] | None = None,
) -> list[StandingsEntry]:
    """根据各局胜场分布计算累计排名（并列同名次，取最高名次）。"""
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
) -> MatchLog:
    """运行一局（正赛局或加赛局规则相同），返回完整局日志。

    wins_history: 每名玩家在已结束各局的胜场分布，用于构造 Observation 中的排名。
    past_logs: 已结束各局的完整日志，局末公开后玩家可见。
    """
    coins = {player.name: INITIAL_COINS for player in players}
    match_wins = {player.name: 0 for player in players}
    wins_remaining = WINS_PER_MATCH
    log = MatchLog(match_index=match_index, is_playoff=is_playoff)
    past_log_dicts = [past_log.to_dict() for past_log in past_logs]

    day = 1
    while wins_remaining > 0 and day <= MAX_DAYS:
        if all(balance == 0 for balance in coins.values()):
            log.end_reason = "all_coins_zero"
            break

        bids: list[BidRecord] = []
        for player in players:
            obs = Observation(
                my_name=player.name,
                my_coins=coins[player.name],
                my_match_wins=match_wins[player.name],
                my_total_wins=sum(wins_history[player.name]) + match_wins[player.name],
                day=day,
                wins_remaining=wins_remaining,
                max_days=MAX_DAYS,
                today_history=[day_result.public_view() for day_result in log.days],
                standings=compute_standings(wins_history, match_wins),
                past_match_logs=past_log_dicts,
            )
            raw_bid = player.decide_bid(obs)
            bid, status = validate_bid(raw_bid, coins[player.name])
            bids.append(BidRecord(player=player.name, raw_bid=raw_bid, bid=bid, status=status))

        price_counts = Counter(record.bid for record in bids)
        unique_prices_desc = sorted(
            (price for price, count in price_counts.items() if count == 1),
            reverse=True,
        )

        if not unique_prices_desc:
            # 流局：不产生胜场，全员出价全额返还（即不扣除），无分红
            voided_count = sum(1 for count in price_counts.values() if count >= 2)
            log.days.append(
                DayResult(
                    day=day,
                    bids=bids,
                    winner=None,
                    winning_bid=None,
                    voided_price_count=voided_count,
                    dividend=0,
                    is_void=True,
                )
            )
        else:
            winning_bid = unique_prices_desc[0]
            winner = next(record.player for record in bids if record.bid == winning_bid)
            # 作废价位数：高于获胜价且两人及以上撞车的不同价位（低于获胜价的不计入）
            voided_count = sum(
                1
                for price, count in price_counts.items()
                if count >= 2 and price > winning_bid
            )

            # 出价即消耗：无论输赢立即扣除
            for record in bids:
                coins[record.player] -= record.bid
            match_wins[winner] += 1
            wins_remaining -= 1

            # 当天结算后本局结束则不再分红（第 3 个胜场拍出，或第 5 天结算）
            match_ends_today = wins_remaining == 0 or day == MAX_DAYS
            dividend = 0 if match_ends_today else sum(record.bid for record in bids) // len(players)
            if dividend:
                for player in players:
                    coins[player.name] += dividend

            log.days.append(
                DayResult(
                    day=day,
                    bids=bids,
                    winner=winner,
                    winning_bid=winning_bid,
                    voided_price_count=voided_count,
                    dividend=dividend,
                    is_void=False,
                )
            )

        day += 1

    if not log.end_reason:
        log.end_reason = "wins_exhausted" if wins_remaining == 0 else "max_days"
    log.wins = match_wins
    return log
