"""analyze.py 玩家维度统计的零网络测试。

合成对局（两局，每天一行出价 = NAMES 顺序）：

第 1 局
  第 1 天：出价 [5,4,3,2,1,0]；乙 超级狙 5（命中）、丙 超级狙 6（空）；
            5 被撞车（甲 扣款 5），乙 以 4 获胜。
  第 2 天：出价 [3,3,1,2,0,6]；3 互撞（甲、乙 扣款 3），己 以 6 获胜。
  胜场：乙 1、己 1（局冠军 乙/己 各 0.5）。
  第 2 局
  第 1 天：出价 [0,3,0,2,5,4]；甲 非法提交整单转正常 0、无超级；
           戊 以 5 获胜。
  第 2 天：出价 [0,4,1,2,3,2]；2 互撞（丁、己 扣款 2），乙 以 4 获胜。
  胜场：乙 1、戊 1（局冠军 乙/戊 各 0.5）。

手算期望值见各断言注释；局末余额按日志 game（初始 7、日工资 3、超级成本 2、悬赏 3）重放。
"""

import json
from copy import deepcopy
from dataclasses import replace

import pytest

from tests.support.factories import TEST_GAME, make_day, make_log

from auction_game.reporting.analyze import (
    analyze, collect_player_stats, find_latest_ai_run, load_match_logs, main, recompute_remaining_coins,
    validate_match, validate_matches,
)
from auction_game.reporting.run_output import initialize_run

ANALYZE_GAME = replace(TEST_GAME, daily_pool=(1, 1))


def make_match_1() -> dict:
    days = [
        make_day(1, [5, 4, 3, 2, 1, 0], pool=1, winner="乙", winning_bid=4,
                 collision_values=[5], wins_taken=1,
                 super_bids={"乙": (5, True), "丙": (6, False)}),
        make_day(2, [3, 3, 1, 2, 0, 6], pool=1, winner="己", winning_bid=6,
                 collision_values=[3], wins_taken=1),
    ]
    wins = {"甲": 0, "乙": 1, "丙": 0, "丁": 0, "戊": 0, "己": 1}
    return make_log(match_index=1, days=days, wins=wins, game=ANALYZE_GAME).to_dict()


def make_match_2() -> dict:
    day_1 = make_day(1, [0, 3, 0, 2, 5, 4], pool=1, winner="戊", winning_bid=5,
                     collision_values=[0], wins_taken=1)
    day_1.bids[0].raw_bid = -1
    day_1.bids[0].status = "illegal"  # 非法整单只保留正常 0，无超级记录与成本
    days = [
        day_1,
        make_day(2, [0, 4, 1, 2, 3, 2], pool=1, winner="乙", winning_bid=4,
                 collision_values=[2], wins_taken=1),
    ]
    wins = {"甲": 0, "乙": 1, "丙": 0, "丁": 0, "戊": 1, "己": 0}
    return make_log(match_index=2, days=days, wins=wins, game=ANALYZE_GAME).to_dict()


def make_matches() -> list[dict]:
    return [
        {"match": make_match_1(), "standings_after_match": []},
        {"match": make_match_2(), "standings_after_match": []},
    ]


def rows_by_name(stats: dict) -> dict:
    return {row["name"]: row for row in stats["rows"]}


def test_win_matrix_and_champions():
    stats = collect_player_stats(make_matches())
    assert stats["match_labels"] == ["1", "2"]
    assert stats["win_matrix"]["乙"] == [1, 1]
    assert stats["win_matrix"]["己"] == [1, 0]
    rows = rows_by_name(stats)
    # 局冠军：第 1 局 乙/己 并列各 0.5，第 2 局 乙/戊 并列各 0.5
    assert rows["乙"]["match_champions"] == 1.0
    assert rows["己"]["match_champions"] == 0.5
    assert rows["戊"]["match_champions"] == 0.5
    assert rows["甲"]["match_champions"] == 0.0
    # 排序：总胜场降序，同胜场按名字（己 U+5DF1 < 戊 U+620A）
    assert [row["name"] for row in stats["rows"]] == ["乙", "己", "戊", "丁", "丙", "甲"]


