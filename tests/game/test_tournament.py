"""整场流程：拍卖分与总分分别累计，按总分排名并决定加赛和发言。"""

from dataclasses import replace
from copy import deepcopy

from tests.support.factories import TEST_GAME, ScriptedPlayer
from auction_game.game.tournament import first_place_tied, run_tournament

NAMES = ["甲", "乙", "丙", "丁", "戊", "己"]


def make_players(match_winners: dict[int, str]):
    """每局由指定玩家包揽拍卖分，其他人以 0 撞车。"""
    def make_strategy(name):
        def strategy(obs):
            return 1 if match_winners.get(obs.match_index) == name else 0
        return strategy

    return [ScriptedPlayer(name, make_strategy(name)) for name in NAMES]


def make_bid_players(table, capture=None):
    """三人合法整单脚本；键为 (局号, 天)，未列出的日子全员出 0。"""
    def submission(obs):
        bids = table.get((obs.match_index, obs.day), {})
        return bids.get(obs.my_name, (0, None))

    def strategy(obs):
        if capture is not None:
            capture.append(obs)
        return submission(obs)[0]

    return [
        ScriptedPlayer(name, strategy, lambda obs: submission(obs)[1])
        for name in NAMES[:3]
    ]


def totals(result):
    return {entry.player: entry.total_auction_score for entry in result.final_standings}


def scores(result):
    return {entry.player: entry.total_score for entry in result.final_standings}


def ranks(result):
    return {entry.player: entry.rank for entry in result.final_standings}


class TestRegularSeasonOnly:
    def test_custom_regular_count_and_coins_reset_between_matches(self):
        game = replace(TEST_GAME, initial_coins=11, daily_salary=2, daily_pool=(2, 3, 4),
                       max_bid=9, super_cost=4, super_bounty=6,
                       super_hit_score=2, regular_matches=2)
        seen = []

        def strategy(obs):
            seen.append(obs)
            return 9 if obs.my_name == "甲" and obs.day == 1 else (1 if obs.my_name == "甲" else 0)

        players = [ScriptedPlayer(name, strategy) for name in NAMES[:3]]
        result = run_tournament(players, game)

        assert len(result.match_logs) == 2
        assert result.playoff_count == 0
        assert totals(result) == {"甲": 18, "乙": 0, "丙": 0}
        assert scores(result) == {"甲": 18, "乙": 0, "丙": 0}
        assert result.final_standings[0].scores_per_match == [9, 9]
        assert all(not log.is_playoff and log.game is game for log in result.match_logs)
        first_days = [obs for obs in seen if obs.day == 1]
        assert len(first_days) == 6
        assert all(obs.my_coins == 11 for obs in first_days)
        assert all(obs.game is game for obs in seen)
        assert [obs.match_index for obs in first_days] == [1, 1, 1, 2, 2, 2]
        assert first_days[3].my_match_score == 0
        assert first_days[3].my_total_score == 9
        assert first_days[3].standings[0].total_score == 9
        assert first_days[3].standings[0].scores_per_match == [9]
        assert first_days[0].my_total_score == 0
        assert first_days[0].standings[0].total_score == 0

    def test_unique_first_place_no_playoff(self):
        players = make_players({1: "甲", 2: "甲", 3: "甲", 4: "甲", 5: "甲"})
        result = run_tournament(players, TEST_GAME)

        assert len(result.match_logs) == 5
        assert result.playoff_count == 0
        assert not result.playoff_cap_hit
        assert totals(result) == {"甲": 120, "乙": 0, "丙": 0, "丁": 0, "戊": 0, "己": 0}
        assert scores(result) == {"甲": 120, "乙": 0, "丙": 0, "丁": 0, "戊": 0, "己": 0}
        assert ranks(result)["甲"] == 1
        # 乙丙丁戊己全部 0 分并列，但第一名唯一，不触发加赛，并列保持
        for name in ["乙", "丙", "丁", "戊", "己"]:
            assert ranks(result)[name] == 2  # 第一名以外并列同名次


