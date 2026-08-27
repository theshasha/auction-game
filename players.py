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
    def decide_bid(self, obs: Observation) -> int:
        """根据当前可见信息给出今日出价。返回值会被引擎校验，非法一律记 0。"""


class RandomBot(Player):
    """在 0 到 min(出价上限, 余额) 之间均匀随机出价。"""

    def __init__(self, name: str, rng: Optional[random.Random] = None):
        super().__init__(name)
        self.rng = rng or random.Random()

    def decide_bid(self, obs: Observation) -> int:
        return self.rng.randint(0, min(obs.my_coins, 6))


class ScriptedPlayer(Player):
    """按预设策略函数出价的确定性玩家，用于单元测试构造指定场景。

    strategy: 签名为 (Observation) -> int 的函数。
    便捷构造：ScriptedPlayer.constant("甲", 3) 每天固定出 3。
    """

    def __init__(self, name: str, strategy: Callable[[Observation], int]):
        super().__init__(name)
        self.strategy = strategy

    def decide_bid(self, obs: Observation) -> int:
        return self.strategy(obs)

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
