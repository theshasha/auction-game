"""单局规则引擎的边界测试：出价校验、胜负判定、金币结算、流局奖池与参数贯通。
主要使用 ScriptedPlayer 构造确定性场景（场景构造器见 conftest.py）。

测试基准参数：每局固定 6 天，每天奖池固定 4 分（流局当天作废）；
超级出价不限次数（每天最多 1 笔），每次提交固定消耗 2 金币，0~7、不能等于
当天正常出价、与正常出价合计不得超过余额、不能获胜；命中（其数值上 ≥1 笔
正常出价）则该数值全部正常出价按撞车处理，狙击者获得系统印发的 3 金币悬赏。
引擎收到不符合规则的出价时抛出 ValueError。
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
from auction_game.game.engine import calculate_day_scores, run_match
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


class TestWinningRules:
    def test_highest_unique_bid_wins(self):
        log = run_scenario({1: [5, 4, 3, 2, 1, 0]})
        day1 = log.days[0]
        assert day1.winner == "甲"
        assert day1.winning_bid == 5
        assert day1.collision_values == []
        assert not day1.is_void
        assert day1.pool == 4
        assert day1.auction_score == 4

    def test_collision_at_top_falls_through_to_next_unique(self):
        # 5 撞车、3 三人撞车，唯一 4 获胜；撞车价位公布具体数值（3 和 5）
        log = run_scenario({1: [5, 5, 4, 3, 3, 3]})
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
        # 第 2 天工资 +3：甲 5、其余 10
        captured = {}
        log = run_scenario({1: [5, 4, 3, 2, 1, 0], 2: [5, 7, 0, 0, 0, 0]}, capture=captured)
        day1, day2 = log.days[0], log.days[1]
        assert day1.winner == "甲" and day1.winning_bid == 5
        assert captured["甲"][1].all_coins == {"甲": 5, "乙": 10, "丙": 10, "丁": 10, "戊": 10, "己": 10}
        assert day2.winner == "乙"  # 0 撞车（丙丁戊己），7 最高唯一

    def test_colliders_pay_on_normal_day(self):
        # 第 1 天 [5,5,4,3,3,3]：丙唯一 4 获胜付 4（余额 3）；甲乙撞车各付 5（余额 2）；
        # 丁戊己撞车各付 3（余额 4）。第 2 天工资 +3：甲乙 5、丙 6、丁戊己 7
        captured = {}
        log = run_scenario({1: [5, 5, 4, 3, 3, 3], 2: [5, 4, 6, 7, 0, 0]}, capture=captured)
        day2 = log.days[1]
        assert log.days[0].winner == "丙"
        assert captured["甲"][1].all_coins == {"甲": 5, "乙": 5, "丙": 6, "丁": 7, "戊": 7, "己": 7}
        assert day2.winner == "丁" and day2.winning_bid == 7  # 0 撞车，7/6/5/4 中 7 最高唯一

    def test_void_day_colliders_still_pay(self):
        # 流局日撞车照扣：第 1 天全员出 7 撞车流局，各付 7（余额 0）；
        # 第 2 天工资 +3 后全员余额 3，乙出 3 唯一获胜
        captured = {}
        log = run_scenario({1: [7, 7, 7, 7, 7, 7], 2: [0, 3, 0, 0, 0, 0]}, capture=captured)
        day1, day2 = log.days[0], log.days[1]
        assert day1.is_void and day1.winner is None
        assert day1.collision_values == [7]
        assert set(captured["甲"][1].all_coins.values()) == {3}
        assert day2.winner == "乙" and day2.winning_bid == 3
        assert day2.pool == 4
        assert log.auction_scores["乙"] == 4


class TestPoolDiscard:
    """流局当天的拍卖分作废，后续每天只使用当天配置奖池。"""

    def test_late_winner_only_receives_that_days_pool(self):
        # 前 4 天流局，第 5、6 天获胜也只能领取各自当天的奖池。
        game = replace(TEST_GAME, daily_pool=(2, 3, 4, 5, 6, 7))
        cap = TEST_GAME.max_bid
        log = run_scenario({
            1: [1, 1, 1, 1, 1, 1],
            2: [1, 1, 1, 1, 1, 1],
            3: [1, 1, 1, 1, 1, 1],
            4: [1, 1, 1, 1, 1, 1],
            5: [cap, 5, 4, 3, 2, 0],
            6: [cap, 5, 4, 3, 2, 0],
        }, game=game)
        assert [day.pool for day in log.days] == [2, 3, 4, 5, 6, 7]
        assert all(day.is_void and day.winner is None for day in log.days[:4])
        day5 = log.days[4]
        assert day5.winner == "甲" and day5.winning_bid == cap
        assert day5.auction_score == 6
        assert log.auction_scores["甲"] == 13


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

    def test_observation_carries_pool_and_salary(self):
        # 全员每天出 0 撞车：天天流局、无人花钱；每天奖池 4 分，工资每天 +3
        captured = {}
        run_scenario({}, capture=captured)
        seen = [
            (obs.day, obs.today_pool, obs.my_coins, obs.my_match_auction_score, obs.my_total_auction_score)
            for obs in captured["甲"]
        ]
        assert seen == [
            (1, 4, 7, 0, 0),
            (2, 4, 10, 0, 0),
            (3, 4, 13, 0, 0),
            (4, 4, 16, 0, 0),
            (5, 4, 19, 0, 0),
            (6, 4, 22, 0, 0),
        ]


def test_rule_breaking_bid_is_a_program_error():
    with pytest.raises(ValueError, match="甲 的出价不符合规则"):
        run_one_match(constant_by_name({"甲": 8, "乙": 0, "丙": 0, "丁": 5, "戊": 5, "己": 3}))


class TestConfiguredMatch:
    def test_production_winner_can_take_auction_and_hit_points_same_day(self):
        game = load_game_settings()
        pool = game.daily_pool[0]
        score = pool + game.super_hit_score
        captured = {}
        log = run_scenario(
            {1: [6, 6, 4, 3, 2, 1]},
            super_strategies={"丙": lambda obs: 6 if obs.day == 1 else None},
            capture=captured,
            game=game,
        )
        assert log.days[0].pool == log.days[0].auction_score == pool
        assert log.days[0].winner == "丙"
        assert log.auction_scores == {"甲": 0, "乙": 0, "丙": pool, "丁": 0, "戊": 0, "己": 0}
        assert log.scores == {"甲": 0, "乙": 0, "丙": score, "丁": 0, "戊": 0, "己": 0}
        assert captured["丙"][1].my_match_auction_score == pool
        assert captured["丙"][1].my_match_score == score

    def test_production_void_discards_pool_each_day(self):
        game = load_game_settings()
        pool = game.daily_pool[1]
        log = run_scenario({2: [1, 0, 0, 0, 0, 0]}, game=game)
        assert [day.pool for day in log.days] == list(game.daily_pool)
        assert [day.auction_score for day in log.days] == [
            pool if day == 2 else 0 for day in range(1, game.max_days + 1)
        ]
        assert log.days[0].is_void and log.days[-1].is_void
        assert log.auction_scores == {"甲": pool, "乙": 0, "丙": 0, "丁": 0, "戊": 0, "己": 0}
        assert log.scores == {"甲": pool, "乙": 0, "丙": 0, "丁": 0, "戊": 0, "己": 0}

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
        bids = {"甲": [9, 0, 5], "乙": [3, 8, 2], "丙": [2, 2, 5]}
        supers = {"乙": {1: 9, 2: 9}}
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
        # 首日甲被狙付 9；乙付正常 3、成本 4、获赏 6；丙退还正常 2。
        assert captured["甲"][1].all_coins == {"甲": 4, "乙": 12, "丙": 13}
        # 第二日乙付正常 8 和落空成本 4；第三日再加工资 2。
        assert captured["甲"][2].all_coins == {"甲": 6, "乙": 2, "丙": 15}
        assert captured["甲"][1].my_last_sniped
        assert captured["甲"][1].my_last_deduction == 9
        assert all(obs.game is game for observations in captured.values() for obs in observations)
        assert log.game is game
        assert log.to_dict()["game"] == game.to_dict()

    @pytest.mark.parametrize("is_playoff", [False, True])
    def test_custom_every_void_day_discards_its_pool(self, is_playoff):
        game = GameSettings(
            initial_coins=11, daily_salary=2, daily_pool=(2, 3, 4), max_bid=9,
            super_cost=4, super_bounty=6, super_hit_score=2,
            regular_matches=2, max_playoffs=1,
        )
        log = run_one_match(constant_strategies([0] * 6), game=game, is_playoff=is_playoff,
                            match_index=game.regular_matches + 1 if is_playoff else 1)
        assert len(log.days) == 3
        assert [day.pool for day in log.days] == [2, 3, 4]
        assert all(day.is_void and day.winner is None and day.auction_score == 0 for day in log.days)
        assert log.auction_scores == {name: 0 for name in ["甲", "乙", "丙", "丁", "戊", "己"]}
        assert log.scores == {"甲": 0, "乙": 0, "丙": 0, "丁": 0, "戊": 0, "己": 0}