class TestPlayoff:
    def test_first_place_tie_triggers_playoff_until_unique(self):
        players = make_players({1: "甲", 2: "甲", 3: "乙", 4: "乙", 5: "丙", 6: "甲"})
        result = run_tournament(players, TEST_GAME)

        assert result.playoff_count == 1
        assert len(result.match_logs) == 6
        assert result.match_logs[5].is_playoff
        # 加赛拍卖分计入累计：甲 48 + 24 = 72，乙 48
        assert totals(result)["甲"] == 72
        assert totals(result)["乙"] == 48
        assert scores(result)["甲"] == 72
        assert scores(result)["乙"] == 48
        assert ranks(result)["甲"] == 1

    def test_playoff_continues_when_tie_persists_with_new_leaders(self):
        # 正赛后甲乙并列 48 分；第 6 局丙包揽后甲乙丙三人并列（并列者变化），
        # 第 7 局甲包揽后第一名唯一，加赛结束
        players = make_players({1: "甲", 2: "甲", 3: "乙", 4: "乙", 5: "丙", 6: "丙", 7: "甲"})
        result = run_tournament(players, TEST_GAME)

        assert result.playoff_count == 2
        assert len(result.match_logs) == 7
        assert all(log.is_playoff for log in result.match_logs[5:])
        assert totals(result) == {"甲": 72, "乙": 48, "丙": 48, "丁": 0, "戊": 0, "己": 0}
        assert scores(result) == {"甲": 72, "乙": 48, "丙": 48, "丁": 0, "戊": 0, "己": 0}
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


