"""把一场打完的比赛整理成对局回放页：按轮次列出每人的策略、信、发言、记忆和每天的出价结果。

页面是给别人看的，配置只取白名单字段：名字、人设（开了人设时）、队伍、型号和推理档位、规则参数；任务日志只取结果。
场次快照里有渠道名和能力快照，任务日志里有 thinking 和提示词原文，这些不公开。命令行入口在 auction_game.cli.replay。
"""

from __future__ import annotations

import json
from pathlib import Path
import re

from auction_game.agent.log import read_records
from auction_game.reporting.analyze import balances_before_bids, load_match_logs
from auction_game.reporting.run_output import is_finished, model_label
from auction_game.reporting.run_paths import OFFLINE_RUN_TYPES, RunPaths

TEMPLATE = Path(__file__).with_name("replay.html")
OUTCOME = {"won": "w", "collided": "c", "refunded": ""}
# 任务标题到（局，列）；局为 None 的是整场最后两轮，归到最后一局。
# 列：open 开局发言，t1–t3 团队赛讨论，d1–d5 出价日（d1 是规划轮），sp 发言或感言，mem 记忆或观众总结。
TITLES = [
    (re.compile(r"^开局发言$"), lambda m: (1, "open")),
    (re.compile(r"^第 (\d+) 局开局讨论第 (\d+) 轮$"), lambda m: (int(m[1]), f"t{m[2]}")),
    (re.compile(r"^第 (\d+) 局策略规划（含首日出价）$"), lambda m: (int(m[1]), "d1")),
    (re.compile(r"^第 (\d+) 局第 (\d+) 天出价$"), lambda m: (int(m[1]), f"d{m[2]}")),
    (re.compile(r"^第 (\d+) 局局末发言$"), lambda m: (int(m[1]), "sp")),
    (re.compile(r"^第 (\d+) 局累计私有记忆$"), lambda m: (int(m[1]), "mem")),
    (re.compile(r"^最终感言$"), lambda m: (None, "sp")),
    (re.compile(r"^观众总结（仅观众可见）$"), lambda m: (None, "mem")),
]


def _place(title: str, last: int) -> tuple[int, str]:
    for pattern, where in TITLES:
        found = pattern.match(title)
        if found:
            index, key = where(found)
            return index or last, key
    raise ValueError(f"认不出的轮次标题：{title}")


def round_records(paths: RunPaths, number: int, last: int) -> dict[tuple[int, str], dict]:
    """编号为 number 的玩家每一轮的成功任务记录，键是（局，列）；last 是这场的局数。"""
    records, _ = read_records(paths.player(number))
    return {_place(entry["title"], last): entry for entry in records[1:] if entry["status"] == "success"}


def _columns(index: int, days: int, team: bool) -> list[str]:
    keys = ["open"] if index == 1 else []
    if team:
        keys += ["t1", "t2", "t3"]
    return keys + [f"d{day}" for day in range(1, days + 1)] + ["sp", "mem"]


