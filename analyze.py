"""统计一场对战的日志数据。默认取 logs/ 下最新的 *_ai 目录，也可传入目录路径。

用法：
    python3 analyze.py                 # 分析最新的 AI 场次
    python3 analyze.py logs/<目录名>   # 分析指定场次（Bot 场次目录亦可）
"""

from __future__ import annotations

import json
import re
import sys
from collections import Counter
from pathlib import Path

from game_rules import GameSettings

ROOT_DIR = Path(__file__).parent
LOG_DIR = ROOT_DIR / "logs"

RUN_DIR_PATTERN = re.compile(r"^\d{8}_\d{6}_ai(_\d+)?$")  # 时间戳场次目录，排除 archive 等归档目录


def _require_fields(record: dict, fields: tuple[str, ...], context: str) -> None:
    if not isinstance(record, dict):
        raise ValueError(f"日志格式不受支持：{context} 必须是对象")
    missing = [field for field in fields if field not in record]
    if missing:
        raise ValueError(f"日志数据缺失：{context} 缺少 {', '.join(missing)}")


def validate_match(match: dict) -> tuple[GameSettings, list[str]]:
    """只接受含完整参数快照及当前结算明细的日志。"""
    _require_fields(match, ("game", "match_index", "is_playoff", "wins", "days"), "match")
    try:
        game = GameSettings.from_dict(match["game"])
    except ValueError as exc:
        raise ValueError(f"日志 game 参数无效：{exc}") from exc
    if not isinstance(match["wins"], dict):
        raise ValueError("日志格式不受支持：match.wins 必须是对象")
    if not isinstance(match["days"], list) or not match["days"]:
        raise ValueError("日志数据缺失：match.days 必须包含至少一天")
    player_names = None
    for day in match["days"]:
        _require_fields(day, ("day", "bids", "pool", "winner", "winning_bid", "wins_taken",
                              "is_void", "collision_values", "super_uses"), "day")
        if not all(isinstance(day[field], list) for field in ("bids", "collision_values", "super_uses")):
            raise ValueError("日志格式不受支持：bids、collision_values、super_uses 必须是列表")
        for bid in day["bids"]:
            _require_fields(bid, ("player", "raw_bid", "bid", "status", "is_super"), "bid")
        names = [bid["player"] for bid in day["bids"] if not bid["is_super"]]
        if (len(names) < 2 or any(not isinstance(name, str) or not name.strip() for name in names)
                or len(names) != len(set(names))):
            raise ValueError("日志参赛名单无效：每天必须有至少两名不重名玩家的正常出价")
        if player_names is None:
            player_names = names
        if set(names) != set(player_names) or set(match["wins"]) != set(player_names):
            raise ValueError("日志参赛名单不一致：每日正常出价与 wins 必须覆盖同一名单")
        for use in day["super_uses"]:
            _require_fields(use, ("player", "value", "hit"), "super_uses 条目")
        if any(record["player"] not in player_names for record in day["bids"] + day["super_uses"]):
            raise ValueError("日志参赛名单不一致：出价或超级出价含未知玩家")
    return game, player_names


def validate_matches(matches: list[dict]) -> tuple[GameSettings, list[str]]:
    if not matches:
        raise ValueError("日志数据缺失：没有比赛")
    first_game = None
    first_names = None
    for payload in matches:
        _require_fields(payload, ("match",), "场次记录")
        game, names = validate_match(payload["match"])
        if first_game is None:
            first_game, first_names = game, names
        elif game != first_game or set(names) != set(first_names):
            raise ValueError("场次日志不一致：各局 game 参数与参赛名单必须一致，请检查是否混入其他比赛")
    return first_game, first_names


def find_latest_ai_run() -> Path:
    candidates = sorted(
        (path for path in LOG_DIR.iterdir() if path.is_dir() and RUN_DIR_PATTERN.match(path.name)),
        key=lambda path: path.name,
    )
    if not candidates:
        sys.exit("logs/ 下没有找到任何时间戳格式的 *_ai 场次目录")
    return candidates[-1]


def load_match_logs(run_dir: Path) -> list[dict]:
    match_files = sorted(run_dir.glob("match_*.json"), key=lambda p: int(p.stem.split("_")[1]))
    if not match_files:
        sys.exit(f"{run_dir} 下没有 match_*.json")
    matches = []
    for path in match_files:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            _require_fields(payload, ("match",), "场次记录")
            validate_match(payload["match"])
            matches.append(payload)
        except ValueError as exc:
            raise ValueError(f"{path.name}：日志格式不受支持或数据缺失：{exc}") from exc
    validate_matches(matches)
    return matches


