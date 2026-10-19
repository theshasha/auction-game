"""analyze.py 统计口径测试。

合成对局（两局，每天一行出价 = NAMES 顺序）：

第 1 局
  第 1 天：出价 [5,4,3,2,1,0]；乙 超级狙 5（命中）、丙 超级狙 6（空）；
            5 被撞车（甲 扣款 5），乙 以 4 获胜。
  第 2 天：出价 [3,3,1,2,0,6]；3 互撞（甲、乙 扣款 3），己 以 6 获胜。
  拍卖分：乙 1、己 1；乙超级命中另得 2 分，独占最高。
第 2 局
  第 1 天：出价 [0,3,0,2,5,4]；0 互撞，戊 以 5 获胜。
  第 2 天：出价 [0,4,1,2,3,2]；2 互撞（丁、己 扣款 2），乙 以 4 获胜。
  拍卖分：乙 1、戊 1（乙/戊 各记并列最高 1 局）。

局末余额按 game（初始 7、日工资 3、超级成本 2、悬赏 3）手算后写进日志。
"""

import json
from copy import deepcopy
from dataclasses import asdict, replace

import pytest

from tests.support.factories import TEST_GAME, make_day, make_log

from auction_game.reporting.analyze import analyze, collect_player_stats, find_latest_ai_run, load_match_logs, main
from auction_game.reporting.run_output import initialize_run
from auction_game.game.engine import compute_standings

ANALYZE_GAME = replace(TEST_GAME, daily_pool=(1, 1))


def make_match_1() -> dict:
    days = [
        make_day(1, [5, 4, 3, 2, 1, 0], pool=1, winner="乙", winning_bid=4,
                 collision_values=[5], auction_score=1,
                 super_bids={"乙": (5, True), "丙": (6, False)}),
        make_day(2, [3, 3, 1, 2, 0, 6], pool=1, winner="己", winning_bid=6,
                 collision_values=[3], auction_score=1),
    ]
    auction_scores = {"甲": 0, "乙": 1, "丙": 0, "丁": 0, "戊": 0, "己": 1}
    scores = {**auction_scores, "乙": 3}
    remaining = {"甲": 2, "乙": 4, "丙": 8, "丁": 10, "戊": 10, "己": 4}
    return make_log(match_index=1, days=days, auction_scores=auction_scores,
                    scores=scores, remaining_coins=remaining, game=ANALYZE_GAME).to_dict()


def make_match_2() -> dict:
    days = [
        make_day(1, [0, 3, 0, 2, 5, 4], pool=1, winner="戊", winning_bid=5,
                 collision_values=[0], auction_score=1),
        make_day(2, [0, 4, 1, 2, 3, 2], pool=1, winner="乙", winning_bid=4,
                 collision_values=[2], auction_score=1),
    ]
    auction_scores = {"甲": 0, "乙": 1, "丙": 0, "丁": 0, "戊": 1, "己": 0}
    remaining = {"甲": 10, "乙": 6, "丙": 10, "丁": 8, "戊": 5, "己": 8}
    return make_log(match_index=2, days=days, auction_scores=auction_scores,
                    scores=auction_scores, remaining_coins=remaining, game=ANALYZE_GAME).to_dict()


def make_matches() -> list[dict]:
    return [
        {"match": make_match_1(), "standings_after_match": []},
        {"match": make_match_2(), "standings_after_match": []},
    ]


def rows_by_name(stats: dict) -> dict:
    return {row["name"]: row for row in stats["rows"]}


def test_score_matrices_and_top_match_counts():
    stats = collect_player_stats(make_matches())
    assert stats["match_labels"] == ["正赛第 1 局", "正赛第 2 局"]
    assert stats["auction_score_matrix"]["乙"] == [1, 1]
    assert stats["score_matrix"]["乙"] == [3, 1]
    assert stats["score_matrix"]["己"] == [1, 0]
    rows = rows_by_name(stats)
    # 首局乙超级命中后独占最高，次局乙/戊并列最高。
    assert rows["乙"]["tied_top_matches"] == 1
    assert rows["乙"]["solo_top_matches"] == 1
    assert rows["己"]["tied_top_matches"] == 0
    assert rows["戊"]["tied_top_matches"] == 1
    assert rows["甲"]["tied_top_matches"] == 0
    assert sum(row["solo_top_matches"] for row in stats["rows"]) == 1
    # 排序：总分降序，同分按名字（己 U+5DF1 < 戊 U+620A）
    assert [row["name"] for row in stats["rows"]] == ["乙", "己", "戊", "丁", "丙", "甲"]