class TestDirectorMatchEnd:
    """局末发言只在还有下一局时发生；判断下一局要考虑加赛上限。"""

    class FakeDirector:
        def __init__(self):
            self.match_end_calls = []  # [log]
            self.final_calls = []      # [(standings, log)]

        def on_opening(self):
            pass

        def on_match_end(self, logs, standings):
            assert [log.match_index for log in logs] == list(range(1, len(logs) + 1))
            self.match_end_calls.append(logs[-1])

        def on_final(self, standings, log):
            self.final_calls.append((standings, log))

    def test_auction_tie_with_unique_total_score_ends_without_match_end_speech(self):
        game = replace(TEST_GAME, daily_pool=(5, 5), regular_matches=1,
                       super_hit_score=1, max_playoffs=2)
        director = self.FakeDirector()
        players = make_bid_players({
            (1, 1): {"甲": (1, 0)},
            (1, 2): {"乙": (1, None)},
        })
        publications = []

        result = run_tournament(players, game, director=director,
                                on_match_end=lambda log, standings: publications.append((log, standings)))

        assert totals(result) == {"甲": 5, "乙": 5, "丙": 0}
        assert scores(result) == {"甲": 6, "乙": 5, "丙": 0}
        assert ranks(result) == {"甲": 1, "乙": 2, "丙": 3}
        assert result.playoff_count == 0
        assert not result.playoff_cap_hit
        assert not first_place_tied(result.final_standings)
        assert len(publications) == 1
        assert director.match_end_calls == []
        assert director.final_calls == [(result.final_standings, result.match_logs[0])]

    def test_unique_auction_leader_needs_playoff_until_hit_leads_by_one(self):
        game = replace(TEST_GAME, daily_pool=(1, 1), regular_matches=1,
                       super_hit_score=1, max_playoffs=2)
        director = self.FakeDirector()
        observations = []
        players = make_bid_players({
            (1, 1): {"甲": (2, None), "乙": (0, 1), "丙": (1, None)},
            (1, 2): {"甲": (2, None), "乙": (0, 1), "丙": (1, None)},
            # 加赛全部正常出价作废，只有甲命中得 1 分；次日流局不再得分。
            (2, 1): {"甲": (0, 1), "乙": (1, None)},
        }, capture=observations)
        publications = []

        result = run_tournament(players, game, director=director,
                                on_match_end=lambda log, standings: publications.append((log, standings)))

        regular_log, regular_standings = publications[0]
        assert regular_log.auction_scores == {"甲": 2, "乙": 0, "丙": 0}
        assert regular_log.scores == {"甲": 2, "乙": 2, "丙": 0}
        assert first_place_tied(regular_standings)
        assert result.playoff_count == 1
        assert not result.playoff_cap_hit
        assert len(result.match_logs) == 2
        assert result.match_logs[1].is_playoff
        assert all(day.is_void for day in result.match_logs[1].days)
        assert result.match_logs[1].auction_scores == {"甲": 0, "乙": 0, "丙": 0}
        assert result.match_logs[1].scores == {"甲": 1, "乙": 0, "丙": 0}
        assert totals(result) == {"甲": 2, "乙": 0, "丙": 0}
        assert scores(result) == {"甲": 3, "乙": 2, "丙": 0}
        assert ranks(result) == {"甲": 1, "乙": 2, "丙": 3}
        assert {entry.player: entry.scores_per_match for entry in result.final_standings} == {
            "甲": [2, 1], "乙": [2, 0], "丙": [0, 0],
        }
        assert [log.match_index for log in director.match_end_calls] == [1]
        assert director.final_calls == [(result.final_standings, result.match_logs[1])]
        # 已发布的常规局排名不随加赛累计而漂移。
        assert {entry.player: entry.total_score for entry in regular_standings} == {"甲": 2, "乙": 2, "丙": 0}
        assert {entry.player: entry.scores_per_match for entry in regular_standings} == {
            "甲": [2], "乙": [2], "丙": [0],
        }
        playoff_opening = [obs for obs in observations if obs.match_index == 2 and obs.day == 1]
        assert {obs.my_name: obs.my_total_score for obs in playoff_opening} == {"甲": 2, "乙": 2, "丙": 0}
        assert all(obs.my_match_score == 0 and obs.my_coins == game.initial_coins for obs in playoff_opening)
        after_hit = next(obs for obs in observations if obs.match_index == 2
                         and obs.day == 2 and obs.my_name == "甲")
        assert after_hit.my_match_score == 1 and after_hit.my_total_score == 3
        assert after_hit.standings[0].total_score == 3
        assert after_hit.standings[0].scores_per_match == [2]
        assert {entry.player: entry.total_score for entry in playoff_opening[0].standings} == {
            "甲": 2, "乙": 2, "丙": 0,
        }

    def test_score_tie_at_cap_keeps_champions_with_different_auction_scores(self):
        game = replace(TEST_GAME, daily_pool=(1, 1), regular_matches=1,
                       super_hit_score=1, max_playoffs=2)
        director = self.FakeDirector()
        players = make_bid_players({
            (1, 1): {"甲": (2, None), "乙": (0, 1), "丙": (1, None)},
            (1, 2): {"甲": (2, None), "乙": (0, 1), "丙": (1, None)},
        })

        result = run_tournament(players, game, director=director)

        assert result.playoff_cap_hit
        assert result.playoff_count == 2
        assert len(result.match_logs) == 3
        assert first_place_tied(result.final_standings)
        assert totals(result) == {"甲": 2, "乙": 0, "丙": 0}
        assert scores(result) == {"甲": 2, "乙": 2, "丙": 0}
        assert ranks(result) == {"甲": 1, "乙": 1, "丙": 3}
        assert [log.match_index for log in director.match_end_calls] == [1, 2]
        assert director.final_calls == [(result.final_standings, result.match_logs[2])]

    def test_last_match_skips_match_end_when_decided(self):
        director = self.FakeDirector()
        players = make_players({i: "甲" for i in range(1, 6)})
        result = run_tournament(players, TEST_GAME, director=director)

        # 前 4 局仍有下一局，触发局末发言；第 5 局（最后一局）跳过局末发言
        assert [log.match_index for log in director.match_end_calls] == [1, 2, 3, 4]
        # 终局感言恰好一次，且拿到最后一局（第 5 局）的完整出价日志
        assert len(director.final_calls) == 1
        _, final_log = director.final_calls[0]
        assert final_log.match_index == 5
        assert final_log is result.match_logs[-1]

    def test_match_end_respects_playoff_cap(self):
        # 上限缩到 2：全员每天出 0 撞车永远流局、人人 0 分永远并列，直到封顶
        game = replace(TEST_GAME, max_playoffs=2)
        director = self.FakeDirector()
        players = make_players({})  # 无人被指定为赢家，全员每天出 0 撞车流局
        result = run_tournament(players, game, director=director)

        assert result.playoff_cap_hit
        assert result.playoff_count == 2
        # 第 5 局后还有加赛空间，第 6 局（加赛 1/2）后也有；
        # 第 7 局（加赛 2/2）封顶为最后一局，跳过局末发言
        assert [log.match_index for log in director.match_end_calls] == [1, 2, 3, 4, 5, 6]
        # 终局感言拿到的是封顶的第 7 局（最后一局）日志
        assert len(director.final_calls) == 1
        _, final_log = director.final_calls[0]
        assert final_log.match_index == 7
        assert final_log is result.match_logs[-1]


