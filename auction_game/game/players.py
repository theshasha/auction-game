"""玩家接口。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional

from auction_game.game.models import Observation


class Player(ABC):
    """规则引擎只通过这个接口取出价，AI 玩家和测试里的脚本玩家都实现它。"""

    def __init__(self, name: str):
        self.name = name

    @abstractmethod
    def decide_bid(self, obs: Observation) -> tuple[int, Optional[int]]:
        """返回今日的 (正常出价, 超级出价或 None)；提交不符合规则时引擎报错。"""
