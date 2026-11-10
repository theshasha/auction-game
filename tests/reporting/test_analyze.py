"""analyze 的读取和计算口径；合成对局见 tests/support/analyze_runs.py，打印见 tests/cli/test_analyze_output.py。"""

from collections import Counter
from copy import deepcopy
from dataclasses import replace

import pytest

from auction_game.reporting.analyze import (collect_player_stats, collect_run_stats, collect_team_stats,
                                             find_latest_ai_run, load_match_logs)
from tests.support.analyze_runs import (ANALYZE_GAME, make_match_1, make_match_2, make_matches, make_team_match,
                                        write_matches)
from tests.support.factories import TEAM_OF, TEST_GAME, make_day, make_log


def rows_by_name(stats: dict) -> dict:
    return {row["name"]: row for row in stats["rows"]}


def test_score_matrices_and_top_match_counts():
    stats = collect_player_stats(make_matches())
    assert stats["match_labels"] == ["正赛第 1 局", "正赛第 2 局"]
    assert stats["auction_score_matrix"]["乙"] == [1, 1]
    assert stats["score_matrix"]["乙"] == [3, 1]
    assert stats["score_matrix"]["己"] == [1, 0]
    rows = rows_by_name(stats)
    # 首局乙命中后独占最高，次局乙/戊并列最高。
    assert rows["乙"]["tied_top_matches"] == 1
    assert rows["乙"]["solo_top_matches"] == 1
    assert rows["己"]["tied_top_matches"] == 0
    assert rows["戊"]["tied_top_matches"] == 1
    assert rows["甲"]["tied_top_matches"] == 0
    assert sum(row["solo_top_matches"] for row in stats["rows"]) == 1
    # 排序：总分降序，同分按名字（己 U+5DF1 < 戊 U+620A）
    assert [row["name"] for row in stats["rows"]] == ["乙", "己", "戊", "丁", "丙", "甲"]


@pytest.mark.parametrize("all_void", [False, True])
def test_solo_top_and_all_zero_tied_matches(all_void):
    days = [make_day(day, [2, 2] if all_void else [2, 1]) for day in (1, 2)]
    match = make_log(days=days, game=ANALYZE_GAME).to_dict()
    rows = rows_by_name(collect_player_stats([{"match": match}]))
    assert rows["甲"]["solo_top_matches"] == (0 if all_void else 1)
    assert rows["乙"]["solo_top_matches"] == 0
    assert rows["甲"]["tied_top_matches"] == int(all_void)
    assert rows["乙"]["tied_top_matches"] == int(all_void)
    if all_void:
        assert rows["甲"]["avg_bid"] is None


def test_player_detail_metrics():
    rows = rows_by_name(collect_player_stats(make_matches()))
    # 乙：超 1 用 1 中；撞车扣款 = 3（第 1 局第 2 天互撞）；均出 = (4+3+3+4)/4
    assert rows["乙"]["total_score"] == 4
    assert rows["乙"]["auction_score"] == 2
    assert rows["乙"]["super_score"] == 2
    assert rows["乙"]["super_uses"] == 1 and rows["乙"]["super_hits"] == 1
    assert rows["乙"]["super_net_coins"] == 1
    assert rows["乙"]["collision_burn"] == 3
    assert rows["乙"]["normal_collision_burn"] == 3
    assert rows["乙"]["unique_sniped_burn"] == 0
    assert rows["乙"]["avg_bid"] == 3.5
    assert rows["乙"]["end_coins"] == [4, 6]
    assert rows["乙"]["zero_bids"] == 0
    # 甲：没用超级出价；撞车扣款 = 5+3；均出 = (5+3+0+0)/4；出 0 两次
    assert rows["甲"]["super_uses"] == 0 and rows["甲"]["super_hits"] == 0
    assert rows["甲"]["super_net_coins"] == 0
    assert rows["甲"]["collision_burn"] == 8
    assert rows["甲"]["normal_collision_burn"] == 3
    assert rows["甲"]["unique_sniped_burn"] == 5
    assert rows["甲"]["avg_bid"] == 2.0
    assert rows["甲"]["zero_bids"] == 2
    # 丙：超级出价 1 次，命中 0 次（狙 6 落空）；无撞车扣款；出 0 一次
    assert rows["丙"]["super_uses"] == 1 and rows["丙"]["super_hits"] == 0
    assert rows["丙"]["super_net_coins"] == -2
    assert rows["丙"]["collision_burn"] == 0
    assert rows["丙"]["avg_bid"] == 1.25
    assert rows["丙"]["zero_bids"] == 1
    # 丁：没用超级出价；撞车扣款 = 2（第 2 局第 2 天互撞）
    assert rows["丁"]["super_uses"] == 0 and rows["丁"]["super_hits"] == 0
    assert rows["丁"]["collision_burn"] == 2
    assert rows["丁"]["avg_bid"] == 2.0
    assert rows["丁"]["zero_bids"] == 0
    # 戊 / 己：各 1 胜；戊 出 0 一次、己 撞车扣款 2 且出 0 一次
    assert rows["戊"]["total_score"] == 1 and rows["戊"]["zero_bids"] == 1
    assert rows["己"]["total_score"] == 1 and rows["己"]["collision_burn"] == 2
    assert rows["己"]["avg_bid"] == 3.0 and rows["己"]["zero_bids"] == 1


