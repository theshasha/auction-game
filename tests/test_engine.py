"""单局规则引擎的边界测试。全部使用 ScriptedPlayer 构造确定性场景。"""

import engine
from engine import compute_standings, fold_match_penalties, run_match, validate_bid
from models import bid_limit
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
        assert validate_bid(3, 6, 6) == (3, "ok")
        assert validate_bid(0, 6, 6) == (0, "ok")
        assert validate_bid(6, 6, 6) == (6, "ok")

    def test_over_balance(self):
        assert validate_bid(6, 3, 6) == (0, "illegal")

    def test_over_limit(self):
        assert validate_bid(7, 100, 6) == (0, "illegal")
        assert validate_bid(6, 100, 5) == (0, "illegal")  # 第 3~4 天上限已降为 5

    def test_negative(self):
        assert validate_bid(-1, 6, 6) == (0, "illegal")

    def test_non_integer(self):
        assert validate_bid(2.5, 6, 6) == (0, "illegal")
        assert validate_bid("3", 6, 6) == (0, "illegal")
        assert validate_bid(None, 6, 6) == (0, "illegal")
        assert validate_bid(True, 6, 6) == (0, "illegal")  # bool 也视为非法


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
        # 第 1 天总消耗 12，分红 12 // 6 = 2。"己"出 0 获胜不扣钱，余额变为 6 + 2 = 8
        def strategy(obs):
            if obs.day == 1:
                return {"甲": 3, "乙": 3, "丙": 2, "丁": 2, "戊": 2, "己": 0}[obs.my_name]
            return 6 if obs.my_name == "己" else 0  # 己凭分红余额出 6 仍合法

        log = run_one_match({name: strategy for name in NAMES})
        day2 = log.days[1]
        assert log.days[0].dividend == 2  # 12 // 6 = 2
        record = next(r for r in day2.bids if r.player == "己")
        assert record.bid == 6 and record.status == "ok"

    def test_no_dividend_on_match_final_day_by_wins(self):
        # 前 3 天甲出 1 连胜（拿 6 个胜场），第 4 天全员放开出价、总消耗 14 本应分红 14 // 6 = 2，
        # 但拍出最后 2 个胜场的结束日不分红（第 4 天上限已降为 5，乙出 5 为合法最高价）
        def strategy(obs):
            if obs.day <= 3:
                return 1 if obs.my_name == "甲" else 0
            return {"甲": 3, "乙": 5, "丙": 2, "丁": 2, "戊": 1, "己": 1}[obs.my_name]

        log = run_one_match({name: strategy for name in NAMES})

        assert len(log.days) == 4
        assert log.end_reason == "wins_exhausted"
        assert log.days[0].dividend == 1 // 6  # 0
        assert log.days[1].dividend == 1 // 6
        assert log.days[2].dividend == 1 // 6
        assert log.days[3].dividend == 0  # 结束日不分红（当天总消耗 14，本应分 2）
        assert log.wins == {"甲": 6, "乙": 2, "丙": 0, "丁": 0, "戊": 0, "己": 0}

    def test_no_dividend_on_day5_even_if_wins_remain(self):
        # 前 4 天全员撞车流局，第 5 天分出胜负：仍不分红，且本局强制结束
        # （第 5 天上限已降为 4，差异化出价从 4 开始）
        def strategy(obs):
            if obs.day < 5:
                return 1  # 六人全撞车
            return {"甲": 4, "乙": 3, "丙": 2, "丁": 1, "戊": 0, "己": 0}[obs.my_name]

        log = run_one_match({name: strategy for name in NAMES})
        day5 = log.days[4]
        assert len(log.days) == 5
        assert not day5.is_void
        assert day5.winner == "甲"
        assert day5.dividend == 0
        assert log.end_reason == "max_days"
        assert log.wins["甲"] == 2  # 未拍出的 6 个胜场作废


