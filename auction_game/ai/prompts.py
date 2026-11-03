"""生成模型提示词，以及出价记录和排名表。

规则正文维护于 RULES.template.md，使用本场参数与实际名单生成系统提示。
文本输出格式见 text_output，业务校验和纠错见 agent_tasks。
"""

from __future__ import annotations


from auction_game.game.game_rules import GameSettings
from auction_game.game.models import MatchLog, Observation, PlayerDayResult, StandingsEntry
from auction_game.game.rules import render_rules


# 出价要求在系统提示的规则里，这里只提醒当前任务。
BID_HINT = "请分析局势并提交今日出价。"
# 正常出价结果在出价表里的写法；被狙也写作撞车。
OUTCOME_TEXT = {"won": "获胜", "collided": "撞车", "refunded": "退款"}
# 局末发言和最终感言收到的信写于本局最后一天出价时；一天一局时就是规划，规划请求同时交了这一天的出价。
LAST_DAY_LETTERS = "在本局最后一天出价时"


def build_system_prompt(
    name: str,
    persona_title: str,
    persona_text: str,
    predictions: list[tuple[str, int]],
    all_names: list[str],
    game: GameSettings,
    *, enable_predictions: bool = True, enable_personas: bool = True,
    writing_style: str | None = None,
) -> str:
    """保留规则与身份，按独立开关加入人设和全员公开的赛前预测，写作要求放在最后。

    人设只影响发言和私信的口吻；分析出价和规划策略时仍需认真思考。
    predictions 是 players.yaml 中的虚构数字，不是模型的真实表现统计。
    """
    others = "、".join(n for n in all_names if n != name)
    text = (
        f"{render_rules(game, all_names, include_note=False)}\n"
        f"【你的身份】\n你叫 {name}，其他 {len(all_names) - 1} 位玩家是：{others}。\n"
    )
    if enable_personas:
        text += f"【你的人设】\n{persona_title}：{persona_text}\n"
    if enable_predictions:
        prediction_text = "、".join(f"{player_name} {rate}%" for player_name, rate in predictions)
        text += f"【赛前预测】\n专家根据各玩家之前的对局表现，给出的夺冠率预测为：{prediction_text}。\n"
    if enable_personas:
        text += "请在游戏过程中认真思考，不要被人设的说话方式影响你的思考。\n"
    if writing_style:
        text += writing_style
    return text


def opening_speech_request() -> str:
    return "比赛即将开始。请向全体玩家发表你的开局发言（不超过 300 字）。"


def _quoted(title: str, texts: dict[str, str]) -> str:
    """其他玩家的发言或私信：标题之后每人一段，以【名字】开头。"""
    return "\n\n".join([title, *(f"【{name}】{text}" for name, text in texts.items())])


def _received_letters(received: dict[str, str] | None, written: str) -> list[str]:
    """收到的私信接在事实信息之后、任务提示之前；没有信时不出现。received 是写信人到正文。

    written 说明信是在哪一环节写的：每次送到的信都来自收件人上一次行动的那一轮，
    信里的"今日"指写信那天。
    """
    return [_quoted(f"以下是其他玩家{written}写给你的私信：", received)] if received else []


def _remaining_scores_info(game: GameSettings, match_index: int, day: int, today_pool: int) -> str:
    """最后一局正赛和加赛显示剩余分数。"""
    if match_index < game.regular_matches:
        return ""
    remaining_auction_score = today_pool + sum(game.daily_pool[day:])
    remaining_days = game.max_days - day + 1
    return (
        f"\n\n本局还剩 {remaining_days} 天（含今天），剩余拍卖奖池共 {remaining_auction_score} 分。"
        f"如果每天都命中，每人最多还能获得 {remaining_days * game.super_hit_score} 命中分。"
    )


def _total_scores_line(standings: list[StandingsEntry]) -> str:
    """使用每日结算后的累计分数。"""
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


def opening_plan_request(others_speeches: dict[str, str], game: GameSettings, *,
                         received: dict[str, str] | None = None) -> str:
    return "\n\n".join([
        _quoted("以下是其他玩家的开局发言：", others_speeches),
        *_received_letters(received, "在开局发言时"),
        "请结合他们的发言，规划你第一局的初步策略，并直接给出第 1 局第 1 天的出价。"
        + _day1_constant_info(game)
        + "\n\n全员累计总分：全员 0 分。",
    ])