def test_player_detail_metrics():
    rows = rows_by_name(collect_player_stats(make_matches()))
    # 乙：超 1 用 1 中；撞车扣款 = 3（第 1 局第 2 天互撞）；均出 = (4+3+3+4)/4
    assert rows["乙"]["total_wins"] == 2
    assert rows["乙"]["super_uses"] == 1 and rows["乙"]["super_hits"] == 1
    assert rows["乙"]["super_net_coins"] == 1
    assert rows["乙"]["collision_burn"] == 3
    assert rows["乙"]["avg_bid"] == 3.5
    assert rows["乙"]["end_coins"] == [4, 6]  # 局1: 7-4-2+3=4→+3-3=4；局2: 7→+3-4=6
    assert rows["乙"]["illegal"] == 0 and rows["乙"]["zero_bids"] == 0
    # 甲：无超级；撞车扣款 = 5+3；均出 = (5+3+0+0)/4；违规 1 笔；出 0 两次
    assert rows["甲"]["super_uses"] == 0 and rows["甲"]["super_hits"] == 0
    assert rows["甲"]["super_net_coins"] == 0
    assert rows["甲"]["collision_burn"] == 8
    assert rows["甲"]["avg_bid"] == 2.0
    assert rows["甲"]["end_coins"] == [2, 10]  # 局1: 7-5=2→+3-3=2；局2: 7→+3=10
    assert rows["甲"]["illegal"] == 1 and rows["甲"]["zero_bids"] == 2
    # 丙：超 1 用 0 中（狙 6 空）；无撞车扣款；出 0 一次
    assert rows["丙"]["super_uses"] == 1 and rows["丙"]["super_hits"] == 0
    assert rows["丙"]["super_net_coins"] == -2
    assert rows["丙"]["collision_burn"] == 0
    assert rows["丙"]["avg_bid"] == 1.25
    assert rows["丙"]["end_coins"] == [8, 10]
    assert rows["丙"]["zero_bids"] == 1
    # 丁：无超级；撞车扣款 = 2（第 2 局第 2 天互撞）
    assert rows["丁"]["super_uses"] == 0 and rows["丁"]["super_hits"] == 0
    assert rows["丁"]["collision_burn"] == 2
    assert rows["丁"]["avg_bid"] == 2.0
    assert rows["丁"]["end_coins"] == [10, 8]
    assert rows["丁"]["zero_bids"] == 0
    # 戊 / 己：各 1 胜；戊 出 0 一次、己 撞车扣款 2 且出 0 一次
    assert rows["戊"]["total_wins"] == 1 and rows["戊"]["end_coins"] == [10, 5]
    assert rows["戊"]["zero_bids"] == 1
    assert rows["己"]["total_wins"] == 1 and rows["己"]["collision_burn"] == 2
    assert rows["己"]["avg_bid"] == 3.0 and rows["己"]["end_coins"] == [4, 8]
    assert rows["己"]["zero_bids"] == 1


def test_analyze_prints_player_section(tmp_path, capsys):
    run_dir = tmp_path / "20260909_120000_ai"
    run_dir.mkdir()
    write_matches(run_dir, make_matches())
    analyze(run_dir)
    output = capsys.readouterr().out
    # 新统计段存在且位于「单局 ≥3 胜场」列表之前
    assert "玩家统计" in output
    assert "明细（胜场 / 局冠军" in output
    assert output.index("玩家统计") < output.index("单局 ≥3 胜场")
    # 矩阵行与明细行内容
    assert "  乙：1 1" in output
    assert "  乙：胜2 局冠1 超1用1中（100%） 超级净金币收益1 撞车扣款3 均出3.50 局末余额4/6 违规0笔 出0共0笔" in output
    assert "  甲：胜0 局冠0 超0用 超级净金币收益0 撞车扣款8 均出2.00 局末余额2/10 违规1笔 出0共2笔" in output
    assert "撞车扣款：全场因撞车被没收" in output
    assert "成本消耗 4 金币，悬赏发放 3 金币，超级净金币收益 -1 金币" in output
    assert "使用超级出价 1 天，改变当日结果 1 天" in output
    assert "含有效正常报价及超级数值" in output
    assert "获胜价平均" in output


