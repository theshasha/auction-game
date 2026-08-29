"""单局规则引擎的边界测试。全部使用 ScriptedPlayer 构造确定性场景。"""

from engine import compute_standings, run_match, validate_bid
from players import ScriptedPlayer

NAMES = ["甲", "乙", "丙", "丁", "戊", "己"]


def run_one_match(strategies, match_index=1, is_playoff=False):
    """用给定的 {名字: 策略函数} 跑一局，返回 MatchLog。"""
    players = [ScriptedPlayer(name, strategies[name]) for name in NAMES]
    wins_history = {name: [] for name in NAMES}
    return run_match(players, match_index, is_playoff, wins_history, [])


def constant_strategies(bids: list[int]):
    """每人每天固定出同一个价：zip(NAMES, bids)。"""
    return {name: (lambda b: lambda obs: b)(bid) for name, bid in zip(NAMES, bids)}


class TestValidateBid:
    def test_legal_bid(self):
        assert validate_bid(3, 6) == (3, "ok")
        assert validate_bid(0, 6) == (0, "ok")
        assert validate_bid(6, 6) == (6, "ok")

    def test_over_balance(self):
        assert validate_bid(6, 3) == (0, "illegal")

    def test_over_limit(self):
        assert validate_bid(7, 100) == (0, "illegal")  # 固定上限 6，余额充足也不行

    def test_negative(self):
        assert validate_bid(-1, 6) == (0, "illegal")

    def test_non_integer(self):
        assert validate_bid(2.5, 6) == (0, "illegal")
        assert validate_bid("3", 6) == (0, "illegal")
        assert validate_bid(None, 6) == (0, "illegal")
        assert validate_bid(True, 6) == (0, "illegal")  # bool 也视为非法


class TestWinningRules:
    def test_highest_unique_bid_wins(self):
        log = run_one_match(constant_strategies([5, 4, 3, 2, 1, 0]))
        day1 = log.days[0]
        assert day1.winner == "甲"
        assert day1.winning_bid == 5
        assert day1.collision_price_count == 0
        assert not day1.is_void
        assert day1.pool == 1  # 第 1 天奖池 1 个胜场

    def test_collision_at_top_falls_through_to_next_unique(self):
        # 5 撞车、3 三人撞车，唯一 4 获胜；撞车价位统计全部的（5 和 3 两个）
        log = run_one_match(constant_strategies([5, 5, 4, 3, 3, 3]))
        day1 = log.days[0]
        assert day1.winner == "丙"
        assert day1.winning_bid == 4
        assert day1.collision_price_count == 2

    def test_unique_zero_wins_when_all_positive_collide(self):
        # 3 与 2 均撞车，唯一出价 0 的"己"获胜（0 是合法价位）
        log = run_one_match(constant_strategies([3, 3, 2, 2, 2, 0]))
        day1 = log.days[0]
        assert day1.winner == "己"
        assert day1.winning_bid == 0
        assert day1.collision_price_count == 2


class TestCoinSettlement:
    """金币结算：获胜者与撞车者扣除出价，唯一未中全额退还；扣除部分系统没收。"""

    def test_winner_pays_unique_losers_refunded(self):
        # 第 1 天全价位唯一：甲出 5 获胜付 5（余额 1）；其余人唯一未中全额退还（余额仍 6）
        # 第 2 天工资 +3：甲 4、其余 9 —— 甲出 5 非法（余额 4），乙出 6 合法（余额 9）
        def strategy(obs):
            if obs.day == 1:
                return {"甲": 5, "乙": 4, "丙": 3, "丁": 2, "戊": 1, "己": 0}[obs.my_name]
            if obs.day == 2:
                return {"甲": 5, "乙": 6, "丙": 0, "丁": 0, "戊": 0, "己": 0}[obs.my_name]
            return 0

        log = run_one_match({name: strategy for name in NAMES})
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
        def strategy(obs):
            if obs.day == 1:
                return {"甲": 5, "乙": 5, "丙": 4, "丁": 3, "戊": 3, "己": 3}[obs.my_name]
            if obs.day == 2:
                return {"甲": 5, "乙": 4, "丙": 5, "丁": 6, "戊": 0, "己": 0}[obs.my_name]
            return 0

        log = run_one_match({name: strategy for name in NAMES})
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
        def strategy(obs):
            if obs.day == 1:
                return 6
            if obs.day == 2:
                return {"甲": 4, "乙": 3, "丙": 0, "丁": 0, "戊": 0, "己": 0}[obs.my_name]
            return 0

        log = run_one_match({name: strategy for name in NAMES})
        day1, day2 = log.days[0], log.days[1]
        assert day1.is_void and day1.winner is None
        assert day1.collision_price_count == 1

        record_jia = next(r for r in day2.bids if r.player == "甲")
        assert record_jia.raw_bid == 4
        assert record_jia.bid == 0 and record_jia.status == "illegal"
        assert day2.winner == "乙" and day2.winning_bid == 3
        assert day2.pool == 3  # 第 1 天奖池 1 并入：2 + 1 = 3
        assert log.wins["乙"] == 3  # 赢家一次拿走含并入的全部奖池


