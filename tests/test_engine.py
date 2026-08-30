"""单局规则引擎的边界测试：出价校验、胜负判定、金币结算、流局奖池、固定六天。
全部使用 ScriptedPlayer 构造确定性场景（场景构造器见 conftest.py）。

现行规则要点（v5）：每局固定 6 天，每天奖池固定 1 个（流局并入次日）；
超级出价每人每局 1 次机会（命中即恢复、无上限），0~6 不查余额、不能等于
当天正常出价、不能获胜、永不扣款；命中（其数值上恰好 1 笔正常出价）则该
正常出价按撞车处理，狙击者获得系统印发的 3 金币悬赏。
第 2 天起每天开始时公布其他人总金币与剩余超级出价总数。
"""

import pytest

from conftest import (
    constant_by_name,
    constant_strategies,
    run_one_match,
    run_scenario,
)
from engine import validate_bid, validate_super_bid


class TestValidateBid:
    @pytest.mark.parametrize(
        "bid, balance, expected",
        [
            (3, 6, (3, "ok")),
            (0, 6, (0, "ok")),
            (6, 6, (6, "ok")),
            (6, 3, (0, "illegal")),    # 超余额
            (7, 100, (0, "illegal")),  # 固定上限 6，余额充足也不行
            (-1, 6, (0, "illegal")),
            (2.5, 6, (0, "illegal")),  # 非整数
            ("3", 6, (0, "illegal")),
            (None, 6, (0, "illegal")),
            (True, 6, (0, "illegal")),  # bool 也视为非法
        ],
        ids=["ok-3", "ok-0", "ok-6", "over-balance", "over-limit",
             "negative", "float", "str", "none", "bool"],
    )
    def test_validate_bid(self, bid, balance, expected):
        assert validate_bid(bid, balance) == expected


class TestValidateSuperBid:
    """超级出价：0~6 的整数，不查余额，但不能等于当天正常出价；非法视为未使用（None）。"""

    @pytest.mark.parametrize(
        "super_bid, normal_bid, expected",
        [
            (0, 3, 0),
            (6, 3, 6),
            (5, 5, None),   # 等于当天正常出价：非法
            (0, 0, None),
            (7, 3, None),   # 超范围
            (-1, 3, None),
            (2.5, 3, None),
            ("3", 3, None),
            (True, 3, None),  # bool 同样非法
        ],
        ids=["ok-0", "ok-6", "equal-5", "equal-0", "over-limit",
             "negative", "float", "str", "bool"],
    )
    def test_validate_super_bid(self, super_bid, normal_bid, expected):
        assert validate_super_bid(super_bid, normal_bid) == expected


class TestWinningRules:
    def test_highest_unique_bid_wins(self):
        log = run_one_match(constant_strategies([5, 4, 3, 2, 1, 0]))
        day1 = log.days[0]
        assert day1.winner == "甲"
        assert day1.winning_bid == 5
        assert day1.collision_values == []
        assert not day1.is_void
        assert day1.pool == 1  # 每天奖池固定 1 个胜场

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

    def test_win_at_six_takes_full_pool_no_tax(self):
        # 无冲顶税：获胜价为 6 也拿全部奖池。第 1 天流局后第 2 天奖池 2，甲唯一 6 拿满 2 个
        log = run_scenario({1: [1, 1, 1, 1, 1, 1], 2: [6, 5, 4, 3, 2, 0]})
        day2 = log.days[1]
        assert day2.pool == 2
        assert day2.winner == "甲" and day2.winning_bid == 6
        assert day2.wins_taken == 2
        assert log.wins["甲"] == 2


