"""场次目录、公开发言、私信、每日战报、局末日志与最终结果展示。"""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from auction_game.game.engine import team_standings
from auction_game.game.models import MatchLog, StandingsEntry
from auction_game.game.tables import render_match_log, render_standings
from auction_game.paths import LOG_DIR
from auction_game.reporting.run_paths import OFFLINE_RUN_TYPES, RunPaths, RUN_TYPES

# 节标题是写信的环节，说明送达时机，免得读成收信的时候。
LETTERS_HEADER = "# 私信\n\n每节是这一环节里写的信，收件人在下一次行动时收到。\n"
TEAM_LETTERS_HEADER = "# 队内信件区\n\n每节是这一环节里写到队内信件区的信，队友在下一次行动时看到。\n"


def make_run_dir(mode: str) -> Path:
    """为本场运行创建独立日志目录 logs/<时间戳>_<模式>/，同名时加后缀。"""
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


def publish_match(paths: RunPaths, log: MatchLog, standings: list[StandingsEntry]) -> None:
    """每局结束时写入该局的完整结算 JSON，并把出价表和累计排名追加到 matches.md，终端只提示阅读位置。"""
    payload = {
        "match": log.to_dict(),
        "standings_after_match": [asdict(entry) for entry in standings],
    }
    paths.match(log.match_index).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    kind = "加赛" if log.is_playoff else "正赛"
    text = (
        f"\n## 第 {log.match_index} 局（{kind}）\n\n"
        f"{render_match_log(log)}\n\n"
        f"截至本局的累计排名：\n\n{render_standings(standings)}\n"
    )
    _append(paths.matches_report, text)
    write_readme(paths, standings)
    print(f"第 {log.match_index} 局结束（{kind}），日志已写入 {paths.matches_report}")


def publish_speeches(paths: RunPaths, title: str, speeches: dict[str, str]) -> None:
    """公开发言按配置中的玩家顺序追加到 speeches.md，并打印到终端。"""
    text = "\n".join([f"\n===== {title} =====", *(f"【{name}】{speech}" for name, speech in speeches.items())])
    _append(paths.speeches, text + "\n")
    print(text)


def record_letters(paths: RunPaths, title: str, letters: list[tuple[str, str, str]], *, team: bool = False) -> None:
    """这一轮有人写信时向 letters.md 追加一节，第一次有信时创建文件；终端不打印。

    letters 是 (写信人, 收件人, 正文)，写信人按配置顺序；团队赛的收件人是写信人的阵营。
    """
    if not letters:
        return
    text = f"\n## {title}\n\n" + "\n\n".join(f"【{sender} → {to}】{body}" for sender, to, body in letters) + "\n"
    header = TEAM_LETTERS_HEADER if team else LETTERS_HEADER
    _append(paths.letters, text if paths.letters.exists() else header + text)


def _append(path: Path, text: str) -> None:
    with path.open("a", encoding="utf-8") as file:
        file.write(text)


def initialize_run(run_dir, snapshot, rules):
    paths = RunPaths(run_dir)
    if run_dir.exists() and any(run_dir.iterdir()):
        raise FileExistsError(f"场次已存在，拒绝覆盖：{run_dir}")
    snapshot = {**snapshot,
                "players": [{"number": index, **pc} for index, pc in enumerate(snapshot["players"], 1)]}
    paths.prepare()
    paths.config.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (run_dir / "RULES.md").write_text(rules, encoding="utf-8")
    paths.matches_report.write_text("# 逐局结果\n\n[本场规则](RULES.md)\n", encoding="utf-8")
    write_readme(paths)
    return paths


