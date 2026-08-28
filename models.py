"""数据结构定义：规则引擎与玩家之间流转的所有对象。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

INITIAL_COINS = 6              # 每局初始金币
DAILY_SALARY = 3               # 第 2 天起每天开始时每人发放的工资
MAX_DAYS = 5                   # 每局固定天数（流局不顺延天数，只把奖池并入次日）
MAX_BID = 6                    # 出价上限（固定）
DAILY_POOL = (1, 2, 2, 2, 2)   # 每天奖池的胜场数（后载：第 5 天永远有货，不用提前结束）
WINS_PER_MATCH = sum(DAILY_POOL)  # 每局拍卖的胜场总数（9）


@dataclass
class BidRecord:
    """一名玩家某一天的出价记录（对局内保密，局末公开）。"""

    player: str
    raw_bid: Any          # 玩家提交的原始值，用于审计非法出价
    bid: int              # 校验后的有效出价（非法一律记 0）
    status: str = "ok"    # "ok" | "illegal"


@dataclass
class DayResult:
    """一天的完整结算结果。public 视图 = 系统每日公布的信息。"""

    day: int
    bids: list[BidRecord]
    pool: int                    # 当天奖池的胜场数（含流局并入）
    winner: Optional[str]        # None 表示流局
    winning_bid: Optional[int]
    wins_taken: int              # 获胜者实际获得的胜场数（冲顶税后；流局为 0）
    transfer_recipient: Optional[str] = None  # 冲顶税转判获得者（次高唯一出价者；无则 None）
    transfer_wins: int = 0       # 转判获得的胜场数（无转判对象时该部分作废，此字段为 0）
    collision_price_count: int = 0   # 当天撞车价位的个数（全部撞车价位）
    is_void: bool = False            # 是否流局（奖池并入次日；第 5 天流局则作废）

    def public_view(self) -> dict:
        """规则四.1：每天结束时系统公布的信息（获胜者身份与获胜价 + 撞车价位个数 + 转判获得者）。"""
        return {
            "day": self.day,
            "winner": self.winner if not self.is_void else "流局",
            "winning_bid": self.winning_bid,
            "pool": self.pool,
            "wins_taken": self.wins_taken,
            "transfer_recipient": self.transfer_recipient,
            "transfer_wins": self.transfer_wins,
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
                        {"player": b.player, "raw_bid": b.raw_bid, "bid": b.bid, "status": b.status}
                        for b in day.bids
                    ],
                    "pool": day.pool,
                    "winner": day.winner,
                    "winning_bid": day.winning_bid,
                    "wins_taken": day.wins_taken,
                    "transfer_recipient": day.transfer_recipient,
                    "transfer_wins": day.transfer_wins,
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