class TestMyLastDayObservation:
    """Observation 携带昨日自我结算结果（出价/扣款/撞车/非法），供提示词直接告知。"""

    def test_day1_has_no_last_day_fields(self):
        seen = []

        def strategy(obs):
            if obs.day == 1:
                seen.append(obs)
            return 0

        run_one_match({name: strategy for name in NAMES})
        assert len(seen) == 6
        for obs in seen:
            assert obs.my_last_bid is None and obs.my_last_deduction is None
            assert not obs.my_last_collided and not obs.my_last_bid_illegal

    def test_crash_winner_refund_and_transfer_fields(self):
        # 第 1 天：己唯一 1 获胜（奖池不滚存）；第 2 天（奖池 2）复现 RULES.md
        # 结算示例一：6、5、5、3、2、0 → 甲出 6 获胜扣 6，丁出 3 获转判扣 3，
        # 乙丙出 5 撞车各扣 5，戊出 2 唯一未中退还；第 3 天的 Observation 回看第 2 天
        captured = {name: [] for name in NAMES}

        def strategy(obs):
            captured[obs.my_name].append(obs)
            if obs.day == 1:
                return 1 if obs.my_name == "己" else 0
            if obs.day == 2:
                return {"甲": 6, "乙": 5, "丙": 5, "丁": 3, "戊": 2, "己": 0}[obs.my_name]
            return 0

        run_one_match({name: strategy for name in NAMES})
        day3 = {name: captured[name][2] for name in NAMES}

        assert (day3["甲"].my_last_bid, day3["甲"].my_last_deduction) == (6, 6)
        assert not day3["甲"].my_last_collided
        assert (day3["丁"].my_last_bid, day3["丁"].my_last_deduction) == (3, 3)
        assert not day3["丁"].my_last_collided  # 转判获得者不算撞车
        assert (day3["乙"].my_last_bid, day3["乙"].my_last_deduction) == (5, 5)
        assert day3["乙"].my_last_collided
        assert (day3["戊"].my_last_bid, day3["戊"].my_last_deduction) == (2, 0)
        assert not day3["戊"].my_last_collided

    def test_illegal_bid_marked_and_zero_collision_on_void_day(self):
        # 甲第 1 天出 6 获胜扣 6（余额 0）；第 2 天 +3 后出 5 非法记 0，
        # 全员出 0 撞车流局——甲的非法出价记 0 且撞车、扣款 0
        captured = []

        def strategy(obs):
            if obs.my_name == "甲":
                captured.append(obs)
                return {1: 6, 2: 5}.get(obs.day, 0)
            return 0

        log = run_one_match({name: strategy for name in NAMES})
        assert log.days[1].is_void

        day2_obs, day3_obs = captured[1], captured[2]
        assert (day2_obs.my_last_bid, day2_obs.my_last_deduction) == (6, 6)
        assert day3_obs.my_last_bid == 0 and day3_obs.my_last_bid_illegal
        assert day3_obs.my_last_deduction == 0 and day3_obs.my_last_collided


