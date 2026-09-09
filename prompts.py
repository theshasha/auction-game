"""所有发给 AI 的消息模板 + 局日志/排名的可读渲染（AI 消息与终端输出共用）。

规则全文单一来源是 RULES.md：本模块加载时读入为 RULES_TEXT 注入 system 提示词，
改规则只需改 RULES.md（进程重启后生效），不再在此维护副本。
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from models import (
    DAILY_POOL,
    INITIAL_COINS,
    MAX_BID,
    MAX_DAYS,
    SUPER_BOUNTY,
    SUPER_COST,
    MatchLog,
    Observation,
    StandingsEntry,
)

RULES_TEXT = (Path(__file__).parent / "RULES.md").read_text(encoding="utf-8").strip()

# 局末展示出价日志前的精简规则重申：结算三步是模型复盘局日志时的框架，
# 实证错误（如「自己撞车推成退款」）都源于复盘时没套用这三步。
# 只放局末发言请求（局日志唯一展示处），不重发全文。
RULES_REMINDER = (
    "【规则要点重申】每日结算按三步：① 某数值出现两笔及以上正常出价即撞车；"
    "此外，被超级出价命中的数值（其上至少有 1 笔正常出价、且有超级出价指向它），"
    "该数值上的所有正常出价也全部撞车——撞车的正常出价全部扣除且不能获胜；"
    "② 未撞车的正常出价中最高的一笔获胜、其主人获得当天全部奖池"
    "（0 是合法价位），当天正常出价全部撞车则流局、奖池并入次日；"
    "③ 获胜与撞车的正常出价全额扣除，其余正常出价全额退还；"
    f"超级出价每次提交固定扣除 {SUPER_COST} 金币（无论是否命中），"
    f"命中的狙击者获得系统印发的 {SUPER_BOUNTY} 金币悬赏"
    "（多人命中同一数值时每人各得一份）。"
    f"超级出价不限次数（每天最多 1 笔）、0~{MAX_BID}、不能等于当天正常出价、"
    "与当天正常出价合计不得超过你的余额、只狙击不能获胜；"
    "命中 = 其数值上至少有 1 笔正常出价。"
    "第 2 天起每天开始时公布昨日完整出价明细（含各人的超级出价数值）"
    "与全场每人的当前金币余额。"
    "友情提示：由于3-7都能直接赢，出0-2的胜率不足30%，天天出低价无法靠捡漏提高胜率。"
    "友情提示：狙中只能赚1金币。狙7只会亏钱，因为出高价的人大概率已经狙7了，于是几乎无人出7。在出低价的时候不狙中间价而狙高价（如7）几乎无法提高胜率，因为出中间价的人大概率已经狙高价了。"
)


def bid_format_hint() -> str:
    """出价格式提示：示例保持无围栏裸 JSON（实证围栏示例会诱导模型包代码块）。

    super_bid 为可选字段：不使用就不要包含该字段；使用时两笔出价放在一个
    JSON 对象里一次提交。规则细节（命中判定、悬赏、不能获胜等）不在此每日
    复述——规则有 system 的 RULES.md 与局末重申兜底，每日复述实证是诱导弱
    模型跳读的冗余（20260829_230755 场次：超级规则一天出现 3 遍约 155 字符，
    误解仍集中在不读上下文的模型身上，复述并未起到防误解作用）。
    例外是提交合法性约束（同值、合计余额——v6 违反即整单作废）：
    这不是策略规则，放在模板里防止无谓的非法提交。
    """
    return (
        "请先回复你对当前局势的简要分析，然后在回复末尾用如下 JSON 给出今日出价：\n"
        f'{{"bid": <0到{MAX_BID}的整数，不得超过你的余额>}}\n'
        f"若要同时使用超级出价（不得与正常出价同值，"
        f"且正常出价加 {SUPER_COST} 金币成本不得超过你的余额），格式为：\n"
        '{"bid": <正常出价>, "super_bid": <超级出价>}\n'
        "不使用超级出价就不要包含 super_bid 字段。"
    )


def speech_format_hint() -> str:
    """发言格式提示：先规划发言策略，末尾用定界符给出实际公开发言。

    定界符格式（【公开发言】…【发言结束】）取代裸 JSON：发言是自由文本，
    JSON 字符串要求转义双引号，实证模型在发言内嵌未转义的 " 导致解析失败；
    定界符之间是纯文本，引号与换行均不受限。
    免责句把「发言」与「后续出价」显式解耦：避免模型把公开发言当成自己
    接下来要做的事（说出去的话泼出去的水效应）。
    """
    return (
        "请先给出你的发言策略，"
        "然后在回复末尾按如下格式给出你实际要公开的发言：\n"
        "【公开发言】\n"
        "<你实际要公开的发言，可自由使用引号与换行>\n"
        "【发言结束】\n"
        "注意：公开发言只是对其他玩家释放的信号，不构成对你后续出价的任何约束。"
    )


def retry_speech_request() -> str:
    return (
        "你的上一条回复缺少合法的发言块（【公开发言】…【发言结束】），请重新发言。\n\n"
        + speech_format_hint()
    )


def build_system_prompt(
    name: str,
    persona_title: str,
    persona_text: str,
    predictions: list[tuple[str, int]],
    all_names: list[str],
) -> str:
    """system 提示词：规则 + 身份 + 人设 + 全员公开的赛前预测。

    人设只约束公开发言的口吻（开局/局末/终局感言），出价推理与策略规划
    明确要求保持冷静认真——实证三无/天然呆类人设若带入推理会拉低决策质量。
    predictions 是虚构数字（config 静态配置），与真实表现无关。
    """
    others = "、".join(n for n in all_names if n != name)
    prediction_text = "、".join(f"{player_name} {rate}%" for player_name, rate in predictions)
    return (
        f"{RULES_TEXT}\n"
        f"【你的身份】\n你叫 {name}，其他五位玩家是：{others}。\n"
        f"【你的人设】\n{persona_title}：{persona_text}\n"
        f"【赛前预测】\n专家根据各玩家之前的对局表现，给出的夺冠率预测为：{prediction_text}。\n"
        "请在游戏过程中认真思考，不要被人设的说话方式影响你的思考。"
    )


def opening_speech_request() -> str:
    return (
        "比赛即将开始。请向全体玩家发表你的开局发言（不超过 300 字）。\n\n"
        + speech_format_hint()
    )


def _day1_constant_info() -> str:
    """第 1 天信息恒定：初始金币、0 胜场、当天奖池与固定上限都是常量。"""
    return (
        f"（第 1 天信息恒定：你的金币 {INITIAL_COINS}，本局已获胜场 0，"
        f"今日奖池 {DAILY_POOL[0]} 个胜场，出价上限 {MAX_BID}）"
    )


def opening_plan_request(others_speeches: dict[str, str]) -> str:
    return (
        "以下是其他玩家的开局发言：\n"
        + "\n".join(f"【{name}】{text}\n" for name, text in others_speeches.items())
        + "\n请结合他们的发言，规划你第一局的初步策略，并直接给出第 1 局第 1 天的出价"
        + _day1_constant_info()
        + "。\n\n"
        + bid_format_hint()
    )


def _my_last_day_line(obs: Observation) -> Optional[str]:
    """「昨日你的结果」行：直接告知昨日自己的出价、结局与扣款。

    零信息泄露（自己的出价与余额变化本来就可推出），只是免去推导——实证模型
    会把自己的撞车推成「唯一未中退款」且不与状态行余额核对，错误随即级联。
    正常出价与超级出价的结局分段给出：正常段只含正常出价的扣款，
    超级段单独表述固定成本与命中悬赏（v6：超级出价每次提交固定扣 SUPER_COST）。
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
        # 超级出价的结局只有命中/落空两种，固定成本 SUPER_COST 两种都扣
        if obs.my_last_super_hit:
            segments.append(
                f"超级出价 {obs.my_last_super_bid} 命中，"
                f"扣除 {SUPER_COST} 金币成本，获得 {SUPER_BOUNTY} 金币悬赏"
            )
        else:
            segments.append(
                f"超级出价 {obs.my_last_super_bid} 落空，扣除 {SUPER_COST} 金币成本"
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
    status_notes.append(f"今日可出最高价：{min(MAX_BID, obs.my_coins)}")
    status_line = (
        f"今日奖池：{obs.today_pool} 个胜场；"
        f"你的金币余额：{obs.my_coins}（{'，'.join(status_notes)}）；"
        f"本局已获胜场：{obs.my_match_wins}；你的累计总胜场：{obs.my_total_wins}。"
    )
    if not obs.today_history:
        # 首日：单行不换句
        return f"当前是第 {match_index} 局第 {obs.day} 天。{status_line}\n\n{bid_format_hint()}"

    yesterday = obs.today_history[-1]
    if yesterday["winner"] == "流局":
        announcement = f"昨日公布：流局，奖池 {yesterday['pool']} 个胜场并入今日。"
    else:
        announcement = (
            f"昨日公布：{yesterday['winner']} 以 {yesterday['winning_bid']} 获胜，"
            f"赢得 {yesterday['pool']} 个胜场。"
        )
    # 规则四.1（v6）：昨日完整出价明细（含各人超级出价数值）+ 全场每人当前余额
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
    return "\n".join(lines) + "\n\n" + bid_format_hint()


def retry_bid_request() -> str:
    return (
        "你的上一条回复缺少合法的 JSON 出价块，请重新出价。\n\n" + bid_format_hint()
    )


def invalid_bid_retry_request(reason: str) -> str:
    """数值非法（解析成功但违反出价规则）的提醒重试：写清具体违规原因。"""
    return f"你的上一提交非法：{reason}。请重新出价。\n\n" + bid_format_hint()


def render_match_log(log: MatchLog) -> str:
    """整局出价日志渲染为 markdown 表格：玩家列顺序取首日正常出价顺序（即 config 顺序）。

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
            outcome = "流局（奖池作废）" if day.day == MAX_DAYS else "流局（奖池并入次日）"
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
        RULES_REMINDER,
        f"本局完整出价日志：\n{render_match_log(log)}",
        f"截至当前的累计排名与胜场明细：\n{render_standings(standings)}",
    ]
    # 只有正赛打完（或加赛后）第一名仍并列才有信息增量，其余局数无需预告
    if will_continue and log.match_index >= 5:
        lines.append("第一名出现并列，按规则全员将进入加赛局，加赛局胜场计入总胜场。")
    lines.append("请向全体玩家发表你的局末发言（不超过 300 字）。")
    lines.append(speech_format_hint())
    return "\n\n".join(lines)


def next_match_plan_request(others_speeches: dict[str, str]) -> str:
    return (
        "以下是其他玩家的局末发言：\n"
        + "\n".join(f"【{name}】{text}\n" for name, text in others_speeches.items())
        + "\n请结合他们的发言，规划你下一局的初步策略，并直接给出下一局第 1 天的出价"
        + _day1_constant_info()
        + "。\n\n"
        + bid_format_hint()
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
