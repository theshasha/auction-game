"""单局规则引擎的边界测试。全部使用 ScriptedPlayer 构造确定性场景。

现行规则要点：每局固定 6 天，每天奖池固定 1 个（流局并入次日）；
超级出价每人每局一次，0~6 不查余额，按笔数参与撞车、不能获胜、永不扣款；
第 2 天起每天开始时公布其他人总金币与剩余超级出价总数。
"""

from engine import compute_standings, run_match, validate_bid, validate_super_bid
from players import ScriptedPlayer

NAMES = ["甲", "乙", "丙", "丁", "戊", "己"]


def run_one_match(strategies, match_index=1, is_playoff=False, super_strategies=None):
    """用给定的 {名字: 策略函数} 跑一局，返回 MatchLog。super_strategies 可选。"""
    super_strategies = super_strategies or {}
    players = [
        ScriptedPlayer(name, strategies[name], super_strategies.get(name)) for name in NAMES
    ]
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


class TestValidateSuperBid:
    """超级出价：0~6 的整数，不查余额；非法视为未使用（None）。"""

    def test_legal_super_bid(self):
        assert validate_super_bid(0) == 0
        assert validate_super_bid(6) == 6

    def test_illegal_super_bid_returns_none(self):
        assert validate_super_bid(7) is None
        assert validate_super_bid(-1) is None
        assert validate_super_bid(2.5) is None
        assert validate_super_bid("3") is None
        assert validate_super_bid(True) is None  # bool 同样非法


class TestWinningRules:
    def test_highest_unique_bid_wins(self):
        log = run_one_match(constant_strategies([5, 4, 3, 2, 1, 0]))
        day1 = log.days[0]
        assert day1.winner == "甲"
        assert day1.winning_bid == 5
        assert day1.collision_price_count == 0
        assert not day1.is_void
        assert day1.pool == 1  # 每天奖池固定 1 个胜场

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

    def test_win_at_six_takes_full_pool_no_tax(self):
        # 无冲顶税：获胜价为 6 也拿全部奖池。第 1 天流局后第 2 天奖池 2，甲唯一 6 拿满 2 个
        def strategy(obs):
            if obs.day == 1:
                return 1
            if obs.day == 2:
                return {"甲": 6, "乙": 5, "丙": 4, "丁": 3, "戊": 2, "己": 0}[obs.my_name]
            return 0

        log = run_one_match({name: strategy for name in NAMES})
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
        assert day2.pool == 2  # 第 1 天奖池 1 并入：1 + 1 = 2
        assert log.wins["乙"] == 2  # 赢家一次拿走含并入的全部奖池


