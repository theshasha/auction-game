"""Observation 字段、每日信息公布与局末日志/排名的引擎测试。场景构造器见 conftest.py。"""

from conftest import (
    constant_strategies,
    run_one_match,
    run_scenario,
    super_by_day,
)
from engine import compute_standings


class TestMyLastDayObservation:
    """Observation 携带昨日自我结算结果（出价/扣款/撞车/非法），供提示词直接告知。"""

    def test_day1_has_no_last_day_fields(self):
        captured = {}
        run_scenario({}, capture=captured)  # 全员每天出 0
        seen = [obs for obs_list in captured.values() for obs in obs_list if obs.day == 1]
        assert len(seen) == 6  # 六人的第 1 天观察
        for obs in seen:
            assert obs.my_last_bid is None and obs.my_last_deduction is None
            assert not obs.my_last_collided and not obs.my_last_bid_illegal

    def test_crash_winner_refund_fields(self):
        # 第 1 天：己唯一 1 获胜（奖池不滚存）；第 2 天：6、5、5、3、2、0 →
        # 甲出 6 唯一获胜扣 6（无冲顶税拿全部奖池 1 个），乙丙出 5 撞车各扣 5，
        # 丁出 3、戊出 2 唯一未中退还；第 3 天的 Observation 回看第 2 天
        captured = {}
        run_scenario({1: [0, 0, 0, 0, 0, 1], 2: [6, 5, 5, 3, 2, 0]}, capture=captured)
        day3 = {name: captured[name][2] for name in captured}

        assert (day3["甲"].my_last_bid, day3["甲"].my_last_deduction) == (6, 6)
        assert not day3["甲"].my_last_collided
        assert (day3["乙"].my_last_bid, day3["乙"].my_last_deduction) == (5, 5)
        assert day3["乙"].my_last_collided
        assert (day3["丁"].my_last_bid, day3["丁"].my_last_deduction) == (3, 0)
        assert not day3["丁"].my_last_collided
        assert (day3["戊"].my_last_bid, day3["戊"].my_last_deduction) == (2, 0)
        assert not day3["戊"].my_last_collided

    def test_illegal_bid_marked_and_zero_collision_on_void_day(self):
        # 甲第 1 天出 6 获胜扣 6（余额 0）；第 2 天 +3 后出 5 非法记 0，
        # 全员出 0 撞车流局——甲的非法出价记 0 且撞车、扣款 0
        captured = {}
        log = run_scenario({1: [6, 0, 0, 0, 0, 0], 2: [5, 0, 0, 0, 0, 0]}, capture=captured)
        assert log.days[1].is_void

        day2_obs, day3_obs = captured["甲"][1], captured["甲"][2]
        assert (day2_obs.my_last_bid, day2_obs.my_last_deduction) == (6, 6)
        assert day3_obs.my_last_bid == 0 and day3_obs.my_last_bid_illegal
        assert day3_obs.my_last_deduction == 0 and day3_obs.my_last_collided


class TestAllCoinsPublication:
    """全场每人金币余额：第 2 天起每天开始时公布（含当日工资，含自己），
    第 1 天不公布（恒定每人 6 无信息量）。同一天内全场看到的快照完全相同。"""

    def test_day1_not_published(self):
        captured = {}
        run_scenario({}, capture=captured)  # 全员每天出 0
        assert captured["甲"][0].all_coins is None

    def test_all_coins_includes_self_and_same_for_everyone(self):
        # 第 1 天：甲出 6 唯一获胜付 6（余额 0），其余五人出 1 撞车各付 1（余额 5）；
        # 第 2 天工资 +3：甲 3、其余 8 —— 每人看到同一张全场余额表（含自己）
        captured = {}
        run_scenario({1: [6, 1, 1, 1, 1, 1]}, capture=captured)
        expected = {"甲": 3, "乙": 8, "丙": 8, "丁": 8, "戊": 8, "己": 8}
        assert captured["甲"][1].all_coins == expected
        assert captured["乙"][1].all_coins == expected


class TestMatchLogPublication:
    def test_match_log_to_dict_contains_full_bid_history(self):
        log = run_one_match(constant_strategies([5, 4, 3, 2, 1, 0]))
        data = log.to_dict()

        assert data["rules_version"] == "v6"  # 版本标记：analyze.py 按它分发统计口径
        assert data["match_index"] == 1
        assert data["is_playoff"] is False
        assert len(data["days"]) == 6  # 固定 6 天
        first_day = data["days"][0]
        assert first_day["bids"][0]["player"] == "甲"
        assert [b["bid"] for b in first_day["bids"]] == [5, 4, 3, 2, 1, 0]
        assert all(b["is_super"] is False for b in first_day["bids"])
        assert first_day["winner"] == "甲"
        assert first_day["winning_bid"] == 5
        assert first_day["pool"] == 1
        assert "winning_is_super" not in first_day  # 规则 v4 起超级出价不能获胜，字段移除
        assert first_day["super_uses"] == []
        assert first_day["collision_values"] == []
        assert first_day["is_void"] is False
        assert sum(data["wins"].values()) == 6  # 无流局：6 个胜场全部拍出

    def test_super_bid_visible_in_match_end_log(self):
        # 局末完整日志含超级出价的数值与命中结果（对局内次日也公开出价明细）
        log = run_one_match(
            constant_strategies([5, 4, 3, 2, 1, 0]),
            super_strategies={"乙": super_by_day({1: 6})},
        )
        day1 = log.to_dict()["days"][0]
        super_records = [b for b in day1["bids"] if b["is_super"]]
        assert len(super_records) == 1
        assert super_records[0]["player"] == "乙" and super_records[0]["bid"] == 6
        # 6 上没有正常出价 → 落空
        assert day1["super_uses"] == [{"player": "乙", "value": 6, "hit": False}]
        assert day1["winner"] == "甲" and day1["winning_bid"] == 5


class TestComputeStandings:
    def test_tied_players_share_rank(self):
        wins_history = {
            "甲": [2, 1], "乙": [1, 1], "丙": [1, 0],
            "丁": [0, 1], "戊": [0, 0], "己": [0, 0],
        }
        standings = compute_standings(wins_history)
        ranks = {entry.player: entry.rank for entry in standings}
        assert ranks["甲"] == 1
        assert ranks["乙"] == 2
        assert ranks["丙"] == 3 and ranks["丁"] == 3  # 并列同名次
        assert ranks["戊"] == 5 and ranks["己"] == 5

    def test_current_match_wins_add_to_total(self):
        # 进行中的本局已决出胜场计入总胜场，但不进各局明细
        wins_history = {name: [] for name in ["甲", "乙", "丙", "丁", "戊", "己"]} | {"甲": [1]}
        standings = compute_standings(wins_history, {"甲": 2})
        entry = next(e for e in standings if e.player == "甲")
        assert entry.total_wins == 3
        assert entry.wins_per_match == [1]
