"""读取场次日志并计算统计，口径见 docs/analyze.md；打印和命令行入口在 auction_game.cli.analyze。"""

from __future__ import annotations

import json
import re
import sys
from collections import Counter
from pathlib import Path

from auction_game.game.engine import resolve_bids
from auction_game.game.settings import GameSettings
from auction_game.paths import LOG_DIR
from auction_game.reporting.run_paths import RunPaths

# 真实 AI 场次的时间戳目录，排除离线赛和 archive 等归档目录；目录名的类型取自场次的 run_type。
RUN_DIR_PATTERN = re.compile(r"^\d{8}_\d{6}_(?:ai|team)(?:-test)?(_\d+)?$")
TASK_LABELS = {"speech": "发言", "discussion": "讨论", "plan": "规划", "bid": "出价", "memory": "记忆",
               "summary": "观众总结"}


def find_latest_ai_run() -> Path:
    candidates = [path for path in (LOG_DIR.iterdir() if LOG_DIR.exists() else ())
                  if RUN_DIR_PATTERN.match(path.name) and any((path / "data" / "matches").glob("*.json"))]
    if not candidates:
        sys.exit("logs/ 下没有已完成至少一局的真实 AI 场次（ai、ai-test、team 或 team-test）；离线模拟赛请显式指定目录")
    return max(candidates, key=lambda path: (path.name[:15], int(path.name.rsplit('_', 1)[1])
                                           if path.name.rsplit('_', 1)[1].isdigit() else 1))


def load_match_logs(run_dir: Path) -> list[dict]:
    """按文件名顺序读取已保存的各局日志；未完赛场次只有已完成的局。"""
    match_files = sorted((RunPaths(run_dir).data / "matches").glob("*.json"))
    if not match_files:
        raise ValueError(f"{run_dir} 下没有 data/matches/*.json")
    return [json.loads(path.read_text(encoding="utf-8")) for path in match_files]


def match_setup(matches: list[dict]) -> tuple[GameSettings, list[str]]:
    """各局共用的比赛参数和参赛名单，取自第一局日志。"""
    first = matches[0]["match"]
    return GameSettings.from_dict(first["game"]), list(first["scores"])


def collect_player_stats(matches: list[dict]) -> dict:
    """聚合各局总分及拍卖分矩阵，玩家按总分降序；均价只计正常出价。"""
    game, player_names = match_setup(matches)
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


def collect_run_stats(matches: list[dict], player_stats: dict) -> dict:
    """全场指标：天数和流局、按天序号的出价和奖池、获胜价和出 0，以及各玩家汇总之和。

    player_stats 是 collect_player_stats 的结果。
    """
    game, _ = match_setup(matches)
    rows = player_stats["rows"]
    total_days = 0
    void_days = 0
    # 按「第几天」聚合非流局日的全部出价；奖池包括流局日
    bids_by_day_index: dict[int, list[int]] = {}
    pools_by_day_index: dict[int, list[int]] = {}
    winning_bids: list[int] = []
    zero_bids = 0
    zero_bid_wins = 0
    cap_wins = 0            # 获胜价等于出价上限的天数

    for payload in matches:
        for day in payload["match"]["days"]:
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

    super_uses = sum(row["super_uses"] for row in rows)
    super_hits = sum(row["super_hits"] for row in rows)
    return {"total_days": total_days, "void_days": void_days,
            "playoff_count": sum(1 for payload in matches if payload["match"]["is_playoff"]),
            "end_coins": [coins for row in rows for coins in row["end_coins"]],
            "collision_burn": sum(row["collision_burn"] for row in rows),
            "normal_collision_burn": sum(row["normal_collision_burn"] for row in rows),
            "unique_sniped_burn": sum(row["unique_sniped_burn"] for row in rows),
            "super_uses": super_uses, "super_hits": super_hits,
            "super_cost_spent": super_uses * game.super_cost,
            "super_bounty_minted": super_hits * game.super_bounty,
            "bids_by_day_index": bids_by_day_index, "pools_by_day_index": pools_by_day_index,
            "winning_bids": winning_bids, "cap_wins": cap_wins,
            "zero_bids": zero_bids, "zero_bid_wins": zero_bid_wins}


def collect_team_stats(matches: list[dict], team_of: dict[str, str]) -> dict[str, Counter]:
    """各阵营的队内撞车次数、超级出价的使用和命中次数，以及只狙到队友的落空次数。"""
    stats = {team: Counter() for team in team_of.values()}
    for payload in matches:
        for day in payload["match"]["days"]:
            results = day["players"]
            # 一天里一个价位上有 2 笔及以上队友的正常出价算一次，另有对方出价也算。
            team_bid_counts = Counter((team_of[result["player"]], result["bid"]) for result in results)
            for (team, _), count in team_bid_counts.items():
                if count >= 2:
                    stats[team]["team_collisions"] += 1
            normal_bid_counts = Counter(result["bid"] for result in results)
            for result in results:
                if result["super_bid"] is None:
                    continue
                totals = stats[team_of[result["player"]]]
                totals["super_uses"] += 1
                if result["super_hit"]:
                    totals["super_hits"] += 1
                # 超级出价不能和自己的正常出价相同，落空的价位上还有正常出价，就只能是队友的。
                elif normal_bid_counts[result["super_bid"]]:
                    totals["teammate_only_misses"] += 1
    return stats


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
