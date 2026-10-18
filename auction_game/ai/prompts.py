"""拍卖业务提示的集中入口 + 局日志/排名的可读渲染。

规则正文维护于 RULES.template.md，按本场 game 与实际名单实时渲染到 system。
依次提供 system、开局、规划、出价、局末和终局提示；schema 与纠错留在 agent_tasks。
"""

from __future__ import annotations

from typing import Optional

from auction_game.game.engine import calculate_day_scores
from auction_game.game.game_rules import GameSettings
from auction_game.game.models import MatchLog, Observation, StandingsEntry
from auction_game.game.rules import render_rules


def bid_format_hint(game: GameSettings) -> str:
    """合法性由 system 规则与任务 schema 声明，参数保留兼容。"""
    return "请分析局势并提交今日出价。"


def speech_format_hint() -> str:
    """保持私有策略与公开信号的业务边界。"""
    return "speech 公开，strategy 仅自己可见。"


def with_output_requirement(prompt: str, tool_name: str | None) -> str:
    """统一追加工具提交提醒；字段格式交给 schema，工具名来自任务映射。"""
    if tool_name is None:
        return prompt
    requirement = f"当前输出要求：调用 `{tool_name}` 工具提交；不要在正文模拟工具调用。"
    return prompt + "\n\n" + requirement


def build_system_prompt(
    name: str,
    persona_title: str,
    persona_text: str,
    predictions: list[tuple[str, int]],
    all_names: list[str],
    game: GameSettings,
) -> str:
    """system 提示词：规则 + 身份 + 人设 + 全员公开的赛前预测。

    人设只约束公开发言的口吻（开局/局末/终局感言），出价推理与策略规划
    明确要求保持冷静认真——实证三无/天然呆类人设若带入推理会拉低决策质量。
    predictions 是虚构数字（players.yaml 静态配置），与真实表现无关。
    """
    others = "、".join(n for n in all_names if n != name)
    prediction_text = "、".join(f"{player_name} {rate}%" for player_name, rate in predictions)
    return (
        f"{render_rules(game, all_names, include_note=False)}\n"
        f"【你的身份】\n你叫 {name}，其他 {len(all_names) - 1} 位玩家是：{others}。\n"
        f"【你的人设】\n{persona_title}：{persona_text}\n"
        f"【赛前预测】\n专家根据各玩家之前的对局表现，给出的夺冠率预测为：{prediction_text}。\n"
        "请在游戏过程中认真思考，不要被人设的说话方式影响你的思考。"
    )


def opening_speech_request() -> str:
    return (
        "比赛即将开始。请向全体玩家发表你的开局发言（不超过 300 字）。\n\n"
        + speech_format_hint()
    )


def _remaining_scores_info(game: GameSettings, match_index: int, day: int, today_pool: int) -> str:
    """最后一局正赛与各局加赛展示余量；命中分按每人独立计算。"""
    if match_index < game.regular_matches:
        return ""
    remaining_auction_score = today_pool + sum(game.daily_pool[day:])
    remaining_days = game.max_days - day + 1
    return (
        f"\n本局含今天还剩 {remaining_days} 天，拍卖奖池 {remaining_auction_score} 分，"
        f"每人理论最多再得 {remaining_days * game.super_hit_score} 命中分。"
    )


def _total_scores_line(standings: list[StandingsEntry]) -> str:
    """每日直接使用结算后的累计分数，不从历史重新计算。"""
    return "全员累计总分：" + "、".join(
        f"{entry.player} {entry.total_score} 分" for entry in standings
    ) + "。"


def _day1_constant_info(game: GameSettings, match_index: int = 1) -> str:
    """第 1 天信息由本场参数确定，各信息行在此完成句末标点。"""
    final_note = "\n本局最后一天，结束后金币清零。" if game.max_days == 1 else ""
    return (
        f"（第 1 天：你的金币 {game.initial_coins}；"
        f"今日奖池 {game.daily_pool[0]} 分）。"
        + final_note
        + _remaining_scores_info(game, match_index, 1, game.daily_pool[0])
    )


def opening_plan_request(others_speeches: dict[str, str], game: GameSettings) -> str:
    return (
        "以下是其他玩家的开局发言：\n"
        + "\n".join(f"【{name}】{text}\n" for name, text in others_speeches.items())
        + "\n请结合他们的发言，规划你第一局的初步策略，并直接给出第 1 局第 1 天的出价"
        + _day1_constant_info(game)
        + "\n全员累计总分：全员 0 分。\n\n"
        + bid_format_hint(game)
    )


