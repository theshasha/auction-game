"""单局规则引擎的边界测试。全部使用 ScriptedPlayer 构造确定性场景。"""

import engine
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
        assert validate_bid(7, 100) == (0, "illegal")

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
        assert day1.voided_price_count == 0
        assert not day1.is_void

    def test_collision_at_top_falls_through_to_next_unique(self):
        # 5 撞车、3 三人撞车，唯一 4 获胜；作废价位只算高于 4 的（即 5），3 不计入
        log = run_one_match(constant_strategies([5, 5, 4, 3, 3, 3]))
        day1 = log.days[0]
        assert day1.winner == "丙"
        assert day1.winning_bid == 4
        assert day1.voided_price_count == 1

    def test_unique_zero_wins_when_all_positive_collide(self):
        # 3 与 2 均撞车，唯一出价 0 的"己"获胜
        log = run_one_match(constant_strategies([3, 3, 2, 2, 2, 0]))
        day1 = log.days[0]
        assert day1.winner == "己"
        assert day1.winning_bid == 0
        assert day1.voided_price_count == 2  # 高于 0 的撞车价位 3 和 2


class TestVoidDay:
    def test_all_collide_is_void_with_refund_and_no_dividend(self):
        def strategy(obs):
            # 第 1 天全员撞车流局；第 2 天出差异化价位
            if obs.day == 1:
                return {"甲": 2, "乙": 2, "丙": 2, "丁": 1, "戊": 1, "己": 1}[obs.my_name]
            return {"甲": 5, "乙": 4, "丙": 3, "丁": 2, "戊": 1, "己": 0}[obs.my_name]

        log = run_one_match({name: strategy for name in NAMES})
        day1, day2 = log.days[0], log.days[1]

        assert day1.is_void
        assert day1.winner is None
        assert day1.voided_price_count == 2  # 流局日统计全部撞车价位
        assert day1.dividend == 0

        # 流局全额返还：第 2 天"甲"出 5 是合法的（余额仍是 6），且"甲"获胜
        assert not day2.is_void
        assert day2.winner == "甲"
        assert day2.winning_bid == 5


class TestDividend:
    def test_dividend_is_floor_of_total_over_players(self):
        # 第 1 天总消耗 15，分红 15 // 6 = 2
        log = run_one_match(constant_strategies([5, 4, 3, 2, 1, 0]))
        assert log.days[0].dividend == 2

    def test_dividend_added_to_coins(self):
        # 第 1 天总消耗 12，分红 2。"己"出 0 获胜不扣钱，余额变为 6 + 2 = 8
        def strategy(obs):
            if obs.day == 1:
                return {"甲": 3, "乙": 3, "丙": 2, "丁": 2, "戊": 2, "己": 0}[obs.my_name]
            return 6 if obs.my_name == "己" else 0  # 己凭分红余额出 6 仍合法

        log = run_one_match({name: strategy for name in NAMES})
        day2 = log.days[1]
        assert log.days[0].dividend == 2
        record = next(r for r in day2.bids if r.player == "己")
        assert record.bid == 6 and record.status == "ok"

    def test_no_dividend_on_match_final_day_by_wins(self):
        # 甲每天出 2、其余每天出 1（余额可持续），甲连赢 3 天本局结束，第 3 天不分红
        log = run_one_match(constant_strategies([2, 1, 1, 1, 1, 1]))

        assert len(log.days) == 3
        assert log.end_reason == "wins_exhausted"
        assert log.days[0].dividend == 7 // 6
        assert log.days[1].dividend == 7 // 6
        assert log.days[2].dividend == 0  # 结束日不分红
        assert log.wins == {"甲": 3, "乙": 0, "丙": 0, "丁": 0, "戊": 0, "己": 0}

    def test_no_dividend_on_day5_even_if_wins_remain(self):
        # 前 4 天全员撞车流局，第 5 天分出胜负：仍不分红，且本局强制结束
        def strategy(obs):
            if obs.day < 5:
                return 1  # 六人全撞车
            return {"甲": 5, "乙": 4, "丙": 3, "丁": 2, "戊": 1, "己": 0}[obs.my_name]

        log = run_one_match({name: strategy for name in NAMES})
        day5 = log.days[4]
        assert len(log.days) == 5
        assert not day5.is_void
        assert day5.winner == "甲"
        assert day5.dividend == 0
        assert log.end_reason == "max_days"
        assert log.wins["甲"] == 1  # 未拍出的 2 个胜场作废


class TestMatchEnd:
    def test_all_void_for_five_days_ends_match(self):
        log = run_one_match({name: ScriptedPlayer.constant(name, 1).strategy for name in NAMES})
        assert len(log.days) == 5
        assert all(day.is_void for day in log.days)
        assert all(wins == 0 for wins in log.wins.values())
        assert log.end_reason == "max_days"

    def test_all_coins_zero_ends_match_immediately(self, monkeypatch):
        # 真实对局里全员余额归零几乎不可达（分红托底），用初始金币为 0 触发该检查分支
        monkeypatch.setattr(engine, "INITIAL_COINS", 0)
        log = run_one_match({name: (lambda obs: 0) for name in NAMES})
        assert len(log.days) == 0
        assert log.end_reason == "all_coins_zero"
        assert all(wins == 0 for wins in log.wins.values())


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

    def test_bid_over_balance_after_spending_is_illegal(self):
        # 甲第 1 天出 6 获胜，分红后余额 1；第 2 天再出 6 超余额，记非法 0
        def strategy(obs):
            if obs.my_name == "甲":
                return 6
            return 1

        log = run_one_match({name: strategy for name in NAMES})
        day2 = log.days[1]
        record = next(r for r in day2.bids if r.player == "甲")
        assert record.raw_bid == 6
        assert record.bid == 0
        assert record.status == "illegal"


class TestMatchLogPublication:
    def test_match_log_to_dict_contains_full_bid_history(self):
        day_bids = [2, 1, 1, 1, 1, 1]
        log = run_one_match(constant_strategies(day_bids))
        data = log.to_dict()

        assert data["match_index"] == 1
        assert data["is_playoff"] is False
        assert len(data["days"]) == 3
        first_day = data["days"][0]
        assert {b["player"]: b["bid"] for b in first_day["bids"]} == dict(zip(NAMES, day_bids))
        assert first_day["winner"] == "甲"
        assert data["wins"]["甲"] == 3


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
