"""analyze.py 玩家维度统计的零网络测试。

合成对局（两局，每天一行出价 = NAMES 顺序）：

第 1 局
  第 1 天：出价 [5,4,3,2,1,0]；乙 超级狙 5（命中）、丙 超级狙 6（空）；
           5 被撞车（甲 放血 5），乙 以 4 获胜。
  第 2 天：出价 [3,3,1,2,0,6]；3 互撞（甲、乙 放血 3），己 以 6 获胜。
  胜场：乙 1、己 1（局冠军 乙/己 各 0.5）。
第 2 局
  第 1 天：出价 [1,3,0,2,5,4]；甲 超级狙 7（空）；甲 该笔正常出价记 illegal；
           戊 以 5 获胜。
  第 2 天：出价 [0,4,1,2,3,2]；2 互撞（丁、己 放血 2），乙 以 4 获胜。
  胜场：乙 1、戊 1（局冠军 乙/戊 各 0.5）。

手算期望值见各断言注释；局末余额按 v7（初始 7、日工资 3、超级成本 2、悬赏 3）重放。
"""

import json

from conftest import make_day, make_log

from analyze import analyze, collect_player_stats


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
    day_1 = make_day(1, [1, 3, 0, 2, 5, 4], pool=1, winner="戊", winning_bid=5,
                     collision_values=[], wins_taken=1,
                     super_bids={"甲": (7, False)})
    day_1.bids[0].status = "illegal"  # 甲 第 1 天正常出价记为非法提交
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
    # 甲：超 1 用 0 中；放血 = 5+3；均出 = (5+3+1+0)/4；违规 1 笔；出 0 一次
    assert rows["甲"]["super_uses"] == 1 and rows["甲"]["super_hits"] == 0
    assert rows["甲"]["collision_burn"] == 8
    assert rows["甲"]["avg_bid"] == 2.25
    assert rows["甲"]["end_coins"] == [2, 8]  # 局1: 7-5=2→+3-3=2；局2: 7-2=5→+3=8
    assert rows["甲"]["illegal"] == 1 and rows["甲"]["zero_bids"] == 1
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


def test_old_format_degrades_hit_rate():
    stats = collect_player_stats([make_old_format_match()])
    rows = rows_by_name(stats)
    # 使用次数从 bids 的 is_super 统计；命中率不可得（None → 展示层显示降级标记）
    assert rows["乙"]["super_uses"] == 1
    assert rows["乙"]["super_hits"] is None
    assert rows["甲"]["super_uses"] == 0
    # 旧口径撞车：超级出价计入笔数，甲 的 5 因笔数 ≥2 被放血
    assert rows["甲"]["collision_burn"] == 5
    # 旧口径重放：初始 6，甲 撞车扣 5 → 1；乙 超级不扣款 → 6
    assert rows["甲"]["end_coins"] == [1]
    assert rows["乙"]["end_coins"] == [6]


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
    assert "  甲：胜0 局冠0 超1用0中（0%） 放血8 均出2.25 局末余额2/8 违规1笔 出0共1笔" in output
    # 既有聚合输出保持不变
    assert "撞车放血：全场因撞车被没收" in output
    assert "获胜价平均" in output
