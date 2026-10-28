"""批量策略对照与统计（不写日志）。"""

from __future__ import annotations

from collections import Counter
import random

from auction_game.configuration import ConfigurationError, load_game_settings
from auction_game.game.players import BOT_NAMES, RandomBot, ScriptedPlayer
from auction_game.game.tournament import run_tournament
from auction_game.game.rules import sync_root_rules


def run_benchmark(count: int) -> int:
    """两组各 count 场：第一组 6 个随机玩家，只作过程统计参考；
    第二组 1 个每日出最高可付价、不用超级出价的玩家对 5 个随机玩家，用于比较平均总分。
    """
    try:
        game = load_game_settings()
        sync_root_rules(game)
    except ConfigurationError as exc:
        print(f"错误：配置读取失败：{exc}")
        return 1

    def affordable_max_bid(obs):
        return min(obs.game.max_bid, obs.my_coins)

    def play_tournament(seed: int, with_affordable_max: bool):
        rng = random.Random(seed)
        players: list = []
        if with_affordable_max:
            players.append(ScriptedPlayer("最高可付价玩家", affordable_max_bid))
        for name in BOT_NAMES[: 5 if with_affordable_max else 6]:
            players.append(RandomBot(name, random.Random(rng.random())))
        return run_tournament(players, game)

    def aggregate(results) -> dict:
        stats = {
            "days": 0,
            "void_days": 0,
            "winning_bids": Counter(),
            "bids_by_day": {},
            "pools_by_day": {},
            "playoffs": 0,
        }
        for result in results:
            stats["playoffs"] += result.playoff_count
            for log in result.match_logs:
                for day in log.days:
                    stats["days"] += 1
                    stats["pools_by_day"].setdefault(day.day, []).append(day.pool)
                    if day.is_void:
                        stats["void_days"] += 1
                        continue
                    stats["winning_bids"][day.winning_bid] += 1
                    stats["bids_by_day"].setdefault(day.day, []).extend(
                        bid for result in day.players for bid in (result.bid, result.super_bid) if bid is not None
                    )
        return stats

    def print_stats(label: str, stats: dict) -> None:
        days = stats["days"]
        decided = days - stats["void_days"]
        print(f"\n===== {label} =====")
        print(f"总拍卖天数：{days}，流局 {stats['void_days']} 天（{stats['void_days'] / days:.1%}），"
              f"加赛总局数 {stats['playoffs']}")
        print("按天统计：")
        for day_index in sorted(stats["pools_by_day"]):
            bids = stats["bids_by_day"].get(day_index, [])
            pools = stats["pools_by_day"][day_index]
            avg_bid = f"{sum(bids) / len(bids):.2f}" if bids else "—（全流局）"
            print(f"  第 {day_index} 天：平均出价 {avg_bid}，平均拍卖奖池 {sum(pools) / len(pools):.2f} 分")
        win_dist = dict(sorted(stats["winning_bids"].items()))
        cap_wins = stats["winning_bids"][game.max_bid]
        print(f"获胜价分布：{win_dist}")
        print(f"获胜价={game.max_bid}（出价上限）占比：{cap_wins}/{decided}（{cap_wins / max(decided, 1):.1%}）")

    print(f"两组各 {count} 场，共 {2 * count} 场；第一组仅作过程统计参考。")
    print("均价不含流局日，含超级数值；奖池均值含流局日。")
    baseline = [play_tournament(seed=1000 + i, with_affordable_max=False) for i in range(count)]
    print_stats(f"纯随机对照：{count} 场随机 Bot 对战", aggregate(baseline))

    strategy_results = [play_tournament(seed=2000 + i, with_affordable_max=True) for i in range(count)]
    print_stats(f"最高可付价对照：{count} 场（上限 {game.max_bid}，无超级，5 个随机对手）",
                aggregate(strategy_results))
    strategy_scores = []
    opponent_scores = []
    strategy_ranks = []
    for result in strategy_results:
        for entry in result.final_standings:
            if entry.player == "最高可付价玩家":
                strategy_scores.append(entry.total_score)
                strategy_ranks.append(entry.rank)
            else:
                opponent_scores.append(entry.total_score)
    avg_strategy_score = sum(strategy_scores) / len(strategy_scores)
    avg_opponent_score = sum(opponent_scores) / len(opponent_scores)
    avg_strategy_rank = sum(strategy_ranks) / len(strategy_ranks)
    print(f"以下比较均取第二组：最高可付价玩家平均总分 {avg_strategy_score:.2f} 分（平均排名 {avg_strategy_rank:.2f}），"
          f"随机对手平均总分 {avg_opponent_score:.2f} 分")
    return 0
