"""整场流程（排名结算与加赛循环）的测试，用 ScriptedPlayer 构造指定胜场分布。"""

import tournament
from players import ScriptedPlayer
from tournament import first_place_tied, run_tournament

NAMES = ["甲", "乙", "丙", "丁", "戊", "己"]


def make_players(match_winners: dict[int, str]):
    """构造 6 个 ScriptedPlayer：每一局由 match_winners[局号] 指定的玩家包揽胜场。

    指定赢家每天出 1、其他人每天出 0（余额可持续、价位唯一），因此赢家 4 天连胜、
    每天拿走打包的 2 个胜场，一局共 8 个。
    非赢家出 0 而非 1：0 撞车低于获胜价，避免领先者在后续局触发罚没、
    破坏本类用例对总胜场的精确断言。
    局号 = len(obs.past_match_logs) + 1。
    """
    def make_strategy(name):
        def strategy(obs):
            current_match = len(obs.past_match_logs) + 1
            return 1 if match_winners.get(current_match) == name else 0
        return strategy

    return [ScriptedPlayer(name, make_strategy(name)) for name in NAMES]


def totals(result):
    return {entry.player: entry.total_wins for entry in result.final_standings}


def ranks(result):
    return {entry.player: entry.rank for entry in result.final_standings}


class TestRegularSeasonOnly:
    def test_unique_first_place_no_playoff(self):
        players = make_players({1: "甲", 2: "甲", 3: "甲", 4: "甲", 5: "甲"})
        result = run_tournament(players)

        assert len(result.match_logs) == 5
        assert result.playoff_count == 0
        assert not result.playoff_cap_hit
        assert totals(result) == {"甲": 40, "乙": 0, "丙": 0, "丁": 0, "戊": 0, "己": 0}
        assert ranks(result)["甲"] == 1

    def test_tie_below_first_place_does_not_trigger_playoff(self):
        # 甲乙丙丁戊全部 0 胜场并列，但第一名唯一，不触发加赛，并列保持
        players = make_players({1: "甲", 2: "甲", 3: "甲", 4: "甲", 5: "甲"})
        result = run_tournament(players)

        assert result.playoff_count == 0
        for name in ["乙", "丙", "丁", "戊", "己"]:
            assert ranks(result)[name] == 2  # 第一名以外并列同名次


class TestCollisionPenaltyFolding:
    def test_penalties_reduce_final_standings(self):
        # 甲每局出 2 包揽胜场；第 3 局乙也出 2 撞车、丙出 1 唯一获胜、其余出 0。
        # 第 3 局开局甲已攒 16 胜，前 3 天与 0 胜的乙在高于获胜价的 2 撞车，各被罚 1，共 3
        # （第 4 天甲乙余额耗尽出 2 非法记 0，撞车价位 0 低于获胜价，不再罚）。
        def make_strategy(name):
            def strategy(obs):
                current_match = len(obs.past_match_logs) + 1
                if current_match == 3:
                    return {"甲": 2, "乙": 2, "丙": 1}.get(name, 0)
                return 2 if name == "甲" else 1
            return strategy

        players = [ScriptedPlayer(name, make_strategy(name)) for name in NAMES]
        result = run_tournament(players)

        assert result.playoff_count == 0
        # 甲毛胜场 32（第 1/2/4/5 局各 8），第 3 局被罚 3，净 29；罚没从最近有胜场局折算
        assert totals(result)["甲"] == 29
        entry = next(e for e in result.final_standings if e.player == "甲")
        assert entry.wins_per_match == [8, 5, 0, 8, 8]
        # 丙第 3 局拿满 8 胜且后续未被罚（除数 6 分红托底下余额可持续，第 4/5 局第 4 天仍出 1、
        # 获胜价 1 高于 0 撞车，不再触发「非法 0 撞车罚没」）
        assert totals(result)["丙"] == 8
        assert ranks(result)["甲"] == 1


class TestDividendSustainsBalances:
    """除数 6 的经济效果：每天总消耗不低于 6 时分红 ≥1 补回出价，余额可持续，不再被耗尽。"""

    def test_daily_dividend_keeps_balance_positive(self):
        # 甲每天出 2（唯一最高）获胜，其余人每天出 1（低于获胜价、撞车不罚）。
        # 每天总消耗 2+5 = 7，分红 7 // 6 = 1 恰好补回其余人的出价：余额永续，4 天都出得起。
        players = [ScriptedPlayer(n, (lambda b: lambda obs: b)(2 if n == "甲" else 1)) for n in NAMES]
        result = run_tournament(players)

        # 只跑到第 1 局结束即可验证：甲 4 天连赢，8 个胜场拍完，无人余额耗尽
        first_log = result.match_logs[0]
        assert len(first_log.days) == 4
        assert all(not day.is_void for day in first_log.days)
        assert all(day.winner == "甲" and day.winning_bid == 2 for day in first_log.days)
        # 每天分红 1（最后一天结束不分红）
        assert [day.dividend for day in first_log.days] == [1, 1, 1, 0]
        # 甲 4 天余额：6-2+1=5, -2+1=4, -2+1=3, -2=1，第 4 天仍能出 2（不非法）
        assert all(
            next(b for b in day.bids if b.player == "甲").status == "ok"
            for day in first_log.days
        )