@pytest.mark.parametrize("bids,super_bids,winner,winning_bid,collisions,changed,collision_hits,"
                         "normal_burn,sniped_burn", [
    ([5, 4, 3], {"乙": (6, False)}, "甲", 5, [], 0, 0, 0, 0),  # 落空
    ([5, 4, 3], {"乙": (3, True)}, "甲", 5, [3], 0, 0, 0, 3),  # 命中低价
    ([5, 5, 3], {"丙": (5, True)}, "丙", 3, [5], 0, 1, 10, 0),  # 原本就互撞
    ([5, 5, 3, 2], {"丙": (5, True), "丁": (5, True)}, "丙", 3, [5], 0, 2, 10, 0),
    ([5, 5, 3, 1], {"丙": (5, True), "丁": (3, True)}, "丁", 1, [3, 5], 1, 1, 10, 3),
    ([5, 4, 3], {"乙": (5, True), "丙": (5, True)}, "乙", 4, [5], 1, 0, 0, 5),
    ([5, 3, 1], {"甲": (3, True), "乙": (1, True), "丙": (5, True)},
     None, None, [1, 3, 5], 1, 0, 0, 9),  # 全部被狙后流局
    ([2, 2, 1, 1], {"甲": (5, False)}, None, None, [1, 2], 0, 0, 6, 0),
    ([0, 2, 2], {"乙": (0, True)}, None, None, [0, 2], 1, 0, 4, 0),  # 移除后 0 也能获胜
])
def test_super_result_days_compare_all_supers_once_without_changing_logs(
    bids, super_bids, winner, winning_bid, collisions, changed, collision_hits, normal_burn, sniped_burn,
):
    days = [
        make_day(1, bids, super_bids={name: value for name, (value, _) in super_bids.items()}),
        make_day(2, [0] * len(bids)),
    ]
    # 场景本身由引擎结算，这里确认它就是参数描述的那种情况。
    assert (days[0].winner, days[0].winning_bid, days[0].collision_values) == (winner, winning_bid, collisions)
    assert {result.player: result.super_hit for result in days[0].players if result.super_bid is not None} == {
        name: hit for name, (_, hit) in super_bids.items()}
    matches = [{"match": make_log(days=days, game=ANALYZE_GAME).to_dict()}]
    original = deepcopy(matches)
    stats = collect_player_stats(matches)
    assert stats["super_days"] == 1
    assert stats["super_changed_days"] == changed
    assert stats["super_collision_hits"] == collision_hits
    assert sum(row["normal_collision_burn"] for row in stats["rows"]) == normal_burn
    assert sum(row["unique_sniped_burn"] for row in stats["rows"]) == sniped_burn
    assert sum(row["collision_burn"] for row in stats["rows"]) == normal_burn + sniped_burn
    for row in stats["rows"]:
        assert row["collision_burn"] == row["normal_collision_burn"] + row["unique_sniped_burn"]
    assert matches == original


def test_super_result_days_without_super_usage():
    matches = [{"match": make_match_2()}]
    matches[0]["match"]["match_index"] = 1
    stats = collect_player_stats(matches)
    assert stats["super_days"] == 0
    assert stats["super_changed_days"] == 0
    assert stats["super_collision_hits"] == 0
    assert all(row["super_net_coins"] == 0 for row in stats["rows"])


def test_super_collision_hits_include_void_days():
    days = [
        make_day(1, [0, 0, 3, 3], super_bids={"乙": 3, "丙": 0, "丁": 0}),
        make_day(2, [0, 0, 0, 0]),
    ]
    matches = [{"match": make_log(days=days, game=ANALYZE_GAME).to_dict()}]
    stats = collect_player_stats(matches)
    assert stats["super_collision_hits"] == 3  # 同一天、两个价位，按三笔使用分别计数。
    assert stats["super_changed_days"] == 0