def _my_last_day_line(obs: Observation) -> str:
    """只解释本人金币结算；出价与得分由公开明细提供。

    这些都能从公开记录推出，单独写出是为了避免模型算错扣款，进而误判其他玩家的余额。
    """
    yesterday = obs.today_history[-1]
    mine = yesterday.result_of(obs.my_name)

    segments = []
    if yesterday.is_void:
        segments.append(f"所有正常出价都撞车，昨天流局，你的正常出价扣除 {mine.bid} 金币")
    elif mine.outcome == "won":
        segments.append(f"正常出价获胜，扣除 {mine.bid} 金币")
    elif mine.sniped:
        segments.append(f"正常出价被超级出价命中，扣除 {mine.bid} 金币")
    elif mine.outcome == "collided":
        segments.append(f"正常出价撞车，扣除 {mine.bid} 金币")
    else:
        segments.append("正常出价没有撞车，但也没有获胜，金币全额退还")

    if mine.super_bid is not None:
        segments.append(f"超级出价花费 {obs.game.super_cost} 金币")
        if mine.super_hit:
            segments.append(f"获得 {obs.game.super_bounty} 金币悬赏")
    return "昨日你的金币结算：\n\n" + "\n".join(f"- {segment}。" for segment in segments)


def _super_cell(result: PlayerDayResult) -> tuple[str, str]:
    """超级出价数值和命中情况；未使用时两格都是“—”。"""
    if result.super_bid is None:
        return "—", "—"
    return str(result.super_bid), "命中" if result.super_hit else "落空"


def _yesterday_bids_table(obs: Observation) -> str:
    """昨日出价与当前余额按玩家对齐；正常出价结果与局末出价日志的写法一致。"""
    headers = ["玩家", "正常出价", "正常出价结果", "超级出价", "命中情况", "当前金币"]
    lines = ["| " + " | ".join(headers) + " |", "|" + " --- |" * len(headers)]
    for result in obs.today_history[-1].players:
        cells = [result.player, str(result.bid), OUTCOME_TEXT[result.outcome], *_super_cell(result),
                 str(obs.all_coins[result.player])]
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def bid_request(obs: Observation, *, received: dict[str, str] | None = None) -> str:
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
    status_line += "\n\n" + _total_scores_line(obs.standings)
    status_line += _remaining_scores_info(obs.game, obs.match_index, obs.day, obs.today_pool)
    # 第 2 天收到的是规划时写的信，规划请求同时交了第 1 天的出价，所以也是昨天出价时写的。
    task = "\n\n".join([*_received_letters(received, "昨天出价时"), BID_HINT])
    if not obs.today_history:
        return f"当前是第 {obs.match_index} 局第 {obs.day} 天。\n\n{status_line}\n\n{task}"

    yesterday = obs.today_history[-1]
    if yesterday.is_void:
        announcement = f"昨日公布：流局，奖池 {yesterday.pool} 分作废。"
    else:
        announcement = (
            f"昨日公布：{yesterday.winner} 以 {yesterday.winning_bid} 金币的正常出价获胜，"
            f"拍卖得分 {yesterday.auction_score} 分。"
        )
    hit_scores = "、".join(
        f"{result.player} {obs.game.super_hit_score} 分" for result in yesterday.players if result.super_hit
    )
    hit_announcement = f"昨日命中得分：{hit_scores}。" if hit_scores else "昨日无人命中。"
    lines = [
        f"当前是第 {obs.match_index} 局第 {obs.day} 天。", "",
        announcement, "", hit_announcement, "", "昨日出价明细与当前金币（已含今日工资）：", "",
        _yesterday_bids_table(obs), "",
        _my_last_day_line(obs), "",
        status_line,
    ]
    return "\n".join(lines) + "\n\n" + task


def render_match_log(log: MatchLog) -> str:
    """生成共用出价表：每日得分读取每天的结算，本局总分读取局末结果。"""
    summary = [
        "本局得分：" + "、".join(f"{name} {score} 分" for name, score in log.scores.items()),
        "本局拍卖分：" + "、".join(f"{name} {score} 分" for name, score in log.auction_scores.items()),
    ]
    player_names = [result.player for result in log.days[0].players]
    header = "| 天 | 奖池 | " + " | ".join(player_names) + " | 结果 | 撞车价位 | 当日得分 |"
    separator = "|" + " --- |" * (len(player_names) + 5)
    rows = []
    for day in log.days:
        cells = []
        for result in day.players:
            cell = f"{result.bid}（{OUTCOME_TEXT[result.outcome]}）"
            if result.super_bid is not None:
                super_bid, hit = _super_cell(result)
                cell += f"；超级出价 {super_bid}（{hit}）"
            cells.append(cell)
        if day.is_void:
            outcome = "流局（奖池作废）"
        else:
            outcome = f"{day.winner} 以 {day.winning_bid} 金币获胜"
        collision_text = "、".join(str(value) for value in day.collision_values) or "—"
        score_text = "、".join(f"{result.player} {result.score}" for result in day.players if result.score) or "—"
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
    """按总分展示累计排名及已结束局分数，拍卖分和命中分作为辅助信息。

    总分只由拍卖分和命中分组成，命中分用两者之差得出。
    """
    match_count = max((len(entry.scores_per_match) for entry in standings), default=0)
    match_headers = [f"第{i + 1}局" for i in range(match_count)]
    header = "| 排名 | 玩家 | 总分 |"
    if match_headers:
        header += " " + " | ".join(match_headers) + " |"
    header += " 拍卖分 | 命中分 |"
    separator = "|" + " --- |" * (5 + match_count)
    rows = []
    for entry in standings:
        row = f"| {entry.rank} | {entry.player} | {entry.total_score} |"
        if match_count:
            cells = [str(score) for score in entry.scores_per_match]
            cells += [""] * (match_count - len(cells))
            row += " " + " | ".join(cells) + " |"
        row += f" {entry.total_auction_score} | {entry.total_score - entry.total_auction_score} |"
        rows.append(row)
    return "\n".join([header, separator, *rows])


