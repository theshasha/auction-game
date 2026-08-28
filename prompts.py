"""所有发给 AI 的消息模板 + 局日志/排名的可读渲染（AI 消息与终端输出共用）。"""

from __future__ import annotations

from models import MatchLog, Observation, StandingsEntry

RULES_TEXT = """\
【唯一高价拍卖游戏规则】

一、赛制
1. 共 6 名 AI 玩家参赛，个人赛，各自独立争取排名。
2. 比赛共 5 局正赛。每局拍卖 3 个胜场，每天拍卖 1 个，因流局可顺延，每局最多 5 天。
   第 5 天结算后无论分出胜负还是流局，本局结束，未拍出的胜场作废。
3. 每天开局时若全员金币余额均为 0，本局立即结束，未拍出的胜场作废。

二、金币与出价
1. 每局开始时每位玩家获得 6 枚金币。金币在局内跨天累积，跨局不继承，局末作废。
2. 每天所有玩家同时提交秘密出价。出价为 0 到 6 的整数，且不得超过当前余额。
3. 非法出价（超余额、超上限、非整数、负数）一律视为出价 0。未提交视为出价 0。
4. 出价即消耗：正常结算当天无论输赢，出价立即被扣除。流局日全员出价全额返还。

三、胜负判定（最高唯一出价）
1. 当天出价最高、且该价位没有任何其他玩家出价的玩家获胜，获得 1 个胜场。
2. 两人及以上出价相同的价位作废，系统向下寻找下一个唯一的最高出价。
3. 0 是合法价位：若出 0 以外的玩家均撞车，唯一出价 0 的玩家获胜。
4. 若所有出价均撞车、没有任何唯一出价，当天流局：不产生胜场、出价全额返还、无分红。
5. 获胜者不退场，以剩余金币继续参与后续天数的竞拍。

四、每日分红
1. 每个正常分出胜负的当天结算后，当天全员出价总和平均分给所有在场玩家：
   总消耗除以 6 向下取整为每人分红，零头由系统没收。
2. 若当天结算后本局结束（拍出第 3 个胜场或第 5 天结算），则不再分红。

五、信息公布
1. 每天结束只公布三项：获胜者身份（或流局）、作废价位数量、当天分红金额。
   作废价位数量：正常分出胜负的当天，统计高于最终获胜价、且因两人及以上撞车而被
   作废的不同出价数值的个数（低于获胜价的撞车不计入）；流局的当天，统计全部撞车价位的个数。
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

BID_FORMAT_HINT = """\
请先分析当前局势，然后在回复末尾用如下 JSON 给出今日出价：
```json
{"bid": <0到6的整数，不得超过你的余额>}
```"""


def build_system_prompt(name: str, persona: str, all_names: list[str]) -> str:
    others = "、".join(n for n in all_names if n != name)
    return (
        f"{RULES_TEXT}\n"
        f"【你的身份】\n你叫 {name}，其他五位玩家是：{others}。\n"
        f"【你的人设】\n{persona}\n"
        "请始终以这个人设的口吻思考、发言和决策。"
    )


def opening_speech_request() -> str:
    return "比赛即将开始。请向全体玩家发表你的开局发言（纯文本，不超过 300 字）。"


def opening_plan_request(others_speeches: dict[str, str]) -> str:
    return (
        "以下是其他玩家的开局发言：\n"
        + "\n".join(f"【{name}】{text}\n" for name, text in others_speeches.items())
        + "\n请结合他们的发言，规划你第一局的初步策略。"
    )


def bid_request(obs: Observation) -> str:
    match_index = len(obs.past_match_logs) + 1
    lines = [
        f"第 {match_index} 局第 {obs.day} 天。你的金币余额：{obs.my_coins}；"
        f"本局已获胜场：{obs.my_match_wins}；本局剩余待拍胜场：{obs.wins_remaining}。"
    ]
    if obs.today_history:
        yesterday = obs.today_history[-1]
        lines.append(
            f"昨日公布：{yesterday['winner']}，作废价位 {yesterday['voided_price_count']} 个，"
            f"分红 {yesterday['dividend']} 金币。"
        )
    lines.append("")
    lines.append(BID_FORMAT_HINT)
    return "\n".join(lines)


def retry_bid_request() -> str:
    return (
        "你的上一条回复缺少合法的 JSON 出价块，请重新出价。\n\n" + BID_FORMAT_HINT
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
        rows.append(
            f"| {day.day} | {bid_cells} | {outcome} | {day.voided_price_count} | {day.dividend} |"
        )
    wins = "、".join(f"{name} {w} 胜" for name, w in log.wins.items())
    return "\n".join([header, separator, *rows, "", f"本局胜场：{wins}"])


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
    lines.append("请向全体玩家发表你的局末发言（纯文本，不超过 300 字）。")
    return "\n\n".join(lines)


def next_match_plan_request(others_speeches: dict[str, str]) -> str:
    return (
        "以下是其他玩家的局末发言：\n"
        + "\n".join(f"【{name}】{text}\n" for name, text in others_speeches.items())
        + "\n请结合他们的发言，规划你下一局的初步策略。"
    )


def final_speech_request(standings: list[StandingsEntry]) -> str:
    return (
        f"比赛全部结束。最终排名与胜场明细：\n{render_standings(standings)}\n\n"
        "请向全体玩家发表你的最终感言（纯文本）。"
    )


def final_summary_request(others_speeches: dict[str, str]) -> str:
    return (
        "以下是其他玩家的最终感言：\n"
        + "\n".join(f"【{name}】{text}\n" for name, text in others_speeches.items())
        + "\n最后，请写一段只给观众看的总结发言（不会给其他玩家看到），"
        "回顾你整场比赛的表现与得失（纯文本）。"
    )
