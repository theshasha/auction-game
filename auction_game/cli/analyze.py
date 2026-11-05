"""打印场次统计。默认选最新的真实 AI 场次（ai、ai-test、team 或 team-test），也可显式指定其他类型。

用法：
    python -m auction_game.cli.analyze
    python -m auction_game.cli.analyze logs/<目录名>
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path
import sys

from auction_game.game.engine import team_standings
from auction_game.game.models import StandingsEntry
from auction_game.reporting.analyze import (TASK_LABELS, collect_player_stats, collect_run_stats, collect_team_stats,
                                            collect_usage, find_latest_ai_run, load_match_logs, match_setup)
from auction_game.reporting.run_paths import OFFLINE_RUN_TYPES, RunPaths, RUN_TYPES


def print_player_stats(stats: dict) -> None:
    """每玩家一个小块，分数与余额共用标注了正赛/加赛的列序。"""
    match_labels = stats["match_labels"]
    print("玩家明细（按总分排序）")
    print(f"各局顺序：{' / '.join(match_labels)}")
    for row in stats["rows"]:
        print()
        print(row["name"])
        print(f"  总分：{row['total_score']}分（拍卖{row['auction_score']}分，命中{row['super_score']}分）")
        cells = " / ".join(str(score) for score in stats["score_matrix"][row["name"]])
        auction_cells = " / ".join(str(score) for score in stats["auction_score_matrix"][row["name"]])
        print(f"  各局总分：{cells}")
        print(f"  各局拍卖分：{auction_cells}")
        print(f"  单局最高总分：独占{row['solo_top_matches']}局，并列{row['tied_top_matches']}局")
        super_text = f"使用{row['super_uses']}次，命中{row['super_hits']}次"
        if row["super_uses"]:
            super_text += f"（{row['super_hits'] / row['super_uses']:.0%}）"
        print(f"  超级出价：{super_text}")
        print(f"  超级出价净金币变化：{row['super_net_coins']:+d}金币")
        print(f"  撞车损失：{row['collision_burn']}金币"
              f"（正常出价互撞{row['normal_collision_burn']}，唯一正常出价被狙{row['unique_sniped_burn']}）")
        avg_bid_text = f"{row['avg_bid']:.2f}" if row["avg_bid"] is not None else "—"
        print(f"  平均正常出价（不含流局日）：{avg_bid_text}")
        end_coins_text = " / ".join(str(coins) for coins in row["end_coins"])
        print(f"  各局结束余额：{end_coins_text} 金币")
        print(f"  正常出价为零：{row['zero_bids']}次")


def print_team_sections(snapshot: dict, matches: list[dict], match_labels: list[str]) -> None:
    """团队赛的阵营总分和阵营配合。阵营总分用最后一局日志里的累计排名汇总，和 README、终端同一个汇总函数。

    快照里玩家的 team 有值才是团队赛，个人赛不输出。
    """
    if not any(pc["team"] for pc in snapshot["players"]):
        return
    teams = team_standings([StandingsEntry(**entry) for entry in matches[-1]["standings_after_match"]])
    print("阵营总分")
    print(f"  各局顺序：{' / '.join(match_labels)}")
    for team in teams:
        print(f"  {team.team}：{team.total_score} 分（拍卖 {team.total_auction_score} 分，"
              f"命中 {team.total_score - team.total_auction_score} 分），"
              f"各局 {' / '.join(str(score) for score in team.scores_per_match)}")
    print()

    team_stats = collect_team_stats(matches, {pc["name"]: pc["team"] for pc in snapshot["players"]})
    print("阵营配合")
    print("  队内撞车按天和价位计次：同一价位有 2 笔及以上队友的正常出价就算一次，另有对方出价也算")
    for team in teams:
        totals = team_stats[team.team]
        hit_rate = f"（{totals['super_hits'] / totals['super_uses']:.0%}）" if totals["super_uses"] else ""
        print(f"  {team.team}：队内撞车 {totals['team_collisions']} 次；超级出价 {totals['super_uses']} 次，"
              f"命中 {totals['super_hits']} 次{hit_rate}，只狙到队友 {totals['teammate_only_misses']} 次")
    print()


def _usage_text(totals: Counter) -> str:
    text = f"请求 {totals['requests']} 次，输入 {totals['input']:,}"
    if totals["input"]:
        text += f"（缓存命中 {totals['cache_read']:,}，{totals['cache_read'] / totals['input']:.0%}）"
    text += f"，输出 {totals['output']:,}"
    if totals["failed_requests"]:
        text += f"；另有 {totals['failed_requests']} 次请求失败"
    return text


def print_usage(usage: dict) -> None:
    print("模型用量（token）")
    print("  输入含缓存命中，输出含推理；失败的请求没有用量记录，只计次数")
    for name, totals in usage["players"].items():
        print(f"  {name}：{_usage_text(totals)}")
    print("  按任务：")
    for task, totals in usage["tasks"].items():
        line = f"  {TASK_LABELS[task]}：{_usage_text(totals)}"
        if task in usage["largest_inputs"]:
            tokens, name, context = usage["largest_inputs"][task]
            line += f"；最大单次输入 {tokens:,}，来自 {name} {context}"
        print(line)


def analyze(run_dir: Path) -> None:
    snapshot = RunPaths(run_dir).read_config()
    matches = load_match_logs(run_dir)
    game, player_names = match_setup(matches)
    player_stats = collect_player_stats(matches)
    print("比赛概况")
    print(f"场次目录：{run_dir.name}")
    print(f"运行类型：{RUN_TYPES[snapshot['run_type']]}")
    offline = snapshot["run_type"] in OFFLINE_RUN_TYPES
    if offline:
        print("离线假模型结果：实际执行为假模型，无真实模型调用")
    else:
        print("测试运行结果（非身份模型成绩）" if snapshot["mode"] == "test_model" else "常规阵容结果")
    for pc in snapshot["players"]:
        execution_label = "配置模型" if offline else "实际"
        print(f"  {pc['name']}：身份 {pc['identity_model']['model']} → {execution_label} "
              f"{pc['execution_model']['protocol']} / {pc['execution_model']['model']}")

    run_stats = collect_run_stats(matches, player_stats)

    print(f"玩家：{'、'.join(player_names)}")
    playoff_count = run_stats["playoff_count"]
    total_days, void_days = run_stats["total_days"], run_stats["void_days"]
    print(f"局数：{len(matches)}（正赛 {len(matches) - playoff_count} + 加赛 {playoff_count}）")
    print(f"总拍卖天数：{total_days}，其中流局 {void_days} 天（{void_days / total_days:.1%}）")
    print()
    print_team_sections(snapshot, matches, player_stats["match_labels"])

    print("经济与超级出价")
    end_coins = run_stats["end_coins"]
    print(f"局末平均剩余金币：{sum(end_coins) / len(end_coins):.2f}（最低 {min(end_coins)}，最高 {max(end_coins)}）")
    print(f"撞车损失：{run_stats['collision_burn']} 金币（正常出价互撞 {run_stats['normal_collision_burn']}，"
          f"唯一正常出价被狙 {run_stats['unique_sniped_burn']}；只计正常出价）")
    print(f"  每局平均：{run_stats['collision_burn'] / len(matches):.1f} 金币")
    super_uses, super_hits = run_stats["super_uses"], run_stats["super_hits"]
    hit_rate = f"（命中率 {super_hits / super_uses:.0%}）" if super_uses else ""
    print(f"超级出价：使用 {super_uses} 次，命中 {super_hits} 次{hit_rate}")
    print(f"  成本消耗：{run_stats['super_cost_spent']} 金币")
    print(f"  悬赏发放：{run_stats['super_bounty_minted']} 金币")
    print(f"  超级出价净金币变化：{run_stats['super_bounty_minted'] - run_stats['super_cost_spent']:+d} 金币")
    print(f"  命中原本互撞价 {player_stats['super_collision_hits']} 次（按超级出价笔数计，包含流局）")
    print(f"  使用超级出价：{player_stats['super_days']} 天")
    print(f"  改变当日结果：{player_stats['super_changed_days']} 天")
    print("    保持正常出价不变，对比取消全部超级出价后的结果")
    print()

    print("平均出价与奖池：")
    print("  出价排除流局日，含正常出价及超级出价的价位；奖池包含流局日")
    bids_by_day_index, pools_by_day_index = run_stats["bids_by_day_index"], run_stats["pools_by_day_index"]
    for day_index in sorted(pools_by_day_index):
        pools = pools_by_day_index[day_index]
        bids = bids_by_day_index.get(day_index, [])
        avg_bid = f"{sum(bids) / len(bids):.2f}（{len(bids)} 笔）" if bids else "—（全流局）"
        print(f"  第 {day_index} 天：出价 {avg_bid}，奖池 {sum(pools) / len(pools):.2f}")
    all_bids = [bid for bids in bids_by_day_index.values() for bid in bids]
    if all_bids:
        print(f"  全天总体：平均 {sum(all_bids) / len(all_bids):.2f}（{len(all_bids)} 笔）")
    winning_bids = run_stats["winning_bids"]
    if winning_bids:
        win_dist = dict(sorted(Counter(winning_bids).items()))
        print(f"  获胜价平均：{sum(winning_bids) / len(winning_bids):.2f}（{len(winning_bids)} 个获胜日）")
        print(f"  获胜价分布：{win_dist}")
        print(f"  获胜价={game.max_bid}（出价上限）占比 {run_stats['cap_wins'] / len(winning_bids):.1%}")
    print()

    print_player_stats(player_stats)
    print()

    print("特殊情况")
    print(f"出 0 共 {run_stats['zero_bids']} 笔（含流局日和超级出价），其中 0 价获胜 {run_stats['zero_bid_wins']} 次")

    # 离线赛的用量是假模型估算的。
    if not offline:
        print()
        print_usage(collect_usage(run_dir, snapshot["players"]))


def main() -> None:
    run_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else find_latest_ai_run()
    try:
        analyze(run_dir)
    except (OSError, ValueError) as exc:
        sys.exit(str(exc))


if __name__ == "__main__":
    main()
