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
        assert day2.pool == 2  # 第 1 天奖池 1 并入：1 + 1 = 2
        assert log.wins["乙"] == 2  # 赢家一次拿走含并入的全部奖池


class TestPoolRollover:
    """流局奖池并入次日；第 5 天流局（或未拍出）则作废。"""

    def test_consecutive_voids_accumulate_pool(self):
        # 第 1~2 天全员出 1 撞车流局（各付 1）；第 3 天奖池 = 2 + 1 + 1 = 4
        def strategy(obs):
            if obs.day <= 2:
                return 1
            if obs.day == 3:
                return {"甲": 5, "乙": 0, "丙": 0, "丁": 0, "戊": 0, "己": 0}[obs.my_name]
            return 0

        log = run_one_match({name: strategy for name in NAMES})
        assert [day.pool for day in log.days[:3]] == [1, 2, 4]
        assert log.days[0].is_void and log.days[1].is_void
        assert log.days[2].winner == "甲"
        assert log.wins["甲"] == 4  # 第 3 天奖池 4 个胜场全部拿走

    def test_day5_pool_includes_full_rollover(self):
        # 前 4 天全员出 1 撞车流局，第 5 天奖池滚到 8；差异化出价，甲唯一 6 全拿——
        # 但 6 触发冲顶税：8 - 1 = 7，被扣的 1 个胜场作废
        def strategy(obs):
            if obs.day <= 4:
                return 1
            return {"甲": 6, "乙": 5, "丙": 4, "丁": 3, "戊": 2, "己": 0}[obs.my_name]

        log = run_one_match({name: strategy for name in NAMES})
        assert [day.pool for day in log.days] == [1, 2, 4, 6, 8]
        assert log.days[4].winner == "甲"
        assert log.days[4].wins_taken == 7
        assert log.wins["甲"] == 7

    def test_day5_void_forfeits_pool(self):
        # 5 天全流局（全员每天出 0 撞车，支付 0 无损失）：未拍出的胜场全部作废
        log = run_one_match({name: (lambda obs: 0) for name in NAMES})
        assert len(log.days) == 5
        assert all(day.is_void for day in log.days)
        assert [day.pool for day in log.days] == [1, 2, 4, 6, 8]
        assert all(wins == 0 for wins in log.wins.values())


class TestCapTax:
    """冲顶税：获胜价为 6 时，获胜者获得奖池数减 1（最低 0），扣除的 1 个作废。"""

    def test_pool1_win_at_6_yields_zero(self):
        # 第 1 天奖池只有 1：出 6 获胜 = 付 6 金币拿 0 个胜场（1-1=0），纯陷阱
        def strategy(obs):
            if obs.day == 1:
                return {"甲": 6, "乙": 5, "丙": 4, "丁": 3, "戊": 2, "己": 0}[obs.my_name]
            return 0

        log = run_one_match({name: strategy for name in NAMES})
        day1 = log.days[0]
        assert day1.winner == "甲" and day1.winning_bid == 6
        assert day1.wins_taken == 0
        assert log.wins["甲"] == 0
        # 被作废的 1 个胜场不顺延：第 2 天奖池仍是 1
        assert log.days[1].pool == 1

    def test_pool2_win_at_6_yields_one(self):
        # 第 1 天全员出 1 撞车流局；第 2 天奖池 2，甲唯一 6 获胜 → 税后 1 个胜场
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
        assert day2.wins_taken == 1
        assert log.wins["甲"] == 1

    def test_jackpot_pool8_win_at_6_yields_seven(self):
        # 前 4 天全流局奖池滚到 8，第 5 天出 6 获胜 → 税后 7（大奖池时 6 仍有吸引力）
        def strategy(obs):
            if obs.day <= 4:
                return 1
            return {"甲": 6, "乙": 5, "丙": 4, "丁": 3, "戊": 2, "己": 0}[obs.my_name]

        log = run_one_match({name: strategy for name in NAMES})
        assert log.days[4].pool == 8
        assert log.days[4].wins_taken == 7
        assert log.wins["甲"] == 7

    def test_win_at_5_takes_full_pool(self):
        # 以 0~5 获胜不缴税：第 1 天流局后第 2 天奖池 2，甲唯一 5 获胜拿满 2 个
        def strategy(obs):
            if obs.day == 1:
                return 1
            if obs.day == 2:
                return {"甲": 5, "乙": 4, "丙": 3, "丁": 2, "戊": 1, "己": 0}[obs.my_name]
            return 0

        log = run_one_match({name: strategy for name in NAMES})
        day2 = log.days[1]
        assert day2.pool == 2
        assert day2.winner == "甲" and day2.winning_bid == 5
        assert day2.wins_taken == 2
        assert log.wins["甲"] == 2


class TestFixedFiveDays:
    """每局固定 5 天：不存在提前结束；奖池后载保证第 5 天永远有货。"""

    def test_match_always_runs_five_days(self):
        # 甲每天出 1 唯一获胜，其余人出 0 撞车（支付 0 无代价）：
        # 即便甲一路通吃也打满 5 天，按奖池 1,1,2,2,2 共拿 8 个胜场
        def strategy(obs):
            return 1 if obs.my_name == "甲" else 0

        log = run_one_match({name: strategy for name in NAMES})
        assert len(log.days) == 5
        assert [day.pool for day in log.days] == [1, 1, 2, 2, 2]
        assert all(day.winner == "甲" for day in log.days)
        assert log.wins["甲"] == 8

    def test_observation_carries_pool_and_salary(self):
        # 全员每天出 0 撞车：天天流局、无人花钱；奖池 1→2→4→6→8，工资每天 +3
        seen = []

        def strategy(obs):
            if obs.my_name == "甲":
                seen.append((obs.day, obs.today_pool, obs.my_coins, obs.my_match_wins, obs.my_total_wins))
            return 0

        run_one_match({name: strategy for name in NAMES})
        assert seen == [
            (1, 1, 6, 0, 0),
            (2, 2, 9, 0, 0),
            (3, 4, 12, 0, 0),
            (4, 6, 15, 0, 0),
            (5, 8, 18, 0, 0),
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
        assert first_day["collision_price_count"] == 0
        assert first_day["is_void"] is False
        assert sum(data["wins"].values()) == 8  # 无流局：8 个胜场全部拍出


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
