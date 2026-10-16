"""拍卖业务提示的集中入口 + 局日志/排名的可读渲染。

规则正文维护于 RULES.template.md，按本场 game 与实际名单实时渲染到 system。
依次提供 system、开局、规划、出价、局末和终局提示；schema 与纠错留在 agent_tasks。
"""

from __future__ import annotations

from typing import Optional

from auction_game.game.game_rules import GameSettings
from auction_game.game.models import MatchLog, Observation, StandingsEntry
from auction_game.game.rules import render_rules

# 局末展示出价日志前的精简规则重申：结算三步是模型复盘局日志时的框架，
# 实证错误（如「自己撞车推成退款」）都源于复盘时没套用这三步。
# 只放局末发言请求（局日志唯一展示处），不重发全文。
def rules_reminder(game: GameSettings) -> str:
    """使用本局参数重申结算、成本约束与末日流局例外。"""
    return (
        "【规则要点重申】每日结算按三步：① 某数值出现两笔及以上正常出价即撞车；"
        "此外，被超级出价命中的数值（其上至少有 1 笔正常出价、且有超级出价指向它），"
        "该数值上的所有正常出价也全部撞车——撞车的正常出价全部扣除且不能获胜；"
        "② 未撞车的正常出价中最高的一笔获胜、其主人获得当天全部奖池"
        "（0 是合法价位），当天正常出价全部撞车则流局、奖池并入次日"
        f"（第 {game.max_days} 天流局时奖池作废，不延长比赛）；"
        "③ 获胜与撞车的正常出价全额扣除，其余正常出价全额退还；"
        f"超级出价每次提交固定扣除 {game.super_cost} 金币（无论是否命中），"
        f"命中的狙击者获得系统印发的 {game.super_bounty} 金币悬赏"
        "（多人命中同一数值时每人各得一份）。"
        f"超级出价不限次数（每天最多 1 笔）、0~{game.max_bid}、不能等于当天正常出价、"
        f"当天正常出价加固定成本 {game.super_cost} 金币不得超过你的余额，"
        "超级出价数值不计入支付金额，不能预支悬赏、只狙击不能获胜；"
        "命中 = 其数值上至少有 1 笔正常出价。"
        "第 2 天起每天开始时公布昨日完整出价明细（含各人的超级出价数值）"
        "与全场每人的当前金币余额。"
    )


def bid_format_hint(game: GameSettings) -> str:
    """业务要求；输出格式由任务 schema 唯一声明。"""
    return (
        "请在 strategy 中保留对当前局势的简要分析及策略，提交今日出价。"
        f"正常出价与超级出价须为 0 到 {game.max_bid} 的整数；正常出价不得超过余额。"
        f"超级出价不得与正常出价同值，正常出价加 {game.super_cost} 金币成本不得超过余额。"
        "不使用超级出价时 super_bid 明确为 null。"
    )


def speech_format_hint() -> str:
    """保持私有策略与公开信号的业务边界。"""
    return (
        "请在 strategy 中保留私有发言策略，在 speech 中给出实际公开发言。只有 speech 会给其他玩家看。"
        "注意：公开发言只是对其他玩家释放的信号，不构成对你后续出价的任何约束。"
    )


def with_output_requirement(prompt: str, tool_name: str | None, *, task: str) -> str:
    """任务组装时追加一次；工具名由 agent_tasks 的映射传入。"""
    if tool_name is None:
        return prompt
    requirement = f"当前输出要求：调用 `{tool_name}` 工具提交；不要在正文模拟工具调用。"
    if task == "speech":
        requirement += "私有策略填 strategy，公开发言填 speech。"
    else:
        requirement += ('super_bid 必须出现：整数不加引号；不使用时填 JSON null，不能填字符串 "null" 或 "None"。'
                        '类型示例：{"super_bid": null}（仅示意字段类型，完整提交仍须包含全部必填字段）。')
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
        f"{render_rules(game, all_names)}\n"
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


def _day1_constant_info(game: GameSettings) -> str:
    """第 1 天信息由本场参数确定。"""
    return (
        f"（第 1 天信息恒定：你的金币 {game.initial_coins}，本局已获胜场 0，"
        f"今日奖池 {game.daily_pool[0]} 个胜场，出价上限 {game.max_bid}）"
    )


def opening_plan_request(others_speeches: dict[str, str], game: GameSettings) -> str:
    return (
        "以下是其他玩家的开局发言：\n"
        + "\n".join(f"【{name}】{text}\n" for name, text in others_speeches.items())
        + "\n请结合他们的发言，规划你第一局的初步策略，并直接给出第 1 局第 1 天的出价"
        + _day1_constant_info(game)
        + "。\n\n"
        + bid_format_hint(game)
    )