class TestBundleWinsPerDay:
    """每天打包拍卖 2 个胜场：赢家一次拿 2 个，流局则 2 个整体顺延。"""

    def test_winner_takes_two_wins_per_day(self):
        # 甲连赢 4 天正好拿满 8 个；观察到的剩余量按 8→6→4→2 递减、本局胜场按 0→2→4→6 递增
        # （出价按天递减以保证 4 天余额可持续：6-3+分红1 起算，其余人前两天出 1 后两天出 0）
        seen = []

        def strategy(obs):
            if obs.my_name == "甲":
                seen.append((obs.day, obs.wins_remaining, obs.my_match_wins))
                return {1: 3, 2: 2, 3: 1, 4: 1}[obs.day]
            return 1 if obs.day <= 2 else 0

        log = run_one_match({name: strategy for name in NAMES})
        assert seen == [(1, 8, 0), (2, 6, 2), (3, 4, 4), (4, 2, 6)]
        assert log.wins["甲"] == 8
        assert len(log.days) == 4
        assert log.end_reason == "wins_exhausted"

    def test_void_day_postpones_whole_bundle(self):
        # 第 1 天全员撞车流局：2 个胜场整体顺延，第 2 天剩余量仍是 8
        remaining_seen = []

        def strategy(obs):
            if obs.my_name == "甲":
                remaining_seen.append((obs.day, obs.wins_remaining))
            if obs.day == 1:
                return 1  # 六人全撞车
            # 甲 1、其余 0（低出价保证余额可持续 4 天；0 撞车低于获胜价不影响）
            return 1 if obs.my_name == "甲" else 0

        log = run_one_match({name: strategy for name in NAMES})
        assert log.days[0].is_void
        assert remaining_seen[:2] == [(1, 8), (2, 8)]
        # 第 2~5 天甲各拿 2 个，第 5 天拍完结束
        assert len(log.days) == 5
        assert log.wins["甲"] == 8
        assert log.end_reason == "wins_exhausted"


class TestBidLimitSchedule:
    """出价上限按天降档：第 1~2 天 6、第 3~4 天 5、第 5 天 4，流局日照样降档。"""

    def test_limits_step_down_by_day(self):
        assert [bid_limit(day) for day in range(1, 6)] == [6, 6, 5, 5, 4]

    def test_over_daily_cap_is_illegal(self):
        # 甲前两天出 0 攒钱，第 3 天上限已降为 5：出 6 即使余额充足也记非法 0
        seen_caps = []

        def strategy(obs):
            if obs.my_name == "甲":
                seen_caps.append((obs.day, obs.bid_cap))
                return 6 if obs.day == 3 else 0
            return {"乙": 1, "丙": 2, "丁": 3, "戊": 4, "己": 5}[obs.my_name]

        log = run_one_match({name: strategy for name in NAMES})
        assert seen_caps[:3] == [(1, 6), (2, 6), (3, 5)]
        record = next(record for record in log.days[2].bids if record.player == "甲")
        assert record.raw_bid == 6
        assert record.bid == 0
        assert record.status == "illegal"

    def test_void_day_still_ticks_the_cap_down(self):
        # 第 1~2 天全员撞车流局：胜场顺延但降档时钟照常走，第 3 天上限已是 5
        observed_caps = []

        def strategy(obs):
            if obs.my_name == "甲":
                observed_caps.append(obs.bid_cap)
                return 1 if obs.day <= 2 else 5  # 前两天跟随全员撞车，第 3 天上限 5 下出 5 合法
            return 1

        log = run_one_match({name: strategy for name in NAMES})
        assert observed_caps == [6, 6, 5, 5, 4]
        assert log.days[0].is_void and log.days[1].is_void
        day3_record = next(record for record in log.days[2].bids if record.player == "甲")
        assert day3_record.bid == 5 and day3_record.status == "ok"


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
        assert len(data["days"]) == 4
        first_day = data["days"][0]
        assert {b["player"]: b["bid"] for b in first_day["bids"]} == dict(zip(NAMES, day_bids))
        assert first_day["winner"] == "甲"
        # 甲前 3 天出 2 连胜拿 6 胜场；第 4 天余额归零出 2 非法记 0，
        # 其余人出 1 撞车，唯一的 0 再拿 2 个胜场，8 个拍完结束
        assert data["wins"]["甲"] == 8


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

    def test_current_match_penalties_reduce_total_wins(self):
        # 进行中的本局：已获 2 胜、被罚 1，总胜场 = 历史 1 + 2 - 1 = 2
        wins_history = {name: [] for name in NAMES} | {"甲": [1]}
        standings = compute_standings(wins_history, {"甲": 2}, {"甲": 1})
        totals = {entry.player: entry.total_wins for entry in standings}
        assert totals["甲"] == 2