def recompute_remaining_coins(match: dict) -> dict[str, int]:
    """按本局 game 快照重放金币：工资、正常价扣款、超级成本及命中悬赏。"""
    game, player_names = validate_match(match)
    days = match["days"]
    coins = dict.fromkeys(player_names, game.initial_coins)
    for day in days:
        if day["day"] >= 2:
            for name in coins:
                coins[name] += game.daily_salary
        burned_values = set(day["collision_values"])
        for bid in day["bids"]:
            if bid["is_super"]:
                continue
            won = bid["player"] == day["winner"] and bid["bid"] == day["winning_bid"]
            if won or bid["bid"] in burned_values:
                coins[bid["player"]] -= bid["bid"]
        for use in day["super_uses"]:
            coins[use["player"]] -= game.super_cost
            if use["hit"]:
                coins[use["player"]] += game.super_bounty
    return coins


def collect_player_stats(matches: list[dict]) -> dict:
    """按玩家聚合整场统计：每局胜场矩阵 + 玩家明细行（均价只计正常出价）。

    返回 {"match_labels", "win_matrix", "rows"}；rows 按总胜场降序（同胜场按名字）。
    """
    _, player_names = validate_matches(matches)
    match_labels: list[str] = []
    win_matrix: dict[str, list[int]] = {name: [] for name in player_names}
    match_champions = {name: 0.0 for name in player_names}  # 第一名并列按 1/n 计
    super_uses_count = {name: 0 for name in player_names}
    super_hits = {name: 0 for name in player_names}
    collision_burn = {name: 0 for name in player_names}
    normal_bids: dict[str, list[int]] = {name: [] for name in player_names}
    end_coins: dict[str, list[int]] = {name: [] for name in player_names}
    illegal_counts = {name: 0 for name in player_names}
    zero_bid_counts = {name: 0 for name in player_names}

    for payload in matches:
        match = payload["match"]
        days = match["days"]
        game = GameSettings.from_dict(match["game"])
        if match["is_playoff"]:
            match_labels.append(f"加{match['match_index'] - game.regular_matches}")
        else:
            match_labels.append(str(match["match_index"]))
        wins = match["wins"]
        for name in player_names:
            win_matrix[name].append(wins[name])
        if wins:
            top_wins = max(wins.values())
            champions = [name for name, count in wins.items() if count == top_wins]
            for name in champions:
                match_champions[name] += 1 / len(champions)
        for day in days:
            burned_values = set(day["collision_values"])
            for bid in day["bids"]:
                name = bid["player"]
                if bid["status"] == "illegal":
                    illegal_counts[name] += 1
                if bid["is_super"]:
                    continue
                if bid["bid"] == 0:
                    zero_bid_counts[name] += 1
                if bid["bid"] in burned_values:
                    collision_burn[name] += bid["bid"]
                if not day["is_void"]:
                    normal_bids[name].append(bid["bid"])
            for use in day["super_uses"]:
                super_uses_count[use["player"]] += 1
                if use["hit"]:
                    super_hits[use["player"]] += 1
        remaining = recompute_remaining_coins(match)
        for name in player_names:
            end_coins[name].append(remaining[name])

    rows = []
    for name in player_names:
        bids = normal_bids[name]
        rows.append({
            "name": name,
            "total_wins": sum(win_matrix[name]),
            "match_champions": match_champions[name],
            "super_uses": super_uses_count[name],
            "super_hits": super_hits[name],
            "collision_burn": collision_burn[name],
            "avg_bid": sum(bids) / len(bids) if bids else None,
            "end_coins": end_coins[name],
            "illegal": illegal_counts[name],
            "zero_bids": zero_bid_counts[name],
        })
    rows.sort(key=lambda row: (-row["total_wins"], row["name"]))
    return {"match_labels": match_labels, "win_matrix": win_matrix, "rows": rows}


def print_player_stats(stats: dict) -> None:
    """打印玩家维度统计段：每局胜场矩阵 + 玩家明细行。"""
    match_labels = stats["match_labels"]
    win_matrix = stats["win_matrix"]
    print(f"玩家维度统计（按总胜场降序；矩阵列 = 第 {'/'.join(match_labels)} 局）：")
    for row in stats["rows"]:
        cells = " ".join(str(count) for count in win_matrix[row["name"]])
        print(f"  {row['name']}：{cells}")
    print("  明细（胜场 / 局冠军 / 超级出价 / 撞车放血 / 平均正常出价 / 局末余额 / 违规 / 出0）：")
    for row in stats["rows"]:
        if row["super_uses"]:
            super_text = f"{row['super_uses']}用{row['super_hits']}中（{row['super_hits'] / row['super_uses']:.0%}）"
        else:
            super_text = "0用"
        avg_bid_text = f"{row['avg_bid']:.2f}" if row["avg_bid"] is not None else "—"
        end_coins_text = "/".join(str(coins) for coins in row["end_coins"])
        print(
            f"  {row['name']}：胜{row['total_wins']} 局冠{row['match_champions']:g} "
            f"超{super_text} 放血{row['collision_burn']} 均出{avg_bid_text} "
            f"局末余额{end_coins_text} 违规{row['illegal']}笔 出0共{row['zero_bids']}笔"
        )