@pytest.mark.parametrize("all_void", [False, True])
def test_solo_top_and_all_zero_tied_matches(all_void, tmp_path, capsys):
    days = [
        make_day(day, [2, 2] if all_void else [2, 1], 1,
                 None if all_void else "甲", None if all_void else 2,
                 [2] if all_void else [], 0 if all_void else 1, is_void=all_void)
        for day in (1, 2)
    ]
    points = {"甲": 0 if all_void else 2, "乙": 0}
    match = make_log(days=days, auction_scores=points, scores=points,
                     game=ANALYZE_GAME).to_dict()
    rows = rows_by_name(collect_player_stats([{"match": match}]))
    assert rows["甲"]["solo_top_matches"] == (0 if all_void else 1)
    assert rows["乙"]["solo_top_matches"] == 0
    assert rows["甲"]["tied_top_matches"] == int(all_void)
    assert rows["乙"]["tied_top_matches"] == int(all_void)
    if all_void:
        assert rows["甲"]["avg_bid"] is None
        write_matches(tmp_path, [{"match": match}])
        analyze(tmp_path)
        output = capsys.readouterr().out
        assert output.count("单局最高总分：独占0局，并列1局") == 2
        assert output.count("平均正常出价（不含流局日）：—") == 2


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
    # 甲：无超级；撞车扣款 = 5+3；均出 = (5+3+0+0)/4；出 0 两次
    assert rows["甲"]["super_uses"] == 0 and rows["甲"]["super_hits"] == 0
    assert rows["甲"]["super_net_coins"] == 0
    assert rows["甲"]["collision_burn"] == 8
    assert rows["甲"]["normal_collision_burn"] == 3
    assert rows["甲"]["unique_sniped_burn"] == 5
    assert rows["甲"]["avg_bid"] == 2.0
    assert rows["甲"]["zero_bids"] == 2
    # 丙：超 1 用 0 中（狙 6 空）；无撞车扣款；出 0 一次
    assert rows["丙"]["super_uses"] == 1 and rows["丙"]["super_hits"] == 0
    assert rows["丙"]["super_net_coins"] == -2
    assert rows["丙"]["collision_burn"] == 0
    assert rows["丙"]["avg_bid"] == 1.25
    assert rows["丙"]["zero_bids"] == 1
    # 丁：无超级；撞车扣款 = 2（第 2 局第 2 天互撞）
    assert rows["丁"]["super_uses"] == 0 and rows["丁"]["super_hits"] == 0
    assert rows["丁"]["collision_burn"] == 2
    assert rows["丁"]["avg_bid"] == 2.0
    assert rows["丁"]["zero_bids"] == 0
    # 戊 / 己：各 1 胜；戊 出 0 一次、己 撞车扣款 2 且出 0 一次
    assert rows["戊"]["total_score"] == 1 and rows["戊"]["zero_bids"] == 1
    assert rows["己"]["total_score"] == 1 and rows["己"]["collision_burn"] == 2
    assert rows["己"]["avg_bid"] == 3.0 and rows["己"]["zero_bids"] == 1


def test_analyze_prints_player_section(tmp_path, capsys):
    run_dir = tmp_path / "20260909_120000_ai"
    run_dir.mkdir()
    write_matches(run_dir, make_matches())
    analyze(run_dir)
    output = capsys.readouterr().out
    sections = ["比赛概况", "经济与超级出价", "玩家明细", "特殊情况"]
    assert [output.index(section) for section in sections] == sorted(output.index(section) for section in sections)
    assert "各局顺序：正赛第 1 局 / 正赛第 2 局" in output
    yi = output.split("\n乙\n", 1)[1].split("\n\n", 1)[0]
    for text in ["总分：4分（拍卖2分，超级命中2分）", "各局总分：3 / 1", "各局拍卖分：1 / 1",
                 "单局最高总分：独占1局，并列1局",
                 "超级出价：使用1次，命中1次（100%）", "超级出价净金币变化：+1金币",
                 "撞车损失：3金币（正常报价互撞3，唯一报价被狙0）",
                 "平均正常出价（不含流局日）：3.50", "各局结束余额：4 / 6 金币",
                 "正常出价为零：0次"]:
        assert text in yi
    jia = output.split("\n甲\n", 1)[1].split("\n\n", 1)[0]
    assert "超级出价：使用0次，命中0次\n" in jia
    assert "%" not in jia
    assert "正常出价为零：2次" in jia
    assert "撞车损失：8金币（正常报价互撞3，唯一报价被狙5）" in jia
    assert "撞车损失：15 金币（正常报价互撞 10，唯一报价被狙 5；只计正常出价）" in output
    assert "局末平均剩余金币：7.08（最低 2，最高 10）" in output
    assert "成本消耗：4 金币" in output
    assert "悬赏发放：3 金币" in output
    assert "超级出价净金币变化：-1 金币" in output
    assert "超级出价净金币变化：-2金币" in output
    assert "命中原本互撞价 0 次（按超级使用笔数计，包含流局）" in output
    assert "使用超级出价：1 天" in output
    assert "改变当日结果：1 天" in output
    assert "含有效正常报价及超级数值" in output
    assert "获胜价平均" in output


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
        make_day(1, bids, 1, winner, winning_bid, collisions, int(winner is not None),
                 is_void=winner is None, super_bids=super_bids),
        make_day(2, [0] * len(bids), 1, None, None, [0], is_void=True),
    ]
    names = [bid.player for bid in days[0].bids if not bid.is_super]
    auction_scores = {name: int(name == winner) for name in names}
    # 各超级命中独立得 2 分，含流局日。
    scores = {name: auction_scores[name] + (2 if super_bids.get(name, (0, False))[1] else 0)
              for name in names}
    matches = [{"match": make_log(days=days, auction_scores=auction_scores,
                                  scores=scores, game=ANALYZE_GAME).to_dict()}]
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


