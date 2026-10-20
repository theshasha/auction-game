"""比赛参数和出价规则检查，引擎和模型层共用。"""

from __future__ import annotations

from dataclasses import dataclass, fields


@dataclass(frozen=True)
class GameSettings:
    """一场比赛的完整参数；生产默认值只在 game.yaml 中维护。"""

    initial_coins: int
    daily_salary: int
    daily_pool: tuple[int, ...]
    max_bid: int
    super_cost: int
    super_bounty: int
    super_hit_score: int
    regular_matches: int
    max_playoffs: int

    def __post_init__(self) -> None:
        for field in fields(self):
            name = field.name
            if name == "daily_pool":
                continue
            value = getattr(self, name)
            minimum = 1 if name == "regular_matches" else 0
            if type(value) is not int or value < minimum:
                requirement = "正整数" if minimum else "非负整数"
                raise ValueError(f"{name} 必须是{requirement}")
        if not self.daily_pool:
            raise ValueError("daily_pool 不能为空")
        for index, value in enumerate(self.daily_pool):
            if type(value) is not int or value <= 0:
                raise ValueError(f"daily_pool[{index}] 必须是正整数")

    @property
    def max_days(self) -> int:
        return len(self.daily_pool)

    @property
    def auction_pool_per_match(self) -> int:
        """本局每日奖池的总和；流局当天的拍卖分作废，实际发放的分数可能更少。"""
        return sum(self.daily_pool)

    @classmethod
    def from_dict(cls, data: dict) -> GameSettings:
        """从 game.yaml 或场次快照加载；daily_pool 之外的缺字段或未知字段由构造函数报 TypeError。"""
        pool = data.get("daily_pool")
        if not isinstance(pool, list):
            raise ValueError("daily_pool 必须是非空列表，各日奖池必须是正整数")
        return cls(**{**data, "daily_pool": tuple(pool)})

    def to_dict(self) -> dict:
        """返回独立的 JSON 友好快照，奖池序列输出为 list。"""
        return {
            field.name: list(self.daily_pool)
            if field.name == "daily_pool"
            else getattr(self, field.name)
            for field in fields(self)
        }


def validate_submission(
    raw_bid: object,
    raw_super_bid: object,
    coins: int,
    game: GameSettings,
) -> str | None:
    """检查一次完整出价，返回违规原因供模型纠正；符合规则时返回 None。"""
    if type(raw_bid) is not int:
        return f"bid 必须是不加引号的整数，收到 {raw_bid!r}"
    if raw_bid < 0 or raw_bid > game.max_bid:
        return f"出价 {raw_bid} 超出 0~{game.max_bid} 的范围"
    if raw_bid > coins:
        return f"出价 {raw_bid} 超过当前余额 {coins}"
    if raw_super_bid is None:
        return None
    if type(raw_super_bid) is not int:
        return f"super_bid 必须是不加引号的整数，不使用超级出价时填 JSON null，收到 {raw_super_bid!r}"
    if raw_super_bid < 0 or raw_super_bid > game.max_bid:
        return f"超级出价 {raw_super_bid} 超出 0~{game.max_bid} 的范围"
    if raw_super_bid == raw_bid:
        return f"超级出价 {raw_super_bid} 与你的正常出价相同，请选择不同的数值"
    if raw_bid + game.super_cost > coins:
        return (
            f"正常出价 {raw_bid} 加超级出价成本 {game.super_cost} 金币，"
            f"合计超过当前余额 {coins}"
        )
    return None