def run_match_with_history(strategies, wins_history, match_index=2):
    """带历史净胜场跑一局：wins_history 为 {名字: [各局胜场]}。"""
    players = [ScriptedPlayer(name, strategies[name]) for name in NAMES]
    history = {name: list(wins_history.get(name, [])) for name in NAMES}
    return run_match(players, match_index, False, history, [])


class TestCollisionPenalty:
    """高位撞车罚没：同价位玩家总胜场不全相同时，最多者各罚 1 个累计胜场。"""

    def test_richest_offender_pays_above_winning_price(self):
        # 甲乙出 2 撞车、高于获胜价 1：甲有历史 1 胜被罚，乙 0 胜免疫；
        # 丁戊己出 0 撞车但低于获胜价，即使丁有 3 个历史胜场也不罚
        history = {"甲": [1], "丁": [3]}
        log = run_match_with_history(constant_strategies([2, 2, 1, 0, 0, 0]), history)
        day1 = log.days[0]
        assert day1.winner == "丙" and day1.winning_bid == 1
        assert day1.penalties == [("甲", 1)]
        # 被罚后甲与乙持平（都是 0），第 2 天起不再触发罚没（自我刹车）
        assert log.days[1].penalties == []
        # 罚没不影响本局拍卖流程：丙照常连赢 4 天（拿满 8 个胜场）结束
        # （甲乙第 4 天余额归零、出 2 非法记 0，不影响丙每天唯一 1 获胜）
        assert log.wins["丙"] == 8
        assert log.end_reason == "wins_exhausted"
        assert len(log.days) == 4

    def test_tied_richest_offenders_all_pay(self):
        # 甲乙丙出 2 撞车：甲乙各 1 胜并列最多都被罚，丙 0 胜免罚
        history = {"甲": [1], "乙": [1]}
        log = run_match_with_history(constant_strategies([2, 2, 2, 1, 0, 0]), history)
        day1 = log.days[0]
        assert day1.winner == "丁" and day1.winning_bid == 1
        assert day1.penalties == [("甲", 1), ("乙", 1)]

    def test_all_equal_offenders_no_penalty(self):
        # 全员 0 胜开局撞车：胜场全相同，无人能罚
        log = run_one_match(constant_strategies([5, 5, 4, 3, 2, 1]))
        assert log.days[0].winner == "丙"
        assert log.days[0].penalties == []

    def test_void_day_taxes_all_collided_prices(self):
        # 流局日检查全部撞车价位：2 价位罚甲（1 胜 vs 0 胜），0 价位罚丙（2 胜 vs 0 胜），
        # 1 价位戊己同为 0 胜不罚
        history = {"甲": [1], "丙": [2]}

        def strategy(obs):
            if obs.day == 1:
                return {"甲": 2, "乙": 2, "丙": 0, "丁": 0, "戊": 1, "己": 1}[obs.my_name]
            return {"甲": 5, "乙": 4, "丙": 3, "丁": 2, "戊": 1, "己": 0}[obs.my_name]

        log = run_match_with_history({name: strategy for name in NAMES}, history)
        day1 = log.days[0]
        assert day1.is_void
        assert day1.penalties == [("甲", 1), ("丙", 1)]
        # to_dict 同步携带罚没记录
        assert log.to_dict()["days"][0]["penalties"] == [
            {"player": "甲", "wins": 1},
            {"player": "丙", "wins": 1},
        ]

    def test_penalty_counts_current_match_wins(self):
        # 甲第 1 天唯一 3 获胜、拿走打包的 2 个胜场；第 2 天甲乙出 2 撞车，
        # 甲的总胜场含本局已获胜场（2 > 乙的 0）→ 被罚
        def strategy(obs):
            if obs.day == 1:
                return {"甲": 3, "乙": 2, "丙": 1, "丁": 0, "戊": 0, "己": 0}[obs.my_name]
            if obs.day == 2:
                return {"甲": 2, "乙": 2, "丙": 1, "丁": 0, "戊": 0, "己": 0}[obs.my_name]
            # 第 3 天起甲出 0 避开撞车（继续出 2 会与乙持续撞车连吃罚没直至归零，
            # 污染本用例的断言），且甲后续不再获胜，便于直接断言累计胜场；
            # 乙出 1（余额 2 正好够连赢两天），丙丁戊出 2 撞车压过 1，
            # 第 5 天乙余额归零出 1 非法记 0，全场唯一 0 收走最后 2 个胜场
            return {"甲": 0, "乙": 1, "丙": 2, "丁": 2, "戊": 2, "己": 0}[obs.my_name]

        log = run_one_match({name: strategy for name in NAMES})
        assert log.days[0].winner == "甲"
        assert log.days[0].penalties == []
        # 第 2 天：甲乙出 2 撞车，唯一 1 的丙获胜；甲总胜场含本局已获的 2 个（> 乙的 0）→ 被罚
        assert log.days[1].winner == "丙"
        assert log.days[1].penalties == [("甲", 1)]
        # 第 3 天起甲出 0 脱身，唯一 1 的乙连赢 2 天（丙丁戊的 2 撞车抬价），
        # 8 个胜场拍完本局结束；丙这两天因 2 撞车持续单独被罚
        assert log.days[2].winner == "乙"
        # 撞车价位 2 高于获胜价：丙总胜场 2（本局第 2 天赢的）> 丁戊的 0，单独被罚
        assert log.days[2].penalties == [("丙", 1)]
        assert len(log.days) == 4
        assert log.wins == {"甲": 2, "乙": 4, "丙": 2, "丁": 0, "戊": 0, "己": 0}
        assert log.end_reason == "wins_exhausted"


