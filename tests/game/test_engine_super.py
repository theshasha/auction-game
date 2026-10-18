"""超级出价（收费狙击制）的引擎测试。场景构造器见 conftest.py。

测试基准参数：不限次数（每天最多 1 笔），每次提交固定消耗 2 金币（无论是否命中）；
0~7、不能等于当天正常出价、与正常出价合计不得超过余额、不能获胜；
命中 = 其数值上 ≥1 笔正常出价 → 该数值所有正常出价按撞车处理，
狙击者获得系统发放的 3 金币悬赏（多人命中同一数值各得 3）。
"""

from copy import deepcopy

from auction_game.game.engine import resolve_bids
from tests.support.factories import make_day, run_scenario, super_by_day


def test_resolve_bids_is_pure_and_uses_effective_normal_bids():
    day = make_day(1, [0, 4, 3, 2, 2, 1], 1, "丙", 3, [2, 4],
                   super_bids={"丙": (4, True), "己": (4, True), "丁": (7, False)})
    day.bids[0].raw_bid = 7
    day.bids[0].status = "illegal"
    original = deepcopy(day.bids)
    winner, collisions, hits = resolve_bids(day.bids)
    assert winner is day.bids[2]
    assert collisions == {2, 4}
    assert hits == {4}
    normal_bids = [bid for bid in day.bids if not bid.is_super]
    winner, collisions, hits = resolve_bids(normal_bids)
    assert winner is day.bids[1]
    assert collisions == {2}
    assert hits == set()
    assert day.bids == original


