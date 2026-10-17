"""统计新格式场次。默认选最新有效真实 AI（ai 或 ai-test），也可显式指定其他类型。

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
from auction_game.paths import ROOT_DIR, LOG_DIR
from auction_game.reporting.run_paths import RunPaths, RUN_TYPES

RUN_DIR_PATTERN = re.compile(r"^\d{8}_\d{6}_ai(?:-test)?(_\d+)?$")  # 时间戳场次目录，排除 archive 等归档目录


def _require_fields(record: dict, fields: tuple[str, ...], context: str) -> None:
    if not isinstance(record, dict):
        raise ValueError(f"日志格式不受支持：{context} 必须是对象")
    missing = [field for field in fields if field not in record]
    if missing:
        raise ValueError(f"日志数据缺失：{context} 缺少 {', '.join(missing)}")


def validate_match(match: dict) -> tuple[GameSettings, list[str]]:
    """只接受含完整参数快照及当前结算明细的日志。"""
    _require_fields(match, ("game", "match_index", "is_playoff", "wins", "days"), "match")
    if type(match["match_index"]) is not int or match["match_index"] < 1:
        raise ValueError(f"日志局号无效：match_index 必须是正整数，实际为 {match['match_index']!r}")
    try:
        game = GameSettings.from_dict(match["game"])
    except ValueError as exc:
        raise ValueError(f"日志 game 参数无效：{exc}") from exc
    if not isinstance(match["wins"], dict):
        raise ValueError("日志格式不受支持：match.wins 必须是对象")
    if not isinstance(match["days"], list) or not match["days"]:
        raise ValueError(f"第 {match['match_index']} 局日志数据缺失：match.days 必须包含至少一天")
    player_names = None
    day_indices = []
    for day in match["days"]:
        _require_fields(day, ("day", "bids", "pool", "winner", "winning_bid", "wins_taken",
                              "is_void", "collision_values", "super_uses"), "day")
        day_indices.append(day["day"])
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
    if (any(type(index) is not int for index in day_indices)
            or day_indices != list(range(1, game.max_days + 1))):
        raise ValueError(f"第 {match['match_index']} 局天序不完整：必须依次为 1..{game.max_days}，"
                         f"实际为 {day_indices}")
    return game, player_names


def validate_matches(matches: list[dict]) -> tuple[GameSettings, list[str]]:
    """独立调用时检查每局结构及跨局一致性。"""
    return _validate_matches(matches)


def _validate_matches(matches: list[dict], files: list[Path] | None = None) -> tuple[GameSettings, list[str]]:
    """每局只校验一次；文件来源仅用于核对编号及定位错误。"""
    if not matches:
        raise ValueError("日志数据缺失：没有比赛")
    first_game = None
    first_names = None
    seen_indices = set()
    for position, payload in enumerate(matches):
        context = files[position].name if files is not None else f"第 {position + 1} 条场次记录"
        try:
            _require_fields(payload, ("match",), "场次记录")
            match = payload["match"]
            game, names = validate_match(match)
            index = match["match_index"]
            if index in seen_indices:
                raise ValueError(f"局号 {index} 重复")
            seen_indices.add(index)
            if files is not None and int(files[position].stem) != index:
                raise ValueError(f"文件编号与 match_index {index} 不一致")
            if first_game is None:
                first_game, first_names = game, names
            elif game != first_game or set(names) != set(first_names):
                raise ValueError(f"第 {index} 局场次日志不一致：各局 game 参数与参赛名单必须一致，请检查是否混入其他比赛")
        except ValueError as exc:
            raise ValueError(f"{context}：{exc}") from exc
    return first_game, first_names


def find_latest_ai_run() -> Path:
    candidates = []
    for path in LOG_DIR.iterdir() if LOG_DIR.exists() else ():
        if not path.is_dir() or not RUN_DIR_PATTERN.match(path.name):
            continue
        try:
            if RunPaths(path).read_config()["run_type"] in ("ai", "ai-test"):
                load_match_logs(path)
                candidates.append(path)
        except (OSError, ValueError):
            continue
    if not candidates:
        sys.exit("logs/ 下没有有效的新格式真实 AI 场次（ai 或 ai-test）；Bot 与离线模拟赛请显式指定目录")
    return max(candidates, key=lambda path: (path.name[:15], int(path.name.rsplit('_', 1)[1])
                                           if path.name.rsplit('_', 1)[1].isdigit() else 1))


def load_match_logs(run_dir: Path) -> list[dict]:
    """读取并完整校验场次；允许只有已完成局的未完赛场次。"""
    matches, _, _, _ = _load_match_logs(run_dir)
    return matches


def _load_match_logs(run_dir: Path) -> tuple[list[dict], dict, GameSettings, list[str]]:
    paths = RunPaths(run_dir)
    config = paths.read_config()
    match_files = list((paths.data / "matches").glob("*.json"))
    for path in match_files:
        if not path.stem.isascii() or not path.stem.isdecimal():
            raise ValueError(f"{path.name}：日志文件编号必须是整数")
    match_files.sort(key=lambda p: int(p.stem))
    if not match_files:
        raise ValueError(f"{run_dir} 下没有 data/matches/*.json")
    matches = []
    for path in match_files:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            matches.append(payload)
        except ValueError as exc:
            raise ValueError(f"{path.name}：日志格式不受支持或数据缺失：{exc}") from exc
    game, names = _validate_matches(matches, match_files)
    if config.get("game") != game.to_dict() or {pc["name"] for pc in config["players"]} != set(names):
        raise ValueError("场次执行快照与比赛参数或名单不一致")
    return matches, config, game, names


def recompute_remaining_coins(match: dict) -> dict[str, int]:
    """按本局 game 快照重放金币：工资、正常价扣款、超级成本及命中悬赏。"""
    game, player_names = validate_match(match)
    return _recompute_remaining_coins(match, game, player_names)


def _recompute_remaining_coins(match: dict, game: GameSettings, player_names: list[str]) -> dict[str, int]:
    """仅供已校验的统计路径复用，避免再次扫描日志结构。"""
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
    """按玩家聚合整场统计：每局胜场矩阵 + 玩家明细（均价只计正常出价）。

    返回每局胜场矩阵、玩家明细、超级命中原本互撞价次数及使用/改变当天结果的天数；玩家按总胜场降序。
    """
    game, player_names = validate_matches(matches)
    return _collect_player_stats(matches, game, player_names)


def _collect_player_stats(matches: list[dict], game: GameSettings, player_names: list[str]) -> dict:
    """已校验场次的玩家统计，局末余额在此统一重放。"""
    match_labels: list[str] = []
    win_matrix: dict[str, list[int]] = {name: [] for name in player_names}
    solo_top_matches = dict.fromkeys(player_names, 0)
    tied_top_matches = dict.fromkeys(player_names, 0)
    super_uses_count = {name: 0 for name in player_names}
    super_hits = {name: 0 for name in player_names}
    super_collision_hits = 0
    super_days = 0
    super_changed_days = 0
    collision_burn = {name: 0 for name in player_names}
    normal_bids: dict[str, list[int]] = {name: [] for name in player_names}
    end_coins: dict[str, list[int]] = {name: [] for name in player_names}
    illegal_counts = {name: 0 for name in player_names}
    zero_bid_counts = {name: 0 for name in player_names}

    for payload in matches:
        match = payload["match"]
        days = match["days"]
        if match["is_playoff"]:
            match_labels.append(f"加赛第 {match['match_index'] - game.regular_matches} 局"
                                f"（总第 {match['match_index']} 局）")
        else:
            match_labels.append(f"正赛第 {match['match_index']} 局")
        wins = match["wins"]
        for name in player_names:
            win_matrix[name].append(wins[name])
        if wins:
            top_wins = max(wins.values())
            champions = [name for name, count in wins.items() if count == top_wins]
            # 全员 0 胜也保留为并列最高；每人计一局，不折算份额。
            top_matches = solo_top_matches if len(champions) == 1 else tied_top_matches
            for name in champions:
                top_matches[name] += 1
        for day in days:
            if day["super_uses"]:
                super_days += 1
                # 固定当天有效正常报价，移除全部超级后重新判定；不推演未来。
                effective_normal_bids = [
                    BidRecord(player=bid["player"], raw_bid=bid["raw_bid"], bid=bid["bid"], status=bid["status"])
                    for bid in day["bids"] if not bid["is_super"]
                ]
                normal_bid_counts = Counter(bid.bid for bid in effective_normal_bids)
                super_collision_hits += sum(
                    use["hit"] and normal_bid_counts[use["value"]] >= 2
                    for use in day["super_uses"]
                )
                without_super, _, _ = resolve_bids(effective_normal_bids)
                without_super_winner = without_super.player if without_super is not None else None
                if (without_super_winner, without_super is None) != (day["winner"], day["is_void"]):
                    super_changed_days += 1
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
        remaining = _recompute_remaining_coins(match, game, player_names)
        for name in player_names:
            end_coins[name].append(remaining[name])

    rows = []
    for name in player_names:
        bids = normal_bids[name]
        rows.append({
            "name": name,
            "total_wins": sum(win_matrix[name]),
            "solo_top_matches": solo_top_matches[name],
            "tied_top_matches": tied_top_matches[name],
            "super_uses": super_uses_count[name],
            "super_hits": super_hits[name],
            "super_net_coins": super_hits[name] * game.super_bounty - super_uses_count[name] * game.super_cost,
            "collision_burn": collision_burn[name],
            "avg_bid": sum(bids) / len(bids) if bids else None,
            "end_coins": end_coins[name],
            "illegal": illegal_counts[name],
            "zero_bids": zero_bid_counts[name],
        })
    rows.sort(key=lambda row: (-row["total_wins"], row["name"]))
    return {"match_labels": match_labels, "win_matrix": win_matrix, "rows": rows,
            "super_collision_hits": super_collision_hits,
            "super_days": super_days, "super_changed_days": super_changed_days}


def print_player_stats(stats: dict) -> None:
    """每玩家一个小块，胜场与余额共用明确标注正赛/加赛的列序。"""
    match_labels = stats["match_labels"]
    win_matrix = stats["win_matrix"]
    print("玩家明细（按总胜场排序）")
    print(f"各局顺序：{' / '.join(match_labels)}")
    for row in stats["rows"]:
        print()
        print(row["name"])
        print(f"  总胜场：{row['total_wins']}")
        cells = " / ".join(str(count) for count in win_matrix[row["name"]])
        print(f"  各局胜场：{cells}")
        print(f"  单局最高胜场：独占{row['solo_top_matches']}局，并列{row['tied_top_matches']}局")
        super_text = f"使用{row['super_uses']}次，命中{row['super_hits']}次"
        if row["super_uses"]:
            super_text += f"（{row['super_hits'] / row['super_uses']:.0%}）"
        print(f"  超级出价：{super_text}")
        print(f"  超级净收益：{row['super_net_coins']:+d}金币")
        print(f"  撞车损失：{row['collision_burn']}金币")
        avg_bid_text = f"{row['avg_bid']:.2f}" if row["avg_bid"] is not None else "—"
        print(f"  平均正常出价：{avg_bid_text}")
        end_coins_text = " / ".join(str(coins) for coins in row["end_coins"])
        print(f"  各局结束余额：{end_coins_text} 金币")
        print(f"  非法出价：{row['illegal']}次")
        print(f"  正常出价为零：{row['zero_bids']}次")


def analyze(run_dir: Path) -> None:
    matches, snapshot, game, player_names = _load_match_logs(run_dir)
    player_stats = _collect_player_stats(matches, game, player_names)
    print("比赛概况")
    print(f"场次目录：{run_dir.name}")
    print(f"运行类型：{RUN_TYPES[snapshot['run_type']]}")
    print("测试运行结果（非身份模型成绩）" if snapshot["mode"] == "test_model" else "常规阵容结果")
    for pc in snapshot["players"]:
        if "execution_model" not in pc:
            continue
        print(f"  {pc['name']}：身份 {pc['identity_model']['model']} → 实际 "
              f"{pc['execution_model']['protocol']} / {pc['execution_model']['model']}")

    total_days = 0
    void_days = 0
    # 按「第几天」聚合非流局日的全部有效出价；奖池包括流局日
    bids_by_day_index: dict[int, list[int]] = {}
    pools_by_day_index: dict[int, list[int]] = {}
    winning_bids: list[int] = []
    remaining_coins_all = [coins for row in player_stats["rows"] for coins in row["end_coins"]]
    multi_win_matches: list[tuple[str, str, int]] = []  # (正赛/加赛标签, 玩家, 胜场)
    illegal_bids = sum(row["illegal"] for row in player_stats["rows"])
    zero_bids = 0
    zero_bid_wins = 0
    collision_burn = sum(row["collision_burn"] for row in player_stats["rows"])
    super_uses = sum(row["super_uses"] for row in player_stats["rows"])
    super_hits = sum(row["super_hits"] for row in player_stats["rows"])
    super_bounty_minted = super_hits * game.super_bounty
    super_cost_spent = super_uses * game.super_cost
    cap_wins = 0            # 获胜价等于出价上限的天数
    cap_bid = game.max_bid

    for payload, match_label in zip(matches, player_stats["match_labels"]):
        match = payload["match"]
        wins = match["wins"]
        for name, win_count in wins.items():
            if win_count >= 3:  # 固定统计口径，不随本局计划总胜场改变
                multi_win_matches.append((match_label, name, win_count))

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
    print(f"撞车损失：{collision_burn} 金币（只计正常出价）")
    print(f"  每局平均：{collision_burn / len(matches):.1f} 金币")
    hit_rate = f"（命中率 {super_hits / super_uses:.0%}）" if super_uses else ""
    print(f"超级出价：使用 {super_uses} 次，命中 {super_hits} 次{hit_rate}")
    print(f"  成本消耗：{super_cost_spent} 金币")
    print(f"  悬赏发放：{super_bounty_minted} 金币")
    print(f"  超级净收益：{super_bounty_minted - super_cost_spent:+d} 金币")
    print(f"  命中原本互撞价 {player_stats['super_collision_hits']} 次（按超级使用笔数计，包含流局）")
    print(f"  使用超级出价：{player_stats['super_days']} 天")
    print(f"  改变当日结果：{player_stats['super_changed_days']} 天")
    print("    保持正常报价不变，对比取消全部超级后的结果")
    print()

    print("平均出价与奖池：")
    print("  出价排除流局日，含有效正常报价及超级数值；奖池包含流局日")
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
    print(f"单局 ≥3 胜场的玩家-局次数：{len(multi_win_matches)}"
          f"（占 {len(matches)} 局的 {len({m for m, _, _ in multi_win_matches})} 局）")
    for match_label, name, win_count in multi_win_matches:
        print(f"  {match_label}：{name} 共 {win_count} 胜场")
    print()

    print(f"非法出价：{illegal_bids} 笔")
    print(f"出 0 共 {zero_bids} 笔，其中 0 价获胜 {zero_bid_wins} 次")


def main() -> None:
    run_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else find_latest_ai_run()
    try:
        analyze(run_dir)
    except ValueError as exc:
        sys.exit(str(exc))


if __name__ == "__main__":
    main()
