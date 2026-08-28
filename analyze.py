"""统计一场 AI 对战的日志数据。默认取 logs/ 下最新的 *_ai 目录，也可传入目录路径。

用法：
    python3 analyze.py                 # 分析最新的 AI 场次
    python3 analyze.py logs/<目录名>   # 分析指定场次
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).parent
LOG_DIR = ROOT_DIR / "logs"

RUN_DIR_PATTERN = re.compile(r"^\d{8}_\d{6}_ai(_\d+)?$")  # 时间戳场次目录，排除 archive 等归档目录

INITIAL_COINS = 6  # 与 models.py 保持一致：每局初始金币


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
    """按规则从出价日志重放金币：正常日扣除出价并加分红，流局日全额返还。

    json 中的 dividend 字段已体现「结束日不分红」，直接累加即可。
    """
    coins = {bid["player"]: INITIAL_COINS for bid in match["days"][0]["bids"]}
    for day in match["days"]:
        if day["is_void"]:
            continue
        for bid in day["bids"]:
            coins[bid["player"]] -= bid["bid"]
        if day["dividend"]:
            for name in coins:
                coins[name] += day["dividend"]
    return coins


def analyze(run_dir: Path) -> None:
    matches = load_match_logs(run_dir)
    player_names = [bid["player"] for bid in matches[0]["match"]["days"][0]["bids"]]

    total_days = 0
    void_days = 0
    # 按「第几天」聚合非流局日的全部有效出价
    bids_by_day_index: dict[int, list[int]] = {}
    winning_bids: list[int] = []
    dividends_per_match: list[int] = []      # 每局全场累计分红（每人）
    remaining_coins_all: list[int] = []
    multi_win_matches: list[tuple[int, str, int]] = []  # (局号, 玩家, 胜场)
    illegal_bids = 0
    zero_bids = 0
    zero_bid_wins = 0
    penalty_events = 0   # 胜场罚没次数（每次 1 胜场）
    penalty_wins = 0     # 罚没的胜场总数

    for payload in matches:
        match = payload["match"]
        wins = match["wins"]
        for name, win_count in wins.items():
            if win_count >= 2:
                multi_win_matches.append((match["match_index"], name, win_count))

        match_dividend_per_player = 0
        for day in match["days"]:
            total_days += 1
            for penalty in day.get("penalties", []):  # 旧日志无此字段，按无罚没处理
                penalty_events += 1
                penalty_wins += penalty["wins"]
            if day["is_void"]:
                void_days += 1
                continue
            bids_by_day_index.setdefault(day["day"], []).extend(b["bid"] for b in day["bids"])
            winning_bids.append(day["winning_bid"])
            match_dividend_per_player += day["dividend"]
            if day["winning_bid"] == 0:
                zero_bid_wins += 1
            for bid in day["bids"]:
                if bid["status"] == "illegal":
                    illegal_bids += 1
                if bid["bid"] == 0:
                    zero_bids += 1
        dividends_per_match.append(match_dividend_per_player)
        remaining_coins_all.extend(recompute_remaining_coins(match).values())

    decided_days = total_days - void_days

    print(f"场次目录：{run_dir.name}")
    print(f"玩家：{'、'.join(player_names)}")
    playoff_count = sum(1 for m in matches if m["match"]["is_playoff"])
    print(f"局数：{len(matches)}（正赛 {len(matches) - playoff_count} + 加赛 {playoff_count}）")
    print()

    print(f"总拍卖天数：{total_days}，其中流局 {void_days} 天（{void_days / total_days:.1%}）")
    print()

    print("每一天的平均出价（刨除流局日，全员有效出价）：")
    for day_index in sorted(bids_by_day_index):
        bids = bids_by_day_index[day_index]
        print(f"  第 {day_index} 天：平均 {sum(bids) / len(bids):.2f}（{len(bids)} 笔）")
    all_bids = [bid for bids in bids_by_day_index.values() for bid in bids]
    print(f"  全天总体：平均 {sum(all_bids) / len(all_bids):.2f}（{len(all_bids)} 笔）")
    print(f"  获胜价平均：{sum(winning_bids) / len(winning_bids):.2f}（{len(winning_bids)} 个胜场）")
    print()

    print(f"局末平均剩余金币：{sum(remaining_coins_all) / len(remaining_coins_all):.2f}"
          f"（最低 {min(remaining_coins_all)}，最高 {max(remaining_coins_all)}）")
    print(f"平均分红金币：每正常结算日 {sum(dividends_per_match) / max(decided_days, 1):.2f}，"
          f"每人每局累计 {sum(dividends_per_match) / len(dividends_per_match):.2f}")
    print()

    print(f"单局赢超过 1 次的玩家-局次数：{len(multi_win_matches)}"
          f"（占 {len(matches)} 局的 {len({m for m, _, _ in multi_win_matches})} 局）")
    for match_index, name, win_count in multi_win_matches:
        print(f"  第 {match_index} 局：{name} 赢 {win_count} 次")
    print()

    print(f"胜场罚没：共 {penalty_events} 次，合计 {penalty_wins} 个胜场")
    print(f"其他：非法出价 {illegal_bids} 笔；出 0 共 {zero_bids} 笔，"
          f"其中 0 价获胜 {zero_bid_wins} 次")


def main() -> None:
    run_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else find_latest_ai_run()
    analyze(run_dir)


if __name__ == "__main__":
    main()