def test_super_result_days_do_not_simulate_future():
    game = replace(ANALYZE_GAME, daily_salary=0)
    days = [
        make_day(1, [0, 5, 3], super_bids={"甲": 6}, game=game),
        make_day(2, [0, 0, 3], game=game),
    ]
    # 移除首日超级出价后只重判首日，不重算次日余额或出价。
    log = make_log(days=days, game=game).to_dict()
    stats = collect_player_stats([{"match": log}])
    assert stats["super_days"] == 1
    assert stats["super_changed_days"] == 0


@pytest.mark.parametrize("latest_type,older_type", [
    ("ai-test", "ai"), ("ai", "ai-test"), ("team", "ai"), ("team-test", "team"), ("ai", "team-test"),
])
def test_default_analysis_selects_latest_real_ai(tmp_path, monkeypatch, latest_type, older_type):
    monkeypatch.setattr("auction_game.reporting.analyze.LOG_DIR", tmp_path)
    write_matches(tmp_path / f"20261016_110000_{older_type}", make_matches(), run_type=older_type)
    latest = tmp_path / f"20261016_120000_{latest_type}"
    write_matches(latest, make_matches(), run_type=latest_type)
    write_matches(tmp_path / "20261016_130000_offline", make_matches())
    write_matches(tmp_path / "20261016_130000_team-offline", make_matches(), run_type="team-offline")
    (tmp_path / "20261016_140000_ai").mkdir()  # 还没有完成的局
    write_matches(tmp_path / "archive_20261016_160000_ai", make_matches(), run_type="ai")
    assert find_latest_ai_run() == latest


@pytest.mark.parametrize("run_type", ["ai", "ai-test"])
def test_default_analysis_orders_collision_suffix_numerically(tmp_path, monkeypatch, run_type):
    monkeypatch.setattr("auction_game.reporting.analyze.LOG_DIR", tmp_path)
    for suffix in ("", "_2", "_10"):
        write_matches(tmp_path / f"20261016_120000_{run_type}{suffix}", make_matches(), run_type=run_type)
    assert find_latest_ai_run() == tmp_path / f"20261016_120000_{run_type}_10"


def test_default_analysis_rejects_only_offline(tmp_path, monkeypatch):
    monkeypatch.setattr("auction_game.reporting.analyze.LOG_DIR", tmp_path)
    write_matches(tmp_path / "20261016_120000_offline", make_matches())
    write_matches(tmp_path / "20261016_120000_team-offline", make_matches(), run_type="team-offline")
    with pytest.raises(SystemExit, match="没有.*ai、ai-test、team、team-test"):
        find_latest_ai_run()


def test_team_stats_count_collisions_and_teammate_only_misses():
    match = make_team_match()
    # 场景本身由引擎按阵营结算，这里确认它就是 make_team_match 描述的那种情况。
    assert [{result["player"]: result["super_hit"] for result in day["players"] if result["super_bid"] is not None}
            for day in match["days"]] == [{"丙": False, "丁": False, "己": True}, {"甲": True, "戊": False}]
    assert [day["winner"] for day in match["days"]] == ["己", "己"]
    assert collect_team_stats([{"match": match}], TEAM_OF) == {
        "红队": Counter(team_collisions=2, super_uses=2, super_hits=1, teammate_only_misses=1),
        "蓝队": Counter(team_collisions=1, super_uses=3, super_hits=1, teammate_only_misses=1),
    }


def test_nondefault_game_drives_labels_and_net_coins():
    game = replace(TEST_GAME, initial_coins=20, daily_salary=4, daily_pool=(2, 4, 3),
                   max_bid=9, super_cost=1, super_bounty=5, regular_matches=2, max_playoffs=4)
    days = [
        make_day(1, [9, 7, 5], pool=2, super_bids={"乙": 9}, game=game),
        make_day(2, [2, 2, 0], pool=4, game=game),
        make_day(3, [9, 3, 1], pool=3, game=game),
    ]
    log = make_log(match_index=3, is_playoff=True, game=game, days=days).to_dict()
    stats = collect_player_stats([{"match": log}])
    assert stats["match_labels"] == ["加赛第 1 局（总第 3 局）"]
    assert rows_by_name(stats)["丙"]["tied_top_matches"] == 1
    assert rows_by_name(stats)["乙"]["avg_bid"] == 4
    assert rows_by_name(stats)["乙"]["super_net_coins"] == 4