def test_super_result_days_without_super_usage(tmp_path, capsys):
    matches = [{"match": make_match_2()}]
    matches[0]["match"]["match_index"] = 1
    stats = collect_player_stats(matches)
    assert stats["super_days"] == 0
    assert stats["super_changed_days"] == 0
    assert stats["super_collision_hits"] == 0
    assert all(row["super_net_coins"] == 0 for row in stats["rows"])
    write_matches(tmp_path, matches)
    analyze(tmp_path)
    output = capsys.readouterr().out
    assert "超级出价：使用 0 次，命中 0 次\n" in output
    assert "命中率" not in output
    assert "（0%）" not in output


def test_super_collision_hits_include_void_days(tmp_path, capsys):
    days = [
        make_day(1, [0, 0, 3, 3], 1, None, None, [0, 3], is_void=True,
                 super_bids={"乙": (3, True), "丙": (0, True), "丁": (0, True)}),
        make_day(2, [0, 0, 0, 0], 1, None, None, [0], is_void=True),
    ]
    matches = [{"match": make_log(days=days, auction_scores=dict.fromkeys(["甲", "乙", "丙", "丁"], 0),
                                  scores={"甲": 0, "乙": 2, "丙": 2, "丁": 2},
                                  game=ANALYZE_GAME).to_dict()}]
    stats = collect_player_stats(matches)
    assert stats["super_collision_hits"] == 3  # 同一天、两个价位，按三笔使用分别计数。
    assert stats["super_changed_days"] == 0
    write_matches(tmp_path, matches)
    analyze(tmp_path)
    output = capsys.readouterr().out
    assert "命中原本互撞价 3 次（按超级使用笔数计，包含流局）" in output
    assert "撞车损失：6 金币（正常报价互撞 6，唯一报价被狙 0；只计正常出价）" in output


def test_super_result_days_do_not_simulate_future():
    game = replace(ANALYZE_GAME, daily_salary=0)
    days = [
        make_day(1, [0, 5, 3], 1, "乙", 5, [], 1, super_bids={"甲": (6, False)}),
        make_day(2, [0, 0, 3], 1, "丙", 3, [0], 1),
    ]
    # 移除首日超级后只重判首日，不重算次日余额或报价。
    points = {"甲": 0, "乙": 1, "丙": 1}
    log = make_log(days=days, auction_scores=points, scores=points, game=game).to_dict()
    stats = collect_player_stats([{"match": log}])
    assert stats["super_days"] == 1
    assert stats["super_changed_days"] == 0


