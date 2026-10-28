"""超级出价（收费狙击制）的引擎测试。

测试基准参数：不限次数（每天最多 1 笔），每次提交固定消耗 2 金币（无论是否命中）；
0~7、不能等于当天正常出价、与正常出价合计不得超过余额、不能获胜；
命中 = 其数值上 ≥1 笔正常出价 → 该数值所有正常出价按撞车处理，
狙击者获得系统发放的 3 金币悬赏（多人命中同一数值各得 3）。
"""

import pytest

from auction_game.game.engine import resolve_bids
from tests.support.factories import NAMES, run_scenario, super_by_day


def super_uses(day):
    """当天用了超级出价的人：(玩家, 超级出价, 是否命中)。"""
    return [(result.player, result.super_bid, result.super_hit)
            for result in day.players if result.super_bid is not None]


def test_resolve_bids_with_and_without_supers():
    normal_bids = list(zip(NAMES, [0, 4, 3, 2, 2, 1]))
    assert resolve_bids(normal_bids, [4, 4, 7]) == ("丙", {2, 4}, {4})
    # 统计里的反事实判定：去掉超级出价后只按正常出价重判。
    assert resolve_bids(normal_bids, []) == ("乙", {2}, set())


class TestSuperBid:
    def test_snipe_unique_normal_bounty_and_reuse(self):
        # 狙击唯一正常出价：甲出 6；乙正常出 4 + 超级出价 6；丙 5；
        # 丁戊 3；己 2。乙的超级 6 命中甲的唯一 6（甲按撞车扣 6），3 互撞
        # （丁戊各扣 3），丙未撞车最高 5 获胜扣 5；乙 -2 成本 +3 悬赏，正常出价 4 退还
        captured = {}
        log = run_scenario(
            {1: [6, 4, 5, 3, 3, 2], 2: [0, 6, 0, 0, 0, 0]},
            super_strategies={"乙": super_by_day({1: 6, 2: 5})},
            capture=captured,
        )
        day1 = log.days[0]
        assert day1.winner == "丙" and day1.winning_bid == 5
        assert super_uses(day1) == [("乙", 6, True)]
        assert day1.collision_values == [3, 6]  # 甲被狙的 6 + 丁戊互撞的 3
        assert [(result.outcome, result.sniped) for result in day1.players] == [
            ("collided", True), ("refunded", False), ("won", False),
            ("collided", False), ("collided", False), ("refunded", False),
        ]

        # 甲被狙扣 6：余额 7-6+3=4
        assert captured["甲"][1].my_coins == 4
        # 乙第 1 天净 +1（-2 成本 +3 悬赏）：次日余额 7-2+3+3=11。
        assert captured["乙"][1].my_coins == 11
        # 不限次数：第 2 天乙的超级出价（5，异于正常出价 6，6+2≤11）再次被受理
        assert log.days[1].result_of("乙").super_bid == 5

    def test_super_whiff_still_costs(self):
        # 乙正常出 4 + 超级出价 6：正常出价 5/4/3/2/1/0 全唯一，6 上没有正常出价
        # → 落空：无效果但照扣 2 金币成本；甲的唯一 5 获胜
        captured = {}
        log = run_scenario(
            {1: [5, 4, 3, 2, 1, 0]},
            super_strategies={"乙": super_by_day({1: 6})},
            capture=captured,
        )
        day1 = log.days[0]
        assert day1.winner == "甲" and day1.winning_bid == 5
        assert super_uses(day1) == [("乙", 6, False)]
        assert day1.collision_values == []
        # 落空也扣 2 成本：乙第 1 天 7-2=5，第 2 天工资后为 8（而非 10）
        assert captured["乙"][1].my_coins == 8
        assert log.scores == {"甲": 4, "乙": 0, "丙": 0, "丁": 0, "戊": 0, "己": 0}

    def test_super_bid_requires_balance_headroom(self):
        # 甲第 1 天出 6 获胜余额 1；第 2 天 +3 后正常出 3 再狙 6：3+2>4
        with pytest.raises(ValueError, match="合计超过当前余额 4"):
            run_scenario(
                {1: [6, 1, 1, 1, 1, 1], 2: [3, 0, 0, 0, 0, 0]},
                super_strategies={"甲": super_by_day({2: 6})},
            )

    def test_pure_snipe_with_zero_normal_bid(self):
        # 正常出 0 + 超级出价（纯狙击）：0+2 ≤ 余额即合法。
        # 甲第 1 天出 6 获胜余额 1，第 2 天 +3 后纯狙击出 6：落空（全员正常出 0 互撞），
        # 正常出价全灭流局，当天奖池作废；甲扣 2 成本
        captured = {}
        log = run_scenario(
            {1: [6, 1, 1, 1, 1, 1], 2: [0, 0, 0, 0, 0, 0]},
            super_strategies={"甲": super_by_day({2: 6})},
            capture=captured,
        )
        day2 = log.days[1]
        assert super_uses(day2) == [("甲", 6, False)]
        assert day2.is_void and day2.winner is None and day2.auction_score == 0
        assert log.days[2].pool == 4
        assert captured["甲"][2].my_coins == 5  # 7-6+3-2+3，纯狙击仍扣成本

    def test_super_bid_equal_to_normal_is_rejected(self):
        with pytest.raises(ValueError, match="与你的正常出价相同"):
            run_scenario(
                {1: [5, 4, 3, 2, 1, 0]},
                super_strategies={"甲": super_by_day({1: 5})},
            )

    def test_multi_snipers_same_value_both_rewarded(self):
        # 多人命中同一数值各得 3 金币：乙、己的超级 6 同时命中甲的唯一 6
        captured = {}
        log = run_scenario(
            {1: [6, 4, 5, 3, 3, 2], 2: [0, 6, 0, 0, 0, 6]},
            super_strategies={"乙": super_by_day({1: 6}), "己": super_by_day({1: 6})},
            capture=captured,
        )
        day1 = log.days[0]
        assert super_uses(day1) == [("乙", 6, True), ("己", 6, True)]
        assert day1.winner == "丙" and day1.winning_bid == 5
        # 乙/己各净 +1（-2 +3）：次日余额均为 11；甲被狙扣 6：余额 4
        assert captured["乙"][1].my_coins == 11
        assert captured["己"][1].my_coins == 11
        assert captured["甲"][1].my_coins == 4
        assert log.scores == {"甲": 0, "乙": 2, "丙": 4, "丁": 0, "戊": 0, "己": 2}
        assert captured["乙"][1].my_match_score == 2
        assert captured["己"][1].my_match_score == 2

    def test_snipe_non_top_unique_also_pays_bounty(self):
        # 命中不限于最高唯一价：乙的超级 0 命中己的唯一 0（扣 0 无实质损失），
        # 乙照拿 3 金币悬赏（净 +1）；胜者仍是未撞车最高的甲
        captured = {}
        log = run_scenario(
            {1: [5, 4, 3, 2, 1, 0]},
            super_strategies={"乙": super_by_day({1: 0})},
            capture=captured,
        )
        day1 = log.days[0]
        assert super_uses(day1) == [("乙", 0, True)]
        assert day1.winner == "甲" and day1.winning_bid == 5
        assert day1.collision_values == [0]
        assert captured["乙"][1].my_coins == 11

    def test_snipe_value_with_multiple_normal_bids_still_hits(self):
        # 被狙数值上有 ≥2 笔正常出价（本来就会互撞）也算命中发赏，
        # 该数值所有正常出价全部撞车。丙狙 6（甲乙各一笔 6），命中拿悬赏；
        # 6 全灭后未撞车最高的是丙自己的 4——丙捡漏获胜（付 4，净 +1 悬赏差）
        captured = {}
        log = run_scenario(
            {1: [6, 6, 4, 3, 2, 1]},
            super_strategies={"丙": super_by_day({1: 6})},
            capture=captured,
        )
        day1 = log.days[0]
        assert super_uses(day1) == [("丙", 6, True)]
        assert day1.collision_values == [6]
        # 本来就互撞的价位被狙，结局仍是撞车，同时记为被狙。
        assert [(result.outcome, result.sniped) for result in day1.players[:2]] == [("collided", True)] * 2
        assert day1.winner == "丙" and day1.winning_bid == 4
        # 丙：7 - 4（获胜扣款）- 2（成本）+ 3（悬赏）= 4，第 2 天工资后为 7
        assert captured["丙"][1].my_coins == 7
        assert log.scores == {"甲": 0, "乙": 0, "丙": 6, "丁": 0, "戊": 0, "己": 0}
        assert captured["丙"][1].my_match_score == 6

    def test_void_discards_pool_and_keeps_each_snipers_rewards(self):
        captured = {}
        log = run_scenario(
            {
                1: [4, 4, 0, 0, 0, 0],
                2: [0, 0, 0, 1, 0, 0],
                6: [1, 1, 0, 0, 0, 0],
            },
            super_strategies={
                "甲": super_by_day({1: 0, 6: 0}),
                "乙": super_by_day({1: 0, 6: 0}),
                "丙": super_by_day({1: 4, 6: 1}),
            },
            capture=captured,
        )
        assert log.days[0].is_void and log.days[-1].is_void
        assert super_uses(log.days[0]) == [("甲", 0, True), ("乙", 0, True), ("丙", 4, True)]
        assert [day.pool for day in log.days] == [4, 4, 4, 4, 4, 4]
        assert log.auction_scores == {"甲": 0, "乙": 0, "丙": 0, "丁": 4, "戊": 0, "己": 0}
        assert log.scores == {"甲": 4, "乙": 4, "丙": 4, "丁": 4, "戊": 0, "己": 0}
        # 首日流局照扣正常出价和超级成本，并发悬赏；次日再加工资。
        assert captured["甲"][1].all_coins == {"甲": 7, "乙": 7, "丙": 11, "丁": 10, "戊": 10, "己": 10}
        assert captured["甲"][1].my_match_score == 2
        assert captured["乙"][1].my_match_score == 2
        assert captured["丙"][1].my_match_score == 2
        assert captured["丁"][2].my_match_score == 4

    def test_next_day_observation_carries_settled_records(self):
        # 次日观察带昨日全员结算：狙击者的命中和悬赏、被狙者的扣款、未使用者的空超级出价
        captured = {}
        log = run_scenario(
            {1: [6, 4, 5, 3, 3, 2]},
            super_strategies={"乙": super_by_day({1: 6})},
            capture=captured,
        )
        yesterday = captured["丙"][1].today_history[0]
        assert yesterday is log.days[0]
        sniper, target, winner = (yesterday.result_of(name) for name in ("乙", "甲", "丙"))
        # 乙正常出价 4 退还，超级出价 -2 成本 +3 悬赏，得 2 命中分
        assert (sniper.super_bid, sniper.super_hit, sniper.coins_delta, sniper.score) == (6, True, 1, 2)
        assert (target.outcome, target.sniped, target.coins_delta, target.score) == ("collided", True, -6, 0)
        assert (winner.super_bid, winner.super_hit, winner.coins_delta, winner.score) == (None, False, -5, 4)
        assert [(result.player, result.bid, result.super_bid) for result in yesterday.players] == [
            ("甲", 6, None), ("乙", 4, 6), ("丙", 5, None), ("丁", 3, None), ("戊", 3, None), ("己", 2, None),
        ]
