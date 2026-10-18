"""生成模型提示词，以及出价记录和排名表。

规则正文维护于 RULES.template.md，使用本场参数与实际名单生成系统提示。
工具的输出格式和纠错处理见 agent_tasks。
"""

from __future__ import annotations

from typing import Optional

from auction_game.game.engine import calculate_day_scores
from auction_game.game.game_rules import GameSettings
from auction_game.game.models import MatchLog, Observation, StandingsEntry
from auction_game.game.rules import render_rules


def bid_format_hint(game: GameSettings) -> str:
    """系统规则与工具字段已说明出价要求，这里只提醒当前任务。"""
    return "请分析局势并提交今日出价。"


def speech_format_hint() -> str:
    """告诉玩家哪些内容会公开。"""
    return "在 speech 中填写公开发言，其他玩家都能看到。strategy 用来写发言策略，仅自己可见。"


def with_output_requirement(prompt: str, tool_name: str | None) -> str:
    """使用当前任务的工具名，提醒模型通过工具提交结果。"""
    if tool_name is None:
        return prompt
    requirement = f"请调用 `{tool_name}` 工具提交结果，不要只在正文中写出工具调用的样子。"
    return prompt + "\n\n" + requirement


def build_system_prompt(
    name: str,
    persona_title: str,
    persona_text: str,
    predictions: list[tuple[str, int]],
    all_names: list[str],
    game: GameSettings,
) -> str:
    """系统提示包含规则、身份、人设和向全员公布的赛前预测。

    人设只影响公开发言的口吻；分析出价和规划策略时仍需认真思考。
    predictions 是 players.yaml 中的虚构数字，不是模型的真实表现统计。
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
    """最后一局正赛和加赛显示剩余分数；命中分上限按每人分别计算。"""
    if match_index < game.regular_matches:
        return ""
    remaining_auction_score = today_pool + sum(game.daily_pool[day:])
    remaining_days = game.max_days - day + 1
    return (
        f"\n\n本局还剩 {remaining_days} 天（含今天），剩余拍卖奖池共 {remaining_auction_score} 分。"
        f"如果每天都命中，每人最多还能获得 {remaining_days * game.super_hit_score} 命中分。"
    )


def _total_scores_line(standings: list[StandingsEntry]) -> str:
    """每日直接使用结算后的累计分数，不从历史重新计算。"""
    return "全员累计总分：" + "、".join(
        f"{entry.player} {entry.total_score} 分" for entry in standings
    ) + "。"


def _day1_constant_info(game: GameSettings, match_index: int = 1) -> str:
    """按本场参数给出第一天的金币和奖池。"""
    final_note = "\n本局最后一天，结束后金币清零。" if game.max_days == 1 else ""
    return (
        f"\n\n第 1 天，你有 {game.initial_coins} 枚金币，今日奖池为 {game.daily_pool[0]} 分。"
        + final_note
        + _remaining_scores_info(game, match_index, 1, game.daily_pool[0])
    )


def opening_plan_request(others_speeches: dict[str, str], game: GameSettings) -> str:
    return (
        "以下是其他玩家的开局发言：\n"
        + "\n".join(f"【{name}】{text}\n" for name, text in others_speeches.items())
        + "\n请结合他们的发言，规划你第一局的初步策略，并直接给出第 1 局第 1 天的出价。"
        + _day1_constant_info(game)
        + "\n\n全员累计总分：全员 0 分。\n\n"
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
        segments = ["提交不符合规则，已改为正常出价 0，且不使用超级出价"]
    else:
        segments = []
    if is_void:
        segments.append(f"所有正常出价都撞车，昨天流局，你的正常出价扣除 {obs.my_last_deduction} 金币")
    elif won:
        segments.append(f"正常出价获胜，扣除 {obs.my_last_deduction} 金币")
    elif obs.my_last_sniped:
        segments.append(f"正常出价被超级出价命中，扣除 {obs.my_last_deduction} 金币")
    elif obs.my_last_collided:
        segments.append(f"正常出价撞车，扣除 {obs.my_last_deduction} 金币")
    else:
        segments.append("正常出价没有撞车，但也没有获胜，金币全额退还")

    if obs.my_last_super_bid is not None:
        segments.append(f"超级出价花费 {obs.game.super_cost} 金币")
        if obs.my_last_super_hit:
            segments.append(f"获得 {obs.game.super_bounty} 金币悬赏")
    return "昨日你的金币结算：\n\n" + "\n".join(f"- {segment}。" for segment in segments)


def _yesterday_bids_table(obs: Observation, super_hit_by_player: dict[str, bool]) -> str:
    """昨日报价与当前余额按玩家对齐；未提供全场余额时省略金币列。"""
    headers = ["玩家", "正常出价", "超级出价", "命中情况"]
    if obs.all_coins is not None:
        headers.append("当前金币")
    lines = ["| " + " | ".join(headers) + " |", "|" + " --- |" * len(headers)]
    for detail in obs.today_history[-1]["bids_detail"]:
        name = detail["player"]
        super_bid = detail["super_bid"]
        hit = "—" if super_bid is None else ("命中" if super_hit_by_player.get(name, False) else "落空")
        cells = [name, str(detail["bid"]), "—" if super_bid is None else str(super_bid), hit]
        if obs.all_coins is not None:
            cells.append(str(obs.all_coins[name]))
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def bid_request(obs: Observation) -> str:
    match_index = len(obs.past_match_logs) + 1
    # 余额已经包含当天工资，注明这一点，避免重复计算。
    status_notes = []
    if obs.day >= 2:
        status_notes.append("已含今日工资")
    status_notes.append(f"正常出价上限：{min(obs.game.max_bid, obs.my_coins)}")
    status_line = (
        "今日状态：\n\n"
        f"- 今日奖池：{obs.today_pool} 分。\n"
        f"- 你的金币余额：{obs.my_coins}（{'，'.join(status_notes)}）。\n"
        f"- 本局得分：{obs.my_match_score} 分。"
    )
    if obs.day == obs.game.max_days:
        status_line += "\n\n本局最后一天，结束后金币清零。"
    status_line += "\n\n" + (
        _total_scores_line(obs.standings)
        if obs.standings
        else f"你的累计总分：{obs.my_total_score} 分。"
    )
    status_line += _remaining_scores_info(obs.game, match_index, obs.day, obs.today_pool)
    if not obs.today_history:
        return f"当前是第 {match_index} 局第 {obs.day} 天。\n\n{status_line}\n\n{bid_format_hint(obs.game)}"

    yesterday = obs.today_history[-1]
    winner = None if yesterday["winner"] == "流局" else yesterday["winner"]
    if winner is None:
        announcement = f"昨日公布：流局，奖池 {yesterday['pool']} 分并入今日。"
    else:
        announcement = (
            f"昨日公布：{winner} 以 {yesterday['winning_bid']} 金币的正常出价获胜，"
            f"拍卖得分 {yesterday['auction_score']} 分。"
        )
    # 保留含 0 出价与超级出价命中状态的完整明细。
    super_hit_by_player = {use["player"]: use["hit"] for use in yesterday["super_uses"]}
    hit_scores = "、".join(
        f"{name} {obs.game.super_hit_score} 分"
        for name, hit in super_hit_by_player.items()
        if hit
    )
    hit_announcement = f"昨日命中得分：{hit_scores}。" if hit_scores else "昨日无人命中。"
    table_title = "昨日出价明细："
    if obs.all_coins is not None:
        table_title = "昨日出价明细与当前金币（已含今日工资）："
    lines = [
        f"当前是第 {match_index} 局第 {obs.day} 天。", "",
        announcement, "", hit_announcement, "", table_title, "",
        _yesterday_bids_table(obs, super_hit_by_player), "",
    ]
    my_last_line = _my_last_day_line(obs)
    if my_last_line is not None:
        lines.extend([my_last_line, ""])
    lines.append(status_line)
    return "\n".join(lines) + "\n\n" + bid_format_hint(obs.game)


def render_match_log(log: MatchLog) -> str:
    """生成共用出价表：每日得分由引擎计算，本局总分读取已结算的结果。"""
    summary = [
        "本局得分：" + "、".join(f"{name} {score} 分" for name, score in log.scores.items()),
        "本局拍卖分：" + "、".join(f"{name} {score} 分" for name, score in log.auction_scores.items()),
    ]
    if not log.days:
        return "\n".join(["  （本局未进行任何拍卖）", "", *summary])
    player_names = [record.player for record in log.days[0].bids if not record.is_super]
    header = "| 天 | 奖池 | " + " | ".join(player_names) + " | 结果 | 撞车价位 | 当日得分 |"
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
                cell += f"；超级出价 {super_record.bid}"
                if name in hit_by_player:
                    cell += "（命中）"
                else:
                    cell += "（落空）"
            cells.append(cell)
        if day.is_void:
            outcome = "流局（奖池作废）" if day.day == log.game.max_days else "流局"
        else:
            outcome = f"{day.winner} 以 {day.winning_bid} 金币获胜"
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
    lines = [
        "奖池和得分的单位为分，正常出价的单位为金币。", "", header, separator, *rows,
        "", scoring_note, "", "\n\n".join(summary),
    ]
    return "\n".join(lines)


def render_standings(standings: list[StandingsEntry]) -> str:
    """按总分展示累计排名及已结束局分数，拍卖分作为辅助信息。"""
    match_count = max((len(entry.scores_per_match) for entry in standings), default=0)
    match_headers = [f"第{i + 1}局" for i in range(match_count)]
    header = "| 排名 | 玩家 | 总分 |"
    if match_headers:
        header += " " + " | ".join(match_headers) + " |"
    header += " 拍卖分 |"
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
        f"本局完整出价日志：\n\n{render_match_log(log)}",
        f"截至当前的累计排名与分数明细：\n\n{render_standings(standings)}",
    ]
    # 正赛结束后，第一名仍并列时才提示加赛。
    if will_continue and log.match_index >= log.game.regular_matches:
        lines.append("第一名总分并列，全员进入加赛。")
    lines.append("请向全体玩家发表本局结束后的感想（不超过 300 字）。")
    lines.append(speech_format_hint())
    return "\n\n".join(lines)


def memory_request(others_speeches: dict[str, str]) -> str:
    """补充其他玩家的发言；本人的发言已在对话中，整理记忆后才清空对话。"""
    return (
        "以下是其他玩家在本局结束后的公开发言：\n\n"
        + "\n\n".join(f"【{name}】{text}" for name, text in others_speeches.items())
        + "\n\n请结合旧记忆、本局经历和公开发言，整理一份只有自己能看的笔记，只填写 memory。\n\n"
        "这份新记忆会完整替换旧记忆。下一局你看不到之前各局的原始对话和公开发言，"
        "需要继续使用的信息都应保存在这份笔记中。\n\n"
        "- 保留之前仍然有用的经验，修正已经失效的判断，并区分事实与猜测。\n"
        "- 重点记录对手的行为规律、自己需要调整的策略，以及对后续决策有用的信息，不必逐日复述。\n"
        "- 规则与最新成绩会另行提供，无需抄写。\n\n"
        "尽量整理成一页简短笔记，篇幅可以根据有用信息的多少调整，无需凑字数或精确计字。"
        "如果没有值得保留的信息，直接说明即可。"
    )


def next_match_plan_request(standings: list[StandingsEntry], game: GameSettings, match_index: int) -> str:
    return (
        f"截至当前的最新累计成绩：\n\n{render_standings(standings)}\n\n"
        f"请结合之前保存的私人笔记，规划第 {match_index} 局的初步策略，并直接给出第 {match_index} 局第 1 天的出价。"
        + _day1_constant_info(game, match_index)
        + "\n\n"
        + bid_format_hint(game)
    )


def final_speech_request(standings: list[StandingsEntry], log: MatchLog) -> str:
    """请求最终感言，并提供最后一局的出价日志；这一局不再单独安排局后发言。"""
    kind = "加赛局" if log.is_playoff else "正赛局"
    return (
        f"第 {log.match_index} 局（{kind}）结束，这也是最后一局。\n\n本局完整出价日志：\n\n"
        f"{render_match_log(log)}\n\n"
        f"比赛全部结束。最终排名与分数明细：\n\n{render_standings(standings)}\n\n"
        "请向全体玩家发表你的最终感言。\n\n"
        + speech_format_hint()
    )


def final_summary_request(others_speeches: dict[str, str]) -> str:
    return (
        "以下是其他玩家的最终感言：\n"
        + "\n".join(f"【{name}】{text}\n" for name, text in others_speeches.items())
        + "\n最后，请写一段只给观众看的总结发言（不会给其他玩家看到）。"
    )
