"""单局规则引擎（裁判）。与玩家实现完全解耦，只依赖 Player 接口。"""

from __future__ import annotations

from collections import Counter
from typing import Any, Optional

from models import (
    DAILY_POOL,
    DAILY_SALARY,
    INITIAL_COINS,
    MAX_BID,
    MAX_DAYS,
    SUPER_BOUNTY,
    BidRecord,
    DayResult,
    MatchLog,
    Observation,
    StandingsEntry,
)
from players import Player


def validate_bid(raw_bid: object, coins: int) -> tuple[int, str]:
    """校验正常出价。非法出价（超余额、超上限、非整数、负数）一律记 0。"""
    if type(raw_bid) is not int:  # bool 是 int 子类，同样视为非法
        return 0, "illegal"
    if raw_bid < 0 or raw_bid > MAX_BID or raw_bid > coins:
        return 0, "illegal"
    return raw_bid, "ok"


def validate_super_bid(raw_super_bid: object, normal_bid: int) -> Optional[int]:
    """校验超级出价：0~上限的整数（不查余额），且不能等于当天正常出价，
    非法返回 None（视为未使用）。

    AI 模式下非法提交在收集层已被拦截重试（InvalidBidError），这里是
    Bot 模式与防御性兜底，静默丢弃不会产生错误结算。
    """
    if type(raw_super_bid) is not int:  # bool 是 int 子类，同样视为非法
        return None
    if raw_super_bid < 0 or raw_super_bid > MAX_BID or raw_super_bid == normal_bid:
        return None
    return raw_super_bid


