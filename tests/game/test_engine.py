"""单局规则引擎的边界测试：出价校验、胜负判定、金币结算、流局奖池与参数贯通。
主要使用 ScriptedPlayer 构造确定性场景（场景构造器见 conftest.py）。

测试基准参数：每局固定 6 天，每天奖池固定 4 分（流局并入次日）；
超级出价不限次数（每天最多 1 笔），每次提交固定消耗 2 金币，0~7、不能等于
当天正常出价、与正常出价合计不得超过余额、不能获胜；命中（其数值上 ≥1 笔
正常出价）则该数值全部正常出价按撞车处理，狙击者获得系统印发的 3 金币悬赏。
任何非法提交整单强制为正常出价 0、无超级出价。
第 2 天起每天开始时公布昨日完整出价明细与全场每人金币余额。
"""

import random
from copy import deepcopy
from dataclasses import replace

import pytest

from tests.support.factories import (
    TEST_GAME,
    constant_by_name,
    constant_strategies,
    make_obs,
    run_one_match,
    run_scenario,
)
from auction_game.game.engine import calculate_day_scores, run_match, validate_submission
from auction_game.game.game_rules import GameSettings
from auction_game.game.players import RandomBot, ScriptedPlayer
from auction_game.configuration import load_game_settings


class TestDayScores:
    @pytest.mark.parametrize(
        "auction_score, hit_score, expected",
        [
            (3, 1, {"甲": 4, "乙": 1, "丙": 0}),
            (12, 2, {"甲": 14, "乙": 2, "丙": 0}),
            (0, 2, {"甲": 2, "乙": 2, "丙": 0}),
            (12, 0, {"甲": 12, "乙": 0, "丙": 0}),
            (0, 0, {"甲": 0, "乙": 0, "丙": 0}),
        ],
    )
    def test_pure_scores_add_auction_points_and_configured_hit_score(
        self, auction_score, hit_score, expected
    ):
        uses = [
            {"player": "甲", "value": 6, "hit": True},
            {"player": "乙", "value": 6, "hit": True},
            {"player": "丙", "value": 5, "hit": False},
        ]
        original = deepcopy(uses)
        game = replace(TEST_GAME, super_hit_score=hit_score)
        scores = calculate_day_scores(
            iter(["甲", "乙", "丙"]),
            winner="甲", auction_score=auction_score, super_uses=uses, game=game,
        )
        assert scores == expected
        assert uses == original
        scores["乙"] = 99
        assert calculate_day_scores(
            ["甲", "乙", "丙"],
            winner="甲", auction_score=auction_score, super_uses=uses, game=game,
        ) == expected


class TestValidateSubmission:
    """整单校验：任何非法提交一律整单作废为（正常出价 0, 无超级出价）。"""

    @pytest.mark.parametrize(
        "bid, super_bid, balance, expected",
        [
            (3, None, 6, (3, "ok", None)),
            (8, None, 100, (0, "illegal", None)),  # 正常出价非法，保留整单审计状态
            (3, 5, 7, (3, "ok", 5)),      # 合法超级：3 + 2 ≤ 7
            (5, 5, 7, (0, "illegal", None)),  # 同值：整单作废
            (4, 5, 5, (0, "illegal", None)),   # 4 + 2 > 5：总耗资超余额
        ],
        ids=["normal-ok", "normal-illegal", "super-ok", "super-same-value", "total-over-balance"],
    )
    def test_validate_submission(self, bid, super_bid, balance, expected):
        assert validate_submission(bid, super_bid, balance, TEST_GAME) == expected


class TestWinningRules:
    def test_highest_unique_bid_wins(self):
        log = run_one_match(constant_strategies([5, 4, 3, 2, 1, 0]))
        day1 = log.days[0]
        assert day1.winner == "甲"
        assert day1.winning_bid == 5
        assert day1.collision_values == []
        assert not day1.is_void
        assert day1.pool == 4
        assert day1.auction_score == 4

    def test_collision_at_top_falls_through_to_next_unique(self):
        # 5 撞车、3 三人撞车，唯一 4 获胜；撞车价位公布具体数值（3 和 5）
        log = run_one_match(constant_strategies([5, 5, 4, 3, 3, 3]))
        day1 = log.days[0]
        assert day1.winner == "丙"
        assert day1.winning_bid == 4
        assert day1.collision_values == [3, 5]

    def test_unique_zero_wins_when_all_positive_collide(self):
        # 3 与 2 均撞车，唯一出价 0 的"己"获胜（0 是合法价位）
        log = run_one_match(constant_strategies([3, 3, 2, 2, 2, 0]))
        day1 = log.days[0]
        assert day1.winner == "己"
        assert day1.winning_bid == 0
        assert day1.collision_values == [2, 3]