def build_replay(run_dir: Path) -> dict:
    """页面数据；不是场次目录或这场没打完时报 ValueError。"""
    paths = RunPaths(run_dir)
    if not paths.config.exists():
        raise ValueError(f"{run_dir} 不是场次目录：没有 data/run_config.json")
    config = paths.read_config()
    if not is_finished(paths):
        hint = "" if config["run_type"] in OFFLINE_RUN_TYPES else "，先续跑打完"
        raise ValueError(f"{run_dir} 这场没打完{hint}；回放只支持打完的场次")
    game = config["game"]
    players = config["players"]
    names = [pc["name"] for pc in players]
    teams = list(dict.fromkeys(pc["team"] for pc in players if pc["team"]))
    team_of = [teams.index(pc["team"]) if pc["team"] else None for pc in players]
    logs = load_match_logs(run_dir)
    last = len(logs)
    results = [{where: entry["result"] for where, entry in round_records(paths, pc["number"], last).items()}
               for pc in players]
    columns = [_columns(index, len(log["match"]["days"]), bool(teams)) for index, log in enumerate(logs, 1)]
    order = [(index, key) for index, keys in enumerate(columns, 1) for key in keys]

    def letters_out(i, where):
        return [[letter["to"], letter["text"]] for letter in results[i][where].get("letters", [])]

    def letters_in(i, where):
        """信在收件人的下一次请求里送达：取上一轮别人写给她的信。"""
        previous = order[order.index(where) - 1]
        received = []
        for j in range(len(names)):
            for to, text in letters_out(j, previous) if j != i else ():
                if (team_of[j] == team_of[i] if teams else to == names[i]):
                    received.append([j, text])
        return received

    totals_before = [0] * len(names)
    matches = []
    for index, log in enumerate(logs, 1):
        days = []
        score = list(totals_before)
        by_day = [{result["player"]: result for result in day["players"]} for day in log["match"]["days"]]
        balances = balances_before_bids(game["initial_coins"], game["daily_salary"],
                                        [[by[n]["coins_delta"] for n in names] for by in by_day])
        for day, by, coins in zip(log["match"]["days"], by_day, balances):
            # coins、score 是当天出价前的金币和累计得分，和这天请求里的表一致。
            days.append({
                "pool": day["pool"], "void": day["winner"] is None, "coins": coins, "score": list(score),
                "p": [[by[n]["bid"], OUTCOME[by[n]["outcome"]], by[n]["super_bid"], int(by[n]["super_hit"])]
                      for n in names],
            })
            score = [value + by[n]["score"] for value, n in zip(score, names)]
        standings = {entry["player"]: entry for entry in log["standings_after_match"]}

        note = None
        if log["match"]["is_playoff"]:
            before = {entry["player"]: entry for entry in logs[index - 2]["standings_after_match"]}
            if teams:
                note = f"加赛：两队各 {sum(before[n]['total_score'] for i, n in enumerate(names) if team_of[i] == 0)} 分"
            else:
                leaders = [n for n in names if before[n]["rank"] == 1]
                note = f"加赛：{'、'.join(leaders)}并列第一，各 {before[leaders[0]]['total_score']} 分"

        rounds = {}
        for key in columns[index - 1]:
            entries = []
            for i in range(len(names)):
                result = results[i][(index, key)]
                if isinstance(result, str):
                    entries.append({"sum": result})
                    continue
                entry = {field: result[field] for field in ("strategy", "speech", "memory") if field in result}
                if "letters" in result:
                    entry["out"] = letters_out(i, (index, key))
                if key != "open":
                    entry["in"] = letters_in(i, (index, key))
                entries.append(entry)
            rounds[key] = entries
        matches.append({
            "playoff": log["match"]["is_playoff"], "note": note, "cols": columns[index - 1], "days": days,
            "rounds": rounds,
            "total": [standings[n]["total_score"] for n in names], "rank": [standings[n]["rank"] for n in names],
        })
        totals_before = [standings[n]["total_score"] for n in names]

    final = {entry["player"]: entry for entry in logs[-1]["standings_after_match"]}
    # 关了人设的场次，模型看不到人设，页面也不写。
    personas = config["prompts"]["enable_personas"]
    return {
        "run": run_dir.name, "teams": teams or None, "maxBid": game["max_bid"], "superHit": game["super_hit_score"],
        "players": [{"n": pc["name"], **({"persona": pc["persona_title"]} if personas else {}), "t": team_of[i],
                     "model": model_label(pc["execution_model"]["model"],
                                          pc["execution_model"]["capabilities"]["reasoning_status"])}
                    for i, pc in enumerate(players)],
        "scores": [final[n]["scores_per_match"] for n in names],
        "matches": matches,
    }


def render_replay(data: dict) -> str:
    """把数据填进页面模板。数据里的 < 全部转义：模型文本里的 <!--<script> 之类会让内嵌的 JSON 提前结束或解析出错。"""
    text = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c")
    title = data["run"].replace("&", "&amp;").replace("<", "&lt;")
    return TEMPLATE.read_text(encoding="utf-8").replace("__TITLE__", title).replace("__DATA__", text)
