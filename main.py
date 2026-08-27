"""入口：6 个 RandomBot 跑完整场比赛，输出每局完整日志与最终排名。"""

from __future__ import annotations

import argparse
import json
import random
from dataclasses import asdict
from pathlib import Path

from models import MatchLog, StandingsEntry
from players import RandomBot
from tournament import run_tournament

BOT_NAMES = ["Alpha", "Bravo", "Charlie", "Delta", "Echo", "Foxtrot"]
LOG_DIR = Path(__file__).parent / "logs"

END_REASON_LABELS = {
    "wins_exhausted": "3 个胜场全部拍出",
    "max_days": "达到第 5 天上限",
    "all_coins_zero": "全员余额归零",
}


def format_standings(standings: list[StandingsEntry]) -> str:
    lines = ["排名  玩家        总胜场  各局胜场"]
    for entry in standings:
        per_match = ",".join(str(w) for w in entry.wins_per_match)
        lines.append(f"  {entry.rank}   {entry.player:<10}  {entry.total_wins:>4}   [{per_match}]")
    return "\n".join(lines)


def format_match_log(log: MatchLog) -> str:
    kind = "加赛" if log.is_playoff else "正赛"
    lines = [f"===== 第 {log.match_index} 局（{kind}）====="]
    for day in log.days:
        bid_summary = ", ".join(
            f"{record.player}:{record.bid}" + ("(非法)" if record.status != "ok" else "")
            for record in day.bids
        )
        if day.is_void:
            outcome = "流局"
        else:
            outcome = f"{day.winner} 以 {day.winning_bid} 获胜"
        lines.append(
            f"  第 {day.day} 天 | 出价: {bid_summary} | {outcome} "
            f"| 作废价位数: {day.voided_price_count} | 分红: {day.dividend}"
        )
    lines.append(f"  结束原因: {END_REASON_LABELS.get(log.end_reason, log.end_reason)}")
    lines.append(f"  本局胜场: {log.wins}")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="唯一高价拍卖游戏 demo（RandomBot 版）")
    parser.add_argument("--seed", type=int, default=None, help="随机种子，用于复现对局")
    args = parser.parse_args()

    rng = random.Random(args.seed)
    players = [RandomBot(name, random.Random(rng.random())) for name in BOT_NAMES]
    LOG_DIR.mkdir(exist_ok=True)

    def publish(log: MatchLog, standings: list[StandingsEntry]) -> None:
        # 局末公开：该局完整出价日志 + 截至当前的累计排名与胜场明细
        standings_data = [asdict(entry) for entry in standings]
        payload = {"match": log.to_dict(), "standings_after_match": standings_data}
        (LOG_DIR / f"match_{log.match_index}.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        text = format_match_log(log) + "\n\n截至本局的累计排名:\n" + format_standings(standings) + "\n"
        (LOG_DIR / f"match_{log.match_index}.txt").write_text(text, encoding="utf-8")
        print(f"第 {log.match_index} 局结束（{'加赛' if log.is_playoff else '正赛'}），日志已写入 logs/match_{log.match_index}.json/.txt")

    result = run_tournament(players, on_match_end=publish)

    print(f"\n共进行 {len(result.match_logs)} 局（正赛 5 局 + 加赛 {result.playoff_count} 局）")
    if result.playoff_cap_hit:
        print("警告：加赛达到安全上限，第一名仍为并列，按当前总胜场结算")
    print("\n===== 最终排名 =====")
    print(format_standings(result.final_standings))
    champion = result.final_standings[0]
    tied_champions = [e.player for e in result.final_standings if e.rank == 1]
    if len(tied_champions) > 1:
        print(f"\n并列冠军: {', '.join(tied_champions)}（{champion.total_wins} 胜）")
    else:
        print(f"\n冠军: {champion.player}（{champion.total_wins} 胜）")


if __name__ == "__main__":
    main()
