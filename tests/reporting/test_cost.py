"""单条回复的花费；单价是每百万 token 的美元价，金额在注释里手算。"""

import pytest

from auction_game.reporting.cost import reasoning_priced_apart, response_cost

COST = {"input": 2, "output": 10, "cache_read": 0.2, "cache_write": 2.5,
        "tiers": [{"tier": {"type": "context", "size": 2000}, "input": 8, "output": 40, "cache_read": 0.8},
                  {"tier": {"type": "context", "size": 1000}, "input": 4, "output": 20, "cache_read": 0.4,
                   "cache_write": 5}]}


def usage(input_tokens, output_tokens, cache_read=0, cache_write=0, **extra):
    return {"input_tokens": input_tokens, "cache_read_tokens": cache_read, "cache_write_tokens": cache_write,
            "output_tokens": output_tokens, **extra}


@pytest.mark.parametrize("reply,dollars", [
    # 未命中 600 × 2 + 命中 300 × 0.2 + 写入 100 × 2.5 + 输出 50 × 10
    (usage(1000, 50, cache_read=300, cache_write=100), 1200 + 60 + 250 + 500),
    # 超过 1000 一档：未命中 1001 × 4 + 输出 50 × 20
    (usage(1001, 50), 1001 * 4 + 50 * 20),
    # 同时超过两档时按最大的一档；这一档没写 cache_write，写入按本档输入价
    (usage(3000, 10, cache_read=1000, cache_write=1000), 1000 * 8 + 1000 * 0.8 + 1000 * 8 + 10 * 40),
], ids=["base", "tier", "largest-tier"])
def test_tiers_price_the_whole_reply(reply, dollars):
    assert response_cost(reply, COST, 1) == pytest.approx(dollars / 1_000_000)


def test_one_hour_cache_write_costs_twice_input():
    # Anthropic 回复：未命中 4 × 0.1 + 1 小时写入 1749 × 0.2 + 输出 3651 × 0.5；日志里 usage.cost 是 0.0021757
    haiku = {"input": 0.1, "output": 0.5, "cache_read": 0.01, "cache_write": 0.125}
    reply = usage(1753, 3651, cache_write=1749, cache_write_1h_tokens=1749)
    assert response_cost(reply, haiku, 1) == pytest.approx(0.0021757)
    # 没有 1 小时写入时同一份用量按 cache_write 计
    assert response_cost(usage(1753, 3651, cache_write=1749), haiku, 1) == pytest.approx(
        (4 * 0.1 + 1749 * 0.125 + 3651 * 0.5) / 1_000_000)


def test_missing_cache_prices_fall_back_to_input_and_multiplier_scales():
    cost = {"input": 2, "output": 10}
    # (未命中 500 + 命中 300 + 写入 200) × 2 + 输出 100 × 10，再乘 0.075
    assert response_cost(usage(1000, 100, cache_read=300, cache_write=200), cost, 0.075) == pytest.approx(
        (1000 * 2 + 100 * 10) * 0.075 / 1_000_000)


def test_without_price_returns_none():
    assert response_cost(usage(1000, 100), None, 1) is None


def test_reasoning_price_apart_only_when_it_differs():
    assert not reasoning_priced_apart({"input": 1, "output": 2, "reasoning": 2})
    assert not reasoning_priced_apart({"input": 1, "output": 2})
    assert not reasoning_priced_apart(None)
    assert reasoning_priced_apart({"input": 1, "output": 2, "reasoning": 3})
