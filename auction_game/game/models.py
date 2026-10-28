"""数据结构定义：规则引擎与玩家之间流转的所有对象。"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Literal, Optional

from auction_game.game.game_rules import GameSettings


@dataclass(frozen=True)
class PlayerDayResult:
    """一名玩家一天的结算，由引擎生成，其他地方只读。"""

    player: str
    bid: int                     # 正常出价
    super_bid: Optional[int]     # 超级出价，未使用为 None
    outcome: Literal["won", "collided", "refunded"]  # 正常出价获胜、撞车或退还；获胜和撞车全额扣款
    sniped: bool                 # 正常出价所在价位被超级出价命中；此时 outcome 为 collided，可能同时与他人互撞
    super_hit: bool              # 超级出价是否命中；未使用为 False
    coins_delta: int             # 当天结算的金币变化：正常出价扣款、超级成本和悬赏，不含工资
    score: int                   # 当日得分：拍卖分加命中分


@dataclass(frozen=True)
class DayResult:
    """一天的结算结果。规则四.1：第 2 天起每天开始时全部公开。"""

    day: int
    pool: int                    # 当天配置奖池，流局时仍记录作废前的金额
    players: tuple[PlayerDayResult, ...]  # 按参赛顺序

    def result_of(self, name: str) -> PlayerDayResult:
        return next(result for result in self.players if result.player == name)

    @property
    def _winning(self) -> Optional[PlayerDayResult]:
        return next((result for result in self.players if result.outcome == "won"), None)

    @property
    def winner(self) -> Optional[str]:
        """None 表示流局。"""
        return self._winning.player if self._winning else None

    @property
    def winning_bid(self) -> Optional[int]:
        return self._winning.bid if self._winning else None

    @property
    def is_void(self) -> bool:
        """所有正常出价都撞车，当天奖池作废。"""
        return self._winning is None

    @property
    def auction_score(self) -> int:
        """获胜者取得的拍卖分，流局为 0。"""
        return 0 if self.is_void else self.pool

    @property
    def collision_values(self) -> list[int]:
        """正常出价被扣款的价位（互撞或被狙），升序。"""
        return sorted({result.bid for result in self.players if result.outcome == "collided"})

    def to_dict(self) -> dict:
        return {
            "day": self.day,
            "pool": self.pool,
            "winner": self.winner,
            "winning_bid": self.winning_bid,
            "players": [asdict(result) for result in self.players],
        }


@dataclass
class MatchLog:
    """一局的完整日志，局末向全体参赛者公开。"""

    match_index: int             # 第几局（1 起；正赛结束后继续编号加赛）
    is_playoff: bool
    game: GameSettings
    days: list[DayResult] = field(default_factory=list)
    auction_scores: dict[str, int] = field(default_factory=dict)  # 本局各人的拍卖分（含零分）
    scores: dict[str, int] = field(default_factory=dict)  # 本局总分（含零分）
    remaining_coins: dict[str, int] = field(default_factory=dict)  # 最近一天结算后的余额；局末即局末余额

    def to_dict(self) -> dict:
        return {
            "game": self.game.to_dict(),
            "match_index": self.match_index,
            "is_playoff": self.is_playoff,
            "auction_scores": dict(self.auction_scores),
            "scores": dict(self.scores),
            "remaining_coins": dict(self.remaining_coins),
            "days": [day.to_dict() for day in self.days],
        }


@dataclass
class StandingsEntry:
    """按总分排名，拍卖分单独统计；同分保持并列。"""

    player: str
    total_auction_score: int              # 含当前局的累计拍卖分
    auction_scores_per_match: list[int]   # 已完成各局取得的拍卖分
    rank: int = 0
    total_score: int = 0         # 含当前局已结算部分的累计分数
    scores_per_match: list[int] = field(default_factory=list)  # 仅含已完成各局分数


@dataclass
class Observation:
    """喂给玩家的决策依据，严格等于规则允许其知道的信息。"""

    my_name: str
    my_coins: int
    my_match_auction_score: int  # 本局已取得的拍卖分
    my_total_auction_score: int  # 含本局在内的累计拍卖分
    match_index: int             # 当前第几局（1 起；正赛结束后继续编号加赛）
    day: int                     # 当前第几天（1 起）
    today_pool: int              # 当天配置奖池
    today_history: list[DayResult]  # 本局已结束各天的结算，含自己昨日的结果
    standings: list[StandingsEntry]   # 截至昨日结算后的累计排名
    game: GameSettings          # 本场不可变规则参数，与裁判和日志共用
    # 全场每人金币余额（含自己，含当日工资）：规则四.1 规定第 2 天起每天
    # 开始时公布；第 1 天为 None（均为本场初始金币，无信息量，不渲染）。
    all_coins: Optional[dict[str, int]] = None
    my_match_score: int = 0      # 本局截至昨日的得分
    my_total_score: int = 0      # 全场截至昨日的累计分数
