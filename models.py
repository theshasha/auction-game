"""数据结构定义：规则引擎与玩家之间流转的所有对象。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

INITIAL_COINS = 6              # 每局初始金币
DAILY_SALARY = 3               # 第 2 天起每天开始时每人发放的工资
MAX_DAYS = 6                   # 每局固定天数（流局不顺延天数，只把奖池并入次日）
MAX_BID = 6                    # 出价上限（固定；超级出价同上限但不查余额）
DAILY_POOL = (1, 1, 1, 1, 1, 1)  # 每天奖池的胜场数（固定 1，流局并入次日）
WINS_PER_MATCH = sum(DAILY_POOL)  # 每局拍卖的胜场总数（6）


@dataclass
class BidRecord:
    """一笔出价记录（对局内保密，局末公开）。一名玩家一天最多产生两笔：
    正常出价 + 超级出价（is_super=True，不消耗金币、不受余额限制）。"""

    player: str
    raw_bid: Any          # 玩家提交的原始值，用于审计非法出价
    bid: int              # 校验后的有效出价（正常出价非法一律记 0）
    status: str = "ok"    # "ok" | "illegal"（超级出价只可能为 "ok"，非法则在收集层拦截重试）
    is_super: bool = False  # 超级出价：参与撞车判定但不能获胜，任何结局都不扣金币


@dataclass
class DayResult:
    """一天的完整结算结果。public 视图 = 系统每日公布的信息。"""

    day: int
    bids: list[BidRecord]
    pool: int                    # 当天奖池的胜场数（含流局并入）
    winner: Optional[str]        # None 表示流局
    winning_bid: Optional[int]
    wins_taken: int              # 获胜者实际获得的胜场数（恒等于 pool；流局为 0）
    super_users: list[str] = field(default_factory=list)  # 当天使用超级出价的玩家（每日公布）
    collision_price_count: int = 0   # 当天撞车价位的个数（全部撞车价位）
    is_void: bool = False            # 是否流局（奖池并入次日；第 6 天流局则作废）

    def public_view(self) -> dict:
        """规则五.1：每天结束时系统公布的信息（获胜者身份与获胜价 + 撞车价位个数
        + 超级出价使用情况——只公布谁用了，不公布数值）。"""
        return {
            "day": self.day,
            "winner": self.winner if not self.is_void else "流局",
            "winning_bid": self.winning_bid,
            "pool": self.pool,
            "wins_taken": self.wins_taken,
            "super_users": list(self.super_users),
            "collision_price_count": self.collision_price_count,
        }


@dataclass
class MatchLog:
    """一局的完整日志，局末向全体参赛者公开。"""

    match_index: int             # 第几局（1 起；正赛 1-5，加赛 6 起）
    is_playoff: bool
    days: list[DayResult] = field(default_factory=list)
    wins: dict[str, int] = field(default_factory=dict)   # 本局各人获得的胜场

    def to_dict(self) -> dict:
        return {
            "match_index": self.match_index,
            "is_playoff": self.is_playoff,
            "wins": dict(self.wins),
            "days": [
                {
                    "day": day.day,
                    "bids": [
                        {
                            "player": b.player,
                            "raw_bid": b.raw_bid,
                            "bid": b.bid,
                            "status": b.status,
                            "is_super": b.is_super,
                        }
                        for b in day.bids
                    ],
                    "pool": day.pool,
                    "winner": day.winner,
                    "winning_bid": day.winning_bid,
                    "wins_taken": day.wins_taken,
                    "super_users": list(day.super_users),
                    "collision_price_count": day.collision_price_count,
                    "is_void": day.is_void,
                }
                for day in self.days
            ],
        }


@dataclass
class StandingsEntry:
    """累计排名条目：总胜场 + 各局胜场分布。"""

    player: str
    total_wins: int
    wins_per_match: list[int]    # 第 i 元素 = 第 i+1 局拿到的胜场数
    rank: int = 0


@dataclass
class Observation:
    """喂给玩家的决策依据，严格等于规则允许其知道的信息。"""

    my_name: str
    my_coins: int
    my_match_wins: int           # 本局已获胜场
    my_total_wins: int           # 含本局在内的累计总胜场
    day: int                     # 当前第几天（1 起）
    today_pool: int              # 当天奖池的胜场数（含流局并入）
    today_history: list[dict]    # 本局已过天数的每日公布信息（DayResult.public_view）
    standings: list[StandingsEntry]   # 截至当前的累计排名（含本局已决出的胜场）
    past_match_logs: list[dict]  # 已结束各局的完整公开日志
    # 超级出价状态：由引擎按本局已落日志权威计算（不用玩家自己记，暂停回滚
    # 丢弃结果时状态天然一致）。available=False 时 used_day 为使用日。
    my_super_available: bool = True
    my_super_used_day: Optional[int] = None
    # 昨日自己的结算结果（第 1 天为 None/默认值）。自己的出价与扣款本来就可从
    # 对话历史与余额变化推出，直接给结论不泄露任何信息——实证模型复盘自己的
    # 结局时经常推错且不与状态行余额核对，错误随后级联到全场资金推断。
    my_last_bid: Optional[int] = None      # 昨日自己的有效正常出价（非法出价已记 0）
    my_last_deduction: Optional[int] = None  # 昨日自己被扣除的金币（退还为 0；超级出价永不计入）
    my_last_collided: bool = False         # 昨日自己的正常出价是否撞车
    my_last_bid_illegal: bool = False      # 昨日正常出价是否非法（被规则记 0）
    my_last_super_bid: Optional[int] = None  # 昨日自己的超级出价（None = 昨日未使用）
    my_last_super_collided: bool = False     # 昨日自己的超级出价是否撞车（公开信息推不出，引擎直接给）
    # 全场平均金币余额（含自己、含当日工资，保留 1 位小数）：规则五.1 规定
    # 第 2 天起每天开始时公布；第 1 天为 None（恒定 6.0 无信息量，不渲染）。
    avg_coins: Optional[float] = None
