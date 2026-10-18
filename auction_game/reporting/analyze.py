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
from dataclasses import asdict
from pathlib import Path

from auction_game.game.engine import calculate_day_scores, compute_standings, resolve_bids
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
    """按有效出价核对当前结算明细；原始提交仅供审计，不重验经济预算。"""
    _require_fields(match, ("game", "match_index", "is_playoff", "auction_scores", "scores", "days"), "match")
    if type(match["match_index"]) is not int or match["match_index"] < 1:
        raise ValueError(f"日志局号无效：match_index 必须是正整数，实际为 {match['match_index']!r}")
    try:
        game = GameSettings.from_dict(match["game"])
    except ValueError as exc:
        raise ValueError(f"日志 game 参数无效：{exc}") from exc
    if type(match["is_playoff"]) is not bool:
        raise ValueError("日志 is_playoff 必须是布尔值")
    for field in ("auction_scores", "scores"):
        if (not isinstance(match[field], dict)
                or any(type(score) is not int or score < 0 for score in match[field].values())):
            raise ValueError(f"日志 match.{field} 必须是全员非负整数对象（拒绝 bool）")
    if not isinstance(match["days"], list) or not match["days"]:
        raise ValueError(f"第 {match['match_index']} 局日志数据缺失：match.days 必须包含至少一天")
    player_names = None
    day_indices = []
    for day in match["days"]:
        _require_fields(day, ("day", "bids", "pool", "winner", "winning_bid", "auction_score",
                              "is_void", "collision_values", "super_uses"), "day")
        day_indices.append(day["day"])
        if not all(isinstance(day[field], list) for field in ("bids", "collision_values", "super_uses")):
            raise ValueError("日志格式不受支持：bids、collision_values、super_uses 必须是列表")
        for bid in day["bids"]:
            _require_fields(bid, ("player", "raw_bid", "bid", "status", "is_super"), "bid")
            if not isinstance(bid["player"], str) or not bid["player"].strip():
                raise ValueError("日志 bid.player 必须是非空玩家名")
            if type(bid["is_super"]) is not bool:
                raise ValueError("日志 bid.is_super 必须是布尔值")
            if type(bid["bid"]) is not int or not 0 <= bid["bid"] <= game.max_bid:
                raise ValueError("日志 bid.bid 必须是 0..max_bid 的有效整数（拒绝 bool）")
            if (bid["status"] not in ("ok", "illegal")
                    or (bid["status"] == "illegal" and (bid["is_super"] or bid["bid"] != 0))):
                raise ValueError("日志 bid.status 无效：非法提交只保留正常出价 0")
        names = [bid["player"] for bid in day["bids"] if not bid["is_super"]]
        if (len(names) < 2 or any(not isinstance(name, str) or not name.strip() for name in names)
                or len(names) != len(set(names))):
            raise ValueError("日志参赛名单无效：每天必须有至少两名不重名玩家的正常出价")
        if player_names is None:
            player_names = names
        if (set(names) != set(player_names)
                or any(set(match[field]) != set(player_names) for field in ("auction_scores", "scores"))):
            raise ValueError("日志参赛名单不一致：每日正常出价、auction_scores 与 scores 必须覆盖同一名单")
        for use in day["super_uses"]:
            _require_fields(use, ("player", "value", "hit"), "super_uses 条目")
            if not isinstance(use["player"], str) or not use["player"].strip():
                raise ValueError("日志 super_uses.player 必须是非空玩家名")
            if type(use["value"]) is not int or not 0 <= use["value"] <= game.max_bid:
                raise ValueError("日志 super_uses.value 必须是 0..max_bid 的有效整数（拒绝 bool）")
            if type(use["hit"]) is not bool:
                raise ValueError("日志 super_uses.hit 必须是布尔值")
        if any(record["player"] not in player_names for record in day["bids"] + day["super_uses"]):
            raise ValueError("日志参赛名单不一致：出价或超级出价含未知玩家")
        normal_by_player = {bid["player"]: bid for bid in day["bids"] if not bid["is_super"]}
        super_players = set()
        for bid in day["bids"]:
            if not bid["is_super"]:
                continue
            if bid["player"] in super_players:
                raise ValueError("日志超级出价重复：同一玩家每天最多一笔")
            super_players.add(bid["player"])
            normal = normal_by_player[bid["player"]]
            if normal["status"] == "illegal" or normal["bid"] == bid["bid"]:
                raise ValueError("日志超级出价无效：正常提交须合法且与超级出价异值")
    if (any(type(index) is not int for index in day_indices)
            or day_indices != list(range(1, game.max_days + 1))):
        raise ValueError(f"第 {match['match_index']} 局天序不完整：必须依次为 1..{game.max_days}，"
                         f"实际为 {day_indices}")
    auction_scores = dict.fromkeys(player_names, 0)
    scores = dict.fromkeys(player_names, 0)
    carry = 0
    for day in match["days"]:
        expected_pool = game.daily_pool[day["day"] - 1] + carry
        if type(day["pool"]) is not int or day["pool"] != expected_pool:
            raise ValueError(f"第 {day['day']} 天 pool 与配置奖池及流局滚存不一致")
        effective_bids = [BidRecord(player=bid["player"], raw_bid=bid["raw_bid"], bid=bid["bid"],
                                    status=bid["status"], is_super=bid["is_super"])
                          for bid in day["bids"]]
        winner, collision_values, sniped_values = resolve_bids(effective_bids)
        if (type(day["is_void"]) is not bool or day["is_void"] != (winner is None)
                or day["winner"] != (winner.player if winner else None)
                or day["winning_bid"] != (winner.bid if winner else None)
                or (winner is not None and type(day["winning_bid"]) is not int)):
            raise ValueError(f"第 {day['day']} 天 winner、winning_bid 或 is_void 与有效出价不一致")
        if (any(type(value) is not int for value in day["collision_values"])
                or day["collision_values"] != sorted(collision_values)):
            raise ValueError(f"第 {day['day']} 天 collision_values 与有效出价不一致")
        expected_uses = {bid.player: (bid.bid, bid.bid in sniped_values)
                         for bid in effective_bids if bid.is_super}
        actual_uses = {use["player"]: (use["value"], use["hit"]) for use in day["super_uses"]}
        if len(actual_uses) != len(day["super_uses"]) or actual_uses != expected_uses:
            raise ValueError(f"第 {day['day']} 天 super_uses 与超级出价及实际命中不一致")
        expected_auction_score = 0 if day["is_void"] else day["pool"]
        if type(day["auction_score"]) is not int or day["auction_score"] != expected_auction_score:
            raise ValueError(f"第 {day['day']} 天 auction_score 与实际奖池结算不一致")
        daily_scores = calculate_day_scores(player_names, winner=day["winner"],
                                            auction_score=day["auction_score"],
                                            super_uses=day["super_uses"], game=game)
        if day["winner"] is not None:
            auction_scores[day["winner"]] += day["auction_score"]
        for name in player_names:
            scores[name] += daily_scores[name]
        carry = day["pool"] if day["is_void"] else 0
    for field, expected in (("auction_scores", auction_scores), ("scores", scores)):
        if match[field] != expected:
            raise ValueError(f"日志 match.{field} 与每日计分累计不一致")
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
    auction_scores_history = {name: [] for name in names}
    scores_history = {name: [] for name in names}
    for position, (path, payload) in enumerate(zip(match_files, matches), 1):
        match = payload["match"]
        if match["match_index"] != position:
            raise ValueError(f"{path.name}：累计排名校验要求完整历史，局号必须从 1 连续排列")
        for name in names:
            auction_scores_history[name].append(match["auction_scores"][name])
            scores_history[name].append(match["scores"][name])
        expected = [asdict(entry) for entry in compute_standings(
            auction_scores_history, scores_history=scores_history)]
        actual = payload.get("standings_after_match")
        # JSON 比较同时保留 bool 与 int 的区别，逐局核对累计值、排名和全部历史。
        if (not isinstance(actual, list)
                or json.dumps(actual, sort_keys=True) != json.dumps(expected, sort_keys=True)):
            raise ValueError(f"{path.name}：standings_after_match 与已完成各局累计排名或历史不一致")
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
    """聚合各局总分及拍卖分矩阵，玩家按总分降序；均价只计正常出价。"""
    game, player_names = validate_matches(matches)
    return _collect_player_stats(matches, game, player_names)


