"""玩家抽象与本阶段的两个实现：RandomBot（跑游戏）、ScriptedPlayer（测试用）。"""

from __future__ import annotations

import random
from abc import ABC, abstractmethod
from typing import Callable, Optional

from models import Observation


class Player(ABC):
    """玩家统一接口。规则引擎只依赖此接口，与具体实现（Bot / LLM）解耦。"""

    def __init__(self, name: str):
        self.name = name

    @abstractmethod
    def decide_bid(self, obs: Observation) -> tuple[int, Optional[int]]:
        """根据当前可见信息给出今日出价，返回 (正常出价, 超级出价或 None)。

        v6 整单校验：任何非法提交（正常出价超余额/超范围/非整数、超级出价
        与正常出价同值、正常出价 + 超级成本合计超余额等）一律
        整单强制为正常出价 0、无超级出价。超级出价不限次数（每天最多 1 笔）、
        每次提交消耗本场配置的固定成本、只狙击不能获胜、不参与撞车笔数。
        """


class RandomBot(Player):
    """在 0 到 min(出价上限, 余额) 之间均匀随机出价；
    超级出价每天以固定概率随机使用（值在 0~上限间均匀随机；
    余额约束：正常出价 + 超级成本 ≤ 余额才使用，抽到同值则放弃）。"""

    def __init__(
        self,
        name: str,
        rng: Optional[random.Random] = None,
        super_chance: float = 0.3,
    ):
        super().__init__(name)
        self.rng = rng or random.Random()
        self.super_chance = super_chance

    def decide_bid(self, obs: Observation) -> tuple[int, Optional[int]]:
        bid = self.rng.randint(0, min(obs.my_coins, obs.game.max_bid))
        super_bid = None
        if self.rng.random() < self.super_chance and bid + obs.game.super_cost <= obs.my_coins:
            candidate = self.rng.randint(0, obs.game.max_bid)
            if candidate != bid:  # 同值在 v6 属非法提交（整单作废），直接放弃本次
                super_bid = candidate
        return bid, super_bid


class ScriptedPlayer(Player):
    """按预设策略函数出价的确定性玩家，用于单元测试构造指定场景。

    strategy: 签名为 (Observation) -> int 的函数，决定正常出价。
    super_strategy: 可选，签名为 (Observation) -> Optional[int] 的函数，
    决定超级出价（返回 None 表示不使用）。
    便捷构造：ScriptedPlayer.constant("甲", 3) 每天固定出 3。
    """

    def __init__(
        self,
        name: str,
        strategy: Callable[[Observation], int],
        super_strategy: Optional[Callable[[Observation], Optional[int]]] = None,
    ):
        super().__init__(name)
        self.strategy = strategy
        self.super_strategy = super_strategy

    def decide_bid(self, obs: Observation) -> tuple[int, Optional[int]]:
        super_bid = None
        if self.super_strategy is not None:
            super_bid = self.super_strategy(obs)
        return self.strategy(obs), super_bid

    @classmethod
    def constant(cls, name: str, bid: int) -> "ScriptedPlayer":
        return cls(name, lambda obs: bid)

    @classmethod
    def from_bid_list(cls, name: str, bids: list[int], fallback: int = 0) -> "ScriptedPlayer":
        """按列表顺序依次出价（跨天、跨局连续消耗），用完后出 fallback。"""
        remaining = list(bids)

        def strategy(obs: Observation) -> int:
            if remaining:
                return remaining.pop(0)
            return fallback

        return cls(name, strategy)