def _match_title(log: MatchLog) -> str:
    return f"第 {log.match_index} 局（{'加赛局' if log.is_playoff else '正赛局'}）"


def match_end_speech_request(log: MatchLog, standings: list[StandingsEntry], *,
                             received: dict[str, str] | None = None) -> str:
    """只在还有下一局时调用。"""
    lines = [
        f"{_match_title(log)}结束。",
        f"本局完整出价日志：\n\n{render_match_log(log)}",
        f"截至当前的累计排名与分数明细：\n\n{render_standings(standings)}",
    ]
    # 正赛打完后还有下一局，说明第一名并列，进入加赛。
    if log.match_index >= log.game.regular_matches:
        lines.append("第一名总分并列，全员进入加赛。")
    lines.extend(_received_letters(received, LAST_DAY_LETTERS))
    lines.append("请向全体玩家发表你的局末发言（不超过 300 字）。")
    return "\n\n".join(lines)


def memory_request(others_speeches: dict[str, str], match_index: int, *,
                   received: dict[str, str] | None = None) -> str:
    """补充其他玩家的发言；本人的发言已在对话中，整理记忆后才清空对话。第 1 局结束时还没有旧记忆。

    本局出价时收到的信也在对话里，所以没有收到信时也提私信。
    """
    has_old_memory = match_index > 1
    task = (
        f"请结合{'旧记忆、' if has_old_memory else ''}本局经历、公开发言和私信，整理你的记忆，只有你自己能看到。"
        + ("新记忆会完整替换旧记忆；" if has_old_memory else "")
        + "下一局你看不到之前各局的原始对话、公开发言和私信，要继续用的信息都写在这里。\n\n"
        "- 记下对手的出价倾向和自己要做的调整，每条都要能直接用于下一局的出价。\n"
        "- 私信里谈好的约定和得到的消息，下一局还用得上的也记下来。\n"
        + ("- 保留仍然有用的旧经验，改掉被本局推翻的判断。\n" if has_old_memory else "")
        + "- 只写结论，不写例子和出价记录。\n"
        "- 规则、最新成绩和之前各局的出价日志会另行提供，无需抄写。\n\n"
        "全文不超过 1000 字。"
    )
    return "\n\n".join([_quoted("以下是其他玩家在本局结束后的公开发言：", others_speeches),
                        *_received_letters(received, "在局末发言时"), task])


def next_match_plan_request(past_logs: list[MatchLog], standings: list[StandingsEntry], game: GameSettings) -> str:
    """past_logs 是已结束的全部局。整理记忆清空了旧对话，这里补回各局出价日志，留在新一局的对话里。

    加赛局写明并列第一的玩家：进入加赛的消息只在局末发言请求里，整理记忆后就看不到了。
    """
    match_index = len(past_logs) + 1
    logs_text = "\n\n".join(f"{_match_title(log)}：\n\n{render_match_log(log)}" for log in past_logs)
    playoff_note = ""
    if match_index > game.regular_matches:
        leaders = [entry for entry in standings if entry.rank == 1]
        playoff_note = (f"第 {match_index} 局是加赛局。目前第一名总分并列："
                        f"{'、'.join(entry.player for entry in leaders)}各 {leaders[0].total_score} 分。\n\n")
    return (
        playoff_note
        + f"之前各局的完整出价日志：\n\n{logs_text}\n\n"
        f"截至当前的最新累计成绩：\n\n{render_standings(standings)}\n\n"
        f"请结合之前整理的记忆，规划第 {match_index} 局的初步策略，并直接给出第 {match_index} 局第 1 天的出价。"
        + _day1_constant_info(game, match_index)
    )


def final_speech_request(standings: list[StandingsEntry], log: MatchLog, *,
                         received: dict[str, str] | None = None) -> str:
    """请求最终感言，并提供最后一局的出价日志；之前各局的日志已在本局规划请求中给出。"""
    return "\n\n".join([
        f"{_match_title(log)}结束，这也是最后一局。",
        f"本局完整出价日志：\n\n{render_match_log(log)}",
        f"比赛全部结束。最终排名与分数明细：\n\n{render_standings(standings)}",
        *_received_letters(received, LAST_DAY_LETTERS),
        "请向全体玩家发表你的最终感言。",
    ])


def final_summary_request(others_speeches: dict[str, str]) -> str:
    return "\n\n".join([_quoted("以下是其他玩家的最终感言：", others_speeches),
                        "最后，请写一段只给观众看的总结发言，用第一人称，直接写正文。"])
