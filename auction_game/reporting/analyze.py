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
from auction_game.game.settings import GameSettings
from auction_game.paths import LOG_DIR
from auction_game.reporting.run_paths import OFFLINE_RUN_TYPES, RunPaths, RUN_TYPES

RUN_DIR_PATTERN = re.compile(r"^\d{8}_\d{6}_ai(?:-test)?(_\d+)?$")  # 时间戳场次目录，排除 archive 等归档目录
TASK_LABELS = {"speech": "发言", "plan": "规划", "bid": "出价", "memory": "记忆", "summary": "观众总结"}


def find_latest_ai_run() -> Path:
    candidates = [path for path in (LOG_DIR.iterdir() if LOG_DIR.exists() else ())
                  if RUN_DIR_PATTERN.match(path.name) and any((path / "data" / "matches").glob("*.json"))]
    if not candidates:
        sys.exit("logs/ 下没有已完成至少一局的真实 AI 场次（ai 或 ai-test）；离线模拟赛请显式指定目录")
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
            results = day["players"]
            normal_bid_counts = Counter(result["bid"] for result in results)
            super_results = [result for result in results if result["super_bid"] is not None]
            if super_results:
                super_days += 1
                super_collision_hits += sum(
                    result["super_hit"] and normal_bid_counts[result["super_bid"]] >= 2
                    for result in super_results
                )
                # 固定当天正常出价，移除全部超级出价后重新判定，只比较当天结果。
                without_super = resolve_bids(((result["player"], result["bid"]) for result in results), ())[0]
                if without_super != day["winner"]:
                    super_changed_days += 1
            for result in results:
                name, bid = result["player"], result["bid"]
                if bid == 0:
                    zero_bid_counts[name] += 1
                if result["outcome"] == "collided":
                    collision_burn[name] += bid
                    # 原本互撞的价位即使同时被狙，也只归入正常出价互撞。
                    if normal_bid_counts[bid] >= 2:
                        normal_collision_burn[name] += bid
                    else:
                        unique_sniped_burn[name] += bid
                if day["winner"] is not None:
                    normal_bids[name].append(bid)
                if result["super_bid"] is not None:
                    super_uses_count[name] += 1
                    if result["super_hit"]:
                        super_hits[name] += 1

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


def collect_usage(run_dir: Path, players: list[dict]) -> dict:
    """逐条累加任务日志中模型回复的用量，含纠错往返和失败、取消任务里已收到的回复。

    失败的请求在日志里只有报错，没有用量，只计次数。
    """
    paths = RunPaths(run_dir)
    by_player: dict[str, Counter] = {}
    by_task: dict[str, Counter] = {}
    largest_inputs: dict[str, tuple[int, str, str]] = {}
    for pc in players:
        name = pc["name"]
        player_totals = by_player[name] = Counter()
        for line in paths.player(pc["number"]).read_text(encoding="utf-8").splitlines():
            entry = json.loads(line)
            if entry["task"] == "system":
                continue
            task_totals = by_task.setdefault(entry["task"], Counter())
            for totals in (player_totals, task_totals):
                totals["failed_requests"] += len(entry["request_errors"])
            for message in entry["messages"]:
                if message["kind"] != "response":
                    continue
                usage = message["usage"]
                for totals in (player_totals, task_totals):
                    totals["requests"] += 1
                    totals["input"] += usage["input_tokens"]
                    totals["cache_read"] += usage["cache_read_tokens"]
                    totals["output"] += usage["output_tokens"]
                if usage["input_tokens"] > largest_inputs.get(entry["task"], (-1,))[0]:
                    largest_inputs[entry["task"]] = (usage["input_tokens"], name, entry["context"])
    return {"players": by_player,
            "tasks": {task: by_task[task] for task in TASK_LABELS if task in by_task},
            "largest_inputs": largest_inputs}


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
    game, player_names = _match_setup(matches)
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
            # 正常出价和超级出价的数值都算。
            values = [value for result in day["players"] for value in (result["bid"], result["super_bid"])
                      if value is not None]
            zero_bids += values.count(0)
            if day["winner"] is None:
                void_days += 1
                continue
            bids_by_day_index.setdefault(day["day"], []).extend(values)
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
          f"（正常出价互撞 {normal_collision_burn}，唯一正常出价被狙 {unique_sniped_burn}；只计正常出价）")
    print(f"  每局平均：{collision_burn / len(matches):.1f} 金币")
    hit_rate = f"（命中率 {super_hits / super_uses:.0%}）" if super_uses else ""
    print(f"超级出价：使用 {super_uses} 次，命中 {super_hits} 次{hit_rate}")
    print(f"  成本消耗：{super_cost_spent} 金币")
    print(f"  悬赏发放：{super_bounty_minted} 金币")
    print(f"  超级出价净金币变化：{super_bounty_minted - super_cost_spent:+d} 金币")
    print(f"  命中原本互撞价 {player_stats['super_collision_hits']} 次（按超级出价笔数计，包含流局）")
    print(f"  使用超级出价：{player_stats['super_days']} 天")
    print(f"  改变当日结果：{player_stats['super_changed_days']} 天")
    print("    保持正常出价不变，对比取消全部超级出价后的结果")
    print()

    print("平均出价与奖池：")
    print("  出价排除流局日，含正常出价及超级出价的价位；奖池包含流局日")
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

    # 离线赛的用量是假模型估算的。
    if snapshot["run_type"] in ("ai", "ai-test"):
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