class TestCoinSettlement:
    """金币结算：获胜的正常出价与撞车的正常出价扣除，唯一未中全额退还；
    超级出价每次提交固定扣 2 金币成本；扣除部分系统没收。"""

    def test_winner_pays_unique_losers_refunded(self):
        # 第 1 天全价位唯一：甲出 5 获胜付 5（余额 2）；其余人唯一未中全额退还（余额仍 7）
        # 第 2 天工资 +3：甲 5、其余 10 —— 甲出 6 非法（余额 5），乙出 7 合法（余额 10）
        log = run_scenario({1: [5, 4, 3, 2, 1, 0], 2: [6, 7, 0, 0, 0, 0]})
        day1, day2 = log.days[0], log.days[1]
        assert day1.winner == "甲" and day1.winning_bid == 5

        record_jia = next(r for r in day2.bids if r.player == "甲")
        assert record_jia.raw_bid == 6
        assert record_jia.bid == 0 and record_jia.status == "illegal"  # 余额 5 不够出 6
        record_yi = next(r for r in day2.bids if r.player == "乙")
        assert record_yi.bid == 7 and record_yi.status == "ok"  # 退还+工资后余额 10
        assert day2.winner == "乙"  # 0 撞车（甲丙丁戊己），唯一 7 获胜

    def test_colliders_pay_on_normal_day(self):
        # 第 1 天 [5,5,4,3,3,3]：丙唯一 4 获胜付 4（余额 3）；甲乙撞车各付 5（余额 2）；
        # 丁戊己撞车各付 3（余额 4）。第 2 天工资 +3：甲乙 5、丙 6、丁戊己 7
        log = run_scenario({1: [5, 5, 4, 3, 3, 3], 2: [6, 4, 6, 7, 0, 0]})
        day2 = log.days[1]
        assert log.days[0].winner == "丙"

        records = {r.player: r for r in day2.bids}
        assert records["甲"].status == "illegal"  # 余额 5 出 6 不行
        assert records["乙"].bid == 4 and records["乙"].status == "ok"   # 余额 5
        assert records["丙"].bid == 6 and records["丙"].status == "ok"   # 余额 6
        assert records["丁"].bid == 7 and records["丁"].status == "ok"   # 余额 7
        assert day2.winner == "丁" and day2.winning_bid == 7  # 0 撞车，7/6/4 中 7 最高唯一

    def test_void_day_colliders_still_pay(self):
        # 流局日撞车照扣：第 1 天全员出 7 撞车流局，各付 7（余额 0）；
        # 第 2 天工资 +3：甲出 4 非法（余额 3），乙出 3 合法且唯一获胜
        log = run_scenario({1: [7, 7, 7, 7, 7, 7], 2: [4, 3, 0, 0, 0, 0]})
        day1, day2 = log.days[0], log.days[1]
        assert day1.is_void and day1.winner is None
        assert day1.collision_values == [7]

        record_jia = next(r for r in day2.bids if r.player == "甲")
        assert record_jia.raw_bid == 4
        assert record_jia.bid == 0 and record_jia.status == "illegal"
        assert day2.winner == "乙" and day2.winning_bid == 3
        assert day2.pool == 8  # 第 1 天奖池 4 分并入：4 + 4 = 8
        assert log.auction_scores["乙"] == 8


