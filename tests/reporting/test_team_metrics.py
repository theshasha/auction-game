"""团队赛对比的计算口径；合成对局见 tests/support/analyze_runs.py，打印见 tests/cli/test_team_report_output.py。"""

from dataclasses import replace

import pytest

from auction_game.reporting.team_metrics import (Mean, Ratio, balance_prices, bid_caps, bootstrap_interval,
                                                 collect_team_metrics, load_team_run)
from tests.support.analyze_runs import (TEAM_REPORT_GAME, make_matches, make_team_report_match, make_team_void_match,
                                        write_matches)
from tests.support.factories import TEAM_OF, make_day, make_log


def metrics_of(*matches):
    return collect_team_metrics([{"match": match} for match in matches], TEAM_OF)


def one_day(bids, super_bids=None, game=TEAM_REPORT_GAME):
    day = make_day(1, bids, pool=1, super_bids=super_bids, game=game, team_of=TEAM_OF)
    return make_log(days=[day], game=game).to_dict()


@pytest.mark.parametrize("balance,caps,prices", [
    (7, (7, 5), {7, 5}),
    (12, (7, 7), {7}),      # 两种上限都封顶在出价上限
    (2, (2, 0), {2, 0}),
    (1, (1, None), {1}),    # 余额不够开枪，只有不带狙的上限
])
def test_caps_follow_game_settings(balance, caps, prices):
    assert bid_caps(balance, TEAM_REPORT_GAME) == caps
    assert balance_prices(balance, TEAM_REPORT_GAME) == prices


def test_scores_and_super_reruns():
    match = make_team_report_match()
    # 场景本身由引擎按阵营结算，这里确认它就是 make_team_report_match 描述的那种情况。
    assert [day["winner"] for day in match["days"]] == ["甲", "己"]
    assert [{result["player"]: result["super_hit"] for result in day["players"] if result["super_bid"] is not None}
            for day in match["days"]] == [{"戊": False}, {"乙": True, "丁": True, "戊": True, "己": True}]
    metrics = metrics_of(match)
    assert metrics["teams"] == ("红队", "蓝队")
    assert metrics["total_score"] == (3, 8)
    assert metrics["auction_score"] == (1, 2)
    assert metrics["hit_score"] == (2, 6)
    assert metrics["days_won"] == (1, 1)
    assert metrics["score_by_day"] == {1: (1, 0), 2: (2, 8)}
    assert metrics["days_won_by_day"] == {1: (1, 0), 2: (0, 1)}
    assert metrics["later_score"] == (2, 8)
    assert metrics["first_day_bid"] == (Mean(8, 3), Mean(9, 3))
    assert metrics["first_day_winning_bid"] == Mean(7, 1)
    assert metrics["score_diff"][0] == -5
    assert metrics["super_uses"] == (Ratio(1, 6), Ratio(4, 6))
    assert metrics["super_uses_combined"] == Ratio(5, 12)
    assert metrics["super_hits"] == (Ratio(1, 1), Ratio(3, 4))
    assert metrics["first_day_supers"] == (0, 1)
    assert metrics["super_net_coins"] == (1, 1)
    # 第 2 天：全部去掉时丙以 7 获胜；只去掉红队的，蓝队打掉 1、6、7，己以 4 获胜；只去掉蓝队的，丙仍以 7 获胜。
    assert metrics["auction_without_all"] == (3, 0)
    assert metrics["auction_without_first"] == (1, 2)
    assert metrics["auction_without_second"] == (3, 0)
    assert metrics["switched_to_own"] == (0, 1)
    assert metrics["switched_to_own_points"] == (0, 2)
    assert metrics["win_to_void"] == (0, 0)
    assert metrics["super_auction_gain"] == (-2, 2)
    assert metrics["end_coins"] == (4, 19)


def test_balance_shots_and_targets_use_opponent_balances():
    metrics = metrics_of(make_team_report_match())
    # 蓝队 4 枪都打在红队的余额价上：第 1 天的 5、第 2 天的 1 和 6，以及出价上限 7。
    assert metrics["balance_shots"] == (Ratio(0, 1), Ratio(4, 4))
    assert metrics["balance_hits"] == (Ratio(0, 0), Ratio(3, 4))
    assert metrics["balance_max"] == (Ratio(0, 0), Ratio(1, 1))
    assert metrics["balance_first_day"] == (Ratio(0, 0), Ratio(0, 1))
    assert metrics["balance_read"] == (Ratio(0, 0), Ratio(2, 2))
    # 丙余额 8 出 7，封顶后也是余额价。
    assert metrics["target_balance"] == (Ratio(0, 1), Ratio(3, 3))
    assert (metrics["target_max"], metrics["target_first_day"], metrics["target_read"]) == ((0, 1), (0, 0), (0, 2))
    assert metrics["yesterday_shots"] == (Ratio(1, 1), Ratio(2, 3))
    assert metrics["yesterday_hits"] == (Ratio(1, 1), Ratio(2, 2))
    assert metrics["best_fixed_price"] == (([2, 4], Ratio(2, 2)), ([1, 7], Ratio(2, 2)))