def _collect_player_stats(matches: list[dict], game: GameSettings, player_names: list[str]) -> dict:
    """已校验场次的玩家统计，局末余额在此统一重放。"""
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
        scores = match["scores"]
        for name in player_names:
            score_matrix[name].append(scores[name])
            auction_score_matrix[name].append(match["auction_scores"][name])
        if scores:
            top_score = max(scores.values())
            champions = [name for name, score in scores.items() if score == top_score]
            # 全员 0 分也保留为并列最高；每人计一局，不折算份额。
            top_matches = solo_top_matches if len(champions) == 1 else tied_top_matches
            for name in champions:
                top_matches[name] += 1
        for day in days:
            normal_bid_counts = Counter(bid["bid"] for bid in day["bids"] if not bid["is_super"])
            if day["super_uses"]:
                super_days += 1
                # 固定当天有效正常报价，移除全部超级后重新判定；不推演未来。
                effective_normal_bids = [
                    BidRecord(player=bid["player"], raw_bid=bid["raw_bid"], bid=bid["bid"], status=bid["status"])
                    for bid in day["bids"] if not bid["is_super"]
                ]
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
        remaining = _recompute_remaining_coins(match, game, player_names)
        for name in player_names:
            end_coins[name].append(remaining[name])

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
            "illegal": illegal_counts[name],
            "zero_bids": zero_bid_counts[name],
        })
    rows.sort(key=lambda row: (-row["total_score"], row["name"]))
    return {"match_labels": match_labels, "score_matrix": score_matrix,
            "auction_score_matrix": auction_score_matrix, "rows": rows,
            "super_collision_hits": super_collision_hits,
            "super_days": super_days, "super_changed_days": super_changed_days}


def print_player_stats(stats: dict) -> None:
    """每玩家一个小块，分数与余额共用明确标注正赛/加赛的列序。"""
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
        print(f"  非法出价：{row['illegal']}次")
        print(f"  正常出价为零（含非法转0）：{row['zero_bids']}次")


def analyze(run_dir: Path) -> None:
    matches, snapshot, game, player_names = _load_match_logs(run_dir)
    player_stats = _collect_player_stats(matches, game, player_names)
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
    # 按「第几天」聚合非流局日的全部有效出价；奖池包括流局日
    bids_by_day_index: dict[int, list[int]] = {}
    pools_by_day_index: dict[int, list[int]] = {}
    winning_bids: list[int] = []
    remaining_coins_all = [coins for row in player_stats["rows"] for coins in row["end_coins"]]
    illegal_bids = sum(row["illegal"] for row in player_stats["rows"])
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
    print(f"非法出价：{illegal_bids} 笔")
    print(f"出 0 共 {zero_bids} 笔（含流局日、超级出价及非法转0），其中 0 价获胜 {zero_bid_wins} 次")


def main() -> None:
    run_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else find_latest_ai_run()
    try:
        analyze(run_dir)
    except ValueError as exc:
        sys.exit(str(exc))


if __name__ == "__main__":
    main()