class TestFoldMatchPenalties:
    def test_deducts_from_most_recent_match_first(self):
        # 甲历史 2 胜，本局被罚 2 次、净胜场归零，全部从历史局里扣空
        log = run_match_with_history(constant_strategies([2, 2, 1, 0, 0, 0]), {"甲": [2]})
        assert log.days[0].penalties == [("甲", 1)]
        assert log.days[1].penalties == [("甲", 1)]  # 第 2 天甲净胜场仍为 1（2-1），再罚
        assert log.days[2].penalties == []           # 第 3 天甲归零，与乙持平不罚

        wins_history = {name: [] for name in NAMES}
        wins_history["甲"] = [2]
        for name, wins in log.wins.items():
            wins_history[name].append(wins)
        fold_match_penalties(wins_history, log)
        assert wins_history["甲"] == [0, 0]  # 本局 0 胜不可扣，第 1 局 2 胜被扣空
        assert wins_history["丙"] == [8]     # 丙连赢 4 天拿满 8 个胜场

    def test_skips_matches_without_wins(self):
        from models import BidRecord, DayResult, MatchLog

        log = MatchLog(match_index=3, is_playoff=False, wins={"甲": 0})
        log.days.append(DayResult(
            day=1,
            bids=[BidRecord(player="甲", raw_bid=2, bid=2)],
            winner=None, winning_bid=None,
            voided_price_count=0, dividend=0, is_void=True,
            penalties=[("甲", 1)],
        ))
        wins_history = {"甲": [2, 0, 0]}  # 当前局（0 胜）已追加
        fold_match_penalties(wins_history, log)
        assert wins_history["甲"] == [1, 0, 0]  # 跳过无胜场的局，从第 1 局扣