class TestSuperBid:
    """超级出价：每局一次的免费额外出价（按笔数参与撞车、不能获胜、永不扣款）。"""

    def test_super_bid_collides_whale_and_sniping_is_free(self):
        # RULES.md 结算示例一：甲出 6；乙正常出 4 + 超级出价 6；丙 5；丁戊 3；己 2。
        # 6 两笔撞车（甲扣 6），3 两笔撞车（丁戊各扣 3），丙唯一 5 获胜扣 5；
        # 乙的超级出价撞车不扣款、正常出价 4 唯一未中退还——乙当天零成本
        def strategy(obs):
            if obs.day == 1:
                return {"甲": 6, "乙": 4, "丙": 5, "丁": 3, "戊": 3, "己": 2}[obs.my_name]
            return 0

        log = run_one_match(
            {name: strategy for name in NAMES},
            super_strategies={"乙": lambda obs: 6 if obs.day == 1 else None},
        )
        day1 = log.days[0]
        assert day1.winner == "丙" and day1.winning_bid == 5
        assert day1.super_users == ["乙"]
        assert day1.collision_price_count == 2  # 6 与 3 两个撞车价位

        # 乙零成本：第 2 天工资后余额 = 6 + 3 = 9（出 6 合法即为证明）；
        # 甲被狙杀扣 6：余额 0+3=3，第 2 天出 6 应非法
        def strategy_day2(obs):
            if obs.day == 1:
                return {"甲": 6, "乙": 4, "丙": 5, "丁": 3, "戊": 3, "己": 2}[obs.my_name]
            if obs.day == 2 and obs.my_name in ("甲", "乙"):
                return 6
            return 0

        log2 = run_one_match(
            {name: strategy_day2 for name in NAMES},
            super_strategies={"乙": lambda obs: 6 if obs.day == 1 else None},
        )
        record_yi = next(
            r for r in log2.days[1].bids if r.player == "乙" and not r.is_super
        )
        assert record_yi.bid == 6 and record_yi.status == "ok"  # 余额 9：第 1 天分文未扣
        # 甲被狙杀扣 6：余额 0+3=3，出 6 应非法
        record_jia = next(
            r for r in log2.days[1].bids if r.player == "甲" and not r.is_super
        )
        assert record_jia.bid == 0 and record_jia.status == "illegal"

    def test_super_bid_cannot_win_falls_through_to_normal(self):
        # 乙正常出 4 + 超级出价 6：6 是当天最高唯一出价但超级出价不能获胜，
        # 胜场顺延给唯一正常出价中最高的甲的 5；乙的超级未撞车不扣款、
        # 正常出价 4 唯一未中退还——乙当天零成本
        def strategy(obs):
            if obs.day == 1:
                return {"甲": 5, "乙": 4, "丙": 3, "丁": 2, "戊": 1, "己": 0}[obs.my_name]
            if obs.day == 2 and obs.my_name == "乙":
                return 6  # 若第 1 天分文未扣，余额 9，出 6 合法
            return 0

        log = run_one_match(
            {name: strategy for name in NAMES},
            super_strategies={"乙": lambda obs: 6 if obs.day == 1 else None},
        )
        day1 = log.days[0]
        assert day1.winner == "甲" and day1.winning_bid == 5
        assert day1.super_users == ["乙"]
        assert day1.collision_price_count == 0  # 超级出价 6 是唯一出价，未撞车

        record_yi = next(
            r for r in log.days[1].bids if r.player == "乙" and not r.is_super
        )
        assert record_yi.bid == 6 and record_yi.status == "ok"  # 余额 9：分文未扣

    def test_super_bid_ignores_balance(self):
        # 甲第 1 天出 6 获胜余额归零；第 2 天 +3 后正常出价最多出 3，
        # 但超级出价不受余额限制可以出 6
        def strategy(obs):
            if obs.day == 1:
                return 6 if obs.my_name == "甲" else 1
            return 0

        log = run_one_match(
            {name: strategy for name in NAMES},
            super_strategies={"甲": lambda obs: 6 if obs.day == 2 else None},
        )
        day2 = log.days[1]
        super_record = next(
            (r for r in day2.bids if r.player == "甲" and r.is_super), None
        )
        assert super_record is not None and super_record.bid == 6
        # 第 2 天所有人正常出 0（六笔 0 撞车），甲的超级出价 6 是唯一最高
        # 但不能获胜：没有唯一正常出价，当天流局，奖池并入第 3 天
        assert day2.is_void and day2.winner is None
        assert log.days[2].pool == 2

    def test_super_bid_once_per_match_engine_enforced(self):
        # 引擎权威去重：第 1 天已用超级出价，第 2 天再提交直接丢弃（不进日志）
        def strategy(obs):
            return 0

        log = run_one_match(
            {name: strategy for name in NAMES},
            super_strategies={"甲": lambda obs: 6},
        )
        assert log.days[0].super_users == ["甲"]
        assert log.days[1].super_users == []
        super_records_day2 = [
            r for r in log.days[1].bids if r.player == "甲" and r.is_super
        ]
        assert super_records_day2 == []

    def test_self_collision_counts_per_entry(self):
        # 同一玩家正常出价与超级出价同值：按笔数计两笔，构成撞车
        def strategy(obs):
            if obs.day == 1:
                return {"甲": 5, "乙": 4, "丙": 3, "丁": 2, "戊": 1, "己": 0}[obs.my_name]
            return 0

        log = run_one_match(
            {name: strategy for name in NAMES},
            super_strategies={"甲": lambda obs: 5 if obs.day == 1 else None},
        )
        day1 = log.days[0]
        assert day1.collision_price_count == 1  # 5 撞车（甲自己两笔）
        assert day1.winner == "乙" and day1.winning_bid == 4
        # 甲的正常出价 5 撞车扣 5（余额 1），超级出价同值同样撞车但不扣款；
        # 第二场复跑并在第 2 天捕获甲的 Observation 直接核对昨日字段
        captured = {}

        def strategy2(obs):
            if obs.day == 2:
                captured[obs.my_name] = obs  # 第 2 天的 Observation 回看第 1 天
            if obs.day == 1:
                return {"甲": 5, "乙": 4, "丙": 3, "丁": 2, "戊": 1, "己": 0}[obs.my_name]
            return 0

        run_one_match(
            {name: strategy2 for name in NAMES},
            super_strategies={"甲": lambda obs: 5 if obs.day == 1 else None},
        )
        assert captured["甲"].my_last_collided
        assert captured["甲"].my_last_deduction == 5
        assert captured["甲"].my_last_super_bid == 5
        assert captured["甲"].my_last_super_collided

    def test_super_observation_fields(self):
        # Observation 携带超级出价状态：可用/已用（含使用日）、昨日超级出价结局
        captured = {name: [] for name in NAMES}

        def strategy(obs):
            captured[obs.my_name].append(obs)
            if obs.day == 1:
                return {"甲": 6, "乙": 4, "丙": 5, "丁": 3, "戊": 3, "己": 2}[obs.my_name]
            return 0

        run_one_match(
            {name: strategy for name in NAMES},
            super_strategies={"乙": lambda obs: 6 if obs.day == 1 else None},
        )
        day1_obs = captured["乙"][0]
        assert day1_obs.my_super_available and day1_obs.my_super_used_day is None
        day2_obs = captured["乙"][1]
        assert not day2_obs.my_super_available
        assert day2_obs.my_super_used_day == 1
        assert day2_obs.my_last_super_bid == 6
        assert day2_obs.my_last_super_collided  # 与甲的 6 对撞
        # 未使用者的昨日字段为默认值
        assert captured["丙"][1].my_last_super_bid is None
        assert captured["丙"][1].my_super_available
        # 每日公布含使用情况但不数值泄露：public_view 只有名单
        assert captured["丙"][1].today_history[0]["super_users"] == ["乙"]


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

    def test_crash_winner_refund_fields(self):
        # 第 1 天：己唯一 1 获胜（奖池不滚存）；第 2 天：6、5、5、3、2、0 →
        # 甲出 6 唯一获胜扣 6（无冲顶税拿全部奖池 1 个），乙丙出 5 撞车各扣 5，
        # 丁出 3、戊出 2 唯一未中退还；第 3 天的 Observation 回看第 2 天
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
        assert (day3["丁"].my_last_bid, day3["丁"].my_last_deduction) == (3, 0)
        assert not day3["丁"].my_last_collided
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
    """流局奖池并入次日；第 6 天流局（或未拍出）则作废。每天基础奖池 1。"""

    def test_consecutive_voids_accumulate_pool(self):
        # 第 1~2 天全员出 1 撞车流局（各付 1）；第 3 天奖池 = 1 + 1 + 1 = 3
        def strategy(obs):
            if obs.day <= 2:
                return 1
            if obs.day == 3:
                return {"甲": 5, "乙": 0, "丙": 0, "丁": 0, "戊": 0, "己": 0}[obs.my_name]
            return 0

        log = run_one_match({name: strategy for name in NAMES})
        assert [day.pool for day in log.days[:3]] == [1, 2, 3]
        assert log.days[0].is_void and log.days[1].is_void
        assert log.days[2].winner == "甲"
        assert log.wins["甲"] == 3  # 第 3 天奖池 3 个胜场全部拿走

    def test_late_pool_includes_full_rollover(self):
        # 前 4 天全员出 1 撞车流局，第 5 天奖池滚到 5；差异化出价，甲唯一 6 获胜
        # 拿全部 5 个；第 6 天奖池回到 1，甲再拿 1 个
        def strategy(obs):
            if obs.day <= 4:
                return 1
            return {"甲": 6, "乙": 5, "丙": 4, "丁": 3, "戊": 2, "己": 0}[obs.my_name]

        log = run_one_match({name: strategy for name in NAMES})
        assert [day.pool for day in log.days] == [1, 2, 3, 4, 5, 1]
        day5 = log.days[4]
        assert day5.winner == "甲"
        assert day5.wins_taken == 5
        assert log.wins["甲"] == 6

    def test_last_day_void_forfeits_pool(self):
        # 6 天全流局（全员每天出 0 撞车，支付 0 无损失）：未拍出的胜场全部作废
        log = run_one_match({name: (lambda obs: 0) for name in NAMES})
        assert len(log.days) == 6
        assert all(day.is_void for day in log.days)
        assert [day.pool for day in log.days] == [1, 2, 3, 4, 5, 6]
        assert all(wins == 0 for wins in log.wins.values())


