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
    MatchLog,
    Observation,
    StandingsEntry,
)

RULES_TEXT = (Path(__file__).parent / "RULES.md").read_text(encoding="utf-8").strip()

# 局末展示出价日志前的精简规则重申：结算四步是模型复盘局日志时的框架，
# 实证错误（如「转判获得者免费得胜场」「自己撞车推成退款」）都源于复盘时
# 没套用这四步。只放局末发言请求（局日志唯一展示处），不重发全文。
RULES_REMINDER = (
    "【规则要点重申】每日结算按四步：① 某数值被两名及以上玩家出了即撞车，撞车者全部扣除出价；"
    "② 唯一出价中最高者获胜（0 是合法价位），当天无唯一出价则流局、奖池并入次日；"
    "③ 获胜价为 6 时，获胜者只得奖池一半（向上取整），另一半（向下取整）转判次高唯一出价者"
    "——转判获得者照常扣除自己的出价，并非免费获赠；无次高唯一出价者该部分作废；"
    "④ 获胜者、转判获得者与撞车者扣除各自出价（流局日撞车照扣），其余玩家全额退还。"
    "他人的出价与余额不公布，只能由每日公布信息推断，推断可能出错。"
)


def bid_format_hint() -> str:
    """出价格式提示：示例保持无围栏裸 JSON（实证围栏示例会诱导模型包代码块）。"""
    return (
        "请先分析当前局势，然后在回复末尾用如下 JSON 给出今日出价：\n"
        f'{{"bid": <0到{MAX_BID}的整数，不得超过你的余额>}}'
    )


def speech_format_hint() -> str:
    """发言格式提示：先规划发言策略，末尾裸 JSON 给出实际公开发言（示例无围栏，同出价）。

    免责句把「发言」与「后续出价」显式解耦：避免模型把公开发言当成自己
    接下来要做的事（说出去的话泼出去的水效应）。
    """
    return (
        "请先规划你的发言策略（想对其他玩家释放什么信号、是否放烟雾弹），"
        "然后在回复末尾用如下 JSON 给出你实际要公开的发言：\n"
        '{"speech": "<你实际要公开的发言>"}\n'
        "注意：公开发言只是对其他玩家释放的信号，不构成对你后续出价的任何约束。"
    )


def retry_speech_request() -> str:
    return "你的上一条回复缺少合法的 JSON 发言块，请重新发言。\n\n" + speech_format_hint()


def build_system_prompt(name: str, persona: str, all_names: list[str]) -> str:
    others = "、".join(n for n in all_names if n != name)
    return (
        f"{RULES_TEXT}\n"
        f"【你的身份】\n你叫 {name}，其他五位玩家是：{others}。\n"
        f"【你的人设】\n{persona}\n"
        "请始终以这个人设的口吻思考、发言和决策。"
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
    """
    if obs.my_last_bid is None:
        return None
    yesterday = obs.today_history[-1]
    if obs.my_last_bid_illegal:
        segments = ["出价非法按规则记 0"]
    else:
        segments = [f"出价 {obs.my_last_bid}"]
    if yesterday["winner"] == "流局":
        segments.append(f"当天流局全员撞车，扣除 {obs.my_last_deduction}")
    elif yesterday["winner"] == obs.my_name:
        if yesterday["winning_bid"] == MAX_BID:
            segments.append(
                f"唯一最高出价获胜，缴冲顶税后实得 {yesterday['wins_taken']} 个胜场，"
                f"扣除 {obs.my_last_deduction}"
            )
        else:
            segments.append(
                f"唯一最高出价获胜，赢得 {yesterday['wins_taken']} 个胜场，"
                f"扣除 {obs.my_last_deduction}"
            )
    elif yesterday["transfer_recipient"] == obs.my_name:
        segments.append(
            f"获转判 {yesterday['transfer_wins']} 个胜场，照常扣除 {obs.my_last_deduction}"
        )
    elif obs.my_last_collided:
        segments.append(f"与他人撞车，扣除 {obs.my_last_deduction}")
    else:
        segments.append("是唯一出价但未获胜，全额退还")
    return "昨日你的结果：" + "，".join(segments) + "。"


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
        announcement = (
            f"昨日公布：流局，奖池 {yesterday['pool']} 个胜场并入今日，"
            f"撞车价位 {yesterday['collision_price_count']} 个。"
        )
    elif yesterday["winning_bid"] == MAX_BID:
        announcement = (
            f"昨日公布：{yesterday['winner']} 以 {yesterday['winning_bid']} 获胜，"
            f"奖池 {yesterday['pool']} 个胜场，缴冲顶税后实得 {yesterday['wins_taken']} 个，"
        )
        if yesterday["transfer_recipient"] is not None:
            announcement += (
                f"转判 {yesterday['transfer_recipient']} 获得 {yesterday['transfer_wins']} 个"
                f"（转判获得者照常扣除自己的出价），"
            )
        announcement += f"撞车价位 {yesterday['collision_price_count']} 个。"
    else:
        announcement = (
            f"昨日公布：{yesterday['winner']} 以 {yesterday['winning_bid']} 获胜，"
            f"赢得 {yesterday['pool']} 个胜场，"
            f"撞车价位 {yesterday['collision_price_count']} 个。"
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


def render_match_log(log: MatchLog) -> str:
    """整局出价日志渲染为 markdown 表格：玩家列顺序取首日出价顺序（即 config 顺序）。"""
    if not log.days:
        return "  （本局未进行任何拍卖）"
    player_names = [record.player for record in log.days[0].bids]
    header = "| 天 | 奖池 | " + " | ".join(player_names) + " | 结果 | 撞车价位 |"
    separator = "|" + " --- |" * (len(player_names) + 4)
    rows = []
    for day in log.days:
        bid_by_player = {record.player: record.bid for record in day.bids}
        bid_cells = " | ".join(str(bid_by_player[name]) for name in player_names)
        if day.is_void:
            outcome = "流局（奖池作废）" if day.day == MAX_DAYS else "流局（奖池并入次日）"
        else:
            outcome = f"{day.winner} 以 {day.winning_bid} 获胜"
            if day.winning_bid == MAX_BID:
                outcome += f"（冲顶税：{day.pool}→{day.wins_taken}"
                if day.transfer_recipient is not None:
                    outcome += f"，转判 {day.transfer_recipient} 得 {day.transfer_wins}，照扣其出价"
                outcome += "）"
        rows.append(
            f"| {day.day} | {day.pool} | {bid_cells} | {outcome} | {day.collision_price_count} |"
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
        # 多因复盘时不套用结算四步，而非遗忘条款）
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
        + "\n最后，请写一段只给观众看的总结发言（不会给其他玩家看到），"
        "回顾你整场比赛的表现与得失（纯文本）。"
    )