def write_readme(paths, standings=None):
    """团队赛看快照里玩家的 team：标题写团队赛，私人对话按阵营分组。"""
    config = paths.read_config()
    team = any(pc["team"] for pc in config["players"])
    if config["run_type"] in OFFLINE_RUN_TYPES:
        model_note = "全员使用假模型，无真实调用。"
    elif config["mode"] == "test_model":
        model_note = "全员调用测试模型，角色身份不变。"
    else:
        model_note = "各玩家使用配置中的模型。"
    lines = [f"# 拍卖{'团队赛' if team else '个人赛'}", f"运行类型：{RUN_TYPES[config['run_type']]}。",
             model_note,
             "[本场规则](RULES.md)",
             "本场配置见 [配置快照](data/run_config.json)，统计以此为准。"]
    if paths.speeches.exists():
        lines.append("[公开发言](speeches.md)")
    if paths.letters.exists():
        lines.append("[队内信件区](letters.md)" if team else "[私信](letters.md)")
    lines.append("[逐局结果](matches.md)")
    if standings is not None:
        lines.extend(["## 截至当前的结果", render_standings(standings)])
    private = [pc for pc in config["players"] if paths.player(pc["number"], readable=True).exists()]
    if private:
        lines.append("## 私人对话（含私有策略，仅供赛后阅读）")

        def links(players):
            return [f"- [{pc['name']}](players/{pc['number']:03d}.md)" for pc in players]

        if team:
            for team_name in dict.fromkeys(pc["team"] for pc in private):
                lines.append(f"### {team_name}")
                lines.extend(links(pc for pc in private if pc["team"] == team_name))
        else:
            lines.extend(links(private))
    (paths.root / "README.md").write_text("\n\n".join(lines) + "\n", encoding="utf-8")


def print_day_result(log: MatchLog, standings: list[StandingsEntry]) -> None:
    """仅在终端展示已提交的每日结算，不写文件或生成玩家提示。同分的玩家名次相同，金币多的列在前面。"""
    coins = log.remaining_coins
    day = log.days[-1]
    kind = "加赛" if log.is_playoff else "正赛"
    print(f"\n===== 第 {log.match_index} 局（{kind}）第 {day.day} 天 =====")
    if day.is_void:
        outcome = "流局，奖池作废"
    else:
        outcome = f"{day.winner} 以 {day.winning_bid} 金币获胜"
    print(f"奖池 {day.pool} 分；{outcome}。")
    hits = [f"{result.player} +{log.game.super_hit_score} 分" for result in day.players if result.super_hit]
    print("超级出价命中：" + ("；".join(hits) if hits else "无") + "。")
    teams = team_standings(standings)
    if teams is not None:
        print("阵营累计总分：" + "、".join(f"{team.team} {team.total_score} 分" for team in teams) + "。")
    print("| 排名 | 玩家 | 累计总分 | 金币（结算后） |")
    print("| --- | --- | --- | --- |")
    for entry in sorted(standings, key=lambda entry: (entry.rank, -coins[entry.player])):
        print(f"| {entry.rank} | {entry.player} | {entry.total_score} | {coins[entry.player]} |")


def print_final_result(result) -> None:
    """团队赛宣布获胜阵营；结束时两个阵营仍同分，只会是加赛到了上限，算平局。"""
    regular_count = sum(not log.is_playoff for log in result.match_logs)
    print(f"\n共进行 {len(result.match_logs)} 局（正赛 {regular_count} 局 + 加赛 {result.playoff_count} 局）")
    teams = team_standings(result.final_standings)
    if result.playoff_cap_hit:
        print("加赛已达上限，保留并列冠军。" if teams is None else "加赛已达上限，算平局。")
    print("\n===== 最终排名 =====")
    print(render_standings(result.final_standings))
    if teams is not None:
        if result.playoff_cap_hit:
            print(f"\n平局: {'、'.join(team.team for team in teams)}各 {teams[0].total_score} 分")
        else:
            print(f"\n获胜阵营: {teams[0].team}（{teams[0].total_score} 比 {teams[1].total_score}）")
        return
    champions = [e for e in result.final_standings if e.rank == 1]
    if len(champions) > 1:
        names = "、".join(e.player for e in champions)
        print(f"\n并列冠军: {names}（{champions[0].total_score} 分）")
    else:
        print(f"\n冠军: {champions[0].player}（{champions[0].total_score} 分）")