class TestCoinSettlement:
    """金币结算：获胜的正常出价与撞车的正常出价扣除，唯一未中全额退还；
    超级出价任何结局都不扣款；扣除部分系统没收。"""

    def test_winner_pays_unique_losers_refunded(self):
        # 第 1 天全价位唯一：甲出 5 获胜付 5（余额 1）；其余人唯一未中全额退还（余额仍 6）
        # 第 2 天工资 +3：甲 4、其余 9 —— 甲出 5 非法（余额 4），乙出 6 合法（余额 9）
        log = run_scenario({1: [5, 4, 3, 2, 1, 0], 2: [5, 6, 0, 0, 0, 0]})
        day1, day2 = log.days[0], log.days[1]
        assert day1.winner == "甲" and day1.winning_bid == 5

        record_jia = next(r for r in day2.bids if r.player == "甲")
        assert record_jia.raw_bid == 5
        assert record_jia.bid == 0 and record_jia.status == "illegal"  # 余额 4 不够出 5
        record_yi = next(r for r in day2.bids if r.player == "乙")
        assert record_yi.bid == 6 and record_yi.status == "ok"  # 退还+工资后余额 9
        assert day2.winner == "乙"  # 0 撞车（甲丙丁戊己），唯一 6 获胜

    def test_colliders_pay_on_normal_day(self):
        # 第 1 天 [5,5,4,3,3,3]：丙唯一 4 获胜付 4（余额 2）；甲乙撞车各付 5（余额 1）；
        # 丁戊己撞车各付 3（余额 3）。第 2 天工资 +3：甲乙 4、丙 5、丁戊己 6
        log = run_scenario({1: [5, 5, 4, 3, 3, 3], 2: [5, 4, 5, 6, 0, 0]})
        day2 = log.days[1]
        assert log.days[0].winner == "丙"

        records = {r.player: r for r in day2.bids}
        assert records["甲"].status == "illegal"  # 余额 4 出 5 不行
        assert records["乙"].bid == 4 and records["乙"].status == "ok"   # 余额 4
        assert records["丙"].bid == 5 and records["丙"].status == "ok"   # 余额 5
        assert records["丁"].bid == 6 and records["丁"].status == "ok"   # 余额 6
        assert day2.winner == "丁" and day2.winning_bid == 6  # 0 撞车，6/5/4 中 6 最高唯一

    def test_void_day_colliders_still_pay(self):
        # 流局日撞车照扣：第 1 天全员出 6 撞车流局，各付 6（余额 0）；
        # 第 2 天工资 +3：甲出 4 非法（余额 3），乙出 3 合法且唯一获胜
        log = run_scenario({1: [6, 6, 6, 6, 6, 6], 2: [4, 3, 0, 0, 0, 0]})
        day1, day2 = log.days[0], log.days[1]
        assert day1.is_void and day1.winner is None
        assert day1.collision_values == [6]

        record_jia = next(r for r in day2.bids if r.player == "甲")
        assert record_jia.raw_bid == 4
        assert record_jia.bid == 0 and record_jia.status == "illegal"
        assert day2.winner == "乙" and day2.winning_bid == 3
        assert day2.pool == 2  # 第 1 天奖池 1 并入：1 + 1 = 2
        assert log.wins["乙"] == 2  # 赢家一次拿走含并入的全部奖池