class TestPoolRollover:
    """流局奖池并入次日；第 5 天流局（或未拍出）则作废。"""

    def test_consecutive_voids_accumulate_pool(self):
        # 第 1~2 天全员出 1 撞车流局（各付 1）；第 3 天奖池 = 2 + 2 + 1 = 5
        def strategy(obs):
            if obs.day <= 2:
                return 1
            if obs.day == 3:
                return {"甲": 5, "乙": 0, "丙": 0, "丁": 0, "戊": 0, "己": 0}[obs.my_name]
            return 0

        log = run_one_match({name: strategy for name in NAMES})
        assert [day.pool for day in log.days[:3]] == [1, 3, 5]
        assert log.days[0].is_void and log.days[1].is_void
        assert log.days[2].winner == "甲"
        assert log.wins["甲"] == 5  # 第 3 天奖池 5 个胜场全部拿走

    def test_day5_pool_includes_full_rollover(self):
        # 前 4 天全员出 1 撞车流局，第 5 天奖池滚到 9；差异化出价，甲唯一 6 触发
        # 冲顶税与转判：甲拿 ceil(9/2)=5，转判次高唯一价 5 的乙拿 floor(9/2)=4
        def strategy(obs):
            if obs.day <= 4:
                return 1
            return {"甲": 6, "乙": 5, "丙": 4, "丁": 3, "戊": 2, "己": 0}[obs.my_name]

        log = run_one_match({name: strategy for name in NAMES})
        assert [day.pool for day in log.days] == [1, 3, 5, 7, 9]
        day5 = log.days[4]
        assert day5.winner == "甲"
        assert day5.wins_taken == 5
        assert day5.transfer_recipient == "乙"
        assert day5.transfer_wins == 4
        assert log.wins["甲"] == 5
        assert log.wins["乙"] == 4

    def test_day5_void_forfeits_pool(self):
        # 5 天全流局（全员每天出 0 撞车，支付 0 无损失）：未拍出的胜场全部作废
        log = run_one_match({name: (lambda obs: 0) for name in NAMES})
        assert len(log.days) == 5
        assert all(day.is_void for day in log.days)
        assert [day.pool for day in log.days] == [1, 3, 5, 7, 9]
        assert all(wins == 0 for wins in log.wins.values())