class TestPoolRollover:
    """流局奖池并入次日；第 6 天流局（或未拍出）则作废。每天基础奖池 4 分。"""

    def test_late_pool_includes_full_rollover(self):
        # 前 4 天流局，第 5 天唯一上限价获胜，领取全部滚存奖池 20 分；
        # 第 6 天奖池重置为 4 分。
        cap = TEST_GAME.max_bid
        log = run_scenario({
            1: [1, 1, 1, 1, 1, 1],
            2: [1, 1, 1, 1, 1, 1],
            3: [1, 1, 1, 1, 1, 1],
            4: [1, 1, 1, 1, 1, 1],
            5: [cap, 5, 4, 3, 2, 0],
            6: [cap, 5, 4, 3, 2, 0],
        })
        assert [day.pool for day in log.days] == [4, 8, 12, 16, 20, 4]
        assert all(day.is_void and day.winner is None for day in log.days[:4])
        day5 = log.days[4]
        assert day5.winner == "甲" and day5.winning_bid == cap
        assert day5.auction_score == 20
        assert log.auction_scores["甲"] == 24


class TestFixedSixDays:
    """测试每局固定 6 天：不存在提前结束；第 6 天永远有货。"""

    def test_match_always_runs_six_days(self):
        # 甲每天出 1 唯一获胜，其余人出 0 撞车（支付 0 无代价）：
        # 即便甲一路通吃也打满 6 天，共拿 24 分
        log = run_one_match(constant_strategies([1, 0, 0, 0, 0, 0]))
        assert len(log.days) == 6
        assert [day.pool for day in log.days] == [4, 4, 4, 4, 4, 4]
        assert all(day.winner == "甲" for day in log.days)
        assert log.auction_scores["甲"] == 24
        assert all(record.status == "ok" for day in log.days for record in day.bids)

    def test_observation_carries_pool_and_salary(self):
        # 全员每天出 0 撞车：天天流局、无人花钱；奖池 4→…→24，工资每天 +3
        captured = {}
        run_scenario({}, capture=captured)
        seen = [
            (obs.day, obs.today_pool, obs.my_coins, obs.my_match_auction_score, obs.my_total_auction_score)
            for obs in captured["甲"]
        ]
        assert seen == [
            (1, 4, 7, 0, 0),
            (2, 8, 10, 0, 0),
            (3, 12, 13, 0, 0),
            (4, 16, 16, 0, 0),
            (5, 20, 19, 0, 0),
            (6, 24, 22, 0, 0),
        ]


class TestIllegalBidInMatch:
    def test_illegal_bids_recorded_as_zero(self):
        # 甲出 8（超上限）、乙出 -1（负数）、丙出 2.5（非整数），均记 0 并撞车；
        # 丁戊出 5 撞车，己的唯一 3 获胜
        log = run_one_match(constant_by_name({"甲": 8, "乙": -1, "丙": 2.5, "丁": 5, "戊": 5, "己": 3}))
        day1 = log.days[0]

        for name in ["甲", "乙", "丙"]:
            record = next(r for r in day1.bids if r.player == name)
            assert record.bid == 0
            assert record.status == "illegal"
        assert day1.winner == "己"
        assert day1.winning_bid == 3
        assert day1.collision_values == [0, 5]  # 0 与 5 两个撞车价位


