"""不可变比赛参数与共享出价校验；不读取配置文件或依赖玩家实现。"""

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
        if not isinstance(self.daily_pool, tuple) or not self.daily_pool:
            raise ValueError("daily_pool 必须是非空 tuple，各日奖池必须是正整数")
        for index, value in enumerate(self.daily_pool):
            if type(value) is not int or value <= 0:
                raise ValueError(f"daily_pool[{index}] 必须是正整数")

    @property
    def max_days(self) -> int:
        return len(self.daily_pool)

    @property
    def wins_per_match(self) -> int:
        """计划总胜场；末日流局时实际分配数量可能更少。"""
        return sum(self.daily_pool)

    @classmethod
    def from_dict(cls, data: dict) -> GameSettings:
        """从配置或日志快照加载，拒绝缺少字段、未知字段及非法值。"""
        if not isinstance(data, dict):
            raise ValueError("game 根节点必须是 mapping（键值配置）")
        names = [field.name for field in fields(cls)]
        unknown = [str(name) for name in data if name not in names]
        if unknown:
            raise ValueError(f"未知字段 {', '.join(unknown)}，请检查字段拼写")
        missing = [name for name in names if name not in data]
        if missing:
            raise ValueError(f"缺少必填字段 {', '.join(missing)}")
        values = dict(data)
        pool = values["daily_pool"]
        if not isinstance(pool, (list, tuple)):
            raise ValueError("daily_pool 必须是非空列表，各日奖池必须是正整数")
        values["daily_pool"] = tuple(pool)
        return cls(**values)

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
    """返回整单提交的违规原因，合法返回 None；保留 AI 重试的错误语义。"""
    if type(raw_bid) is not int:
        return f"bid 不是整数（{raw_bid!r}）"
    if raw_bid < 0 or raw_bid > game.max_bid:
        return f"出价 {raw_bid} 超出 0~{game.max_bid} 的范围"
    if raw_bid > coins:
        return f"出价 {raw_bid} 超过当前余额 {coins}"
    if raw_super_bid is None:
        return None
    if type(raw_super_bid) is not int:
        return f"super_bid 不是整数（{raw_super_bid!r}）"
    if raw_super_bid < 0 or raw_super_bid > game.max_bid:
        return f"超级出价 {raw_super_bid} 超出 0~{game.max_bid} 的范围"
    if raw_super_bid == raw_bid:
        return f"超级出价 {raw_super_bid} 不能等于你的正常出价（同值属非法提交）"
    if raw_bid + game.super_cost > coins:
        return (
            f"正常出价 {raw_bid} 加超级出价成本 {game.super_cost} 金币，"
            f"合计超过当前余额 {coins}"
        )
    return None