def _my_last_day_line(obs: Observation) -> Optional[str]:
    """「昨日你的结果」行：直接告知昨日自己的出价、结局与扣款。

    零信息泄露（自己的出价与余额变化本来就可推出），只是免去推导——实证模型
    会把自己的撞车推成「唯一未中退款」且不与状态行余额核对，错误随即级联。
    正常出价与超级出价的结局分段给出：正常段只含正常出价的扣款，
    超级段单独表述本场固定成本与命中悬赏。
    """
    if obs.my_last_bid is None:
        return None
    yesterday = obs.today_history[-1]
    is_void = yesterday["winner"] == "流局"
    won = yesterday["winner"] == obs.my_name

    if obs.my_last_bid_illegal:
        segments = ["提交非法，按规则整单记为正常出价 0"]
    else:
        segments = [f"正常出价 {obs.my_last_bid}"]
    if is_void:
        segments.append(f"当天流局（正常出价全部撞车），扣除 {obs.my_last_deduction}")
    elif won:
        segments.append(
            f"唯一最高出价获胜，赢得 {yesterday['wins_taken']} 个胜场，"
            f"扣除 {obs.my_last_deduction}"
        )
    elif obs.my_last_sniped:
        segments.append(f"被超级出价狙中，扣除 {obs.my_last_deduction}")
    elif obs.my_last_collided:
        segments.append(f"与他人撞车，扣除 {obs.my_last_deduction}")
    else:
        segments.append("未撞车但也不是最高，全额退还")

    if obs.my_last_super_bid is not None:
        # 超级出价的结局只有命中/落空两种，两种都扣本场固定成本
        if obs.my_last_super_hit:
            segments.append(
                f"超级出价 {obs.my_last_super_bid} 命中，"
                f"扣除 {obs.game.super_cost} 金币成本，获得 {obs.game.super_bounty} 金币悬赏"
            )
        else:
            segments.append(
                f"超级出价 {obs.my_last_super_bid} 落空，扣除 {obs.game.super_cost} 金币成本"
            )
    return "昨日你的结果：" + "，".join(segments) + "。"


def _bid_detail_text(detail: dict, super_hit_by_player: dict[str, bool]) -> str:
    """单日出价明细的一人份：「甲 4」或「甲 4+超6（命中）」。

    命中/落空可由明细与结算规则推出，直给标注省一步推导（零信息泄露）。
    """
    text = f"{detail['player']} {detail['bid']}"
    if detail["super_bid"] is not None:
        hit = super_hit_by_player.get(detail["player"], False)
        text += f"+超{detail['super_bid']}（{'命中' if hit else '落空'}）"
    return text


def bid_request(obs: Observation) -> str:
    match_index = len(obs.past_match_logs) + 1
    # 第 2 天起每天开始时已发放工资，余额是发放后的值，明示避免模型自己再加一次；
    # 可出最高价直接给数值，避免模型自己算 min（实证有「余额 7 打不了 6」的算术错误）
    status_notes = []
    if obs.day >= 2:
        status_notes.append("已含今日工资")
    status_notes.append(f"今日可出最高价：{min(obs.game.max_bid, obs.my_coins)}")
    status_line = (
        f"今日奖池：{obs.today_pool} 个胜场；"
        f"你的金币余额：{obs.my_coins}（{'，'.join(status_notes)}）；"
        f"本局已获胜场：{obs.my_match_wins}；你的累计总胜场：{obs.my_total_wins}。"
    )
    if not obs.today_history:
        # 首日：单行不换句
        return f"当前是第 {match_index} 局第 {obs.day} 天。{status_line}\n\n{bid_format_hint(obs.game)}"

    yesterday = obs.today_history[-1]
    if yesterday["winner"] == "流局":
        announcement = f"昨日公布：流局，奖池 {yesterday['pool']} 个胜场并入今日。"
    else:
        announcement = (
            f"昨日公布：{yesterday['winner']} 以 {yesterday['winning_bid']} 获胜，"
            f"赢得 {yesterday['pool']} 个胜场。"
        )
    # 规则四.1：昨日完整出价明细（含各人超级出价数值）+ 全场每人当前余额
    super_hit_by_player = {use["player"]: use["hit"] for use in yesterday["super_uses"]}
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
    """整局出价日志渲染为 markdown 表格：玩家列顺序取首日正常出价顺序（即玩家名单顺序）。

    出价单元格含超级出价时写作「5+超6」，命中再加注「中」（如「4+超6中」）；
    超级出价不能获胜，结果列的获胜者必定凭正常出价。
    撞车价位列给具体数值（只含正常出价被扣款的价位）。
    """
    if not log.days:
        return "  （本局未进行任何拍卖）"
    player_names = [record.player for record in log.days[0].bids if not record.is_super]
    header = "| 天 | 奖池 | " + " | ".join(player_names) + " | 结果 | 撞车价位 |"
    separator = "|" + " --- |" * (len(player_names) + 4)
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
            if super_record is not None:
                cell += f"+超{super_record.bid}"
                if name in hit_by_player:
                    cell += "中"
            cells.append(cell)
        if day.is_void:
            outcome = "流局（奖池作废）" if day.day == log.game.max_days else "流局（奖池并入次日）"
        else:
            outcome = f"{day.winner} 以 {day.winning_bid} 获胜"
        collision_text = "、".join(str(value) for value in day.collision_values) or "—"
        rows.append(
            f"| {day.day} | {day.pool} | {' | '.join(cells)} | {outcome} | {collision_text} |"
        )
    wins_text = "、".join(f"{name} {w} 胜" for name, w in log.wins.items())
    lines = [header, separator, *rows, "", f"本局胜场：{wins_text}"]
    return "\n".join(lines)


