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

ROOT_DIR = Path(__file__).parent
LOG_DIR = ROOT_DIR / "logs"

RUN_DIR_PATTERN = re.compile(r"^\d{8}_\d{6}_ai(_\d+)?$")  # 时间戳场次目录，排除 archive 等归档目录

FALLBACK_INITIAL_COINS = 6  # v7 起日志自带 initial_coins 字段；旧日志回退到此值（与 v5/v6 一致）
FALLBACK_MAX_BID = 6        # 同上：v7 起日志自带 max_bid，旧日志上限为 6
DAILY_SALARY = 3   # 与 models.py 保持一致：第 2 天起每天开始时的工资
SUPER_BOUNTY = 3   # 与 models.py 保持一致：超级出价命中的悬赏金币（v5 起）
SUPER_COST = 2     # 与 models.py 保持一致：超级出价每次提交的固定消耗（v6 起）
REGULAR_MATCHES = 5  # 与 tournament.py 保持一致：正赛局数（加赛局号从 REGULAR_MATCHES+1 起）


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
    return [json.loads(path.read_text(encoding="utf-8")) for path in match_files]


def recompute_remaining_coins(match: dict) -> dict[str, int]:
    """按规则从出价日志重放金币：第 2 天起每天 +3 工资。
    v6 日志（rules_version="v6"）：超级出价每次提交扣 SUPER_COST，命中 +SUPER_BOUNTY。
    v5 日志（含 collision_values 字段、无 rules_version）：超级出价不扣款，
    命中悬赏 +3 由系统印发。
    旧格式日志：正常与超级出价混在一起按笔数计撞车；凭正常出价获胜者
    （旧规则含冲顶税转判获得者）与撞车者扣除出价，超级出价永不扣款。"""
    days = match["days"]
    is_v6 = match.get("rules_version") in ("v6", "v7")
    is_new_format = bool(days) and "collision_values" in days[0]
    initial_coins = match.get("initial_coins", FALLBACK_INITIAL_COINS)
    coins = {bid["player"]: initial_coins for bid in days[0]["bids"] if not bid.get("is_super")}
    for day in days:
        if day["day"] >= 2:
            for name in coins:
                coins[name] += DAILY_SALARY
        if is_new_format:
            burned_values = set(day["collision_values"])
            for bid in day["bids"]:
                if bid.get("is_super"):
                    continue
                won = bid["player"] == day["winner"] and bid["bid"] == day["winning_bid"]
                if won or bid["bid"] in burned_values:
                    coins[bid["player"]] -= bid["bid"]
            for use in day.get("super_uses", []):
                if is_v6:
                    coins[use["player"]] -= SUPER_COST
                if use["hit"]:
                    coins[use["player"]] += SUPER_BOUNTY
            continue
        price_counts = Counter(bid["bid"] for bid in day["bids"])
        transfer_recipient = day.get("transfer_recipient")  # 旧规则日志（含冲顶税转判）才有此字段
        winner_paid_with_super = day.get("winning_is_super", False)
        for bid in day["bids"]:
            if bid.get("is_super"):
                continue  # 超级出价永不扣款
            is_collider = price_counts[bid["bid"]] >= 2
            won_with_normal = bid["player"] == day["winner"] and not winner_paid_with_super
            if won_with_normal or bid["player"] == transfer_recipient or is_collider:
                coins[bid["player"]] -= bid["bid"]
    return coins