class TestPlayoff:
    def test_first_place_tie_triggers_playoff_until_unique(self):
        players = make_players({1: "甲", 2: "甲", 3: "乙", 4: "乙", 5: "丙", 6: "甲"})
        result = run_tournament(players)

        assert result.playoff_count == 1
        assert len(result.match_logs) == 6
        assert result.match_logs[5].is_playoff
        # 加赛胜场计入总胜场：甲 16 + 8 = 24，乙 16
        assert totals(result)["甲"] == 24
        assert totals(result)["乙"] == 16
        assert ranks(result)["甲"] == 1

    def test_playoff_continues_when_tie_persists_with_new_leaders(self):
        # 正赛后甲乙并列 16 胜；第 6 局丙包揽后甲乙丙三人并列（并列者变化），
        # 第 7 局甲包揽后第一名唯一，加赛结束
        players = make_players({1: "甲", 2: "甲", 3: "乙", 4: "乙", 5: "丙", 6: "丙", 7: "甲"})
        result = run_tournament(players)

        assert result.playoff_count == 2
        assert len(result.match_logs) == 7
        assert all(log.is_playoff for log in result.match_logs[5:])
        assert totals(result) == {"甲": 24, "乙": 16, "丙": 16, "丁": 0, "戊": 0, "己": 0}
        assert ranks(result)["甲"] == 1
        assert ranks(result)["乙"] == 2 and ranks(result)["丙"] == 2  # 第二名并列保持

    def test_playoff_cap_marks_abnormal_end(self, monkeypatch):
        # 安全上限为 0 时，第一名并列直接触发封顶异常，不再加赛
        monkeypatch.setattr(tournament, "MAX_PLAYOFFS", 0)
        players = make_players({1: "甲", 2: "甲", 3: "乙", 4: "乙", 5: "丙"})
        result = run_tournament(players)

        assert result.playoff_cap_hit
        assert result.playoff_count == 0
        assert first_place_tied(result.final_standings)


class TestDirectorWillContinue:
    """will_continue 的准确性：决定局末是否还有「下一局规划」轮，必须考虑加赛上限。"""

    class FakeDirector:
        def __init__(self):
            self.match_end_calls = []  # [(log, will_continue)]
            self.final_calls = []      # [(standings, log)]

        def on_opening(self):
            pass

        def on_match_end(self, log, standings, will_continue):
            self.match_end_calls.append((log, will_continue))

        def on_final(self, standings, log):
            self.final_calls.append((standings, log))

    def test_will_continue_false_on_last_match_when_decided(self):
        director = self.FakeDirector()
        players = make_players({i: "甲" for i in range(1, 6)})
        result = run_tournament(players, director=director)

        # 前 4 局仍有下一局，触发局末发言；第 5 局（最后一局）跳过局末发言
        assert [call[1] for call in director.match_end_calls] == [True, True, True, True]
        assert [call[0].match_index for call in director.match_end_calls] == [1, 2, 3, 4]
        # 终局感言恰好一次，且拿到最后一局（第 5 局）的完整出价日志
        assert len(director.final_calls) == 1
        _, final_log = director.final_calls[0]
        assert final_log.match_index == 5
        assert final_log is result.match_logs[-1]

    def test_will_continue_respects_playoff_cap(self, monkeypatch):
        # 上限缩到 2：全员每天出 1 撞车永远流局、人人 0 胜永远并列，直到封顶
        monkeypatch.setattr(tournament, "MAX_PLAYOFFS", 2)
        director = self.FakeDirector()
        players = make_players({})  # 无人被指定为赢家，全员每天出 1 撞车流局
        result = run_tournament(players, director=director)

        assert result.playoff_cap_hit
        assert result.playoff_count == 2
        # 第 5 局后还有加赛空间→True；第 6 局（加赛 1/2）→True；
        # 第 7 局（加赛 2/2）封顶为最后一局，跳过局末发言
        assert [call[1] for call in director.match_end_calls] == [True, True, True, True, True, True]
        assert [call[0].match_index for call in director.match_end_calls] == [1, 2, 3, 4, 5, 6]
        # 终局感言拿到的是封顶的第 7 局（最后一局）日志
        assert len(director.final_calls) == 1
        _, final_log = director.final_calls[0]
        assert final_log.match_index == 7
        assert final_log is result.match_logs[-1]


class TestPublicationOnMatchEnd:
    def test_callback_receives_match_log_and_standings_after_each_match(self):
        publications = []
        players = make_players({1: "甲", 2: "乙", 3: "丙", 4: "丁", 5: "戊", 6: "甲"})
        run_tournament(players, on_match_end=lambda log, standings: publications.append((log, standings)))

        # 正赛后甲乙丙丁戊五人 8 胜并列第一，触发加赛；第 6 局甲包揽后唯一
        assert len(publications) == 6
        first_log, first_standings = publications[0]
        # 局日志完整：含每天每人出价
        assert len(first_log.days) == 4
        assert len(first_log.days[0].bids) == 6
        # 局末公开排名含胜场明细，且反映该局结果
        first_entry = next(e for e in first_standings if e.player == "甲")
        assert first_entry.total_wins == 8
        assert first_entry.wins_per_match == [8]

    def test_players_observe_past_logs_and_standings(self):
        # 第 2 局第 1 天时，玩家应能看到第 1 局的完整公开日志与当前排名
        seen = []

        def make_strategy(name):
            def strategy(obs):
                if len(obs.past_match_logs) == 1 and obs.day == 1:
                    seen.append(obs)
                return 2 if match_winners.get(len(obs.past_match_logs) + 1) == name else 1
            return strategy

        match_winners = {i: "甲" for i in range(1, 8)}
        players = [ScriptedPlayer(name, make_strategy(name)) for name in NAMES]
        run_tournament(players)

        assert len(seen) == 6
        obs = seen[0]
        assert obs.past_match_logs[0]["match_index"] == 1
        assert obs.past_match_logs[0]["wins"]["甲"] == 8
        leader = obs.standings[0]
        assert leader.player == "甲" and leader.total_wins == 8
