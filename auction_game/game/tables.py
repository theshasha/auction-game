"""出价表、排名表和局标题的文字渲染，提示词、场次报告和终端共用。"""

from __future__ import annotations

from auction_game.game.engine import team_standings
from auction_game.game.models import DayResult, MatchLog, PlayerDayResult, StandingsEntry, TeamStandingsEntry

# 正常出价结果在出价表里的写法，被狙也算撞车。写明扣款，因为模型常把撞车记成退款。
OUTCOME_TEXT = {"won": "获胜", "collided": "撞车扣款", "refunded": "退款"}


def super_cells(result: PlayerDayResult) -> tuple[str, str]:
    """超级出价数值和命中情况；未使用时两格都是“—”。"""
    if result.super_bid is None:
        return "—", "—"
    return str(result.super_bid), "命中" if result.super_hit else "落空"


def _player_cells(day: DayResult) -> list[str]:
    """每名玩家一格：正常出价和结果，用了超级出价时再加价位和命中情况。"""
    cells = []
    for result in day.players:
        cell = f"{result.bid}（{OUTCOME_TEXT[result.outcome]}）"
        if result.super_bid is not None:
            super_bid, hit = super_cells(result)
            cell += f"；超级出价 {super_bid}（{hit}）"
        cells.append(cell)
    return cells


def _score_text(day: DayResult) -> str:
    return "、".join(f"{result.player} {result.score}" for result in day.players if result.score) or "—"


def render_bid_table(log: MatchLog) -> str:
    """请求里给模型看的出价表：结果和撞车价位能从单元格读出，各局得分在排名表里。"""
    player_names = [result.player for result in log.days[0].players]
    return "\n".join([
        "| 天 | 奖池 | " + " | ".join(player_names) + " | 当日得分 |",
        "|" + " --- |" * (len(player_names) + 3),
        *(f"| {day.day} | {day.pool} | {' | '.join(_player_cells(day))} | {_score_text(day)} |" for day in log.days),
    ])


def render_match_log(log: MatchLog) -> str:
    """场次报告的出价表：每行是一天的结算，表后是本局各人的得分和拍卖分。"""
    summary = [
        "本局得分：" + "、".join(f"{name} {score} 分" for name, score in log.scores.items()),
        "本局拍卖分：" + "、".join(f"{name} {score} 分" for name, score in log.auction_scores.items()),
    ]
    player_names = [result.player for result in log.days[0].players]
    header = "| 天 | 奖池 | " + " | ".join(player_names) + " | 结果 | 撞车价位 | 当日得分 |"
    separator = "|" + " --- |" * (len(player_names) + 5)
    rows = []
    for day in log.days:
        if day.is_void:
            outcome = "流局（奖池作废）"
        else:
            outcome = f"{day.winner} 以 {day.winning_bid} 金币获胜"
        collision_text = "、".join(str(value) for value in day.collision_values) or "—"
        rows.append(
            f"| {day.day} | {day.pool} | {' | '.join(_player_cells(day))} | {outcome} | {collision_text} "
            f"| {_score_text(day)} |"
        )
    scoring_note = "当日得分仅列非零玩家，未列者为 0 分。"
    lines = [
        "奖池和得分的单位为分，正常出价的单位为金币。", "", header, separator, *rows,
        "", scoring_note, "", "\n\n".join(summary),
    ]
    return "\n".join(lines)


def render_standings(standings: list[StandingsEntry]) -> str:
    """按总分展示累计排名及已结束局分数，拍卖分和命中分作为辅助信息。

    总分只由拍卖分和命中分组成，命中分用两者之差得出。团队赛先列阵营排名，玩家表多一列阵营。
    """
    match_count = max((len(entry.scores_per_match) for entry in standings), default=0)
    score_headers = ["总分", *(f"第{i + 1}局" for i in range(match_count)), "拍卖分", "命中分"]

    def score_cells(entry: StandingsEntry | TeamStandingsEntry) -> list[str]:
        cells = [str(score) for score in entry.scores_per_match]
        cells += [""] * (match_count - len(cells))
        return [str(entry.total_score), *cells, str(entry.total_auction_score),
                str(entry.total_score - entry.total_auction_score)]

    teams = team_standings(standings)
    if teams is None:
        return _table(["排名", "玩家", *score_headers],
                      [[str(entry.rank), entry.player, *score_cells(entry)] for entry in standings])
    team_table = _table(["排名", "阵营", *score_headers],
                        [[str(team.rank), team.team, *score_cells(team)] for team in teams])
    player_table = _table(["排名", "玩家", "阵营", *score_headers],
                          [[str(entry.rank), entry.player, entry.team, *score_cells(entry)] for entry in standings])
    return f"{team_table}\n\n{player_table}"


def _table(headers: list[str], rows: list[list[str]]) -> str:
    lines = ["| " + " | ".join(headers) + " |", "|" + " --- |" * len(headers)]
    return "\n".join([*lines, *("| " + " | ".join(row) + " |" for row in rows)])


def match_title(log: MatchLog) -> str:
    return f"第 {log.match_index} 局（{'加赛局' if log.is_playoff else '正赛局'}）"
