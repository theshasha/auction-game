"""运行目录、局末日志与最终结果展示。"""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from auction_game.ai import prompts
from auction_game.game.models import MatchLog, StandingsEntry
from auction_game.paths import ROOT_DIR, LOG_DIR
from auction_game.reporting.run_paths import RunPaths, RUN_TYPES, FORMAT_VERSION


def make_run_dir(mode: str) -> Path:
    """为本场运行创建独立日志目录 logs/<时间戳>_<模式>/。"""
    if mode not in RUN_TYPES:
        raise ValueError(f"未知运行类型：{mode}")
    base_name = f"{datetime.now():%Y%m%d_%H%M%S}_{mode}"
    run_dir = LOG_DIR / base_name
    suffix = 2
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    while True:
        try:
            run_dir.mkdir()
            break
        except FileExistsError:
            run_dir = LOG_DIR / f"{base_name}_{suffix}"
            suffix += 1

    print(f"本场日志目录：{run_dir}")
    return run_dir


def make_publish(log_dir: Path):
    """局末公开：完整出价日志 + 截至当前的累计排名与胜场明细，写 json/md 并打印。"""

    paths = RunPaths(log_dir)
    paths.prepare()

    def publish(log: MatchLog, standings: list[StandingsEntry]) -> None:
        payload = {
            "match": log.to_dict(),
            "standings_after_match": [asdict(entry) for entry in standings],
        }
        paths.match(log.match_index).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        kind = "加赛" if log.is_playoff else "正赛"
        text = (
            f"===== 第 {log.match_index} 局（{kind}）=====\n"
            f"{prompts.render_match_log(log)}\n\n"
            f"截至本局的累计排名:\n{prompts.render_standings(standings)}\n"
        )
        paths.match(log.match_index, readable=True).write_text(text, encoding="utf-8")
        write_readme(paths, standings)
        print(f"第 {log.match_index} 局结束（{kind}），日志已写入 {paths.match(log.match_index, readable=True)}")

    return publish


def initialize_run(run_dir, snapshot, rules):
    paths = RunPaths(run_dir)
    if run_dir.exists() and any(run_dir.iterdir()):
        raise FileExistsError(f"场次已存在，拒绝覆盖：{run_dir}")
    paths.prepare()
    snapshot = {"format_version": FORMAT_VERSION, **snapshot,
                "players": [{"number": index, **pc} for index, pc in enumerate(snapshot["players"], 1)]}
    paths.config.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (run_dir / "RULES.md").write_text(rules, encoding="utf-8")
    write_readme(paths)
    return paths


def write_readme(paths, standings=None):
    config = paths.read_config()
    lines = ["# 拍卖个人赛", f"运行类型：{RUN_TYPES[config['run_type']]}。",
             "测试模型覆盖身份调用配置。" if config["mode"] == "test_model" else "采用常规身份配置。",
             "[本场规则](RULES.md)",
             "参数与身份见 [本场快照](data/run_config.json)。统计只读取此场次。"]
    for name, title in (("speeches.md", "公开发言"), ("replay.md", "关键节点回放")):
        if (paths.root / name).exists():
            lines.append(f"[{title}]({name})")
    if standings is not None:
        standings = [StandingsEntry(**entry) if isinstance(entry, dict) else entry for entry in standings]
        lines.extend(["## 截至当前的结果", prompts.render_standings(standings)])
    lines.append("## 逐局结果")
    lines.extend(f"- [第 {int(path.stem)} 局](matches/{path.name})" for path in sorted((paths.root / "matches").glob("*.md")))
    private = [pc for pc in config["players"] if paths.player(pc["number"], readable=True).exists()]
    if private:
        lines.append("## 私人对话（含私有策略，仅供赛后阅读）")
        lines.extend(f"- [{pc['name']}](players/{pc['number']:03d}.md)" for pc in private)
    (paths.root / "README.md").write_text("\n\n".join(lines) + "\n", encoding="utf-8")


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