def test_loading_allows_completed_matches_from_unfinished_run(tmp_path):
    match = make_match_1()
    match["game"]["regular_matches"] = 5
    matches = [{"match": match}]
    write_matches(tmp_path, matches)
    assert load_match_logs(tmp_path) == matches


def test_loading_orders_matches_by_file_number(tmp_path):
    matches = make_matches()
    matches = [{"match": {**deepcopy(matches[index % 2]["match"]), "match_index": index + 1}}
               for index in range(10)]
    write_matches(tmp_path, list(reversed(matches)))
    assert load_match_logs(tmp_path) == matches


def test_mean_bids_exclude_void_days():
    days = [
        make_day(1, [2, 2, 2, 2, 2, 2]),
        make_day(2, [0, 3, 0, 2, 5, 4]),
    ]
    match = make_log(days=days, game=ANALYZE_GAME).to_dict()
    matches = [{"match": match}]
    player_stats = collect_player_stats(matches)
    rows = rows_by_name(player_stats)
    assert rows["甲"]["avg_bid"] == 0
    assert rows["乙"]["avg_bid"] == 3
    assert rows["甲"]["collision_burn"] == 2  # 流局日撞车仍然扣币
    assert rows["甲"]["normal_collision_burn"] == 2
    assert rows["甲"]["unique_sniped_burn"] == 0
    run_stats = collect_run_stats(matches, player_stats)
    assert run_stats["void_days"] == 1
    assert list(run_stats["bids_by_day_index"]) == [2]  # 流局日的出价不计入，奖池计入
    assert run_stats["pools_by_day_index"] == {1: [1], 2: [1]}


def test_aggregate_zero_bids_include_super_but_player_zero_bids_only_normal():
    days = [
        make_day(1, [0, 0, 1, 1, 0, 0], super_bids={"丙": 0}),
        make_day(2, [1, 0, 2, 3, 4, 5], super_bids={"甲": 0}),
    ]
    match = make_log(days=days, game=ANALYZE_GAME).to_dict()
    matches = [{"match": match}]
    player_stats = collect_player_stats(matches)
    rows = rows_by_name(player_stats)
    assert rows["甲"]["zero_bids"] == 1  # 流局日正常 0 计入，两笔狙 0 的超级出价不计
    assert rows["乙"]["zero_bids"] == 2
    run_stats = collect_run_stats(matches, player_stats)
    assert (run_stats["zero_bids"], run_stats["zero_bid_wins"]) == (7, 0)


def test_total_score_ranking_can_reverse_auction_score_ranking():
    game = replace(ANALYZE_GAME, daily_pool=(1, 1), super_hit_score=3)
    days = [
        make_day(1, [5, 4, 3], super_bids={"乙": 3}, game=game),
        make_day(2, [5, 1, 0], game=game),
    ]
    match = make_log(days=days, game=game).to_dict()
    stats = collect_player_stats([{"match": match}])
    assert [row["name"] for row in stats["rows"]] == ["乙", "甲", "丙"]
    assert rows_by_name(stats)["乙"]["solo_top_matches"] == 1
    assert stats["auction_score_matrix"]["乙"] == [0]
    assert stats["score_matrix"]["乙"] == [3]


def test_run_stats_sum_players_and_group_days_by_index():
    matches = make_matches()
    stats = collect_run_stats(matches, collect_player_stats(matches))
    assert (stats["total_days"], stats["void_days"], stats["playoff_count"]) == (4, 0, 0)
    assert (stats["collision_burn"], stats["normal_collision_burn"], stats["unique_sniped_burn"]) == (15, 10, 5)
    assert (stats["super_uses"], stats["super_hits"]) == (2, 1)
    assert (stats["super_cost_spent"], stats["super_bounty_minted"]) == (4, 3)
    assert stats["pools_by_day_index"] == {1: [1, 1], 2: [1, 1]}
    # 第 1 局第 1 天的两笔超级出价按价位计入这一天的出价。
    assert sorted(stats["bids_by_day_index"][1]) == sorted([5, 4, 3, 2, 1, 0, 5, 6, 0, 3, 0, 2, 5, 4])
    assert stats["winning_bids"] == [4, 6, 5, 4]
    assert stats["cap_wins"] == 0 and stats["zero_bids"] == 5 and stats["zero_bid_wins"] == 0
    assert (min(stats["end_coins"]), max(stats["end_coins"])) == (2, 10)
