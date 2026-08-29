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

INITIAL_COINS = 6  # 与 models.py 保持一致：每局初始金币
DAILY_SALARY = 3   # 与 models.py 保持一致：第 2 天起每天开始时的工资


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
    """按规则从出价日志重放金币：第 2 天起每天 +3 工资；
    只扣正常出价——凭正常出价获胜者（旧规则日志含转判获得者）与撞车者扣除出价
    （流局日撞车照扣），其余全额退还；超级出价（is_super）任何结局都不扣款。"""
    coins = {bid["player"]: INITIAL_COINS for bid in match["days"][0]["bids"] if not bid.get("is_super")}
    for day in match["days"]:
        if day["day"] >= 2:
            for name in coins:
                coins[name] += DAILY_SALARY
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


def analyze(run_dir: Path) -> None:
    matches = load_match_logs(run_dir)
    player_names = [bid["player"] for bid in matches[0]["match"]["days"][0]["bids"]]

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
    collision_burn = 0   # 因撞车被没收的金币总数（只算正常出价；超级出价不扣款）
    super_uses = 0          # 超级出价使用总次数
    super_wins = 0          # 凭超级出价获胜的天数
    super_collisions = 0    # 超级出价撞车的次数（狙杀/对撞）
    cap_wins = 0            # 获胜价=6 的天数（新规则无冲顶税，仅作分布参考）

    for payload in matches:
        match = payload["match"]
        wins = match["wins"]
        for name, win_count in wins.items():
            if win_count >= 3:  # 单局多胜口径：≥3 胜场（每天奖池 1 个、单局共 5 个）
                multi_win_matches.append((match["match_index"], name, win_count))

        for day in match["days"]:
            total_days += 1
            pools_by_day_index.setdefault(day["day"], []).append(day["pool"])
            price_counts = Counter(bid["bid"] for bid in day["bids"])
            for bid in day["bids"]:
                if bid["status"] == "illegal":
                    illegal_bids += 1
                if bid["bid"] == 0:
                    zero_bids += 1
                if bid.get("is_super"):
                    super_uses += 1
                    if price_counts[bid["bid"]] >= 2:
                        super_collisions += 1
                elif price_counts[bid["bid"]] >= 2:
                    collision_burn += bid["bid"]
            if day["is_void"]:
                void_days += 1
                continue
            bids_by_day_index.setdefault(day["day"], []).extend(b["bid"] for b in day["bids"])
            winning_bids.append(day["winning_bid"])
            if day["winning_bid"] == 6:
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
        cap_wins = sum(1 for bid in winning_bids if bid == 6)
        print(f"  获胜价平均：{sum(winning_bids) / len(winning_bids):.2f}（{len(winning_bids)} 个获胜日）")
        print(f"  获胜价分布：{win_dist}；获胜价=6（极点）占比 {cap_wins / len(winning_bids):.1%}")
    print()

    print(f"局末平均剩余金币：{sum(remaining_coins_all) / len(remaining_coins_all):.2f}"
          f"（最低 {min(remaining_coins_all)}，最高 {max(remaining_coins_all)}）")
    print(f"撞车放血：全场因撞车被没收 {collision_burn} 金币"
          f"（每局平均 {collision_burn / len(matches):.1f}，只计正常出价）")
    print(f"超级出价：使用 {super_uses} 次，凭超级出价获胜 {super_wins} 天，"
          f"超级出价撞车 {super_collisions} 次")
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
