"""所有发给 AI 的消息模板 + 局日志/排名的可读渲染（AI 消息与终端输出共用）。"""

from __future__ import annotations

from models import (
    DAILY_POOL,
    INITIAL_COINS,
    MAX_BID,
    MAX_DAYS,
    MatchLog,
    Observation,
    StandingsEntry,
)

RULES_TEXT = """\
【唯一高价拍卖游戏规则】

一、赛制
1. 6 名 AI 玩家参赛，个人赛，各自独立争取排名。
2. 共 5 局正赛，每局固定 5 天，共拍卖 8 个胜场。每天奖池：第 1 天 1 个、第 2 天 1 个、
   第 3 天 2 个、第 4 天 2 个、第 5 天 2 个。流局当天的奖池并入次日；
   第 5 天未拍出的胜场作废。

二、金币与出价
1. 每局开始时每人 6 枚金币；从第 2 天起，每天开始时每人发放 3 枚金币（工资）。
   金币局内累积，跨局不继承，局末作废。
2. 每天所有玩家同时提交秘密出价。出价为 0 到 6 的整数，且不得超过当前余额。
3. 非法出价（超余额、超范围、非整数）与未提交一律视为出价 0。

三、每日结算（按顺序执行四步）
1. 判定撞车：若某个出价数值被两名及以上玩家出了，该数值即为撞车价位，
   出了该数值的玩家全部视为撞车。只出现一次的数值为唯一出价。
2. 判定胜负：所有唯一出价中最高的一个获胜。0 是合法价位：若其他价位全部撞车，
   唯一的 0 出价也能获胜。若当天没有任何唯一出价，则流局：无人获胜，奖池并入次日。
3. 冲顶税：若获胜价为 6，获胜者获得的胜场数为当天奖池数减 1（最低为 0），
   被扣除的 1 个胜场作废。以 0~5 获胜则获得奖池全部胜场。
4. 金币结算：获胜者扣除其出价；所有撞车的玩家扣除各自出价（流局日同样扣除）；
   唯一但未获胜的玩家全额退还出价。被扣除的金币由系统没收。

   结算示例：六人出价为 6、5、5、3、2、0。两个 5 撞车，各扣 5；6 是最高唯一出价，
   获胜并扣 6，当天奖池为 2，缴冲顶税后获得 1 个胜场；
   出 3、2、0 者唯一但未获胜，全额退还。

四、信息公布
1. 每天结束公布：获胜者身份与获胜价（或流局），以及当天撞车价位的个数。
   不公布任何玩家的具体出价与余额。
2. 每局结束后，公布该局完整出价日志，以及截至当前的累计排名。

五、发言
开赛前与每局结束后各一次公开发言，同时提交同时公开，无规则约束力，其余时间禁止通信。

六、排名与加赛
1. 正赛 5 局结束后比较总胜场数。第一名唯一则直接确定最终排名。
2. 第一名有并列则全员进入加赛局（规则与正赛局完全相同），加赛局胜场计入总胜场，
   每局加赛后重新排名，第一名仍并列（包括并列者变化）则继续加赛，直到第一名唯一。
3. 第一名以外的并列保持并列，不再区分。
"""


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


def bid_request(obs: Observation) -> str:
    match_index = len(obs.past_match_logs) + 1
    # 第 2 天起每天开始时已发放工资，余额是发放后的值，明示避免模型自己再加一次
    salary_note = "（已含今日工资）" if obs.day >= 2 else ""
    status_line = (
        f"今日奖池：{obs.today_pool} 个胜场；"
        f"你的金币余额：{obs.my_coins}{salary_note}；本局已获胜场：{obs.my_match_wins}；"
        f"你的累计总胜场：{obs.my_total_wins}。"
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
            f"撞车价位 {yesterday['collision_price_count']} 个。"
        )
    else:
        announcement = (
            f"昨日公布：{yesterday['winner']} 以 {yesterday['winning_bid']} 获胜，"
            f"赢得 {yesterday['pool']} 个胜场，"
            f"撞车价位 {yesterday['collision_price_count']} 个。"
        )
    lines = [f"当前是第 {match_index} 局第 {obs.day} 天。", announcement, status_line]
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
                outcome += f"（冲顶税：{day.pool}→{day.wins_taken}）"
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
        f"第 {log.match_index} 局（{kind}）结束。本局完整出价日志：\n{render_match_log(log)}",
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
