"""数据结构定义：规则引擎与玩家之间流转的所有对象。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from auction_game.game.game_rules import GameSettings


@dataclass
class BidRecord:
    """一笔出价记录（次日即公开，局末再随完整日志公开）。一名玩家一天最多产生两笔：
    正常出价 + 超级出价（is_super=True，狙击工具：不能获胜、不参与撞车笔数，
    每次提交消耗本场配置的固定成本，命中——其数值上 ≥1 笔正常出价——使该数值
    全部正常出价按撞车处理，并获得本场配置的悬赏）。"""

    player: str
    raw_bid: Any          # 玩家提交的原始值，用于审计非法出价
    bid: int              # 校验后的有效出价（整单口径：任何非法提交整单强制为正常出价 0）
    status: str = "ok"    # "ok" | "illegal"（超级出价只可能为 "ok"，非法则在收集层拦截重试）
    is_super: bool = False


@dataclass
class DayResult:
    """一天的完整结算结果。public 视图 = 系统每日公布的信息。"""

    day: int
    bids: list[BidRecord]
    pool: int                    # 当天奖池的胜场数（含流局并入）
    winner: Optional[str]        # None 表示流局
    winning_bid: Optional[int]
    wins_taken: int              # 获胜者实际获得的胜场数（恒等于 pool；流局为 0）
    # 当天超级出价使用情况；每条 {"player": str, "value": int, "hit": bool}，
    # 命中即获得本场配置的悬赏；出价明细次日公开，命中与否亦可由明细推出
    super_uses: list[dict] = field(default_factory=list)
    # 当天撞车价位的具体数值（升序；只含正常出价被扣款的价位：
    # 正常出价互撞 ≥2 笔 + 被超级出价命中（≥1 笔正常出价）的数值）
    collision_values: list[int] = field(default_factory=list)
    is_void: bool = False            # 是否流局（奖池并入次日；末日流局则作废）

    def public_view(self) -> dict:
        """规则四.1：第 2 天起每天开始时公布昨日完整出价明细（全场玩家的
        正常出价数值 + 是否使用超级出价及狙击数值）与昨日获胜者/流局情况。
        super_uses 与 collision_values 可由明细推出，保留是为呈现层免推导。"""
        bids_detail = []
        for record in self.bids:
            if record.is_super:
                continue
            super_record = next(
                (r for r in self.bids if r.player == record.player and r.is_super),
                None,
            )
            bids_detail.append(
                {
                    "player": record.player,
                    "bid": record.bid,
                    "super_bid": super_record.bid if super_record is not None else None,
                }
            )
        return {
            "day": self.day,
            "winner": self.winner if not self.is_void else "流局",
            "winning_bid": self.winning_bid,
            "pool": self.pool,
            "wins_taken": self.wins_taken,
            "bids_detail": bids_detail,
            "super_uses": [dict(use) for use in self.super_uses],
            "collision_values": list(self.collision_values),
        }


@dataclass
class MatchLog:
    """一局的完整日志，局末向全体参赛者公开。"""

    match_index: int             # 第几局（1 起；正赛结束后继续编号加赛）
    is_playoff: bool
    game: GameSettings
    days: list[DayResult] = field(default_factory=list)
    wins: dict[str, int] = field(default_factory=dict)   # 本局各人获得的胜场

    def to_dict(self) -> dict:
        return {
            "game": self.game.to_dict(),
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
                    "super_uses": [dict(use) for use in day.super_uses],
                    "collision_values": list(day.collision_values),
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
    game: GameSettings          # 本场不可变规则参数，与裁判和日志共用
    # 昨日自己的结算结果（第 1 天用默认值）。这些信息都来自公开记录；
    # 直接列出，避免模型误算扣款，进而误判其他玩家的余额。
    my_last_bid: Optional[int] = None      # 昨日自己的有效正常出价（非法提交整单记 0）
    my_last_deduction: Optional[int] = None  # 昨日正常出价的扣款（退还为 0；超级成本另列）
    my_last_collided: bool = False         # 昨日自己的正常出价是否撞车（互撞或被狙）
    my_last_sniped: bool = False           # 昨日自己的正常出价是否被超级出价狙中
    my_last_bid_illegal: bool = False      # 昨日提交是否非法（整单强制为正常出价 0）
    my_last_super_bid: Optional[int] = None  # 昨日自己的超级出价（None = 昨日未使用）
    my_last_super_hit: bool = False        # 昨日自己的超级出价是否命中（命中获得悬赏）
    # 全场每人金币余额（含自己，含当日工资）：规则四.1 规定第 2 天起每天
    # 开始时公布；第 1 天为 None（均为本场初始金币，无信息量，不渲染）。
    all_coins: Optional[dict[str, int]] = None