def write_matches(run_dir, matches, *, run_type="bot", players=None):
    first = matches[0]["match"]
    roster = deepcopy(players) if players is not None else [{"name": name} for name in first["auction_scores"]]
    if run_type != "bot":
        for player in roster:
            player.setdefault("identity_model", {"model": "synthetic-identity"})
            player.setdefault("execution_model", {"protocol": "chat", "model": "synthetic-execution"})
    paths = initialize_run(run_dir, {"run_type": run_type,
                                     "mode": "test_model" if run_type == "ai-test" else "regular",
                                     "game": first["game"], "players": roster}, "test rules")
    auction_scores_history = {name: [] for name in first["auction_scores"]}
    scores_history = {name: [] for name in first["scores"]}
    for payload in sorted(matches, key=lambda payload: payload["match"]["match_index"]):
        # 用生产排名函数准备磁盘元数据；每日和每局分数始终由场景手写。
        for name in auction_scores_history:
            auction_scores_history[name].append(payload["match"]["auction_scores"].get(name, 0))
            scores_history[name].append(payload["match"]["scores"].get(name, 0))
        payload["standings_after_match"] = [asdict(entry) for entry in compute_standings(
            auction_scores_history, scores_history=scores_history)]
        paths.match(payload["match"]["match_index"]).write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )


@pytest.mark.parametrize("latest_type,older_type", [("ai-test", "ai"), ("ai", "ai-test")])
def test_default_analysis_selects_latest_real_ai(tmp_path, monkeypatch, latest_type, older_type):
    monkeypatch.setattr("auction_game.reporting.analyze.LOG_DIR", tmp_path)
    write_matches(tmp_path / f"20261016_110000_{older_type}", make_matches(), run_type=older_type)
    latest = tmp_path / f"20261016_120000_{latest_type}"
    write_matches(latest, make_matches(), run_type=latest_type)
    for run_type in ("bot", "offline"):
        write_matches(tmp_path / f"20261016_130000_{run_type}", make_matches(), run_type=run_type)
    (tmp_path / "20261016_140000_ai").mkdir()  # 还没有完成的局
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
               for index, name in enumerate(make_match_1()["auction_scores"], 1)]
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
    with pytest.raises(SystemExit, match="没有.*ai 或 ai-test"):
        find_latest_ai_run()


def test_cli_reports_run_without_matches(tmp_path, monkeypatch):
    initialize_run(tmp_path, {"run_type": "bot", "mode": "regular", "game": ANALYZE_GAME.to_dict(),
                              "players": [{"name": "甲"}, {"name": "乙"}]}, "test rules")
    monkeypatch.setattr("sys.argv", ["analyze", str(tmp_path)])
    with pytest.raises(SystemExit, match="没有 data/matches"):
        main()