@pytest.mark.parametrize("bids,super_bids,winner,winning_bid,collisions,changed", [
    ([5, 4, 3], {"乙": (6, False)}, "甲", 5, [], 0),  # 落空
    ([5, 4, 3], {"乙": (3, True)}, "甲", 5, [3], 0),  # 命中低价
    ([5, 5, 3], {"丙": (5, True)}, "丙", 3, [5], 0),  # 原本就互撞
    ([5, 4, 3], {"乙": (5, True), "丙": (5, True)}, "乙", 4, [5], 1),
    ([5, 3, 1], {"甲": (3, True), "乙": (1, True), "丙": (5, True)},
     None, None, [1, 3, 5], 1),  # 全部被狙后流局
    ([2, 2, 1, 1], {"甲": (5, False)}, None, None, [1, 2], 0),
    ([0, 2, 2], {"乙": (0, True)}, None, None, [0, 2], 1),  # 移除后 0 也能获胜
])
def test_super_result_days_compare_all_supers_once_without_changing_logs(
    bids, super_bids, winner, winning_bid, collisions, changed,
):
    days = [
        make_day(1, bids, 1, winner, winning_bid, collisions, int(winner is not None),
                 is_void=winner is None, super_bids=super_bids),
        make_day(2, [0] * len(bids), 2 if winner is None else 1, None, None, [0], is_void=True),
    ]
    names = [bid.player for bid in days[0].bids if not bid.is_super]
    wins = {name: int(name == winner) for name in names}
    matches = [{"match": make_log(days=days, wins=wins, game=ANALYZE_GAME).to_dict()}]
    original = deepcopy(matches)
    stats = collect_player_stats(matches)
    assert stats["super_days"] == 1
    assert stats["super_changed_days"] == changed
    assert matches == original


def test_super_result_days_without_super_usage():
    stats = collect_player_stats([{"match": make_match_2()}])
    assert stats["super_days"] == 0
    assert stats["super_changed_days"] == 0
    assert all(row["super_net_coins"] == 0 for row in stats["rows"])


def test_super_result_days_keep_effective_bids_and_do_not_simulate_future():
    game = replace(ANALYZE_GAME, daily_salary=0)
    days = [
        make_day(1, [0, 5, 3], 1, "乙", 5, [], 1, super_bids={"甲": (6, False)}),
        make_day(2, [0, 0, 3], 1, "丙", 3, [0], 1),
    ]
    # 甲首日落空花掉 2，次日提交 7 被转成 0；移除首日超级也不重算次日余额或报价。
    days[1].bids[0].raw_bid = 7
    days[1].bids[0].status = "illegal"
    log = make_log(days=days, wins={"甲": 0, "乙": 1, "丙": 1}, game=game).to_dict()
    stats = collect_player_stats([{"match": log}])
    assert stats["super_days"] == 1
    assert stats["super_changed_days"] == 0


def test_super_result_days_do_not_revalidate_rejected_raw_bid():
    match = make_match_1()
    day = match["days"][0]
    # 甲原始提交 7 因非法超级被整单拒绝；反事实仍保留有效正常 0。
    day["bids"][0].update(raw_bid=7, bid=0, status="illegal")
    day["collision_values"] = [0]
    day["super_uses"][0]["hit"] = False
    stats = collect_player_stats([{"match": match}])
    assert stats["super_days"] == 1
    assert stats["super_changed_days"] == 0


def write_matches(run_dir, matches, *, run_type="bot", players=None):
    first = matches[0]["match"]
    paths = initialize_run(run_dir, {"run_type": run_type,
                          "mode": "test_model" if run_type == "ai-test" else "regular", "game": first["game"],
                          "players": players if players is not None else
                          [{"name": name} for name in first["wins"]]}, "test rules")
    for payload in matches:
        paths.match(payload["match"]["match_index"]).write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )


@pytest.mark.parametrize("latest_type,older_type", [("ai-test", "ai"), ("ai", "ai-test")])
def test_default_analysis_selects_latest_valid_real_ai(tmp_path, monkeypatch, latest_type, older_type):
    monkeypatch.setattr("auction_game.reporting.analyze.LOG_DIR", tmp_path)
    write_matches(tmp_path / f"20261016_110000_{older_type}", make_matches(), run_type=older_type)
    latest = tmp_path / f"20261016_120000_{latest_type}"
    write_matches(latest, make_matches(), run_type=latest_type)
    for suffix, run_type in [("bot", "bot"), ("offline", "offline"), ("ai_2", "offline")]:
        write_matches(tmp_path / f"20261016_130000_{suffix}", make_matches(), run_type=run_type)
    (tmp_path / "20261016_140000_ai").mkdir()  # 缺场次快照
    incomplete = make_matches()
    incomplete[0]["match"]["days"].pop()
    write_matches(tmp_path / "20261016_150000_ai-test", incomplete, run_type="ai-test")
    write_matches(tmp_path / "archive_20261016_160000_ai", make_matches(), run_type="ai")
    assert find_latest_ai_run() == latest


@pytest.mark.parametrize("run_type", ["ai", "ai-test"])
def test_default_analysis_orders_collision_suffix_numerically(tmp_path, monkeypatch, run_type):
    monkeypatch.setattr("auction_game.reporting.analyze.LOG_DIR", tmp_path)
    for suffix in ("", "_2", "_10"):
        write_matches(tmp_path / f"20261016_120000_{run_type}{suffix}", make_matches(), run_type=run_type)
    assert find_latest_ai_run() == tmp_path / f"20261016_120000_{run_type}_10"