def render_standings(standings: list[StandingsEntry]) -> str:
    """累计排名渲染为 markdown 表格：局数列随已结束场次动态增长。"""
    match_count = max((len(entry.wins_per_match) for entry in standings), default=0)
    match_headers = [f"第{i + 1}局" for i in range(match_count)]
    header = "| 排名 | 玩家 | 总胜场 |"
    if match_headers:
        header += " " + " | ".join(match_headers) + " |"
    separator = "|" + " --- |" * (3 + match_count)
    rows = []
    for entry in standings:
        row = f"| {entry.rank} | {entry.player} | {entry.total_wins} |"
        if match_count:
            cells = [str(w) for w in entry.wins_per_match]
            cells += [""] * (match_count - len(cells))
            row += " " + " | ".join(cells) + " |"
        rows.append(row)
    return "\n".join([header, separator, *rows])


def match_end_speech_request(log: MatchLog, standings: list[StandingsEntry], will_continue: bool) -> str:
    kind = "加赛局" if log.is_playoff else "正赛局"
    lines = [
        f"第 {log.match_index} 局（{kind}）结束。",
        # 局日志是模型复盘他人出价的唯一材料，复盘前先重申结算框架（实证错误
        # 多因复盘时不套用结算步骤，而非遗忘条款）
        rules_reminder(log.game),
        f"本局完整出价日志：\n{render_match_log(log)}",
        f"截至当前的累计排名与胜场明细：\n{render_standings(standings)}",
    ]
    # 只有正赛打完（或加赛后）第一名仍并列才有信息增量，其余局数无需预告
    if will_continue and log.match_index >= log.game.regular_matches:
        lines.append("第一名出现并列，按规则全员将进入加赛局，加赛局胜场计入总胜场。")
    lines.append("请向全体玩家发表你的局末发言（不超过 300 字）。")
    lines.append(speech_format_hint())
    return "\n\n".join(lines)


def next_match_plan_request(others_speeches: dict[str, str], game: GameSettings) -> str:
    return (
        "以下是其他玩家的局末发言：\n"
        + "\n".join(f"【{name}】{text}\n" for name, text in others_speeches.items())
        + "\n请结合他们的发言，规划你下一局的初步策略，并直接给出下一局第 1 天的出价"
        + _day1_constant_info(game)
        + "。\n\n"
        + bid_format_hint(game)
    )


def final_speech_request(standings: list[StandingsEntry], log: MatchLog) -> str:
    """终局感言请求：并入最后一局的完整出价日志（该局已跳过局末发言）。"""
    kind = "加赛局" if log.is_playoff else "正赛局"
    return (
        f"第 {log.match_index} 局（{kind}）结束，这也是最后一局。本局完整出价日志：\n"
        f"{render_match_log(log)}\n\n"
        f"比赛全部结束。最终排名与胜场明细：\n{render_standings(standings)}\n\n"
        "请向全体玩家发表你的最终感言。\n\n"
        + speech_format_hint()
    )


def final_summary_request(others_speeches: dict[str, str]) -> str:
    return (
        "以下是其他玩家的最终感言：\n"
        + "\n".join(f"【{name}】{text}\n" for name, text in others_speeches.items())
        + "\n最后，请写一段只给观众看的总结发言（不会给其他玩家看到）。"
    )
