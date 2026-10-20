"""统计场次。默认选最新的真实 AI 场次（ai 或 ai-test），也可显式指定其他类型。

用法：
    python -m auction_game.reporting.analyze
    python -m auction_game.reporting.analyze logs/<目录名>
"""

from __future__ import annotations

import json
import re
import sys
from collections import Counter
from pathlib import Path

from auction_game.game.engine import resolve_bids
from auction_game.game.game_rules import GameSettings
from auction_game.game.models import BidRecord
from auction_game.paths import LOG_DIR
from auction_game.reporting.run_paths import RunPaths, RUN_TYPES

RUN_DIR_PATTERN = re.compile(r"^\d{8}_\d{6}_ai(?:-test)?(_\d+)?$")  # 时间戳场次目录，排除 archive 等归档目录


def find_latest_ai_run() -> Path:
    candidates = [path for path in (LOG_DIR.iterdir() if LOG_DIR.exists() else ())
                  if RUN_DIR_PATTERN.match(path.name) and any((path / "data" / "matches").glob("*.json"))]
    if not candidates:
        sys.exit("logs/ 下没有已完成至少一局的真实 AI 场次（ai 或 ai-test）；Bot 与离线模拟赛请显式指定目录")
    return max(candidates, key=lambda path: (path.name[:15], int(path.name.rsplit('_', 1)[1])
                                           if path.name.rsplit('_', 1)[1].isdigit() else 1))


def load_match_logs(run_dir: Path) -> list[dict]:
    """按文件名顺序读取已保存的各局日志；未完赛场次只有已完成的局。"""
    match_files = sorted((RunPaths(run_dir).data / "matches").glob("*.json"))
    if not match_files:
        raise ValueError(f"{run_dir} 下没有 data/matches/*.json")
    return [json.loads(path.read_text(encoding="utf-8")) for path in match_files]


def _match_setup(matches: list[dict]) -> tuple[GameSettings, list[str]]:
    """各局共用的比赛参数和参赛名单，取自第一局日志。"""
    first = matches[0]["match"]
    return GameSettings.from_dict(first["game"]), list(first["scores"])


def collect_player_stats(matches: list[dict]) -> dict:
    """聚合各局总分及拍卖分矩阵，玩家按总分降序；均价只计正常出价。"""
    game, player_names = _match_setup(matches)
    match_labels: list[str] = []
    score_matrix: dict[str, list[int]] = {name: [] for name in player_names}
    auction_score_matrix: dict[str, list[int]] = {name: [] for name in player_names}
    solo_top_matches = dict.fromkeys(player_names, 0)
    tied_top_matches = dict.fromkeys(player_names, 0)
    super_uses_count = {name: 0 for name in player_names}
    super_hits = {name: 0 for name in player_names}
    super_collision_hits = 0
    super_days = 0
    super_changed_days = 0
    collision_burn = {name: 0 for name in player_names}
    normal_collision_burn = {name: 0 for name in player_names}
    unique_sniped_burn = {name: 0 for name in player_names}
    normal_bids: dict[str, list[int]] = {name: [] for name in player_names}
    end_coins: dict[str, list[int]] = {name: [] for name in player_names}
    zero_bid_counts = {name: 0 for name in player_names}

    for payload in matches:
        match = payload["match"]
        days = match["days"]
        if match["is_playoff"]:
            match_labels.append(f"加赛第 {match['match_index'] - game.regular_matches} 局"
                                f"（总第 {match['match_index']} 局）")
        else:
            match_labels.append(f"正赛第 {match['match_index']} 局")
        scores = match["scores"]
        for name in player_names:
            score_matrix[name].append(scores[name])
            auction_score_matrix[name].append(match["auction_scores"][name])
            end_coins[name].append(match["remaining_coins"][name])
        top_score = max(scores.values())
        champions = [name for name, score in scores.items() if score == top_score]
        # 全员 0 分也算并列最高；每人计一局，不折算份额。
        top_matches = solo_top_matches if len(champions) == 1 else tied_top_matches
        for name in champions:
            top_matches[name] += 1
        for day in days:
            normal_bid_counts = Counter(bid["bid"] for bid in day["bids"] if not bid["is_super"])
            if day["super_uses"]:
                super_days += 1
                # 固定当天正常报价，移除全部超级后重新判定，只比较当天结果。
                normal_only = [BidRecord(player=bid["player"], bid=bid["bid"])
                               for bid in day["bids"] if not bid["is_super"]]
                super_collision_hits += sum(
                    use["hit"] and normal_bid_counts[use["value"]] >= 2
                    for use in day["super_uses"]
                )
                without_super, _, _ = resolve_bids(normal_only)
                without_super_winner = without_super.player if without_super is not None else None
                if (without_super_winner, without_super is None) != (day["winner"], day["is_void"]):
                    super_changed_days += 1
            burned_values = set(day["collision_values"])
            for bid in day["bids"]:
                name = bid["player"]
                if bid["is_super"]:
                    continue
                if bid["bid"] == 0:
                    zero_bid_counts[name] += 1
                if bid["bid"] in burned_values:
                    collision_burn[name] += bid["bid"]
                    # 原本互撞的价位即使同时被狙，也只归入正常报价互撞。
                    if normal_bid_counts[bid["bid"]] >= 2:
                        normal_collision_burn[name] += bid["bid"]
                    else:
                        unique_sniped_burn[name] += bid["bid"]
                if not day["is_void"]:
                    normal_bids[name].append(bid["bid"])
            for use in day["super_uses"]:
                super_uses_count[use["player"]] += 1
                if use["hit"]:
                    super_hits[use["player"]] += 1

    rows = []
    for name in player_names:
        bids = normal_bids[name]
        rows.append({
            "name": name,
            "total_score": sum(score_matrix[name]),
            "auction_score": sum(auction_score_matrix[name]),
            "super_score": sum(score_matrix[name]) - sum(auction_score_matrix[name]),
            "solo_top_matches": solo_top_matches[name],
            "tied_top_matches": tied_top_matches[name],
            "super_uses": super_uses_count[name],
            "super_hits": super_hits[name],
            "super_net_coins": super_hits[name] * game.super_bounty - super_uses_count[name] * game.super_cost,
            "collision_burn": collision_burn[name],
            "normal_collision_burn": normal_collision_burn[name],
            "unique_sniped_burn": unique_sniped_burn[name],
            "avg_bid": sum(bids) / len(bids) if bids else None,
            "end_coins": end_coins[name],
            "zero_bids": zero_bid_counts[name],
        })
    rows.sort(key=lambda row: (-row["total_score"], row["name"]))
    return {"match_labels": match_labels, "score_matrix": score_matrix,
            "auction_score_matrix": auction_score_matrix, "rows": rows,
            "super_collision_hits": super_collision_hits,
            "super_days": super_days, "super_changed_days": super_changed_days}