def test_default_cli_keeps_test_run_type_and_actual_models(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("auction_game.reporting.analyze.LOG_DIR", tmp_path)
    monkeypatch.setattr("sys.argv", ["analyze"])
    players = [{"name": name, "identity_model": {"model": f"identity-{index}"},
                "execution_model": {"protocol": "responses", "model": "actual-test-model"}}
               for index, name in enumerate(make_match_1()["wins"], 1)]
    write_matches(tmp_path / "20261016_120000_ai-test", make_matches(), run_type="ai-test", players=players)
    main()
    output = capsys.readouterr().out
    assert "运行类型：测试模型真实 AI（收费）" in output
    assert "测试运行结果（非身份模型成绩）" in output
    assert "甲：身份 identity-1 → 实际 responses / actual-test-model" in output


def test_default_analysis_rejects_only_bot_and_offline(tmp_path, monkeypatch):
    monkeypatch.setattr("auction_game.reporting.analyze.LOG_DIR", tmp_path)
    for run_type in ("bot", "offline"):
        write_matches(tmp_path / f"20261016_120000_{run_type}", make_matches(), run_type=run_type)
    with pytest.raises(SystemExit, match="没有有效.*ai 或 ai-test"):
        find_latest_ai_run()


def test_nondefault_snapshot_drives_economy_cap_and_playoff_label(tmp_path, capsys, monkeypatch):
    game = replace(TEST_GAME, initial_coins=20, daily_salary=4, daily_pool=(2, 4, 3),
                   max_bid=9, super_cost=1, super_bounty=5, regular_matches=2, max_playoffs=4)
    days = [
        make_day(1, [9, 7, 5], 2, "乙", 7, [9], 2, super_bids={"乙": (9, True)}),
        make_day(2, [2, 2, 0], 4, "丙", 0, [2], 4),
        make_day(3, [9, 3, 1], 3, "甲", 9, [], 3),
    ]
    log = make_log(match_index=3, is_playoff=True, game=game, days=days,
                   wins={"甲": 3, "乙": 2, "丙": 4}).to_dict()
    # 手算：甲 20-9+4-2+4-9=8；乙 20-7-1+5+4-2+4=23；丙 20+4+4=28。
    assert recompute_remaining_coins(log) == {"甲": 8, "乙": 23, "丙": 28}
    stats = collect_player_stats([{"match": log}])
    assert stats["match_labels"] == ["加1"]
    assert rows_by_name(stats)["乙"]["avg_bid"] == 4
    write_matches(tmp_path, [{"match": log}])
    # 分析不能依赖当前配置；当前目录甚至放置无效的 game.yaml。
    (tmp_path / "game.yaml").write_text("not: valid game settings", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    analyze(tmp_path)
    output = capsys.readouterr().out
    assert "获胜价=9（出价上限）占比 33.3%" in output
    assert "成本消耗 1 金币，悬赏发放 5 金币，超级净金币收益 4 金币" in output
    assert rows_by_name(stats)["乙"]["super_net_coins"] == 4
    assert "局末平均剩余金币：19.67（最低 8，最高 28）" in output
    assert "全天总体：平均 4.70（10 笔）" in output  # 聚合均价包含超级出价数值
    # 总奖池 9：恰好 3 胜仍入选，2 胜不入选；门槛固定，不能改成过半。
    assert "单局 ≥3 胜场的玩家-局次数：2" in output
    assert "第 3 局：甲 共 3 胜场" in output
    assert "第 3 局：丙 共 4 胜场" in output
    assert "第 3 局：乙 共 2 胜场" not in output


def test_missing_game_setting_is_rejected():
    match = make_match_1()
    del match["game"]["max_bid"]
    with pytest.raises(ValueError, match="日志 game 参数无效.*max_bid"):
        recompute_remaining_coins(match)


@pytest.mark.parametrize("section,field", [
    ("match", "game"), ("match", "wins"), ("match", "is_playoff"),
    ("day", "super_uses"), ("day", "collision_values"), ("day", "winning_bid"),
    ("bid", "status"), ("bid", "is_super"), ("super_use", "hit"),
])
def test_missing_current_log_field_is_rejected(section, field):
    match = make_match_1()
    records = {"match": match, "day": match["days"][0],
               "bid": match["days"][0]["bids"][0],
               "super_use": match["days"][0]["super_uses"][0]}
    del records[section][field]
    with pytest.raises(ValueError, match=field):
        collect_player_stats([{"match": match}])


def test_loading_wraps_invalid_log_with_filename(tmp_path):
    match = make_match_1()
    del match["days"][0]["bids"][0]["status"]
    write_matches(tmp_path, [{"match": match}])
    with pytest.raises(ValueError, match=r"001.json.*bid 缺少 status"):
        load_match_logs(tmp_path)


@pytest.mark.parametrize("difference", ["game", "players"])
def test_loading_rejects_mixed_matches(tmp_path, difference):
    matches = make_matches()
    second = matches[1]["match"]
    if difference == "game":
        second["game"]["super_cost"] += 1
    else:
        second["wins"]["庚"] = second["wins"].pop("甲")
        for day in second["days"]:
            for bid in day["bids"]:
                if bid["player"] == "甲":
                    bid["player"] = "庚"
    write_matches(tmp_path, matches)
    with pytest.raises(ValueError, match="各局 game 参数与参赛名单必须一致"):
        load_match_logs(tmp_path)


def test_loading_rejects_inconsistent_daily_roster(tmp_path):
    match = make_match_1()
    match["days"][1]["bids"][0]["player"] = "庚"
    write_matches(tmp_path, [{"match": match}])
    with pytest.raises(ValueError, match="参赛名单不一致"):
        load_match_logs(tmp_path)


@pytest.mark.parametrize("day_indices", [[1], [1, 1], [2, 1], [1, 3], [True, 2]])
@pytest.mark.parametrize("entrypoint", [validate_match, recompute_remaining_coins,
                                       validate_matches, collect_player_stats])
def test_public_statistics_reject_incomplete_or_invalid_day_sequence(day_indices, entrypoint):
    match = make_match_1()
    match["days"] = match["days"][:len(day_indices)]
    for day, index in zip(match["days"], day_indices):
        day["day"] = index
    data = [{"match": match}] if entrypoint in (validate_matches, collect_player_stats) else match
    with pytest.raises(ValueError, match=r"第 1 局.*天序.*1.*2"):
        entrypoint(data)


@pytest.mark.parametrize("entrypoint", [validate_matches, collect_player_stats])
def test_public_statistics_reject_duplicate_match_indices(entrypoint):
    matches = make_matches()
    matches[1]["match"]["match_index"] = 1
    with pytest.raises(ValueError, match="局号.*1.*重复"):
        entrypoint(matches)


@pytest.mark.parametrize("index", [0, -1, True, "1"])
def test_public_statistics_reject_invalid_match_index(index):
    match = make_match_1()
    match["match_index"] = index
    with pytest.raises(ValueError, match="match_index 必须是正整数"):
        collect_player_stats([{"match": match}])


def test_loading_rejects_duplicate_match_indices_with_filename(tmp_path):
    matches = make_matches()
    write_matches(tmp_path, matches)
    matches[1]["match"]["match_index"] = 1
    (tmp_path / "data/matches/002.json").write_text(json.dumps(matches[1]), encoding="utf-8")
    with pytest.raises(ValueError, match=r"002.json.*局号.*1.*重复"):
        load_match_logs(tmp_path)


def test_loading_rejects_mismatched_file_number(tmp_path):
    write_matches(tmp_path, [{"match": make_match_2()}])
    (tmp_path / "data/matches/002.json").rename(tmp_path / "data/matches/001.json")
    with pytest.raises(ValueError, match=r"001.json.*match_index.*2.*不一致"):
        load_match_logs(tmp_path)


def test_cli_rejects_missing_day_with_filename(tmp_path, monkeypatch):
    match = make_match_1()
    match["days"].pop()
    write_matches(tmp_path, [{"match": match}])
    monkeypatch.setattr("sys.argv", ["analyze", str(tmp_path)])
    with pytest.raises(SystemExit, match=r"001.json.*第 1 局.*天序"):
        main()


def test_loading_allows_completed_matches_from_unfinished_run(tmp_path, capsys):
    match = make_match_1()
    match["game"]["regular_matches"] = 5
    write_matches(tmp_path, [{"match": match}])
    assert load_match_logs(tmp_path) == [{"match": match}]
    analyze(tmp_path)
    assert "局数：1（正赛 1 + 加赛 0）" in capsys.readouterr().out


def test_loading_orders_matches_by_numeric_file_number(tmp_path):
    matches = make_matches()
    matches[0]["match"]["match_index"] = 2
    matches[1]["match"]["match_index"] = 10
    write_matches(tmp_path, list(reversed(matches)))
    assert load_match_logs(tmp_path) == matches


def test_invalid_game_snapshot_is_rejected():
    match = make_match_1()
    match["game"]["max_bid"] = True
    with pytest.raises(ValueError, match=r"日志 game 参数无效.*max_bid"):
        recompute_remaining_coins(match)


def test_mean_bids_exclude_void_days_but_include_illegal_zero(tmp_path, capsys):
    days = [
        make_day(1, [2, 2, 2, 2, 2, 2], 1, None, None, [2], is_void=True),
        make_day(2, [0, 3, 0, 2, 5, 4], 2, "戊", 5, [0], 2),
    ]
    days[1].bids[0].status = "illegal"
    days[1].bids[0].raw_bid = -1
    match = make_log(days=days, wins={"甲": 0, "乙": 0, "丙": 0, "丁": 0, "戊": 2, "己": 0},
                     game=ANALYZE_GAME).to_dict()
    rows = rows_by_name(collect_player_stats([{"match": match}]))
    assert rows["甲"]["avg_bid"] == 0
    assert rows["乙"]["avg_bid"] == 3
    assert rows["甲"]["illegal"] == 1
    assert rows["甲"]["collision_burn"] == 2  # 流局日撞车仍然扣币
    write_matches(tmp_path, [{"match": match}])
    analyze(tmp_path)
    output = capsys.readouterr().out
    assert "第 1 天：出价 —（全流局），奖池 1.00" in output
    assert "全天总体：平均 2.33（6 笔）" in output


def test_aggregate_zero_bids_include_super_but_player_zero_bids_only_normal(tmp_path, capsys):
    days = [
        make_day(1, [0, 0, 0, 0, 0, 0], 1, None, None, [0], is_void=True,
                 super_bids={"甲": (0, True)}),
        make_day(2, [1, 0, 2, 3, 4, 5], 2, "己", 5, [0], 2,
                 super_bids={"甲": (0, True)}),
    ]
    match = make_log(days=days, wins={"甲": 0, "乙": 0, "丙": 0, "丁": 0, "戊": 0, "己": 2},
                     game=ANALYZE_GAME).to_dict()
    rows = rows_by_name(collect_player_stats([{"match": match}]))
    assert rows["甲"]["zero_bids"] == 1  # 流局日正常 0 计入，两笔超级 0 不计
    assert rows["乙"]["zero_bids"] == 2
    write_matches(tmp_path, [{"match": match}])
    analyze(tmp_path)
    assert "出 0 共 9 笔，其中 0 价获胜 0 次" in capsys.readouterr().out