def test_daily_callback_reports_cumulative_scores_and_settled_coin_snapshots(capsys):
    game = replace(TEST_GAME, initial_coins=12, daily_salary=7, daily_pool=(3, 3),
                   super_cost=3, super_bounty=5, super_hit_score=2,
                   regular_matches=2, max_playoffs=0)
    table = {
        (1, 1): {"甲": (2, None), "乙": (0, 1), "丙": (1, None)},
        (1, 2): {"乙": (3, None)},
        (2, 1): {"甲": (1, None)},
        (2, 2): {"乙": (1, None)},
    }
    reports, observations = [], []

    def report(log, standings, coins):
        reports.append((log.to_dict(), standings, coins))

    result = run_tournament(make_bid_players(table, observations), game, on_day_end=report)
    assert [(log["match_index"], len(log["days"])) for log, _, _ in reports] == [
        (1, 1), (1, 2), (2, 1), (2, 2),
    ]
    assert [{entry.player: entry.total_score for entry in standings} for _, standings, _ in reports] == [
        {"甲": 3, "乙": 2, "丙": 0}, {"甲": 3, "乙": 5, "丙": 0},
        {"甲": 6, "乙": 5, "丙": 0}, {"甲": 6, "乙": 8, "丙": 0},
    ]
    assert [coins for _, _, coins in reports] == [
        {"甲": 10, "乙": 14, "丙": 11}, {"甲": 17, "乙": 18, "丙": 18},
        {"甲": 11, "乙": 12, "丙": 12}, {"甲": 18, "乙": 18, "丙": 19},
    ]
    assert reports[0][0]["scores"] == {"甲": 3, "乙": 2, "丙": 0}
    assert reports[2][0]["scores"] == {"甲": 3, "乙": 0, "丙": 0}
    assert reports[2][1][0].scores_per_match == [3]
    assert [(entry.player, entry.rank, entry.total_score) for entry in reports[3][1]] == [
        (entry.player, entry.rank, entry.total_score) for entry in result.final_standings
    ]

    # 展示回调不改变玩家观察，未注入展示的流程保持静默。
    without_report = []
    silent_result = run_tournament(make_bid_players(table, without_report), game)
    assert observations == without_report
    assert result == silent_result
    assert capsys.readouterr().out == ""


def test_daily_callback_also_runs_for_playoffs_and_preserves_tied_ranks():
    game = replace(TEST_GAME, daily_pool=(3,), regular_matches=1, max_playoffs=1)
    reports = []

    def report(log, standings, coins):
        reports.append((deepcopy(log), standings, coins))

    run_tournament(make_bid_players({}), game, on_day_end=report)
    assert [(log.match_index, log.is_playoff) for log, _, _ in reports] == [(1, False), (2, True)]
    assert all(entry.rank == 1 for _, standings, _ in reports for entry in standings)


class TestPublicationOnMatchEnd:
    def test_callback_receives_match_log_and_standings_after_each_match(self):
        publications = []
        players = make_players({1: "甲", 2: "乙", 3: "丙", 4: "丁", 5: "戊", 6: "甲"})
        run_tournament(players, TEST_GAME, on_match_end=lambda log, standings: publications.append((log, standings)))

        # 正赛后甲乙丙丁戊五人 24 分并列第一，触发加赛；第 6 局甲包揽后唯一
        assert len(publications) == 6
        first_log, first_standings = publications[0]
        # 局日志完整：固定 6 天，含每天每人出价
        assert len(first_log.days) == 6
        assert len(first_log.days[0].players) == 6
        # 局末公开排名含拍卖分明细，且反映该局结果
        first_entry = next(e for e in first_standings if e.player == "甲")
        assert first_entry.total_auction_score == 24
        assert first_entry.auction_scores_per_match == [24]
        assert first_log.scores == {"甲": 24, "乙": 0, "丙": 0, "丁": 0, "戊": 0, "己": 0}
        assert first_entry.total_score == 24
        assert first_entry.scores_per_match == [24]

    def test_players_observe_match_index_and_standings(self):
        # 第 2 局第 1 天时，玩家应能看到局号和含第 1 局成绩的当前排名
        seen = []

        def make_strategy(name):
            def strategy(obs):
                if obs.match_index == 2 and obs.day == 1:
                    seen.append(obs)
                return 2 if match_winners.get(obs.match_index) == name else 1
            return strategy

        match_winners = {i: "甲" for i in range(1, 8)}
        players = [ScriptedPlayer(name, make_strategy(name)) for name in NAMES]
        run_tournament(players, TEST_GAME)

        assert len(seen) == 6
        obs = seen[0]
        assert obs.my_match_score == 0
        assert obs.my_total_score == 24
        leader = obs.standings[0]
        assert leader.player == "甲" and leader.total_auction_score == 24
        assert leader.total_score == 24 and leader.scores_per_match == [24]
