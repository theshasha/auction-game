"""数据结构定义：规则引擎与玩家之间流转的所有对象。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

INITIAL_COINS = 6      # 每局初始金币
MAX_DAYS = 5           # 每局最多天数（含顺延）
WINS_PER_MATCH = 8     # 每局拍卖的胜场数
WINS_PER_DAY = 2       # 每天打包拍卖的胜场数（8 ÷ 2 整除，不产生零头）
# 每日出价上限按天降档：第 1~2 天 6、第 3~4 天 5、第 5 天 4；流局日照样降档。
# 动机：拆掉永恒极点（固定上限下最高价位零风险必胜），并让苟着攒钱付出时间成本——
# 攒到后期高价已过期，金币只能低价花出。
BID_LIMITS = (6, 6, 5, 5, 4)
DIVIDEND_DIVISOR = 6   # 分红除数，固定为 6（等于玩家数，全员极低价苟活时总消耗不足 6、无分红）
COLLISION_PENALTY = 1  # 高位撞车每次罚没的累计胜场数


def bid_limit(day: int) -> int:
    """第 day 天（1 起）的出价上限：第 1~2 天 6、第 3~4 天 5、第 5 天 4。"""
    return BID_LIMITS[day - 1]


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
    winner: Optional[str]        # None 表示流局
    winning_bid: Optional[int]
    voided_price_count: int      # 作废价位数量
    dividend: int                # 当天每人分红，流局为 0
    is_void: bool                # 是否流局
    penalties: list[tuple[str, int]] = field(default_factory=list)  # 当天胜场罚没 [(玩家, 罚没数)]

    def public_view(self) -> dict:
        """规则五.1：每天结束时系统公布的信息。"""
        return {
            "day": self.day,
            "winner": self.winner if not self.is_void else "流局",
            "voided_price_count": self.voided_price_count,
            "dividend": self.dividend,
            "penalties": [
                {"player": player, "wins": wins} for player, wins in self.penalties
            ],
        }


@dataclass
class MatchLog:
    """一局的完整日志，局末向全体参赛者公开。"""

    match_index: int             # 第几局（1 起；正赛 1-5，加赛 6 起）
    is_playoff: bool
    days: list[DayResult] = field(default_factory=list)
    wins: dict[str, int] = field(default_factory=dict)   # 本局各人获得的胜场
    end_reason: str = ""         # "wins_exhausted" | "max_days" | "all_coins_zero"

    def to_dict(self) -> dict:
        return {
            "match_index": self.match_index,
            "is_playoff": self.is_playoff,
            "end_reason": self.end_reason,
            "wins": dict(self.wins),
            "days": [
                {
                    "day": day.day,
                    "bids": [
                        {"player": b.player, "raw_bid": b.raw_bid, "bid": b.bid, "status": b.status}
                        for b in day.bids
                    ],
                    "winner": day.winner,
                    "winning_bid": day.winning_bid,
                    "voided_price_count": day.voided_price_count,
                    "dividend": day.dividend,
                    "is_void": day.is_void,
                    "penalties": [
                        {"player": player, "wins": wins} for player, wins in day.penalties
                    ],
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
    my_total_wins: int           # 含本局在内的总胜场（已扣除罚没）
    day: int                     # 当前第几天（1 起）
    bid_cap: int                 # 当天出价上限（随天数降档）
    wins_remaining: int          # 本局剩余待拍胜场
    max_days: int                # 每局天数上限
    today_history: list[dict]    # 本局已过天数的每日公布信息（DayResult.public_view）
    standings: list[StandingsEntry]   # 截至当前的累计排名（含本局已决出的胜场）
    past_match_logs: list[dict]  # 已结束各局的完整公开日志