def print_player_stats(stats: dict) -> None:
    """每玩家一个小块，分数与余额共用标注了正赛/加赛的列序。"""
    match_labels = stats["match_labels"]
    print("玩家明细（按总分排序）")
    print(f"各局顺序：{' / '.join(match_labels)}")
    for row in stats["rows"]:
        print()
        print(row["name"])
        print(f"  总分：{row['total_score']}分（拍卖{row['auction_score']}分，超级命中{row['super_score']}分）")
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
              f"（正常报价互撞{row['normal_collision_burn']}，唯一报价被狙{row['unique_sniped_burn']}）")
        avg_bid_text = f"{row['avg_bid']:.2f}" if row["avg_bid"] is not None else "—"
        print(f"  平均正常出价（不含流局日）：{avg_bid_text}")
        end_coins_text = " / ".join(str(coins) for coins in row["end_coins"])
        print(f"  各局结束余额：{end_coins_text} 金币")
        print(f"  正常出价为零：{row['zero_bids']}次")


def analyze(run_dir: Path) -> None:
    snapshot = RunPaths(run_dir).read_config()
    matches = load_match_logs(run_dir)
    game, player_names = _match_setup(matches)
    player_stats = collect_player_stats(matches)
    print("比赛概况")
    print(f"场次目录：{run_dir.name}")
    print(f"运行类型：{RUN_TYPES[snapshot['run_type']]}")
    if snapshot["run_type"] == "offline":
        print("离线假模型结果：实际执行为假模型，无真实模型调用")
    elif snapshot["run_type"] == "bot":
        print("Bot 策略结果：实际执行为本地程序")
    else:
        print("测试运行结果（非身份模型成绩）" if snapshot["mode"] == "test_model" else "常规阵容结果")
    for pc in snapshot["players"]:
        if "execution_model" not in pc:
            continue
        execution_label = "配置模型" if snapshot["run_type"] == "offline" else "实际"
        print(f"  {pc['name']}：身份 {pc['identity_model']['model']} → {execution_label} "
              f"{pc['execution_model']['protocol']} / {pc['execution_model']['model']}")

    total_days = 0
    void_days = 0
    # 按「第几天」聚合非流局日的全部出价；奖池包括流局日
    bids_by_day_index: dict[int, list[int]] = {}
    pools_by_day_index: dict[int, list[int]] = {}
    winning_bids: list[int] = []
    remaining_coins_all = [coins for row in player_stats["rows"] for coins in row["end_coins"]]
    zero_bids = 0
    zero_bid_wins = 0
    collision_burn = sum(row["collision_burn"] for row in player_stats["rows"])
    normal_collision_burn = sum(row["normal_collision_burn"] for row in player_stats["rows"])
    unique_sniped_burn = sum(row["unique_sniped_burn"] for row in player_stats["rows"])
    super_uses = sum(row["super_uses"] for row in player_stats["rows"])
    super_hits = sum(row["super_hits"] for row in player_stats["rows"])
    super_bounty_minted = super_hits * game.super_bounty
    super_cost_spent = super_uses * game.super_cost
    cap_wins = 0            # 获胜价等于出价上限的天数
    cap_bid = game.max_bid

    for payload in matches:
        match = payload["match"]

        for day in match["days"]:
            total_days += 1
            pools_by_day_index.setdefault(day["day"], []).append(day["pool"])
            for bid in day["bids"]:
                if bid["bid"] == 0:
                    zero_bids += 1
            if day["is_void"]:
                void_days += 1
                continue
            bids_by_day_index.setdefault(day["day"], []).extend(b["bid"] for b in day["bids"])
            winning_bids.append(day["winning_bid"])
            if day["winning_bid"] == game.max_bid:
                cap_wins += 1
            if day["winning_bid"] == 0:
                zero_bid_wins += 1

    print(f"玩家：{'、'.join(player_names)}")
    playoff_count = sum(1 for m in matches if m["match"]["is_playoff"])
    print(f"局数：{len(matches)}（正赛 {len(matches) - playoff_count} + 加赛 {playoff_count}）")
    print(f"总拍卖天数：{total_days}，其中流局 {void_days} 天（{void_days / total_days:.1%}）")
    print()

    print("经济与超级出价")
    print(f"局末平均剩余金币：{sum(remaining_coins_all) / len(remaining_coins_all):.2f}"
          f"（最低 {min(remaining_coins_all)}，最高 {max(remaining_coins_all)}）")
    print(f"撞车损失：{collision_burn} 金币"
          f"（正常报价互撞 {normal_collision_burn}，唯一报价被狙 {unique_sniped_burn}；只计正常出价）")
    print(f"  每局平均：{collision_burn / len(matches):.1f} 金币")
    hit_rate = f"（命中率 {super_hits / super_uses:.0%}）" if super_uses else ""
    print(f"超级出价：使用 {super_uses} 次，命中 {super_hits} 次{hit_rate}")
    print(f"  成本消耗：{super_cost_spent} 金币")
    print(f"  悬赏发放：{super_bounty_minted} 金币")
    print(f"  超级出价净金币变化：{super_bounty_minted - super_cost_spent:+d} 金币")
    print(f"  命中原本互撞价 {player_stats['super_collision_hits']} 次（按超级使用笔数计，包含流局）")
    print(f"  使用超级出价：{player_stats['super_days']} 天")
    print(f"  改变当日结果：{player_stats['super_changed_days']} 天")
    print("    保持正常报价不变，对比取消全部超级后的结果")
    print()

    print("平均出价与奖池：")
    print("  出价排除流局日，含正常报价及超级数值；奖池包含流局日")
    for day_index in sorted(pools_by_day_index):
        pools = pools_by_day_index[day_index]
        bids = bids_by_day_index.get(day_index, [])
        avg_bid = f"{sum(bids) / len(bids):.2f}（{len(bids)} 笔）" if bids else "—（全流局）"
        print(f"  第 {day_index} 天：出价 {avg_bid}，奖池 {sum(pools) / len(pools):.2f}")
    all_bids = [bid for bids in bids_by_day_index.values() for bid in bids]
    if all_bids:
        print(f"  全天总体：平均 {sum(all_bids) / len(all_bids):.2f}（{len(all_bids)} 笔）")
    if winning_bids:
        win_dist = dict(sorted(Counter(winning_bids).items()))
        print(f"  获胜价平均：{sum(winning_bids) / len(winning_bids):.2f}（{len(winning_bids)} 个获胜日）")
        print(f"  获胜价分布：{win_dist}")
        print(f"  获胜价={cap_bid}（出价上限）占比 {cap_wins / len(winning_bids):.1%}")
    print()

    print_player_stats(player_stats)
    print()

    print("特殊情况")
    print(f"出 0 共 {zero_bids} 笔（含流局日和超级出价），其中 0 价获胜 {zero_bid_wins} 次")


def main() -> None:
    run_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else find_latest_ai_run()
    try:
        analyze(run_dir)
    except (OSError, ValueError) as exc:
        sys.exit(str(exc))


if __name__ == "__main__":
    main()