class TestPoolRollover:
    """流局奖池并入次日；第 6 天流局（或未拍出）则作废。每天基础奖池 1。"""

    def test_consecutive_voids_accumulate_pool(self):
        # 第 1~2 天全员出 1 撞车流局（各付 1）；第 3 天奖池 = 1 + 1 + 1 = 3
        log = run_scenario({
            1: [1, 1, 1, 1, 1, 1],
            2: [1, 1, 1, 1, 1, 1],
            3: [5, 0, 0, 0, 0, 0],
        })
        assert [day.pool for day in log.days[:3]] == [1, 2, 3]
        assert log.days[0].is_void and log.days[1].is_void
        assert log.days[2].winner == "甲"
        assert log.wins["甲"] == 3  # 第 3 天奖池 3 个胜场全部拿走

    def test_late_pool_includes_full_rollover(self):
        # 前 4 天全员出 1 撞车流局，第 5 天奖池滚到 5；差异化出价，甲唯一 6 获胜
        # 拿全部 5 个；第 6 天奖池回到 1，甲再拿 1 个
        log = run_scenario({
            1: [1, 1, 1, 1, 1, 1],
            2: [1, 1, 1, 1, 1, 1],
            3: [1, 1, 1, 1, 1, 1],
            4: [1, 1, 1, 1, 1, 1],
            5: [6, 5, 4, 3, 2, 0],
            6: [6, 5, 4, 3, 2, 0],
        })
        assert [day.pool for day in log.days] == [1, 2, 3, 4, 5, 1]
        day5 = log.days[4]
        assert day5.winner == "甲"
        assert day5.wins_taken == 5
        assert log.wins["甲"] == 6

    def test_last_day_void_forfeits_pool(self):
        # 6 天全流局（全员每天出 0 撞车，支付 0 无损失）：未拍出的胜场全部作废
        log = run_one_match(constant_strategies([0, 0, 0, 0, 0, 0]))
        assert len(log.days) == 6
        assert all(day.is_void for day in log.days)
        assert [day.pool for day in log.days] == [1, 2, 3, 4, 5, 6]
        assert all(wins == 0 for wins in log.wins.values())


class TestFixedSixDays:
    """每局固定 6 天：不存在提前结束；每天奖池固定 1 个，第 6 天永远有货。"""

    def test_match_always_runs_six_days(self):
        # 甲每天出 1 唯一获胜，其余人出 0 撞车（支付 0 无代价）：
        # 即便甲一路通吃也打满 6 天，按每天奖池 1 共拿 6 个胜场
        log = run_one_match(constant_strategies([1, 0, 0, 0, 0, 0]))
        assert len(log.days) == 6
        assert [day.pool for day in log.days] == [1, 1, 1, 1, 1, 1]
        assert all(day.winner == "甲" for day in log.days)
        assert log.wins["甲"] == 6

    def test_observation_carries_pool_and_salary(self):
        # 全员每天出 0 撞车：天天流局、无人花钱；奖池 1→…→6，工资每天 +3
        captured = {}
        run_scenario({}, capture=captured)
        seen = [
            (obs.day, obs.today_pool, obs.my_coins, obs.my_match_wins, obs.my_total_wins)
            for obs in captured["甲"]
        ]
        assert seen == [
            (1, 1, 6, 0, 0),
            (2, 2, 9, 0, 0),
            (3, 3, 12, 0, 0),
            (4, 4, 15, 0, 0),
            (5, 5, 18, 0, 0),
            (6, 6, 21, 0, 0),
        ]


class TestIllegalBidInMatch:
    def test_illegal_bids_recorded_as_zero(self):
        # 甲出 7（超上限）、乙出 -1（负数）、丙出 2.5（非整数），均记 0 并撞车；
        # 丁戊出 5 撞车，己的唯一 3 获胜
        log = run_one_match(constant_by_name({"甲": 7, "乙": -1, "丙": 2.5, "丁": 5, "戊": 5, "己": 3}))
        day1 = log.days[0]

        for name in ["甲", "乙", "丙"]:
            record = next(r for r in day1.bids if r.player == name)
            assert record.bid == 0
            assert record.status == "illegal"
        assert day1.winner == "己"
        assert day1.winning_bid == 3
        assert day1.collision_values == [0, 5]  # 0 与 5 两个撞车价位

    def test_bid_over_balance_after_spending_is_illegal(self):
        # 甲第 1 天出 6 获胜、余额归零；第 2 天工资 +3 后出 6 超余额，记非法 0
        log = run_one_match(constant_strategies([6, 1, 1, 1, 1, 1]))
        day2 = log.days[1]
        record = next(r for r in day2.bids if r.player == "甲")
        assert record.raw_bid == 6
        assert record.bid == 0
        assert record.status == "illegal"
