"""analyze.py 玩家维度统计的零网络测试。

合成对局（两局，每天一行出价 = NAMES 顺序）：

第 1 局
  第 1 天：出价 [5,4,3,2,1,0]；乙 超级狙 5（命中）、丙 超级狙 6（空）；
           5 被撞车（甲 放血 5），乙 以 4 获胜。
  第 2 天：出价 [3,3,1,2,0,6]；3 互撞（甲、乙 放血 3），己 以 6 获胜。
  胜场：乙 1、己 1（局冠军 乙/己 各 0.5）。
  第 2 局
  第 1 天：出价 [0,3,0,2,5,4]；甲 非法提交整单转正常 0、无超级；
           戊 以 5 获胜。
  第 2 天：出价 [0,4,1,2,3,2]；2 互撞（丁、己 放血 2），乙 以 4 获胜。
  胜场：乙 1、戊 1（局冠军 乙/戊 各 0.5）。

手算期望值见各断言注释；局末余额按日志 game（初始 7、日工资 3、超级成本 2、悬赏 3）重放。
"""

import json
from dataclasses import replace

import pytest

from conftest import TEST_GAME, make_day, make_log

from analyze import analyze, collect_player_stats, load_match_logs, recompute_remaining_coins


def make_match_1() -> dict:
    days = [
        make_day(1, [5, 4, 3, 2, 1, 0], pool=1, winner="乙", winning_bid=4,
                 collision_values=[5], wins_taken=1,
                 super_bids={"乙": (5, True), "丙": (6, False)}),
        make_day(2, [3, 3, 1, 2, 0, 6], pool=1, winner="己", winning_bid=6,
                 collision_values=[3], wins_taken=1),
    ]
    wins = {"甲": 0, "乙": 1, "丙": 0, "丁": 0, "戊": 0, "己": 1}
    return make_log(match_index=1, days=days, wins=wins).to_dict()


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
    return make_log(match_index=2, days=days, wins=wins).to_dict()


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
    # 乙：超 1 用 1 中；放血 = 3（第 1 局第 2 天互撞）；均出 = (4+3+3+4)/4
    assert rows["乙"]["total_wins"] == 2
    assert rows["乙"]["super_uses"] == 1 and rows["乙"]["super_hits"] == 1
    assert rows["乙"]["collision_burn"] == 3
    assert rows["乙"]["avg_bid"] == 3.5
    assert rows["乙"]["end_coins"] == [4, 6]  # 局1: 7-4-2+3=4→+3-3=4；局2: 7→+3-4=6
    assert rows["乙"]["illegal"] == 0 and rows["乙"]["zero_bids"] == 0
    # 甲：无超级；放血 = 5+3；均出 = (5+3+0+0)/4；违规 1 笔；出 0 两次
    assert rows["甲"]["super_uses"] == 0 and rows["甲"]["super_hits"] == 0
    assert rows["甲"]["collision_burn"] == 8
    assert rows["甲"]["avg_bid"] == 2.0
    assert rows["甲"]["end_coins"] == [2, 10]  # 局1: 7-5=2→+3-3=2；局2: 7→+3=10
    assert rows["甲"]["illegal"] == 1 and rows["甲"]["zero_bids"] == 2
    # 丙：超 1 用 0 中（狙 6 空）；零放血；出 0 一次
    assert rows["丙"]["super_uses"] == 1 and rows["丙"]["super_hits"] == 0
    assert rows["丙"]["collision_burn"] == 0
    assert rows["丙"]["avg_bid"] == 1.25
    assert rows["丙"]["end_coins"] == [8, 10]
    assert rows["丙"]["zero_bids"] == 1
    # 丁：无超级；放血 = 2（第 2 局第 2 天互撞）
    assert rows["丁"]["super_uses"] == 0 and rows["丁"]["super_hits"] == 0
    assert rows["丁"]["collision_burn"] == 2
    assert rows["丁"]["avg_bid"] == 2.0
    assert rows["丁"]["end_coins"] == [10, 8]
    assert rows["丁"]["zero_bids"] == 0
    # 戊 / 己：各 1 胜；戊 出 0 一次、己 放血 2 且出 0 一次
    assert rows["戊"]["total_wins"] == 1 and rows["戊"]["end_coins"] == [10, 5]
    assert rows["戊"]["zero_bids"] == 1
    assert rows["己"]["total_wins"] == 1 and rows["己"]["collision_burn"] == 2
    assert rows["己"]["avg_bid"] == 3.0 and rows["己"]["end_coins"] == [4, 8]
    assert rows["己"]["zero_bids"] == 1


def test_playoff_match_label():
    log = make_log(match_index=6, is_playoff=True)
    stats = collect_player_stats([{"match": log.to_dict()}])
    assert stats["match_labels"] == ["加1"]


def make_old_format_match() -> dict:
    """旧格式（v4 及以前）：无 rules_version / collision_values / super_uses，
    超级出价混在 bids 里按笔数计撞车。乙 超级出 5 使 甲 的 5 撞车。"""
    return {
        "match": {
            "match_index": 1,
            "is_playoff": False,
            "wins": {"甲": 1, "乙": 0},
            "days": [{
                "day": 1, "pool": 1, "winner": "甲", "winning_bid": 5,
                "wins_taken": 1, "is_void": False, "transfer_recipient": None,
                "bids": [
                    {"player": "甲", "raw_bid": 5, "bid": 5, "status": "ok"},
                    {"player": "乙", "raw_bid": 3, "bid": 3, "status": "ok"},
                    {"player": "乙", "raw_bid": 5, "bid": 5, "status": "ok", "is_super": True},
                ],
            }],
        }
    }


