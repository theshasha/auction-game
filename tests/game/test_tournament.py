"""整场流程（排名结算与加赛循环）的测试，用 ScriptedPlayer 构造指定胜场分布。"""

from dataclasses import replace

import pytest

from tests.support.factories import TEST_GAME
from auction_game.game.players import ScriptedPlayer
from auction_game.game.tournament import first_place_tied, run_tournament

NAMES = ["甲", "乙", "丙", "丁", "戊", "己"]


def make_players(match_winners: dict[int, str]):
    """构造 6 个 ScriptedPlayer：每一局由 match_winners[局号] 指定的玩家包揽胜场。

    指定赢家每天出 1（唯一获胜，每天付 1，工资 +3 足以 sustain），其他人每天出 0
    （撞车但支付 0，无代价）；赢家 6 天连赢，按每天奖池 1 共拿 6 个胜场。
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
    def test_custom_regular_count_and_coins_reset_between_matches(self):
        game = replace(TEST_GAME, initial_coins=11, daily_salary=2, daily_pool=(2, 3, 4),
                       max_bid=9, super_cost=4, super_bounty=6, regular_matches=2)
        seen = []

        def strategy(obs):
            seen.append(obs)
            return 9 if obs.my_name == "甲" and obs.day == 1 else (1 if obs.my_name == "甲" else 0)

        players = [ScriptedPlayer(name, strategy) for name in NAMES[:3]]
        result = run_tournament(players, game)

        assert len(result.match_logs) == 2
        assert result.playoff_count == 0
        assert totals(result) == {"甲": 18, "乙": 0, "丙": 0}
        assert all(not log.is_playoff and log.game is game for log in result.match_logs)
        first_days = [obs for obs in seen if obs.day == 1]
        assert len(first_days) == 6
        assert all(obs.my_coins == 11 for obs in first_days)
        assert all(obs.game is game for obs in seen)
        assert first_days[3].past_match_logs[0]["game"] == game.to_dict()

    @pytest.mark.parametrize("names", [[], ["甲"], ["甲", "甲"], ["甲", ""], ["甲", "  "]])
    def test_invalid_roster_is_rejected_before_playing(self, names):
        players = [ScriptedPlayer.constant(name, 0) for name in names]
        with pytest.raises(ValueError, match="玩家|比赛"):
            run_tournament(players, TEST_GAME)

    def test_unique_first_place_no_playoff(self):
        players = make_players({1: "甲", 2: "甲", 3: "甲", 4: "甲", 5: "甲"})
        result = run_tournament(players, TEST_GAME)

        assert len(result.match_logs) == 5
        assert result.playoff_count == 0
        assert not result.playoff_cap_hit
        assert totals(result) == {"甲": 30, "乙": 0, "丙": 0, "丁": 0, "戊": 0, "己": 0}
        assert ranks(result)["甲"] == 1
        # 乙丙丁戊己全部 0 胜场并列，但第一名唯一，不触发加赛，并列保持
        for name in ["乙", "丙", "丁", "戊", "己"]:
            assert ranks(result)[name] == 2  # 第一名以外并列同名次


class TestPlayoff:
    def test_first_place_tie_triggers_playoff_until_unique(self):
        players = make_players({1: "甲", 2: "甲", 3: "乙", 4: "乙", 5: "丙", 6: "甲"})
        result = run_tournament(players, TEST_GAME)

        assert result.playoff_count == 1
        assert len(result.match_logs) == 6
        assert result.match_logs[5].is_playoff
        # 加赛胜场计入总胜场：甲 12 + 6 = 18，乙 12
        assert totals(result)["甲"] == 18
        assert totals(result)["乙"] == 12
        assert ranks(result)["甲"] == 1

    def test_playoff_continues_when_tie_persists_with_new_leaders(self):
        # 正赛后甲乙并列 12 胜；第 6 局丙包揽后甲乙丙三人并列（并列者变化），
        # 第 7 局甲包揽后第一名唯一，加赛结束
        players = make_players({1: "甲", 2: "甲", 3: "乙", 4: "乙", 5: "丙", 6: "丙", 7: "甲"})
        result = run_tournament(players, TEST_GAME)

        assert result.playoff_count == 2
        assert len(result.match_logs) == 7
        assert all(log.is_playoff for log in result.match_logs[5:])
        assert totals(result) == {"甲": 18, "乙": 12, "丙": 12, "丁": 0, "戊": 0, "己": 0}
        assert ranks(result)["甲"] == 1
        assert ranks(result)["乙"] == 2 and ranks(result)["丙"] == 2  # 第二名并列保持

    def test_zero_playoff_cap_preserves_tied_champions(self):
        # 上限为 0 时，第一名并列直接封顶，不再加赛
        game = replace(TEST_GAME, max_playoffs=0)
        players = make_players({1: "甲", 2: "甲", 3: "乙", 4: "乙", 5: "丙"})
        result = run_tournament(players, game)

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
        result = run_tournament(players, TEST_GAME, director=director)

        # 前 4 局仍有下一局，触发局末发言；第 5 局（最后一局）跳过局末发言
        assert [call[1] for call in director.match_end_calls] == [True, True, True, True]
        assert [call[0].match_index for call in director.match_end_calls] == [1, 2, 3, 4]
        # 终局感言恰好一次，且拿到最后一局（第 5 局）的完整出价日志
        assert len(director.final_calls) == 1
        _, final_log = director.final_calls[0]
        assert final_log.match_index == 5
        assert final_log is result.match_logs[-1]

    def test_will_continue_respects_playoff_cap(self):
        # 上限缩到 2：全员每天出 0 撞车永远流局、人人 0 胜永远并列，直到封顶
        game = replace(TEST_GAME, max_playoffs=2)
        director = self.FakeDirector()
        players = make_players({})  # 无人被指定为赢家，全员每天出 0 撞车流局
        result = run_tournament(players, game, director=director)

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
        run_tournament(players, TEST_GAME, on_match_end=lambda log, standings: publications.append((log, standings)))

        # 正赛后甲乙丙丁戊五人 6 胜并列第一，触发加赛；第 6 局甲包揽后唯一
        assert len(publications) == 6
        first_log, first_standings = publications[0]
        # 局日志完整：固定 6 天，含每天每人出价
        assert len(first_log.days) == 6
        assert len(first_log.days[0].bids) == 6
        # 局末公开排名含胜场明细，且反映该局结果
        first_entry = next(e for e in first_standings if e.player == "甲")
        assert first_entry.total_wins == 6
        assert first_entry.wins_per_match == [6]

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
        run_tournament(players, TEST_GAME)

        assert len(seen) == 6
        obs = seen[0]
        assert obs.past_match_logs[0]["match_index"] == 1
        assert obs.past_match_logs[0]["wins"]["甲"] == 6
        leader = obs.standings[0]
        assert leader.player == "甲" and leader.total_wins == 6