class TestFixedSixDays:
    """每局固定 6 天：不存在提前结束；每天奖池固定 1 个，第 6 天永远有货。"""

    def test_match_always_runs_six_days(self):
        # 甲每天出 1 唯一获胜，其余人出 0 撞车（支付 0 无代价）：
        # 即便甲一路通吃也打满 6 天，按每天奖池 1 共拿 6 个胜场
        def strategy(obs):
            return 1 if obs.my_name == "甲" else 0

        log = run_one_match({name: strategy for name in NAMES})
        assert len(log.days) == 6
        assert [day.pool for day in log.days] == [1, 1, 1, 1, 1, 1]
        assert all(day.winner == "甲" for day in log.days)
        assert log.wins["甲"] == 6

    def test_observation_carries_pool_and_salary(self):
        # 全员每天出 0 撞车：天天流局、无人花钱；奖池 1→…→6，工资每天 +3
        seen = []

        def strategy(obs):
            if obs.my_name == "甲":
                seen.append((obs.day, obs.today_pool, obs.my_coins, obs.my_match_wins, obs.my_total_wins))
            return 0

        run_one_match({name: strategy for name in NAMES})
        assert seen == [
            (1, 1, 6, 0, 0),
            (2, 2, 9, 0, 0),
            (3, 3, 12, 0, 0),
            (4, 4, 15, 0, 0),
            (5, 5, 18, 0, 0),
            (6, 6, 21, 0, 0),
        ]