@pytest.mark.parametrize("rules_version", [None, "v6", "v7"])
def test_old_format_is_rejected(rules_version):
    payload = make_old_format_match()
    payload["match"].update(initial_coins=7, max_bid=7)
    if rules_version:
        payload["match"]["rules_version"] = rules_version
    with pytest.raises(ValueError, match="缺少 game"):
        collect_player_stats([payload])


def test_analyze_prints_player_section(tmp_path, capsys):
    run_dir = tmp_path / "20260909_120000_ai"
    run_dir.mkdir()
    for index, payload in enumerate(make_matches(), start=1):
        (run_dir / f"match_{index}.json").write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )
    analyze(run_dir)
    output = capsys.readouterr().out
    # 新统计段存在且位于「单局 ≥3 胜场」列表之前
    assert "玩家维度统计" in output
    assert "明细（胜场 / 局冠军" in output
    assert output.index("玩家维度统计") < output.index("单局 ≥3 胜场")
    # 矩阵行与明细行内容
    assert "  乙：1 1" in output
    assert "  乙：胜2 局冠1 超1用1中（100%） 放血3 均出3.50 局末余额4/6 违规0笔 出0共0笔" in output
    assert "  甲：胜0 局冠0 超0用 放血8 均出2.00 局末余额2/10 违规1笔 出0共2笔" in output
    # 既有聚合输出保持不变
    assert "撞车放血：全场因撞车被没收" in output
    assert "获胜价平均" in output


def write_matches(run_dir, matches):
    for index, payload in enumerate(matches, start=1):
        (run_dir / f"match_{index}.json").write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )


def test_nondefault_snapshot_drives_economy_cap_and_playoff_label(tmp_path, capsys, monkeypatch):
    game = replace(TEST_GAME, initial_coins=20, daily_salary=4, daily_pool=(1, 2, 3),
                   max_bid=9, super_cost=1, super_bounty=5, regular_matches=2, max_playoffs=4)
    days = [
        make_day(1, [9, 7, 5], 1, "乙", 7, [9], 1, super_bids={"乙": (9, True)}),
        make_day(2, [2, 2, 0], 2, "丙", 0, [2], 2),
        make_day(3, [9, 3, 1], 3, "甲", 9, [], 3),
    ]
    log = make_log(match_index=3, is_playoff=True, game=game, days=days,
                   wins={"甲": 3, "乙": 1, "丙": 2}).to_dict()
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
    assert "获胜价=9（极点）占比 33.3%" in output
    assert "成本消耗 1 金币，悬赏印发 5 金币" in output
    assert "局末平均剩余金币：19.67（最低 8，最高 28）" in output
    assert "全天总体：平均 4.70（10 笔）" in output  # 聚合均价包含超级出价数值
    assert "单局 ≥3 胜场的玩家-局次数：1" in output


@pytest.mark.parametrize("field", list(TEST_GAME.to_dict()))
def test_missing_game_setting_is_rejected(field):
    match = make_match_1()
    del match["game"][field]
    with pytest.raises(ValueError, match=field):
        recompute_remaining_coins(match)


@pytest.mark.parametrize("section,field", [
    ("match", "game"), ("match", "wins"), ("match", "is_playoff"),
    ("day", "super_uses"), ("day", "collision_values"), ("day", "winning_bid"),
    ("bid", "status"), ("bid", "is_super"), ("super_use", "hit"),
])
def test_missing_current_log_field_is_rejected(tmp_path, section, field):
    match = make_match_1()
    records = {"match": match, "day": match["days"][0],
               "bid": match["days"][0]["bids"][0],
               "super_use": match["days"][0]["super_uses"][0]}
    del records[section][field]
    with pytest.raises(ValueError, match=field):
        collect_player_stats([{"match": match}])
    write_matches(tmp_path, [{"match": match}])
    with pytest.raises(ValueError, match=rf"match_1.json.*{field}"):
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


@pytest.mark.parametrize("field,value", [("max_bid", True), ("daily_salary", -1),
                                         ("daily_pool", []), ("regular_matches", 0)])
def test_invalid_game_snapshot_is_rejected(field, value):
    match = make_match_1()
    match["game"][field] = value
    with pytest.raises(ValueError, match=rf"日志 game 参数无效.*{field}"):
        recompute_remaining_coins(match)


def test_mean_bids_exclude_void_days_but_include_illegal_zero(tmp_path, capsys):
    days = [
        make_day(1, [2, 2, 2, 2, 2, 2], 1, None, None, [2], is_void=True),
        make_day(2, [0, 3, 0, 2, 5, 4], 2, "戊", 5, [0], 2),
    ]
    days[1].bids[0].status = "illegal"
    days[1].bids[0].raw_bid = -1
    match = make_log(days=days, wins={"甲": 0, "乙": 0, "丙": 0, "丁": 0, "戊": 2, "己": 0}).to_dict()
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