def normalize_bid_submission(submission: Any) -> tuple[Any, Any]:
    """把 decide_bid 的返回值统一为 (正常出价, 超级出价)。

    约定返回 (raw_bid, raw_super_bid) 二元组；兼容裸 int（无超级出价）。
    """
    if isinstance(submission, tuple):
        return submission
    return submission, None


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
    """运行一局（正赛局或加赛局规则相同），固定 6 天，返回完整局日志。

    wins_history: 每名玩家在已结束各局的胜场分布，用于构造 Observation 中的排名。
    past_logs: 已结束各局的完整日志，局末公开后玩家可见。
    bid_collector: 可选，签名为 list[(player, obs)] -> list[出价提交]（顺序对齐，
    每个提交为 (正常出价, 超级出价) 二元组），AI 模式注入并行收集；
    默认逐个顺序调用 decide_bid。暂停/失败的重跑由 run_parallel_round
    内部处理，对本函数透明。
    """
    coins = {player.name: INITIAL_COINS for player in players}
    match_wins = {player.name: 0 for player in players}
    rollover = 0  # 流局并入次日奖池的胜场数
    # 超级出价机会状态（引擎权威）：使用后不可用，命中后立即恢复（无上限）。
    # super_used_day 记录最近一次使用日（信息性），available 才是判定依据
    super_available: dict[str, bool] = {player.name: True for player in players}
    super_used_day: dict[str, int] = {}
    log = MatchLog(match_index=match_index, is_playoff=is_playoff)
    past_log_dicts = [past_log.to_dict() for past_log in past_logs]

    for day in range(1, MAX_DAYS + 1):
        if day >= 2:
            for name in coins:
                coins[name] += DAILY_SALARY
        pool = DAILY_POOL[day - 1] + rollover

        # 昨日各玩家的自我结算结果（正常出价/扣款/撞车/被狙/非法/超级出价命中），
        # 从昨日 DayResult 复算。扣款只可能来自正常出价（获胜或撞车）；
        # 超级出价不能获胜、任何结局都不扣款，命中则 +SUPER_BOUNTY 且机会恢复。
        my_last_by_player: dict[str, tuple[int, int, bool, bool, bool, Optional[int], bool]] = {}
        if log.days:
            yesterday_result = log.days[-1]
            yesterday_normal_counts = Counter(
                record.bid for record in yesterday_result.bids if not record.is_super
            )
            yesterday_sniped_values = {
                use["value"] for use in yesterday_result.super_uses if use["hit"]
            }
            yesterday_super_by_player = {
                use["player"]: use for use in yesterday_result.super_uses
            }
            for record in yesterday_result.bids:
                if record.is_super:
                    continue
                sniped = record.bid in yesterday_sniped_values
                collided = yesterday_normal_counts[record.bid] >= 2 or sniped
                won_with_normal = (
                    record.player == yesterday_result.winner
                    and record.bid == yesterday_result.winning_bid
                )
                paid = won_with_normal or collided
                my_super_use = yesterday_super_by_player.get(record.player)
                my_last_by_player[record.player] = (
                    record.bid,
                    record.bid if paid else 0,
                    collided,
                    sniped,
                    record.status != "ok",
                    my_super_use["value"] if my_super_use else None,
                    bool(my_super_use and my_super_use["hit"]),
                )

        # 同一天内 6 人的排名快照完全相同，循环外算一次；全场总金币
        # （规则五.1「其他人总金币」的全场部分，第 2 天起公布）同理，
        # 取工资发放后的余额，每人观察到的是全场减自己
        total_coins = sum(coins.values()) if day >= 2 else None
        standings_snapshot = compute_standings(wins_history, match_wins)
        observations = []
        for player in players:
            (
                last_bid,
                last_deduction,
                last_collided,
                last_sniped,
                last_illegal,
                last_super,
                last_super_hit,
            ) = my_last_by_player.get(player.name, (None, None, False, False, False, None, False))
            observations.append(
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
                        my_super_available=super_available[player.name],
                        my_super_used_day=super_used_day.get(player.name),
                        my_last_bid=last_bid,
                        my_last_deduction=last_deduction,
                        my_last_collided=last_collided,
                        my_last_sniped=last_sniped,
                        my_last_bid_illegal=last_illegal,
                        my_last_super_bid=last_super,
                        my_last_super_hit=last_super_hit,
                        others_coins_total=(
                            total_coins - coins[player.name]
                            if total_coins is not None
                            else None
                        ),
                        others_super_remaining=(
                            sum(
                                1
                                for name in coins
                                if name != player.name and super_available[name]
                            )
                            if day >= 2
                            else None
                        ),
                    ),
                )
            )

        if bid_collector is None:
            submissions = [player.decide_bid(obs) for player, obs in observations]
        else:
            submissions = bid_collector(observations)

        bids: list[BidRecord] = []
        super_uses: list[dict] = []
        for (player, _), submission in zip(observations, submissions):
            raw_bid, raw_super_bid = normalize_bid_submission(submission)
            bid, status = validate_bid(raw_bid, coins[player.name])
            bids.append(
                BidRecord(player=player.name, raw_bid=raw_bid, bid=bid, status=status)
            )
            # 超级出价：机会可用才受理（引擎权威去重），不能等于当天正常出价
            super_value = (
                validate_super_bid(raw_super_bid, bid) if raw_super_bid is not None else None
            )
            if super_value is not None and super_available[player.name]:
                bids.append(
                    BidRecord(
                        player=player.name,
                        raw_bid=raw_super_bid,
                        bid=super_value,
                        status="ok",
                        is_super=True,
                    )
                )
                super_uses.append({"player": player.name, "value": super_value, "hit": False})
                super_available[player.name] = False
                super_used_day[player.name] = day

        normal_counts = Counter(record.bid for record in bids if not record.is_super)

        # 狙击命中判定：超级出价数值上恰好 1 笔正常出价（其他超级出价不影响判定）
        for use in super_uses:
            use["hit"] = normal_counts[use["value"]] == 1
        sniped_values = {use["value"] for use in super_uses if use["hit"]}

        # 判定撞车：正常出价互撞（同值 ≥2 笔）或被狙中的唯一正常出价
        burned_normal_values = {
            record.bid
            for record in bids
            if not record.is_super
            and (normal_counts[record.bid] >= 2 or record.bid in sniped_values)
        }

        # 判定胜负：未撞车的正常出价中最高的一笔获胜，其主人获得当天全部奖池；
        # 正常出价全灭则流局（超级出价不参与获胜判定）
        winning_record = max(
            (
                record
                for record in bids
                if not record.is_super and record.bid not in burned_normal_values
            ),
            key=lambda record: record.bid,
            default=None,
        )

        if winning_record is not None:
            winning_bid = winning_record.bid
            winner = winning_record.player
            wins_taken = pool  # 任何价位获胜都拿全部奖池
            match_wins[winner] += wins_taken
            rollover = 0
            is_void = False
        else:
            winner = None
            winning_bid = None
            wins_taken = 0
            rollover = pool  # 流局：奖池整体并入次日（第 6 天流局则随循环结束作废）
            is_void = True

        # 金币结算：只扣正常出价——获胜的那笔正常出价、以及所有撞车的正常出价
        # （流局日必然全员撞车，即全员扣除）；超级出价任何结局都不扣款，
        # 其余正常出价全额退还。被扣除的金币由系统没收；
        # 命中悬赏 +SUPER_BOUNTY 由系统印发，且机会立即恢复（无上限）。
        for record in bids:
            if record.is_super:
                continue
            if record is winning_record or record.bid in burned_normal_values:
                coins[record.player] -= record.bid
        for use in super_uses:
            if use["hit"]:
                coins[use["player"]] += SUPER_BOUNTY
                super_available[use["player"]] = True

        log.days.append(
            DayResult(
                day=day,
                bids=bids,
                pool=pool,
                winner=winner,
                winning_bid=winning_bid,
                wins_taken=wins_taken,
                super_uses=super_uses,
                collision_values=sorted(burned_normal_values),
                is_void=is_void,
            )
        )

    log.wins = match_wins
    return log
