"""整场流程（排名结算与加赛循环）的测试，用 ScriptedPlayer 构造指定胜场分布。"""

import tournament
from players import ScriptedPlayer
from tournament import first_place_tied, run_tournament

NAMES = ["甲", "乙", "丙", "丁", "戊", "己"]


def make_players(match_winners: dict[int, str]):
    """构造 6 个 ScriptedPlayer：每一局由 match_winners[局号] 指定的玩家包揽胜场。

    指定赢家每天出 2、其他人每天出 1（余额可持续、价位唯一），因此赢家 3 天连胜。
    局号 = len(obs.past_match_logs) + 1。
    """
    def make_strategy(name):
        def strategy(obs):
            current_match = len(obs.past_match_logs) + 1
            return 2 if match_winners.get(current_match) == name else 1
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
        assert totals(result) == {"甲": 15, "乙": 0, "丙": 0, "丁": 0, "戊": 0, "己": 0}
        assert ranks(result)["甲"] == 1

    def test_tie_below_first_place_does_not_trigger_playoff(self):
        # 甲乙丙丁戊全部 0 胜场并列，但第一名唯一，不触发加赛，并列保持
        players = make_players({1: "甲", 2: "甲", 3: "甲", 4: "甲", 5: "甲"})
        result = run_tournament(players)

        assert result.playoff_count == 0
        for name in ["乙", "丙", "丁", "戊", "己"]:
            assert ranks(result)[name] == 2  # 第一名以外并列同名次


class TestPlayoff:
    def test_first_place_tie_triggers_playoff_until_unique(self):
        players = make_players({1: "甲", 2: "甲", 3: "乙", 4: "乙", 5: "丙", 6: "甲"})
        result = run_tournament(players)

        assert result.playoff_count == 1
        assert len(result.match_logs) == 6
        assert result.match_logs[5].is_playoff
        # 加赛胜场计入总胜场：甲 6 + 3 = 9，乙 6
        assert totals(result)["甲"] == 9
        assert totals(result)["乙"] == 6
        assert ranks(result)["甲"] == 1

    def test_playoff_continues_when_tie_persists_with_new_leaders(self):
        # 正赛后甲乙并列 6 胜；第 6 局丙包揽后甲乙丙三人并列（并列者变化），
        # 第 7 局甲包揽后第一名唯一，加赛结束
        players = make_players({1: "甲", 2: "甲", 3: "乙", 4: "乙", 5: "丙", 6: "丙", 7: "甲"})
        result = run_tournament(players)

        assert result.playoff_count == 2
        assert len(result.match_logs) == 7
        assert all(log.is_playoff for log in result.match_logs[5:])
        assert totals(result) == {"甲": 9, "乙": 6, "丙": 6, "丁": 0, "戊": 0, "己": 0}
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


class TestPublicationOnMatchEnd:
    def test_callback_receives_match_log_and_standings_after_each_match(self):
        publications = []
        players = make_players({1: "甲", 2: "乙", 3: "丙", 4: "丁", 5: "戊", 6: "甲"})
        run_tournament(players, on_match_end=lambda log, standings: publications.append((log, standings)))

        # 正赛后甲乙丙丁戊五人 3 胜并列第一，触发加赛；第 6 局甲包揽后唯一
        assert len(publications) == 6
        first_log, first_standings = publications[0]
        # 局日志完整：含每天每人出价
        assert len(first_log.days) == 3
        assert len(first_log.days[0].bids) == 6
        # 局末公开排名含胜场明细，且反映该局结果
        first_entry = next(e for e in first_standings if e.player == "甲")
        assert first_entry.total_wins == 3
        assert first_entry.wins_per_match == [3]

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
        assert obs.past_match_logs[0]["wins"]["甲"] == 3
        leader = obs.standings[0]
        assert leader.player == "甲" and leader.total_wins == 3