class TestConfiguredMatch:
    def test_production_winner_can_take_three_points_and_one_hit_point_same_day(self):
        game = load_game_settings()
        assert game.daily_pool == (3, 3, 3, 3, 3)
        assert game.super_hit_score == 1
        assert game.auction_pool_per_match == 15
        captured = {}
        log = run_scenario(
            {1: [6, 6, 4, 3, 2, 1]},
            super_strategies={"丙": lambda obs: 6 if obs.day == 1 else None},
            capture=captured,
            game=game,
        )
        assert log.days[0].pool == log.days[0].auction_score == 3
        assert log.days[0].winner == "丙"
        assert log.auction_scores == {"甲": 0, "乙": 0, "丙": 3, "丁": 0, "戊": 0, "己": 0}
        assert log.scores == {"甲": 0, "乙": 0, "丙": 4, "丁": 0, "戊": 0, "己": 0}
        assert captured["丙"][1].my_match_auction_score == 3
        assert captured["丙"][1].my_match_score == 4

    def test_production_void_rolls_three_points_into_six_point_auction(self):
        log = run_scenario({2: [1, 0, 0, 0, 0, 0]}, game=load_game_settings())
        assert [day.pool for day in log.days] == [3, 6, 3, 6, 9]
        assert [day.auction_score for day in log.days] == [0, 6, 0, 0, 0]
        assert log.days[0].is_void and log.days[-1].is_void
        assert log.auction_scores == {"甲": 6, "乙": 0, "丙": 0, "丁": 0, "戊": 0, "己": 0}
        assert log.scores == {"甲": 6, "乙": 0, "丙": 0, "丁": 0, "戊": 0, "己": 0}

    def test_random_bot_uses_observed_limit_and_super_cost(self):
        game = GameSettings(
            initial_coins=11, daily_salary=2, daily_pool=(2, 3, 4), max_bid=9,
            super_cost=4, super_bounty=6, super_hit_score=2,
            regular_matches=2, max_playoffs=1,
        )
        obs = make_obs(coins=11, game=game)
        bot = RandomBot("甲", random.Random(42), super_chance=1)
        submissions = [bot.decide_bid(obs) for _ in range(100)]

        assert any(bid == 9 or super_bid == 9 for bid, super_bid in submissions)
        for bid, super_bid in submissions:
            assert 0 <= bid <= 9
            if super_bid is not None:
                assert 0 <= super_bid <= 9
                assert super_bid != bid
                assert bid + 4 <= 11

    def test_three_players_custom_economy_and_unequal_daily_pool(self):
        game = GameSettings(
            initial_coins=11, daily_salary=2, daily_pool=(2, 3, 4), max_bid=9,
            super_cost=4, super_bounty=6, super_hit_score=2,
            regular_matches=2, max_playoffs=1,
        )
        names = ["甲", "乙", "丙"]
        bids = {"甲": [9, 4, 5], "乙": [3, 8, 2], "丙": [2, 2, 5]}
        supers = {"甲": {2: 8}, "乙": {1: 9, 2: 9}}
        captured = {name: [] for name in names}

        def strategy(obs):
            captured[obs.my_name].append(obs)
            return bids[obs.my_name][obs.day - 1]

        players = [
            ScriptedPlayer(name, strategy, lambda obs: supers.get(obs.my_name, {}).get(obs.day))
            for name in names
        ]
        log = run_match(
            players, 1, False, {name: [] for name in names}, [], game,
            scores_history={name: [] for name in names},
        )

        assert len(log.days) == 3
        assert [day.pool for day in log.days] == [2, 3, 4]
        assert [day.winner for day in log.days] == ["乙", "乙", "乙"]
        assert log.auction_scores == {"甲": 0, "乙": 9, "丙": 0}
        assert log.scores == {"甲": 0, "乙": 11, "丙": 0}
        assert captured["乙"][1].my_match_score == 4
        assert captured["乙"][2].my_match_score == 7
        assert log.days[0].super_uses == [{"player": "乙", "value": 9, "hit": True}]
        assert log.days[1].super_uses == [{"player": "乙", "value": 9, "hit": False}]
        assert log.days[1].bids[0].status == "illegal"  # 甲余额 4，正常 4 加成本 4 超余额
        # 首日甲被狙付 9；乙付正常 3、成本 4、获赏 6；丙退还正常 2。
        assert captured["甲"][1].all_coins == {"甲": 4, "乙": 12, "丙": 13}
        # 第二日乙付正常 8 和落空成本 4；第三日再加工资 2。
        assert captured["甲"][2].all_coins == {"甲": 6, "乙": 2, "丙": 15}
        assert captured["甲"][1].my_last_sniped
        assert captured["甲"][1].my_last_deduction == 9
        assert all(obs.game is game for observations in captured.values() for obs in observations)
        assert log.game is game
        assert log.to_dict()["game"] == game.to_dict()

    def test_custom_last_day_void_discards_accumulated_pool(self):
        game = GameSettings(
            initial_coins=11, daily_salary=2, daily_pool=(2, 3, 4), max_bid=9,
            super_cost=4, super_bounty=6, super_hit_score=2,
            regular_matches=2, max_playoffs=1,
        )
        log = run_one_match(constant_strategies([0] * 6), game=game)
        assert len(log.days) == 3
        assert [day.pool for day in log.days] == [2, 5, 9]
        assert all(day.is_void and day.winner is None and day.auction_score == 0 for day in log.days)
        assert log.auction_scores == {name: 0 for name in ["甲", "乙", "丙", "丁", "戊", "己"]}
        assert log.scores == {"甲": 0, "乙": 0, "丙": 0, "丁": 0, "戊": 0, "己": 0}
