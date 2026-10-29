"""Observation 字段、每日信息公布与局末日志/排名的引擎测试。"""

from dataclasses import replace

from tests.support.factories import (
    TEST_GAME,
    ScriptedPlayer,
    constant_strategies,
    run_one_match,
    run_scenario,
    super_by_day,
)
from auction_game.game.engine import compute_standings, run_match


class TestDayHistoryObservation:
    """Observation 带本局已结束各天的全员结算，提示从中取本人昨日的结果。"""

    def test_day1_has_no_history(self):
        captured = {}
        run_scenario({}, capture=captured)  # 全员每天出 0
        seen = [obs for obs_list in captured.values() for obs in obs_list if obs.day == 1]
        assert len(seen) == 6  # 六人的第 1 天观察
        assert all(obs.today_history == [] and obs.match_index == 1 for obs in seen)

    def test_crash_winner_refund_results(self):
        # 第 1 天：己唯一 1 获胜；第 2 天：6、5、5、3、2、0 →
        # 甲出 6 唯一获胜扣 6（拿全部奖池 4 分），乙丙出 5 撞车各扣 5，
        # 丁出 3、戊出 2 唯一未中退还；第 3 天的 Observation 回看第 2 天
        captured = {}
        log = run_scenario({1: [0, 0, 0, 0, 0, 1], 2: [6, 5, 5, 3, 2, 0]}, capture=captured)
        history = captured["丁"][2].today_history
        assert history == log.days[:2]
        assert [(result.player, result.outcome, result.coins_delta) for result in history[-1].players] == [
            ("甲", "won", -6), ("乙", "collided", -5), ("丙", "collided", -5),
            ("丁", "refunded", 0), ("戊", "refunded", 0), ("己", "refunded", 0),
        ]

    def test_zero_collision_on_void_day(self):
        # 甲第 1 天出 7 获胜扣 7；第 2 天全员出 0 撞车流局——甲撞车、扣款 0
        captured = {}
        log = run_scenario({1: [7, 0, 0, 0, 0, 0], 2: [0, 0, 0, 0, 0, 0]}, capture=captured)
        assert log.days[1].is_void

        day2_obs, day3_obs = captured["甲"][1], captured["甲"][2]
        assert day2_obs.today_history[-1].result_of("甲").coins_delta == -7
        mine = day3_obs.today_history[-1].result_of("甲")
        assert (mine.bid, mine.outcome, mine.coins_delta) == (0, "collided", 0)


class TestAllCoinsPublication:
    """全场每人金币余额：第 2 天起每天开始时公布（含当日工资，含自己），
    第 1 天不公布（本组参数恒定每人 7 无信息量）。同一天内全场看到的快照完全相同。"""

    def test_day1_not_published(self):
        captured = {}
        run_scenario({}, capture=captured)  # 全员每天出 0
        assert captured["甲"][0].all_coins is None

    def test_all_coins_includes_self_and_same_for_everyone(self):
        # 第 1 天：甲出 6 唯一获胜付 6（余额 1），其余五人出 1 撞车各付 1（余额 6）；
        # 第 2 天工资 +3：甲 4、其余 9 —— 每人看到同一张全场余额表（含自己）
        captured = {}
        run_scenario({1: [6, 1, 1, 1, 1, 1]}, capture=captured)
        expected = {"甲": 4, "乙": 9, "丙": 9, "丁": 9, "戊": 9, "己": 9}
        for name, observations in captured.items():
            assert observations[1].all_coins == expected, name
            assert observations[1].my_coins == expected[name]


class TestMatchLogPublication:
    def test_game_snapshot_stays_bound_to_match_and_is_independently_serialized(self):
        game = replace(TEST_GAME, initial_coins=11, daily_pool=(2, 3, 4), max_bid=9)
        log = run_one_match(constant_strategies([0] * 6), game=game)
        snapshot = log.to_dict()
        snapshot["game"]["initial_coins"] = 0
        snapshot["game"]["daily_pool"][0] = 99

        assert log.to_dict()["game"] == {
            "initial_coins": 11,
            "daily_salary": 3,
            "daily_pool": [2, 3, 4],
            "max_bid": 9,
            "super_cost": 2,
            "super_bounty": 3,
            "super_hit_score": 2,
            "regular_matches": 5,
            "max_playoffs": 20,
        }

    def test_match_log_to_dict_contains_full_bid_history(self):
        later = [1, 0, 0, 0, 0, 0]  # 甲之后每天唯一获胜
        log = run_scenario({1: [5, 4, 3, 2, 1, 0], **{day: later for day in range(2, 7)}})
        data = log.to_dict()

        assert data["game"] == TEST_GAME.to_dict()
        assert data["match_index"] == 1
        assert data["is_playoff"] is False
        assert len(data["days"]) == 6  # 固定 6 天
        first_day = data["days"][0]
        assert first_day["players"][0] == {
            "player": "甲", "bid": 5, "super_bid": None, "outcome": "won", "sniped": False,
            "super_hit": False, "coins_delta": -5, "score": 4,
        }
        assert [result["bid"] for result in first_day["players"]] == [5, 4, 3, 2, 1, 0]
        assert [result["outcome"] for result in first_day["players"][1:]] == ["refunded"] * 5
        assert first_day["winner"] == "甲"
        assert first_day["winning_bid"] == 5
        assert first_day["pool"] == 4
        assert sum(data["auction_scores"].values()) == 24
        assert data["scores"] == log.scores
        assert sum(data["scores"].values()) == 24

    def test_super_bid_visible_in_match_end_log(self):
        # 局末完整日志含超级出价的数值与命中结果（对局内次日也公开出价明细）
        log = run_scenario({1: [5, 4, 3, 2, 1, 0]}, super_strategies={"乙": super_by_day({1: 6})})
        day1 = log.to_dict()["days"][0]
        # 6 上没有正常出价 → 落空
        assert [(result["player"], result["super_bid"], result["super_hit"])
                for result in day1["players"] if result["super_bid"] is not None] == [("乙", 6, False)]
        assert day1["winner"] == "甲" and day1["winning_bid"] == 5