def collect_player_stats(matches: list[dict]) -> dict:
    """按玩家聚合整场统计：每局胜场矩阵 + 玩家明细行（口径与聚合段一致）。

    返回 {"match_labels", "win_matrix", "rows"}；rows 按总胜场降序（同胜场按名字）。
    旧格式日志（无 super_uses / collision_values）降级：超级出价使用次数从 bids 的
    is_super 统计，命中率不可得（super_hits=None，展示层显示降级标记）。
    """
    player_names = [
        bid["player"] for bid in matches[0]["match"]["days"][0]["bids"] if not bid.get("is_super")
    ]
    match_labels: list[str] = []
    win_matrix: dict[str, list[int]] = {name: [] for name in player_names}
    match_champions = {name: 0.0 for name in player_names}  # 第一名并列按 1/n 计
    super_uses_count = {name: 0 for name in player_names}
    super_hits = {name: 0 for name in player_names}
    super_hit_known = True   # 任一旧格式局即无法得到全场命中数
    collision_burn = {name: 0 for name in player_names}
    normal_bids: dict[str, list[int]] = {name: [] for name in player_names}
    end_coins: dict[str, list[int]] = {name: [] for name in player_names}
    illegal_counts = {name: 0 for name in player_names}
    zero_bid_counts = {name: 0 for name in player_names}

    for payload in matches:
        match = payload["match"]
        days = match["days"]
        is_new_format = bool(days) and "collision_values" in days[0]
        if match["is_playoff"]:
            match_labels.append(f"加{match['match_index'] - REGULAR_MATCHES}")
        else:
            match_labels.append(str(match["match_index"]))
        wins = match["wins"]
        for name in player_names:
            win_matrix[name].append(wins.get(name, 0))
        if wins:
            top_wins = max(wins.values())
            champions = [name for name, count in wins.items() if count == top_wins]
            for name in champions:
                match_champions[name] += 1 / len(champions)
        for day in days:
            if is_new_format:
                burned_values = set(day["collision_values"])
            else:
                # 旧格式：正常与超级出价混在一起按笔数计撞车（与聚合段旧口径一致）
                price_counts = Counter(bid["bid"] for bid in day["bids"])
            for bid in day["bids"]:
                name = bid["player"]
                if bid["status"] == "illegal":
                    illegal_counts[name] += 1
                if bid.get("is_super"):
                    if not is_new_format:
                        super_uses_count[name] += 1
                    continue
                if bid["bid"] == 0:
                    zero_bid_counts[name] += 1
                burned = (
                    bid["bid"] in burned_values
                    if is_new_format
                    else price_counts[bid["bid"]] >= 2
                )
                if burned:
                    collision_burn[name] += bid["bid"]
                if not day["is_void"]:
                    normal_bids[name].append(bid["bid"])
            if is_new_format:
                for use in day.get("super_uses", []):
                    super_uses_count[use["player"]] += 1
                    if use["hit"]:
                        super_hits[use["player"]] += 1
        if not is_new_format:
            super_hit_known = False
        remaining = recompute_remaining_coins(match)
        for name in player_names:
            end_coins[name].append(remaining.get(name, 0))

    rows = []
    for name in player_names:
        bids = normal_bids[name]
        rows.append({
            "name": name,
            "total_wins": sum(win_matrix[name]),
            "match_champions": match_champions[name],
            "super_uses": super_uses_count[name],
            "super_hits": super_hits[name] if super_hit_known else None,
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
        if row["super_hits"] is None:
            super_text = f"{row['super_uses']}用（旧格式命中率不可得：—）"
        elif row["super_uses"]:
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
    player_names = [
        bid["player"] for bid in matches[0]["match"]["days"][0]["bids"] if not bid.get("is_super")
    ]

    total_days = 0
    void_days = 0
    # 按「第几天」聚合非流局日的全部有效出价与当天奖池
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
    super_wins = 0          # 凭超级出价获胜的天数（旧规则日志才有意义，v4 起恒 0）
    super_collisions = 0    # 旧格式（v4 及以前）：超级出价撞车的次数（狙杀/对撞）
    super_hits = 0          # 新格式（v5/v6）：超级出价命中次数（命中即 +3 悬赏）
    super_bounty_minted = 0  # 新格式（v5/v6）：命中悬赏印发的金币总数
    super_cost_spent = 0    # v6 起：超级出价固定成本消耗的金币总数（2 × 使用次数）
    cap_wins = 0            # 获胜价=当日上限（极点）的天数（新规则无冲顶税，仅作分布参考）
    cap_bid = matches[0]["match"].get("max_bid", FALLBACK_MAX_BID)  # 极点数值（v7 起从日志读）

    for payload in matches:
        match = payload["match"]
        is_v6 = match.get("rules_version") in ("v6", "v7")
        is_new_format = bool(match["days"]) and "collision_values" in match["days"][0]
        wins = match["wins"]
        for name, win_count in wins.items():
            if win_count >= 3:  # 单局多胜口径：≥3 胜场（每天奖池 1 个、单局共 5 个）
                multi_win_matches.append((match["match_index"], name, win_count))

        for day in match["days"]:
            total_days += 1
            pools_by_day_index.setdefault(day["day"], []).append(day["pool"])
            price_counts = Counter(bid["bid"] for bid in day["bids"])
            burned_values = set(day["collision_values"]) if is_new_format else set()
            for bid in day["bids"]:
                if bid["status"] == "illegal":
                    illegal_bids += 1
                if bid["bid"] == 0:
                    zero_bids += 1
                if bid.get("is_super"):
                    super_uses += 1
                    if is_v6:
                        super_cost_spent += SUPER_COST
                    if not is_new_format and price_counts[bid["bid"]] >= 2:
                        super_collisions += 1
                elif is_new_format:
                    if bid["bid"] in burned_values:
                        collision_burn += bid["bid"]
                elif price_counts[bid["bid"]] >= 2:
                    collision_burn += bid["bid"]
            for use in day.get("super_uses", []):
                if use["hit"]:
                    super_hits += 1
                    super_bounty_minted += SUPER_BOUNTY
            if day["is_void"]:
                void_days += 1
                continue
            bids_by_day_index.setdefault(day["day"], []).extend(b["bid"] for b in day["bids"])
            winning_bids.append(day["winning_bid"])
            if day["winning_bid"] == match.get("max_bid", FALLBACK_MAX_BID):
                cap_wins += 1
            if day.get("winning_is_super"):
                super_wins += 1
            if day["winning_bid"] == 0:
                zero_bid_wins += 1
        remaining_coins_all.extend(recompute_remaining_coins(match).values())

    decided_days = total_days - void_days

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
    has_new_format = any(
        payload["match"]["days"] and "collision_values" in payload["match"]["days"][0]
        for payload in matches
    )
    has_v6 = any(payload["match"].get("rules_version") in ("v6", "v7") for payload in matches)
    if has_v6:
        hit_rate = f"{super_hits / super_uses:.0%}" if super_uses else "—"
        print(f"超级出价：使用 {super_uses} 次，命中 {super_hits} 次（命中率 {hit_rate}），"
              f"成本消耗 {super_cost_spent} 金币，悬赏印发 {super_bounty_minted} 金币")
    elif has_new_format:
        hit_rate = f"{super_hits / super_uses:.0%}" if super_uses else "—"
        print(f"超级出价：使用 {super_uses} 次，命中 {super_hits} 次（命中率 {hit_rate}），"
              f"悬赏印发 {super_bounty_minted} 金币")
    else:
        print(f"超级出价：使用 {super_uses} 次，凭超级出价获胜 {super_wins} 天，"
              f"超级出价撞车 {super_collisions} 次")
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
    analyze(run_dir)


if __name__ == "__main__":
    main()
