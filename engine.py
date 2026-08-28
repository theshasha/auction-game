"""单局规则引擎（裁判）。与玩家实现完全解耦，只依赖 Player 接口。"""

from __future__ import annotations

from collections import Counter

from models import (
    BID_LIMIT,
    COLLISION_PENALTY,
    DIVIDEND_DIVISOR,
    INITIAL_COINS,
    MAX_DAYS,
    WINS_PER_DAY,
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
    current_match_penalties: dict[str, int] | None = None,
) -> list[StandingsEntry]:
    """根据各局胜场分布计算累计排名（并列同名次，取最高名次）。

    current_match_wins / current_match_penalties：进行中的本局已决出胜场与
    已发生罚没，仅影响总胜场，不进各局明细（明细在局末折算后写入 wins_history）。
    """
    current_match_wins = current_match_wins or {}
    current_match_penalties = current_match_penalties or {}
    entries = [
        StandingsEntry(
            player=name,
            total_wins=(
                sum(per_match)
                + current_match_wins.get(name, 0)
                - current_match_penalties.get(name, 0)
            ),
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


def fold_match_penalties(wins_history: dict[str, list[int]], log: MatchLog) -> None:
    """把一局产生的胜场罚没折算进各局胜场明细：从最近一个有净胜场的局往前扣。

    在 run_match 的 log.wins 已追加进 wins_history 之后调用。
    被罚者被罚时总胜场必然至少为 1（同价位玩家胜场不全相同则最多者 ≥1），
    因此罚没总额不超过累计胜场，折算后各局数值不会为负。
    """
    penalty_totals = {name: 0 for name in log.wins}
    for day in log.days:
        for name, amount in day.penalties:
            penalty_totals[name] += amount
    for name, amount in penalty_totals.items():
        remaining = amount
        for index in range(len(wins_history[name]) - 1, -1, -1):
            if remaining == 0:
                break
            deduction = min(wins_history[name][index], remaining)
            wins_history[name][index] -= deduction
            remaining -= deduction


def run_match(
    players: list[Player],
    match_index: int,
    is_playoff: bool,
    wins_history: dict[str, list[int]],
    past_logs: list[MatchLog],
    bid_collector=None,
) -> MatchLog:
    """运行一局（正赛局或加赛局规则相同），返回完整局日志。

    wins_history: 每名玩家在已结束各局的胜场分布，用于构造 Observation 中的排名。
    past_logs: 已结束各局的完整日志，局末公开后玩家可见。
    bid_collector: 可选，签名为 list[(player, obs)] -> list[原始出价]（顺序对齐），
    AI 模式注入并行收集；默认逐个顺序调用 decide_bid。暂停/失败的重跑由
    run_parallel_round 内部处理，对本函数透明。
    """
    coins = {player.name: INITIAL_COINS for player in players}
    match_wins = {player.name: 0 for player in players}          # 本局拍卖流程胜场（驱动 wins_remaining）
    match_penalties = {player.name: 0 for player in players}     # 本局已发生罚没（只影响累计排名）
    wins_remaining = WINS_PER_MATCH                              # 每天打包拍出 WINS_PER_DAY 个
    log = MatchLog(match_index=match_index, is_playoff=is_playoff)
    past_log_dicts = [past_log.to_dict() for past_log in past_logs]

    def ranking_wins(name: str) -> int:
        """计入累计排名的胜场：历史净胜场 + 本局已获胜场 - 本局已罚没。"""
        return sum(wins_history[name]) + match_wins[name] - match_penalties[name]

    day = 1
    while wins_remaining > 0 and day <= MAX_DAYS:
        if all(balance == 0 for balance in coins.values()):
            log.end_reason = "all_coins_zero"
            break

        # 同一天内 6 人的排名快照完全相同，循环外算一次
        standings_snapshot = compute_standings(wins_history, match_wins, match_penalties)
        observations = [
            (
                player,
                Observation(
                    my_name=player.name,
                    my_coins=coins[player.name],
                    my_match_wins=match_wins[player.name],
                    my_total_wins=ranking_wins(player.name),
                    day=day,
                    wins_remaining=wins_remaining,
                    max_days=MAX_DAYS,
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
        unique_prices_desc = sorted(
            (price for price, count in price_counts.items() if count == 1),
            reverse=True,
        )

        def collect_collision_penalties(taxable_prices: list[int]) -> list[tuple[str, int]]:
            """高位撞车罚没：同一撞车价位上总胜场不全相同时，最多者各罚 1 个累计胜场。

            正常日只传入高于获胜价的撞车价位，流局日传入全部撞车价位。
            罚没只落累计排名，不影响当天胜负、金币结算与本局拍卖流程。
            """
            penalties: list[tuple[str, int]] = []
            for price in sorted(taxable_prices, reverse=True):
                offenders = [record.player for record in bids if record.bid == price]
                totals = {name: ranking_wins(name) for name in offenders}
                if len(set(totals.values())) == 1:
                    continue
                top_wins = max(totals.values())
                for name, total in totals.items():
                    if total == top_wins:
                        match_penalties[name] += COLLISION_PENALTY
                        penalties.append((name, COLLISION_PENALTY))
            return penalties

        if not unique_prices_desc:
            # 流局：不产生胜场，全员出价全额返还（即不扣除），无分红；
            # 所有价位必然都撞车，全部参与罚没检查
            voided_count = sum(1 for count in price_counts.values() if count >= 2)
            penalties = collect_collision_penalties(
                [price for price, count in price_counts.items() if count >= 2]
            )
            log.days.append(
                DayResult(
                    day=day,
                    bids=bids,
                    winner=None,
                    winning_bid=None,
                    voided_price_count=voided_count,
                    dividend=0,
                    is_void=True,
                    penalties=penalties,
                )
            )
        else:
            winning_bid = unique_prices_desc[0]
            winner = next(record.player for record in bids if record.bid == winning_bid)
            # 作废价位（高于获胜价且两人及以上撞车）即正常日的罚没检查范围
            taxable_prices = [
                price
                for price, count in price_counts.items()
                if count >= 2 and price > winning_bid
            ]
            voided_count = len(taxable_prices)

            # 出价即消耗：无论输赢立即扣除
            for record in bids:
                coins[record.player] -= record.bid
            wins_taken = min(WINS_PER_DAY, wins_remaining)  # 打包拍卖：赢家一次拿走 2 个胜场
            match_wins[winner] += wins_taken
            wins_remaining -= wins_taken

            # 当天结算后本局结束则不再分红（最后 2 个胜场拍出，或第 5 天结算）
            match_ends_today = wins_remaining == 0 or day == MAX_DAYS
            dividend = 0 if match_ends_today else sum(record.bid for record in bids) // DIVIDEND_DIVISOR
            if dividend:
                for player in players:
                    coins[player.name] += dividend

            penalties = collect_collision_penalties(taxable_prices)
            log.days.append(
                DayResult(
                    day=day,
                    bids=bids,
                    winner=winner,
                    winning_bid=winning_bid,
                    voided_price_count=voided_count,
                    dividend=dividend,
                    is_void=False,
                    penalties=penalties,
                )
            )

        day += 1

    if not log.end_reason:
        log.end_reason = "wins_exhausted" if wins_remaining == 0 else "max_days"
    log.wins = match_wins
    return log
