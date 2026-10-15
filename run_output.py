"""运行目录、局末日志与最终结果展示。"""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

import prompts
from models import MatchLog, StandingsEntry

ROOT_DIR = Path(__file__).parent
LOG_DIR = ROOT_DIR / "logs"


def make_run_dir(mode: str) -> Path:
    """为本场运行创建独立日志目录 logs/<时间戳>_<模式>/，并更新 logs/latest 软链接。"""
    base_name = f"{datetime.now():%Y%m%d_%H%M%S}_{mode}"
    run_dir = LOG_DIR / base_name
    suffix = 2
    while run_dir.exists():
        run_dir = LOG_DIR / f"{base_name}_{suffix}"
        suffix += 1
    run_dir.mkdir(parents=True)

    latest_link = LOG_DIR / "latest"
    try:
        if latest_link.is_symlink() or latest_link.exists():
            latest_link.unlink()
        latest_link.symlink_to(run_dir.name)
    except OSError as exc:
        print(f"警告：更新 logs/latest 软链接失败（{exc}），不影响日志写入")

    print(f"本场日志目录：{run_dir.relative_to(ROOT_DIR)}")
    return run_dir


def make_publish(log_dir: Path):
    """局末公开：完整出价日志 + 截至当前的累计排名与胜场明细，写 json/md 并打印。"""

    def publish(log: MatchLog, standings: list[StandingsEntry]) -> None:
        payload = {
            "match": log.to_dict(),
            "standings_after_match": [asdict(entry) for entry in standings],
        }
        (log_dir / f"match_{log.match_index}.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        kind = "加赛" if log.is_playoff else "正赛"
        text = (
            f"===== 第 {log.match_index} 局（{kind}）=====\n"
            f"{prompts.render_match_log(log)}\n\n"
            f"截至本局的累计排名:\n{prompts.render_standings(standings)}\n"
        )
        (log_dir / f"match_{log.match_index}.md").write_text(text, encoding="utf-8")
        match_log_path = (log_dir / f"match_{log.match_index}").relative_to(ROOT_DIR)
        print(f"第 {log.match_index} 局结束（{kind}），日志已写入 {match_log_path}.json/.md")

    return publish


def print_final_result(result) -> None:
    regular_count = sum(not log.is_playoff for log in result.match_logs)
    print(f"\n共进行 {len(result.match_logs)} 局（正赛 {regular_count} 局 + 加赛 {result.playoff_count} 局）")
    if result.playoff_cap_hit:
        print("警告：加赛达到安全上限，第一名仍为并列，按当前总胜场结算")
    print("\n===== 最终排名 =====")
    print(prompts.render_standings(result.final_standings))
    champions = [e for e in result.final_standings if e.rank == 1]
    if len(champions) > 1:
        names = "、".join(e.player for e in champions)
        print(f"\n并列冠军: {names}（{champions[0].total_wins} 胜）")
    else:
        print(f"\n冠军: {champions[0].player}（{champions[0].total_wins} 胜）")