def analyze(run_dir: Path) -> None:
    matches = load_match_logs(run_dir)
    game, player_names = validate_matches(matches)

    total_days = 0
    void_days = 0
    # 按「第几天」聚合非流局日的全部有效出价；奖池包括流局日
    bids_by_day_index: dict[int, list[int]] = {}
    pools_by_day_index: dict[int, list[int]] = {}
    winning_bids: list[int] = []
    remaining_coins_all: list[int] = []
    multi_win_matches: list[tuple[int, str, int]] = []  # (局号, 玩家, 胜场)
    illegal_bids = 0
    zero_bids = 0
    zero_bid_wins = 0
    collision_burn = 0   # 因撞车被没收的金币总数（只算正常出价；超级出价成本另计）
    super_uses = 0          # 超级出价使用总次数
    super_hits = 0          # 超级出价命中次数
    super_bounty_minted = 0  # 命中悬赏印发的金币总数
    super_cost_spent = 0    # 超级出价固定成本消耗的金币总数
    cap_wins = 0            # 获胜价=出价上限（极点）的天数
    cap_bid = game.max_bid

    for payload in matches:
        match = payload["match"]
        game = GameSettings.from_dict(match["game"])
        wins = match["wins"]
        for name, win_count in wins.items():
            if win_count >= 3:  # 固定统计口径，不随本局计划总胜场改变
                multi_win_matches.append((match["match_index"], name, win_count))

        for day in match["days"]:
            total_days += 1
            pools_by_day_index.setdefault(day["day"], []).append(day["pool"])
            burned_values = set(day["collision_values"])
            for bid in day["bids"]:
                if bid["status"] == "illegal":
                    illegal_bids += 1
                if bid["bid"] == 0:
                    zero_bids += 1
                if not bid["is_super"] and bid["bid"] in burned_values:
                    collision_burn += bid["bid"]
            for use in day["super_uses"]:
                super_uses += 1
                super_cost_spent += game.super_cost
                if use["hit"]:
                    super_hits += 1
                    super_bounty_minted += game.super_bounty
            if day["is_void"]:
                void_days += 1
                continue
            bids_by_day_index.setdefault(day["day"], []).extend(b["bid"] for b in day["bids"])
            winning_bids.append(day["winning_bid"])
            if day["winning_bid"] == game.max_bid:
                cap_wins += 1
            if day["winning_bid"] == 0:
                zero_bid_wins += 1
        remaining_coins_all.extend(recompute_remaining_coins(match).values())

    print(f"场次目录：{run_dir.name}")
    print(f"玩家：{'、'.join(player_names)}")
    playoff_count = sum(1 for m in matches if m["match"]["is_playoff"])
    print(f"局数：{len(matches)}（正赛 {len(matches) - playoff_count} + 加赛 {playoff_count}）")
    print()

    print(f"总拍卖天数：{total_days}，其中流局 {void_days} 天（{void_days / total_days:.1%}）")
    print()

    print("每一天的平均出价 / 平均奖池（出价刨除流局日，全员有效出价）：")
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
        print(f"  获胜价分布：{win_dist}；获胜价={cap_bid}（极点）占比 {cap_wins / len(winning_bids):.1%}")
    print()

    print(f"局末平均剩余金币：{sum(remaining_coins_all) / len(remaining_coins_all):.2f}"
          f"（最低 {min(remaining_coins_all)}，最高 {max(remaining_coins_all)}）")
    print(f"撞车放血：全场因撞车被没收 {collision_burn} 金币"
          f"（每局平均 {collision_burn / len(matches):.1f}，只计正常出价）")
    hit_rate = f"{super_hits / super_uses:.0%}" if super_uses else "—"
    print(f"超级出价：使用 {super_uses} 次，命中 {super_hits} 次（命中率 {hit_rate}），"
          f"成本消耗 {super_cost_spent} 金币，悬赏印发 {super_bounty_minted} 金币")
    print()

    print_player_stats(collect_player_stats(matches))
    print()

    print(f"单局 ≥3 胜场的玩家-局次数：{len(multi_win_matches)}"
          f"（占 {len(matches)} 局的 {len({m for m, _, _ in multi_win_matches})} 局）")
    for match_index, name, win_count in multi_win_matches:
        print(f"  第 {match_index} 局：{name} 共 {win_count} 胜场")
    print()

    print(f"其他：非法出价 {illegal_bids} 笔；出 0 共 {zero_bids} 笔，"
          f"其中 0 价获胜 {zero_bid_wins} 次")


def main() -> None:
    run_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else find_latest_ai_run()
    try:
        analyze(run_dir)
    except ValueError as exc:
        sys.exit(str(exc))


if __name__ == "__main__":
    main()
