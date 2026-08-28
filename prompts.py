"""所有发给 AI 的消息模板 + 局日志/排名的可读渲染（AI 消息与终端输出共用）。"""

from __future__ import annotations

from models import WINS_PER_MATCH, MatchLog, Observation, StandingsEntry, bid_limit

RULES_TEXT = """\
【唯一高价拍卖游戏规则】

一、赛制
1. 共 6 名 AI 玩家参赛，个人赛，各自独立争取排名。
2. 比赛共 5 局正赛。每局拍卖 8 个胜场，每天打包拍卖 2 个，因流局可顺延，每局最多 5 天。
   第 5 天结算后无论分出胜负还是流局，本局结束，未拍出的胜场作废。
3. 每天开局时若全员金币余额均为 0，本局立即结束，未拍出的胜场作废。

二、金币与出价
1. 每局开始时每位玩家获得 6 枚金币。金币在局内跨天累积，跨局不继承，局末作废。
2. 每天所有玩家同时提交秘密出价。出价为 0 到当天出价上限的整数，且不得超过当前余额。
   出价上限随天数降档：第 1~2 天为 6，第 3~4 天为 5，第 5 天为 4；
   流局顺延的日子同样计入降档。每局第 1 天的上限重新从 6 开始。
3. 非法出价（超余额、超上限、非整数、负数）一律视为出价 0。未提交视为出价 0。
4. 出价即消耗：正常结算当天无论输赢，出价立即被扣除。流局日全员出价全额返还。

三、胜负判定（最高唯一出价）
1. 当天出价最高、且该价位没有任何其他玩家出价的玩家获胜，一次获得当天打包的 2 个胜场。
2. 两人及以上出价相同的价位作废，系统向下寻找下一个唯一的最高出价。
3. 0 是合法价位：若出 0 以外的玩家均撞车，唯一出价 0 的玩家获胜。
4. 若所有出价均撞车、没有任何唯一出价，当天流局：不产生胜场、出价全额返还、无分红。
5. 获胜者不退场，以剩余金币继续参与后续天数的竞拍。
6. 胜场罚没判定（每天结算时执行）：
   第一步（检查范围）：正常分出胜负的当天，只检查高于获胜价的撞车价位，
     低于或等于获胜价的撞车一律不检查；流局的当天，检查全部撞车价位。
   第二步（比较口径）：在每个被检查的撞车价位上，比较该价位所有玩家的
     "累计总胜场"（跨全部已结束局 + 本局已获胜场 − 已被罚没胜场，
     就是每天出价消息里"你的累计总胜场"那个数字；与本局胜场无关）。
   第三步（罚没）：若这些玩家的累计总胜场并非全都相同，则其中最多者
     各被罚没 1 个累计胜场（并列最多都罚；全相同则无人被罚）。
   记住：罚没只认"累计总胜场"，永不看"本局已获胜场"；
        正常日低于获胜价的撞车不会被罚。
7. 罚没只影响累计排名：当天的胜负归属、金币结算与本局拍卖流程均不受罚没影响。

四、每日分红
1. 每个正常分出胜负的当天结算后，当天全员出价总和平均分给所有在场玩家：
   总消耗除以 6 向下取整为每人分红，零头由系统没收。
2. 若当天结算后本局结束（拍出最后 2 个胜场或第 5 天结算），则不再分红。

五、信息公布
1. 每天结束只公布四项：获胜者身份（或流局）、作废价位数量、当天分红金额、
   当天胜场罚没情况（若有玩家被罚没，公布其身份与罚没数量）。
   作废价位数量：正常分出胜负的当天，只统计高于最终获胜价、且因两人及以上撞车而被
   作废的不同出价数值的个数（低于或等于获胜价的撞车一律不计入）；
   流局的当天，统计全部撞车价位的个数。
   系统不公布任何玩家的具体出价与剩余余额。
2. 每局结束后，公布该局完整出价日志，以及截至当前的累计排名与每人胜场明细。

六、发言
1. 开赛前与每局结束后，每位玩家各有一次公开发言机会（同时提交同时公开）。
2. 发言面向全体玩家公开，内容不产生任何规则上的约束力。除发言环节外禁止任何通信。

七、排名与加赛
1. 正赛 5 局结束后比较总胜场数。第一名无并列则直接确定最终排名。
2. 第一名有并列则全员进入加赛局（规则与正赛局完全相同），加赛局胜场计入总胜场，
   每局加赛后重新排名，第一名仍并列（包括并列者变化）则继续加赛，直到第一名唯一。
3. 第一名以外的并列保持并列，不再区分。
"""