class TestCapTaxTransfer:
    """冲顶税与转判：获胜价为 6 时，胜者得奖池一半（向上取整），另一半（向下取整）
    转判给次高唯一出价者（照常支付出价）；无次高唯一出价者时转判部分作废。"""

    def test_rules_example_one_transfer_to_second_highest_unique(self):
        # RULES.md 结算示例一：6、5、5、3、2、0，奖池 2 → 胜者 1，转判出 3 者 1
        # 示例在奖池 2 的第 2 天复现（第 1 天奖池只有 1，转判部分为 0 无物可转）
        def strategy(obs):
            if obs.day == 1:
                return 1 if obs.my_name == "己" else 0  # 己唯一 1 获胜，奖池不滚存
            if obs.day == 2:
                return {"甲": 6, "乙": 5, "丙": 5, "丁": 3, "戊": 2, "己": 0}[obs.my_name]
            return 0

        log = run_one_match({name: strategy for name in NAMES})
        day2 = log.days[1]
        assert day2.pool == 2
        assert day2.winner == "甲" and day2.winning_bid == 6
        assert day2.wins_taken == 1
        assert day2.transfer_recipient == "丁"  # 两个 5 撞车，次高唯一价是 3
        assert day2.transfer_wins == 1
        assert day2.collision_price_count == 1
        assert log.wins["甲"] == 1
        assert log.wins["丁"] == 1

    def test_transfer_recipient_pays_bid(self):
        # 转判获得者照常支付出价（对照：唯一未中者全额退还）。临界余额链：
        # 第 2 天示例一丁出 3 被转判（付 3 后余额 6；若错按退还则为 9）；
        # 第 3、4 天丁出 6 连胜（其余人 0 撞车，丁每天付 6）；
        # 第 5 天丁出 4：扣款路径余额 6→9→3→6→3，出 4 非法记 0；
        # 退还路径余额 9→12→6→9→6，出 4 合法——status 直接区分两条路径
        def strategy(obs):
            if obs.day == 1:
                return 1 if obs.my_name == "己" else 0  # 己唯一 1 获胜，奖池不滚存
            if obs.my_name == "丁":
                return {2: 3, 3: 6, 4: 6, 5: 4}[obs.day]
            if obs.day == 2:
                return {"甲": 6, "乙": 5, "丙": 5, "戊": 2, "己": 0}[obs.my_name]
            return 0

        log = run_one_match({name: strategy for name in NAMES})
        day2 = log.days[1]
        assert day2.pool == 2
        assert day2.winner == "甲" and day2.winning_bid == 6
        assert day2.transfer_recipient == "丁" and day2.transfer_wins == 1
        assert log.days[2].winner == "丁" and log.days[3].winner == "丁"

        record = next(r for r in log.days[4].bids if r.player == "丁")
        assert record.raw_bid == 4
        assert record.bid == 0 and record.status == "illegal"  # 余额 3：转判确实扣了 3

    def test_transfer_forfeited_without_second_highest_unique(self):
        # 示例一补充：6、5、5、3、3、3 → 除 6 外全撞车，转判部分无处可转，作废
        log = run_one_match(constant_strategies([6, 5, 5, 3, 3, 3]))
        day1 = log.days[0]
        assert day1.winner == "甲" and day1.winning_bid == 6
        assert day1.wins_taken == 1
        assert day1.transfer_recipient is None
        assert day1.transfer_wins == 0
        # 第 2 天奖池仍为 2：作废的转判部分不顺延
        assert log.days[1].pool == 2

    def test_pool1_win_at_6_keeps_one_nothing_to_transfer(self):
        # 第 1 天奖池只有 1：出 6 获胜拿 ceil(1/2)=1，转判部分 floor(1/2)=0 无物可转
        def strategy(obs):
            if obs.day == 1:
                return {"甲": 6, "乙": 5, "丙": 4, "丁": 3, "戊": 2, "己": 0}[obs.my_name]
            return 0

        log = run_one_match({name: strategy for name in NAMES})
        day1 = log.days[0]
        assert day1.winner == "甲" and day1.winning_bid == 6
        assert day1.wins_taken == 1
        assert day1.transfer_recipient is None
        assert day1.transfer_wins == 0
        assert log.wins["甲"] == 1

    def test_odd_pool_splits_ceil_to_winner_floor_to_recipient(self):
        # 奇数奖池（流局并入后）：第 1 天流局，第 2 天奖池 3，
        # 甲唯一 6 获胜拿 2，转判次高唯一价 5 的乙拿 1
        def strategy(obs):
            if obs.day == 1:
                return 1
            if obs.day == 2:
                return {"甲": 6, "乙": 5, "丙": 4, "丁": 3, "戊": 2, "己": 0}[obs.my_name]
            return 0

        log = run_one_match({name: strategy for name in NAMES})
        day2 = log.days[1]
        assert day2.pool == 3
        assert day2.winner == "甲" and day2.winning_bid == 6
        assert day2.wins_taken == 2
        assert day2.transfer_recipient == "乙"
        assert day2.transfer_wins == 1
        assert log.wins["甲"] == 2
        assert log.wins["乙"] == 1

    def test_rules_example_two_top_collision_falls_through_without_tax(self):
        # RULES.md 结算示例二：6、6、4、4、2、1 → 6 与 4 撞车，唯一 2 获胜拿全部
        # 奖池（获胜价 2 在 0~5，不触发冲顶税与转判）；出 1 者唯一未中全额退还
        log = run_one_match(constant_strategies([6, 6, 4, 4, 2, 1]))
        day1 = log.days[0]
        assert day1.winner == "戊" and day1.winning_bid == 2
        assert day1.wins_taken == 1  # 第 1 天奖池 1
        assert day1.transfer_recipient is None
        assert day1.transfer_wins == 0
        assert day1.collision_price_count == 2

    def test_win_at_5_takes_full_pool(self):
        # 以 0~5 获胜无冲顶税无转判：第 1 天流局后第 2 天奖池 3，甲唯一 5 获胜拿满 3 个
        def strategy(obs):
            if obs.day == 1:
                return 1
            if obs.day == 2:
                return {"甲": 5, "乙": 4, "丙": 3, "丁": 2, "戊": 1, "己": 0}[obs.my_name]
            return 0

        log = run_one_match({name: strategy for name in NAMES})
        day2 = log.days[1]
        assert day2.pool == 3
        assert day2.winner == "甲" and day2.winning_bid == 5
        assert day2.wins_taken == 3
        assert day2.transfer_recipient is None
        assert log.wins["甲"] == 3


