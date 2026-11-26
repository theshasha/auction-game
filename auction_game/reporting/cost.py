"""按官方单价和价格倍率算模型回复的花费（美元），口径见 docs/analyze.md 的"模型用量"。

单价是能力快照里 models.dev 的 cost，每百万 token 的美元价；本模块只接收用量、单价和倍率，不读配置。
日志里回复的 usage.cost 是 pydantic-ai 按它自带的价表算的，只有 Anthropic 协议有，所以不用。
hub_anthropic 的回复不区分 5 分钟和 1 小时缓存写入，全部按 cache_write（5 分钟价）计。
"""

from __future__ import annotations


def tier_prices(cost: dict, input_tokens: int) -> dict:
    """单条回复适用的单价：输入超过某档 size 时整条按满足条件的最大档计，档里的单价替换基础单价。"""
    prices = cost
    for tier in sorted(cost.get("tiers", ()), key=lambda tier: tier["tier"]["size"]):
        if input_tokens > tier["tier"]["size"]:
            prices = tier
    return prices


def response_cost(usage: dict, cost: dict | None, multiplier: int | float) -> float | None:
    """一条模型回复的花费；cost 为 None（来源没有单价）时返回 None。

    usage 是任务日志里回复的 usage：input_tokens 含缓存读写，output_tokens 含推理，推理按输出价计。
    cache_write_1h_tokens 只在 Anthropic 协议写了 1 小时缓存时出现，按 2 倍输入价计，其余缓存写入按 cache_write，
    没有 cache_write 单价时按输入价。
    """
    if cost is None:
        return None
    prices = tier_prices(cost, usage["input_tokens"])
    input_price = prices["input"]
    cache_write = usage["cache_write_tokens"]
    write_1h = usage.get("cache_write_1h_tokens", 0)
    uncached = usage["input_tokens"] - usage["cache_read_tokens"] - cache_write
    dollars_per_million = (uncached * input_price
                           + usage["cache_read_tokens"] * prices.get("cache_read", input_price)
                           + write_1h * 2 * input_price
                           + (cache_write - write_1h) * prices.get("cache_write", input_price)
                           + usage["output_tokens"] * prices["output"])
    return dollars_per_million * multiplier / 1_000_000


def reasoning_priced_apart(cost: dict | None) -> bool:
    """来源给推理另标了和输出不同的单价；本模块仍按输出价计，展示时要提示。"""
    return cost is not None and "reasoning" in cost and cost["reasoning"] != cost["output"]


def dollars(value: float) -> str:
    """美元金额；不到 0.1 美元时多留两位，免得便宜的任务显示成 0。"""
    if value >= 0.1:
        return f"${value:,.2f}"
    return f"${value:.4f}" if value else "$0"