def _my_last_day_line(obs: Observation) -> Optional[str]:
    """只解释本人金币结算；报价与得分由公开明细提供。"""
    if obs.my_last_bid is None:
        return None
    yesterday = obs.today_history[-1]
    is_void = yesterday["winner"] == "流局"
    won = yesterday["winner"] == obs.my_name

    if obs.my_last_bid_illegal:
        segments = ["提交非法，按规则整单记为正常出价 0"]
    else:
        segments = []
    if is_void:
        segments.append(f"正常出价全部撞车流局，扣除 {obs.my_last_deduction} 金币")
    elif won:
        segments.append(f"正常出价获胜，扣除 {obs.my_last_deduction} 金币")
    elif obs.my_last_sniped:
        segments.append(f"正常出价被狙中，扣除 {obs.my_last_deduction} 金币")
    elif obs.my_last_collided:
        segments.append(f"正常出价撞车，扣除 {obs.my_last_deduction} 金币")
    else:
        segments.append("正常出价未撞车但非最高，全额退还")

    if obs.my_last_super_bid is not None:
        segments.append(f"超级出价成本 {obs.game.super_cost} 金币")
        if obs.my_last_super_hit:
            segments.append(f"获得 {obs.game.super_bounty} 金币悬赏")
    return "昨日你的金币结算：" + "，".join(segments) + "。"


def _bid_detail_text(detail: dict, super_hit_by_player: dict[str, bool]) -> str:
    """单日出价明细的一人份：「甲 4」或「甲 4+超6（命中）」。

    命中情况可从公开报价得知，直接标注方便阅读。
    """
    text = f"{detail['player']} {detail['bid']}"
    if detail["super_bid"] is not None:
        hit = super_hit_by_player.get(detail["player"], False)
        text += f"+超{detail['super_bid']}（{'命中' if hit else '落空'}）"
    return text


def bid_request(obs: Observation) -> str:
    match_index = len(obs.past_match_logs) + 1
    # 余额已经包含当天工资，注明这一点，避免重复计算。
    status_notes = []
    if obs.day >= 2:
        status_notes.append("已含今日工资")
    status_notes.append(f"正常出价上限：{min(obs.game.max_bid, obs.my_coins)}")
    status_line = (
        f"今日奖池：{obs.today_pool} 分；"
        f"你的金币余额：{obs.my_coins}（{'，'.join(status_notes)}）；"
        f"本局得分：{obs.my_match_score} 分。"
    )
    if obs.day == obs.game.max_days:
        status_line += "\n本局最后一天，结束后金币清零。"
    status_line += "\n" + (
        _total_scores_line(obs.standings)
        if obs.standings
        else f"你的累计总分：{obs.my_total_score} 分。"
    )
    status_line += _remaining_scores_info(obs.game, match_index, obs.day, obs.today_pool)
    if not obs.today_history:
        return f"当前是第 {match_index} 局第 {obs.day} 天。{status_line}\n\n{bid_format_hint(obs.game)}"

    yesterday = obs.today_history[-1]
    winner = None if yesterday["winner"] == "流局" else yesterday["winner"]
    if winner is None:
        announcement = f"昨日公布：流局，奖池 {yesterday['pool']} 分并入今日。"
    else:
        announcement = (
            f"昨日公布：{winner} 以 {yesterday['winning_bid']} 获胜，"
            f"拍卖得分 {yesterday['auction_score']} 分。"
        )
    # 保留含 0 出价与超级出价命中状态的完整明细。
    super_hit_by_player = {use["player"]: use["hit"] for use in yesterday["super_uses"]}
    hit_scores = "、".join(
        f"{name} {obs.game.super_hit_score} 分"
        for name, hit in super_hit_by_player.items()
        if hit
    )
    announcement += f"昨日命中得分：{hit_scores}。" if hit_scores else "昨日无人命中。"
    announcement += (
        "昨日出价明细："
        + "、".join(
            _bid_detail_text(detail, super_hit_by_player)
            for detail in yesterday["bids_detail"]
        )
        + "。"
    )
    if obs.all_coins is not None:
        announcement += (
            "当前全场金币："
            + "、".join(f"{name} {amount}" for name, amount in obs.all_coins.items())
            + "。"
        )
    lines = [f"当前是第 {match_index} 局第 {obs.day} 天。", announcement]
    my_last_line = _my_last_day_line(obs)
    if my_last_line is not None:
        lines.append(my_last_line)
    lines.append(status_line)
    return "\n".join(lines) + "\n\n" + bid_format_hint(obs.game)