class TestFixedFiveDays:
    """每局固定 5 天：不存在提前结束；奖池后载保证第 5 天永远有货。"""

    def test_match_always_runs_five_days(self):
        # 甲每天出 1 唯一获胜，其余人出 0 撞车（支付 0 无代价）：
        # 即便甲一路通吃也打满 5 天，按奖池 1,2,2,2,2 共拿 9 个胜场
        def strategy(obs):
            return 1 if obs.my_name == "甲" else 0

        log = run_one_match({name: strategy for name in NAMES})
        assert len(log.days) == 5
        assert [day.pool for day in log.days] == [1, 2, 2, 2, 2]
        assert all(day.winner == "甲" for day in log.days)
        assert log.wins["甲"] == 9

    def test_observation_carries_pool_and_salary(self):
        # 全员每天出 0 撞车：天天流局、无人花钱；奖池 1→3→5→7→9，工资每天 +3
        seen = []

        def strategy(obs):
            if obs.my_name == "甲":
                seen.append((obs.day, obs.today_pool, obs.my_coins, obs.my_match_wins, obs.my_total_wins))
            return 0

        run_one_match({name: strategy for name in NAMES})
        assert seen == [
            (1, 1, 6, 0, 0),
            (2, 3, 9, 0, 0),
            (3, 5, 12, 0, 0),
            (4, 7, 15, 0, 0),
            (5, 9, 18, 0, 0),
        ]


class TestIllegalBidInMatch:
    def test_illegal_bids_recorded_as_zero(self):
        # 甲出 7（超上限）、乙出 -1（负数）、丙出 2.5（非整数），均记 0 并撞车；
        # 丁戊出 5 撞车，己的唯一 3 获胜
        raw_bids = {"甲": 7, "乙": -1, "丙": 2.5, "丁": 5, "戊": 5, "己": 3}
        strategies = {name: (lambda b: lambda obs: b)(bid) for name, bid in raw_bids.items()}
        log = run_one_match(strategies)
        day1 = log.days[0]

        for name in ["甲", "乙", "丙"]:
            record = next(r for r in day1.bids if r.player == name)
            assert record.bid == 0
            assert record.status == "illegal"
        assert day1.winner == "己"
        assert day1.winning_bid == 3
        assert day1.collision_price_count == 2  # 0 与 5 两个撞车价位

    def test_bid_over_balance_after_spending_is_illegal(self):
        # 甲第 1 天出 6 获胜、余额归零；第 2 天工资 +3 后出 6 超余额，记非法 0
        def strategy(obs):
            return 6 if obs.my_name == "甲" else 1

        log = run_one_match({name: strategy for name in NAMES})
        day2 = log.days[1]
        record = next(r for r in day2.bids if r.player == "甲")
        assert record.raw_bid == 6
        assert record.bid == 0
        assert record.status == "illegal"


class TestMatchLogPublication:
    def test_match_log_to_dict_contains_full_bid_history(self):
        log = run_one_match(constant_strategies([5, 4, 3, 2, 1, 0]))
        data = log.to_dict()

        assert data["match_index"] == 1
        assert data["is_playoff"] is False
        assert len(data["days"]) == 5  # 固定 5 天
        first_day = data["days"][0]
        assert {b["player"]: b["bid"] for b in first_day["bids"]} == dict(
            zip(NAMES, [5, 4, 3, 2, 1, 0])
        )
        assert first_day["winner"] == "甲"
        assert first_day["winning_bid"] == 5
        assert first_day["pool"] == 1
        assert first_day["transfer_recipient"] is None
        assert first_day["transfer_wins"] == 0
        assert first_day["collision_price_count"] == 0
        assert first_day["is_void"] is False
        assert sum(data["wins"].values()) == 9  # 无流局：9 个胜场全部拍出


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
        wins_history = {name: [] for name in NAMES} | {"甲": [1]}
        standings = compute_standings(wins_history, {"甲": 2})
        entry = next(e for e in standings if e.player == "甲")
        assert entry.total_wins == 3
        assert entry.wins_per_match == [1]