class TestScoreObservations:
    def test_live_scores_include_history_and_standings_snapshots_stay_fixed(self):
        game = replace(TEST_GAME, daily_pool=(5, 5, 5), super_hit_score=3)
        names = ["甲", "乙", "丙"]
        auction_scores_history = {"甲": [3], "乙": [1], "丙": [0]}
        scores_history = {"甲": [4], "乙": [6], "丙": [0]}
        captured = {name: [] for name in names}
        bids = {"甲": [4, 0, 0], "乙": [2, 1, 0], "丙": [0, 0, 0]}

        def strategy(obs):
            captured[obs.my_name].append(obs)
            return bids[obs.my_name][obs.day - 1]

        players = [
            ScriptedPlayer(name, strategy, super_by_day({1: 4, 2: 0}) if name == "乙" else None)
            for name in names
        ]
        log = run_match(
            players, 2, False, auction_scores_history, game, scores_history=scores_history,
        )
        assert all(obs.match_index == 2 for observations in captured.values() for obs in observations)
        assert log.auction_scores == {"甲": 0, "乙": 10, "丙": 0}
        assert log.scores == {"甲": 0, "乙": 16, "丙": 0}
        assert [(obs.my_match_score, obs.my_total_score) for obs in captured["乙"]] == [
            (0, 6), (8, 14), (16, 22),
        ]
        assert [(obs.my_match_auction_score, obs.my_total_auction_score) for obs in captured["乙"]] == [
            (0, 1), (5, 6), (10, 11),
        ]
        for day, expected_score in enumerate([6, 14, 22]):
            snapshot = captured["甲"][day].standings
            assert [(entry.player, entry.total_score) for entry in snapshot] == [
                ("乙", expected_score), ("甲", 4), ("丙", 0),
            ]
            assert snapshot[0].scores_per_match == [6]
            assert snapshot[0].auction_scores_per_match == [1]
        assert auction_scores_history == {"甲": [3], "乙": [1], "丙": [0]}
        assert scores_history == {"甲": [4], "乙": [6], "丙": [0]}
        scores_history["乙"].append(99)
        assert captured["甲"][0].standings[0].scores_per_match == [6]


class TestComputeStandings:
    def test_tied_players_share_rank(self):
        auction_scores_history = {
            "甲": [2, 1], "乙": [1, 1], "丙": [1, 0],
            "丁": [0, 1], "戊": [0, 0], "己": [0, 0],
        }
        scores_history = {
            "甲": [8, 4], "乙": [4, 4], "丙": [4, 0],
            "丁": [0, 4], "戊": [0, 0], "己": [0, 0],
        }
        standings = compute_standings(auction_scores_history, scores_history=scores_history)
        ranks = {entry.player: entry.rank for entry in standings}
        assert ranks["甲"] == 1
        assert ranks["乙"] == 2
        assert ranks["丙"] == 3 and ranks["丁"] == 3  # 并列同名次
        assert ranks["戊"] == 5 and ranks["己"] == 5

    def test_current_match_scores_add_only_to_totals(self):
        auction_scores_history = {name: [] for name in ["甲", "乙", "丙", "丁", "戊", "己"]} | {"甲": [1]}
        scores_history = {name: [] for name in auction_scores_history} | {"甲": [4]}
        standings = compute_standings(
            auction_scores_history, scores_history=scores_history,
            current_match_auction_scores={"甲": 2}, current_match_scores={"甲": 10},
        )
        entry = next(e for e in standings if e.player == "甲")
        assert entry.total_auction_score == 3
        assert entry.auction_scores_per_match == [1]
        assert entry.total_score == 14
        assert entry.scores_per_match == [4]

    def test_total_scores_reverse_auction_order_and_auction_scores_do_not_break_ties(self):
        standings = compute_standings(
            {"Charlie": [1], "Alpha": [3], "Bravo": [2], "Delta": [0]},
            scores_history={"Charlie": [10], "Alpha": [9], "Bravo": [10], "Delta": [0]},
        )
        assert [(entry.player, entry.total_auction_score, entry.total_score, entry.rank) for entry in standings] == [
            ("Bravo", 2, 10, 1),
            ("Charlie", 1, 10, 1),
            ("Alpha", 3, 9, 3),
            ("Delta", 0, 0, 4),
        ]