def render_match_log(log: MatchLog) -> str:
    """共用局末表：逐日分数复用引擎计算，局末总分读取结算产物。"""
    summary = [
        "本局得分：" + "、".join(f"{name} {score} 分" for name, score in log.scores.items()),
        "本局拍卖分：" + "、".join(f"{name} {score} 分" for name, score in log.auction_scores.items()),
    ]
    if not log.days:
        return "\n".join(["  （本局未进行任何拍卖）", "", *summary])
    player_names = [record.player for record in log.days[0].bids if not record.is_super]
    header = "| 天 | 奖池（分） | " + " | ".join(player_names) + " | 结果 | 撞车价位 | 当日得分（分） |"
    separator = "|" + " --- |" * (len(player_names) + 5)
    rows = []
    for day in log.days:
        hit_by_player = {use["player"] for use in day.super_uses if use["hit"]}
        cells = []
        for name in player_names:
            normal = next(record for record in day.bids if record.player == name and not record.is_super)
            super_record = next(
                (record for record in day.bids if record.player == name and record.is_super),
                None,
            )
            cell = str(normal.bid)
            if name == day.winner:
                cell += "（获胜）"
            elif normal.bid in day.collision_values:
                cell += "（撞车）"
            else:
                cell += "（退款）"
            if super_record is not None:
                cell += f"+超{super_record.bid}"
                if name in hit_by_player:
                    cell += "（命中）"
                else:
                    cell += "（落空）"
            cells.append(cell)
        if day.is_void:
            outcome = "流局（奖池作废）" if day.day == log.game.max_days else "流局"
        else:
            outcome = f"{day.winner} 以 {day.winning_bid} 获胜"
        collision_text = "、".join(str(value) for value in day.collision_values) or "—"
        day_scores = calculate_day_scores(
            player_names,
            winner=day.winner,
            auction_score=day.auction_score,
            super_uses=day.super_uses,
            game=log.game,
        )
        score_text = "、".join(f"{name} {score}" for name, score in day_scores.items() if score) or "—"
        rows.append(
            f"| {day.day} | {day.pool} | {' | '.join(cells)} | {outcome} | {collision_text} | {score_text} |"
        )
    scoring_note = "当日得分仅列非零玩家，未列者为 0 分。"
    lines = [header, separator, *rows, "", scoring_note, *summary]
    return "\n".join(lines)


def render_standings(standings: list[StandingsEntry]) -> str:
    """按总分展示累计排名及已结束局分数，拍卖分作为辅助信息。"""
    match_count = max((len(entry.scores_per_match) for entry in standings), default=0)
    match_headers = [f"第{i + 1}局（分）" for i in range(match_count)]
    header = "| 排名 | 玩家 | 总分（分） |"
    if match_headers:
        header += " " + " | ".join(match_headers) + " |"
    header += " 拍卖分（分） |"
    separator = "|" + " --- |" * (4 + match_count)
    rows = []
    for entry in standings:
        row = f"| {entry.rank} | {entry.player} | {entry.total_score} |"
        if match_count:
            cells = [str(score) for score in entry.scores_per_match]
            cells += [""] * (match_count - len(cells))
            row += " " + " | ".join(cells) + " |"
        row += f" {entry.total_auction_score} |"
        rows.append(row)
    return "\n".join([header, separator, *rows])


def match_end_speech_request(log: MatchLog, standings: list[StandingsEntry], will_continue: bool) -> str:
    kind = "加赛局" if log.is_playoff else "正赛局"
    lines = [
        f"第 {log.match_index} 局（{kind}）结束。",
        f"本局完整出价日志：\n{render_match_log(log)}",
        f"截至当前的累计排名与分数明细：\n{render_standings(standings)}",
    ]
    # 只有正赛打完（或加赛后）第一名仍并列才有信息增量，其余局数无需预告
    if will_continue and log.match_index >= log.game.regular_matches:
        lines.append("第一名总分并列，全员进入加赛。")
    lines.append("请向全体玩家发表你的局末发言（不超过 300 字）。")
    lines.append(speech_format_hint())
    return "\n\n".join(lines)


def memory_request(others_speeches: dict[str, str]) -> str:
    """仅补充他人发言；本人的发言已在当前历史中，整理完成后才清空。"""
    return (
        "以下是其他玩家的局末公开发言：\n"
        + "\n".join(f"【{name}】{text}" for name, text in others_speeches.items())
        + "\n\n请综合旧记忆、本局经历与公开发言，写累计私有记忆，只填写 memory。"
        "新记忆整体替换旧记忆，下局看不到历局原始对话与公开发言。"
        "保留早期有效经验，修正失效判断，区分事实与猜测；"
        "重点记对手规律、自身修正和有用信号，避免流水账。"
        "规则与最新成绩会另行提供，无需抄写。以一页简短笔记为软目标，"
        "无需填满或精确计字；无有效信息可直说。"
    )


def next_match_plan_request(standings: list[StandingsEntry], game: GameSettings, match_index: int) -> str:
    return (
        f"截至当前的最新累计成绩：\n{render_standings(standings)}\n\n"
        f"请结合累计私有记忆，规划第 {match_index} 局的初步策略，并直接给出第 {match_index} 局第 1 天的出价"
        + _day1_constant_info(game, match_index)
        + "\n\n"
        + bid_format_hint(game)
    )


def final_speech_request(standings: list[StandingsEntry], log: MatchLog) -> str:
    """终局感言请求：并入最后一局的完整出价日志（该局已跳过局末发言）。"""
    kind = "加赛局" if log.is_playoff else "正赛局"
    return (
        f"第 {log.match_index} 局（{kind}）结束，这也是最后一局。本局完整出价日志：\n"
        f"{render_match_log(log)}\n\n"
        f"比赛全部结束。最终排名与分数明细：\n{render_standings(standings)}\n\n"
        "请向全体玩家发表你的最终感言。\n\n"
        + speech_format_hint()
    )


def final_summary_request(others_speeches: dict[str, str]) -> str:
    return (
        "以下是其他玩家的最终感言：\n"
        + "\n".join(f"【{name}】{text}\n" for name, text in others_speeches.items())
        + "\n最后，请写一段只给观众看的总结发言（不会给其他玩家看到）。"
    )