class TestOthersInfoPublication:
    """其他人总金币与剩余超级出价总数：第 2 天起每天开始时公布（含当日工资），
    第 1 天不公布（恒定 30 与 5 无信息量）。每人观察到的都是全场减自己。"""

    def test_day1_not_published(self):
        captured = []

        def strategy(obs):
            if obs.my_name == "甲":
                captured.append(obs)
            return 0

        run_one_match({name: strategy for name in NAMES})
        assert captured[0].others_coins_total is None
        assert captured[0].others_super_remaining is None

    def test_others_coins_total_excludes_self(self):
        # 第 1 天：甲出 6 唯一获胜付 6（余额 0），其余五人出 1 撞车各付 1（余额 5）；
        # 第 2 天工资 +3：甲 3、其余 8 → 甲看到其他人总金币 5×8=40，乙看到 3+4×8=35
        captured = {name: [] for name in NAMES}

        def strategy(obs):
            captured[obs.my_name].append(obs)
            if obs.day == 1:
                return 6 if obs.my_name == "甲" else 1
            return 0

        run_one_match({name: strategy for name in NAMES})
        assert captured["甲"][1].others_coins_total == 40
        assert captured["乙"][1].others_coins_total == 35

    def test_others_super_remaining_excludes_self(self):
        # 乙第 1 天用掉超级出价：第 2 天其他人看到剩余 4，乙自己看到剩余 5
        captured = {name: [] for name in NAMES}

        def strategy(obs):
            captured[obs.my_name].append(obs)
            return 0

        run_one_match(
            {name: strategy for name in NAMES},
            super_strategies={"乙": lambda obs: 6 if obs.day == 1 else None},
        )
        assert captured["甲"][1].others_super_remaining == 4
        assert captured["乙"][1].others_super_remaining == 5


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
        assert len(data["days"]) == 6  # 固定 6 天
        first_day = data["days"][0]
        assert {b["player"]: b["bid"] for b in first_day["bids"]} == dict(
            zip(NAMES, [5, 4, 3, 2, 1, 0])
        )
        assert all(b["is_super"] is False for b in first_day["bids"])
        assert first_day["winner"] == "甲"
        assert first_day["winning_bid"] == 5
        assert first_day["pool"] == 1
        assert "winning_is_super" not in first_day  # 规则 v4 起超级出价不能获胜，字段移除
        assert first_day["super_users"] == []
        assert first_day["collision_price_count"] == 0
        assert first_day["is_void"] is False
        assert sum(data["wins"].values()) == 6  # 无流局：6 个胜场全部拍出

    def test_super_bid_visible_in_match_end_log(self):
        # 局末完整日志含超级出价的数值（对局内只公布使用情况）
        log = run_one_match(
            constant_strategies([5, 4, 3, 2, 1, 0]),
            super_strategies={"乙": lambda obs: 6 if obs.day == 1 else None},
        )
        day1 = log.to_dict()["days"][0]
        super_records = [b for b in day1["bids"] if b["is_super"]]
        assert len(super_records) == 1
        assert super_records[0]["player"] == "乙" and super_records[0]["bid"] == 6
        assert day1["super_users"] == ["乙"]
        # 超级出价 6 是唯一最高但不能获胜：胜场顺延给唯一正常价最高的甲（5）
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
        wins_history = {name: [] for name in NAMES} | {"甲": [1]}
        standings = compute_standings(wins_history, {"甲": 2})
        entry = next(e for e in standings if e.player == "甲")
        assert entry.total_wins == 3
        assert entry.wins_per_match == [1]