def test_capped_bids_depend_on_super_use():
    metrics = metrics_of(make_team_report_match())
    # 红队：甲第 1 天出 7、第 2 天余额 1 出 1，乙带狙出 8 − 2，丙不带狙出封顶的 7。蓝队都没有贴上限，
    # 第 1 天戊带狙出 3，上限是 5。
    assert metrics["capped"] == (Ratio(4, 6), Ratio(0, 6))
    assert metrics["capped_sniped"] == (Ratio(3, 4), Ratio(0, 0))
    assert metrics["uncapped_sniped"] == (Ratio(0, 2), Ratio(1, 6))
    assert metrics["capped_high_sniped"] == (Ratio(2, 3), Ratio(0, 0))
    assert metrics["uncapped_high_sniped"] == (Ratio(0, 0), Ratio(0, 0))
    assert metrics["low_balance_capped"] == (Ratio(4, 6), Ratio(0, 6))
    # 最高三档是 5–7。
    assert metrics["top_bids"] == (3, 0)
    assert metrics["top_bids_won"] == (Ratio(1, 3), Ratio(0, 0))
    assert metrics["top_bids_sniped"] == (Ratio(2, 3), Ratio(0, 0))
    assert metrics["reused"] == (Ratio(2, 3), Ratio(2, 3))
    assert metrics["later_balance"] == (Mean(17, 3), Mean(22, 3))
    assert metrics["last_day_balance"] == (Mean(17, 3), Mean(22, 3))


def test_capped_bid_without_super_is_not_balance_minus_cost():
    # 甲带狙出 7 − 2 贴上限，乙不带狙出同样的 5 没有贴上限；丙出 7 贴上限。
    metrics = metrics_of(one_day([5, 5, 7, 0, 1, 2], super_bids={"甲": 0}))
    assert metrics["capped"] == (Ratio(2, 3), Ratio(0, 3))


def test_super_caused_void_and_keep_one_rerun():
    metrics = metrics_of(make_team_void_match())
    assert metrics["win_to_void"] == (1, 0)
    assert metrics["switched_to_own"] == (0, 0)
    assert metrics["auction_without_all"] == (1, 0)
    assert metrics["auction_without_first"] == (0, 1)
    assert metrics["auction_without_second"] == (1, 0)
    assert metrics["team_collisions"] == (1, 1)
    assert metrics["intra_burn"] == (2, 0)
    assert metrics["all_burn"] == (7, 3)
    assert metrics["keep_one_days"] == ([(1, 1, 1)], [(1, 1, 1)])
    assert metrics["void_tables"] == [(1, 1, [1, 1, 5], [0, 0, 3])]


def test_keep_one_ignores_collisions_that_cannot_win():
    # 甲乙同出 2，只留一人也低于丁的 6。
    metrics = metrics_of(one_day([2, 2, 0, 6, 1, 3]))
    assert metrics["team_collisions"] == (1, 0)
    assert metrics["intra_burn"] == (4, 0)
    assert metrics["keep_one_days"] == ([], [])


def test_focus_counts_shots_and_full_team_days():
    # 红队三人同狙 3 命中丁；丁戊同狙 1 命中乙；己狙 4 只狙到队友戊，不算命中。
    match = one_day([0, 1, 2, 3, 4, 5], super_bids={"甲": 3, "乙": 3, "丙": 3, "丁": 1, "戊": 1, "己": 4})
    assert [result["super_hit"] for result in match["days"][0]["players"]] == [True] * 5 + [False]
    metrics = metrics_of(match)
    assert metrics["group_shots"] == (Ratio(3, 3), Ratio(2, 2))
    assert metrics["single_shots"] == (Ratio(0, 0), Ratio(0, 1))
    assert metrics["full_team_days"] == (Ratio(1, 1), Ratio(0, 0))
    assert metrics["super_hits"] == (Ratio(3, 3), Ratio(2, 3))


def test_first_day_tables_and_matches():
    metrics = metrics_of(make_team_report_match(1), make_team_report_match(2))
    assert metrics["first_day_tables"] == [(1, [0, 1, 7], [2, 3, 4], 0), (2, [0, 1, 7], [2, 3, 4], 0)]
    assert (metrics["match_count"], metrics["total_days"], metrics["playoff_count"]) == (2, 4, 0)
    # 余额每局从头算，两局结果相同。
    assert metrics["capped"] == (Ratio(8, 12), Ratio(0, 12))


def test_nondefault_cost_moves_super_cap():
    game = replace(TEAM_REPORT_GAME, super_cost=4)
    # 余额 7、成本 4，带狙的上限是 3：甲带狙出 3 贴上限，乙狙 3 打在蓝队的余额价上，甲狙 6 不是。
    metrics = metrics_of(one_day([3, 1, 0, 2, 4, 6], super_bids={"甲": 6, "乙": 3}, game=game))
    assert metrics["capped"] == (Ratio(1, 3), Ratio(0, 3))
    assert metrics["balance_shots"] == (Ratio(1, 2), Ratio(0, 0))


def test_bootstrap_interval_is_reproducible():
    samples = [3, -1, 0, 2, -4]
    assert bootstrap_interval(samples) == bootstrap_interval(samples)
    assert bootstrap_interval([2, 2, 2]) == (6, 6)
    low, high = bootstrap_interval(samples, rounds=2000)
    assert low <= sum(samples) <= high


def test_load_team_run_requires_teams(tmp_path):
    write_matches(tmp_path / "solo", make_matches())
    with pytest.raises(ValueError, match="不是团队赛"):
        load_team_run(tmp_path / "solo")
    write_matches(tmp_path / "team", [{"match": make_team_report_match()}], run_type="team-offline", team_of=TEAM_OF)
    run = load_team_run(tmp_path / "team")
    assert (run.name, run.teams) == ("team", ("红队", "蓝队"))
    assert len(run.matches) == 1