class TestSuperBid:
    def test_snipe_unique_normal_bounty_and_reuse(self):
        # 狙击唯一正常出价：甲出 6；乙正常出 4 + 超级出价 6；丙 5；
        # 丁戊 3；己 2。乙的超级 6 命中甲的唯一 6（甲按撞车扣 6），3 互撞
        # （丁戊各扣 3），丙未撞车最高 5 获胜扣 5；乙 -2 成本 +3 悬赏，正常出价 4 退还
        captured = {}
        log = run_scenario(
            {1: [6, 4, 5, 3, 3, 2], 2: [6, 6, 0, 0, 0, 0]},
            super_strategies={"乙": super_by_day({1: 6, 2: 5})},
            capture=captured,
        )
        day1 = log.days[0]
        assert day1.winner == "丙" and day1.winning_bid == 5
        assert day1.super_uses == [{"player": "乙", "value": 6, "hit": True}]
        assert day1.collision_values == [3, 6]  # 甲被狙的 6 + 丁戊互撞的 3

        records = {r.player: r for r in log.days[1].bids if not r.is_super}
        # 甲被狙扣 6：余额 7-6+3=4，出 6 应非法
        assert records["甲"].bid == 0 and records["甲"].status == "illegal"
        # 乙第 1 天净 +1（-2 成本 +3 悬赏）：次日余额 7-2+3+3=11。
        assert captured["乙"][1].my_coins == 11
        assert records["乙"].bid == 6 and records["乙"].status == "ok"
        # 不限次数：第 2 天乙的超级出价（5，异于正常出价 6，6+2≤11）再次被受理
        day2_super = next(
            (r for r in log.days[1].bids if r.player == "乙" and r.is_super), None
        )
        assert day2_super is not None and day2_super.bid == 5

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
        assert day1.super_uses == [{"player": "乙", "value": 6, "hit": False}]
        assert day1.collision_values == []
        # 落空也扣 2 成本：乙第 1 天 7-2=5，第 2 天工资后为 8（而非 10）
        assert captured["乙"][1].my_coins == 8
        assert log.scores == {"甲": 4, "乙": 0, "丙": 0, "丁": 0, "戊": 0, "己": 0}

    def test_super_bid_requires_balance_headroom(self):
        # 总耗资约束：正常出价 + 2 成本不得超过余额，否则整单作废
        # 甲第 1 天出 6 获胜余额 1；第 2 天 +3 后正常出 3 再狙 6：3+2>4 → 整单作废
        log = run_scenario(
            {1: [6, 1, 1, 1, 1, 1], 2: [3, 0, 0, 0, 0, 0]},
            super_strategies={"甲": super_by_day({2: 6})},
        )
        day2 = log.days[1]
        assert day2.super_uses == []
        record_jia = next(r for r in day2.bids if r.player == "甲" and not r.is_super)
        assert record_jia.raw_bid == 3
        assert record_jia.bid == 0 and record_jia.status == "illegal"

    def test_pure_snipe_with_zero_normal_bid(self):
        # 正常出 0 + 超级出价（纯狙击）：0+2 ≤ 余额即合法。
        # 甲第 1 天出 6 获胜余额 1，第 2 天 +3 后纯狙击出 6：落空（全员正常出 0 互撞），
        # 正常出价全灭流局，奖池并入第 3 天；甲扣 2 成本
        captured = {}
        log = run_scenario(
            {1: [6, 1, 1, 1, 1, 1], 2: [0, 0, 0, 0, 0, 0]},
            super_strategies={"甲": super_by_day({2: 6})},
            capture=captured,
        )
        day2 = log.days[1]
        assert day2.super_uses == [{"player": "甲", "value": 6, "hit": False}]
        assert day2.is_void and day2.winner is None
        assert log.days[2].pool == 8
        assert captured["甲"][2].my_coins == 5  # 7-6+3-2+3，纯狙击仍扣成本

    def test_super_bid_equal_to_normal_voids_whole_submission(self):
        # 同值属非法提交：整单作废为正常出价 0、无超级出价。
        # 甲正常出 5 + 超级出 5 → 甲记非法 0；0 撞车（甲己），乙的唯一 4 获胜
        log = run_scenario(
            {1: [5, 4, 3, 2, 1, 0]},
            super_strategies={"甲": super_by_day({1: 5})},
        )
        day1 = log.days[0]
        assert day1.super_uses == []
        assert not any(r.is_super for r in day1.bids)
        record_jia = next(r for r in day1.bids if r.player == "甲" and not r.is_super)
        assert record_jia.raw_bid == 5
        assert record_jia.bid == 0 and record_jia.status == "illegal"
        assert day1.winner == "乙" and day1.winning_bid == 4
        assert day1.collision_values == [0]

    def test_multi_snipers_same_value_both_rewarded(self):
        # 多人命中同一数值各得 3 金币：乙、己的超级 6 同时命中甲的唯一 6
        captured = {}
        log = run_scenario(
            {1: [6, 4, 5, 3, 3, 2], 2: [6, 6, 0, 0, 0, 6]},
            super_strategies={"乙": super_by_day({1: 6}), "己": super_by_day({1: 6})},
            capture=captured,
        )
        day1 = log.days[0]
        assert day1.super_uses == [
            {"player": "乙", "value": 6, "hit": True},
            {"player": "己", "value": 6, "hit": True},
        ]
        assert day1.winner == "丙" and day1.winning_bid == 5
        records = {r.player: r for r in log.days[1].bids if not r.is_super}
        # 乙/己各净 +1（-2 +3）：次日余额均为 11；甲被狙扣 6：余额 4，出 6 非法
        assert captured["乙"][1].my_coins == 11
        assert captured["己"][1].my_coins == 11
        assert records["乙"].status == "ok" and records["己"].status == "ok"
        assert records["甲"].status == "illegal"
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
        assert day1.super_uses == [{"player": "乙", "value": 0, "hit": True}]
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
        assert day1.super_uses == [{"player": "丙", "value": 6, "hit": True}]
        assert day1.collision_values == [6]
        assert day1.winner == "丙" and day1.winning_bid == 4
        # 丙：7 - 4（获胜扣款）- 2（成本）+ 3（悬赏）= 4，第 2 天工资后为 7
        assert captured["丙"][1].my_coins == 7
        assert log.scores == {"甲": 0, "乙": 0, "丙": 6, "丁": 0, "戊": 0, "己": 0}
        assert captured["丙"][1].my_match_score == 6

    def test_void_hits_score_each_sniper_and_only_auction_pool_rolls_over(self):
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
        assert all(record.status == "ok" for day in log.days for record in day.bids)
        assert log.days[0].is_void and log.days[-1].is_void
        assert log.days[0].super_uses == [
            {"player": "甲", "value": 0, "hit": True},
            {"player": "乙", "value": 0, "hit": True},
            {"player": "丙", "value": 4, "hit": True},
        ]
        assert [day.pool for day in log.days] == [4, 8, 4, 8, 12, 16]
        assert log.auction_scores == {"甲": 0, "乙": 0, "丙": 0, "丁": 8, "戊": 0, "己": 0}
        assert log.scores == {"甲": 4, "乙": 4, "丙": 4, "丁": 8, "戊": 0, "己": 0}
        assert captured["甲"][1].my_match_score == 2
        assert captured["乙"][1].my_match_score == 2
        assert captured["丙"][1].my_match_score == 2
        assert captured["丁"][2].my_match_score == 8

    def test_super_observation_fields(self):
        # Observation 携带昨日超级出价结局（数值/命中，无次数状态字段）；
        # 被狙者标记「被狙」；公开视图含出价明细（次日即公开正常出价与超级数值）
        captured = {}
        run_scenario(
            {1: [6, 4, 5, 3, 3, 2]},
            super_strategies={"乙": super_by_day({1: 6})},
            capture=captured,
        )
        day2_obs = captured["乙"][1]
        assert day2_obs.my_last_super_bid == 6
        assert day2_obs.my_last_super_hit  # 命中甲的唯一 6
        # 被狙者视角：甲的正常出价 6 标记为「被狙」，按撞车扣 6
        assert captured["甲"][1].my_last_sniped
        assert captured["甲"][1].my_last_collided
        assert captured["甲"][1].my_last_deduction == 6
        # 未使用者的昨日字段为默认值
        assert captured["丙"][1].my_last_super_bid is None
        assert not captured["丙"][1].my_last_super_hit
        # 出价明细次日公开：正常出价数值 + 超级出价数值（命中与否可由明细推出）
        assert captured["丙"][1].today_history[0]["super_uses"] == [
            {"player": "乙", "value": 6, "hit": True}
        ]
        assert captured["丙"][1].today_history[0]["collision_values"] == [3, 6]
        assert captured["丙"][1].today_history[0]["bids_detail"] == [
            {"player": "甲", "bid": 6, "super_bid": None},
            {"player": "乙", "bid": 4, "super_bid": 6},
            {"player": "丙", "bid": 5, "super_bid": None},
            {"player": "丁", "bid": 3, "super_bid": None},
            {"player": "戊", "bid": 3, "super_bid": None},
            {"player": "己", "bid": 2, "super_bid": None},
        ]
