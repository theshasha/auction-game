"""玩家接口和两个本地实现：RandomBot 用于 Bot 比赛和对照，ScriptedPlayer 按给定策略出价。"""

from __future__ import annotations

import random
from abc import ABC, abstractmethod
from typing import Callable, Optional

from auction_game.game.models import Observation

BOT_NAMES = ["Alpha", "Bravo", "Charlie", "Delta", "Echo", "Foxtrot"]


class Player(ABC):
    """规则引擎只通过这个接口取出价，Bot 和 AI 玩家都实现它。"""

    def __init__(self, name: str):
        self.name = name

    @abstractmethod
    def decide_bid(self, obs: Observation) -> tuple[int, Optional[int]]:
        """返回今日的 (正常出价, 超级出价或 None)；提交不符合规则时引擎报错。"""


class RandomBot(Player):
    """正常出价在 0 到 min(出价上限, 余额) 间均匀随机；余额够付超级成本时，
    按 super_chance 随机提交一笔超级出价，抽到与正常出价同值就放弃。"""

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
            if candidate != bid:
                super_bid = candidate
        return bid, super_bid


class ScriptedPlayer(Player):
    """按给定函数出价：strategy(obs) 返回正常出价，可选的 super_strategy(obs) 返回超级出价或 None。"""

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