def test_nondefault_snapshot_drives_economy_cap_and_playoff_label(tmp_path, capsys, monkeypatch):
    game = replace(TEST_GAME, initial_coins=20, daily_salary=4, daily_pool=(2, 4, 3),
                   max_bid=9, super_cost=1, super_bounty=5, regular_matches=2, max_playoffs=4)
    days = [
        make_day(1, [9, 7, 5], 2, "乙", 7, [9], 2, super_bids={"乙": (9, True)}),
        make_day(2, [2, 2, 0], 4, "丙", 0, [2], 4),
        make_day(3, [9, 3, 1], 3, "甲", 9, [], 3),
    ]
    log = make_log(match_index=3, is_playoff=True, game=game, days=days,
                   auction_scores={"甲": 3, "乙": 2, "丙": 4},
                   scores={"甲": 3, "乙": 4, "丙": 4},
                   remaining_coins={"甲": 8, "乙": 23, "丙": 28}).to_dict()
    stats = collect_player_stats([{"match": log}])
    assert stats["match_labels"] == ["加赛第 1 局（总第 3 局）"]
    assert rows_by_name(stats)["丙"]["tied_top_matches"] == 1
    assert rows_by_name(stats)["乙"]["avg_bid"] == 4
    disk_matches = [{"match": {**deepcopy(log), "match_index": index, "is_playoff": index == 3}}
                    for index in (1, 2, 3)]
    write_matches(tmp_path, disk_matches)
    # 分析不能依赖当前配置；当前目录甚至放置无效的 game.yaml。
    (tmp_path / "game.yaml").write_text("not: valid game settings", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    analyze(tmp_path)
    output = capsys.readouterr().out
    assert "获胜价=9（出价上限）占比 33.3%" in output
    assert "成本消耗：3 金币" in output
    assert "悬赏发放：15 金币" in output
    assert "超级出价净金币变化：+12 金币" in output
    assert "加赛第 1 局（总第 3 局）" in output
    assert rows_by_name(stats)["乙"]["super_net_coins"] == 4
    assert "局末平均剩余金币：19.67（最低 8，最高 28）" in output
    assert "全天总体：平均 4.70（30 笔）" in output  # 聚合均价包含超级出价数值


def test_loading_allows_completed_matches_from_unfinished_run(tmp_path, capsys):
    match = make_match_1()
    match["game"]["regular_matches"] = 5
    matches = [{"match": match}]
    write_matches(tmp_path, matches)
    assert load_match_logs(tmp_path) == matches
    analyze(tmp_path)
    assert "局数：1（正赛 1 + 加赛 0）" in capsys.readouterr().out


def test_loading_orders_matches_by_file_number(tmp_path):
    matches = make_matches()
    matches = [{"match": {**deepcopy(matches[index % 2]["match"]), "match_index": index + 1}}
               for index in range(10)]
    write_matches(tmp_path, list(reversed(matches)))
    assert load_match_logs(tmp_path) == matches


def test_mean_bids_exclude_void_days(tmp_path, capsys):
    days = [
        make_day(1, [2, 2, 2, 2, 2, 2], 1, None, None, [2], is_void=True),
        make_day(2, [0, 3, 0, 2, 5, 4], 1, "戊", 5, [0], 1),
    ]
    points = {"甲": 0, "乙": 0, "丙": 0, "丁": 0, "戊": 1, "己": 0}
    match = make_log(days=days, auction_scores=points, scores=points,
                     game=ANALYZE_GAME).to_dict()
    rows = rows_by_name(collect_player_stats([{"match": match}]))
    assert rows["甲"]["avg_bid"] == 0
    assert rows["乙"]["avg_bid"] == 3
    assert rows["甲"]["collision_burn"] == 2  # 流局日撞车仍然扣币
    assert rows["甲"]["normal_collision_burn"] == 2
    assert rows["甲"]["unique_sniped_burn"] == 0
    write_matches(tmp_path, [{"match": match}])
    analyze(tmp_path)
    output = capsys.readouterr().out
    assert "第 1 天：出价 —（全流局），奖池 1.00" in output
    assert "第 2 天：出价 2.33（6 笔），奖池 1.00" in output
    assert "全天总体：平均 2.33（6 笔）" in output


def test_aggregate_zero_bids_include_super_but_player_zero_bids_only_normal(tmp_path, capsys):
    days = [
        make_day(1, [0, 0, 1, 1, 0, 0], 1, None, None, [0, 1], is_void=True,
                 super_bids={"丙": (0, True)}),
        make_day(2, [1, 0, 2, 3, 4, 5], 1, "己", 5, [0], 1,
                 super_bids={"甲": (0, True)}),
    ]
    match = make_log(days=days, auction_scores={"甲": 0, "乙": 0, "丙": 0, "丁": 0, "戊": 0, "己": 1},
                     scores={"甲": 2, "乙": 0, "丙": 2, "丁": 0, "戊": 0, "己": 1},
                     game=ANALYZE_GAME).to_dict()
    rows = rows_by_name(collect_player_stats([{"match": match}]))
    assert rows["甲"]["zero_bids"] == 1  # 流局日正常 0 计入，两笔超级 0 不计
    assert rows["乙"]["zero_bids"] == 2
    write_matches(tmp_path, [{"match": match}])
    analyze(tmp_path)
    assert "出 0 共 7 笔（含流局日和超级出价），其中 0 价获胜 0 次" in capsys.readouterr().out


def test_total_score_ranking_can_reverse_auction_score_ranking():
    game = replace(ANALYZE_GAME, daily_pool=(1, 1), super_hit_score=3)
    days = [
        make_day(1, [5, 4, 3], 1, "甲", 5, [3], 1, super_bids={"乙": (3, True)}),
        make_day(2, [5, 1, 0], 1, "甲", 5, [], 1),
    ]
    match = make_log(days=days, auction_scores={"甲": 2, "乙": 0, "丙": 0},
                     scores={"甲": 2, "乙": 3, "丙": 0}, game=game).to_dict()
    stats = collect_player_stats([{"match": match}])
    assert [row["name"] for row in stats["rows"]] == ["乙", "甲", "丙"]
    assert rows_by_name(stats)["乙"]["solo_top_matches"] == 1
    assert stats["auction_score_matrix"]["乙"] == [0]
    assert stats["score_matrix"]["乙"] == [3]


def test_offline_execution_is_fake_even_with_configured_protocol_model(tmp_path, capsys):
    players = [{"name": name, "identity_model": {"model": "identity"},
                "execution_model": {"protocol": "responses", "model": "configured-model"}}
               for name in make_match_1()["auction_scores"]]
    write_matches(tmp_path, make_matches(), run_type="offline", players=players)
    analyze(tmp_path)
    output = capsys.readouterr().out
    assert "实际执行为假模型" in output
    assert "配置模型 responses / configured-model" in output
    assert "常规阵容结果" not in output
    assert "→ 实际 responses" not in output