def bid_format_hint(bid_cap: int) -> str:
    """出价格式提示：示例保持无围栏裸 JSON（实证围栏示例会诱导模型包代码块）。"""
    return (
        "请先分析当前局势，然后在回复末尾用如下 JSON 给出今日出价：\n"
        f'{{"bid": <0到{bid_cap}的整数，不得超过你的余额>}}'
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


def opening_plan_request(others_speeches: dict[str, str]) -> str:
    return (
        "以下是其他玩家的开局发言：\n"
        + "\n".join(f"【{name}】{text}\n" for name, text in others_speeches.items())
        + "\n请结合他们的发言，规划你第一局的初步策略，并直接给出第 1 局第 1 天的出价"
        f"（第 1 天信息恒定：你的金币 6，本局已获胜场 0，本局剩余待拍胜场 {WINS_PER_MATCH}，"
        f"出价上限 {bid_limit(1)}）。\n\n"
        + bid_format_hint(bid_limit(1))
    )


def bid_request(obs: Observation) -> str:
    match_index = len(obs.past_match_logs) + 1
    status_line = (
        f"今日出价上限：{obs.bid_cap}；"
        f"你的金币余额：{obs.my_coins}；本局已获胜场：{obs.my_match_wins}；"
        f"本局剩余待拍胜场：{obs.wins_remaining}；"
        f"你的累计总胜场：{obs.my_total_wins}（已含罚没）。"
        "罚没提示：若今日与他人撞车，被罚没的是同价者中\"累计总胜场\"最多的人"
        "（正常日仅检查高于获胜价的撞车价位，低于获胜价的撞车不罚）；"
        "每日公布的作废价位同样只统计高于获胜价的撞车。"
    )
    if not obs.today_history:
        # 首日：单行不换句
        return f"当前是第 {match_index} 局第 {obs.day} 天。{status_line}\n\n{bid_format_hint(obs.bid_cap)}"

    yesterday = obs.today_history[-1]
    if yesterday["winner"] == "流局":
        announcement = f"昨日公布：流局（无分红），作废价位 {yesterday['voided_price_count']} 个"
    else:
        announcement = (
            f"昨日公布：{yesterday['winner']} 获胜，作废价位 {yesterday['voided_price_count']} 个，"
            f"分红 {yesterday['dividend']} 金币"
        )
    penalties = yesterday.get("penalties") or []
    if penalties:
        details = "、".join(f"{p['player']} {p['wins']} 胜场" for p in penalties)
        announcement += f"，胜场罚没：{details}"
    announcement += "。"
    # 余额已含昨日分红，放在公布之后并明示，避免模型误再加一次分红
    status_line = status_line.replace(
        f"你的金币余额：{obs.my_coins}；", f"你的金币余额：{obs.my_coins}（已含昨日分红）；"
    )
    lines = [f"当前是第 {match_index} 局第 {obs.day} 天。", announcement, status_line]
    return "\n".join(lines) + "\n\n" + bid_format_hint(obs.bid_cap)


def retry_bid_request(bid_cap: int) -> str:
    return (
        "你的上一条回复缺少合法的 JSON 出价块，请重新出价。\n\n" + bid_format_hint(bid_cap)
    )


def render_match_log(log: MatchLog) -> str:
    """整局出价日志渲染为 markdown 表格：玩家列顺序取首日出价顺序（即 config 顺序）。"""
    if not log.days:
        return "  （本局未进行任何拍卖）"
    player_names = [record.player for record in log.days[0].bids]
    header = "| 天 | " + " | ".join(player_names) + " | 结果 | 作废价位 | 分红 |"
    separator = "|" + " --- |" * (len(player_names) + 4)
    rows = []
    for day in log.days:
        bid_by_player = {record.player: record.bid for record in day.bids}
        bid_cells = " | ".join(str(bid_by_player[name]) for name in player_names)
        if day.is_void:
            outcome = "流局（全额返还）"
        else:
            outcome = f"{day.winner} 以 {day.winning_bid} 获胜"
        if day.penalties:
            details = "、".join(f"{name} 被罚 {amount} 胜场" for name, amount in day.penalties)
            outcome += f"；{details}"
        rows.append(
            f"| {day.day} | {bid_cells} | {outcome} | {day.voided_price_count} | {day.dividend} |"
        )
    wins_text = "、".join(f"{name} {w} 胜" for name, w in log.wins.items())
    lines = [header, separator, *rows, "", f"本局胜场：{wins_text}"]
    penalty_totals: dict[str, int] = {}
    for day in log.days:
        for name, amount in day.penalties:
            penalty_totals[name] = penalty_totals.get(name, 0) + amount
    if penalty_totals:
        lines.append(
            "本局罚没胜场：" + "、".join(f"{name} {amount}" for name, amount in penalty_totals.items())
        )
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
        f"（第 1 天信息恒定：你的金币 6，本局已获胜场 0，本局剩余待拍胜场 {WINS_PER_MATCH}，"
        f"出价上限 {bid_limit(1)}）。\n\n"
        + bid_format_hint(bid_limit(1))
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
